// scheduler-lib/multicandidate/strategy.go
package multicandidate

import (
	"math"
	"math/rand"
	"sort"

	"example.com/scheduler-lib/types"
)

// Strategy name constants (used for factory lookup).
const (
	QualityFirst        = "QualityFirst"
	LatencyFirst        = "LatencyFirst"
	WeightedRandom      = "WeightedRandom"
	QualityFirstParSync = "QualityFirstParSync"
	LatencyFirstParSync = "LatencyFirstParSync"
)

// SelectionStrategy 选择策略接口
type SelectionStrategy interface {
	// Name 策略名称
	Name() string

	// Select 执行选择
	Select(candidates []types.CandidateNode, k int) []types.CandidateNode

	// Reorder 重排序候选
	Reorder(candidates []types.CandidateNode)
}

// === QualityFirst ===

// QualityFirstStrategy 质量优先策略（带冲突率惩罚）。
//
// 打分公式（与论文 Penalty 定义一致）：
//
//	adjusted = (1-p) * normalized_score + p * (1 - conflictRate)
//
// 其中 normalized_score = score / maxScore ∈ [0,1]，adjusted ∈ [0,1]。
//   - p=0：退化为纯按原始 Score 排序（Penalty 未启用）
//   - p=1：完全按 (1 - conflictRate) 排序，忽略原始得分
//
// 注：与旧 k8s-scheduler 中 applyConflictPenalty 使用的乘法形式
// score*(1-p*cr) 不等价；Phase 1 迁移顺便修正了公式与论文不一致的历史问题。
type QualityFirstStrategy struct {
	penaltyWeight float64
}

func NewQualityFirstStrategy(penaltyWeight float64) *QualityFirstStrategy {
	return &QualityFirstStrategy{penaltyWeight: penaltyWeight}
}

func (s *QualityFirstStrategy) Name() string {
	return QualityFirst
}

func (s *QualityFirstStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	s.Reorder(candidates)
	if len(candidates) < k {
		return candidates
	}
	return candidates[:k]
}

func (s *QualityFirstStrategy) Reorder(candidates []types.CandidateNode) {
	lenCandidates := len(candidates)
	if lenCandidates == 0 {
		return
	}

	maxScore := float64(candidates[0].Score)
	if maxScore <= 0 {
		// 防御除零 / 负分：此时所有节点归一化得分视作相等（=1），
		// 最终顺序仅由 (1-conflictRate) 决定。
		maxScore = 1
	}
	p := s.penaltyWeight

	type scoredCandidate struct {
		candidate     types.CandidateNode
		combinedScore float64
	}

	scored := make([]scoredCandidate, lenCandidates)
	for i, c := range candidates {
		normalizedScore := float64(c.Score) / maxScore
		if normalizedScore < 0 {
			normalizedScore = 0
		}
		combinedScore := (1-p)*normalizedScore + p*(1-c.ConflictRate)
		scored[i] = scoredCandidate{candidate: c, combinedScore: combinedScore}
	}

	sort.SliceStable(scored, func(i, j int) bool {
		return scored[i].combinedScore > scored[j].combinedScore
	})

	for i := 0; i < lenCandidates; i++ {
		candidates[i] = scored[i].candidate
		candidates[i].Rank = i
	}
}

// === LatencyFirst ===

// LatencyFirstStrategy 延迟优先策略（与 ParSync 论文对齐）。
//
// 排序规则：Freshness 降序优先；相同 Freshness 时按 Score 降序 tie-break。
// 该策略不使用 PenaltyWeight，因为其出发点是尽量选择状态最新鲜的分区节点，
// 通过"拿最新数据"而非"惩罚高冲突节点"来降低冲突。
type LatencyFirstStrategy struct{}

func NewLatencyFirstStrategy() *LatencyFirstStrategy {
	return &LatencyFirstStrategy{}
}

func (s *LatencyFirstStrategy) Name() string {
	return LatencyFirst
}

func (s *LatencyFirstStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	s.Reorder(candidates)
	if len(candidates) < k {
		return candidates
	}
	return candidates[:k]
}

