package binder

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clientset "k8s.io/client-go/kubernetes"
	"k8s.io/klog/v2"

	"example.com/para-scheduler/pkg/cache"
	"example.com/para-scheduler/pkg/metrics"
	"example.com/scheduler-lib/parsync"
)

// maxConfigMapBytes caps the marshalled snapshot payload. The API server's
// hard limit on a ConfigMap is 1 MiB; we subtract a small headroom for the
// ObjectMeta overhead (labels/annotations/managedFields) so that a payload
// right at this threshold still Updates successfully.
const maxConfigMapBytes = 1024*1024 - 4096

// maxNodesPerChunk caps the node count in a single ConfigMap chunk. At ~180
// bytes per node entry this gives ~900 KiB per chunk, safely under
// maxConfigMapBytes (≈1 MiB) with headroom for ObjectMeta. If a chunk still
// exceeds the limit at runtime (pathologically long node names), flushDirty
// will fatal rather than silently drop — see design-parsync-pull-fix.md §4.3.
const maxNodesPerChunk = 5000

// chunkConfigMapName returns the ConfigMap name for a given partition and
// chunk index. Kept stable across generations so Update (not Create+Delete)
// rotates payloads — Schedulers then see a single Update event per chunk.
func chunkConfigMapName(partitionID, chunkIndex int) string {
	return fmt.Sprintf("parasched-snapshot-%d-c%d", partitionID, chunkIndex)
}

// SnapshotPublisher batches dirty partition marks and periodically flushes
// their snapshots to ConfigMaps. This reduces API Server write pressure
// from O(bind_rate) to O(M / flushInterval).
//
// A heartbeat mechanism periodically marks ALL partitions dirty, ensuring
// that Schedulers receive fresh authoritative snapshots on a bounded
// schedule even when the Binder has no new bindings. This caps Scheduler
// staleness at ~heartbeatPeriod regardless of bind activity.
type SnapshotPublisher struct {
	client      clientset.Interface
	binderCache *cache.BinderCache
	namespace   string        // ConfigMap namespace, default "para-system"
	generation  atomic.Int64  // monotonically increasing snapshot generation

	mu              sync.Mutex
	dirtyPartitions map[int]bool  // partitionID -> dirty flag
	flushInterval   time.Duration // default 100ms
	numPartitions   int           // total partition count (for heartbeat)
	heartbeatPeriod time.Duration // how often to force-publish all partitions

	// prevChunkCounts: previous chunk count per partition; drives stale-tail
	// cleanup when a new generation shrinks the chunk count (see cleanupStaleChunks).
	prevChunkCounts map[int]int
}

// NewSnapshotPublisher creates a publisher. flushInterval=0 uses the default 100ms.
// heartbeatPeriod=0 uses syncPeriod as default; set to a positive duration to
// force periodic re-publish of all partitions even without new bindings.
func NewSnapshotPublisher(
	client clientset.Interface,
	binderCache *cache.BinderCache,
	namespace string,
	flushInterval time.Duration,
	numPartitions int,
	heartbeatPeriod time.Duration,
) *SnapshotPublisher {
	if namespace == "" {
		namespace = "para-system"
	}
	if flushInterval <= 0 {
		flushInterval = 100 * time.Millisecond
	}
	if heartbeatPeriod <= 0 {
		heartbeatPeriod = time.Second // default: 1 heartbeat per syncPeriod
	}
	return &SnapshotPublisher{
		client:          client,
		binderCache:     binderCache,
		namespace:       namespace,
		dirtyPartitions: make(map[int]bool),
		flushInterval:   flushInterval,
		numPartitions:   numPartitions,
		heartbeatPeriod: heartbeatPeriod,
		prevChunkCounts: make(map[int]int),
	}
}

// MarkDirty marks a partition as needing re-publish.
// Called by Binder after a successful bind. O(1), non-blocking.
// Ignores invalid partition IDs (< 0), which can occur when nodes
// haven't been assigned to partitions yet.
func (p *SnapshotPublisher) MarkDirty(partitionID int) {
	if partitionID < 0 {
		return
	}
	p.mu.Lock()
	p.dirtyPartitions[partitionID] = true
	p.mu.Unlock()
}

