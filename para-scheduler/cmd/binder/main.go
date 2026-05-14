// Binder process entry point.
// Watches pods with candidate-node annotations and binds them with fallback.
// In ParSync mode, additionally publishes partition snapshots to ConfigMaps.
package main

import (
	"context"
	"flag"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/klog/v2"

	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"example.com/scheduler-lib/parsync"

	"example.com/para-scheduler/pkg/binder"
	"example.com/para-scheduler/pkg/cache"
	"example.com/para-scheduler/pkg/metrics"
)

func main() {
	// ---- flags ----
	var (
		kubeconfig            string
		namespace             string
		metricsAddr           string
		syncMode              string
		syncPeriod            time.Duration
		numPartitions         int
		snapshotFlushInterval time.Duration
		workers               int
		assumedPodTTL         time.Duration
		statsName             string
		statsFlushPeriod      time.Duration
		kubeAPIQPS            float64
		kubeAPIBurst          int
	)
	flag.StringVar(&kubeconfig, "kubeconfig", "", "Path to kubeconfig (uses in-cluster if empty)")
	flag.StringVar(&namespace, "namespace", "para-system", "Namespace for ConfigMaps and CRDs")
	flag.StringVar(&metricsAddr, "metrics-addr", ":8080", "Address for Prometheus metrics HTTP server")
	flag.StringVar(&syncMode, "sync-mode", "event", "Sync mode: event or periodic")
	flag.DurationVar(&syncPeriod, "sync-period", 3*time.Second, "ParSync full sync period G (periodic mode)")
	flag.IntVar(&numPartitions, "num-partitions", 3, "Number of partitions M (periodic mode)")
	flag.DurationVar(&snapshotFlushInterval, "snapshot-flush-interval", 100*time.Millisecond, "Snapshot batch publish interval (periodic mode)")
	flag.IntVar(&workers, "workers", 4, "Number of bind workers")
	flag.DurationVar(&assumedPodTTL, "assumed-pod-ttl", 30*time.Second, "TTL for assumed pods in cache")
	flag.StringVar(&statsName, "stats-name", "default", "Name of the AdoptionStats CRD object")
	flag.DurationVar(&statsFlushPeriod, "stats-flush-period", 1*time.Second, "Binding stats flush period")
	flag.Float64Var(&kubeAPIQPS, "kube-api-qps", 10000, "QPS to API server")
	flag.IntVar(&kubeAPIBurst, "kube-api-burst", 10000, "Burst to API server")
	klog.InitFlags(nil)
	flag.Parse()

	// ---- context with signal handling ----
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// ---- build clients ----
	restConfig, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
	if err != nil {
		klog.Fatalf("Failed to build kubeconfig: %v", err)
	}
	restConfig.QPS = float32(kubeAPIQPS)
	restConfig.Burst = kubeAPIBurst

	kubeClient, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		klog.Fatalf("Failed to create kube client: %v", err)
	}

	crdClient, err := parasched.NewForConfig(restConfig)
	if err != nil {
		klog.Fatalf("Failed to create CRD client: %v", err)
	}

	// ---- binder cache ----
	binderCache := cache.NewBinderCache(assumedPodTTL)

	// ---- [ParSync] partition manager + snapshot publisher ----
	var snapshotPublisher *binder.SnapshotPublisher
	if strings.EqualFold(syncMode, "periodic") {
		// Resolve numPartitions / syncPeriod authoritative values from
		// ParSyncConfig CRD written by the Dispatcher. The CRD reflects
		// glob-mode coercion (glob forces P=1), so the three components
		// (dispatcher / scheduler / binder) agree without relying on
		// operator discipline on CLI flags. CLI values are fallback.
		effectivePartitions := numPartitions
		effectiveSyncPeriod := syncPeriod
		if psc, pscErr := crdClient.SchedulingV1().ParSyncConfigs().Get(ctx, "default", metav1.GetOptions{}); pscErr == nil {
			if psc.Spec.NumPartitions != effectivePartitions {
				klog.InfoS("Overriding --num-partitions with ParSyncConfig CRD value",
					"cliNumPartitions", effectivePartitions, "crdNumPartitions", psc.Spec.NumPartitions)
				effectivePartitions = psc.Spec.NumPartitions
			}
			if psc.Spec.SyncPeriod.Duration > 0 && psc.Spec.SyncPeriod.Duration != effectiveSyncPeriod {
				klog.InfoS("Overriding --sync-period with ParSyncConfig CRD value",
					"cliSyncPeriod", effectiveSyncPeriod, "crdSyncPeriod", psc.Spec.SyncPeriod.Duration)
				effectiveSyncPeriod = psc.Spec.SyncPeriod.Duration
			}
		} else {
			klog.InfoS("ParSyncConfig CRD not found, falling back to CLI flags",
				"numPartitions", effectivePartitions, "syncPeriod", effectiveSyncPeriod, "err", pscErr)
		}
		numPartitions = effectivePartitions
		syncPeriod = effectiveSyncPeriod

		pm := parsync.NewPartitionManager(numPartitions)
		binderCache.SetPartitionManager(pm)

		snapshotPublisher = binder.NewSnapshotPublisher(
			kubeClient, binderCache, namespace, snapshotFlushInterval,
			numPartitions, syncPeriod)

		klog.InfoS("ParSync mode enabled for Binder",
			"partitions", numPartitions, "syncPeriod", syncPeriod,
			"flushInterval", snapshotFlushInterval,
			"heartbeatPeriod", syncPeriod)
	}

	// ---- binder ----
	b := binder.NewBinder(kubeClient, crdClient, binderCache)

	// Set up reporter.
	reporter := binder.NewBindingReporter(crdClient, statsName, statsFlushPeriod)
	b.SetReporter(reporter)

	if snapshotPublisher != nil {
		b.SetSnapshotPublisher(snapshotPublisher)
	}

	// ---- informer factory ----
	factory := informers.NewSharedInformerFactory(kubeClient, 0)
	podInformer := factory.Core().V1().Pods()
	nodeInformer := factory.Core().V1().Nodes()

	// Register event handlers.
	podInformer.Informer().AddEventHandler(newBinderPodHandler(b))
	nodeInformer.Informer().AddEventHandler(newBinderNodeHandler(b))

	// Inject pod lister.
	b.SetPodLister(podInformer.Lister())

	// ---- start metrics server ----
	go metrics.StartMetricsServer(metricsAddr)

	// ---- start everything ----
	factory.Start(ctx.Done())
	factory.WaitForCacheSync(ctx.Done())

	go binderCache.Run(ctx.Done())
	go reporter.Run(ctx)
	if snapshotPublisher != nil {
		go snapshotPublisher.Run(ctx)
	}

	klog.InfoS("Binder starting", "workers", workers, "syncMode", syncMode)
	b.Run(ctx, workers)
}