func (s *LatencyFirstStrategy) Reorder(candidates []types.CandidateNode) {
	sort.SliceStable(candidates, func(i, j int) bool {
		if math.Abs(candidates[i].Freshness-candidates[j].Freshness) > types.Epsilon {
			return candidates[i].Freshness > candidates[j].Freshness
		}
		return candidates[i].Score > candidates[j].Score
	})
	for i := range candidates {
		candidates[i].Rank = i
	}
}

// === WeightedRandom ===

// WeightedRandomStrategy 加权随机策略。
//
// 权重公式与 QualityFirst 公式一致：
//
//	weight = (1-p) * normalized_score + p * (1 - conflictRate)  ∈ [0,1]
//
// 区别仅在于选择方式：QualityFirst 按 weight 降序确定性选；
// WeightedRandom 用 weight 做加权采样（不放回），让多调度器实例在
// 同一批 scores 下不必总是命中同一 tie-breaking 顺序，引入多样性分散压力。
//
// 选项：p=0 且 maxScore 相等时退化为纯均匀采样；totalWeight==0 时同样退化。
type WeightedRandomStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewWeightedRandomStrategy(seed int64, penaltyWeight float64) *WeightedRandomStrategy {
	return &WeightedRandomStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *WeightedRandomStrategy) Name() string {
	return WeightedRandom
}

func (s *WeightedRandomStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	if len(candidates) <= k {
		// 数量不足时全选，按原 Score 顺序输出（调用方已传入降序）。
		out := make([]types.CandidateNode, len(candidates))
		copy(out, candidates)
		for i := range out {
			out[i].Rank = i
		}
		return out
	}

	pool := make([]types.CandidateNode, len(candidates))
	copy(pool, candidates)

	maxScore := float64(pool[0].Score)
	if maxScore <= 0 {
		maxScore = 1
	}
	p := s.penaltyWeight

	result := make([]types.CandidateNode, 0, k)
	for len(result) < k && len(pool) > 0 {
		weights := make([]float64, len(pool))
		totalWeight := 0.0
		for i, c := range pool {
			normScore := float64(c.Score) / maxScore
			if normScore < 0 {
				normScore = 0
			}
			w := (1-p)*normScore + p*(1-c.ConflictRate)
			if w < 0 {
				w = 0
			}
			weights[i] = w
			totalWeight += w
		}

		// 加权采样（totalWeight==0 时退化为均匀采样）。
		var selectedIdx int
		if totalWeight == 0 {
			selectedIdx = s.rng.Intn(len(pool))
		} else {
			r := s.rng.Float64() * totalWeight
			cumulative := 0.0
			for i, w := range weights {
				cumulative += w
				if cumulative >= r {
					selectedIdx = i
					break
				}
			}
		}

		selected := pool[selectedIdx]
		selected.Rank = len(result)
		result = append(result, selected)
		pool = append(pool[:selectedIdx], pool[selectedIdx+1:]...)
	}

	return result
}

// Reorder 对 WeightedRandom 而言是 no-op（随机采样由 Select 完成）。
func (s *WeightedRandomStrategy) Reorder(candidates []types.CandidateNode) {}

// === QualityFirstParSync ===

// QualityFirstParSyncStrategy 论文 ParSync (ATC'21) 中的 Quality-first 策略
// （分区感知版）。算法：
//
//  1. 候选按 PartitionID 分组
//  2. 计算每个分区内节点 adjustedScore 的均值，按均值降序排列分区
//  3. 从最优分区起，组内按 adjustedScore 加权采样无放回，填到 K
//     不够则进入次优分区，依此类推
//
// adjustedScore = (1-p)*normalizedScore + p*(1-conflictRate)，与 QualityFirst
// 共用 conflict-rate penalty 通路；p=0 退化为论文纯净版。
//
// 与 QualityFirstStrategy（项目实验基线）的关键区别：
//   - QualityFirst 是 candidate-grain top-K（与 K8s default scheduler 行为对齐，
//     保证总是返回 K 个最高分节点）
//   - QualityFirstParSync 是 partition-grain，受分区结构限制可能选不出 K 个
//     最高分节点，但更忠于论文 §6.1 的 strategy 定义
type QualityFirstParSyncStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewQualityFirstParSyncStrategy(seed int64, penaltyWeight float64) *QualityFirstParSyncStrategy {
	return &QualityFirstParSyncStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *QualityFirstParSyncStrategy) Name() string { return QualityFirstParSync }