// Run starts the background flush loop. Blocks until ctx is cancelled.
func (p *SnapshotPublisher) Run(ctx context.Context) {
	flushTicker := time.NewTicker(p.flushInterval)
	defer flushTicker.Stop()

	heartbeatTicker := time.NewTicker(p.heartbeatPeriod)
	defer heartbeatTicker.Stop()

	for {
		select {
		case <-flushTicker.C:
			p.flushDirty(ctx)
		case <-heartbeatTicker.C:
			// Heartbeat: mark ALL partitions dirty so that Schedulers
			// receive fresh authoritative snapshots even when the Binder
			// has no new bindings. This breaks the deadlock where phantom
			// resources block scheduling → no bindings → no snapshots.
			p.mu.Lock()
			for i := 0; i < p.numPartitions; i++ {
				p.dirtyPartitions[i] = true
			}
			p.mu.Unlock()
		case <-ctx.Done():
			p.flushDirty(context.Background())
			return
		}
	}
}

// flushDirty publishes snapshots for all accumulated dirty partitions in one
// batch. A snapshot that exceeds maxNodesPerChunk nodes is split into multiple
// ConfigMaps (chunks); each chunk carries the same Generation so the
// Scheduler only applies a generation once every chunk of it has arrived.
func (p *SnapshotPublisher) flushDirty(ctx context.Context) {
	p.mu.Lock()
	if len(p.dirtyPartitions) == 0 {
		p.mu.Unlock()
		return
	}
	dirty := p.dirtyPartitions
	p.dirtyPartitions = make(map[int]bool)
	p.mu.Unlock()

	for partitionID := range dirty {
		gen := p.generation.Add(1)
		snapshot := p.binderCache.SnapshotPartition(partitionID)
		snapshot.Generation = gen

		chunks := chunkSnapshot(snapshot, maxNodesPerChunk)

		published, retry := p.publishChunks(ctx, partitionID, gen, chunks)
		if retry {
			// At least one chunk write failed. Re-mark the partition so the
			// next flush retries; partial chunks for this generation stay on
			// the API server until the next successful publish overwrites
			// them (Scheduler only applies the generation when all chunks
			// for it are present, so a partial publish is harmless).
			p.mu.Lock()
			p.dirtyPartitions[partitionID] = true
			p.mu.Unlock()
			continue
		}

		p.cleanupStaleChunks(ctx, partitionID, len(chunks))
		p.prevChunkCounts[partitionID] = len(chunks)

		metrics.SnapshotPublishChunks.WithLabelValues(strconv.Itoa(partitionID)).Add(float64(len(chunks)))
		klog.V(5).InfoS("Published partition snapshot",
			"partition", partitionID, "generation", gen,
			"nodes", len(snapshot.Nodes), "chunks", len(chunks),
			"publishedChunks", published)
	}
}

// publishChunks writes each chunk as its own ConfigMap. Returns (publishedCount, retry).
// When retry=true, the caller should re-mark the partition dirty.
//
// Oversize handling: a single chunk larger than maxConfigMapBytes (pathologically
// long node names pushing a 5000-node chunk over 1 MiB) is fatal rather than
// silently dropped — the invariant we preserve here is "chunks always publish
// or we halt so the operator notices". Silent drops are exactly what made the
// strategy-N10 experiment data useless (see docs/process/INVESTIGATION.md §4).
func (p *SnapshotPublisher) publishChunks(
	ctx context.Context, partitionID int, gen int64, chunks []*cache.PartitionSnapshot,
) (int, bool) {
	chunkCount := len(chunks)
	published := 0
	for chunkIndex, chunk := range chunks {
		data, err := cache.MarshalSnapshot(chunk)
		if err != nil {
			klog.ErrorS(err, "Failed to marshal chunk snapshot",
				"partition", partitionID, "chunk", chunkIndex)
			return published, true
		}
		if len(data) > maxConfigMapBytes {
			// Fatal: single chunk exceeds limit after chunking. Occurs only
			// under pathological input (node names near K8s' 253-char max,
			// or an unexpected resource-field bloat). Reducing maxNodesPerChunk
			// would work around it; we choose fatal so the operator sees it
			// instead of accumulating silent drops.
			klog.Fatalf("partition %d chunk %d exceeds ConfigMap limit (%d > %d) — reduce maxNodesPerChunk",
				partitionID, chunkIndex, len(data), maxConfigMapBytes)
		}

		cmName := chunkConfigMapName(partitionID, chunkIndex)
		cm := &v1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{
				Name:      cmName,
				Namespace: p.namespace,
				Labels: map[string]string{
					parsync.SnapshotLabelApp:         parsync.SnapshotLabelAppValue,
					parsync.SnapshotLabelPartitionID: strconv.Itoa(partitionID),
					parsync.SnapshotLabelChunkIndex:  strconv.Itoa(chunkIndex),
					parsync.SnapshotLabelChunkCount:  strconv.Itoa(chunkCount),
					parsync.SnapshotLabelGeneration:  strconv.FormatInt(gen, 10),
				},
			},
			Data: map[string]string{
				"snapshot": string(data),
			},
		}

		_, err = p.client.CoreV1().ConfigMaps(p.namespace).Update(ctx, cm, metav1.UpdateOptions{})
		if errors.IsNotFound(err) {
			_, err = p.client.CoreV1().ConfigMaps(p.namespace).Create(ctx, cm, metav1.CreateOptions{})
		}
		if err != nil {
			reason := classifyPublishError(err)
			klog.ErrorS(err, "Failed to publish chunk ConfigMap",
				"configmap", cmName, "partition", partitionID, "chunk", chunkIndex, "reason", reason)
			metrics.SnapshotPublishErrorsTotal.
				WithLabelValues(strconv.Itoa(partitionID), reason).Inc()
			return published, true
		}
		published++
	}
	return published, false
}

