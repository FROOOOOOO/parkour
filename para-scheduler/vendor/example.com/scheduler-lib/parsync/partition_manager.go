package parsync

import (
	"hash/fnv"
	"sync"
)

// PartitionManager assigns nodes to partitions (pure algorithm, no K8s
// dependencies).
type PartitionManager struct {
	mu sync.RWMutex

	numPartitions int

	// node-to-partition mapping
	nodeToPartition map[string]int

	// partition-to-node-list mapping
	partitionToNodes map[int][]string
}

// NewPartitionManager creates a PartitionManager with the given number of
// partitions.
func NewPartitionManager(numPartitions int) *PartitionManager {
	if numPartitions <= 0 {
		numPartitions = 4
	}

	pm := &PartitionManager{
		numPartitions:    numPartitions,
		nodeToPartition:  make(map[string]int),
		partitionToNodes: make(map[int][]string),
	}

	// Initialize partitions.
	for i := 0; i < numPartitions; i++ {
		pm.partitionToNodes[i] = make([]string, 0)
	}

	return pm
}

// AssignNode assigns a node to a partition.
//
// Strategy: greedy least-loaded — always picks the partition with the fewest
// nodes, breaking ties by choosing the lowest partition ID.  Compared to the
// earlier FNV-1a mod M approach:
//   - Output is perfectly balanced (max-min ≤ 1 at all times), eliminating
//     the tail-node skew that caused unequal snapshot sizes with FNV.
//   - Zero migration: already-assigned nodes still return the same partition
//     via the cache, preserving the same semantics as the FNV version.
//   - Does not depend on the uniformity of node names (FNV was uniform for
//     `kwok-node-XXXX` but unverified for EC2/GKE random names).
//
// Idempotent: repeated calls for the same nodeName always return the same
// value (first computed result is stored in nodeToPartition).
func (pm *PartitionManager) AssignNode(nodeName string) int {
	pm.mu.Lock()
	defer pm.mu.Unlock()

	// Return the existing assignment if present.
	if partitionID, exists := pm.nodeToPartition[nodeName]; exists {
		return partitionID
	}

	// Greedy: pick the partition with the fewest nodes.  Iterating in ascending
	// partition-ID order ensures tie-breaking is deterministic (same node join
	// order always produces the same assignment), which aids reproducibility.
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

// RemoveNode removes a node from its assigned partition.
func (pm *PartitionManager) RemoveNode(nodeName string) {
	pm.mu.Lock()
	defer pm.mu.Unlock()

	partitionID, exists := pm.nodeToPartition[nodeName]
	if !exists {
		return
	}

	delete(pm.nodeToPartition, nodeName)

	// Remove the node from the partition's node list.
	nodes := pm.partitionToNodes[partitionID]
	for i, name := range nodes {
		if name == nodeName {
			pm.partitionToNodes[partitionID] = append(nodes[:i], nodes[i+1:]...)
			break
		}
	}
}

// GetPartitionID returns the partition that the given node belongs to.
func (pm *PartitionManager) GetPartitionID(nodeName string) int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	if partitionID, exists := pm.nodeToPartition[nodeName]; exists {
		return partitionID
	}

	// Compute on the fly when the node is not in the map (result not persisted).
	h := fnv.New32a()
	h.Write([]byte(nodeName))
	return int(h.Sum32() % uint32(pm.numPartitions))
}

// GetPartitionNodes returns all nodes in the given partition.
func (pm *PartitionManager) GetPartitionNodes(partitionID int) []string {
	pm.mu.RLock()
	defer pm.mu.RUnlock()

	nodes := pm.partitionToNodes[partitionID]
	result := make([]string, len(nodes))
	copy(result, nodes)
	return result
}

// GetAllPartitions returns all partition IDs.
func (pm *PartitionManager) GetAllPartitions() []int {
	partitions := make([]int, pm.numPartitions)
	for i := 0; i < pm.numPartitions; i++ {
		partitions[i] = i
	}
	return partitions
}

// GetPartitionCount returns the number of partitions.
func (pm *PartitionManager) GetPartitionCount() int {
	return pm.numPartitions
}

// GetNodeCount returns the total number of assigned nodes.
func (pm *PartitionManager) GetNodeCount() int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return len(pm.nodeToPartition)
}

// GetPartitionNodeCount returns the number of nodes in the given partition.
func (pm *PartitionManager) GetPartitionNodeCount(partitionID int) int {
	pm.mu.RLock()
	defer pm.mu.RUnlock()
	return len(pm.partitionToNodes[partitionID])
}

// RebalanceCheck reports whether the partition sizes are balanced.
// A difference of ±1 is considered balanced.
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
	// Allow a difference of at most 1.
	return diff <= 1, diff
}