func (s *QualityFirstParSyncStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	maxScore := maxOriginalScore(candidates)
	groups := groupByPartition(candidates)

	// 按分区平均 adjustedScore 降序；同分均值时按 PartitionID 升序保持可重现
	// （groupByPartition 已按 PartitionID 升序，stable sort 保留该次序）。
	sort.SliceStable(groups, func(i, j int) bool {
		return partitionAvgAdjusted(groups[i], maxScore, s.penaltyWeight) >
			partitionAvgAdjusted(groups[j], maxScore, s.penaltyWeight)
	})
	return fillFromPartitions(groups, k, maxScore, s.penaltyWeight, s.rng)
}

// Reorder 对 ParSync 策略而言是 no-op：随机采样语义由 Select 完成；
// 直接 reorder 一段 candidates 没有"分区粒度"含义。
func (s *QualityFirstParSyncStrategy) Reorder(candidates []types.CandidateNode) {}

// === LatencyFirstParSync ===

// LatencyFirstParSyncStrategy 论文 ParSync (ATC'21) 中的 Latency-first 策略
// （分区感知版）。算法：
//
//  1. 候选按 PartitionID 分组
//  2. 按分区 freshness 降序排列分区（同分区节点 freshness 相同，取代表值）
//  3. 从最新鲜分区起，组内按 adjustedScore 加权采样无放回，填到 K
//     不够则回退到次新鲜分区，依此类推
//
// adjustedScore 同 QualityFirstParSync，让 PenaltyWeight>0 时也能影响 LF 的
// 分区内排序。论文原文未规定 LF 分区内选法；此处取"分数加权采样"以与 QF 对称。
//
// 与 LatencyFirstStrategy（项目实验基线）的关键区别：
//   - LatencyFirst 是 candidate-grain：全候选按 (freshness, score) 一次性 top-K，
//     freshness 相近时会跨分区混选
//   - LatencyFirstParSync 是 partition-grain：严格"填满最新鲜分区再回退"
type LatencyFirstParSyncStrategy struct {
	penaltyWeight float64
	rng           *rand.Rand
}

func NewLatencyFirstParSyncStrategy(seed int64, penaltyWeight float64) *LatencyFirstParSyncStrategy {
	return &LatencyFirstParSyncStrategy{
		penaltyWeight: penaltyWeight,
		rng:           rand.New(rand.NewSource(seed)),
	}
}

func (s *LatencyFirstParSyncStrategy) Name() string { return LatencyFirstParSync }

func (s *LatencyFirstParSyncStrategy) Select(candidates []types.CandidateNode, k int) []types.CandidateNode {
	if len(candidates) == 0 || k <= 0 {
		return nil
	}
	maxScore := maxOriginalScore(candidates)
	groups := groupByPartition(candidates)

	// 按分区 Freshness 降序（同分区内 Freshness 必相等，取首节点代表）。
	// 同 freshness 时按 PartitionID 升序保持可重现（stable sort）。
	sort.SliceStable(groups, func(i, j int) bool {
		return groups[i][0].Freshness > groups[j][0].Freshness
	})
	return fillFromPartitions(groups, k, maxScore, s.penaltyWeight, s.rng)
}

func (s *LatencyFirstParSyncStrategy) Reorder(candidates []types.CandidateNode) {}

// === ParSync helpers ===

