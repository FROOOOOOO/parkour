// Dispatcher process entry point.
// Watches pending pods and distributes them to Scheduler instances.
// In ParSync mode, additionally creates ParSyncConfig and SchedulerAssignment CRDs.
package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/klog/v2"

	parasched "example.com/para-sched-api/generated/clientset/versioned"

	"example.com/para-scheduler/pkg/dispatcher"
	"example.com/para-scheduler/pkg/metrics"
)

func main() {
	// ---- flags ----
	var (
		kubeconfig     string
		schedulerNames string
		metricsAddr    string
		readinessAddr  string
		expectedNodes  int
		syncMode       string
		syncPattern    string
		syncPeriod     time.Duration
		numPartitions  int
		workers        int
		kubeAPIQPS     float64
		kubeAPIBurst   int
	)
	flag.StringVar(&kubeconfig, "kubeconfig", "", "Path to kubeconfig (uses in-cluster if empty)")
	flag.StringVar(&schedulerNames, "scheduler-names", "sched-0,sched-1", "Comma-separated scheduler instance names")
	flag.StringVar(&metricsAddr, "metrics-addr", ":8081", "Address for Prometheus metrics HTTP server")
	flag.StringVar(&readinessAddr, "readiness-addr", ":8082", "Address for the /ready HTTP endpoint (used by Scheduler/Binder initContainers)")
	flag.IntVar(&expectedNodes, "expected-nodes", 0, "Expected node count; /ready returns 200 only after this many nodes carry the partition-id label (0 disables the node-count gate)")
	flag.StringVar(&syncMode, "sync-mode", "event", "Sync mode: event or periodic")
	flag.StringVar(&syncPattern, "sync-pattern", "diff", "Sync pattern: glob, same, or diff (periodic mode)")
	flag.DurationVar(&syncPeriod, "sync-period", 3*time.Second, "ParSync full sync period G (periodic mode)")
	flag.IntVar(&numPartitions, "num-partitions", 3, "Number of partitions M (periodic mode)")
	flag.IntVar(&workers, "workers", 2, "Number of dispatch workers")
	flag.Float64Var(&kubeAPIQPS, "kube-api-qps", 10000, "QPS to API server")
	flag.IntVar(&kubeAPIBurst, "kube-api-burst", 10000, "Burst to API server")
	klog.InitFlags(nil)
	flag.Parse()

	// ---- context with signal handling ----
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// ---- parse scheduler names ----
	names := parseSchedulerNames(schedulerNames)
	if len(names) == 0 {
		klog.Fatalf("No scheduler names provided")
	}

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

	// ---- dispatcher ----
	d := dispatcher.NewDispatcher(kubeClient, names)

	// ---- [ParSync] partition assigner ----
	var crdClient parasched.Interface
	if strings.EqualFold(syncMode, "periodic") {
		var err error
		crdClient, err = parasched.NewForConfig(restConfig)
		if err != nil {
			klog.Fatalf("Failed to create CRD client: %v", err)
		}

		pa := dispatcher.NewPartitionAssigner(
			crdClient, numPartitions, syncPeriod, syncPattern, names)

		if err := pa.Initialize(ctx); err != nil {
			klog.Fatalf("Failed to initialize ParSync partition assigner: %v", err)
		}

		d.SetPartitionAssigner(pa)

		klog.InfoS("ParSync mode enabled for Dispatcher",
			"partitions", numPartitions, "syncPeriod", syncPeriod,
			"syncPattern", syncPattern, "schedulers", len(names))
	}

	// ---- readiness probe (separate listener so kube probe doesn't compete with /metrics) ----
	rc := dispatcher.NewReadinessChecker(d, crdClient, expectedNodes, "default")
	go startReadinessServer(readinessAddr, rc)

	// ---- start metrics server ----
	go metrics.StartMetricsServer(metricsAddr)

	// ---- informer factory ----
	factory := informers.NewSharedInformerFactory(kubeClient, 0)
	podInformer := factory.Core().V1().Pods()

	// Register event handlers.
	podInformer.Informer().AddEventHandler(newDispatcherPodHandler(d))

	// [ParSync] In periodic sync mode, watch nodes to assign partition labels.
	if d.HasPartitionAssigner() {
		nodeInformer := factory.Core().V1().Nodes()
		nodeInformer.Informer().AddEventHandler(newDispatcherNodeHandler(d))
		d.SetNodeLister(nodeInformer.Lister())
	}

	// Inject pod lister.
	d.SetPodLister(podInformer.Lister())

	// ---- start ----
	factory.Start(ctx.Done())
	factory.WaitForCacheSync(ctx.Done())

	klog.InfoS("Dispatcher starting",
		"workers", workers, "syncMode", syncMode, "schedulers", names)
	d.Run(ctx, workers)
}

// ---- helpers ----

func parseSchedulerNames(csv string) []string {
	var names []string
	for _, s := range strings.Split(csv, ",") {
		s = strings.TrimSpace(s)
		if s != "" {
			names = append(names, s)
		}
	}
	return names
}

// startReadinessServer mounts the dispatcher's /ready endpoint on its own
// listener (separate from /metrics so a busy metrics scrape never blocks
// kubelet's readiness probe). Blocks until the listener fails.
func startReadinessServer(addr string, rc *dispatcher.ReadinessChecker) {
	mux := http.NewServeMux()
	mux.HandleFunc("/ready", rc.HandleReady)
	klog.InfoS("Starting Dispatcher readiness server", "addr", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		klog.ErrorS(err, "Readiness server failed")
	}
}

// ---- Informer event handler adapter ----

type dispatcherPodHandler struct{ d *dispatcher.Dispatcher }

func newDispatcherPodHandler(d *dispatcher.Dispatcher) *dispatcherPodHandler {
	return &dispatcherPodHandler{d: d}
}

func (h *dispatcherPodHandler) OnAdd(obj interface{}, _ bool)      { h.d.OnPodAdd(obj) }
func (h *dispatcherPodHandler) OnUpdate(oldObj, newObj interface{}) { h.d.OnPodUpdate(oldObj, newObj) }
func (h *dispatcherPodHandler) OnDelete(obj interface{})            { h.d.OnPodDelete(obj) }

// ---- Node Informer event handler adapter (ParSync mode) ----

type dispatcherNodeHandler struct{ d *dispatcher.Dispatcher }

func newDispatcherNodeHandler(d *dispatcher.Dispatcher) *dispatcherNodeHandler {
	return &dispatcherNodeHandler{d: d}
}

func (h *dispatcherNodeHandler) OnAdd(obj interface{}, _ bool) { h.d.OnNodeAdd(obj) }
func (h *dispatcherNodeHandler) OnUpdate(_, _ interface{})     {} // partition assignment doesn't change
func (h *dispatcherNodeHandler) OnDelete(_ interface{})        {} // partition cleanup not needed
