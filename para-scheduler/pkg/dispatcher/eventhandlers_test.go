package dispatcher

import (
	"context"
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"example.com/para-scheduler/pkg/annotation"
)

func TestOnPodUpdate_DecrementOnBind(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0"})
	d.schedulers["sched-0"].PodCount = 3

	oldPod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: "pod-0", Namespace: "default",
			Annotations: map[string]string{
				annotation.SchedulerNameAnnotationKey: "sched-0",
			},
		},
	}
	newPod := oldPod.DeepCopy()
	newPod.Spec.NodeName = "node-0" // now bound

	d.OnPodUpdate(oldPod, newPod)

	info := d.GetSchedulerInfo("sched-0")
	if info.PodCount != 2 {
		t.Errorf("PodCount after bind: got %d, want 2", info.PodCount)
	}
}

func TestOnPodDelete_DecrementUnbound(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0"})
	d.schedulers["sched-0"].PodCount = 3

	// Unbound pod with scheduler annotation — should decrement.
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: "pod-0", Namespace: "default",
			Annotations: map[string]string{
				annotation.SchedulerNameAnnotationKey: "sched-0",
			},
		},
	}

	d.OnPodDelete(pod)
	info := d.GetSchedulerInfo("sched-0")
	if info.PodCount != 2 {
		t.Errorf("PodCount after delete unbound: got %d, want 2", info.PodCount)
	}
}

func TestOnPodDelete_AlreadyBound(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0"})
	d.schedulers["sched-0"].PodCount = 3

	// Already bound pod — should NOT decrement (was decremented on bind).
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: "pod-0", Namespace: "default",
			Annotations: map[string]string{
				annotation.SchedulerNameAnnotationKey: "sched-0",
			},
		},
		Spec: v1.PodSpec{NodeName: "node-0"},
	}

	d.OnPodDelete(pod)
	info := d.GetSchedulerInfo("sched-0")
	if info.PodCount != 3 {
		t.Errorf("PodCount after delete bound pod: got %d, want 3 (no change)", info.PodCount)
	}
}

// --- Scheduler Pod event handler tests ---

func makeSchedulerPod(name, instanceLabel string, ready bool) *v1.Pod {
	conditions := []v1.PodCondition{}
	if ready {
		conditions = append(conditions, v1.PodCondition{
			Type:   v1.PodReady,
			Status: v1.ConditionTrue,
		})
	}
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "para-system",
			Labels: map[string]string{
				"app":                  SchedulerAppLabel,
				SchedulerInstanceLabel: instanceLabel,
			},
		},
		Status: v1.PodStatus{
			Conditions: conditions,
		},
	}
}

func TestOnSchedulerPodAdd_Ready(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	pod := makeSchedulerPod("sched-pod-0", "sched-0", true)

	d.OnSchedulerPodAdd(pod)

	info := d.GetSchedulerInfo("sched-0")
	if info == nil {
		t.Fatal("scheduler sched-0 should be registered")
	}
}

func TestOnSchedulerPodAdd_NotReady(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	pod := makeSchedulerPod("sched-pod-0", "sched-0", false)

	d.OnSchedulerPodAdd(pod)

	info := d.GetSchedulerInfo("sched-0")
	if info != nil {
		t.Error("not-ready scheduler should not be registered")
	}
}

func TestOnSchedulerPodAdd_NoInstanceLabel(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sched-pod-0",
			Namespace: "para-system",
			Labels:    map[string]string{"app": SchedulerAppLabel},
		},
		Status: v1.PodStatus{
			Conditions: []v1.PodCondition{
				{Type: v1.PodReady, Status: v1.ConditionTrue},
			},
		},
	}

	// Should not panic or register.
	d.OnSchedulerPodAdd(pod)
}

func TestOnSchedulerPodUpdate_BecameReady(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	oldPod := makeSchedulerPod("sched-pod-0", "sched-0", false)
	newPod := makeSchedulerPod("sched-pod-0", "sched-0", true)

	d.OnSchedulerPodUpdate(oldPod, newPod)

	info := d.GetSchedulerInfo("sched-0")
	if info == nil {
		t.Fatal("scheduler should be registered when pod becomes ready")
	}
}

func TestOnSchedulerPodUpdate_BecameNotReady(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	// Pre-register the scheduler.
	d.AddScheduler(context.TODO(), "sched-0")

	oldPod := makeSchedulerPod("sched-pod-0", "sched-0", true)
	newPod := makeSchedulerPod("sched-pod-0", "sched-0", false)

	d.OnSchedulerPodUpdate(oldPod, newPod)

	info := d.GetSchedulerInfo("sched-0")
	if info != nil {
		t.Error("scheduler should be removed when pod becomes not ready")
	}
}

func TestOnSchedulerPodDelete(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	d.AddScheduler(context.TODO(), "sched-0")

	pod := makeSchedulerPod("sched-pod-0", "sched-0", true)

	d.OnSchedulerPodDelete(pod)

	info := d.GetSchedulerInfo("sched-0")
	if info != nil {
		t.Error("scheduler should be removed on pod delete")
	}
}

func TestIsSchedulerPodReady(t *testing.T) {
	tests := []struct {
		name     string
		pod      *v1.Pod
		expected bool
	}{
		{
			name:     "ready",
			pod:      makeSchedulerPod("p", "s", true),
			expected: true,
		},
		{
			name:     "not ready",
			pod:      makeSchedulerPod("p", "s", false),
			expected: false,
		},
		{
			name: "no conditions",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{Name: "p"},
			},
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := isSchedulerPodReady(tt.pod)
			if got != tt.expected {
				t.Errorf("isSchedulerPodReady: got %v, want %v", got, tt.expected)
			}
		})
	}
}

func TestGetSchedulerInstanceName(t *testing.T) {
	tests := []struct {
		name     string
		pod      *v1.Pod
		expected string
	}{
		{
			name:     "valid scheduler pod",
			pod:      makeSchedulerPod("p", "sched-0", true),
			expected: "sched-0",
		},
		{
			name: "wrong app label",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{"app": "other", SchedulerInstanceLabel: "s0"},
				},
			},
			expected: "",
		},
		{
			name: "no labels",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{Name: "p"},
			},
			expected: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := getSchedulerInstanceName(tt.pod)
			if got != tt.expected {
				t.Errorf("getSchedulerInstanceName: got %q, want %q", got, tt.expected)
			}
		})
	}
}

func TestAddScheduler_WithCoordinator(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx := context.Background()
	coord.Start(ctx)
	defer coord.Stop()

	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	d.SetCoordinator(coord)

	d.AddScheduler(ctx, "sched-0")

	// Check both dispatcher and coordinator registered.
	info := d.GetSchedulerInfo("sched-0")
	if info == nil {
		t.Fatal("dispatcher should have sched-0")
	}

	regInfo := coord.GetRegistry().GetScheduler("sched-0")
	if regInfo == nil {
		t.Fatal("coordinator should have sched-0")
	}
}

func TestRemoveScheduler_WithCoordinator(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx := context.Background()
	coord.Start(ctx)
	defer coord.Stop()

	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	d.SetCoordinator(coord)

	d.AddScheduler(ctx, "sched-0")
	d.RemoveScheduler(ctx, "sched-0")

	info := d.GetSchedulerInfo("sched-0")
	if info != nil {
		t.Error("dispatcher should not have sched-0 after removal")
	}

	assignments := coord.GetCurrentAssignments()
	if _, ok := assignments["sched-0"]; ok {
		t.Error("coordinator should not have sched-0 in assignments after removal")
	}
}
