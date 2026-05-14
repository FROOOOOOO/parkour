package dispatcher

import (
	"context"
	"fmt"
	"sync"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"
)

// CoordinatorConfig holds configuration for the ParSyncCoordinator.
type CoordinatorConfig struct {
	DefaultConfigName   string        // ParSyncConfig CRD name (default "default")
	HealthCheckInterval time.Duration // interval for heartbeat checks (default 5s)
	RebalanceInterval   time.Duration // interval for periodic rebalance checks (default 30s)
	FailoverTimeout     time.Duration // heartbeat timeout threshold (default 15s)
	NumPartitions       int           // total number of partitions M
	SyncPeriod          time.Duration // complete sync period G
	SyncPattern         string        // "glob", "same", "diff"
	RebalanceThreshold  int           // partition imbalance threshold

	// RebalanceCooldown is the minimum interval between two consecutive
	// rebalance executions. Events arriving within the cooldown window are
	// coalesced into a single deferred rebalance that fires at
	// lastRebalanceAt + RebalanceCooldown. Without this, scheduler flapping
	// (rapid Register/Deregister cycles) would trigger back-to-back CRD
	// updates and force all Schedulers to reload configGeneration, making
	// it hard for the cluster to reach steady state during experiments.
	RebalanceCooldown time.Duration

	// IDCooldownPeriod is the minimum duration a SchedulerID stays reserved
	// after Deregister before it can be handed out to a new scheduler. This
	// prevents a "zombie" scheduler — one whose pod is no longer tracked by
	// the Dispatcher (e.g. kubelet marked NotReady due to network partition)
	// but whose process is still alive and using its cached SchedulerID —
	// from sharing an ID with a newly-registered scheduler, which would
	// collapse the diffSync stagger invariant. Default 2×FailoverTimeout so
	// that any zombie is guaranteed to have been evicted by its own
	// CheckHealth before the ID becomes available again.
	IDCooldownPeriod time.Duration
}

// DefaultCoordinatorConfig returns sensible defaults for CoordinatorConfig.
func DefaultCoordinatorConfig() *CoordinatorConfig {
	return &CoordinatorConfig{
		DefaultConfigName:   "default",
		HealthCheckInterval: 5 * time.Second,
		RebalanceInterval:   30 * time.Second,
		FailoverTimeout:     15 * time.Second,
		NumPartitions:       4,
		SyncPeriod:          3 * time.Second,
		SyncPattern:         SyncPatternDiff,
		RebalanceThreshold:  2,
		RebalanceCooldown:   2 * time.Second,
		IDCooldownPeriod:    30 * time.Second, // 2 × FailoverTimeout
	}
}

// ParSyncCoordinator is the dynamic partition management controller.
// It replaces the static PartitionAssigner with event-driven coordination:
// scheduler registration/deregistration triggers rebalance, and background
// goroutines handle health checks, periodic rebalance, and clock advancement.
type ParSyncCoordinator struct {
	mu sync.RWMutex

	// Sub-components.
	registry     *SchedulerRegistry
	rebalancer   *Rebalancer
	clockManager *ClockManager
	crdClient    parasched.Interface

	// Configuration.
	config      *CoordinatorConfig
	syncPattern string

	// State.
	currentAssignments map[string][]int // schedulerName -> partitions
	configGeneration   int64            // monotonically increasing config version

	// Rebalance cooldown / debounce state. Guarded by rebalanceMu to avoid
	// lock-order issues with c.mu.
	rebalanceMu     sync.Mutex
	lastRebalanceAt time.Time
	pendingTimer    *time.Timer

	stopCh chan struct{}
}

// NewParSyncCoordinator creates a ParSyncCoordinator with the given config and CRD client.
func NewParSyncCoordinator(config *CoordinatorConfig, crdClient parasched.Interface) *ParSyncCoordinator {
	if config == nil {
		config = DefaultCoordinatorConfig()
	}
	return &ParSyncCoordinator{
		registry:           NewSchedulerRegistry(config.FailoverTimeout, config.IDCooldownPeriod),
		rebalancer:         NewRebalancer(apisv1.RebalancePolicyMinimalMove),
		clockManager:       NewClockManager(config.SyncPeriod),
		crdClient:          crdClient,
		config:             config,
		syncPattern:        config.SyncPattern,
		currentAssignments: make(map[string][]int),
		stopCh:             make(chan struct{}),
	}
}

