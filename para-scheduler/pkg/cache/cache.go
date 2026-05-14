package cache

import (
	"fmt"
	"sync"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	"example.com/scheduler-lib/parsync"
)

// BinderCache is the Binder's authoritative view of cluster node resources.
// It tracks every node's Allocatable/Requested resources and supports
// optimistic assume/forget for pods being bound.
//
// In ParSync mode, an optional PartitionManager is attached so that nodes
// are assigned to partitions and snapshots can be exported per partition.
type BinderCache struct {
	mu          sync.RWMutex
	nodes       map[string]*NodeInfo  // nodeName -> NodeInfo
	assumedPods map[string]bool       // podKey -> true (assumed but not yet confirmed)
	podStates   map[string]*PodState  // podKey -> state
	ttl         time.Duration         // assumed pod expiry duration

	// [ParSync] optional, nil when not in ParSync mode
	partitionManager *parsync.PartitionManager

	// [ParSync] tracks last applied generation per partition to reject stale snapshots
	partitionGenerations map[int]int64
}

// NewBinderCache creates a new BinderCache with the given assumed-pod TTL.
func NewBinderCache(ttl time.Duration) *BinderCache {
	if ttl <= 0 {
		ttl = 30 * time.Second
	}
	return &BinderCache{
		nodes:       make(map[string]*NodeInfo),
		assumedPods: make(map[string]bool),
		podStates:   make(map[string]*PodState),
		ttl:         ttl,
	}
}

// SetPartitionManager attaches a partition manager for ParSync mode.
func (c *BinderCache) SetPartitionManager(pm *parsync.PartitionManager) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.partitionManager = pm
}

// --- Node operations ---

// AddNode adds a node to the cache, extracting Allocatable from the Node spec.
func (c *BinderCache) AddNode(node *v1.Node) {
	c.mu.Lock()
	defer c.mu.Unlock()

	ni := NewNodeInfo()
	ni.NodeName = node.Name
	ni.Allocatable = extractAllocatable(node)

	if c.partitionManager != nil {
		ni.PartitionID = c.partitionManager.AssignNode(node.Name)
	}

	c.nodes[node.Name] = ni
}

// UpdateNode updates a node's Allocatable resources.
func (c *BinderCache) UpdateNode(oldNode, newNode *v1.Node) {
	c.mu.Lock()
	defer c.mu.Unlock()

	ni, ok := c.nodes[newNode.Name]
	if !ok {
		// Node not tracked yet — treat as add.
		ni = NewNodeInfo()
		ni.NodeName = newNode.Name
		if c.partitionManager != nil {
			ni.PartitionID = c.partitionManager.AssignNode(newNode.Name)
		}
		c.nodes[newNode.Name] = ni
	}
	ni.Allocatable = extractAllocatable(newNode)
}

// RemoveNode removes a node and all its tracked pods from the cache.
func (c *BinderCache) RemoveNode(node *v1.Node) {
	c.mu.Lock()
	defer c.mu.Unlock()

	ni, ok := c.nodes[node.Name]
	if !ok {
		return
	}

	// Clean up pod states for pods on this node.
	for _, pod := range ni.Pods {
		key := podKey(pod)
		delete(c.assumedPods, key)
		delete(c.podStates, key)
	}

	if c.partitionManager != nil {
		c.partitionManager.RemoveNode(node.Name)
	}

	delete(c.nodes, node.Name)
}

// GetNodeInfo returns a clone of the NodeInfo for the given node, or nil if not found.
func (c *BinderCache) GetNodeInfo(nodeName string) *NodeInfo {
	c.mu.RLock()
	defer c.mu.RUnlock()

	ni, ok := c.nodes[nodeName]
	if !ok {
		return nil
	}
	return ni.Clone()
}

// NodeCount returns the number of tracked nodes.
func (c *BinderCache) NodeCount() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.nodes)
}

// --- Pod operations ---

// AddPod records a bound pod on its assigned node. This is called when
// a pod's binding is confirmed by the API server (via Informer event).
func (c *BinderCache) AddPod(pod *v1.Pod) {
	c.mu.Lock()
	defer c.mu.Unlock()

	key := podKey(pod)

	// If this was an assumed pod, it's now confirmed.
	if c.assumedPods[key] {
		delete(c.assumedPods, key)
		delete(c.podStates, key)
		// Resources were already accounted for in AssumePod, so skip double-counting.
		// But we need to replace the pod reference on the node.
		if ni, ok := c.nodes[pod.Spec.NodeName]; ok {
			// Remove the assumed pod reference and add the confirmed one.
			for i, p := range ni.Pods {
				if podKey(p) == key {
					ni.Pods[i] = pod
					return
				}
			}
			// If not found in pod list (shouldn't happen), just append.
			ni.Pods = append(ni.Pods, pod)
		}
		return
	}

	// Not an assumed pod — add resources.
	nodeName := pod.Spec.NodeName
	if nodeName == "" {
		return
	}
	ni, ok := c.nodes[nodeName]
	if !ok {
		return
	}
	ni.AddPod(pod)
}

// UpdatePod updates a pod in the cache. If the pod moved nodes (rare),
// it removes from the old and adds to the new.
func (c *BinderCache) UpdatePod(oldPod, newPod *v1.Pod) {
	if oldPod.Spec.NodeName != newPod.Spec.NodeName {
		c.RemovePod(oldPod)
		c.AddPod(newPod)
		return
	}
	// Same node: just update the pod reference.
	c.mu.Lock()
	defer c.mu.Unlock()
	key := podKey(newPod)
	if ni, ok := c.nodes[newPod.Spec.NodeName]; ok {
		for i, p := range ni.Pods {
			if podKey(p) == key {
				ni.Pods[i] = newPod
				return
			}
		}
	}
}