// classifyPublishError keeps label cardinality bounded: callers must not
// pass the raw error string into SnapshotPublishErrorsTotal.
func classifyPublishError(err error) string {
	switch {
	case errors.IsTooManyRequests(err):
		return metrics.PublishReasonThrottled
	case errors.IsConflict(err):
		return metrics.PublishReasonConflict
	case errors.IsServerTimeout(err) || errors.IsTimeout(err):
		return metrics.PublishReasonTimeout
	default:
		return metrics.PublishReasonOther
	}
}

// cleanupStaleChunks deletes chunk ConfigMaps with index >= newChunkCount for
// a partition whose previous generation used more chunks. Without this the
// Scheduler's label-selector watch would keep merging the old tail chunks
// into the new generation, applying stale resource data. Hygiene rather than
// correctness — the Scheduler's generation guard already rejects stale
// chunks, but a leftover ConfigMap costs etcd space and noise on long runs.
func (p *SnapshotPublisher) cleanupStaleChunks(ctx context.Context, partitionID, newChunkCount int) {
	prev, ok := p.prevChunkCounts[partitionID]
	if !ok || prev <= newChunkCount {
		return
	}
	for i := newChunkCount; i < prev; i++ {
		cmName := chunkConfigMapName(partitionID, i)
		err := p.client.CoreV1().ConfigMaps(p.namespace).Delete(ctx, cmName, metav1.DeleteOptions{})
		if err != nil && !errors.IsNotFound(err) {
			// Non-fatal: Scheduler's generation guard prevents apply from the
			// stale set; the leftover ConfigMap will be retried next cleanup.
			klog.ErrorS(err, "Failed to delete stale chunk ConfigMap",
				"configmap", cmName, "partition", partitionID)
		}
	}
}

// chunkSnapshot splits a snapshot into multiple smaller snapshots, each
// carrying at most `maxNodes` Nodes entries. The PartitionID, Timestamp,
// and Generation are copied unchanged to every chunk so the Scheduler can
// key chunks by (partitionID, generation) and validate chunk completeness.
func chunkSnapshot(snapshot *cache.PartitionSnapshot, maxNodes int) []*cache.PartitionSnapshot {
	if snapshot == nil {
		return nil
	}
	if len(snapshot.Nodes) == 0 {
		// Empty partition: publish one empty chunk so Schedulers see a
		// sentinel "this partition has zero nodes" rather than assuming
		// stale data.
		empty := *snapshot
		empty.Nodes = nil
		return []*cache.PartitionSnapshot{&empty}
	}

	chunks := make([]*cache.PartitionSnapshot, 0, (len(snapshot.Nodes)+maxNodes-1)/maxNodes)
	for start := 0; start < len(snapshot.Nodes); start += maxNodes {
		end := start + maxNodes
		if end > len(snapshot.Nodes) {
			end = len(snapshot.Nodes)
		}
		chunk := &cache.PartitionSnapshot{
			PartitionID: snapshot.PartitionID,
			Timestamp:   snapshot.Timestamp,
			Generation:  snapshot.Generation,
			Nodes:       snapshot.Nodes[start:end],
		}
		chunks = append(chunks, chunk)
	}
	return chunks
}

// DirtyCount returns the current number of dirty partitions (for testing).
func (p *SnapshotPublisher) DirtyCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.dirtyPartitions)
}

// Generation returns the current generation counter (for testing).
func (p *SnapshotPublisher) Generation() int64 {
	return p.generation.Load()
}
