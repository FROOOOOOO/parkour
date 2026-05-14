// Package metrics registers Prometheus counters and histograms for the
// Binder and Dispatcher components.
//
// Metric naming follows the convention: parasched_<component>_<metric>_<unit>.
// All metrics are registered in the default prometheus registry and exposed
// via an HTTP /metrics endpoint started by StartMetricsServer().
package metrics

import (
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"k8s.io/klog/v2"
)

// Reason label values for SnapshotPublishErrorsTotal. Kept in one place so
// the classification helper, dashboard queries, and process-results.py stay
// in sync; adding a new reason without bumping this list is a label-cardinality
// audit failure waiting to happen.
const (
	PublishReasonThrottled = "throttled"
	PublishReasonConflict  = "conflict"
	PublishReasonTimeout   = "timeout"
	PublishReasonOther     = "other"
)

var (
	// BindAttemptsTotal counts every bind attempt with detailed labels for debugging.
	// WARNING: "node" label creates high-cardinality series (one per node).
	// For aggregated queries (sum, rate, increase), use BindResultTotal instead.
	BindAttemptsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_bind_attempts_total",
			Help: "Total number of bind attempts, labelled by result, node, and candidate rank.",
		},
		[]string{"result", "node", "rank"},
	)

	// BindResultTotal is a low-cardinality counter for reliable aggregated queries.
	// Only labelled by result (success/conflict), safe for sum(increase(...)).
	BindResultTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_bind_result_total",
			Help: "Total number of bind attempts by result (low-cardinality, for aggregation).",
		},
		[]string{"result"},
	)

	// BindDurationSeconds measures the wall-clock time of a single pod
	// binding cycle (from first candidate attempt to final outcome).
	BindDurationSeconds = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "parasched_bind_duration_seconds",
			Help:    "Duration of a pod binding cycle in seconds.",
			Buckets: prometheus.ExponentialBuckets(0.001, 2, 14), // 1ms … ~8s
		},
		[]string{"result"},
	)

	// CandidateRankAccepted records which candidate rank was ultimately
	// adopted for each successful bind.
	CandidateRankAccepted = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "parasched_candidate_rank_accepted",
			Help:    "Rank of the candidate node that was successfully bound (0=primary, 1=backup-1, ...).",
			Buckets: prometheus.LinearBuckets(0, 1, 11), // 0..10
		},
	)

	// AllCandidatesFailedTotal counts pods where every candidate failed.
	AllCandidatesFailedTotal = prometheus.NewCounter(
		prometheus.CounterOpts{
			Name: "parasched_all_candidates_failed_total",
			Help: "Total number of pods where all candidate nodes failed.",
		},
	)

	// SnapshotOversizeTotal counts partition snapshots dropped because a
	// single chunk's marshalled payload exceeded the ConfigMap 1MB limit
	// after chunking. In current code this path is fatal (klog.Fatalf in
	// snapshot.go) so the counter is reserved: a non-zero value would
	// indicate the fatal handling was relaxed, and warrants investigation.
	SnapshotOversizeTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_snapshot_oversize_total",
			Help: "Total number of partition snapshots dropped due to exceeding the ConfigMap size limit.",
		},
		[]string{"partition"},
	)

	// SnapshotPublishErrorsTotal counts transient ConfigMap publish failures
	// (Update/Create), broken down by reason. Unlike SnapshotOversizeTotal
	// these are retried by re-marking the partition dirty; a sustained
	// non-zero rate points to client-side QPS throttling or apiserver pressure.
	SnapshotPublishErrorsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_snapshot_publish_errors_total",
			Help: "Total number of ConfigMap chunk publish failures, labelled by partition and reason.",
		},
		[]string{"partition", "reason"},
	)

	// SnapshotPublishChunks counts the number of ConfigMap chunks published
	// per partition (sum over time). Verifies that chunking is working:
	// for a 10000-node glob partition this should climb in increments of 2.
	SnapshotPublishChunks = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_snapshot_publish_chunks_total",
			Help: "Total number of ConfigMap chunks published per partition.",
		},
		[]string{"partition"},
	)

	// ---- Dispatcher metrics ----

	// DispatchTotal counts every dispatch attempt by result and target scheduler.
	DispatchTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "parasched_dispatch_total",
			Help: "Total number of pod dispatch attempts, labelled by result and scheduler.",
		},
		[]string{"result", "scheduler"},
	)

	// DispatchDurationSeconds measures wall-clock time of a single dispatch
	// operation (scheduler selection + pod annotation patch).
	DispatchDurationSeconds = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "parasched_dispatch_duration_seconds",
			Help:    "Duration of a single pod dispatch in seconds.",
			Buckets: prometheus.ExponentialBuckets(0.0001, 2, 14), // 0.1ms … ~800ms
		},
	)

	// DispatcherQueueDepth reports the current number of pods waiting in the
	// dispatcher work queue.
	DispatcherQueueDepth = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "parasched_dispatcher_queue_depth",
			Help: "Current number of pods in the dispatcher work queue.",
		},
	)

	// DispatcherSchedulerInflight reports the in-flight pod count per scheduler
	// instance (pods dispatched but not yet bound).
	DispatcherSchedulerInflight = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "parasched_dispatcher_scheduler_inflight",
			Help: "Number of in-flight pods per scheduler instance.",
		},
		[]string{"scheduler"},
	)

	// DispatcherReadyAt is the Unix timestamp (seconds) when the
	// Dispatcher's /ready endpoint first returned 200. Cold-start
	// observability per docs/design-parsync-pull-fix.md §4.6.4 — used to
	// validate that initContainer gating actually held off Scheduler /
	// Binder pods until partition labels were assigned. Stays at 0 until
	// the first ready response.
	DispatcherReadyAt = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "parasched_dispatcher_ready_timestamp_seconds",
			Help: "Unix timestamp (seconds) when the dispatcher first signaled readiness; 0 if never ready.",
		},
	)
)

