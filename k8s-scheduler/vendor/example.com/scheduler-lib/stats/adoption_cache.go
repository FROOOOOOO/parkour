package stats

import (
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// AdoptionStatsCache 采纳统计缓存
type AdoptionStatsCache struct {
	mu sync.RWMutex

	// 配置
	config *StatsConfig

	// 全局统计：P[i] = 第 i 个候选被采纳的次数
	// P[n+1] = 全部失败的次数
	globalCounts []int64

	// 按分区统计
	partitionCounts map[int][]int64

	// 按节点统计（用于节点级别的概率调整）
	nodeStats map[string]*NodeAdoptionStats

	// 滑动窗口（用于计算近期成功率）
	globalWindow     *SlidingWindow
	partitionWindows map[int]*SlidingWindow

	// 概率计算器
	calculator *ProbabilityCalculator

	// 统计摘要
	summary *types.AdoptionStats

	// 更新时间
	lastUpdateTime time.Time
}

// StatsConfig 统计配置
type StatsConfig struct {
	// MaxCandidates 最大候选数（决定 counts 数组大小）
	MaxCandidates int

	// WindowSize 滑动窗口大小
	WindowSize int

	// EnableNodeStats 是否启用节点级统计
	EnableNodeStats bool

	// EnablePartitionStats 是否启用分区级统计
	EnablePartitionStats bool

	// DecayInterval 衰减间隔
	DecayInterval time.Duration

	// DecayFactor 衰减因子
	DecayFactor float64
}

// DefaultStatsConfig 默认配置
func DefaultStatsConfig() *StatsConfig {
	return &StatsConfig{
		MaxCandidates:        10,
		WindowSize:           1000,
		EnableNodeStats:      true,
		EnablePartitionStats: true,
		DecayInterval:        time.Minute,
		DecayFactor:          0.95,
	}
}

// NodeAdoptionStats 节点采纳统计
type NodeAdoptionStats struct {
	NodeName     string
	SuccessCount int64
	FailureCount int64
	LastSuccess  time.Time
	LastFailure  time.Time
	Window       *SlidingWindow
}

// NewAdoptionStatsCache 创建统计缓存
func NewAdoptionStatsCache(config *StatsConfig) *AdoptionStatsCache {
	if config == nil {
		config = DefaultStatsConfig()
	}

	// counts 数组大小 = MaxCandidates + 1 (最后一个是全部失败)
	countsSize := config.MaxCandidates + 1

	cache := &AdoptionStatsCache{
		config:           config,
		globalCounts:     make([]int64, countsSize),
		partitionCounts:  make(map[int][]int64),
		nodeStats:        make(map[string]*NodeAdoptionStats),
		globalWindow:     NewSlidingWindow(config.WindowSize),
		partitionWindows: make(map[int]*SlidingWindow),
		calculator:       NewProbabilityCalculator(),
		summary:          &types.AdoptionStats{RankDistribution: make([]int64, countsSize)},
		lastUpdateTime:   time.Now(),
	}

	return cache
}

// UpdateResult 更新绑定结果
//
// 三种调用语义（由 BindingResult.Success 与 NodeName 组合决定）：
//  (a) Success=true,  NodeName=X    → pod 绑定成功到节点 X。更新 global/partition/node。
//  (b) Success=false, NodeName=""   → pod 所有候选均失败 (ACF)。更新 global/partition（算入
//                                      pod-level FailureCount 与 globalCounts[last]）；
//                                      不触及 nodeStats（没有具体节点可归因）。
//  (c) Success=false, NodeName=X    → candidate-level 失败：单个候选节点 bind 失败，但
//                                      整个 pod 可能通过其他候选成功。**只更新 nodeStats[X]**，
//                                      不触及 global/partition 汇总——保持 SuccessCount/
//                                      FailureCount 的 "pod 级" 语义，避免一个 ACF pod
//                                      同时被计为 K+1 次失败 + 1 次 ACF。
//
// 这样得到：
//   - global/partition 维度：pod 级统计（匹配 Prometheus "ACF" 语义）
//   - node 维度：attempt 级统计（匹配 Prometheus "binder conflict" 语义）
//     供 penalty 机制按节点差异化扣分。
func (c *AdoptionStatsCache) UpdateResult(result types.BindingResult) {
	c.mu.Lock()
	defer c.mu.Unlock()

	isCandidateLevelFailure := !result.Success && result.NodeName != ""

	if !isCandidateLevelFailure {
		// 1. 更新全局统计（仅 pod-level 事件）
		if result.Success {
			rank := result.Rank
			if rank >= 0 && rank < len(c.globalCounts)-1 {
				c.globalCounts[rank]++
			}
			c.globalWindow.Add(true)
			c.summary.SuccessCount++
		} else {
			// 全部失败，更新最后一个计数
			c.globalCounts[len(c.globalCounts)-1]++
			c.globalWindow.Add(false)
			c.summary.FailureCount++
		}
		c.summary.TotalBindings++

		// 2. 更新分区统计（仅 pod-level 事件）
		if c.config.EnablePartitionStats {
			c.updatePartitionStats(result)
		}
	}

	// 3. 更新节点统计（所有带 NodeName 的事件：成功 或 candidate-level 失败）
	if c.config.EnableNodeStats && result.NodeName != "" {
		c.updateNodeStats(result)
	}

	// 4. 更新摘要
	c.updateSummary()

	c.lastUpdateTime = time.Now()
}

// updatePartitionStats 更新分区统计
func (c *AdoptionStatsCache) updatePartitionStats(result types.BindingResult) {
	partitionID := result.PartitionID

	// 确保分区计数数组存在
	if _, exists := c.partitionCounts[partitionID]; !exists {
		c.partitionCounts[partitionID] = make([]int64, len(c.globalCounts))
		c.partitionWindows[partitionID] = NewSlidingWindow(c.config.WindowSize / 4) // 超参数
	}

	counts := c.partitionCounts[partitionID]
	window := c.partitionWindows[partitionID]

	if result.Success {
		rank := result.Rank
		if rank >= 0 && rank < len(counts)-1 {
			counts[rank]++
		}
		window.Add(true)
	} else {
		counts[len(counts)-1]++
		window.Add(false)
	}
}

// updateNodeStats 更新节点统计
func (c *AdoptionStatsCache) updateNodeStats(result types.BindingResult) {
	nodeName := result.NodeName

	stats, exists := c.nodeStats[nodeName]
	if !exists {
		stats = &NodeAdoptionStats{
			NodeName: nodeName,
			Window:   NewSlidingWindow(100), // 超参数
		}
		c.nodeStats[nodeName] = stats
	}

	if result.Success {
		stats.SuccessCount++
		stats.LastSuccess = result.Timestamp
		stats.Window.Add(true)
	} else {
		stats.FailureCount++
		stats.LastFailure = result.Timestamp
		stats.Window.Add(false)
	}
}

// updateSummary 更新统计摘要
//
// ConflictRate 使用 SlidingWindow 的近期样本计算，反映"近期冲突率"而非
// 启动以来的累计冲突率。当窗口内无样本时返回 0（与原 CalculateConflictRate
// 冷启动行为一致，保证启动初期 Penalty 不会因伪数据立即生效）。
func (c *AdoptionStatsCache) updateSummary() {
	// 复制排名分布
	copy(c.summary.RankDistribution, c.globalCounts)

	// 计算平均尝试次数
	totalAttempts := int64(0)
	for rank, count := range c.globalCounts[:len(c.globalCounts)-1] {
		totalAttempts += int64(rank+1) * count
	}
	// 失败的按最大候选数计算
	totalAttempts += int64(c.config.MaxCandidates) * c.globalCounts[len(c.globalCounts)-1]

	if c.summary.TotalBindings > 0 {
		c.summary.AvgAttempts = float64(totalAttempts) / float64(c.summary.TotalBindings)
	}

	// 计算冲突率：优先用滑动窗口的近期样本；窗口无样本时退化为累计冲突率
	// （同时也覆盖累计也为 0 的冷启动场景，返回 0）
	if c.globalWindow != nil && c.globalWindow.Count() > 0 {
		c.summary.ConflictRate = 1.0 - c.globalWindow.SuccessRate()
	} else {
		c.summary.ConflictRate = c.calculator.CalculateConflictRate(
			c.summary.SuccessCount, c.summary.FailureCount)
	}
}

// GetNodeConflictRate 获取节点近期冲突率 [0,1]。
//
// 数据源：节点级 SlidingWindow 的 (1 - successRate)。冷启动或窗口无样本时
// 返回 0（与 updateSummary 中全局 ConflictRate 冷启动行为一致，避免启动
// 初期 Penalty 因伪数据立即生效）。
//
// 实现 types.AdoptionStatsProvider 接口。
func (c *AdoptionStatsCache) GetNodeConflictRate(nodeName string) float64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	s, ok := c.nodeStats[nodeName]
	if !ok || s.Window == nil || s.Window.Count() == 0 {
		return 0
	}
	return 1.0 - s.Window.SuccessRate()
}

