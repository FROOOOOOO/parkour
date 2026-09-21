/*
Copyright 2014 The Kubernetes Authors.

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

// Package app implements a Server object for running the scheduler.
package app

import (
	"context"
	"fmt"
	"net/http"
	"os"
	goruntime "runtime"
	"strconv"
	"time"

	"github.com/blang/semver/v4"
	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"example.com/scheduler-lib/multicandidate"
	"github.com/spf13/cobra"
	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utilerrors "k8s.io/apimachinery/pkg/util/errors"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/apiserver/pkg/authentication/authenticator"
	"k8s.io/apiserver/pkg/authorization/authorizer"
	genericapifilters "k8s.io/apiserver/pkg/endpoints/filters"
	apirequest "k8s.io/apiserver/pkg/endpoints/request"
	"k8s.io/apiserver/pkg/server"
	genericfilters "k8s.io/apiserver/pkg/server/filters"
	"k8s.io/apiserver/pkg/server/healthz"
	"k8s.io/apiserver/pkg/server/mux"
	"k8s.io/apiserver/pkg/server/routes"
	"k8s.io/apiserver/pkg/util/compatibility"
	utilfeature "k8s.io/apiserver/pkg/util/feature"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/events"
	"k8s.io/client-go/tools/leaderelection"
	cliflag "k8s.io/component-base/cli/flag"
	"k8s.io/component-base/cli/globalflag"
	basecompatibility "k8s.io/component-base/compatibility"
	"k8s.io/component-base/configz"
	"k8s.io/component-base/featuregate"
	"k8s.io/component-base/logs"
	logsapi "k8s.io/component-base/logs/api/v1"
	"k8s.io/component-base/metrics/features"
	"k8s.io/component-base/metrics/legacyregistry"
	"k8s.io/component-base/metrics/prometheus/slis"
	"k8s.io/component-base/term"
	utilversion "k8s.io/component-base/version"
	"k8s.io/component-base/version/verflag"
	zpagesfeatures "k8s.io/component-base/zpages/features"
	"k8s.io/component-base/zpages/flagz"
	"k8s.io/component-base/zpages/statusz"
	"k8s.io/klog/v2"
	schedulerserverconfig "k8s.io/kubernetes/cmd/kube-scheduler/app/config"
	"k8s.io/kubernetes/cmd/kube-scheduler/app/options"
	kubefeatures "k8s.io/kubernetes/pkg/features"
	"k8s.io/kubernetes/pkg/scheduler"
	kubeschedulerconfig "k8s.io/kubernetes/pkg/scheduler/apis/config"
	"k8s.io/kubernetes/pkg/scheduler/apis/config/latest"
	"k8s.io/kubernetes/pkg/scheduler/framework/runtime"
	"k8s.io/kubernetes/pkg/scheduler/metrics/resources"
	"k8s.io/kubernetes/pkg/scheduler/profile"
)

const (
	kubeScheduler = "kube-scheduler"
)

// paraSchedFlags holds all para-sched related flags.
// Flag values take precedence; env vars are used as fallback defaults.
//
// Phase 2 refactor: the former `--parasched-enable-penalty` and
// `--parasched-penalty-weight` are deprecated, replaced by the nested
// `--parasched-strategy.{name,penalty-weight,seed}`. Strategy.Name=QualityFirst
// with PenaltyWeight=0 is equivalent to the old enable-penalty=false.
var paraSchedFlags struct {
	Name              string
	CandidateK        int
	StrategyName      string
	PenaltyWeight     float64
	StrategySeed      int64
	EnableParSync     bool
	NumPartitions     int
	SyncPeriod        string
	Namespace         string
	StatsName         string
	HeartbeatInterval string
}

func init() {
	utilruntime.Must(logsapi.AddFeatureGates(utilfeature.DefaultMutableFeatureGate))
	utilruntime.Must(features.AddFeatureGates(utilfeature.DefaultMutableFeatureGate))
}

// Option configures a framework.Registry.
type Option func(runtime.Registry) error

// NewSchedulerCommand creates a *cobra.Command object with default parameters and registryOptions
func NewSchedulerCommand(registryOptions ...Option) *cobra.Command {
	opts := options.NewOptions()

	cmd := &cobra.Command{
		Use: "kube-scheduler",
		Long: `The Kubernetes scheduler is a control plane process which assigns
Pods to Nodes. The scheduler determines which Nodes are valid placements for
each Pod in the scheduling queue according to constraints and available
resources. The scheduler then ranks each valid Node and binds the Pod to a
suitable Node. Multiple different schedulers may be used within a cluster;
kube-scheduler is the reference implementation.
See [scheduling](https://kubernetes.io/docs/concepts/scheduling-eviction/)
for more information about scheduling and the kube-scheduler component.`,
		PersistentPreRunE: func(*cobra.Command, []string) error {
			// makes sure feature gates are set before RunE.
			return opts.ComponentGlobalsRegistry.Set()
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCommand(cmd, opts, registryOptions...)
		},
		Args: func(cmd *cobra.Command, args []string) error {
			for _, arg := range args {
				if len(arg) > 0 {
					return fmt.Errorf("%q does not take any arguments, got %q", cmd.CommandPath(), args)
				}
			}
			return nil
		},
	}

	nfs := opts.Flags
	verflag.AddFlags(nfs.FlagSet("global"))
	globalflag.AddGlobalFlags(nfs.FlagSet("global"), cmd.Name(), logs.SkipLoggingConfigurationFlags())

	// Para-sched flags. Defaults come from env vars for backward compatibility;
	// explicit flags take precedence.
	psfs := nfs.FlagSet("parasched")
	psfs.StringVar(&paraSchedFlags.Name, "parasched-name", os.Getenv("PARASCHED_NAME"), "Para-sched instance name (empty = disabled)")
	psfs.IntVar(&paraSchedFlags.CandidateK, "parasched-candidate-k", envInt("PARASCHED_CANDIDATE_K", 3), "Number of backup candidates K")
	psfs.StringVar(&paraSchedFlags.StrategyName, "parasched-strategy.name", envStringDefault("PARASCHED_STRATEGY_NAME", "QualityFirst"), "Strategy name: QualityFirst | LatencyFirst | WeightedRandom")
	psfs.Float64Var(&paraSchedFlags.PenaltyWeight, "parasched-strategy.penalty-weight", envFloat("PARASCHED_STRATEGY_PENALTY_WEIGHT", 0.3), "Penalty weight p ∈ [0,1] for QualityFirst/WeightedRandom. p=0 disables penalty.")
	psfs.Int64Var(&paraSchedFlags.StrategySeed, "parasched-strategy.seed", envInt64("PARASCHED_STRATEGY_SEED", 42), "PRNG seed for WeightedRandom (ignored by other strategies)")
	psfs.BoolVar(&paraSchedFlags.EnableParSync, "parasched-enable-parsync", envBool("PARASCHED_ENABLE_PARSYNC", false), "Enable ParSync periodic mode")
	psfs.IntVar(&paraSchedFlags.NumPartitions, "parasched-num-partitions", envInt("PARASCHED_NUM_PARTITIONS", 10), "Number of partitions M")
	psfs.StringVar(&paraSchedFlags.SyncPeriod, "parasched-sync-period", envStringDefault("PARASCHED_SYNC_PERIOD", "1s"), "Sync period G")
	psfs.StringVar(&paraSchedFlags.Namespace, "parasched-namespace", envStringDefault("PARASCHED_NAMESPACE", "para-system"), "Namespace for CRDs and ConfigMaps")
	psfs.StringVar(&paraSchedFlags.StatsName, "parasched-stats-name", envStringDefault("PARASCHED_STATS_NAME", "default"), "AdoptionStats CR name")
	psfs.StringVar(&paraSchedFlags.HeartbeatInterval, "parasched-heartbeat-interval", envStringDefault("PARASCHED_HEARTBEAT_INTERVAL", "5s"), "Heartbeat interval for SchedulerAssignment CRD status update (0 disables)")

	fs := cmd.Flags()
	for _, f := range nfs.FlagSets {
		fs.AddFlagSet(f)
	}

	cols, _, _ := term.TerminalSize(cmd.OutOrStdout())
	cliflag.SetUsageAndHelpFunc(cmd, *nfs, cols)

	if err := cmd.MarkFlagFilename("config", "yaml", "yml", "json"); err != nil {
		klog.Background().Error(err, "Failed to mark flag filename")
	}

	return cmd
}

// runCommand runs the scheduler.
func runCommand(cmd *cobra.Command, opts *options.Options, registryOptions ...Option) error {
	verflag.PrintAndExitIfRequested()
	fg := opts.ComponentGlobalsRegistry.FeatureGateFor(basecompatibility.DefaultKubeComponent)
	// Activate logging as soon as possible, after that
	// show flags with the final logging configuration.
	if err := logsapi.ValidateAndApply(opts.Logs, fg); err != nil {
		fmt.Fprintf(os.Stderr, "%v\n", err)
		os.Exit(1)
	}
	cliflag.PrintFlags(cmd.Flags())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		stopCh := server.SetupSignalHandler()
		<-stopCh
		cancel()
	}()

	cc, sched, err := Setup(ctx, opts, registryOptions...)
	if err != nil {
		return err
	}
	// add feature enablement metrics
	fg.(featuregate.MutableFeatureGate).AddMetrics()
	return Run(ctx, cc, sched)
}

// Run executes the scheduler based on the given configuration. It only returns on error or when context is done.
func Run(ctx context.Context, cc *schedulerserverconfig.CompletedConfig, sched *scheduler.Scheduler) error {
	logger := klog.FromContext(ctx)

	// To help debugging, immediately log version
	logger.Info("Starting Kubernetes Scheduler", "version", utilversion.Get())

	logger.Info("Golang settings", "GOGC", os.Getenv("GOGC"), "GOMAXPROCS", os.Getenv("GOMAXPROCS"), "GOTRACEBACK", os.Getenv("GOTRACEBACK"))

	// Configz registration.
	if cz, err := configz.New("componentconfig"); err != nil {
		return fmt.Errorf("unable to register configz: %s", err)
	} else {
		cz.Set(cc.ComponentConfig)
	}

	// Start events processing pipeline.
	cc.EventBroadcaster.StartRecordingToSink(ctx.Done())
	defer cc.EventBroadcaster.Shutdown()

	// Setup healthz checks.
	var checks, readyzChecks []healthz.HealthChecker
	if cc.ComponentConfig.LeaderElection.LeaderElect {
		checks = append(checks, cc.LeaderElection.WatchDog)
		readyzChecks = append(readyzChecks, cc.LeaderElection.WatchDog)
	}
	readyzChecks = append(readyzChecks, healthz.NewShutdownHealthz(ctx.Done()))

	waitingForLeader := make(chan struct{})
	isLeader := func() bool {
		select {
		case _, ok := <-waitingForLeader:
			// if channel is closed, we are leading
			return !ok
		default:
			// channel is open, we are waiting for a leader
			return false
		}
	}

	handlerSyncReadyCh := make(chan struct{})
	handlerSyncCheck := healthz.NamedCheck("sched-handler-sync", func(_ *http.Request) error {
		select {
		case <-handlerSyncReadyCh:
			return nil
		default:
		}
		return fmt.Errorf("handlers are not fully synchronized")
	})
	readyzChecks = append(readyzChecks, handlerSyncCheck)

	if cc.LeaderElection != nil && utilfeature.DefaultFeatureGate.Enabled(kubefeatures.CoordinatedLeaderElection) {
		binaryVersion, err := semver.ParseTolerant(cc.ComponentGlobalsRegistry.EffectiveVersionFor(basecompatibility.DefaultKubeComponent).BinaryVersion().String())
		if err != nil {
			return err
		}
		emulationVersion, err := semver.ParseTolerant(cc.ComponentGlobalsRegistry.EffectiveVersionFor(basecompatibility.DefaultKubeComponent).EmulationVersion().String())
		if err != nil {
			return err
		}

		// Start lease candidate controller for coordinated leader election
		leaseCandidate, waitForSync, err := leaderelection.NewCandidate(
			cc.Client,
			metav1.NamespaceSystem,
			cc.LeaderElection.Lock.Identity(),
			kubeScheduler,
			binaryVersion.FinalizeVersion(),
			emulationVersion.FinalizeVersion(),
			coordinationv1.OldestEmulationVersion,
		)
		if err != nil {
			return err
		}
		readyzChecks = append(readyzChecks, healthz.NewInformerSyncHealthz(waitForSync))
		go leaseCandidate.Run(ctx)
	}

	// Start up the server for endpoints.
	if cc.SecureServing != nil {
		handler := buildHandlerChain(newEndpointsHandler(&cc.ComponentConfig, cc.InformerFactory, isLeader, checks, readyzChecks, cc.Flagz), cc.Authentication.Authenticator, cc.Authorization.Authorizer)
		// TODO: handle stoppedCh and listenerStoppedCh returned by c.SecureServing.Serve
		if _, _, err := cc.SecureServing.Serve(handler, 0, ctx.Done()); err != nil {
			// fail early for secure handlers, removing the old error loop from above
			return fmt.Errorf("failed to start secure server: %v", err)
		}
	}

	startInformersAndWaitForSync := func(ctx context.Context) {
		// Start all informers.
		cc.InformerFactory.Start(ctx.Done())
		// DynInformerFactory can be nil in tests.
		if cc.DynInformerFactory != nil {
			cc.DynInformerFactory.Start(ctx.Done())
		}

		// Wait for all caches to sync before scheduling.
		cc.InformerFactory.WaitForCacheSync(ctx.Done())
		// DynInformerFactory can be nil in tests.
		if cc.DynInformerFactory != nil {
			cc.DynInformerFactory.WaitForCacheSync(ctx.Done())
		}

		// Wait for all handlers to sync (all items in the initial list delivered) before scheduling.
		if err := sched.WaitForHandlersSync(ctx); err != nil {
			logger.Error(err, "handlers are not fully synchronized")
		}

		close(handlerSyncReadyCh)
		logger.V(3).Info("Handlers synced")
	}
	if !cc.ComponentConfig.DelayCacheUntilActive || cc.LeaderElection == nil {
		startInformersAndWaitForSync(ctx)
	}
	// If leader election is enabled, runCommand via LeaderElector until done and exit.
	if cc.LeaderElection != nil {
		if utilfeature.DefaultFeatureGate.Enabled(kubefeatures.CoordinatedLeaderElection) {
			cc.LeaderElection.Coordinated = true
		}
		cc.LeaderElection.Callbacks = leaderelection.LeaderCallbacks{
			OnStartedLeading: func(ctx context.Context) {
				close(waitingForLeader)
				if cc.ComponentConfig.DelayCacheUntilActive {
					logger.Info("Starting informers and waiting for sync...")
					startInformersAndWaitForSync(ctx)
					logger.Info("Sync completed")
				}
				sched.Run(ctx)
			},
			OnStoppedLeading: func() {
				select {
				case <-ctx.Done():
					// We were asked to terminate. Exit 0.
					logger.Info("Requested to terminate, exiting")
					os.Exit(0)
				default:
					// We lost the lock.
					logger.Error(nil, "Leaderelection lost")
					klog.FlushAndExit(klog.ExitFlushTimeout, 1)
				}
			},
		}
		leaderElector, err := leaderelection.NewLeaderElector(*cc.LeaderElection)
		if err != nil {
			return fmt.Errorf("couldn't create leader elector: %v", err)
		}

		leaderElector.Run(ctx)

		return fmt.Errorf("lost lease")
	}

	// Leader election is disabled, so runCommand inline until done.
	close(waitingForLeader)
	sched.Run(ctx)
	return fmt.Errorf("finished without leader elect")
}

// buildHandlerChain wraps the given handler with the standard filters.
func buildHandlerChain(handler http.Handler, authn authenticator.Request, authz authorizer.Authorizer) http.Handler {
	requestInfoResolver := &apirequest.RequestInfoFactory{}
	failedHandler := genericapifilters.Unauthorized(scheme.Codecs)

	handler = genericapifilters.WithAuthorization(handler, authz, scheme.Codecs)
	handler = genericapifilters.WithAuthentication(handler, authn, failedHandler, nil, nil)
	handler = genericapifilters.WithRequestInfo(handler, requestInfoResolver)
	handler = genericapifilters.WithCacheControl(handler)
	handler = genericfilters.WithHTTPLogging(handler)
	handler = genericfilters.WithPanicRecovery(handler, requestInfoResolver)

	return handler
}

func installMetricHandler(pathRecorderMux *mux.PathRecorderMux, informers informers.SharedInformerFactory, isLeader func() bool) {
	configz.InstallHandler(pathRecorderMux)
	pathRecorderMux.Handle("/metrics", legacyregistry.HandlerWithReset())

	resourceMetricsHandler := resources.Handler(informers.Core().V1().Pods().Lister())
	pathRecorderMux.HandleFunc("/metrics/resources", func(w http.ResponseWriter, req *http.Request) {
		if !isLeader() {
			return
		}
		resourceMetricsHandler.ServeHTTP(w, req)
	})
}

// newEndpointsHandler creates an API health server from the config, and will also
// embed the metrics handler and z-pages handler.
// TODO: healthz check is deprecated, please use livez and readyz instead. Will be removed in the future.
func newEndpointsHandler(config *kubeschedulerconfig.KubeSchedulerConfiguration, informers informers.SharedInformerFactory, isLeader func() bool, healthzChecks, readyzChecks []healthz.HealthChecker, flagReader flagz.Reader) http.Handler {
	pathRecorderMux := mux.NewPathRecorderMux(kubeScheduler)
	healthz.InstallHandler(pathRecorderMux, healthzChecks...)
	healthz.InstallLivezHandler(pathRecorderMux)
	healthz.InstallReadyzHandler(pathRecorderMux, readyzChecks...)
	installMetricHandler(pathRecorderMux, informers, isLeader)
	slis.SLIMetricsWithReset{}.Install(pathRecorderMux)

	if config.EnableProfiling {
		routes.Profiling{}.Install(pathRecorderMux)
		if config.EnableContentionProfiling {
			goruntime.SetBlockProfileRate(1)
		}
		routes.DebugFlags{}.Install(pathRecorderMux, "v", routes.StringFlagPutHandler(logs.GlogSetter))
	}

	if utilfeature.DefaultFeatureGate.Enabled(zpagesfeatures.ComponentFlagz) {
		if flagReader != nil {
			flagz.Install(pathRecorderMux, kubeScheduler, flagReader)
		}
	}

	if utilfeature.DefaultFeatureGate.Enabled(zpagesfeatures.ComponentStatusz) {
		statusz.Install(pathRecorderMux, kubeScheduler, statusz.NewRegistry(compatibility.DefaultBuildEffectiveVersion()))
	}

	return pathRecorderMux
}

func getRecorderFactory(cc *schedulerserverconfig.CompletedConfig) profile.RecorderFactory {
	return func(name string) events.EventRecorder {
		return cc.EventBroadcaster.NewRecorder(name)
	}
}

// WithPlugin creates an Option based on plugin name and factory. Please don't remove this function: it is used to register out-of-tree plugins,
// hence there are no references to it from the kubernetes scheduler code base.
func WithPlugin(name string, factory runtime.PluginFactory) Option {
	return func(registry runtime.Registry) error {
		return registry.Register(name, factory)
	}
}

// Setup creates a completed config and a scheduler based on the command args and options
func Setup(ctx context.Context, opts *options.Options, outOfTreeRegistryOptions ...Option) (*schedulerserverconfig.CompletedConfig, *scheduler.Scheduler, error) {
	if cfg, err := latest.Default(); err != nil {
		return nil, nil, err
	} else {
		opts.ComponentConfig = cfg
	}

	if errs := opts.Validate(); len(errs) > 0 {
		return nil, nil, utilerrors.NewAggregate(errs)
	}

	c, err := opts.Config(ctx)
	if err != nil {
		return nil, nil, err
	}

	// Get the completed config
	cc := c.Complete()

	outOfTreeRegistry := make(runtime.Registry)
	for _, option := range outOfTreeRegistryOptions {
		if err := option(outOfTreeRegistry); err != nil {
			return nil, nil, err
		}
	}

	recorderFactory := getRecorderFactory(&cc)
	completedProfiles := make([]kubeschedulerconfig.KubeSchedulerProfile, 0)
	// Create the scheduler.
	sched, err := scheduler.New(ctx,
		cc.Client,
		cc.InformerFactory,
		cc.DynInformerFactory,
		recorderFactory,
		scheduler.WithComponentConfigVersion(cc.ComponentConfig.TypeMeta.APIVersion),
		scheduler.WithKubeConfig(cc.KubeConfig),
		scheduler.WithProfiles(cc.ComponentConfig.Profiles...),
		scheduler.WithPercentageOfNodesToScore(cc.ComponentConfig.PercentageOfNodesToScore),
		scheduler.WithFrameworkOutOfTreeRegistry(outOfTreeRegistry),
		scheduler.WithPodMaxBackoffSeconds(cc.ComponentConfig.PodMaxBackoffSeconds),
		scheduler.WithPodInitialBackoffSeconds(cc.ComponentConfig.PodInitialBackoffSeconds),
		scheduler.WithPodMaxInUnschedulablePodsDuration(cc.PodMaxInUnschedulablePodsDuration),
		scheduler.WithExtenders(cc.ComponentConfig.Extenders...),
		scheduler.WithParallelism(cc.ComponentConfig.Parallelism),
		scheduler.WithBuildFrameworkCapturer(func(profile kubeschedulerconfig.KubeSchedulerProfile) {
			// Profiles are processed during Framework instantiation to set default plugins and configurations. Capturing them for logging
			completedProfiles = append(completedProfiles, profile)
		}),
	)
	if err != nil {
		return nil, nil, err
	}
	if err := options.LogOrWriteConfig(klog.FromContext(ctx), opts.WriteConfigTo, &cc.ComponentConfig, completedProfiles); err != nil {
		return nil, nil, err
	}

	// Para-sched: activate if --parasched-name is set (or PARASCHED_NAME env var).
	if name := paraSchedFlags.Name; name != "" {
		candidateK := paraSchedFlags.CandidateK
		enableParSync := paraSchedFlags.EnableParSync
		namespace := paraSchedFlags.Namespace
		statsName := paraSchedFlags.StatsName

		strategyCfg := &multicandidate.StrategyConfig{
			Name:          paraSchedFlags.StrategyName,
			PenaltyWeight: paraSchedFlags.PenaltyWeight,
			Seed:          paraSchedFlags.StrategySeed,
		}
		sched.EnableParaSched(name, candidateK, strategyCfg)

		// AdoptionStats watcher is needed iff the selected strategy consumes
		// per-node conflict rates with non-zero penalty (QualityFirst / WeightedRandom
		// with p>0). LatencyFirst does not use conflict rates.
		wantConflictStats := (strategyCfg.Name == multicandidate.QualityFirst ||
			strategyCfg.Name == multicandidate.WeightedRandom) &&
			strategyCfg.PenaltyWeight > 0

		// Create para-sched-api client (needed for SchedulerAssignment CRD lookup
		// in ParSync mode, and for AdoptionStats watcher when conflict-rate feedback
		// is required).
		var paraClient parasched.Interface
		if enableParSync || wantConflictStats {
			pc, err := parasched.NewForConfig(cc.KubeConfig)
			if err != nil {
				klog.FromContext(ctx).Error(err, "Failed to create para-sched-api client")
			} else {
				paraClient = pc
			}
		}

		if enableParSync {
			numPartitions := paraSchedFlags.NumPartitions
			syncPeriod, err := time.ParseDuration(paraSchedFlags.SyncPeriod)
			if err != nil {
				klog.FromContext(ctx).Error(err, "Invalid --parasched-sync-period, using 1s")
				syncPeriod = time.Second
			}

			// Read numPartitions from ParSyncConfig CRD — the authoritative
			// source written by the Dispatcher. The CRD reflects glob-mode
			// coercion (glob forces P=1) so three-component (dispatcher /
			// scheduler / binder) agreement is guaranteed without relying on
			// operator discipline on CLI flags. CLI flag serves as fallback
			// only when the CRD is absent (e.g. Dispatcher not yet started).
			if paraClient != nil {
				psc, err := paraClient.SchedulingV1().ParSyncConfigs().Get(ctx, "default", metav1.GetOptions{})
				if err != nil {
					klog.FromContext(ctx).Info("ParSyncConfig CRD not found, falling back to --parasched-num-partitions",
						"numPartitions", numPartitions, "err", err)
				} else {
					if psc.Spec.NumPartitions != numPartitions {
						klog.FromContext(ctx).Info("Overriding --parasched-num-partitions with ParSyncConfig CRD value",
							"cliNumPartitions", numPartitions, "crdNumPartitions", psc.Spec.NumPartitions)
					}
					numPartitions = psc.Spec.NumPartitions
					if psc.Spec.SyncPeriod.Duration > 0 && psc.Spec.SyncPeriod.Duration != syncPeriod {
						klog.FromContext(ctx).Info("Overriding --parasched-sync-period with ParSyncConfig CRD value",
							"cliSyncPeriod", syncPeriod, "crdSyncPeriod", psc.Spec.SyncPeriod.Duration)
						syncPeriod = psc.Spec.SyncPeriod.Duration
					}
				}
			}

			sched.EnableParaSync()

			// Read partition order from SchedulerAssignment CRD written by
			// the Dispatcher. In diffSync mode the Dispatcher assigns each
			// scheduler a rotated order so that at each time slot every
			// scheduler syncs a *different* partition.  If the CRD is not
			// available (e.g. event-driven mode), fall back to [0,1,...,M-1].
			partitionOrder := make([]int, numPartitions)
			for i := range partitionOrder {
				partitionOrder[i] = i
			}
			if paraClient != nil {
				sa, err := paraClient.SchedulingV1().SchedulerAssignments().Get(ctx, name, metav1.GetOptions{})
				if err != nil {
					klog.FromContext(ctx).Info("SchedulerAssignment CRD not found, using default partition order",
						"name", name, "err", err)
				} else if len(sa.Spec.SyncSlots) > 0 {
					// Extract partition order from SyncSlots (ordered by SlotIndex).
					partitionOrder = make([]int, len(sa.Spec.SyncSlots))
					for _, slot := range sa.Spec.SyncSlots {
						if slot.SlotIndex >= 0 && slot.SlotIndex < len(partitionOrder) {
							partitionOrder[slot.SlotIndex] = slot.PartitionID
						}
					}
					klog.FromContext(ctx).Info("Loaded partition order from SchedulerAssignment CRD",
						"name", name, "partitionOrder", partitionOrder)
				}
			}

			sched.StartParaSyncConsumer(ctx, cc.Client, namespace, numPartitions, syncPeriod, partitionOrder)
		}

		// Start AdoptionStats watcher when the strategy consumes conflict rates.
		// LatencyFirst skips this — it derives freshness from ParSync only.
		if wantConflictStats && paraClient != nil {
			sched.StartAdoptionStatsWatcher(ctx, paraClient, statsName)
		}

		// Start heartbeat loop: patches SchedulerAssignment.status.lastHeartbeat
		// periodically so that the Dispatcher's failoverTimeout check sees a
		// live signal. Only enabled when ParSync is on (the CRD only exists
		// in ParSync mode).
		if enableParSync && paraClient != nil {
			if hb, err := time.ParseDuration(paraSchedFlags.HeartbeatInterval); err == nil && hb > 0 {
				sched.StartHeartbeatLoop(ctx, paraClient, name, hb)
			} else if err != nil {
				klog.FromContext(ctx).Error(err, "Invalid --parasched-heartbeat-interval, heartbeat disabled")
			}
		}

		klog.FromContext(ctx).Info("Para-sched mode enabled",
			"name", name, "candidateK", candidateK,
			"strategy", strategyCfg.Name, "penaltyWeight", strategyCfg.PenaltyWeight,
			"enableParSync", enableParSync, "namespace", namespace)
	}

	return &cc, sched, nil
}

// envStringDefault reads a string from an environment variable, returning defaultVal if unset.
func envStringDefault(key, defaultVal string) string {
	if s := os.Getenv(key); s != "" {
		return s
	}
	return defaultVal
}

// envInt reads an int from an environment variable, returning defaultVal if unset or invalid.
func envInt(key string, defaultVal int) int {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		klog.Warningf("Invalid %s=%q, using default %d", key, s, defaultVal)
		return defaultVal
	}
	return v
}

// envInt64 reads an int64 from an environment variable, returning defaultVal if unset or invalid.
func envInt64(key string, defaultVal int64) int64 {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		klog.Warningf("Invalid %s=%q, using default %d", key, s, defaultVal)
		return defaultVal
	}
	return v
}

// envFloat reads a float64 from an environment variable, returning defaultVal if unset or invalid.
func envFloat(key string, defaultVal float64) float64 {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		klog.Warningf("Invalid %s=%q, using default %f", key, s, defaultVal)
		return defaultVal
	}
	return v
}

// envBool reads a bool from an environment variable, returning defaultVal if unset or invalid.
func envBool(key string, defaultVal bool) bool {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.ParseBool(s)
	if err != nil {
		klog.Warningf("Invalid %s=%q, using default %v", key, s, defaultVal)
		return defaultVal
	}
	return v
}