// maxOriginalScore 取候选最大原始 Score 作为 normalize 分母。所有 Score<=0
// 时返回 1，让 normScore=1 不参与排序，最终顺序由 conflictRate 决定（与
// QualityFirstStrategy 的防御逻辑一致）。
func maxOriginalScore(candidates []types.CandidateNode) float64 {
	max := int64(0)
	for _, c := range candidates {
		if c.Score > max {
			max = c.Score
		}
	}
	if max <= 0 {
		return 1
	}
	return float64(max)
}

// groupByPartition 按 PartitionID 分组；分组内顺序保持入参顺序。返回的分组
// 序列按 PartitionID 升序，给后续 stable sort 提供可重现的 tie-break 基线。
func groupByPartition(candidates []types.CandidateNode) [][]types.CandidateNode {
	idx := make(map[int]int)
	groups := make([][]types.CandidateNode, 0)
	for _, c := range candidates {
		i, ok := idx[c.PartitionID]
		if !ok {
			i = len(groups)
			idx[c.PartitionID] = i
			groups = append(groups, []types.CandidateNode{})
		}
		groups[i] = append(groups[i], c)
	}
	sort.SliceStable(groups, func(i, j int) bool {
		return groups[i][0].PartitionID < groups[j][0].PartitionID
	})
	return groups
}

// adjustedScore 单节点综合得分：(1-p)*normScore + p*(1-conflictRate) ∈ [0,1]。
func adjustedScore(c types.CandidateNode, maxScore, p float64) float64 {
	norm := float64(c.Score) / maxScore
	if norm < 0 {
		norm = 0
	}
	return (1-p)*norm + p*(1-c.ConflictRate)
}

// partitionAvgAdjusted 分区内 adjustedScore 均值。空组返回 0（不会发生，
// groupByPartition 不会产出空分组，但留作防御）。
func partitionAvgAdjusted(group []types.CandidateNode, maxScore, p float64) float64 {
	if len(group) == 0 {
		return 0
	}
	sum := 0.0
	for _, c := range group {
		sum += adjustedScore(c, maxScore, p)
	}
	return sum / float64(len(group))
}

// fillFromPartitions 按 groups 顺序逐分区做加权采样无放回，将结果累积到长度
// k；当前分区不够则进入下一分区。Rank 字段按最终被选中的次序赋值（0=primary）。
func fillFromPartitions(groups [][]types.CandidateNode, k int, maxScore, p float64, rng *rand.Rand) []types.CandidateNode {
	result := make([]types.CandidateNode, 0, k)
	for _, group := range groups {
		if len(result) >= k {
			break
		}
		need := k - len(result)
		picked := weightedSampleNoReplace(group, need, maxScore, p, rng)
		result = append(result, picked...)
	}
	for i := range result {
		result[i].Rank = i
	}
	return result
}

// weightedSampleNoReplace 在 pool 内做 n 次"按 adjustedScore 加权"无放回采样。
// 当 totalWeight==0（罕见：所有节点 adjustedScore 都为 0）时退化为均匀采样；
// 若 n >= len(pool) 则全部返回（保持 pool 入参次序）。
func weightedSampleNoReplace(pool []types.CandidateNode, n int, maxScore, p float64, rng *rand.Rand) []types.CandidateNode {
	if n <= 0 || len(pool) == 0 {
		return nil
	}
	if n >= len(pool) {
		out := make([]types.CandidateNode, len(pool))
		copy(out, pool)
		return out
	}
	work := make([]types.CandidateNode, len(pool))
	copy(work, pool)

	out := make([]types.CandidateNode, 0, n)
	for len(out) < n && len(work) > 0 {
		weights := make([]float64, len(work))
		total := 0.0
		for i, c := range work {
			w := adjustedScore(c, maxScore, p)
			if w < 0 {
				w = 0
			}
			weights[i] = w
			total += w
		}
		var idx int
		if total == 0 {
			idx = rng.Intn(len(work))
		} else {
			r := rng.Float64() * total
			cum := 0.0
			for i, w := range weights {
				cum += w
				if cum >= r {
					idx = i
					break
				}
			}
		}
		out = append(out, work[idx])
		work = append(work[:idx], work[idx+1:]...)
	}
	return out
}
