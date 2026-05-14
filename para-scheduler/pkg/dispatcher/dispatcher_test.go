package dispatcher

import (
	"context"
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes/fake"

	"example.com/para-scheduler/pkg/annotation"
)

func makePod(name, ns string) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
	}
}

func makePodWithAnnotation(name, ns string, annotations map[string]string) *v1.Pod {
	return &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Annotations: annotations},
	}
}

// --- NeedsDispatch ---

func TestNeedsDispatch(t *testing.T) {
	tests := []struct {
		name   string
		pod    *v1.Pod
		expect bool
	}{
		{
			name:   "pending pod without annotations",
			pod:    makePod("pod-0", "default"),
			expect: true,
		},
		{
			name: "already has scheduler annotation",
			pod: makePodWithAnnotation("pod-0", "default", map[string]string{
				annotation.SchedulerNameAnnotationKey: "sched-0",
			}),
			expect: false,
		},
		{
			name: "already has candidate annotation",
			pod: makePodWithAnnotation("pod-0", "default", map[string]string{
				annotation.CandidateNodesAnnotationKey: "{}",
			}),
			expect: false,
		},
		{
			name: "already bound",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{Name: "pod-0", Namespace: "default"},
				Spec:       v1.PodSpec{NodeName: "node-0"},
			},
			expect: false,
		},
		{
			name: "succeeded pod",
			pod: &v1.Pod{
				ObjectMeta: metav1.ObjectMeta{Name: "pod-0", Namespace: "default"},
				Status:     v1.PodStatus{Phase: v1.PodSucceeded},
			},
			expect: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NeedsDispatch(tt.pod)
			if got != tt.expect {
				t.Errorf("NeedsDispatch() = %v, want %v", got, tt.expect)
			}
		})
	}
}

// --- selectScheduler ---

func TestSelectScheduler_LeastLoaded(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0", "sched-1", "sched-2"})

	// Manually set pod counts to simulate load.
	d.schedulers["sched-0"].PodCount = 10
	d.schedulers["sched-1"].PodCount = 5
	d.schedulers["sched-2"].PodCount = 8

	got := d.selectScheduler()
	if got != "sched-1" {
		t.Errorf("selectScheduler() = %q, want %q (least loaded)", got, "sched-1")
	}
}

func TestSelectScheduler_AllZero(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0", "sched-1"})

	got := d.selectScheduler()
	// Both are at 0 — any is valid.
	if got != "sched-0" && got != "sched-1" {
		t.Errorf("selectScheduler() = %q, expected one of sched-0 or sched-1", got)
	}
}

func TestSelectScheduler_Empty(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	got := d.selectScheduler()
	if got != "" {
		t.Errorf("selectScheduler() with no schedulers = %q, want empty", got)
	}
}

// --- DispatchPod ---

func TestDispatchPod(t *testing.T) {
	fakeClient := fake.NewSimpleClientset()
	d := NewDispatcher(fakeClient, []string{"sched-0", "sched-1"})

	// Pre-create the pod in the fake client so Patch succeeds.
	pod := makePod("pod-0", "default")
	_, err := fakeClient.CoreV1().Pods("default").Create(context.Background(), pod, metav1.CreateOptions{})
	if err != nil {
		t.Fatalf("create pod: %v", err)
	}

	err = d.DispatchPod(context.Background(), pod)
	if err != nil {
		t.Fatalf("DispatchPod failed: %v", err)
	}

	// Verify the pod was patched.
	patched, err := fakeClient.CoreV1().Pods("default").Get(context.Background(), "pod-0", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get pod: %v", err)
	}
	schedulerName := patched.Annotations[annotation.SchedulerNameAnnotationKey]
	if schedulerName == "" {
		t.Error("expected scheduler annotation to be set")
	}
	if schedulerName != "sched-0" && schedulerName != "sched-1" {
		t.Errorf("unexpected scheduler: %q", schedulerName)
	}
}

