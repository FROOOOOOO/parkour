// Package dispatcher implements scheduler registration and health management.
package dispatcher

import (
	"sort"
	"sync"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
)

// HeartbeatStatus carries the status information reported by a scheduler heartbeat.
type HeartbeatStatus struct {
	Phase apisv1.SchedulerPhase
}

// RegistrySchedulerInfo tracks the state of a registered scheduler instance.
type RegistrySchedulerInfo struct {
	Name               string
	ID                 int                   // unique numeric ID for SyncSlot offset calculation
	Phase              apisv1.SchedulerPhase // Pending|Running|Degraded|Failed
	LastHeartbeat      time.Time
	AssignedPartitions []int // currently assigned partitions
	Healthy            bool
}

// SchedulerRegistry manages scheduler registration, ID allocation, and health tracking.
//
// ID allocation guarantees:
//  1. IDs surface through `usedIDs`. An ID in `usedIDs` is never handed out
//     by `allocateID` even after its owner is Deregistered.
//  2. After Deregister, the ID enters `cooldownIDs` with a release deadline.
//     Until that deadline passes, the ID stays reserved — preventing a
//     zombie scheduler (whose pod is no longer tracked by the Dispatcher
//     but whose process may still use the old ID) from colliding with a
//     newly registered scheduler that would otherwise pick the same ID.
//  3. PreloadFromCRD seeds the registry at startup from persisted
//     SchedulerAssignment CRDs, so a Dispatcher restart does not reshuffle
//     SchedulerID values and diverge from the Spec the live Schedulers
//     already read at their own startup.
type SchedulerRegistry struct {
	mu              sync.RWMutex
	schedulers      map[string]*RegistrySchedulerInfo
	nextSchedulerID int
	usedIDs         map[int]bool         // IDs currently reserved (active + cooldown)
	cooldownIDs     map[int]time.Time    // ID -> earliest time it can be reused
	cooldownPeriod  time.Duration        // how long a released ID stays reserved
	failoverTimeout time.Duration        // heartbeat timeout threshold (default 15s)
}

// NewSchedulerRegistry creates a new SchedulerRegistry with the given failover
// timeout and ID cooldown period. If cooldownPeriod <= 0, IDs are released
// immediately on Deregister (legacy behavior).
func NewSchedulerRegistry(failoverTimeout, cooldownPeriod time.Duration) *SchedulerRegistry {
	return &SchedulerRegistry{
		schedulers:      make(map[string]*RegistrySchedulerInfo),
		usedIDs:         make(map[int]bool),
		cooldownIDs:     make(map[int]time.Time),
		cooldownPeriod:  cooldownPeriod,
		failoverTimeout: failoverTimeout,
	}
}

// Register registers a new scheduler and allocates a unique ID.
// Returns (info, isNew). If the scheduler already exists, returns existing info with isNew=false.
func (r *SchedulerRegistry) Register(name string) (*RegistrySchedulerInfo, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if info, ok := r.schedulers[name]; ok {
		// Already registered — reset to healthy state.
		info.Healthy = true
		info.Phase = apisv1.SchedulerPhasePending
		info.LastHeartbeat = time.Now()
		return info, false
	}

	id := r.allocateID()
	info := &RegistrySchedulerInfo{
		Name:          name,
		ID:            id,
		Phase:         apisv1.SchedulerPhasePending,
		LastHeartbeat: time.Now(),
		Healthy:       true,
	}
	r.schedulers[name] = info
	return info, true
}

// Deregister removes a scheduler. Its ID enters the cooldown window rather
// than being released immediately, so that the ID cannot be handed out to a
// newly-registering scheduler while the old scheduler's process may still be
// alive and using the ID locally (e.g. network-partitioned kubelet marks the
// pod NotReady but the scheduler process itself is still running).
//
// If cooldownPeriod <= 0, releases the ID immediately (legacy behavior).
func (r *SchedulerRegistry) Deregister(name string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	info, ok := r.schedulers[name]
	if !ok {
		return
	}
	delete(r.schedulers, name)

	if r.cooldownPeriod <= 0 {
		r.releaseID(info.ID)
		return
	}
	// Keep the ID in usedIDs; schedule its release.
	r.cooldownIDs[info.ID] = time.Now().Add(r.cooldownPeriod)
}

// Heartbeat updates the heartbeat time and phase for a scheduler.
func (r *SchedulerRegistry) Heartbeat(name string, status HeartbeatStatus) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if info, ok := r.schedulers[name]; ok {
		info.LastHeartbeat = time.Now()
		info.Phase = status.Phase
		info.Healthy = true
	}
}