// GetStats 获取统计摘要
func (c *AdoptionStatsCache) GetStats() *types.AdoptionStats {
	c.mu.RLock()
	defer c.mu.RUnlock()

	// 返回副本
	statsCopy := *c.summary
	statsCopy.RankDistribution = make([]int64, len(c.summary.RankDistribution))
	copy(statsCopy.RankDistribution, c.summary.RankDistribution)

	return &statsCopy
}

// GetNodeCountsMap 返回所有节点的 [attempts, conflicts] 统计，用于 Reporter 序列化到
// AdoptionStats CRD 的 NodeCounts 字段，供 Scheduler 端计算 per-node 冲突率。
//
// 默认只输出"曾有过失败"的节点（FailureCount > 0）。理由：
//   - HC-1 场景 10000 节点里绝大多数从未冲突，全部包含会让 status payload 涨到 MB 级
//   - 节点缺席时 Scheduler 可安全默认冲突率为 0（Penalty 公式里 conflict_rate=0 即"零惩罚"）
//
// 如需 "全部节点（含零冲突）"，传 includeZero=true。
//
// 返回值：
//   map[nodeName][]int64{attempts, conflicts}
//   其中 attempts = SuccessCount + FailureCount，conflicts = FailureCount。
//   两个计数都会经过 Decay() 随时间衰减（DecayFactor=0.95/DecayInterval），
//   因此反映"近期"冲突趋势，符合论文对 penalty 信号的时效性要求。
func (c *AdoptionStatsCache) GetNodeCountsMap(includeZero bool) map[string][]int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	out := make(map[string][]int64, len(c.nodeStats))
	for name, s := range c.nodeStats {
		if !includeZero && s.FailureCount == 0 {
			continue
		}
		out[name] = []int64{s.SuccessCount + s.FailureCount, s.FailureCount}
	}
	return out
}

