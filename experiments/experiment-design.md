# Cluster Experiment Design

This document describes how the ParKour cluster evaluation is structured: what
each experiment board asks, which baselines it compares, how metrics are defined
and collected, and which parameters are fixed versus swept.

It is a design document, not a results report. No measurement results are
distributed with this repository; every number a board produces is regenerated
by running it. See [README.md](README.md) for the operational instructions and
script inventory.

## Contents

1. [Goals](#1-goals)
2. [Evaluation dimensions and metrics](#2-evaluation-dimensions-and-metrics)
3. [Cluster experiment flow](#3-cluster-experiment-flow)
4. [Baseline design](#4-baseline-design)
5. [Board A: parameter sensitivity](#5-board-a-parameter-sensitivity)
6. [Board B: end-to-end comparison](#6-board-b-end-to-end-comparison)
7. [Board C: ablation](#7-board-c-ablation)
8. [Board D: microbenchmark](#8-board-d-microbenchmark)
9. [Board E: real-workload scheduling quality](#9-board-e-real-workload-scheduling-quality)
10. [Board F: data-plane latency injection](#10-board-f-data-plane-latency-injection)
11. [Board G: production trace profiling](#11-board-g-production-trace-profiling)
12. [Board H: synchronization-channel freshness](#12-board-h-synchronization-channel-freshness)
13. [Parameter matrix](#13-parameter-matrix)

---

## 1. Goals

Validate the proposed parallel-scheduling mechanisms — **multicandidate
selection** and the **scoring strategy family** (QualityFirst / LatencyFirst /
WeightedRandom, with a conflict-rate penalty) — on a real Kubernetes cluster,
and compare them systematically against several baselines.

> Local state predictive update was removed from the design. It caused ghost
> resource deadlock in the Kubernetes implementation, and simulation showed its
> benefit to be negligible.

**Strategy / paradigm compatibility.**

- Event-driven synchronization: `QualityFirst` or `WeightedRandom`.
  `LatencyFirst` depends on per-partition staleness, which is near-constant
  under event-driven synchronization and therefore carries no signal.
- Periodic synchronization: all of `QualityFirst`, `LatencyFirst`,
  `WeightedRandom`.
- `QualityFirst` and `WeightedRandom` share the scoring formula
  `adjusted = (1-p)*normScore + p*(1-conflictRate)`. The penalty weight `p` is
  swept under `QualityFirst`; `WeightedRandom` reuses the QualityFirst optimum.

The boards answer these questions:

| # | Question | Board |
|---|---|---|
| 1 | What is the best parameter combination (candidate count K, strategy, penalty weight p)? | A |
| 2 | How much do the mechanisms improve scheduling speed, conflict rate and quality over each baseline? | B |
| 3 | What does each mechanism contribute on its own, and do they compose? | C |
| 4 | Is the additional resource and time cost acceptable? | D |
| 5 | How does the effect change with cluster scale and load pressure? | B |
| 6 | Does scheduling quality show up in real application performance? | E |
| 7 | Do the conflict-rate and throughput gains survive an injected post-bind data-plane latency calibrated against real-node public measurements (and a small startup failure rate)? | F |
| 8 | Do a production trace's arrival intensity and request profile support the burst operating point, and do the two synthetic scenarios cover it? | G |
| 9 | Is the conflict-rate feedback channel actually fresher than the full snapshot channel? | H |

---

## 2. Evaluation Dimensions and Metrics

### 2.1 Mapping simulation dimensions to cluster metrics

| Simulation dimension | Simulation metric | Cluster metric | Source |
|---|---|---|---|
| **Scheduling speed** | finished_scheduling_time | Time until the last target pod completes Bind (T_bind_total) | Binder success counter + pod lifecycle watch |
| | throughput (tasks/s) | Control-plane binding throughput (Q_bind, pods/s) | CL2 SchedulingThroughput / Binder success counter |
| | — | Per-pod end-to-end scheduling latency P50/P99 (T_e2e) | `scheduler_e2e_scheduling_duration_seconds` |
| | — | Scheduling algorithm latency P50/P99 (T_algo) | `scheduler_scheduling_algorithm_duration_seconds` |
| **Data plane** | — | Bind-to-Ready/Failed latency (T_data) | Pod lifecycle watch |
| | — | Create-to-Ready latency and Ready throughput (T_user, Q_ready) | Pod lifecycle watch / CL2 PodStartupLatency |
| | — | Data-plane startup failure rate and rebuild amplification | Pod lifecycle watch + Deployment status |
| **Conflict rate** | conflict_rate | Binding conflict rate (R_conflict) | Binder logs / custom Prometheus metric |
| | total_conflicts | Total binding failures (N_conflict) | `scheduler_schedule_attempts_total{result="error"}` |
| | — | Distribution of scheduling attempts per pod | `scheduler_pod_scheduling_attempts` |
| | — | Which candidate rank was adopted | `parasched_candidate_rank_accepted` |
| **Scheduling quality** | avg_scheduled_slot_score | Mean score of the selected node (S_avg) | `parasched_selected_node_score` |
| | — | Node resource-utilization balance (stddev / Gini) | Prometheus node-level resource metrics |
| | — | **Real application performance** (latency / throughput) | wrk / redis-benchmark / sysbench (Board E) |
| **Scheduling efficiency** | score/time ratio | S_avg / T_total | Derived |

### 2.2 Cluster-only metrics

These cannot be measured in simulation and are the distinct value of the cluster
experiments.

| Metric | Meaning | Source |
|---|---|---|
| Scheduler CPU | Per-instance CPU usage | `container_cpu_usage_seconds_total` |
| Scheduler memory | Per-instance RSS | `container_memory_rss` |
| API server request latency | Pressure on the API server | CL2 APIResponsivenessPrometheus |
| Dispatcher throughput / latency | Dispatch rate, per-call duration, queue depth | `parasched_dispatch_*` |
| Scheduler load balance | Evenness of in-flight pods per instance | `parasched_dispatcher_scheduler_inflight` |
| Binder throughput / latency | Bind rate, duration, candidate-rank distribution | `parasched_bind_*`, `parasched_candidate_rank_accepted` |
| Partition sync latency | Duration of one ParSync sync | `parasched_sync_duration_seconds` |
| State freshness | Divergence between a scheduler's local cache and the Binder's authoritative state | `parasched_partition_staleness_seconds` |
| Real application performance | nginx QPS/latency, redis OPS, mysql TPS | wrk, redis-benchmark, sysbench |
| Data-plane injection fidelity | KS distance and percentile error between injected `T_data` and the configured target | Pod lifecycle JSONL |
| Failure recovery cost | Failed pod count, extra binds, time to reach target Ready replicas | Pod lifecycle JSONL + Binder counters |

### 2.3 Collection priority

| Priority | Metrics | Why |
|---|---|---|
| P0 | Throughput, conflict rate, total scheduling time | Core conclusions |
| P0 | Per-pod scheduling latency P50/P99 | Core conclusions |
| P0 | Bind-to-Ready latency distribution, data-plane failure rate (Board F) | Data-plane conclusions |
| P1 | CPU/memory usage, scheduling quality (score + balance) | Microbenchmark |
| P1 | Real application performance (Board E) | Direct evidence for scheduling quality |
| P2 | Sync latency, state freshness, API latency | Supporting analysis |

---

## 3. Cluster Experiment Flow

### 3.1 Overall pipeline

```text
1. Environment preparation
   |- Deploy components (Dispatcher + N schedulers + Binder + controller) -> control-plane node
   |- Apply the para-sched CRDs (ParSyncConfig, SchedulerAssignment)
   |- Replace the default KWOK pod-ready Stage per the experiment profile -> infrastructure node
   |- Create fixed or heterogeneous KWOK virtual nodes
   \- Wait for nodes to become Ready

2. Load injection
   |- Start Prometheus metric collection
   |- Start the pod lifecycle watcher, recording first Bind and first Ready/Failed separately
   |- ClusterLoader2 creates the saturation Deployment
   |- Record end of submission, Bind drain and Ready drain separately
   \- Collect SchedulingThroughput / PodStartupLatency

3. Metric collection
   |- CL2 built-in metrics (SchedulingThroughput, PodStartupLatency, APIResponsiveness)
   |- Prometheus queries for the custom metrics
   \- Conflict statistics exported from the logs

4. Real workload test (Board E, on the physical worker nodes)
   |- Deploy nginx/redis/mysql onto the workers
   |- Run the benchmark tools
   \- Collect application performance metrics

5. Cleanup
   |- Delete Deployments and pods
   |- Delete the KWOK nodes
   \- Reset scheduler state
```

### 3.2 Per-round parameterization

Each round is defined by:

```text
round = {
    node count (N_nodes),
    pod count (N_pods),
    scheduler instance count (N_schedulers),
    method configuration: {
        enable_multicandidate: bool,
        candidate_k: int,
        enable_penalty: bool,
        penalty_weight: float,
        enable_parsync: bool,
        num_partitions: int,
        sync_period: str,
    },
    load pattern: "batch" | "poisson",
    data-plane profile: {
        mode: "zero" | "calibrated" | "tail" | "calibrated-failure",
        distribution_version: str,
        failure_rate: float,
    },
    pod resource request (cpu, memory),
    repetitions (trials),
}
```

### 3.3 Pod scheduling path

```text
Pod created (CL2 Deployment / Poisson injector)
  -> API server
  -> Dispatcher assigns the pod to a scheduler instance
  -> Scheduler schedules locally:
      |- Filter: select feasible nodes
      |- Score: score nodes (including the conflict-rate penalty)
      |- [Multicandidate] select K candidates, rank 0 is the preferred one
      |- Reserve: reserve the preferred node in the local cache
      |- Permit
      \- PreBind -> pass the candidate list to the Binder with the bind request
  -> Binder binds the node (holding or able to query global state):
      |- Try the preferred node (rank 0)
      |   |- success -> pod Scheduled
      |   \- conflict -> [Multicandidate] fall back through the remaining candidates
      |        |- one succeeds -> pod Scheduled
      |        \- all fail -> reschedule
      \- Report the conflict event (updates the conflict-rate statistics, which
         feed the penalty in later scoring)
  -> [KWOK Stage, Board F only]
      |- Wait according to the calibrated distribution after Bind
      |- Success branch -> pod Running/Ready
      \- Failure branch -> pod Failed; the Deployment creates a replacement
  -> [ParSync] periodic partition state synchronization
```

> **Design note.** Reserve only touches the scheduler's local cache. Based on a
> local view it always succeeds and therefore cannot detect concurrent conflict.
> Real contention becomes visible only when the Binder talks to the API server,
> which is why the multicandidate fallback lives on the Binder side.
>
> A pod's resources count against a node as soon as Bind succeeds and
> `spec.nodeName` is written, not when it reaches Running/Ready. Board F must
> therefore report pre-Bind control-plane time and post-Bind data-plane time
> separately, and must not use the whole `WaitForControlledPodsRunning` wall
> time as the throughput denominator.

---

## 4. Baseline Design

### 4.1 Synchronization paradigms

| Paradigm | Trigger | Latency characteristic | Example systems |
|---|---|---|---|
| **Event-driven** | API server watch; pushed on state change | Near real time (ms) | kube-scheduler, Godel |
| **Periodic** | The Binder publishes batched snapshots to ConfigMaps; schedulers watch for changes and apply them within a rotation window of period G | Periodic lag (seconds); data is current within the window | ParSync (ATC'21), the simulator |

Key difference: an event-driven scheduler passively receives state updates
through the Kubernetes Informer mechanism — low latency, but briefly
inconsistent. A periodic scheduler watches the partition snapshot ConfigMaps and
caches changes immediately, but applies them to its local cache only when its
rotation window arrives, so freshness is bounded by G. On the Binder side,
batched publication (100 ms window by default) reduces API server write pressure
from `O(bind_rate)` to `O(M / flushInterval)`.

The proposed mechanisms must be validated **separately under both paradigms**,
to show that the benefit does not depend on a particular synchronization design.

### 4.2 Baselines

Event-driven group:

| ID | Name | Schedulers | Description |
|---|---|---|---|
| E1 | Single kube-scheduler | 1 | Native single scheduler; no parallelism, no conflict |
| E2 | Godel-vanilla | N | Native multi-instance Godel, no optimization |
| E3 | Proposed-EventDriven | N | Multicandidate + penalty (no ParSync) |

Periodic group:

| ID | Name | Schedulers | Description |
|---|---|---|---|
| P1 | globSync | N | All schedulers synchronize everything at once, period G |
| P2 | ParSync-sameSync | N | All schedulers synchronize the same partition at the same time |
| P3 | ParSync-diffSync | N | Schedulers stagger across different partitions |
| P4 | Proposed-Periodic | N | Full method: multicandidate + penalty + ParSync |

Why each one is present:

- **E1** is the ideal reference line: a single scheduler has no conflict and the
  best placement quality, but its throughput is capped by one instance. It shows
  at what scale parallel scheduling starts to win.
- **E2** shows how severe conflict is under event-driven synchronization with no
  optimization at all. It uses `strategy=QualityFirst, p=0, K=0`, which is
  equivalent to plain score ordering.
- **E3** measures the mechanisms under near-real-time synchronization, with
  `candidate_k = K*`, `strategy = strategy_E*`, `penalty_weight = p_E*` from
  Board A and `enable_parsync=false`.
- **P1** corresponds to the simulator's globSync baseline and to traditional
  shared-state designs such as Omega: `sync_pattern=glob, num_partitions=1`.
- **P2** and **P3** correspond to the ParSync paper's sameSync and diffSync
  modes, with multicandidate and penalty disabled.
- **P4** is the complete method under periodic synchronization.

### 4.3 Configuration tables

Event-driven group:

| Setting | E1 | E2 | E3 |
|---|---|---|---|
| Schedulers | 1 | N | N |
| enable_multicandidate | no | no | yes |
| candidate_k | — | — | **K\*** (Board A) |
| strategy | — | — | **strategy_E\*** in {QualityFirst, WeightedRandom} |
| penalty_weight (p) | — | — | **p_E\*** (swept under QF only) |
| enable_parsync | no | no | no |

Periodic group:

| Setting | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| Schedulers | N | N | N | N |
| enable_multicandidate | no | no | no | yes |
| candidate_k | — | — | — | **K\*** (Board A) |
| strategy | QF (p=0) | QF (p=0) | QF (p=0) | **strategy_P\*** |
| penalty_weight (p) | — | — | — | **p_P\*** |
| enable_parsync | yes | yes | yes | yes |
| sync_pattern | glob | same | diff | diff |
| num_partitions | 1 | M | M | M |
| sync_period | G | G | G | G |

Defaults: N = 5 schedulers, M = 5 partitions, G = 1.0 s. The starred parameters
are determined by Board A; boards that use E3 or P4 do not start until Board A
has produced `board-A-optima.yaml`.

---

## 5. Board A: Parameter Sensitivity

### 5.1 Objective

Before the large downstream comparison matrices start, sweep the two paradigms
separately under high contention to fix the parameters E3 and P4 will use, so
that the end-to-end comparison does not understate the method through a poor
parameter choice.

Parameters to decide:

- **K\***: the multicandidate backup count;
- **strategy\***: QualityFirst vs. WeightedRandom (event-driven), plus
  LatencyFirst (periodic);
- **p\***: the penalty weight, meaningful only under QualityFirst
  (WeightedRandom reuses the same optimum; LatencyFirst ignores p).

### 5.2 Sweep method

A greedy per-dimension sweep: fix each dimension at the previous step's optimum
before sweeping the next. This reduces the sweep from a full factorial
(3x5x5 = 75 configurations) to a linear 3+5+5 = 13 per paradigm.

1. **K sweep** — fix `strategy=QualityFirst, p=0.3`, sweep `K in {0, 1, 2, 4}`.
   Choose the smallest K at which the conflict rate has stopped falling
   materially but binder fallback counts have not risen materially.
2. **Strategy sweep** — fix `K=K*, p=0.3`, sweep every strategy available to the
   paradigm; take the best combined throughput/conflict result.
3. **p sweep** — fix `K=K*, strategy=QualityFirst` (always under QF, since
   WeightedRandom shares the optimum), sweep `p in {0.0, 0.1, 0.3, 0.5, 0.7}`.
4. If `strategy* = WeightedRandom`, run one confirmation round at
   `K=K*, strategy=WeightedRandom, p=p*`.

> **Greedy risk.** The K sweep runs at `p=0.3`, which may not be the final `p*`.
> After all three dimensions are done, check whether `p*` falls inside
> (0.1, 0.5); if it lands near a boundary, re-run one K configuration to verify.

### 5.3 Setup

Shared by both paradigms: 10,000 nodes / 10,000 pods / 5 schedulers, the HC-V
high-contention scenario at V=0.6 (24 CPU and 192 Gi per pod, 1 pod/node,
heterogeneous capacity), CL2 `cl2-saturation-only.yaml` with a 500 deployments/s
burst, 3 trials per configuration reported as mean and standard deviation.

> **Why capacity variance V > 0.** With homogeneous shard capacity (V=0), the
> baseline ACF at 5 schedulers is only a few percent, which is not enough spread
> for the K / strategy / p sweep to produce a distinguishable signal. V=0.6
> distributes shard CPU capacity over [24, 47] while holding the cluster's total
> pod capacity at N, which makes LeastAllocated scoring show a clear gradient
> even on an empty cluster and lifts the baseline conflict rate into a range
> where tuning is measurable. Among the piloted values {0, 0.3, 0.6, 1.0}, V=0.6
> had the lowest coefficient of variation across trials and still sat below the
> saturation point at V=1.0, so it is the default for all downstream boards.
> V=0 remains available via `--variance 0` as a control, to confirm that V=0.6
> does not reorder the methods.

Paradigm-specific parameters:

| Paradigm | sync-period | partitions (M) | sync-pattern | Note |
|---|---|---|---|---|
| Event-driven | 0.1 s | — | — | ParSync disabled |
| Periodic | 1.0 s | 5 | diff | Same parameters as P3 |

### 5.4 Matrix

Event-driven (Board S-E):

| Sub-experiment | Fixed | Swept | Values | Configurations |
|---|---|---|---|---|
| S-E-K | strategy=QualityFirst, p=0.3 | K | {0, 1, 2, 4} | 4 |
| S-E-Strategy | K=K\*, p=0.3 | strategy | {QualityFirst, WeightedRandom} | 2 |
| S-E-P | K=K\*, strategy=QualityFirst | p | {0.0, 0.1, 0.3, 0.5, 0.7} | 5 |
| S-E-Confirm | K=K\*, strategy=WR, p=p\* | — | only if strategy\*=WR | 0-1 |

Periodic (Board S-P):

| Sub-experiment | Fixed | Swept | Values | Configurations |
|---|---|---|---|---|
| S-P-K | strategy=QualityFirst, p=0.3 | K | {0, 1, 2, 4} | 4 |
| S-P-Strategy | K=K\*, p=0.3 | strategy | {QualityFirst, LatencyFirst, WeightedRandom} | 3 |
| S-P-P | K=K\*, strategy=QualityFirst | p | {0.0, 0.1, 0.3, 0.5, 0.7} | 5 |
| S-P-Confirm | K=K\*, strategy=strategy\*, p=p\* | — | only if strategy\* is WR or LF | 0-1 |

### 5.5 Selection criteria

| Metric | Role | Source |
|---|---|---|
| Conflict rate `R_conflict` | primary | `parasched_bind_attempts_total{result="conflict"}` |
| Throughput | primary | `scheduler_schedule_attempts_total{result="scheduled"}` |
| P99 scheduling latency | secondary | `scheduler_e2e_scheduling_duration_seconds` |
| Rank-0 hit rate | secondary | `parasched_candidate_rank_accepted` |
| Mean scheduler CPU | constraint | `container_cpu_usage_seconds_total` |

- **K\***: the largest K before the conflict rate's relative improvement over
  the previous step falls below 10%, subject to CPU cost under 2x vanilla.
- **strategy\***: at the same K, the lowest conflict rate whose throughput is
  within 5% of the best.
- **p\***: at the same K and strategy, the p minimizing
  `conflict_rate * (1 / throughput)`.

### 5.6 Output

The board produces [`board-A-optima.yaml`](board-A-optima.yaml), recording each
paradigm's optimum. Boards B, C and E read E3 and P4 configurations from it.

---

## 6. Board B: End-to-End Comparison

### 6.1 Objective

Validate the motivation and the effect of the mechanisms under two load
scenarios:

1. **Low contention** (ordinary workloads): show the single-scheduler throughput
   ceiling, and that the mechanisms have no adverse effect where conflict is
   rare.
2. **High contention** (resource-intensive workloads): show that vanilla
   parallel scheduling conflicts heavily and that the mechanisms improve it
   substantially.

### 6.2 Load scenarios

| Parameter | Low contention | High contention (HC-V) |
|---|---|---|
| Pod CPU request | 1000m | 24000m |
| Pod memory request | 8Gi | 192Gi |
| Pods per node (M) | 29 | **1** |
| Capacity variance (V) | 0 (homogeneous) | **0.6** (default; 0 / 0.3 / 1.0 available) |
| Paper narrative | Ordinary microservice workload | Exclusive workload (ML training, HPC, large databases) |
| Simulation analogue | pod_per_node >> 8 (conflict near 0) | pod_per_node = 1 plus node score variance |

> **Rationale.** Simulation identifies `pod_per_node` as the dominant
> determinant of the conflict rate: it is high at one pod per node, drops sharply
> at two, and becomes negligible from four upward. HC-V therefore fixes M=1 and
> adds capacity heterogeneity on top: the CPU capacity of the 10 KWOK shards is
> spread over [24, 47] by a discretized log-normal, while the cluster's total pod
> capacity stays equal to N so the strict one-pod-per-node semantics is
> preserved. This is the cluster analogue of the simulator's
> `slot_score_variance`.
>
> **CL2 configuration.** HC-V uses the dedicated `cl2-saturation-only.yaml`,
> which contains only the saturation phase (at M=1 the nodes are already fully
> saturated, so no latency phase is needed) and bursts every pod in at 500
> deployments/s, far above a single scheduler's throughput, so the scheduling
> queue saturates immediately. Per-shard capacity is injected into CL2 through
> testoverrides generated by
> `experiments/scripts/generate-hetero-config.py --variance V`.

### 6.3 Scenarios

#### B1: Low-contention scale-out

Show that (a) the single-scheduler throughput bottleneck grows with scale, and
(b) the mechanisms match vanilla throughput where conflict is rare.

Fix N=5 schedulers (E2/E3) and vary the node count:

| Scale | Nodes | Pods (29/node) |
|---|---|---|
| S | 1,000 | 29,000 |
| M | 2,000 | 58,000 |
| L | 5,000 | 145,000 |

> 10,000 nodes (290,000 pods) exceeds what a single master API server sustains
> here: the API writes generated during E1 cleanup cause API server timeouts in
> the following experiment. It is therefore excluded from the low-contention
> scenario. High contention has pods = nodes and is not affected.

Groups: E1, E2, E3, 3 trials each, event-driven only. Report throughput and
total scheduling time; conflict rate is a reference only, as it is expected to
be near zero. Periodic synchronization is not tested here, because with almost
no conflict the paradigms are indistinguishable and the point of the scenario is
the chain "a single scheduler is not enough -> parallelism is needed -> the
mechanisms do not cost throughput".

#### B2: High-contention scale-out

Show that (a) vanilla parallel scheduling conflicts heavily, and (b) the
mechanisms cut the conflict rate and raise useful throughput.

Fix N=5 schedulers and vary the node count:

| Scale | Nodes | Pods (1/node) |
|---|---|---|
| M | 2,000 | 2,000 |
| L | 5,000 | 5,000 |
| XL | 10,000 | 10,000 |
| XXL | 20,000 | 20,000 |

Groups: E1, E2, E3 and P1, P2, P3, P4, 3 trials each. Report throughput,
conflict rate, total conflict count and total scheduling time.

#### B3: High-contention scheduler scale-out

Show that the conflict rate worsens as scheduler instances are added, and that
the mechanisms stay robust under higher concurrency.

Fix 10,000 nodes / 10,000 pods (HC-V, V=0.6) and vary the scheduler count over
{2, 4, 6, 8, 10}.

Groups: E2, E3 (event-driven) and P3, P4 (periodic), 3 trials each. E1 is
omitted because its throughput and conflict rate do not vary with the scheduler
count and B1 already provides the reference.

Report the throughput scaling ratio and the conflict rate against instance
count.

> The scheduler count is capped at 10 by control-plane node memory: at 10
> schedulers the para-system working set peaks near half of the 128 GB
> available. The range 2-10 is enough to show both the conflict trend and the
> mechanisms' robustness.

---

## 7. Board C: Ablation

### 7.1 Objective

Decompose the two mechanisms (multicandidate and penalty) to quantify each one's
individual and combined benefit.

- The ablation runs **only under high contention**. With low contention the
  conflict rate is so low that the mechanisms are indistinguishable.
- It runs **under both synchronization paradigms**, to show generality.
- All "+P" groups use Board A's `p*`; all "+M" groups use Board A's `K*`. The
  parameter sweep itself lives in Board A and is not repeated here.

### 7.2 Combinations

Event-driven (Group Ab-E), based on E2, all with `strategy=QualityFirst` so the
+M / +P / +MP semantics stay clean:

| ID | Configuration | K | p |
|---|---|---|---|
| Ab-E0 | E2 (baseline) | 0 | 0 |
| Ab-E1 | +M | K\* | 0 |
| Ab-E2 | +P | 0 | p_E\* |
| Ab-E3 | +MP (E3) | K\* | p_E\* |

Periodic (Group Ab-P), based on P3, also fixed at `strategy=QualityFirst`:

| ID | Configuration | K | p |
|---|---|---|---|
| Ab-P0 | P3 (baseline) | 0 | 0 |
| Ab-P1 | +M | K\* | 0 |
| Ab-P2 | +P | 0 | p_P\* |
| Ab-P3 | +MP (P4-QF) | K\* | p_P\* |

> If Board A selects `strategy_P* != QualityFirst`, Ab-P3 is a forced-QF ablation
> control only, and Boards B and E use the real `strategy_P*` for P4.

### 7.3 Setup

10,000 nodes / 10,000 pods / 5 schedulers, HC-V at V=0.6. Each group runs 3
trials. Periodic groups use G=1.0 s and M=5.

---

## 8. Board D: Microbenchmark

### 8.1 Objective

Quantify the extra cost of multicandidate and penalty, and show it is
negligible.

D1, D2 and D3 all **reuse the Prometheus data of Boards B and C** and need no
independent rounds: container resource usage, per-stage scheduling duration and
Dispatcher/Binder costs are collected continuously during every round, and only
need to be extracted by time window during analysis. Only D4 needs dedicated
rounds.

### 8.2 D1: Scheduler resource cost

| Source | Dimensions covered | Extracted |
|---|---|---|
| B2 | E1/E2/E3/P1-P4, 2,000-20,000 nodes | CPU and memory against cluster scale |
| B3 | 2/4/6/8/10 schedulers | CPU and memory against instance count |
| C | Mechanism on/off combinations | Incremental cost of each mechanism |

Metrics, extracted at 10 s granularity over the experiment window:

- `rate(container_cpu_usage_seconds_total{container="scheduler", namespace="para-system"}[1m])`
- `container_memory_rss{container="scheduler", namespace="para-system"}`

### 8.3 D2: Per-pod scheduling cost

| Stage | Metric |
|---|---|
| Total algorithm time | `scheduler_scheduling_algorithm_duration_seconds` |
| Filter | `scheduler_framework_extension_point_duration_seconds{extension_point="Filter"}` |
| Score | `scheduler_framework_extension_point_duration_seconds{extension_point="Score"}` |
| Candidate selection | `scheduler_parasched_candidate_selection_duration_seconds` |

Compared as E2 vs. E3 and P3 vs. P4.

### 8.4 D3: Dispatcher and Binder cost

Dispatcher:

| Metric | PromQL |
|---|---|
| Dispatch throughput | `rate(parasched_dispatch_total{result="success"}[1m])` |
| Dispatch failure rate | `rate(parasched_dispatch_total{result="error"}[1m]) / rate(parasched_dispatch_total[1m])` |
| Dispatch latency P50/P99 | `histogram_quantile(0.99, rate(parasched_dispatch_duration_seconds_bucket[5m]))` |
| Queue depth | `parasched_dispatcher_queue_depth` |
| Scheduler load balance | `stddev(parasched_dispatcher_scheduler_inflight)` |

Binder:

| Metric | PromQL |
|---|---|
| Bind throughput | `rate(parasched_bind_attempts_total{result="success"}[1m])` |
| Bind conflict rate | `rate(parasched_bind_attempts_total{result="conflict"}[1m]) / rate(parasched_bind_attempts_total[1m])` |
| Bind latency P50/P99 | `histogram_quantile(0.99, rate(parasched_bind_duration_seconds_bucket[5m]))` |
| Candidate rank distribution | `parasched_candidate_rank_accepted` |
| All-candidates-failed rate | `rate(parasched_all_candidates_failed_total[1m])` |

What to watch for:

- Dispatch latency should stay under 1 ms (it is only an annotation patch); a
  P99 above 5 ms indicates a bottleneck.
- Bind latency should grow linearly in `candidate_k` (at most one extra API call
  per additional candidate).
- Queue depth should approach zero in steady state; sustained growth means too
  few Dispatcher workers.
- Per-instance scheduler in-flight counts should differ by under 10%.

### 8.5 D4: Synchronization paradigm cost

| Paradigm | Configuration |
|---|---|
| Event-driven | kube-scheduler / Godel-vanilla, informer List/Watch |
| Global periodic | globSync, G=5 s |
| Partitioned periodic | ParSync, P=10, G=5 s |

Metrics: `parasched_sync_duration_seconds`, the incremental API server request
volume caused by synchronization, and `parasched_partition_staleness_seconds`.

---

## 9. Board E: Real-Workload Scheduling Quality

### 9.1 Objective

Deploy real applications (nginx, redis, mysql) on physical worker nodes and
measure scheduling quality through application-level performance. KWOK nodes
cannot run real containers, so this is the one thing the emulated experiments
cannot provide.

It is a supplementary check on scheduling quality. The main evaluation remains
the large-scale KWOK experiments.

### 9.2 Matrix

2 strategies x 3 stress profiles x 5 trials = 30 benchmark rounds. Each round
runs three benchmarks concurrently for 180 s.

| ID | Strategy | Stress profile | What it shows |
|---|---|---|---|
| RW-E2-none | Godel-vanilla | none | E2 baseline without stress |
| RW-E2-mild | Godel-vanilla | mild | E2 ignores a 2-CPU gradient (it sees score only, not penalty) |
| RW-E2-heavy | Godel-vanilla | heavy | E2 piles onto squeezed nodes under a strong gradient |
| RW-E3-none | Proposed-EventDriven | none | With no conflict signal, E3 should match E2 (no adverse effect) |
| RW-E3-mild | Proposed-EventDriven | mild | Per-node counts start to identify hot spots |
| RW-E3-heavy | Proposed-EventDriven | heavy | E3's advantage over E2 should be largest here |

The expected shape of the result is that E3's advantage over E2 rises
monotonically with stress intensity, and that the placement-quality Gini
coefficient shows E3 shifting workload pods toward less-stressed nodes while E2
stays uniform and ignores the real contention.

### 9.3 Procedure

One-time prerequisite: every workload image must be pulled and imported into the
workers' containerd, with the workload YAML using `imagePullPolicy: Never`, so
that registry pull latency and rate limits do not pollute the measurement.

```bash
# Run once on a node with Docker and external network access
./experiments/scripts/preload-workload-images.sh
# Covers polinux/stress-ng, nginx:1.27-alpine, redis:7-alpine, mysql:8.0
```

Each `<strategy>-<profile>` round:

1. Clean up leftover workload namespaces on the workers.
2. Configure the scheduler (E2/E3 map to CANDIDATE_K / PENALTY).
3. Deploy the stress pods for the profile, bound directly by `nodeName`.
4. Wait 15 s for stress to reach steady-state CPU load.
5. Deploy the nginx / redis / mysql Deployments, scheduled by para-scheduler.
6. Compute `placement-quality.json` once, as the placement-layer snapshot.
7. Run 5 trials: three benchmarks concurrently for 180 s, collecting wrk /
   redis-benchmark / sysbench output plus Prometheus node resource series.
8. Delete the namespace.

### 9.4 Validity boundaries

- Only three physical workers, far smaller than the KWOK experiments. This board
  validates the mapping from scheduling quality to application performance; it
  does not carry the large-scale evaluation narrative.
- Stress pods are bound by `nodeName` and bypass para-scheduler, so this board
  does not test multicandidate under high-conflict load (B2/B3 do). It isolates
  the penalty mechanism's effect on workload placement.
- The `heavy` profile locks 8 CPU per node (4 stress pods x 2 CPU) and needs
  workers with at least 16 allocatable CPU. A smaller testbed must scale the
  stress pods' CPU requests or the workload replica counts proportionally.

---

## 10. Board F: Data-Plane Latency Injection

### 10.1 Question and causal boundary

KWOK omits kubelet and the CRI, so a reader may reasonably suspect that the
omitted data-plane latency changes the conclusions.

Mechanically, a scheduler enters its next cycle as soon as Bind succeeds and
does not wait for Ready; in the fill-type workload used here pods never
terminate, so the data plane never feeds back into scheduling through resource
release. Only two channels remain by which data-plane latency can affect the
control plane: (a) the extra API server pressure of KWOK's state updates, and
(b) startup failures triggering Deployment rebuilds, which add scheduling input.

Board F therefore tests exactly two propositions:

1. **F1**: after injecting real-node-magnitude post-bind latency (`Dreal`),
   ParKour's `ACF` reduction and `Q_bind` gain over same-code vanilla hold
   (`gain_retention >= 0.8`), and the Bind cumulative curve does not shift right
   with the Ready curve.
2. **F2**: with 1% startup failure added, ParKour keeps its gain under the extra
   rebuild pressure.

Three timestamps and two metric classes must stay separate:

```text
t_create -- scheduling / conflict handling --> t_bind -- kubelet / CRI --> t_ready | t_failed
T_control = t_bind - t_create     T_data = t_ready - t_bind     T_user = t_ready - t_create
```

`t_bind` is when the watcher first sees a non-empty `spec.nodeName`; `t_ready`
and `t_failed` are when it first sees `Ready=True` or `phase=Failed`. All are
recorded by one watcher on a monotonic clock. The primary control-plane metrics
(`Q_bind`, `ACF`) end at `BIND_END_TS`; the Ready wait must never enter the
throughput denominator.

### 10.2 Latency distribution source

The distribution is anchored on public Kubernetes SIG-scalability CI
measurements (ClusterLoader2 on real GCE/AWS nodes, upstream default
configuration) published at perf-dash.k8s.io, using the `schedule_to_run` phase:
`spec.nodeName` written to `phase=Running`, which is exactly the post-Bind
kubelet/CRI latency and differs from `T_data` only by readiness.

The snapshot, the fetch script and the full derivation live in
[`kwok-setup/stages/calibration/`](kwok-setup/stages/calibration/README.md).

| Profile | Anchor P50 / P90 / P99 (ms) | Source | Use |
|---|---|---|---|
| `Z0` | 0 | Current fast Stage | No-latency control |
| `Dreal` | 830 / 1109 / 2384 | gce-5000Nodes burst phase (heaviest tail among comparable phases) | Main experiment |
| `Dreal-F1` | same as `Dreal` plus 1% failure | The failure rate is a sensitivity setting, not a measurement | F2 |
| `Dtail` | 923 / 1823 / 5312 | gce-100Nodes fill phase (P99 exceeds the 5 s SLO) | Heavy-tail sensitivity; defined but not implemented |

Paper wording should be "post-bind delays drawn from a distribution matched to
upstream Kubernetes scalability-CI measurements on real GCE/AWS nodes", with the
fetch date and build count, and must not be described as a self-measurement of
this testbed.

### 10.3 Stage injection and the fidelity gate

KWOK v0.7.0 Stages drive pod state transitions by selector and only act on pods
that already carry `spec.nodeName`, so the injection happens after Bind and never
touches the Scheduler or Binder path. Several Stages on the same selector are
chosen at random by `weight`, and each samples uniformly within its interval
using `durationMilliseconds` + `jitterDurationMilliseconds` (in v0.7.0 the
jitter value is the interval's upper bound, verified experimentally).

Upstream CI publishes only P50/P90/P99, so the buckets are derived directly from
those three anchors. The probability mass is strictly consistent with the
percentile definitions, and the two ends use `0.7*P50` and `2*P99` as bounded
approximations:

| Bucket | Range | Probability | weight |
|---|---|---:|---:|
| b0 | `[0.7*P50, P50]` | 0.50 | 5,000 |
| b1 | `(P50, P90]` | 0.40 | 4,000 |
| b2 | `(P90, P99]` | 0.09 | 900 |
| b3 | `(P99, 2*P99]` | 0.01 | 100 |

The fidelity gate accordingly requires the measured post-injection `T_data` to
be within a KS distance of 0.08 of the piecewise-uniform target, with P50/P90/P99
error at most `max(50 ms, 10%)`. It verifies that the injector reproduces the
*configured* distribution, not this testbed's real one.

Scripts: `calibrate-dataplane.py` derives the profile from the anchors;
`injection-fidelity-gate.py` produces the gate report, which must be PASS before
a matrix round is accepted.

### 10.4 Load and matrix

The load is identical to B2 at 10,000 nodes: 10,000 KWOK nodes, HC-V at V=0.6
(24 CPU / 192 Gi requests, 1 pod/node), 10 schedulers, 500 deployments/s burst,
fixed random seed. The `Z0` result should agree with the existing B2-10000n data
within 10% on throughput, which serves as an environment-drift check.

Method configurations match the Board B production configuration item by item
and must not be chosen independently:

| Method | Paper name | Paradigm | partitions | sync_pattern | sync_period | K | w |
|---|---|---|---:|---|---:|---:|---:|
| E2 | vanilla-E | event-driven | 1 | `diff` | 0.1 s | 0 | 0 |
| E3 | ParKour-E | event-driven | 1 | `diff` | 0.1 s | **2** | **0.5** |
| P1 | vanilla-P | periodic globSync | 1 | `glob` | 1.0 s | 0 | 0 |
| P4 | ParKour-P | periodic globSync | 1 | `glob` | 1.0 s | **2** | **0.5** |

Three constraints:

- **Periodic uses globSync only.** The main results already establish globSync as
  the better synchronization mode; the partition rotation of sameSync/diffSync
  raises average staleness. Board F's independent variable is data-plane latency,
  and globSync keeps partition-sync cost out of it.
- **`K=2` and `w=0.5` are Board A's values, used in the paper.** Board F must
  reuse them, or the paper would present two different values of K.
- **Event-driven and periodic partition settings are configured
  independently.** Both happen to be partitions=1, but from different sources,
  and the scripts must not share one variable for them.

Matrix: 3 data-plane scenarios x 2 paradigms x 2 methods, with the repetition
count differing by paradigm.

| Sub-experiment | Paradigm | Methods | Profiles | Trials | Rounds | Entry point |
|---|---|---|---|---:|---:|---|
| F1-E | event-driven | E2, E3 | `Z0`, `Dreal` | 3 | 12 | `run-module-f.sh --experiment F1-E` |
| F1-P | periodic globSync | P1, P4 | `Z0`, `Dreal` | **5** | 20 | `run-module-f1-p.sh` |
| F2-E | event-driven | E2, E3 | `Dreal-F1` | 3 | 6 | `run-module-f2-e.sh` |
| F2-P | periodic globSync | P1, P4 | `Dreal-F1` | **5** | 10 | `run-module-f2-p.sh` |
| **Total** | | | | | **48** | |

**Why the repetition counts differ.** Between-trial variance is small on the
event-driven side, and its `Z0` result agrees with Board B's independent
measurement closely enough that 3 repetitions support the conclusion. The
periodic side is markedly noisier: the baseline's per-round window varies by a
factor of three and per-round ACF reductions scatter widely, so 3 repetitions
cannot separate the real effect from round noise. It uses 5.

Within one trial index all configurations share the load, and the execution
order is randomly rotated under a fixed seed. Each trial is a complete
randomized block, so the round count may only be truncated at block boundaries.
The scripts pick the paradigm's default repetition count from `--experiment`;
`--trials N` overrides it explicitly.

Godel does not enter the Board F matrix: its conflict counters have different
semantics from `scheduler-lib`, and its throughput measurement has the largest
between-round variation, which cannot support a conclusion about data-plane
latency as the independent variable. Its runner code path is kept in the script
but is outside the matrix.

### 10.5 Metrics

| Class | Metric | Definition |
|---|---|---|
| Control plane (primary) | `T_bind_total` | `max(t_bind) - min(t_create)`, until the first `N_target` distinct pods complete Bind |
| | `Q_bind` | `N_target / T_bind_total` |
| | `ACF` | `N_acf / (N_target + N_acf)`, the pod-level reschedule rate; **the only formal conflict metric** |
| Control plane (diagnostic) | `R_conflict` (BCR in the paper) | `N_conflict / (N_target + N_conflict)`, candidate-level; the numerator grows with K, so it is not comparable across K or across implementations |
| | `T_sched_p50/p99`, `rank_hit` | Same definitions as Board B |
| | `partition_staleness` | Age of the partition snapshot at installation; diagnostic here, compared properly in Board H |
| Data plane (secondary) | P50/P90/P99 of `T_data`, `T_user` | Successful pods |
| | `T_ready_total`, `Q_ready` | From `min(t_create)` to `N_target` concurrently Ready replicas |
| F2 only | `F_observed`, `N_failed`, `A_rebuild` | `A_rebuild` = distinct pods that ever bound successfully / `N_target`; theoretical reference `1/(1-f)`. Sanity check only |
| Injector health | The fidelity gate metrics above | Recorded every round |

Gross bind throughput under a failure profile is a reference only and must not
replace `Q_bind` over the first cohort of target pods, since replacement pods
add input.

### 10.6 Statistical protocol

Metric tiers:

| Tier | Metrics | Use |
|---|---|---|
| Formal primary | `Q_bind`, `ACF` | The gain judgement that enters the paper |
| Validity gate | `F_observed` and its Wilson 95% CI, `A_rebuild`, the injection gate | Whether a round is valid |
| Diagnostic | `R_conflict` (BCR), `T_sched_p50/p99`, `rank_hit`, injector health | Explains mechanism behavior; never a gain judgement |
| Auxiliary diagnostic | The watcher's per-pod `T_data`/`T_user`, Bind/Ready curves | Never enters a formal table |

`Q_bind` is taken from the CL2 saturation window (`N_target / T_window`) by
default, with documented fallbacks when that window is contaminated.

A round that times out is no longer discarded outright: if `scheduled >=
--min-complete-pods` (default 9900 = T99), the round counts as valid, is marked
censored, and its makespan is recorded as right-censored.

**Invalid-round handling.** Only one failure class is retryable: the
injection-rate gate's Wilson CI failing to cover the configured value, while CL2
exited normally and the failure semantics were valid. Every other failure aborts
the matrix immediately, with at most 2 retries per round and 4 per matrix. The
classification is implemented in `classify_round_failure` inside
`run-module-f.sh`.

---

## 11. Board G: Production Trace Profiling

### 11.1 Position

Board G is pure offline analysis and runs no cluster experiment. It answers two
questions:

1. **Is the burst operating point representative of production?** Board B
   saturates the scheduling queue at 500 deployments/s, which a reader may see as
   artificially extreme.
2. **How does the synthetic load relate to a production request profile?** Board
   B uses a single 24-CPU shape at 1 pod/node.

Arrival replay is deliberately not done. A production trace's arrival rate is
close to binary along the contention dimension: while there is load, it is
mostly far above a single scheduler's throughput (equivalent to sustained
burst), and in the troughs there is almost no conflict. Replaying the arrival
sequence second by second yields no discriminating transition samples, while a
full replay of 14M tasks / 1.4B pods is prohibitively expensive. The trace is
therefore used as workload-profile evidence, not as an experimental variable.

### 11.2 Data and definitions

Data: Alibaba Cluster Trace v2018 — `batch_task` (about 124 MiB, 14,295,731
tasks over roughly 4,034 machines and more than 8 days), plus `machine_meta` and
`container_meta`. A `batch_task` row is a task group, so pod-equivalent arrivals
expand by instance: `pod_arrivals(t) = sum(instance_num(task))` at
`task.start_time = t`.

Filtering and conversion: drop records with `start_time <= 0`,
`instance_num <= 0`, or invalid requests; `plan_cpu=100` counts as 1 CPU;
`plan_mem` is normalized against a 100-unit node capacity; a task's terminal
state is not used for filtering, since Failed, Running and Terminated tasks all
entered the system. Arrival times are floored to the second and binned into 60 s
windows (1 / 10 / 300 s also reported); the active region is the set of windows
with arrivals.

### 11.3 Results

Arrival intensity over the active region: the median 60 s window is about 1,800
pods/s, and the distribution over 60 s windows is roughly P50/P90/P95/P99 =
1,524 / 3,739 / 4,763 / 9,641 pods/s.

| Threshold | Share of active 60 s windows below it |
|---|---:|
| < 100 pods/s | 1.5% |
| < 250 pods/s | **1.8%** |
| < 500 pods/s | 4.3% |
| < 1000 pods/s | 17.9% |

**Conclusion 1.** While there is load, more than 98% of the time the arrival rate
exceeds a single scheduler's ceiling of roughly 250 pods/s. Board B's saturation
burst therefore targets a common production operating point, not an inflated
extreme.

Request profile, taken from a representative median minute window: pods are far
smaller than nodes (CPU request P50/P95/P99 = 1 / 1 / 1 core, maximum 10;
normalized memory P50/P95/P99 = 0.30 / 0.59 / 0.79; `instance_num` P50/P90/P99 =
1 / 79 / 1,241). Physical machines are largely homogeneous (about 96 CPU and 100
memory units); the heterogeneity comes from co-located online containers
reducing effective available capacity.

**Conclusion 2.** Production pods are much smaller than nodes, so a real node
holds many pods — the simulator's large M and the cluster's low-contention B1.
HC-V's 1 pod/node is a deliberately constructed contention upper bound. The two
synthetic scenarios together cover the production operating point. Cluster
results under mixed request shapes and gang structure are not measured here and
are stated as a limitation.

### 11.4 Artifacts

`experiments/trace/alibaba2018/plot_arrival_figure.py` generates the arrival-rate
figure directly from the raw CSV with no cluster dependency. The script's
docstring gives the download URL for the trace, which is not redistributed here.

### 11.5 Validity boundaries

- `start_time` is a task's start / runnable time, not necessarily user
  submission time; the `instance_num` expansion assumes a task's instances become
  schedulable simultaneously.
- The absolute peak windows are dominated by a few very large tasks, so the
  profile uses a representative percentile window rather than describing the peak
  as typical.
- Effective-capacity heterogeneity comes from co-located background load, not
  from hardware models; CPU requests are over-subscribed in production and
  cannot be mapped to allocatable without processing.
- This board produces no throughput or conflict-rate result, so "no regression
  under real mixed requests" remains unverified.

---

## 12. Board H: Synchronization-Channel Freshness

### 12.1 Position

This board does one thing: **measure the delivery-freshness difference between
the two state synchronization channels**. It produces no method comparison and
does not involve the data plane.

The motivation comes from the cross-paradigm ablation in Board C: enabling the
penalty alone reduces ACF only slightly under event-driven synchronization
(`G=0.1 s`, near-real-time state) but substantially under periodic globSync
(`G=1.0 s`, stale state). Same code, same `w=0.5`; the only difference is
whether the state is fresh. This suggests the penalty works because **at the same
nominal period, a lightweight payload arrives on time and a full snapshot does
not** — the conflict-rate feedback acts as a fresher proxy for node state.

Existing data supports only half of that claim. `partition_staleness` shows the
age a full snapshot already carries at installation, and in Board F's periodic
rounds a majority land in buckets beyond even the most permissive on-time bound
(heartbeat period plus the measured `sync_duration_p99`). But **the conflict-rate
feed has never itself been measured**; the first half of the claim rests on
payload size alone. Board H closes that gap.

### 12.2 The two channels

| Channel | Payload | Publication cadence | Consumption |
|---|---|---|---|
| Full partition snapshot | Complete state of 10,000 nodes, split by partition | `snapshot-flush-interval` 100 ms on change / 1 s heartbeat | Schedulers pull and apply on `G / M` |
| Conflict-rate feed | `[attempts, conflicts]` counters for contended nodes only | `stats-flush-period` **1 s** | Schedulers watch the AdoptionStats CRD and install a whole-table replacement |

Both have a nominal 1 s cadence, **so any lead cannot come from a higher
publication frequency**. If a gap exists, it can only come from the difference in
deliverable payload volume. That is what this board tests.

### 12.3 Metric definitions and bucket requirement

| Metric | Observation point | Meaning |
|---|---|---|
| `scheduler_parasched_partition_staleness_seconds` | The instant the scheduler **applies a partition snapshot** | `now - snapshot.Timestamp` |
| `scheduler_parasched_penalty_signal_age_seconds` | The instant the scheduler **installs the conflict-rate table** | `now - AdoptionStats.status.lastUpdateTime` |

The two metrics **must share one set of histogram buckets**, or their
distributions cannot be compared bucket by bucket. This is enforced by a single
`stalenessBuckets` variable in `metrics/parasched.go` rather than by convention.

The buckets are `ExponentialBuckets(0.01, 1.5, 20)` rather than the earlier
`ExponentialBuckets(0.01, 2, 12)`. The old scheme had only three boundaries
between 0.5 and 3 s, which is exactly the interval that decides "did this exceed
one synchronization period", leaving several Board F rounds undecidable. The new
scheme has five.

`histogram_quantile` interpolates linearly within exponential buckets, so **the
interpolated values must not be quoted as formal numbers**; only bucket
membership is robust, since it depends on cumulative counts alone. Every
judgement is made by bucket membership.

Both observation points are taken at the moment of installation, so the
comparison is like for like.

### 12.4 Configuration

The metrics only produce samples when the penalty is enabled (`w > 0`), so the
ParKour-P configuration is required. The environment reuses B2 at 10,000 nodes,
matching Boards B, C and F.

| Item | Value |
|---|---|
| Sub-experiment | H1 |
| Paradigm | periodic globSync, `G=1.0 s`, partitions=1 |
| Method | ParKour-P (`K=2`, `w=0.5`) |
| Data-plane profile | `Z0` (no injection; the data plane is not this board's variable) |
| Scale | 10,000 nodes, HC-V `V=0.6`, 10 schedulers, 500 deployments/s burst |
| Trials | **3**, about 30 minutes |

`Z0` means the KWOK processes load only `kwok-config.yaml` and use its compiled
built-in default Stage; there is no cluster Stage object to back up or replace.
The preflight therefore checks process arguments: all 10 shards must have exactly
one `--config`, none containing `pod-ready-dreal`. **Do not use
`kubectl get stages`** — that resource type does not exist in this cluster, and a
failed query would be silently swallowed and misread as a pass.

The entry point is `experiments/scripts/run-board-h.sh`, with the parameters
above hard-coded and dry-run by default:

```bash
bash experiments/scripts/run-board-h.sh              # preview the configuration and command
bash experiments/scripts/run-board-h.sh --execute    # run the 3 rounds
```

Under `--execute` it first runs three preflight checks and exits on the first
failure:

1. **No leftover Board F Stage, and the default `pod-ready` Stage in place.**
   This check is not optional: `run-experiment.sh` predates Board F and performs
   no Stage injection at all — it uses whatever Stage the cluster currently
   carries. If a Board F run aborted before its restore logic completed, H1 would
   run all 3 rounds with latency injection still active and look entirely normal.
2. **The schedulers run the instrumented image**, identified by the
   `partition_staleness` bucket count (13 `le` values in the old implementation,
   21 in the new) and by `penalty_signal_age` being exported.
3. All para-system components ready.

After the run the script calls `process-results.py` itself, verifies that the
freshness fields actually landed, and prints a p50/p99 comparison of the two
channels.

### 12.5 Decision rule

Let `S` be `partition_staleness` and `A` be `penalty_signal_age`, from the same
round and the same definition:

| Result | Verdict |
|---|---|
| The upper bound of `A`'s p99 bucket <= the lower bound of `S`'s p99 bucket | **Supported**: the lightweight signal does arrive first, by more than the bucket resolution |
| Both p99 values land in the same bucket | **Not supported**: no gap is visible at this resolution; either reword the claim or re-measure at higher resolution |
| The lower bound of `A`'s p99 bucket >= the upper bound of `S`'s p99 bucket | **Refuted**: the lightweight signal is staler, and the penalty's effect needs another explanation |

p50 is reported too, but used for calibration rather than evidence: it mostly
reflects the refresh cadence rather than lateness.

### 12.6 Isolation requirements

This board uses an **instrumented image** (the new metrics sit outside the
scoring path, but the binary differs), so:

1. It must run only **after** the formal matrices of the boards it depends on
   have completed. The image must not change mid-matrix, or the git commit
   recorded in a matrix's metadata would be inconsistent.
2. The image must use a **traceable dedicated tag** (for example
   `staleness-probe-<date>`), never `latest`.
3. H1's `Q_bind` and `ACF` are **sanity checks only** — they should be the same
   order of magnitude as Board F's ParKour-P `Z0` — and must never be merged into
   another board's statistics or used for method comparison.

### 12.7 Collection-side wiring

A new metric needs three changes in step, and missing any one leaves `null` in
`round-summary.json`:

1. `collect-metrics.sh`: add the `penalty_signal_age_p50/p99` PromQL queries.
2. `process-results.py`: read those JSON files and write the
   `penalty_signal_age_p50_ms` / `penalty_signal_age_p99_ms` fields.
3. `process-results.py`: append the same fields to the CSV column list.

This is not hypothetical: an earlier `penalty_lookup_p50_ms` field was `null` in
every periodic round precisely because the metric was instrumented but never
queried.

### 12.8 Validity boundaries

- Both metrics observe the age **at installation** and exclude further ageing
  between installation and use. They measure delivery timeliness, not the actual
  staleness at decision time, which is strictly worse.
- The two channels have different refresh semantics (snapshots are pulled per
  partition, conflict rates are pushed by CRD watch), so the absolute difference
  cannot be attributed entirely to payload volume.
- This board can only show **whether a freshness gap exists**, not that the
  penalty's benefit comes from that gap. Establishing causality would require
  manipulating the mediating variable (artificially delaying the signal,
  permuting the node-to-conflict-rate mapping), which is a separate experiment.
- Three rounds establish an order-of-magnitude difference only. If the two
  distributions' bucket memberships turn out close, the round count or the bucket
  resolution must be increased and the board re-run.

---

## 13. Parameter Matrix

### 13.1 Fixed parameters

| Parameter | Value |
|---|---|
| KWOK node spec | 32 CPU / 256 Gi (HC-V uses the capacity tiers of section 6.2) |
| Repetitions | 3 (Board F: at least 3, 5 where budget allows) |

### 13.2 Load scenario parameters

| Parameter | Low contention | High contention (HC-V) | Note |
|---|---|---|---|
| Pod CPU request | 1000m | 24000m | Ordinary vs. exclusive |
| Pod memory request | 8Gi | 192Gi | Ordinary vs. exclusive |
| PODS_PER_NODE (M) | 29 | **1** | Node capacity determines conflict probability |
| Capacity variance (V) | 0 | **0.6** | Per-shard CPU in [24, 47], sum fixed at 320 |
| CL2 config | cl2-schedule-pods.yaml | **cl2-saturation-only.yaml** | HC-V has no latency phase |
| CL2 saturation creation rate | 5 deployments/s | **500 deployments/s (burst)** | Burst far above single-scheduler throughput |
| CL2 latency creation rate | 50 deployments/s | — | HC-V has no latency phase |

> **Node capacity arithmetic.**
> Low contention (V=0): a 32 CPU / 256 Gi node gives `floor(32/1)=32` and
> `floor(256/8)=32`, capped at 29 pods to leave 3 CPU for the system.
> HC-V at V=0.6: the 10 shards have CPU in [24, 47], each node takes
> `floor(CPU/24)=1` pod, and the cluster's total pod capacity stays equal to N.
> Run `generate-hetero-config.py --list` to see the generated tiers.

### 13.3 Trace analysis parameters (Board G, offline)

| Parameter | Value |
|---|---|
| Tables | `batch_task`, `machine_meta`, `container_meta` (Alibaba Cluster Trace v2018) |
| Pod-equivalent arrivals | `sum(instance_num)`, binned by `start_time` at second granularity |
| Arrival-rate window | 60 s (1 / 10 / 300 s also reported) |
| Thresholds | 100 / 250 / 500 / 1000 pods/s (250 is roughly the single-scheduler ceiling) |

### 13.4 Adjustable defaults

| Parameter | Default | Range | Note |
|---|---|---|---|
| N_nodes | 10,000 | 1,000 / 2,000 / 5,000 / 10,000 / 20,000 | |
| N_schedulers | 5 | 1 / 2 / 4 / 6 / 8 / 10 | |
| candidate_k (K) | Board A | 0 / 1 / 2 / 4 | E3/P4 use `K*`; baselines use 0 |
| strategy | Board A | QualityFirst / LatencyFirst / WeightedRandom | Event-driven: QF/WR only |
| penalty_weight (p) | Board A | 0.0 / 0.1 / 0.3 / 0.5 / 0.7 | QualityFirst and WeightedRandom only; baselines use 0 |
| strategy_seed | 42 | any int64 | WeightedRandom only |
| num_partitions (M) | 5 | 1 / 5 / 10 / 20 | |
| sync_period (G) | 1.0 s | 0.5 / 1.0 / 2.5 / 5.0 s | |
| dataplane_profile | Z0 | Z0 / Dreal / Dtail / Dreal-F1 | Board F only |
| dataplane_failure_rate | 0 | 0 / 0.01 | 1% is a sensitivity setting |

> K, strategy and p default to Board A's conclusions. Downstream groups that use
> them (E3 / P4 / Ab-E{1,2,3} / Ab-P{1,2,3}) do not start until Board A has
> produced `board-A-optima.yaml`.

### 13.5 Round-count estimate

| Board | Arithmetic | Rounds |
|---|---|---|
| A, event-driven sensitivity (S-E) | (4+2+5+1) x 3 | 36 |
| A, periodic sensitivity (S-P) | (4+3+5+1) x 3 | 39 |
| B1, low-contention scale-out | 3 scales x 3 groups x 3 | 27 |
| B2, high-contention scale-out | 4 scales x (3 E + 4 P) x 3 | 84 |
| B3, high-contention scheduler scale-out | 5 counts x (2 E + 2 P) x 3 | 60 |
| C, event-driven ablation | 4 combinations x 3 | 12 |
| C, periodic ablation | 4 combinations x 3 | 12 |
| D1-D3 | Reuses Board B/C data | 0 |
| E, real workload | 2 strategies x 3 profiles x 5 | 30 |
| F1, data-plane latency | 12 (event) + 20 (periodic) | 32 |
| F2, plus 1% startup failure | 6 (event) + 10 (periodic) | 16 |
| H1, synchronization freshness | 1 configuration x 3 | 3 |
