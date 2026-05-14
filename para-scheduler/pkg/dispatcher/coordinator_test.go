package dispatcher

import (
	"context"
	"testing"
	"time"

	parafake "example.com/para-sched-api/generated/clientset/versioned/fake"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func newTestCoordinator(t *testing.T) (*ParSyncCoordinator, *parafake.Clientset) {
	t.Helper()
	fakeCRD := parafake.NewSimpleClientset()
	config := &CoordinatorConfig{
		DefaultConfigName:   "default",
		HealthCheckInterval: 1 * time.Hour, // disable auto checks in tests
		RebalanceInterval:   1 * time.Hour,
		FailoverTimeout:     15 * time.Second,
		NumPartitions:       4,
		SyncPeriod:          4 * time.Second,
		SyncPattern:         SyncPatternDiff,
		RebalanceThreshold:  2,
	}
	coord := NewParSyncCoordinator(config, fakeCRD)
	return coord, fakeCRD
}

func TestCoordinator_Start(t *testing.T) {
	coord, fakeCRD := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	if err := coord.Start(ctx); err != nil {
		t.Fatalf("Start failed: %v", err)
	}

	// Verify ParSyncConfig CRD was created.
	psc, err := fakeCRD.SchedulingV1().ParSyncConfigs().Get(ctx, "default", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get ParSyncConfig: %v", err)
	}
	if psc.Spec.NumPartitions != 4 {
		t.Errorf("NumPartitions: got %d, want 4", psc.Spec.NumPartitions)
	}
	if psc.Spec.SyncPeriod.Duration != 4*time.Second {
		t.Errorf("SyncPeriod: got %v, want 4s", psc.Spec.SyncPeriod.Duration)
	}
}

func TestCoordinator_RegisterScheduler(t *testing.T) {
	coord, fakeCRD := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	if err := coord.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Register a scheduler.
	if err := coord.RegisterScheduler(ctx, "sched-0"); err != nil {
		t.Fatalf("RegisterScheduler: %v", err)
	}

	// Should have created a SchedulerAssignment CRD.
	sa, err := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "sched-0", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get SchedulerAssignment: %v", err)
	}
	if sa.Spec.SchedulerName != "sched-0" {
		t.Errorf("SchedulerName: got %q, want %q", sa.Spec.SchedulerName, "sched-0")
	}
	if sa.Spec.SchedulerID != 0 {
		t.Errorf("SchedulerID: got %d, want 0", sa.Spec.SchedulerID)
	}
	if len(sa.Spec.AssignedPartitions) != 4 {
		t.Errorf("AssignedPartitions: got %d, want 4", len(sa.Spec.AssignedPartitions))
	}
	if len(sa.Spec.SyncSlots) != 4 {
		t.Errorf("SyncSlots: got %d, want 4", len(sa.Spec.SyncSlots))
	}
	if sa.Spec.ConfigGeneration != 1 {
		t.Errorf("ConfigGeneration: got %d, want 1", sa.Spec.ConfigGeneration)
	}
}

func TestCoordinator_RegisterMultipleSchedulers(t *testing.T) {
	coord, fakeCRD := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)

	coord.RegisterScheduler(ctx, "s0")
	coord.RegisterScheduler(ctx, "s1")
	coord.RegisterScheduler(ctx, "s2")

	// Verify all 3 CRDs exist with correct IDs.
	for j, name := range []string{"s0", "s1", "s2"} {
		sa, err := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			t.Fatalf("get %s: %v", name, err)
		}
		if sa.Spec.SchedulerID != j {
			t.Errorf("%s SchedulerID: got %d, want %d", name, sa.Spec.SchedulerID, j)
		}
	}

	// ConfigGeneration should be 3 (one per new scheduler).
	if coord.GetConfigGeneration() != 3 {
		t.Errorf("ConfigGeneration: got %d, want 3", coord.GetConfigGeneration())
	}
}

func TestCoordinator_DeregisterScheduler(t *testing.T) {
	coord, fakeCRD := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")
	coord.RegisterScheduler(ctx, "s1")

	genBefore := coord.GetConfigGeneration()

	// Deregister s0.
	if err := coord.DeregisterScheduler(ctx, "s0"); err != nil {
		t.Fatalf("DeregisterScheduler: %v", err)
	}

	// CRD should be deleted.
	_, err := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "s0", metav1.GetOptions{})
	if err == nil {
		t.Error("expected s0 CRD to be deleted")
	}

	// s1 should still exist.
	_, err = fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "s1", metav1.GetOptions{})
	if err != nil {
		t.Errorf("s1 should still exist: %v", err)
	}

	// Generation should have incremented.
	if coord.GetConfigGeneration() <= genBefore {
		t.Errorf("ConfigGeneration should have incremented: was %d, now %d",
			genBefore, coord.GetConfigGeneration())
	}

	// Current assignments should not include s0.
	assignments := coord.GetCurrentAssignments()
	if _, ok := assignments["s0"]; ok {
		t.Error("s0 should not be in current assignments")
	}
}

func TestCoordinator_ReRegisterAfterDeregister(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")
	coord.RegisterScheduler(ctx, "s1")
	coord.DeregisterScheduler(ctx, "s0")

	// Re-register s0 — should get a reused ID.
	coord.RegisterScheduler(ctx, "s0-new")
	info := coord.GetRegistry().GetScheduler("s0-new")
	if info == nil {
		t.Fatal("s0-new should be registered")
	}
	// ID 0 was released, so s0-new should get ID 0.
	if info.ID != 0 {
		t.Errorf("s0-new should reuse ID 0, got %d", info.ID)
	}
}

