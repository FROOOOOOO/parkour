package binder

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"time"

	parasched "example.com/para-sched-api/generated/clientset/versioned"
	libstats "example.com/scheduler-lib/stats"
	"example.com/scheduler-lib/types"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/util/retry"
	"k8s.io/klog/v2"
)

// BindingReporter buffers binding results in memory and periodically flushes
// aggregated statistics to the AdoptionStats CRD.
//
// Statistics aggregation and conflict rate calculation are delegated to
// scheduler-lib (AdoptionStatsCache / ProbabilityCalculator) per the
// "scheduler-lib reuse" design principle. This file only handles the
// CRD read/write transport layer.
type BindingReporter struct {
	crdClient   parasched.Interface
	statsName   string        // AdoptionStats CR name (singleton per cluster)
	flushPeriod time.Duration // default 1s

	mu     sync.Mutex
	buffer []types.BindingResult

	// scheduler-lib algorithm components (single source of truth for stats).
	statsCache *libstats.AdoptionStatsCache
	calculator *libstats.ProbabilityCalculator
}

// NewBindingReporter creates a reporter that writes to the named AdoptionStats CR.
func NewBindingReporter(crdClient parasched.Interface, statsName string, flushPeriod time.Duration) *BindingReporter {
	if flushPeriod <= 0 {
		flushPeriod = 1 * time.Second
	}
	return &BindingReporter{
		crdClient:   crdClient,
		statsName:   statsName,
		flushPeriod: flushPeriod,
		statsCache:  libstats.NewAdoptionStatsCache(libstats.DefaultStatsConfig()),
		calculator:  libstats.NewProbabilityCalculator(),
	}
}

// Record appends a binding result to the in-memory buffer. Non-blocking.
func (r *BindingReporter) Record(result types.BindingResult) {
	r.mu.Lock()
	r.buffer = append(r.buffer, result)
	r.mu.Unlock()
}

// Run starts the periodic flush loop. Blocks until ctx is cancelled.
//
// Before entering the flush loop, preloads statsCache from the existing
// AdoptionStats CRD (if any) — this prevents the first post-restart flush
// from overwriting persisted SuccessCount/FailureCount/GlobalCounts with
// zeros drawn from a fresh (empty) in-memory cache. The preload is
// best-effort: NotFound is expected on first-ever run, any other error
// logs and proceeds from zero (degraded but not fatal).
//
// A background Decay goroutine is also launched: it periodically decays
// globalCounts / partitionCounts / summary counts in statsCache at
// DecayInterval (default 1min, DecayFactor 0.95). Without it the CRD's
// SuccessCount/FailureCount grow monotonically and the Scheduler-side
// CalculateConflictRate reflects accumulated (not recent) conflict rate,
// which freezes the Penalty after enough samples are collected.
func (r *BindingReporter) Run(ctx context.Context) {
	r.preloadFromCRD(ctx)

	// Decay loop runs in a separate goroutine so its cadence is independent
	// of the flush cadence.
	go r.statsCache.RunDecayLoop(ctx.Done())

	ticker := time.NewTicker(r.flushPeriod)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			if err := r.flush(ctx); err != nil {
				klog.ErrorS(err, "Failed to flush binding stats")
			}
		case <-ctx.Done():
			// Final flush on shutdown.
			_ = r.flush(context.Background())
			return
		}
	}
}