// GetGlobalCounts 获取全局计数（用于调试）
func (c *AdoptionStatsCache) GetGlobalCounts() []int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	counts := make([]int64, len(c.globalCounts))
	copy(counts, c.globalCounts)
	return counts
}

// GetPartitionCounts 获取分区计数
func (c *AdoptionStatsCache) GetPartitionCounts(partitionID int) []int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if counts, exists := c.partitionCounts[partitionID]; exists {
		result := make([]int64, len(counts))
		copy(result, counts)
		return result
	}
	return nil
}

// LoadSummary 从持久化的统计摘要（通常来自 CRD）恢复累计计数，用于 Binder 进程重启后
// 防止 flush 把 CRD 上的历史数据覆写为 0。
//
// 恢复的字段：
//   - summary.TotalBindings / SuccessCount / FailureCount
//   - globalCounts（按较短长度拷贝，防止配置变更时越界）
//   - partitionCounts
//
// 不恢复（CRD 未持久化）：
//   - SlidingWindow —— 重启后窗口样本从空开始累积；updateSummary 在窗口空时
//     fallback 到 `CalculateConflictRate(summary.SuccessCount, summary.FailureCount)`，
//     即累计冲突率 = CRD 的历史值，语义一致
//   - nodeStats —— 与 CRD 不同步，历史上也不持久化
//
// 幂等性：覆盖式写入；多次调用等效于一次。
func (c *AdoptionStatsCache) LoadSummary(
	totalBindings, successCount, failureCount int64,
	globalCounts []int64,
	partitionCounts map[int][]int64,
) {
	c.mu.Lock()
	defer c.mu.Unlock()

	c.summary.TotalBindings = totalBindings
	c.summary.SuccessCount = successCount
	c.summary.FailureCount = failureCount

	if len(globalCounts) > 0 {
		n := len(c.globalCounts)
		if len(globalCounts) < n {
			n = len(globalCounts)
		}
		for i := 0; i < n; i++ {
			c.globalCounts[i] = globalCounts[i]
		}
		copy(c.summary.RankDistribution, c.globalCounts)
	}

	if len(partitionCounts) > 0 {
		for pid, counts := range partitionCounts {
			cp := make([]int64, len(counts))
			copy(cp, counts)
			c.partitionCounts[pid] = cp
		}
	}

	// 重新计算 summary 派生字段（ConflictRate、AvgAttempts）。SlidingWindow 为空时，
	// updateSummary 会走累计冲突率分支，使用我们刚刚恢复的 SuccessCount/FailureCount。
	c.updateSummary()
}

