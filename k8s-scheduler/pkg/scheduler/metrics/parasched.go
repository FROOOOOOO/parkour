/*
Copyright 2026 The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package metrics

import (
	"k8s.io/component-base/metrics"
)

// para-sched custom metrics.
// These are registered together with the standard scheduler metrics via
// InitParaSchedMetrics(), which is called from InitMetrics().
var (
	// ParaSchedCandidateSelectionDuration measures the latency of the
	// multi-candidate selection phase (including sort + strategy).
	ParaSchedCandidateSelectionDuration *metrics.Histogram

	// ParaSchedPenaltyLookupDuration measures the latency of applying
	// conflict-rate penalties to node scores.
	ParaSchedPenaltyLookupDuration *metrics.Histogram

	// ParaSchedSelectedNodeScore records the score of the node that was
	// ultimately selected as the primary candidate.
	ParaSchedSelectedNodeScore *metrics.Histogram

	// ParaSchedSyncDuration measures the latency of a single partition
	// snapshot application in ParSync mode.
	ParaSchedSyncDuration *metrics.Histogram

	// ParaSchedPartitionStaleness records the staleness (in seconds) of
	// the partition snapshot when it is applied.
	ParaSchedPartitionStaleness *metrics.Histogram

	// ParaSchedFirstSnapshotApplied marks (as a Unix timestamp gauge) the
	// first time each partition's snapshot was successfully applied.
	// Cold-start observability per docs/design-parsync-pull-fix.md §4.6.4:
	// the latest of these across all partitions defines the cold-start
	// completion time. Reported once per partition via SetToCurrentTime.
	ParaSchedFirstSnapshotApplied *metrics.GaugeVec

	// ParaSchedAssumedPodCount samples the current number of assumed pods
	// in the scheduler's cache. In ParSync steady state this should hover
	// around (in-flight pods being assumed) and drain on every rotation
	// window via UpdateNodeResources. A monotonically growing gauge is
	// the symptom of either a cleanup-path bug (§4.5) or a node with
	// partitionID=-1 retaining assumed pods forever.
	ParaSchedAssumedPodCount *metrics.Gauge
)

// InitParaSchedMetrics creates the para-sched metric objects.
// Called from InitMetrics().
func InitParaSchedMetrics() {
	ParaSchedCandidateSelectionDuration = metrics.NewHistogram(
		&metrics.HistogramOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_candidate_selection_duration_seconds",
			Help:           "Latency of the para-sched multi-candidate selection phase in seconds.",
			Buckets:        metrics.ExponentialBuckets(0.0001, 2, 12), // 0.1ms … ~200ms
			StabilityLevel: metrics.ALPHA,
		},
	)

	ParaSchedPenaltyLookupDuration = metrics.NewHistogram(
		&metrics.HistogramOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_penalty_lookup_duration_seconds",
			Help:           "Latency of applying conflict-rate penalty to node scores in seconds.",
			Buckets:        metrics.ExponentialBuckets(0.00001, 2, 10), // 10us … ~5ms
			StabilityLevel: metrics.ALPHA,
		},
	)

	ParaSchedSelectedNodeScore = metrics.NewHistogram(
		&metrics.HistogramOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_selected_node_score",
			Help:           "Score of the primary candidate node selected by para-sched.",
			Buckets:        metrics.LinearBuckets(0, 10, 11), // 0, 10, 20, … 100
			StabilityLevel: metrics.ALPHA,
		},
	)

	ParaSchedSyncDuration = metrics.NewHistogram(
		&metrics.HistogramOpts{
			Subsystem: SchedulerSubsystem,
			Name:      "parasched_sync_duration_seconds",
			Help:      "Latency of applying a single partition snapshot in ParSync mode.",
			// 0.1ms … ~3.3s. The previous 12-bucket range capped at 204.8ms,
			// which the un-batched per-node apply path consistently exceeded
			// on 10000-node P=1 snapshots — every glob trial reported
			// sync_duration_p99 = 204.8ms (bucket-saturated). 16 buckets
			// give the batched apply enough headroom while still surfacing
			// regressions if a future change reintroduces lock contention.
			Buckets:        metrics.ExponentialBuckets(0.0001, 2, 16),
			StabilityLevel: metrics.ALPHA,
		},
	)

	ParaSchedPartitionStaleness = metrics.NewHistogram(
		&metrics.HistogramOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_partition_staleness_seconds",
			Help:           "Staleness of a partition snapshot at the time it is applied (now - snapshot.Timestamp).",
			Buckets:        metrics.ExponentialBuckets(0.01, 2, 12), // 10ms … ~20s
			StabilityLevel: metrics.ALPHA,
		},
	)

	ParaSchedFirstSnapshotApplied = metrics.NewGaugeVec(
		&metrics.GaugeOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_first_snapshot_applied_timestamp_seconds",
			Help:           "Unix timestamp (seconds) when each partition's first snapshot was applied by this scheduler.",
			StabilityLevel: metrics.ALPHA,
		},
		[]string{"partition"},
	)

	ParaSchedAssumedPodCount = metrics.NewGauge(
		&metrics.GaugeOpts{
			Subsystem:      SchedulerSubsystem,
			Name:           "parasched_assumed_pod_count",
			Help:           "Current number of assumed pods in the scheduler cache.",
			StabilityLevel: metrics.ALPHA,
		},
	)
}

// ParaSchedMetricsList returns the list of para-sched metrics for registration.
func ParaSchedMetricsList() []metrics.Registerable {
	return []metrics.Registerable{
		ParaSchedCandidateSelectionDuration,
		ParaSchedPenaltyLookupDuration,
		ParaSchedSelectedNodeScore,
		ParaSchedSyncDuration,
		ParaSchedPartitionStaleness,
		ParaSchedFirstSnapshotApplied,
		ParaSchedAssumedPodCount,
	}
}