func init() {
	prometheus.MustRegister(
		// Binder metrics
		BindAttemptsTotal,
		BindResultTotal,
		BindDurationSeconds,
		CandidateRankAccepted,
		AllCandidatesFailedTotal,
		SnapshotOversizeTotal,
		SnapshotPublishErrorsTotal,
		SnapshotPublishChunks,
		// Dispatcher metrics
		DispatchTotal,
		DispatchDurationSeconds,
		DispatcherQueueDepth,
		DispatcherSchedulerInflight,
		DispatcherReadyAt,
	)
}

// StartMetricsServer starts an HTTP server exposing /metrics on the given addr
// (e.g. ":8080"). Call this from main as a goroutine.
func StartMetricsServer(addr string) {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	klog.InfoS("Starting Prometheus metrics server", "addr", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		klog.ErrorS(err, "Metrics server failed")
	}
}

// ---- Recording helpers (called from binder.go) ----

// RecordConflict records a conflict event for a candidate node.
func RecordConflict(nodeName, reason string) {
	BindAttemptsTotal.WithLabelValues("conflict", nodeName, "").Inc()
	BindResultTotal.WithLabelValues("conflict").Inc()
	klog.V(5).InfoS("Conflict recorded", "node", nodeName, "reason", reason)
}

// RecordBindSuccess records a successful bind.
func RecordBindSuccess(nodeName string, rank int, duration time.Duration) {
	rankStr := rankToString(rank)
	BindAttemptsTotal.WithLabelValues("success", nodeName, rankStr).Inc()
	BindResultTotal.WithLabelValues("success").Inc()
	BindDurationSeconds.WithLabelValues("success").Observe(duration.Seconds())
	CandidateRankAccepted.Observe(float64(rank))
	klog.V(5).InfoS("Bind success recorded",
		"node", nodeName, "rank", rank, "duration", duration)
}

// RecordAllCandidatesFailed records when all candidates fail for a pod.
func RecordAllCandidatesFailed(podKey string) {
	AllCandidatesFailedTotal.Inc()
	klog.V(4).InfoS("All candidates failed", "pod", podKey)
}

// ---- Recording helpers (called from dispatcher.go) ----

// RecordDispatchSuccess records a successful pod dispatch.
func RecordDispatchSuccess(schedulerName string, duration time.Duration) {
	DispatchTotal.WithLabelValues("success", schedulerName).Inc()
	DispatchDurationSeconds.Observe(duration.Seconds())
	DispatcherSchedulerInflight.WithLabelValues(schedulerName).Inc()
}

// RecordDispatchError records a failed pod dispatch.
func RecordDispatchError(schedulerName string) {
	DispatchTotal.WithLabelValues("error", schedulerName).Inc()
}

// RecordSchedulerDecrement decrements the in-flight gauge when a pod finishes.
func RecordSchedulerDecrement(schedulerName string) {
	DispatcherSchedulerInflight.WithLabelValues(schedulerName).Dec()
}

// SetDispatcherQueueDepth sets the current queue depth gauge.
func SetDispatcherQueueDepth(depth int) {
	DispatcherQueueDepth.Set(float64(depth))
}

func rankToString(rank int) string {
	// Avoid fmt.Sprintf in hot path.
	switch rank {
	case 0:
		return "0"
	case 1:
		return "1"
	case 2:
		return "2"
	case 3:
		return "3"
	default:
		return "4+"
	}
}