// Reset 重置统计
func (c *AdoptionStatsCache) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()

	for i := range c.globalCounts {
		c.globalCounts[i] = 0
	}
	c.partitionCounts = make(map[int][]int64)
	c.nodeStats = make(map[string]*NodeAdoptionStats)
	c.globalWindow.Reset()
	c.partitionWindows = make(map[int]*SlidingWindow)
	c.summary = &types.AdoptionStats{RankDistribution: make([]int64, len(c.globalCounts))}
	c.lastUpdateTime = time.Now()
}

// Decay 执行衰减（定期调用）
//
// 对所有累计量（globalCounts、partitionCounts、node 级 SuccessCount/FailureCount
// 以及 summary 的 SuccessCount/FailureCount/TotalBindings）按 DecayFactor 等比衰减，
// 使"近期"事件在统计中占更高权重。SlidingWindow 自身已通过 ring buffer 实现
// 近期窗口，不需要再衰减。
func (c *AdoptionStatsCache) Decay() {
	c.mu.Lock()
	defer c.mu.Unlock()

	factor := c.config.DecayFactor

	// 衰减全局计数
	for i := range c.globalCounts {
		c.globalCounts[i] = int64(float64(c.globalCounts[i]) * factor)
	}

	// 衰减分区计数
	for _, counts := range c.partitionCounts {
		for i := range counts {
			counts[i] = int64(float64(counts[i]) * factor)
		}
	}

	// 衰减节点统计
	for _, stats := range c.nodeStats {
		stats.SuccessCount = int64(float64(stats.SuccessCount) * factor)
		stats.FailureCount = int64(float64(stats.FailureCount) * factor)
	}

	// 衰减 summary 的累计字段，保持其与 globalCounts 一致
	// （否则 CRD 中的 SuccessCount/FailureCount 永远单调增长，Scheduler 侧
	// CalculateConflictRate 无法反映近期趋势）
	c.summary.SuccessCount = int64(float64(c.summary.SuccessCount) * factor)
	c.summary.FailureCount = int64(float64(c.summary.FailureCount) * factor)
	c.summary.TotalBindings = int64(float64(c.summary.TotalBindings) * factor)

	c.updateSummary()
}

// RunDecayLoop periodically invokes Decay() at the interval configured in
// StatsConfig.DecayInterval. Blocks until stop is closed. Callers typically
// invoke this in a goroutine at process start.
//
// When DecayInterval <= 0, the loop exits immediately (decay disabled).
func (c *AdoptionStatsCache) RunDecayLoop(stop <-chan struct{}) {
	if c.config.DecayInterval <= 0 {
		return
	}
	ticker := time.NewTicker(c.config.DecayInterval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			c.Decay()
		}
	}
}
