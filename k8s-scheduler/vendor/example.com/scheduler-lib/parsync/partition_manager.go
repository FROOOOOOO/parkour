package parsync

import (
	"hash/fnv"
	"sync"
)

// PartitionManager 分区管理器（纯算法，不依赖 K8s）
type PartitionManager struct {
	mu sync.RWMutex

	numPartitions int

	// 节点到分区的映射
	nodeToPartition map[string]int

	// 分区到节点列表的映射
	partitionToNodes map[int][]string
}

// NewPartitionManager 创建分区管理器
func NewPartitionManager(numPartitions int) *PartitionManager {
	if numPartitions <= 0 {
		numPartitions = 4
	}

	pm := &PartitionManager{
		numPartitions:    numPartitions,
		nodeToPartition:  make(map[string]int),
		partitionToNodes: make(map[int][]string),
	}

	// 初始化分区
	for i := 0; i < numPartitions; i++ {
		pm.partitionToNodes[i] = make([]string, 0)
	}

	return pm
}

// AssignNode 分配节点到分区。
//
// 策略：贪心选择当前节点数最少的分区（least-loaded greedy），平手时选分区 ID 最小者。
// 相较早期的 `FNV-1a mod M` 方案，贪心策略：
//   - 输出分布完全均匀（任一时刻 max-min ≤ 1），消除 FNV 尾节点倾斜导致的快照大小不均
//   - 零迁移：已分配节点仍通过缓存命中直接返回原分区，语义与 FNV 版一致
//   - 不再依赖节点命名的均匀性（FNV 对 `kwok-node-XXXX` 均匀，对 EC2/GKE 随机命名未验证）
//
// 幂等：对同一 nodeName 多次调用返回相同值（首次计算后写入 nodeToPartition 缓存）。
func (pm *PartitionManager) AssignNode(nodeName string) int {
	pm.mu.Lock()
	defer pm.mu.Unlock()

	// 检查是否已分配
	if partitionID, exists := pm.nodeToPartition[nodeName]; exists {
		return partitionID
	}

	// 贪心：挑选当前节点数最少的分区。按升序遍历分区 ID 保证平手时选小 ID，
	// 结果确定性（同样的节点加入顺序产生同样的分配），便于复现实验。
	bestPartition := 0
	bestCount := -1
	for pid := 0; pid < pm.numPartitions; pid++ {
		count := len(pm.partitionToNodes[pid])
		if bestCount < 0 || count < bestCount {
			bestPartition = pid
			bestCount = count
		}
	}

	pm.nodeToPartition[nodeName] = bestPartition
	pm.partitionToNodes[bestPartition] = append(pm.partitionToNodes[bestPartition], nodeName)

	return bestPartition
}

// SeedAssignment pre-populates the internal node→partition map with an
// existing (node, partition) binding observed from external persistence
// (e.g. Dispatcher startup scanning nodes that already carry the
// `para-scheduler.io/partition-id` label). Without this, the greedy
// AssignNode would see an empty map after Dispatcher restart and bias all
// new-node placements toward partition 0 until counts re-converge.
//
// Idempotent: if the node is already known, the seed is ignored (the
// existing assignment wins — normally identical, but if external data
// disagrees we trust the first observation).
//
// partitionID must be in [0, numPartitions). Out-of-range values are
// silently dropped (defensive: avoids map blowups from corrupted labels).
func (pm *PartitionManager) SeedAssignment(nodeName string, partitionID int) {
	if partitionID < 0 || partitionID >= pm.numPartitions || nodeName == "" {
		return
	}
	pm.mu.Lock()
	defer pm.mu.Unlock()
	if _, exists := pm.nodeToPartition[nodeName]; exists {
		return
	}
	pm.nodeToPartition[nodeName] = partitionID
	pm.partitionToNodes[partitionID] = append(pm.partitionToNodes[partitionID], nodeName)
}

// RemoveNode 移除节点
func (pm *PartitionManager) RemoveNode(nodeName string) {
	pm.mu.Lock()
	defer pm.mu.Unlock()

	partitionID, exists := pm.nodeToPartition[nodeName]
	if !exists {
		return
	}

	delete(pm.nodeToPartition, nodeName)

	// 从分区节点列表中移除
	nodes := pm.partitionToNodes[partitionID]
	for i, name := range nodes {
		if name == nodeName {
			pm.partitionToNodes[partitionID] = append(nodes[:i], nodes[i+1:]...)
			break
		}
	}
}

// GetPartitionID 获取节点所属分区
func (pm *PartitionManager) GetPartitionID(nodeName string) int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	if partitionID, exists := pm.nodeToPartition[nodeName]; exists {
		return partitionID
	}

	// 未分配时计算（不持久化）
	h := fnv.New32a()
	h.Write([]byte(nodeName))
	return int(h.Sum32() % uint32(pm.numPartitions))
}

// GetPartitionNodes 获取分区内的所有节点
func (pm *PartitionManager) GetPartitionNodes(partitionID int) []string {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	nodes := pm.partitionToNodes[partitionID]
	result := make([]string, len(nodes))
	copy(result, nodes)
	return result
}

// GetAllPartitions 获取所有分区 ID
func (pm *PartitionManager) GetAllPartitions() []int {
	partitions := make([]int, pm.numPartitions)
	for i := 0; i < pm.numPartitions; i++ {
		partitions[i] = i
	}
	return partitions
}

// GetPartitionCount 获取分区数量
func (pm *PartitionManager) GetPartitionCount() int {
	return pm.numPartitions
}

// GetNodeCount 获取节点总数
func (pm *PartitionManager) GetNodeCount() int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return len(pm.nodeToPartition)
}

// GetPartitionNodeCount 获取分区内节点数量
func (pm *PartitionManager) GetPartitionNodeCount(partitionID int) int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return len(pm.partitionToNodes[partitionID])
}

// RebalanceCheck 检查分区是否平衡
func (pm *PartitionManager) RebalanceCheck() (balanced bool, maxDiff int) {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	if pm.numPartitions == 0 {
		return true, 0
	}

	minCount := int(^uint(0) >> 1) // MaxInt
	maxCount := 0

	for _, nodes := range pm.partitionToNodes {
		count := len(nodes)
		if count < minCount {
			minCount = count
		}
		if count > maxCount {
			maxCount = count
		}
	}

	diff := maxCount - minCount
	// 允许 ±1 的偏差
	return diff <= 1, diff
}
