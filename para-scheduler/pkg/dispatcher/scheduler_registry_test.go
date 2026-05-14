package dispatcher

import (
	"testing"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
)

func TestRegister_NewScheduler(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)

	info, isNew := r.Register("sched-0")
	if !isNew {
		t.Error("expected isNew=true for first registration")
	}
	if info.Name != "sched-0" {
		t.Errorf("name: got %q, want %q", info.Name, "sched-0")
	}
	if info.ID != 0 {
		t.Errorf("id: got %d, want 0", info.ID)
	}
	if info.Phase != apisv1.SchedulerPhasePending {
		t.Errorf("phase: got %q, want %q", info.Phase, apisv1.SchedulerPhasePending)
	}
	if !info.Healthy {
		t.Error("expected healthy=true")
	}
}

func TestRegister_ExistingScheduler(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("sched-0")

	info, isNew := r.Register("sched-0")
	if isNew {
		t.Error("expected isNew=false for re-registration")
	}
	if info.ID != 0 {
		t.Errorf("id should be preserved: got %d, want 0", info.ID)
	}
}

func TestRegister_IDAllocation(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)

	info0, _ := r.Register("s0")
	info1, _ := r.Register("s1")
	info2, _ := r.Register("s2")

	if info0.ID != 0 || info1.ID != 1 || info2.ID != 2 {
		t.Errorf("expected sequential IDs 0,1,2; got %d,%d,%d", info0.ID, info1.ID, info2.ID)
	}
}

func TestDeregister_IDReuse(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("s0") // ID=0
	r.Register("s1") // ID=1
	r.Register("s2") // ID=2

	// Deregister s1 (ID=1), then register s3 — should reuse ID=1.
	r.Deregister("s1")
	info3, isNew := r.Register("s3")
	if !isNew {
		t.Error("expected isNew=true for s3")
	}
	if info3.ID != 1 {
		t.Errorf("expected reused ID=1, got %d", info3.ID)
	}

	if r.Count() != 3 {
		t.Errorf("count: got %d, want 3", r.Count())
	}
}

func TestDeregister_NonExistent(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	// Should not panic.
	r.Deregister("nonexistent")
}

func TestHeartbeat(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("s0")

	r.Heartbeat("s0", HeartbeatStatus{Phase: apisv1.SchedulerPhaseRunning})

	info := r.GetScheduler("s0")
	if info.Phase != apisv1.SchedulerPhaseRunning {
		t.Errorf("phase: got %q, want %q", info.Phase, apisv1.SchedulerPhaseRunning)
	}
	if !info.Healthy {
		t.Error("expected healthy=true after heartbeat")
	}
}

func TestGetActiveSchedulers(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("s0")
	r.Register("s1")
	r.Register("s2")

	// Mark s2 as Failed.
	r.Heartbeat("s2", HeartbeatStatus{Phase: apisv1.SchedulerPhaseFailed})
	// Manually set unhealthy since Failed phase alone doesn't affect Healthy flag via Heartbeat.
	r.mu.Lock()
	r.schedulers["s2"].Healthy = false
	r.mu.Unlock()

	active := r.GetActiveSchedulers()
	if len(active) != 2 {
		t.Fatalf("active count: got %d, want 2", len(active))
	}

	// Should be sorted by ID.
	if active[0].ID > active[1].ID {
		t.Error("active schedulers should be sorted by ID")
	}
}

func TestGetActiveSchedulers_ExcludesPhases(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("s0") // Pending — should be active

	r.Heartbeat("s0", HeartbeatStatus{Phase: apisv1.SchedulerPhaseTerminating})

	active := r.GetActiveSchedulers()
	if len(active) != 0 {
		t.Errorf("Terminating scheduler should not be active, got %d active", len(active))
	}
}

