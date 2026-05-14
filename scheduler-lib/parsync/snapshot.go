package parsync

import (
	"encoding/json"
	"fmt"
	"time"
)

// Snapshot ConfigMap labels: emitted by the Binder publisher and consumed by
// the Scheduler watcher to group chunk ConfigMaps by (partition-id, generation).
// Both sides MUST reference these constants — duplicating literal label keys
// regressed strategy-N10 once already (silent drop, see
// docs/design-parsync-pull-fix.md §0 root cause A).
const (
	SnapshotLabelApp         = "app"
	SnapshotLabelAppValue    = "parasched-snapshot"
	SnapshotLabelPartitionID = "partition-id"
	SnapshotLabelChunkIndex  = "chunk-index"
	SnapshotLabelChunkCount  = "chunk-count"
	SnapshotLabelGeneration  = "generation"
)

// SnapshotResource is the wire-format resource accounting carried in each
// Snapshot.NodeEntry. Field names and JSON tags must stay stable — both the
// Binder cache and the Scheduler ParSync consumer (un)marshal against this
// type, and the JSON sits in ConfigMap.Data on the wire.
type SnapshotResource struct {
	MilliCPU int64 `json:"MilliCPU"`
	Memory   int64 `json:"Memory"`
	Pods     int   `json:"Pods"`
}

// Add adds another resource to this one in place.
func (r *SnapshotResource) Add(other SnapshotResource) {
	r.MilliCPU += other.MilliCPU
	r.Memory += other.Memory
	r.Pods += other.Pods
}

// Sub subtracts another resource from this one in place.
func (r *SnapshotResource) Sub(other SnapshotResource) {
	r.MilliCPU -= other.MilliCPU
	r.Memory -= other.Memory
	r.Pods -= other.Pods
}

// SnapshotNodeEntry is per-node data inside a Snapshot.
type SnapshotNodeEntry struct {
	NodeName    string           `json:"nodeName"`
	Allocatable SnapshotResource `json:"allocatable"`
	Requested   SnapshotResource `json:"requested"`
	PodCount    int              `json:"podCount"`
}

// Snapshot is the JSON payload published by the Binder per chunk and applied
// by the Scheduler. PartitionID + Generation key a logical snapshot; chunks
// of the same key share Timestamp and Generation but slice the Nodes list.
type Snapshot struct {
	PartitionID int                 `json:"partitionID"`
	Timestamp   time.Time           `json:"ts"`
	Generation  int64               `json:"generation"`
	Nodes       []SnapshotNodeEntry `json:"nodes"`
}

// MarshalSnapshot serializes a Snapshot to JSON.
func MarshalSnapshot(s *Snapshot) ([]byte, error) {
	return json.Marshal(s)
}

// UnmarshalSnapshot deserializes a Snapshot from JSON.
func UnmarshalSnapshot(data []byte) (*Snapshot, error) {
	var s Snapshot
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("failed to unmarshal snapshot: %w", err)
	}
	return &s, nil
}
