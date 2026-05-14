package dispatcher

import (
	"context"
	"testing"
	"time"

	parafake "example.com/para-sched-api/generated/clientset/versioned/fake"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// --- computePartitionOrder ---

func TestComputePartitionOrder_Glob(t *testing.T) {
	pa := NewPartitionAssigner(nil, 1, 3*time.Second, SyncPatternGlob, []string{"s0", "s1"})
	order := pa.computePartitionOrder(0)
	if len(order) != 1 || order[0] != 0 {
		t.Errorf("glob: got %v, want [0]", order)
	}
}

func TestComputePartitionOrder_Same(t *testing.T) {
	pa := NewPartitionAssigner(nil, 3, 3*time.Second, SyncPatternSame, []string{"s0", "s1"})

	for j := 0; j < 2; j++ {
		order := pa.computePartitionOrder(j)
		if len(order) != 3 {
			t.Fatalf("same scheduler %d: len=%d, want 3", j, len(order))
		}
		for i, p := range order {
			if p != i {
				t.Errorf("same scheduler %d: order[%d]=%d, want %d", j, i, p, i)
			}
		}
	}
}

func TestComputePartitionOrder_Diff(t *testing.T) {
	pa := NewPartitionAssigner(nil, 3, 3*time.Second, SyncPatternDiff, []string{"s0", "s1", "s2"})

	// Scheduler 0: [0, 1, 2]
	order0 := pa.computePartitionOrder(0)
	expect0 := []int{0, 1, 2}
	for i, p := range order0 {
		if p != expect0[i] {
			t.Errorf("diff s0: order[%d]=%d, want %d", i, p, expect0[i])
		}
	}

	// Scheduler 1: [1, 2, 0]
	order1 := pa.computePartitionOrder(1)
	expect1 := []int{1, 2, 0}
	for i, p := range order1 {
		if p != expect1[i] {
			t.Errorf("diff s1: order[%d]=%d, want %d", i, p, expect1[i])
		}
	}

	// Scheduler 2: [2, 0, 1]
	order2 := pa.computePartitionOrder(2)
	expect2 := []int{2, 0, 1}
	for i, p := range order2 {
		if p != expect2[i] {
			t.Errorf("diff s2: order[%d]=%d, want %d", i, p, expect2[i])
		}
	}
}

// --- computeSyncSlots ---

func TestComputeSyncSlots(t *testing.T) {
	pa := NewPartitionAssigner(nil, 3, 3*time.Second, SyncPatternDiff, []string{"s0", "s1", "s2"})

	order := []int{1, 2, 0}
	slots := pa.computeSyncSlots(1, order)

	if len(slots) != 3 {
		t.Fatalf("slots length: got %d, want 3", len(slots))
	}

	// slotInterval = 3s / 3 = 1s = 1000ms
	expectedOffsets := []int64{0, 1000, 2000}
	expectedPartitions := []int{1, 2, 0}
	for i, s := range slots {
		if s.SlotIndex != i {
			t.Errorf("slot[%d].SlotIndex: got %d, want %d", i, s.SlotIndex, i)
		}
		if s.PartitionID != expectedPartitions[i] {
			t.Errorf("slot[%d].PartitionID: got %d, want %d", i, s.PartitionID, expectedPartitions[i])
		}
		if s.OffsetMillis != expectedOffsets[i] {
			t.Errorf("slot[%d].OffsetMillis: got %d, want %d", i, s.OffsetMillis, expectedOffsets[i])
		}
	}
}

// --- diffSync uniqueness property ---

func TestDiffSync_NoOverlap(t *testing.T) {
	M := 4
	N := 4
	schedulers := make([]string, N)
	for i := 0; i < N; i++ {
		schedulers[i] = "s" + itoa(i)
	}
	pa := NewPartitionAssigner(nil, M, 4*time.Second, SyncPatternDiff, schedulers)

	// At each slot index, each scheduler should be syncing a different partition.
	for slotIdx := 0; slotIdx < M; slotIdx++ {
		seen := make(map[int]bool)
		for j := 0; j < N; j++ {
			order := pa.computePartitionOrder(j)
			pid := order[slotIdx]
			if seen[pid] {
				t.Errorf("slot %d: duplicate partition %d across schedulers", slotIdx, pid)
			}
			seen[pid] = true
		}
	}
}

// --- Initialize + AssignPartitions with fake CRD client ---

func TestInitialize(t *testing.T) {
	fakeCRD := parafake.NewSimpleClientset()
	pa := NewPartitionAssigner(fakeCRD, 3, 3*time.Second, SyncPatternDiff, []string{"s0", "s1", "s2"})

	ctx := context.Background()
	if err := pa.Initialize(ctx); err != nil {
		t.Fatalf("Initialize failed: %v", err)
	}

	// Verify ParSyncConfig was created.
	psc, err := fakeCRD.SchedulingV1().ParSyncConfigs().Get(ctx, "default", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get ParSyncConfig: %v", err)
	}
	if psc.Spec.NumPartitions != 3 {
		t.Errorf("NumPartitions: got %d, want 3", psc.Spec.NumPartitions)
	}
	if psc.Spec.SyncPeriod.Duration != 3*time.Second {
		t.Errorf("SyncPeriod: got %v, want 3s", psc.Spec.SyncPeriod.Duration)
	}

	// Verify SchedulerAssignment for each scheduler.
	for j, name := range []string{"s0", "s1", "s2"} {
		sa, err := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			t.Fatalf("get SchedulerAssignment %s: %v", name, err)
		}
		if sa.Spec.SchedulerID != j {
			t.Errorf("sa %s SchedulerID: got %d, want %d", name, sa.Spec.SchedulerID, j)
		}
		if len(sa.Spec.AssignedPartitions) != 3 {
			t.Errorf("sa %s AssignedPartitions: got %d, want 3", name, len(sa.Spec.AssignedPartitions))
		}
		if len(sa.Spec.SyncSlots) != 3 {
			t.Errorf("sa %s SyncSlots: got %d, want 3", name, len(sa.Spec.SyncSlots))
		}
		if sa.Spec.ParSyncConfig.Name != "default" {
			t.Errorf("sa %s ParSyncConfig.Name: got %q, want %q", name, sa.Spec.ParSyncConfig.Name, "default")
		}
	}
}