// GetActiveSchedulers returns all healthy schedulers in Pending/Initializing/Running state,
// sorted by ID for deterministic ordering.
func (r *SchedulerRegistry) GetActiveSchedulers() []*RegistrySchedulerInfo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	var active []*RegistrySchedulerInfo
	for _, info := range r.schedulers {
		if !info.Healthy {
			continue
		}
		switch info.Phase {
		case apisv1.SchedulerPhasePending, apisv1.SchedulerPhaseInitializing, apisv1.SchedulerPhaseRunning:
			cp := *info
			if info.AssignedPartitions != nil {
				cp.AssignedPartitions = make([]int, len(info.AssignedPartitions))
				copy(cp.AssignedPartitions, info.AssignedPartitions)
			}
			active = append(active, &cp)
		}
	}

	sort.Slice(active, func(i, j int) bool {
		return active[i].ID < active[j].ID
	})
	return active
}

// CheckHealth checks all schedulers' heartbeats and marks timed-out ones as Failed.
// Returns the list of newly failed schedulers.
func (r *SchedulerRegistry) CheckHealth() []*RegistrySchedulerInfo {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := time.Now()
	var failed []*RegistrySchedulerInfo
	for _, info := range r.schedulers {
		if !info.Healthy {
			continue
		}
		if now.Sub(info.LastHeartbeat) > r.failoverTimeout {
			info.Healthy = false
			info.Phase = apisv1.SchedulerPhaseFailed
			cp := *info
			failed = append(failed, &cp)
		}
	}
	return failed
}

// UpdateAssignedPartitions updates the partition assignment for a scheduler (called after rebalance).
func (r *SchedulerRegistry) UpdateAssignedPartitions(name string, partitions []int) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if info, ok := r.schedulers[name]; ok {
		info.AssignedPartitions = make([]int, len(partitions))
		copy(info.AssignedPartitions, partitions)
	}
}

// GetScheduler returns a copy of a scheduler's info, or nil if not found.
func (r *SchedulerRegistry) GetScheduler(name string) *RegistrySchedulerInfo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	if info, ok := r.schedulers[name]; ok {
		cp := *info
		return &cp
	}
	return nil
}

// Count returns the total number of registered schedulers.
func (r *SchedulerRegistry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.schedulers)
}

// allocateID picks the smallest available ID, honoring the cooldown window:
// IDs whose cooldown has expired are first released back into the pool, then
// the smallest id such that usedIDs[id]==false is returned.
// Must be called with r.mu held.
func (r *SchedulerRegistry) allocateID() int {
	r.pruneCooldownLocked()

	for id := 0; id < r.nextSchedulerID; id++ {
		if !r.usedIDs[id] {
			r.usedIDs[id] = true
			return id
		}
	}
	id := r.nextSchedulerID
	r.usedIDs[id] = true
	r.nextSchedulerID++
	return id
}

// pruneCooldownLocked releases any cooldown IDs whose deadline has passed.
// Must be called with r.mu held.
func (r *SchedulerRegistry) pruneCooldownLocked() {
	now := time.Now()
	for id, deadline := range r.cooldownIDs {
		if !now.Before(deadline) {
			delete(r.cooldownIDs, id)
			r.releaseID(id)
		}
	}
}

// releaseID marks an ID as available for reuse. Must be called with r.mu held.
func (r *SchedulerRegistry) releaseID(id int) {
	delete(r.usedIDs, id)
}

// PreloadFromCRD seeds an existing (name, id) binding into the registry
// without triggering a rebalance. Used at Dispatcher startup to recover
// SchedulerID assignments from SchedulerAssignment CRDs so that the next
// Register(name) for the same name (driven by Pod Informer sync) reuses the
// persisted ID, keeping the Scheduler-side cached SyncSlots consistent with
// the CRD Spec.
//
// The preloaded entry is marked Healthy=true with LastHeartbeat taken from
// the CRD (or time.Now() if the CRD has no heartbeat yet). This is so that:
//   - if the scheduler pod is still alive, the next Pod-Informer-driven
//     Register finds the name and keeps the same ID;
//   - if the scheduler pod is gone, the next CheckHealth tick sees an
//     expired LastHeartbeat and marks it Failed, triggering rebalance.
//
// If id is already reserved (by another preload or by a live scheduler),
// the preload is skipped.
func (r *SchedulerRegistry) PreloadFromCRD(name string, id int, phase apisv1.SchedulerPhase, lastHeartbeat time.Time) {
	if id < 0 || name == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, exists := r.schedulers[name]; exists {
		return
	}
	if r.usedIDs[id] {
		return
	}
	if lastHeartbeat.IsZero() {
		lastHeartbeat = time.Now()
	}
	r.usedIDs[id] = true
	if id >= r.nextSchedulerID {
		r.nextSchedulerID = id + 1
	}
	r.schedulers[name] = &RegistrySchedulerInfo{
		Name:          name,
		ID:            id,
		Phase:         phase,
		LastHeartbeat: lastHeartbeat,
		Healthy:       true,
	}
}
