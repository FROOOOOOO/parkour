package cache

import (
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func makePod(name, ns string, milliCPU, memBytes int64) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{
					Name:      "main",
					Resources: v1.ResourceRequirements{Requests: MakeResourceList(milliCPU, memBytes)},
				},
			},
		},
	}
}

func makePodWithInit(name, ns string, containerCPU, containerMem, initCPU, initMem int64) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{
					Name:      "main",
					Resources: v1.ResourceRequirements{Requests: MakeResourceList(containerCPU, containerMem)},
				},
			},
			InitContainers: []v1.Container{
				{
					Name:      "init",
					Resources: v1.ResourceRequirements{Requests: MakeResourceList(initCPU, initMem)},
				},
			},
		},
	}
}

func TestComputePodRequest(t *testing.T) {
	pod := makePod("p1", "default", 500, 1024*1024)
	res := ComputePodRequest(pod)

	if res.MilliCPU != 500 {
		t.Errorf("MilliCPU: got %d, want 500", res.MilliCPU)
	}
	if res.Memory != 1024*1024 {
		t.Errorf("Memory: got %d, want %d", res.Memory, 1024*1024)
	}
	if res.Pods != 1 {
		t.Errorf("Pods: got %d, want 1", res.Pods)
	}
}

func TestComputePodRequestMultiContainer(t *testing.T) {
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "multi", Namespace: "default"},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{Name: "a", Resources: v1.ResourceRequirements{Requests: MakeResourceList(200, 512)}},
				{Name: "b", Resources: v1.ResourceRequirements{Requests: MakeResourceList(300, 256)}},
			},
		},
	}
	res := ComputePodRequest(pod)

	if res.MilliCPU != 500 {
		t.Errorf("MilliCPU: got %d, want 500", res.MilliCPU)
	}
	if res.Memory != 768 {
		t.Errorf("Memory: got %d, want 768", res.Memory)
	}
	if res.Pods != 1 {
		t.Errorf("Pods: got %d, want 1", res.Pods)
	}
}

func TestComputePodRequestWithInit(t *testing.T) {
	// Init container requests more than regular container.
	pod := makePodWithInit("p-init", "default", 200, 512, 500, 1024)
	res := ComputePodRequest(pod)

	if res.MilliCPU != 500 {
		t.Errorf("MilliCPU: got %d, want 500 (max of init vs container)", res.MilliCPU)
	}
	if res.Memory != 1024 {
		t.Errorf("Memory: got %d, want 1024 (max of init vs container)", res.Memory)
	}
}

func TestComputePodRequestInitSmaller(t *testing.T) {
	// Init container requests less than regular container — regular wins.
	pod := makePodWithInit("p-init2", "default", 500, 1024, 100, 256)
	res := ComputePodRequest(pod)

	if res.MilliCPU != 500 {
		t.Errorf("MilliCPU: got %d, want 500", res.MilliCPU)
	}
	if res.Memory != 1024 {
		t.Errorf("Memory: got %d, want 1024", res.Memory)
	}
}

func TestFitsNode(t *testing.T) {
	ni := &NodeInfo{
		NodeName:    "node-0",
		Allocatable: Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   Resource{MilliCPU: 2000, Memory: 4 * 1024 * 1024 * 1024},
	}

	// Fits.
	fits, reason := FitsNode(Resource{MilliCPU: 1000, Memory: 2 * 1024 * 1024 * 1024}, ni)
	if !fits {
		t.Errorf("expected pod to fit, reason: %s", reason)
	}

	// Does not fit — CPU.
	fits, reason = FitsNode(Resource{MilliCPU: 3000, Memory: 1024}, ni)
	if fits {
		t.Error("expected pod not to fit due to CPU")
	}
	if reason == "" {
		t.Error("expected non-empty reason")
	}

	// Does not fit — Memory.
	fits, reason = FitsNode(Resource{MilliCPU: 100, Memory: 5 * 1024 * 1024 * 1024}, ni)
	if fits {
		t.Error("expected pod not to fit due to memory")
	}
}

func TestFitsNodeExactly(t *testing.T) {
	ni := &NodeInfo{
		NodeName:    "node-0",
		Allocatable: Resource{MilliCPU: 4000, Memory: 8000},
		Requested:   Resource{MilliCPU: 2000, Memory: 4000},
	}
	// Exactly uses remaining capacity.
	fits, _ := FitsNode(Resource{MilliCPU: 2000, Memory: 4000}, ni)
	if !fits {
		t.Error("expected pod to fit with exact remaining capacity")
	}
}