// Start initializes the coordinator: loads/creates CRD state, starts background goroutines.
func (c *ParSyncCoordinator) Start(ctx context.Context) error {
	// 1. Ensure ParSyncConfig CRD exists.
	if err := c.ensureParSyncConfig(ctx); err != nil {
		return fmt.Errorf("ensure ParSyncConfig: %w", err)
	}

	// 2. Load existing SchedulerAssignment CRDs to rebuild state.
	if err := c.loadExistingAssignments(ctx); err != nil {
		return fmt.Errorf("load existing assignments: %w", err)
	}

	// 3. Start background goroutines.
	go c.runHealthCheck(ctx)
	go c.runRebalanceCheck(ctx)
	go c.runClockAdvance(ctx)

	klog.InfoS("ParSyncCoordinator started",
		"partitions", c.config.NumPartitions,
		"syncPeriod", c.config.SyncPeriod,
		"pattern", c.config.SyncPattern)
	return nil
}

// Stop signals all background goroutines to stop.
func (c *ParSyncCoordinator) Stop() {
	close(c.stopCh)
	// Cancel any pending deferred rebalance so AfterFunc does not fire after
	// shutdown and attempt CRD writes on a closed client.
	c.rebalanceMu.Lock()
	if c.pendingTimer != nil {
		c.pendingTimer.Stop()
		c.pendingTimer = nil
	}
	c.rebalanceMu.Unlock()
}

// RegisterScheduler registers a scheduler and triggers rebalance if it's new.
func (c *ParSyncCoordinator) RegisterScheduler(ctx context.Context, name string) error {
	info, isNew := c.registry.Register(name)
	klog.InfoS("Scheduler registered", "name", name, "id", info.ID, "isNew", isNew)

	if isNew {
		return c.triggerRebalance(ctx)
	}
	return nil
}

// DeregisterScheduler removes a scheduler and triggers rebalance.
func (c *ParSyncCoordinator) DeregisterScheduler(ctx context.Context, name string) error {
	c.registry.Deregister(name)

	// Clean up the SchedulerAssignment CRD.
	if c.crdClient != nil {
		err := c.crdClient.SchedulingV1().SchedulerAssignments().Delete(ctx, name, metav1.DeleteOptions{})
		if err != nil && !errors.IsNotFound(err) {
			klog.ErrorS(err, "Failed to delete SchedulerAssignment CRD", "name", name)
		}
	}

	klog.InfoS("Scheduler deregistered", "name", name)

	// Do NOT pre-delete the stale entry from c.currentAssignments here.
	// Rebalancer.rebalanceMinimalMove uses "name present in currentAssignments
	// but absent from activeSchedulers" as its removed-scheduler detection
	// (sets Changed=true and emits Moves). Pre-deleting hides that signal and
	// the rebalance silently becomes a no-op. doRebalance performs a full
	// overwrite `c.currentAssignments = result.Assignments` so the stale
	// entry is cleaned up there.
	return c.triggerRebalance(ctx)
}

// Heartbeat processes a scheduler heartbeat.
func (c *ParSyncCoordinator) Heartbeat(ctx context.Context, name string, status HeartbeatStatus) error {
	c.registry.Heartbeat(name, status)
	return nil
}

// GetConfigGeneration returns the current config generation.
func (c *ParSyncCoordinator) GetConfigGeneration() int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.configGeneration
}

// GetCurrentAssignments returns a copy of the current assignments.
func (c *ParSyncCoordinator) GetCurrentAssignments() map[string][]int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	result := make(map[string][]int, len(c.currentAssignments))
	for k, v := range c.currentAssignments {
		cp := make([]int, len(v))
		copy(cp, v)
		result[k] = cp
	}
	return result
}

// GetRegistry returns the scheduler registry (for testing/inspection).
func (c *ParSyncCoordinator) GetRegistry() *SchedulerRegistry {
	return c.registry
}