// flush drains the buffer, aggregates results, and writes to AdoptionStats CRD status.
//
// Concurrency / conflict handling:
//   - statsCache is the authoritative in-memory aggregate. Apply the drained
//     batch to it exactly ONCE, before entering the retry loop — re-applying
//     on retry would double-count. After that, every retry rebuilds the CRD
//     status from the same statsCache snapshot, so writing it N times is
//     idempotent.
//   - UpdateStatus can fail with 409 Conflict when another writer (or a
//     controller touching the CR) races us. Without retry, the batch is
//     already drained from r.buffer so the write attempt's results would be
//     delayed until the next flush picks them up from statsCache — not a
//     data loss but unnecessary latency and error noise. retry.RetryOnConflict
//     re-fetches the latest resourceVersion and reapplies, which is
//     conventional for status subresource writes.
func (r *BindingReporter) flush(ctx context.Context) error {
	r.mu.Lock()
	if len(r.buffer) == 0 {
		r.mu.Unlock()
		return nil
	}
	batch := r.buffer
	r.buffer = nil
	r.mu.Unlock()

	// Delegate aggregation to scheduler-lib's AdoptionStatsCache (once).
	for _, res := range batch {
		r.statsCache.UpdateResult(res)
	}

	// Collect unique partition IDs present in this batch. PartitionCounts
	// write is batch-scoped: we only refresh the CRD entries for partitions
	// that actually saw activity this round (both success and failure paths,
	// since scheduler-lib's updatePartitionStats aggregates failures into
	// counts[len-1] — filtering by Success here would lose the failure
	// dimension of per-partition conflict rate used by P3/P4 experiments).
	seenPartitions := make(map[int]bool, len(batch))
	for _, res := range batch {
		seenPartitions[res.PartitionID] = true
	}

	retryErr := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		crdStats, err := r.crdClient.SchedulingV1().AdoptionStatses().Get(ctx, r.statsName, metav1.GetOptions{})
		if err != nil {
			return err
		}

		status := &crdStats.Status

		// Read the aggregated summary from scheduler-lib on each retry so
		// late-arriving Record() calls are also reflected (they share the
		// same statsCache, so no staleness risk — just freshness bonus).
		summary := r.statsCache.GetStats()
		status.TotalBindings = summary.TotalBindings
		status.SuccessCount = summary.SuccessCount
		status.FailureCount = summary.FailureCount
		status.ConflictRate = strconv.FormatFloat(summary.ConflictRate, 'f', 4, 64)
		status.AvgAttempts = strconv.FormatFloat(summary.AvgAttempts, 'f', 2, 64)
		status.GlobalCounts = summary.RankDistribution

		if status.PartitionCounts == nil {
			status.PartitionCounts = make(map[string][]int64)
		}
		for pid := range seenPartitions {
			pc := r.statsCache.GetPartitionCounts(pid)
			if pc != nil {
				status.PartitionCounts[strconv.Itoa(pid)] = pc
			}
		}

		// Per-node [attempts, conflicts] counts for the Scheduler's Penalty mechanism.
		// Sparse: includes only nodes that have at least one recorded conflict; scheduler
		// defaults absent nodes to 0 conflict rate. Values are decayed over time via
		// AdoptionStatsCache.Decay (every DecayInterval), implementing the "recent data
		// weighted higher" requirement without transmitting raw sliding windows.
		// Full replacement (not merge) — nodes that drop to zero conflicts after decay
		// will fall out of the map and the scheduler auto-zeros their penalty.
		status.NodeCounts = r.statsCache.GetNodeCountsMap(false)

		now := metav1.Now()
		status.LastUpdateTime = &now
		crdStats.Status = *status

		_, err = r.crdClient.SchedulingV1().AdoptionStatses().UpdateStatus(ctx, crdStats, metav1.UpdateOptions{})
		return err
	})
	if retryErr != nil {
		// On persistent conflict/other errors after retries, statsCache still
		// holds the aggregated state — the next flush will try again with the
		// cumulative summary, so this batch's data is not lost.
		if apierrors.IsConflict(retryErr) {
			return fmt.Errorf("update AdoptionStats status (exhausted conflict retries): %w", retryErr)
		}
		return fmt.Errorf("update AdoptionStats status: %w", retryErr)
	}

	summary := r.statsCache.GetStats()
	klog.V(4).InfoS("Flushed binding stats", "count", len(batch),
		"total", summary.TotalBindings, "conflicts", summary.FailureCount)
	return nil
}

// preloadFromCRD reads the existing AdoptionStats CRD once at startup and
// restores cumulative counts into statsCache, so that the first post-restart
// flush does not overwrite persisted history with zero values.
//
// Semantics:
//   - NotFound: expected on first-ever run — leave statsCache at zero state
//   - Other Get errors: log and proceed with zero state (degraded, but a
//     single lost snapshot is preferable to blocking the Binder on startup)
//   - Success: call statsCache.LoadSummary; the next flush will preserve history
//
// Paper-context note: different experiment groups run against the same CRD
// by convention only if the CRD is cleared between runs. When the CRD is
// cleared (deleted or status zeroed), preload correctly sees zeros and the
// new run starts fresh — so this preload does not leak state across groups.
func (r *BindingReporter) preloadFromCRD(ctx context.Context) {
	crdStats, err := r.crdClient.SchedulingV1().AdoptionStatses().Get(ctx, r.statsName, metav1.GetOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			klog.V(3).InfoS("AdoptionStats CRD not found on preload, starting with empty stats", "name", r.statsName)
			return
		}
		klog.ErrorS(err, "Failed to preload AdoptionStats from CRD, starting with empty stats", "name", r.statsName)
		return
	}

	status := crdStats.Status

	// Convert CRD's partitionCounts (map[string][]int64, keyed by partition
	// stringified) into the cache's canonical map[int][]int64.
	var partitionCounts map[int][]int64
	if len(status.PartitionCounts) > 0 {
		partitionCounts = make(map[int][]int64, len(status.PartitionCounts))
		for key, counts := range status.PartitionCounts {
			pid, convErr := strconv.Atoi(key)
			if convErr != nil {
				klog.V(4).InfoS("Skipping partitionCounts entry with non-integer key", "key", key)
				continue
			}
			partitionCounts[pid] = counts
		}
	}

	r.statsCache.LoadSummary(
		status.TotalBindings,
		status.SuccessCount,
		status.FailureCount,
		status.GlobalCounts,
		partitionCounts,
	)

	klog.InfoS("Preloaded AdoptionStats from CRD",
		"totalBindings", status.TotalBindings,
		"successCount", status.SuccessCount,
		"failureCount", status.FailureCount,
		"partitions", len(partitionCounts))
}

// BufferLen returns the current number of buffered results (for testing).
func (r *BindingReporter) BufferLen() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.buffer)
}