// ---- Informer event handler adapters ----

type binderPodHandler struct{ b *binder.Binder }

func newBinderPodHandler(b *binder.Binder) *binderPodHandler { return &binderPodHandler{b: b} }

func (h *binderPodHandler) OnAdd(obj interface{}, _ bool) {
	h.b.AddPodToCache(obj)
	h.b.OnPodNeedsBind(obj)
}
func (h *binderPodHandler) OnUpdate(oldObj, newObj interface{}) {
	h.b.UpdatePodInCache(oldObj, newObj)
	h.b.OnPodUpdateNeedsBind(oldObj, newObj)
}
func (h *binderPodHandler) OnDelete(obj interface{}) {
	h.b.DeletePodFromCache(obj)
}

type binderNodeHandler struct{ b *binder.Binder }

func newBinderNodeHandler(b *binder.Binder) *binderNodeHandler { return &binderNodeHandler{b: b} }

func (h *binderNodeHandler) OnAdd(obj interface{}, _ bool)      { h.b.AddNodeToCache(obj) }
func (h *binderNodeHandler) OnUpdate(oldObj, newObj interface{}) { h.b.UpdateNodeInCache(oldObj, newObj) }
func (h *binderNodeHandler) OnDelete(obj interface{})            { h.b.DeleteNodeFromCache(obj) }