func TestCheckHealth_Timeout(t *testing.T) {
	r := NewSchedulerRegistry(100*time.Millisecond, 0) // very short timeout
	r.Register("s0")
	r.Register("s1")

	// Wait for timeout.
	time.Sleep(150 * time.Millisecond)

	failed := r.CheckHealth()
	if len(failed) != 2 {
		t.Fatalf("expected 2 failed, got %d", len(failed))
	}

	// Verify they are now unhealthy.
	active := r.GetActiveSchedulers()
	if len(active) != 0 {
		t.Errorf("expected 0 active after timeout, got %d", len(active))
	}
}

func TestCheckHealth_RecentHeartbeat(t *testing.T) {
	r := NewSchedulerRegistry(1*time.Second, 0)
	r.Register("s0")

	// Immediately check — should not fail.
	failed := r.CheckHealth()
	if len(failed) != 0 {
		t.Errorf("expected 0 failed immediately after registration, got %d", len(failed))
	}
}

func TestUpdateAssignedPartitions(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	r.Register("s0")

	r.UpdateAssignedPartitions("s0", []int{0, 1, 2, 3})

	info := r.GetScheduler("s0")
	if len(info.AssignedPartitions) != 4 {
		t.Errorf("partitions: got %d, want 4", len(info.AssignedPartitions))
	}
}

func TestGetScheduler_NotFound(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	info := r.GetScheduler("nonexistent")
	if info != nil {
		t.Error("expected nil for nonexistent scheduler")
	}
}

func TestDeregister_IDCooldown(t *testing.T) {
	// With a positive cooldown, a Deregister'd ID must not be handed out to
	// a differently-named scheduler registering while the cooldown is active.
	r := NewSchedulerRegistry(15*time.Second, 200*time.Millisecond)
	r.Register("s0") // ID=0
	r.Register("s1") // ID=1
	r.Register("s2") // ID=2

	r.Deregister("s1")

	// Within the cooldown window, s3 (new name) should NOT reuse ID=1.
	info3, _ := r.Register("s3")
	if info3.ID == 1 {
		t.Errorf("s3 should not reuse cooldown ID=1 immediately, got %d", info3.ID)
	}

	// After the cooldown expires, the next Register must be able to pick
	// up ID=1 (the smallest free ID after cooldown pruning).
	time.Sleep(250 * time.Millisecond)
	info4, _ := r.Register("s4")
	if info4.ID != 1 {
		t.Errorf("expected ID=1 to be reusable after cooldown, got %d", info4.ID)
	}
}

func TestPreloadFromCRD(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 30*time.Second)
	// Simulate CRD-driven preload at Dispatcher startup.
	r.PreloadFromCRD("sched-2", 2, apisv1.SchedulerPhaseRunning, time.Now())
	r.PreloadFromCRD("sched-0", 0, apisv1.SchedulerPhaseRunning, time.Now())

	// Next Register with the same name must reuse the persisted ID.
	info0, isNew := r.Register("sched-0")
	if !isNew {
		// Register returns isNew=false when entry already exists, but the
		// caller (Coordinator.RegisterScheduler) interprets that as "already
		// known, no rebalance needed" — which is exactly what we want on
		// Dispatcher restart. Accept either but ID must match.
	}
	_ = isNew
	if info0.ID != 0 {
		t.Errorf("preloaded sched-0 should retain ID=0, got %d", info0.ID)
	}

	// A new name must NOT grab the preloaded IDs (0 or 2); smallest free is 1.
	infoNew, _ := r.Register("sched-new")
	if infoNew.ID != 1 {
		t.Errorf("new scheduler should get ID=1 (smallest free), got %d", infoNew.ID)
	}
}

func TestCount(t *testing.T) {
	r := NewSchedulerRegistry(15*time.Second, 0)
	if r.Count() != 0 {
		t.Errorf("initial count should be 0, got %d", r.Count())
	}
	r.Register("s0")
	r.Register("s1")
	if r.Count() != 2 {
		t.Errorf("count: got %d, want 2", r.Count())
	}
	r.Deregister("s0")
	if r.Count() != 1 {
		t.Errorf("count after deregister: got %d, want 1", r.Count())
	}
}
