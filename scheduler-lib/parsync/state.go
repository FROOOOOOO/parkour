package parsync

import (
	"fmt"
	"sync"
	"time"

	"example.com/scheduler-lib/types"
)

// PartitionState holds the state of all partitions (generic structure, no K8s
// dependencies).
type PartitionState struct {
	mu            sync.RWMutex
	partitions    map[int]*PartitionData
	freshnessCalc *FreshnessCalculator
}

// PartitionData holds data for a single partition.
type PartitionData struct {
	ID           int
	LastSyncTime time.Time
	Generation   int64
	// Node data (uses generic types).
	Nodes map[string]*NodeData
}

// NodeData holds generic node data.
type NodeData struct {
	Name           string
	PartitionID    int
	LastUpdateTime time.Time
	// Resource information stored as a map to avoid K8s type dependencies.
	Allocatable map[string]int64 // resource name -> milli value
	Allocated   map[string]int64

	// Pod allocations.
	PodAllocations map[string]*PodAllocation

	// Status flags.
	Ready    bool
	Cordoned bool

	// Custom labels.
	Labels map[string]string
}

// PodAllocation records a pod allocation on a node.
type PodAllocation struct {
	PodKey     string
	Requests   map[string]int64 // resource name -> milli value
	AssignedAt time.Time
	IsAssumed  bool
}

// NewPartitionState creates a PartitionState with the given number of partitions.
func NewPartitionState(numPartitions int, syncPeriod time.Duration) *PartitionState {
	ps := &PartitionState{
		partitions:    make(map[int]*PartitionData),
		freshnessCalc: NewFreshnessCalculator(syncPeriod),
	}
	// Initialize partitions.
	for i := 0; i < numPartitions; i++ {
		ps.partitions[i] = &PartitionData{
			ID:    i,
			Nodes: make(map[string]*NodeData),
		}
	}

	return ps
}

// UpdatePartition replaces the node data for the given partition.
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

// GetPartitionData returns the data for the given partition.
func (ps *PartitionState) GetPartitionData(partitionID int) *PartitionData {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	return ps.partitions[partitionID]
}

// GetNodeData returns the data for the given node within a partition.
func (ps *PartitionState) GetNodeData(nodeName string, partitionID int) *NodeData {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return nil
	}

	return partition.Nodes[nodeName]
}

// GetPartitionFreshness returns the freshness of the given partition.
func (ps *PartitionState) GetPartitionFreshness(partitionID int) float64 {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return 0
	}

	return ps.freshnessCalc.CalculateFreshness(partition.LastSyncTime)
}

// GetPartitionStaleness returns how stale the given partition is.
func (ps *PartitionState) GetPartitionStaleness(partitionID int) time.Duration {
	ps.mu.RLock()
	defer ps.mu.RUnlock()
	partition := ps.partitions[partitionID]
	if partition == nil {
		return 0
	}

	return time.Since(partition.LastSyncTime)
}

// GetPartitionInfo returns metadata for the given partition.
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

// AssumePod optimistically records that a pod has been scheduled to a node.
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

	// Update allocated resources.
	for resource, value := range requests {
		node.Allocated[resource] += value
	}

	return nil
}

// ForgetPod removes an optimistic pod allocation from a node.
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

	// Restore resources.
	for resource, value := range allocation.Requests {
		node.Allocated[resource] -= value
	}

	delete(node.PodAllocations, podKey)
	return nil
}

// GetAllNodes returns node data for every node across all partitions.
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

// GetNodesWithFreshness returns node data annotated with per-partition freshness.
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

// NodeDataWithFreshness wraps NodeData with the partition's freshness value.
type NodeDataWithFreshness struct {
	*NodeData
	PartitionID int
	Freshness   float64
}
