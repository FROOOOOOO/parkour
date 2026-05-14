package parsync

import (
	"fmt"
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// PartitionState 分区状态（通用结构，不依赖 K8s 类型）
type PartitionState struct {
	mu            sync.RWMutex
	partitions    map[int]*PartitionData
	freshnessCalc *FreshnessCalculator
}

// PartitionData 分区数据
type PartitionData struct {
	ID           int
	LastSyncTime time.Time
	Generation   int64
	// 节点数据（使用通用类型）
	Nodes map[string]*NodeData
}

// NodeData 节点数据（通用类型）
type NodeData struct {
	Name           string
	PartitionID    int
	LastUpdateTime time.Time
	// 资源信息（使用 map 避免依赖 K8s 类型）
	Allocatable map[string]int64 // resource name -> milli value
	Allocated   map[string]int64

	// Pod 分配
	PodAllocations map[string]*PodAllocation

	// 状态标记
	Ready    bool
	Cordoned bool

	// 自定义标签
	Labels map[string]string
}

// PodAllocation Pod 分配信息
type PodAllocation struct {
	PodKey     string
	Requests   map[string]int64 // resource name -> milli value
	AssignedAt time.Time
	IsAssumed  bool
}

// NewPartitionState 创建分区状态
func NewPartitionState(numPartitions int, syncPeriod time.Duration) *PartitionState {
	ps := &PartitionState{
		partitions:    make(map[int]*PartitionData),
		freshnessCalc: NewFreshnessCalculator(syncPeriod),
	}
	// 初始化分区
	for i := 0; i < numPartitions; i++ {
		ps.partitions[i] = &PartitionData{
			ID:    i,
			Nodes: make(map[string]*NodeData),
		}
	}

	return ps
}

// UpdatePartition 更新分区数据
func (ps *PartitionState) UpdatePartition(partitionID int, nodes map[string]*NodeData) {
	ps.mu.Lock()
	defer ps.mu.Unlock()
	partition, exists := ps.partitions[partitionID]
	if !exists {
		partition = &PartitionData{
			ID:    partitionID,
			Nodes: make(map[string]*NodeData),
		}
		ps.partitions[partitionID] = partition
	}

	partition.Nodes = nodes
	partition.LastSyncTime = time.Now()
	partition.Generation++
}

// GetPartitionData 获取分区数据
func (ps *PartitionState) GetPartitionData(partitionID int) *PartitionData {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	return ps.partitions[partitionID]
}

// GetNodeData 获取节点数据
func (ps *PartitionState) GetNodeData(nodeName string, partitionID int) *NodeData {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return nil
	}

	return partition.Nodes[nodeName]
}

// GetPartitionFreshness 获取分区新鲜度
func (ps *PartitionState) GetPartitionFreshness(partitionID int) float64 {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return 0
	}

	return ps.freshnessCalc.CalculateFreshness(partition.LastSyncTime)
}

// GetPartitionStaleness 获取分区陈旧度
func (ps *PartitionState) GetPartitionStaleness(partitionID int) time.Duration {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return 0
	}

	return time.Since(partition.LastSyncTime)
}

// GetPartitionInfo 获取分区信息
func (ps *PartitionState) GetPartitionInfo(partitionID int) *types.PartitionInfo {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return nil
	}

	return &types.PartitionInfo{
		ID:           partitionID,
		NodeCount:    len(partition.Nodes),
		LastSyncTime: partition.LastSyncTime,
		Generation:   partition.Generation,
		Staleness:    time.Since(partition.LastSyncTime),
	}
}

// AssumePod 假设 Pod 调度到节点
func (ps *PartitionState) AssumePod(podKey string, nodeName string, partitionID int, requests map[string]int64) error {
	ps.mu.Lock()
	defer ps.mu.Unlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return types.ErrPartitionNotFound
	}

	node := partition.Nodes[nodeName]
	if node == nil {
		return fmt.Errorf("node %s not found in partition %d", nodeName, partitionID)
	}

	if node.PodAllocations == nil {
		node.PodAllocations = make(map[string]*PodAllocation)
	}

	node.PodAllocations[podKey] = &PodAllocation{
		PodKey:     podKey,
		Requests:   requests,
		AssignedAt: time.Now(),
		IsAssumed:  true,
	}

	// 更新已分配资源
	for resource, value := range requests {
		node.Allocated[resource] += value
	}

	return nil
}

// ForgetPod 忘记假设的 Pod
func (ps *PartitionState) ForgetPod(podKey string, nodeName string, partitionID int) error {
	ps.mu.Lock()
	defer ps.mu.Unlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return nil
	}

	node := partition.Nodes[nodeName]
	if node == nil {
		return nil
	}

	allocation := node.PodAllocations[podKey]
	if allocation == nil {
		return nil
	}

	// 恢复资源
	for resource, value := range allocation.Requests {
		node.Allocated[resource] -= value
	}

	delete(node.PodAllocations, podKey)
	return nil
}

// GetAllNodes 获取所有节点数据
func (ps *PartitionState) GetAllNodes() map[string]*NodeData {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	result := make(map[string]*NodeData)
	for _, partition := range ps.partitions {
		for nodeName, nodeData := range partition.Nodes {
			result[nodeName] = nodeData
		}
	}
	return result
}

// GetNodesWithFreshness 获取带新鲜度信息的节点列表
func (ps *PartitionState) GetNodesWithFreshness() map[string]*NodeDataWithFreshness {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	result := make(map[string]*NodeDataWithFreshness)

	for partitionID, partition := range ps.partitions {
		freshness := ps.freshnessCalc.CalculateFreshness(partition.LastSyncTime)

		for nodeName, nodeData := range partition.Nodes {
			result[nodeName] = &NodeDataWithFreshness{
				NodeData:    nodeData,
				PartitionID: partitionID,
				Freshness:   freshness,
			}
		}
	}

	return result
}

// NodeDataWithFreshness 带新鲜度的节点数据
type NodeDataWithFreshness struct {
	*NodeData
	PartitionID int
	Freshness   float64
}