func TestDispatchPod_LoadBalancing(t *testing.T) {
	fakeClient := fake.NewSimpleClientset()
	d := NewDispatcher(fakeClient, []string{"sched-0", "sched-1"})

	// Dispatch 4 pods — should distribute 2+2 or 3+1 at worst.
	for i := 0; i < 4; i++ {
		pod := makePod("pod-"+itoa(i), "default")
		_, _ = fakeClient.CoreV1().Pods("default").Create(context.Background(), pod, metav1.CreateOptions{})
		if err := d.DispatchPod(context.Background(), pod); err != nil {
			t.Fatalf("DispatchPod[%d] failed: %v", i, err)
		}
	}

	info0 := d.GetSchedulerInfo("sched-0")
	info1 := d.GetSchedulerInfo("sched-1")
	if info0.PodCount != 2 || info1.PodCount != 2 {
		t.Errorf("expected 2+2 distribution, got sched-0=%d sched-1=%d",
			info0.PodCount, info1.PodCount)
	}
}

func TestDispatchPod_NoSchedulers(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), nil)
	pod := makePod("pod-0", "default")
	err := d.DispatchPod(context.Background(), pod)
	if err == nil {
		t.Error("expected error when no schedulers available")
	}
}

// --- DecrementPodCount ---

func TestDecrementPodCount(t *testing.T) {
	d := NewDispatcher(fake.NewSimpleClientset(), []string{"sched-0"})
	d.schedulers["sched-0"].PodCount = 5

	d.DecrementPodCount("sched-0")
	if d.schedulers["sched-0"].PodCount != 4 {
		t.Errorf("PodCount after decrement: got %d, want 4", d.schedulers["sched-0"].PodCount)
	}

	// Decrement to zero.
	d.schedulers["sched-0"].PodCount = 0
	d.DecrementPodCount("sched-0")
	if d.schedulers["sched-0"].PodCount != 0 {
		t.Errorf("PodCount should not go below 0: got %d", d.schedulers["sched-0"].PodCount)
	}

	// Unknown scheduler should not panic.
	d.DecrementPodCount("nonexistent")
}

// --- RecoverPodCounts ---

func TestRecoverPodCounts(t *testing.T) {
	fakeClient := fake.NewSimpleClientset()
	d := NewDispatcher(fakeClient, []string{"sched-0", "sched-1"})

	// Simulate existing pods: 2 dispatched-but-unbound to sched-0, 1 to sched-1,
	// 1 already bound (should not count), 1 without annotation (should not count).
	pods := []*v1.Pod{
		makePodWithAnnotation("p1", "default", map[string]string{
			annotation.SchedulerNameAnnotationKey: "sched-0",
		}),
		makePodWithAnnotation("p2", "default", map[string]string{
			annotation.SchedulerNameAnnotationKey: "sched-0",
		}),
		makePodWithAnnotation("p3", "default", map[string]string{
			annotation.SchedulerNameAnnotationKey: "sched-1",
		}),
		// Already bound — should not count.
		func() *v1.Pod {
			p := makePodWithAnnotation("p4", "default", map[string]string{
				annotation.SchedulerNameAnnotationKey: "sched-1",
			})
			p.Spec.NodeName = "node-0"
			return p
		}(),
		// No scheduler annotation — should not count.
		makePod("p5", "default"),
	}
	for _, p := range pods {
		_, _ = fakeClient.CoreV1().Pods("default").Create(context.Background(), p, metav1.CreateOptions{})
	}

	// Build a real informer-backed lister so RecoverPodCounts works.
	informerFactory := informers.NewSharedInformerFactory(fakeClient, 0)
	podInformer := informerFactory.Core().V1().Pods()
	d.SetPodLister(podInformer.Lister())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	informerFactory.Start(ctx.Done())
	informerFactory.WaitForCacheSync(ctx.Done())

	d.RecoverPodCounts()

	info0 := d.GetSchedulerInfo("sched-0")
	info1 := d.GetSchedulerInfo("sched-1")
	if info0.PodCount != 2 {
		t.Errorf("sched-0 PodCount: got %d, want 2", info0.PodCount)
	}
	if info1.PodCount != 1 {
		t.Errorf("sched-1 PodCount: got %d, want 1", info1.PodCount)
	}
}

func itoa(i int) string {
	if i < 10 {
		return string(rune('0' + i))
	}
	return itoa(i/10) + string(rune('0'+i%10))
}
