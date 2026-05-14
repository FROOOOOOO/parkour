package cache

import (
	"fmt"
	"time"

	"example.com/scheduler-lib/parsync"
)

// PartitionSnapshot / NodeSnapshotEntry are aliases of the wire types defined
// in scheduler-lib. Defined as aliases (rather than re-declared) so the Binder
// cache and the Scheduler ParSync consumer encode/decode the exact same
// JSON shape — duplicate definitions previously sat on both sides and were
// the kind of seam that silently dropped chunks when one side drifted.
type (
	PartitionSnapshot = parsync.Snapshot
	NodeSnapshotEntry = parsync.SnapshotNodeEntry
)

// SnapshotPartition exports one partition's data from the cache.
// Returns nil if no nodes belong to the given partition.
func (c *BinderCache) SnapshotPartition(partitionID int) *PartitionSnapshot {
	c.mu.RLock()
	defer c.mu.RUnlock()

	var entries []NodeSnapshotEntry
	for _, ni := range c.nodes {
		if ni.PartitionID == partitionID {
			entries = append(entries, NodeSnapshotEntry{
				NodeName:    ni.NodeName,
				Allocatable: ni.Allocatable,
				Requested:   ni.Requested,
				PodCount:    len(ni.Pods),
			})
		}
	}

	return &PartitionSnapshot{
		PartitionID: partitionID,
		Timestamp:   time.Now(),
		Nodes:       entries,
	}
}

// SnapshotAll exports all partitions. Used for globSync (P1) mode where
// numPartitions=1 or for debug/testing.
func (c *BinderCache) SnapshotAll() []*PartitionSnapshot {
	c.mu.RLock()
	defer c.mu.RUnlock()

	// Collect all distinct partition IDs.
	partitions := make(map[int][]NodeSnapshotEntry)
	for _, ni := range c.nodes {
		entry := NodeSnapshotEntry{
			NodeName:    ni.NodeName,
			Allocatable: ni.Allocatable,
			Requested:   ni.Requested,
			PodCount:    len(ni.Pods),
		}
		partitions[ni.PartitionID] = append(partitions[ni.PartitionID], entry)
	}

	now := time.Now()
	var result []*PartitionSnapshot
	for pid, entries := range partitions {
		result = append(result, &PartitionSnapshot{
			PartitionID: pid,
			Timestamp:   now,
			Nodes:       entries,
		})
	}
	return result
}

// ApplySnapshot replaces local cache data for a given partition with the
// snapshot data. Existing nodes in that partition are updated; nodes present
// in the cache but absent from the snapshot are removed; nodes in the snapshot
// but absent from the cache are added.
//
// A generation check prevents stale (out-of-order) snapshots from overwriting
// newer data. Returns nil without applying if snapshot.Generation <= last applied.
//
// This is used by the Scheduler in periodic sync mode.
func (c *BinderCache) ApplySnapshot(snapshot *PartitionSnapshot) error {
	if snapshot == nil {
		return fmt.Errorf("snapshot is nil")
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	// Generation check: reject stale snapshots.
	if lastGen, ok := c.partitionGenerations[snapshot.PartitionID]; ok && snapshot.Generation <= lastGen {
		return nil
	}
	if c.partitionGenerations == nil {
		c.partitionGenerations = make(map[int]int64)
	}
	c.partitionGenerations[snapshot.PartitionID] = snapshot.Generation

	// Build a set of node names in the incoming snapshot.
	snapshotNodes := make(map[string]*NodeSnapshotEntry, len(snapshot.Nodes))
	for i := range snapshot.Nodes {
		snapshotNodes[snapshot.Nodes[i].NodeName] = &snapshot.Nodes[i]
	}

	// Update or remove existing nodes in this partition.
	for name, ni := range c.nodes {
		if ni.PartitionID != snapshot.PartitionID {
			continue
		}
		if entry, ok := snapshotNodes[name]; ok {
			// Update: overwrite resource data, clear pod list (we don't have
			// detailed pod info from the snapshot, only aggregated counts).
			ni.Allocatable = entry.Allocatable
			ni.Requested = entry.Requested
			ni.Pods = nil
			delete(snapshotNodes, name)
		} else {
			// Node was in this partition but not in snapshot — remove.
			delete(c.nodes, name)
		}
	}

	// Add new nodes from snapshot that weren't in the cache.
	for name, entry := range snapshotNodes {
		c.nodes[name] = &NodeInfo{
			NodeName:    name,
			Allocatable: entry.Allocatable,
			Requested:   entry.Requested,
			PartitionID: snapshot.PartitionID,
		}
	}

	return nil
}

// MarshalSnapshot / UnmarshalSnapshot delegate to scheduler-lib so the
// JSON shape stays single-sourced with the Scheduler ParSync consumer.
var (
	MarshalSnapshot   = parsync.MarshalSnapshot
	UnmarshalSnapshot = parsync.UnmarshalSnapshot
)
