package binder

import (
	"testing"
	"time"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"example.com/para-scheduler/pkg/annotation"
	"example.com/para-scheduler/pkg/cache"
)

func TestNeedsBinding(t *testing.T) {
	tests := []struct {
		name   string
		pod    *v1.Pod
		expect bool
	}{
		{
			name: "pod with candidates and no NodeName",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						annotation.CandidateNodesAnnotationKey: "{}",
					},
				},
			},
			expect: true,
		},
		{
			name: "pod already bound",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						annotation.CandidateNodesAnnotationKey: "{}",
					},
				},
				Spec: v1.PodSpec{NodeName: "node-0"},
			},
			expect: false,
		},
		{
			name:   "pod without candidate annotation",
			pod:    &v1.Pod{},
			expect: false,
		},
		{
			name: "pod in succeeded phase",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						annotation.CandidateNodesAnnotationKey: "{}",
					},
				},
				Status: v1.PodStatus{Phase: v1.PodSucceeded},
			},
			expect: false,
		},
		{
			name: "pod in failed phase",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						annotation.CandidateNodesAnnotationKey: "{}",
					},
				},
				Status: v1.PodStatus{Phase: v1.PodFailed},
			},
			expect: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NeedsBinding(tt.pod)
			if got != tt.expect {
				t.Errorf("NeedsBinding() = %v, want %v", got, tt.expect)
			}
		})
	}
}

func TestNodeEventHandlers(t *testing.T) {
	bc := cache.NewBinderCache(30 * time.Second)
	b := NewBinder(nil, nil, bc)

	node := makeNode("node-test", 4000, 8*1024*1024*1024)

	// Add.
	b.AddNodeToCache(node)
	if bc.NodeCount() != 1 {
		t.Errorf("after AddNodeToCache: NodeCount = %d, want 1", bc.NodeCount())
	}

	// Update.
	newNode := makeNode("node-test", 8000, 16*1024*1024*1024)
	b.UpdateNodeInCache(node, newNode)
	ni := bc.GetNodeInfo("node-test")
	if ni.Allocatable.MilliCPU != 8000 {
		t.Errorf("after UpdateNodeInCache: MilliCPU = %d, want 8000", ni.Allocatable.MilliCPU)
	}

	// Delete.
	b.DeleteNodeFromCache(newNode)
	if bc.NodeCount() != 0 {
		t.Errorf("after DeleteNodeFromCache: NodeCount = %d, want 0", bc.NodeCount())
	}
}

func TestPodEventHandlers(t *testing.T) {
	bc := cache.NewBinderCache(30 * time.Second)
	bc.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))
	b := NewBinder(nil, nil, bc)

	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "pod-0", Namespace: "default"},
		Spec: v1.PodSpec{
			NodeName: "node-0",
			Containers: []v1.Container{
				{Name: "main", Resources: v1.ResourceRequirements{
					Requests: cache.MakeResourceList(500, 1024*1024),
				}},
			},
		},
	}

	// Add bound pod.
	b.AddPodToCache(pod)
	ni := bc.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("after AddPodToCache: MilliCPU = %d, want 500", ni.Requested.MilliCPU)
	}

	// Delete pod.
	b.DeletePodFromCache(pod)
	ni = bc.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 0 {
		t.Errorf("after DeletePodFromCache: MilliCPU = %d, want 0", ni.Requested.MilliCPU)
	}
}

func TestAddPodToCache_IgnoresUnboundPod(t *testing.T) {
	bc := cache.NewBinderCache(30 * time.Second)
	bc.AddNode(makeNode("node-0", 4000, 8*1024*1024*1024))
	b := NewBinder(nil, nil, bc)

	// Pod with no NodeName should be ignored.
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "pod-0", Namespace: "default"},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{Name: "main", Resources: v1.ResourceRequirements{
					Requests: cache.MakeResourceList(500, 1024*1024),
				}},
			},
		},
	}

	b.AddPodToCache(pod)
	ni := bc.GetNodeInfo("node-0")
	if ni.Requested.MilliCPU != 0 {
		t.Errorf("unbound pod should not be added to cache: MilliCPU = %d, want 0", ni.Requested.MilliCPU)
	}
}
