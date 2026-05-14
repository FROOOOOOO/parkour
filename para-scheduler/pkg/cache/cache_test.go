package cache

import (
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/scheduler-lib/parsync"
)

func makeNode(name string, milliCPU, memBytes int64) *v1.Node {
	return &v1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: v1.NodeStatus{
			Allocatable: MakeResourceList(milliCPU, memBytes),
		},
	}
}

func makeBoundPod(name, ns, nodeName string, milliCPU, memBytes int64) *v1.Pod {
	pod := makePod(name, ns, milliCPU, memBytes)
	pod.Spec.NodeName = nodeName
	return pod
}

func TestAddNodeAndGetNodeInfo(t *testing.T) {
	c := NewBinderCache(30 * time.Second)

	node := makeNode("node-0", 4000, 8*1024*1024*1024)
	c.AddNode(node)

	ni := c.GetNodeInfo("node-0")
	if ni == nil {
		t.Fatal("expected non-nil NodeInfo")
	}
	if ni.NodeName != "node-0" {
		t.Errorf("NodeName: got %q, want %q", ni.NodeName, "node-0")
	}
	if ni.Allocatable.MilliCPU != 4000 {
		t.Errorf("Allocatable.MilliCPU: got %d, want 4000", ni.Allocatable.MilliCPU)
	}
	if ni.PartitionID != -1 {
		t.Errorf("PartitionID: got %d, want -1 (unassigned)", ni.PartitionID)
	}

	if c.NodeCount() != 1 {
		t.Errorf("NodeCount: got %d, want 1", c.NodeCount())
	}
}

func TestAddNodeWithPartitionManager(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	pm := parsync.NewPartitionManager(4)
	c.SetPartitionManager(pm)

	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	ni := c.GetNodeInfo("node-0")
	if ni == nil {
		t.Fatal("expected non-nil NodeInfo")
	}
	if ni.PartitionID == -1 {
		t.Error("expected PartitionID to be assigned (not -1)")
	}
	if ni.PartitionID < 0 || ni.PartitionID >= 4 {
		t.Errorf("PartitionID: got %d, expected 0-3", ni.PartitionID)
	}
}

func TestRemoveNode(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	node := makeNode("node-0", 4000, 8*1024*1024*1024)
	c.AddNode(node)
	c.RemoveNode(node)

	if c.GetNodeInfo("node-0") != nil {
		t.Error("expected nil after RemoveNode")
	}
	if c.NodeCount() != 0 {
		t.Errorf("NodeCount: got %d, want 0", c.NodeCount())
	}
}

func TestUpdateNode(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	oldNode := makeNode("node-0", 4000, 8*1024*1024*1024)
	c.AddNode(oldNode)

	newNode := makeNode("node-0", 8000, 16*1024*1024*1024)
	c.UpdateNode(oldNode, newNode)

	ni := c.GetNodeInfo("node-0")
	if ni.Allocatable.MilliCPU != 8000 {
		t.Errorf("Allocatable.MilliCPU after update: got %d, want 8000", ni.Allocatable.MilliCPU)
	}
}

func TestAddPod(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makeBoundPod("pod-0", "default", "node-0", 500, 1024*1024)
	c.AddPod(pod)

	ni := c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("Requested.MilliCPU: got %d, want 500", ni.Requested.MilliCPU)
	}
	if len(ni.Pods) != 1 {
		t.Errorf("Pods count: got %d, want 1", len(ni.Pods))
	}
}

func TestRemovePod(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makeBoundPod("pod-0", "default", "node-0", 500, 1024*1024)
	c.AddPod(pod)
	c.RemovePod(pod)

	ni := c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 0 {
		t.Errorf("Requested.MilliCPU after remove: got %d, want 0", ni.Requested.MilliCPU)
	}
	if len(ni.Pods) != 0 {
		t.Errorf("Pods count after remove: got %d, want 0", len(ni.Pods))
	}
}

func TestAssumePodAndForget(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makePod("pod-0", "default", 500, 1024*1024)

	if err := c.AssumePod(pod, "node-0"); err != nil {
		t.Fatalf("AssumePod failed: %v", err)
	}

	if !c.IsAssumedPod(pod) {
		t.Error("expected pod to be assumed")
	}

	ni := c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("Requested.MilliCPU after assume: got %d, want 500", ni.Requested.MilliCPU)
	}

	// Forget should undo the assume.
	if err := c.ForgetPod(pod); err != nil {
		t.Fatalf("ForgetPod failed: %v", err)
	}

	if c.IsAssumedPod(pod) {
		t.Error("expected pod to not be assumed after forget")
	}

	ni = c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 0 {
		t.Errorf("Requested.MilliCPU after forget: got %d, want 0", ni.Requested.MilliCPU)
	}
}

func TestAssumePodThenConfirm(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makePod("pod-0", "default", 500, 1024*1024)
	if err := c.AssumePod(pod, "node-0"); err != nil {
		t.Fatalf("AssumePod failed: %v", err)
	}

	// Confirm by AddPod with NodeName set (simulating Informer event).
	confirmedPod := pod.DeepCopy()
	confirmedPod.Spec.NodeName = "node-0"
	c.AddPod(confirmedPod)

	// Should not double-count resources.
	ni := c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("Requested.MilliCPU after confirm: got %d, want 500 (no double-count)", ni.Requested.MilliCPU)
	}

	if c.IsAssumedPod(pod) {
		t.Error("expected pod to not be assumed after confirm")
	}
}

func TestAssumePodDuplicate(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makePod("pod-0", "default", 500, 1024*1024)
	if err := c.AssumePod(pod, "node-0"); err != nil {
		t.Fatalf("first AssumePod failed: %v", err)
	}
	if err := c.AssumePod(pod, "node-0"); err == nil {
		t.Error("expected error on duplicate AssumePod")
	}
}

func TestAssumePodNodeNotFound(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	pod := makePod("pod-0", "default", 500, 1024*1024)
	if err := c.AssumePod(pod, "no-such-node"); err == nil {
		t.Error("expected error when node not found")
	}
}

func TestExpireAssumedPods(t *testing.T) {
	c := NewBinderCache(100 * time.Millisecond)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	pod := makePod("pod-0", "default", 500, 1024*1024)
	if err := c.AssumePod(pod, "node-0"); err != nil {
		t.Fatalf("AssumePod failed: %v", err)
	}

	// Wait for TTL to expire.
	time.Sleep(200 * time.Millisecond)
	c.expireAssumedPods()

	if c.IsAssumedPod(pod) {
		t.Error("expected assumed pod to be expired")
	}

	ni := c.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 0 {
		t.Errorf("Requested.MilliCPU after expiry: got %d, want 0", ni.Requested.MilliCPU)
	}
}

func TestGetNodeInfoReturnsClone(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	c.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))

	ni1 := c.GetNodeInfo("node-0")
	ni2 := c.GetNodeInfo("node-0")

	// Mutating one clone should not affect the other.
	ni1.Requested.MilliCPU = 9999
	if ni2.Requested.MilliCPU == 9999 {
		t.Error("GetNodeInfo should return independent clones")
	}
}

func TestGetNodeInfoNotFound(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	if ni := c.GetNodeInfo("nonexistent"); ni != nil {
		t.Error("expected nil for nonexistent node")
	}
}