func TestInitialize_GlobMode(t *testing.T) {
	fakeCRD := parafake.NewSimpleClientset()
	pa := NewPartitionAssigner(fakeCRD, 3, 3*time.Second, SyncPatternGlob, []string{"s0"})

	ctx := context.Background()
	if err := pa.Initialize(ctx); err != nil {
		t.Fatalf("Initialize failed: %v", err)
	}

	// Glob forces numPartitions=1.
	psc, _ := fakeCRD.SchedulingV1().ParSyncConfigs().Get(ctx, "default", metav1.GetOptions{})
	if psc.Spec.NumPartitions != 1 {
		t.Errorf("glob NumPartitions: got %d, want 1", psc.Spec.NumPartitions)
	}

	sa, _ := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "s0", metav1.GetOptions{})
	if len(sa.Spec.SyncSlots) != 1 {
		t.Errorf("glob SyncSlots: got %d, want 1", len(sa.Spec.SyncSlots))
	}
	if sa.Spec.SyncSlots[0].PartitionID != 0 {
		t.Errorf("glob slot[0].PartitionID: got %d, want 0", sa.Spec.SyncSlots[0].PartitionID)
	}
}

// --- RegisterNode ---

func TestRegisterNode(t *testing.T) {
	pa := NewPartitionAssigner(nil, 4, 3*time.Second, SyncPatternDiff, []string{"s0"})
	pid := pa.RegisterNode("node-0")
	if pid < 0 || pid >= 4 {
		t.Errorf("RegisterNode: partition %d out of range [0,4)", pid)
	}
}