// RemovePod removes a pod from its node in the cache.
func (c *BinderCache) RemovePod(pod *v1.Pod) {
	c.mu.Lock()
	defer c.mu.Unlock()

	key := podKey(pod)
	delete(c.assumedPods, key)
	delete(c.podStates, key)

	nodeName := pod.Spec.NodeName
	if nodeName == "" {
		return
	}
	if ni, ok := c.nodes[nodeName]; ok {
		ni.RemovePod(pod)
	}
}

// AssumePod optimistically adds a pod to a node before the actual bind API call.
// If binding fails, call ForgetPod to undo.
func (c *BinderCache) AssumePod(pod *v1.Pod, nodeName string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.assumePodLocked(pod, nodeName)
}

// TryAssumePod atomically checks whether the pod is already assumed and, if
// not, performs the assume in a single critical section. This closes the
// TOCTOU window between IsAssumedPodByKey and AssumePod that could otherwise
// cause two Binder workers processing the same pod key to both proceed past
// the "already assumed" guard.
//
// Return semantics:
//   - (true, nil):    assume succeeded; caller owns the pod and should bind
//   - (false, nil):   pod was already assumed by another worker; caller must
//     abandon this cycle WITHOUT clearing annotations (another worker is in
//     flight and may still succeed)
//   - (false, err):   a hard error (e.g. node not in cache); caller may try
//     the next candidate
func (c *BinderCache) TryAssumePod(pod *v1.Pod, nodeName string) (assumed bool, err error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.assumedPods[podKey(pod)] {
		return false, nil
	}
	if err := c.assumePodLocked(pod, nodeName); err != nil {
		return false, err
	}
	return true, nil
}

// assumePodLocked is the lock-held implementation shared by AssumePod and
// TryAssumePod. Caller must hold c.mu.
func (c *BinderCache) assumePodLocked(pod *v1.Pod, nodeName string) error {
	key := podKey(pod)
	if c.assumedPods[key] {
		return fmt.Errorf("pod %s already assumed", key)
	}

	ni, ok := c.nodes[nodeName]
	if !ok {
		return fmt.Errorf("node %s not found in cache", nodeName)
	}

	ni.AddPod(pod)
	c.assumedPods[key] = true
	c.podStates[key] = &PodState{
		Pod:       pod,
		NodeName:  nodeName,
		AssumedAt: time.Now(),
	}
	return nil
}

// ForgetPod undoes an AssumePod — removes the pod's resources from its assumed node.
func (c *BinderCache) ForgetPod(pod *v1.Pod) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	key := podKey(pod)
	ps, ok := c.podStates[key]
	if !ok {
		return fmt.Errorf("pod %s not in assumed state", key)
	}

	if ni, ok := c.nodes[ps.NodeName]; ok {
		ni.RemovePod(pod)
	}

	delete(c.assumedPods, key)
	delete(c.podStates, key)
	return nil
}

// IsAssumedPod returns whether a pod is currently in the assumed state.
func (c *BinderCache) IsAssumedPod(pod *v1.Pod) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.assumedPods[podKey(pod)]
}

// IsAssumedPodByKey returns whether a pod key is currently in the assumed state.
func (c *BinderCache) IsAssumedPodByKey(key string) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.assumedPods[key]
}

// --- TTL expiry ---

// Run starts a background goroutine that periodically expires assumed pods
// that have exceeded the TTL. Blocks until stop is closed.
func (c *BinderCache) Run(stop <-chan struct{}) {
	ticker := time.NewTicker(c.ttl / 2)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			c.expireAssumedPods()
		case <-stop:
			return
		}
	}
}

// expireAssumedPods removes assumed pods that have exceeded the TTL.
func (c *BinderCache) expireAssumedPods() {
	c.mu.Lock()
	defer c.mu.Unlock()

	now := time.Now()
	for key, ps := range c.podStates {
		if !c.assumedPods[key] {
			continue
		}
		if now.Sub(ps.AssumedAt) > c.ttl {
			// Expire: remove from node.
			if ni, ok := c.nodes[ps.NodeName]; ok {
				ni.RemovePod(ps.Pod)
			}
			delete(c.assumedPods, key)
			delete(c.podStates, key)
		}
	}
}

// --- Helpers ---

// extractAllocatable converts a Node's Allocatable to our Resource type.
func extractAllocatable(node *v1.Node) Resource {
	alloc := node.Status.Allocatable
	if alloc == nil {
		alloc = node.Status.Capacity
	}
	var r Resource
	if cpu, ok := alloc[v1.ResourceCPU]; ok {
		r.MilliCPU = cpu.MilliValue()
	}
	if mem, ok := alloc[v1.ResourceMemory]; ok {
		r.Memory = mem.Value()
	}
	if pods, ok := alloc[v1.ResourcePods]; ok {
		r.Pods = int(pods.Value())
	}
	return r
}

// MakeResourceList is a test helper to create a v1.ResourceList from milli-CPU and bytes of memory.
func MakeResourceList(milliCPU int64, memBytes int64) v1.ResourceList {
	return v1.ResourceList{
		v1.ResourceCPU:    *resource.NewMilliQuantity(milliCPU, resource.DecimalSI),
		v1.ResourceMemory: *resource.NewQuantity(memBytes, resource.BinarySI),
	}
}