// triggerRebalance is the cooldown-gated entry point for rebalance events.
// It coalesces bursts of register/deregister/health/periodic triggers into
// at most one rebalance per RebalanceCooldown window:
//
//   - outside cooldown window: execute immediately and record lastRebalanceAt
//   - inside cooldown window:  arm a single deferred timer that fires at
//     lastRebalanceAt + cooldown; subsequent triggers collapse into the
//     already-armed timer (only one deferred rebalance per window)
//
// When RebalanceCooldown <= 0 the cooldown is disabled and every call runs
// doRebalance inline (legacy behavior).
func (c *ParSyncCoordinator) triggerRebalance(ctx context.Context) error {
	cooldown := c.config.RebalanceCooldown
	if cooldown <= 0 {
		return c.doRebalance(ctx)
	}

	c.rebalanceMu.Lock()
	elapsed := time.Since(c.lastRebalanceAt)
	if c.lastRebalanceAt.IsZero() || elapsed >= cooldown {
		c.lastRebalanceAt = time.Now()
		c.rebalanceMu.Unlock()
		return c.doRebalance(ctx)
	}

	// Within cooldown window: arm a single deferred timer if not already armed.
	if c.pendingTimer != nil {
		c.rebalanceMu.Unlock()
		klog.V(4).InfoS("Rebalance already pending, coalescing trigger")
		return nil
	}
	delay := cooldown - elapsed
	c.pendingTimer = time.AfterFunc(delay, func() {
		c.rebalanceMu.Lock()
		c.pendingTimer = nil
		c.lastRebalanceAt = time.Now()
		c.rebalanceMu.Unlock()
		// Use a background context for the deferred run — the original ctx
		// may have been cancelled (e.g. RegisterScheduler returned long ago).
		if err := c.doRebalance(context.Background()); err != nil {
			klog.ErrorS(err, "Deferred rebalance failed")
		}
	})
	c.rebalanceMu.Unlock()
	klog.V(3).InfoS("Rebalance deferred due to cooldown", "delayMS", delay.Milliseconds())
	return nil
}

// doRebalance performs the core rebalance logic:
// 1. Get active schedulers
// 2. Run rebalancer
// 3. If changed, update CRDs and local state
//
// Callers should prefer triggerRebalance to benefit from cooldown debouncing;
// doRebalance is the unconditional worker invoked by triggerRebalance and is
// also safe to call directly for tests or one-shot initialization paths.
func (c *ParSyncCoordinator) doRebalance(ctx context.Context) error {
	activeSchedulers := c.registry.GetActiveSchedulers()

	// Do NOT short-circuit on empty activeSchedulers: the Rebalancer's
	// removed-scheduler detection is the mechanism that clears stale
	// currentAssignments entries (e.g. when the last scheduler is
	// deregistered). Skipping the Rebalance call leaves ghost entries
	// behind, which breaks GetCurrentAssignments consumers and the
	// "removed scheduler detection" feeding the next rebalance.

	c.mu.Lock()
	result := c.rebalancer.Rebalance(activeSchedulers, c.config.NumPartitions, c.currentAssignments)
	if !result.Changed {
		c.mu.Unlock()
		return nil
	}

	c.configGeneration++
	generation := c.configGeneration
	c.currentAssignments = result.Assignments
	c.mu.Unlock()

	// Update CRDs for each active scheduler.
	for _, s := range activeSchedulers {
		partitions := result.Assignments[s.Name]
		syncSlots := c.clockManager.CalculateSyncSlots(s.ID, partitions, c.config.NumPartitions)

		if err := c.updateSchedulerAssignmentCRD(ctx, s, partitions, syncSlots, generation); err != nil {
			klog.ErrorS(err, "Failed to update SchedulerAssignment CRD", "scheduler", s.Name)
			return err
		}

		c.registry.UpdateAssignedPartitions(s.Name, partitions)
	}

	// Log moves for observability.
	for _, move := range result.Moves {
		klog.V(3).InfoS("Partition move",
			"partition", move.PartitionID,
			"from", move.FromScheduler,
			"to", move.ToScheduler)
	}

	klog.InfoS("Rebalance completed",
		"generation", generation,
		"activeSchedulers", len(activeSchedulers),
		"moves", len(result.Moves))

	return nil
}