func TestCoordinator_RegisterExistingNotNew(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")
	genAfterFirst := coord.GetConfigGeneration()

	// Re-register same scheduler — should not trigger rebalance.
	coord.RegisterScheduler(ctx, "s0")
	if coord.GetConfigGeneration() != genAfterFirst {
		t.Errorf("re-registering same scheduler should not increment generation: was %d, now %d",
			genAfterFirst, coord.GetConfigGeneration())
	}
}

func TestCoordinator_Heartbeat(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")

	err := coord.Heartbeat(ctx, "s0", HeartbeatStatus{Phase: "Running"})
	if err != nil {
		t.Fatalf("Heartbeat: %v", err)
	}
}

func TestCoordinator_SyncSlots_DiffPattern(t *testing.T) {
	coord, fakeCRD := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")
	coord.RegisterScheduler(ctx, "s1")

	// Verify SyncSlots are computed with diff pattern offsets.
	sa0, _ := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "s0", metav1.GetOptions{})
	sa1, _ := fakeCRD.SchedulingV1().SchedulerAssignments().Get(ctx, "s1", metav1.GetOptions{})

	// Both should have 4 sync slots.
	if len(sa0.Spec.SyncSlots) != 4 || len(sa1.Spec.SyncSlots) != 4 {
		t.Fatalf("expected 4 sync slots each, got %d and %d",
			len(sa0.Spec.SyncSlots), len(sa1.Spec.SyncSlots))
	}

	// At the same slot index, s0 and s1 should have different offsets for the same partition.
	// because the offsets use ((pid + schedulerID) % M) * slotInterval.
	for _, slot0 := range sa0.Spec.SyncSlots {
		for _, slot1 := range sa1.Spec.SyncSlots {
			if slot0.PartitionID == slot1.PartitionID {
				if slot0.OffsetMillis == slot1.OffsetMillis {
					t.Errorf("partition %d: s0 and s1 should have different offsets, both have %dms",
						slot0.PartitionID, slot0.OffsetMillis)
				}
			}
		}
	}
}

func TestCoordinator_DefaultConfig(t *testing.T) {
	config := DefaultCoordinatorConfig()

	if config.DefaultConfigName != "default" {
		t.Errorf("DefaultConfigName: got %q, want %q", config.DefaultConfigName, "default")
	}
	if config.HealthCheckInterval != 5*time.Second {
		t.Errorf("HealthCheckInterval: got %v, want 5s", config.HealthCheckInterval)
	}
	if config.RebalanceInterval != 30*time.Second {
		t.Errorf("RebalanceInterval: got %v, want 30s", config.RebalanceInterval)
	}
	if config.FailoverTimeout != 15*time.Second {
		t.Errorf("FailoverTimeout: got %v, want 15s", config.FailoverTimeout)
	}
}

func TestCoordinator_NilCRDClient(t *testing.T) {
	config := DefaultCoordinatorConfig()
	coord := NewParSyncCoordinator(config, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	// Should not panic with nil CRD client.
	if err := coord.Start(ctx); err != nil {
		t.Fatalf("Start with nil CRD client: %v", err)
	}

	if err := coord.RegisterScheduler(ctx, "s0"); err != nil {
		t.Fatalf("RegisterScheduler with nil CRD: %v", err)
	}

	if err := coord.DeregisterScheduler(ctx, "s0"); err != nil {
		t.Fatalf("DeregisterScheduler with nil CRD: %v", err)
	}
}

func TestCoordinator_GetCurrentAssignments(t *testing.T) {
	coord, _ := newTestCoordinator(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer coord.Stop()

	coord.Start(ctx)
	coord.RegisterScheduler(ctx, "s0")

	assignments := coord.GetCurrentAssignments()
	parts, ok := assignments["s0"]
	if !ok {
		t.Fatal("s0 not in assignments")
	}
	if len(parts) != 4 {
		t.Errorf("s0 partitions: got %d, want 4", len(parts))
	}

	// Verify returned map is a copy (modifying it shouldn't affect coordinator).
	assignments["s0"] = nil
	assignments2 := coord.GetCurrentAssignments()
	if assignments2["s0"] == nil {
		t.Error("GetCurrentAssignments should return a copy")
	}
}

func TestCoordinator_LoadExistingAssignments(t *testing.T) {
	fakeCRD := parafake.NewSimpleClientset()
	ctx := context.Background()

	// Pre-create a SchedulerAssignment.
	coord1, _ := newTestCoordinator(t)
	coord1.crdClient = fakeCRD
	coord1.Start(ctx)
	coord1.RegisterScheduler(ctx, "s0")
	coord1.Stop()

	// Create a new coordinator that should load existing state.
	config := &CoordinatorConfig{
		DefaultConfigName:   "default",
		HealthCheckInterval: 1 * time.Hour,
		RebalanceInterval:   1 * time.Hour,
		FailoverTimeout:     15 * time.Second,
		NumPartitions:       4,
		SyncPeriod:          4 * time.Second,
		SyncPattern:         SyncPatternDiff,
		RebalanceThreshold:  2,
	}
	coord2 := NewParSyncCoordinator(config, fakeCRD)
	if err := coord2.Start(ctx); err != nil {
		t.Fatalf("Start coord2: %v", err)
	}
	defer coord2.Stop()

	// Should have loaded the existing assignment.
	assignments := coord2.GetCurrentAssignments()
	if _, ok := assignments["s0"]; !ok {
		t.Error("coord2 should have loaded s0 assignment from CRD")
	}
	if coord2.GetConfigGeneration() < 1 {
		t.Error("coord2 should have loaded configGeneration >= 1")
	}
}
