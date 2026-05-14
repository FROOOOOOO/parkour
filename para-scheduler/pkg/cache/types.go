// Package cache provides a lightweight node resource cache for the Binder,
// tracking Allocatable and Requested resources per node. In ParSync mode,
// it additionally supports partition-based snapshot export/import.
package cache

import (
	"time"

	"example.com/scheduler-lib/parsync"
	v1 "k8s.io/api/core/v1"
)

// Resource is the wire-aligned resource accounting type, shared with the
// Scheduler ParSync consumer via scheduler-lib so JSON round-trips and label
// keys cannot drift between Binder and Scheduler.
type Resource = parsync.SnapshotResource

// NodeInfo tracks a single node's capacity and current resource usage.
type NodeInfo struct {
	NodeName    string
	Allocatable Resource  // total node capacity
	Requested   Resource  // sum of all pod requests on this node
	Pods        []*v1.Pod // pods currently on this node
	PartitionID int       // [ParSync] partition ID (-1 if unassigned)
}

// NewNodeInfo creates a NodeInfo with PartitionID=-1 (unassigned).
func NewNodeInfo() *NodeInfo {
	return &NodeInfo{
		PartitionID: -1,
	}
}

// AddPod adds a pod's resource request to this node.
func (n *NodeInfo) AddPod(pod *v1.Pod) {
	req := ComputePodRequest(pod)
	n.Requested.Add(req)
	n.Pods = append(n.Pods, pod)
}

// RemovePod removes a pod's resource request from this node.
func (n *NodeInfo) RemovePod(pod *v1.Pod) {
	key := podKey(pod)
	for i, p := range n.Pods {
		if podKey(p) == key {
			n.Pods = append(n.Pods[:i], n.Pods[i+1:]...)
			req := ComputePodRequest(pod)
			n.Requested.Sub(req)
			return
		}
	}
}

// Clone returns a deep copy of the NodeInfo.
func (n *NodeInfo) Clone() *NodeInfo {
	clone := &NodeInfo{
		NodeName:    n.NodeName,
		Allocatable: n.Allocatable,
		Requested:   n.Requested,
		PartitionID: n.PartitionID,
	}
	clone.Pods = make([]*v1.Pod, len(n.Pods))
	copy(clone.Pods, n.Pods)
	return clone
}

// PodState tracks an assumed pod's metadata for TTL-based expiry.
type PodState struct {
	Pod       *v1.Pod
	NodeName  string
	AssumedAt time.Time
}

// podKey returns namespace/name as a unique pod identifier.
func podKey(pod *v1.Pod) string {
	return pod.Namespace + "/" + pod.Name
}

// PodKeyFunc is exported for use by other packages.
func PodKeyFunc(pod *v1.Pod) string {
	return podKey(pod)
}