// updateSchedulerAssignmentCRD creates or updates the SchedulerAssignment CRD for a scheduler.
func (c *ParSyncCoordinator) updateSchedulerAssignmentCRD(
	ctx context.Context,
	scheduler *RegistrySchedulerInfo,
	partitions []int,
	syncSlots []apisv1.SyncSlotSpec,
	generation int64,
) error {
	if c.crdClient == nil {
		return nil
	}

	sa := &apisv1.SchedulerAssignment{
		ObjectMeta: metav1.ObjectMeta{
			Name: scheduler.Name,
		},
		Spec: apisv1.SchedulerAssignmentSpec{
			SchedulerName:      scheduler.Name,
			SchedulerID:        scheduler.ID,
			AssignedPartitions: partitions,
			SyncSlots:          syncSlots,
			ParSyncConfig: apisv1.ParSyncConfigRef{
				Name: c.config.DefaultConfigName,
			},
			ConfigGeneration: generation,
		},
	}

	_, err := c.crdClient.SchedulingV1().SchedulerAssignments().Create(ctx, sa, metav1.CreateOptions{})
	if errors.IsAlreadyExists(err) {
		existing, getErr := c.crdClient.SchedulingV1().SchedulerAssignments().Get(ctx, scheduler.Name, metav1.GetOptions{})
		if getErr != nil {
			return fmt.Errorf("get existing SchedulerAssignment for %s: %w", scheduler.Name, getErr)
		}
		sa.ResourceVersion = existing.ResourceVersion
		_, err = c.crdClient.SchedulingV1().SchedulerAssignments().Update(ctx, sa, metav1.UpdateOptions{})
	}
	return err
}

// ensureParSyncConfig creates or updates the ParSyncConfig CRD.
func (c *ParSyncCoordinator) ensureParSyncConfig(ctx context.Context) error {
	if c.crdClient == nil {
		return nil
	}

	psc := &apisv1.ParSyncConfig{
		ObjectMeta: metav1.ObjectMeta{
			Name: c.config.DefaultConfigName,
		},
		Spec: apisv1.ParSyncConfigSpec{
			NumPartitions:      c.config.NumPartitions,
			SyncPeriod:         metav1.Duration{Duration: c.config.SyncPeriod},
			StalenessTolerance: metav1.Duration{Duration: c.config.SyncPeriod * 2},
			FailoverTimeout:    metav1.Duration{Duration: c.config.FailoverTimeout},
			RebalancePolicy:    apisv1.RebalancePolicyMinimalMove,
			RebalanceThreshold: c.config.RebalanceThreshold,
		},
	}

	_, err := c.crdClient.SchedulingV1().ParSyncConfigs().Create(ctx, psc, metav1.CreateOptions{})
	if errors.IsAlreadyExists(err) {
		existing, getErr := c.crdClient.SchedulingV1().ParSyncConfigs().Get(ctx, c.config.DefaultConfigName, metav1.GetOptions{})
		if getErr != nil {
			return fmt.Errorf("get existing ParSyncConfig: %w", getErr)
		}
		psc.ResourceVersion = existing.ResourceVersion
		_, err = c.crdClient.SchedulingV1().ParSyncConfigs().Update(ctx, psc, metav1.UpdateOptions{})
	}
	return err
}

// loadExistingAssignments reads existing SchedulerAssignment CRDs to rebuild local state.
func (c *ParSyncCoordinator) loadExistingAssignments(ctx context.Context) error {
	if c.crdClient == nil {
		return nil
	}

	list, err := c.crdClient.SchedulingV1().SchedulerAssignments().List(ctx, metav1.ListOptions{})
	if err != nil {
		return fmt.Errorf("list SchedulerAssignments: %w", err)
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	for i := range list.Items {
		sa := &list.Items[i]
		c.currentAssignments[sa.Spec.SchedulerName] = sa.Spec.AssignedPartitions
		if sa.Spec.ConfigGeneration > c.configGeneration {
			c.configGeneration = sa.Spec.ConfigGeneration
		}
		// Restore the (name, SchedulerID) binding into the registry so that
		// a subsequent Pod-Informer-driven Register(name) reuses the persisted
		// ID instead of allocating a new one. Without this, a Dispatcher
		// restart whose Pod Informer lists scheduler pods in a different
		// order than before reshuffles SchedulerIDs and diverges from the
		// SyncSlots the live Scheduler already cached at its own startup.
		var lastHb time.Time
		if sa.Status.LastHeartbeat != nil {
			lastHb = sa.Status.LastHeartbeat.Time
		}
		c.registry.PreloadFromCRD(sa.Spec.SchedulerName, sa.Spec.SchedulerID, sa.Status.Phase, lastHb)
	}

	klog.InfoS("Loaded existing assignments",
		"count", len(list.Items), "generation", c.configGeneration)
	return nil
}

// runHealthCheck periodically checks scheduler heartbeats and triggers rebalance on failures.
//
// Before each CheckHealth, heartbeats from the CRD are refreshed into the
// in-memory registry via refreshHeartbeatsFromCRD. Without this refresh the
// registry's LastHeartbeat is only set at Register time, so every scheduler
// would be marked Failed after failoverTimeout even when the Pod is fully
// healthy and the Scheduler is actively patching its CRD status.
func (c *ParSyncCoordinator) runHealthCheck(ctx context.Context) {
	ticker := time.NewTicker(c.config.HealthCheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-c.stopCh:
			return
		case <-ticker.C:
			c.refreshHeartbeatsFromCRD(ctx)
			failed := c.registry.CheckHealth()
			if len(failed) > 0 {
				for _, f := range failed {
					klog.InfoS("Scheduler health check failed", "name", f.Name, "lastHeartbeat", f.LastHeartbeat)
				}
				if err := c.triggerRebalance(ctx); err != nil {
					klog.ErrorS(err, "Rebalance after health check failure")
				}
			}
		}
	}
}

