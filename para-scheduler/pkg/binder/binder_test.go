package binder

import (
	"context"
	"fmt"
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	"example.com/para-scheduler/pkg/annotation"
	"example.com/para-scheduler/pkg/cache"
)

// --- test helpers ---

func makeNode(name string, milliCPU, memBytes int64) *v1.Node {
	return &v1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: v1.NodeStatus{
			Allocatable: cache.MakeResourceList(milliCPU, memBytes),
		},
	}
}

func makePod(name, ns string, milliCPU, memBytes int64) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{
					Name:      "main",
					Resources: v1.ResourceRequirements{Requests: cache.MakeResourceList(milliCPU, memBytes)},
				},
			},
		},
	}
}

func setCandidateAnnotation(pod *v1.Pod, candidates []annotation.CandidateEntry) {
	ann := &annotation.CandidateNodesAnnotation{
		Candidates: candidates,
		PodKey:     pod.Namespace + "/" + pod.Name,
		Scheduler:  "test-sched",
		Timestamp:  time.Now(),
	}
	encoded, _ := annotation.Encode(ann)
	if pod.Annotations == nil {
		pod.Annotations = make(map[string]string)
	}
	pod.Annotations[annotation.CandidateNodesAnnotationKey] = encoded
}

func setupBinder(nodes []*v1.Node) (*Binder, *fake.Clientset) {
	fakeClient := fake.NewSimpleClientset()

	// Default reactor: accept all bind (pods/binding create) calls.
	fakeClient.PrependReactor("create", "pods", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if action.GetSubresource() == "binding" {
			return true, nil, nil
		}
		return false, nil, nil
	})

	bc := cache.NewBinderCache(30 * time.Second)
	for _, n := range nodes {
		bc.AddNode(n)
	}
	b := NewBinder(fakeClient, nil, bc)
	return b, fakeClient
}

// --- ProcessPod tests ---

func TestProcessPod_FirstCandidateSuccess(t *testing.T) {
	b, _ := setupBinder([]*v1.Node{
		makeNode("node-0", 4000, 8*1024*1024*1024),
		makeNode("node-1", 4000, 8*1024*1024*1024),
	})

	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "node-0", Rank: 0, Score: 100},
		{NodeName: "node-1", Rank: 1, Score: 90},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("ProcessPod failed: %v", err)
	}

	// node-0 should have the assumed pod.
	ni := b.cache.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("node-0 Requested.MilliCPU: got %d, want 500", ni.Requested.MilliCPU)
	}
	// node-1 should be unaffected.
	ni1 := b.cache.GetNodeInfo("node-1")
	if ni1.Requested.MilliCPU != 0 {
		t.Errorf("node-1 Requested.MilliCPU: got %d, want 0", ni1.Requested.MilliCPU)
	}
}

func TestProcessPod_FallbackToSecondCandidate(t *testing.T) {
	b, _ := setupBinder([]*v1.Node{
		makeNode("node-0", 4000, 8*1024*1024*1024),
		makeNode("node-1", 4000, 8*1024*1024*1024),
	})

	// Consume almost all resources on node-0.
	heavyPod := makePod("heavy", "default", 3800, 7*1024*1024*1024)
	heavyPod.Spec.NodeName = "node-0"
	b.cache.AddPod(heavyPod)

	// Pod that needs 500m CPU — won't fit on node-0 (only 200m left).
	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "node-0", Rank: 0, Score: 100},
		{NodeName: "node-1", Rank: 1, Score: 90},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("ProcessPod failed: %v", err)
	}

	// node-1 should have the pod (fallback).
	ni1 := b.cache.GetNodeInfo("node-1")
	if ni1.Requested.MilliCPU != 500 {
		t.Errorf("node-1 Requested.MilliCPU: got %d, want 500", ni1.Requested.MilliCPU)
	}
}