// refreshHeartbeatsFromCRD pulls status.lastHeartbeat from every existing
// SchedulerAssignment CRD and feeds it into the in-memory registry. This is
// the counterpart to the Scheduler-side StartHeartbeatLoop: the Scheduler
// patches its CRD status every few seconds, the Dispatcher reads those
// patches each HealthCheckInterval to decide liveness.
//
// Periodic List is used rather than a CRD Informer to avoid pulling the
// full para-sched-api informer factory into the dispatcher binary just for
// one field. At cluster scale (<~100 schedulers) the List is O(KB) and fires
// every HealthCheckInterval (default 5s), which is negligible.
//
// Errors are logged at V(2) only (transient API server blips must not stop
// the health loop; on repeated failure schedulers will legitimately expire
// and fail over).
func (c *ParSyncCoordinator) refreshHeartbeatsFromCRD(ctx context.Context) {
	if c.crdClient == nil {
		return
	}
	list, err := c.crdClient.SchedulingV1().SchedulerAssignments().List(ctx, metav1.ListOptions{})
	if err != nil {
		klog.V(2).InfoS("Failed to list SchedulerAssignments for heartbeat refresh", "err", err)
		return
	}
	for i := range list.Items {
		sa := &list.Items[i]
		if sa.Status.LastHeartbeat == nil {
			continue
		}
		// Only refresh if the CRD heartbeat is newer than what we have in memory.
		// Registry.Heartbeat stamps time.Now() internally so any call refreshes
		// the registry to "now" — we gate on CRD freshness to avoid resurrecting
		// a scheduler whose CRD status is stale (e.g. scheduler crashed but CRD
		// not yet GCed).
		if time.Since(sa.Status.LastHeartbeat.Time) > c.config.FailoverTimeout {
			continue
		}
		c.registry.Heartbeat(sa.Name, HeartbeatStatus{Phase: sa.Status.Phase})
	}
}

// runRebalanceCheck periodically checks if partition distribution is balanced.
func (c *ParSyncCoordinator) runRebalanceCheck(ctx context.Context) {
	ticker := time.NewTicker(c.config.RebalanceInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-c.stopCh:
			return
		case <-ticker.C:
			active := c.registry.GetActiveSchedulers()
			c.mu.RLock()
			needsRebalance := c.rebalancer.NeedsRebalance(
				active, c.config.NumPartitions, c.currentAssignments, c.config.RebalanceThreshold)
			c.mu.RUnlock()

			if needsRebalance {
				klog.InfoS("Periodic rebalance check detected imbalance")
				if err := c.triggerRebalance(ctx); err != nil {
					klog.ErrorS(err, "Periodic rebalance failed")
				}
			}
		}
	}
}

// runClockAdvance advances the epoch every sync period G.
func (c *ParSyncCoordinator) runClockAdvance(ctx context.Context) {
	ticker := time.NewTicker(c.config.SyncPeriod)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-c.stopCh:
			return
		case <-ticker.C:
			c.clockManager.AdvanceEpoch()
			klog.V(5).InfoS("Clock epoch advanced", "epoch", c.clockManager.GetCurrentEpoch())
		}
	}
}