func TestProcessPod_AllCandidatesFail(t *testing.T) {
	b, _ := setupBinder([]*v1.Node{
		makeNode("node-0", 1000, 2*1024*1024*1024),
	})

	// Consume all resources.
	heavyPod := makePod("heavy", "default", 1000, 2*1024*1024*1024)
	heavyPod.Spec.NodeName = "node-0"
	b.cache.AddPod(heavyPod)

	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "node-0", Rank: 0, Score: 100},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != ErrAllCandidatesFailed {
		t.Errorf("expected ErrAllCandidatesFailed, got: %v", err)
	}
}

func TestProcessPod_NodeNotFound(t *testing.T) {
	b, _ := setupBinder([]*v1.Node{
		makeNode("node-0", 4000, 8*1024*1024*1024),
	})

	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "nonexistent", Rank: 0, Score: 100},
		{NodeName: "node-0", Rank: 1, Score: 90},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("ProcessPod should succeed with fallback, got: %v", err)
	}

	// Should have fallen back to node-0.
	ni := b.cache.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("node-0 Requested.MilliCPU: got %d, want 500", ni.Requested.MilliCPU)
	}
}

func TestProcessPod_NoCandidateAnnotation(t *testing.T) {
	b, _ := setupBinder(nil)

	pod := makePod("pod-0", "default", 500, 1024*1024)
	err := b.ProcessPod(context.Background(), pod)
	if err != ErrNoCandidates {
		t.Errorf("expected ErrNoCandidates, got: %v", err)
	}
}

func TestProcessPod_BindAPIFail_FallbackSucceeds(t *testing.T) {
	b, fakeClient := setupBinder([]*v1.Node{
		makeNode("node-0", 4000, 8*1024*1024*1024),
		makeNode("node-1", 4000, 8*1024*1024*1024),
	})

	// Make bind fail for node-0.
	callCount := 0
	fakeClient.PrependReactor("create", "pods", func(action k8stesting.Action) (bool, runtime.Object, error) {
		createAction, ok := action.(k8stesting.CreateAction)
		if !ok {
			return false, nil, nil
		}
		binding, ok := createAction.GetObject().(*v1.Binding)
		if !ok {
			return false, nil, nil
		}
		callCount++
		if binding.Target.Name == "node-0" {
			return true, nil, fmt.Errorf("simulated bind failure")
		}
		return true, nil, nil // node-1 succeeds
	})

	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "node-0", Rank: 0, Score: 100},
		{NodeName: "node-1", Rank: 1, Score: 90},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("ProcessPod should succeed with fallback, got: %v", err)
	}

	// Bind should have been called twice (once for node-0 fail, once for node-1 success).
	if callCount != 2 {
		t.Errorf("bind call count: got %d, want 2", callCount)
	}

	// node-0 should have no assumed pod (forgot after bind fail).
	ni0 := b.cache.GetNodeInfo("node-0")
	if ni0.Requested.MilliCPU != 0 {
		t.Errorf("node-0 Requested.MilliCPU: got %d, want 0 (forgot after fail)", ni0.Requested.MilliCPU)
	}

	// node-1 should have the pod.
	ni1 := b.cache.GetNodeInfo("node-1")
	if ni1.Requested.MilliCPU != 500 {
		t.Errorf("node-1 Requested.MilliCPU: got %d, want 500", ni1.Requested.MilliCPU)
	}
}

func TestProcessPod_SnapshotPublisherMarkDirty(t *testing.T) {
	b, _ := setupBinder([]*v1.Node{
		makeNode("node-0", 4000, 8*1024*1024*1024),
	})

	// Attach a snapshot publisher.
	sp := NewSnapshotPublisher(fake.NewSimpleClientset(), b.cache, "test-ns", 100*time.Millisecond, 2, time.Second)
	b.SetSnapshotPublisher(sp)

	pod := makePod("pod-0", "default", 500, 1024*1024)
	setCandidateAnnotation(pod, []annotation.CandidateEntry{
		{NodeName: "node-0", Rank: 0, Score: 100, PartitionID: 2},
	})

	err := b.ProcessPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("ProcessPod failed: %v", err)
	}

	// Snapshot publisher should have partition 2 marked dirty.
	if sp.DirtyCount() != 1 {
		t.Errorf("DirtyCount: got %d, want 1", sp.DirtyCount())
	}
}
