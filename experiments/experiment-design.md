# Cluster Experiment Design

This document describes how the ParKour cluster evaluation is structured: what
each experiment board asks, which baselines it compares, how metrics are defined
and collected, and which parameters are fixed versus swept.

It is a design document, not a results report. The published matrix, every cell
with the parameters its runs used, is [matrix.md](matrix.md), generated from
[registry.json](registry.json); where this document and the registry could
disagree the registry wins. [reconciliation.md](reconciliation.md) records where
this repository's figures and numbers differ from the camera-ready paper. Raw
measurement results are not distributed; the per-trial records behind the
paper's cluster figures and overhead table are, under [archive/](archive/). See
[README.md](README.md) for the operational instructions and script inventory.

## Contents

1. [Goals](#1-goals)
2. [Evaluation dimensions and metrics](#2-evaluation-dimensions-and-metrics)
3. [Cluster experiment flow](#3-cluster-experiment-flow)
4. [Baseline design](#4-baseline-design)
5. [Board A: parameter sensitivity](#5-board-a-parameter-sensitivity)
6. [Board B: end-to-end comparison](#6-board-b-end-to-end-comparison)
7. [Board C: ablation](#7-board-c-ablation)
8. [Board D: overhead](#8-board-d-overhead)
9. [Board E: application-layer check](#9-board-e-application-layer-check)
10. [Board F: data-plane latency injection](#10-board-f-data-plane-latency-injection)
11. [Board G: production trace profiling](#11-board-g-production-trace-profiling)
12. [Parameter matrix](#12-parameter-matrix)

---

## 1. Goals

Validate the two proposed mechanisms — **multicandidate fallback**, which hands
the binder a score-ordered list of K candidate nodes, and the **conflict-rate
penalty**, which lowers the score of nodes where binds recently failed, with
weight w — on a real Kubernetes cluster, under both event-driven and periodic
synchronization, and compare them systematically against the baselines of
section 4.

> Local state predictive update was removed from the design. It caused ghost
> resource deadlock in the Kubernetes implementation, and simulation showed its
> benefit to be negligible.

**Scoring strategy.** Every published cell uses `QualityFirst`, whose adjusted
score is `adjusted = (1-w)*normScore + w*(1-conflictRate)`. The scheduler also
implements `LatencyFirst`, `WeightedRandom` and partition-grain ParSync variants;
no published result uses them. `LatencyFirst` depends on per-partition
staleness, which is near-constant under event-driven synchronization and
therefore carries no signal there.

The boards answer these questions:

| # | Question | Board |
|---|---|---|
| 1 | How sensitive is the conflict rate to the candidate-list length K and the penalty weight w? | A |
| 2 | How much do the mechanisms improve throughput and the conflict rate over each baseline? | B |
| 3 | What does each mechanism contribute on its own, under each paradigm, and do they compose? | C |
| 4 | Is the additional latency and resource cost acceptable? | D |
| 5 | How does the effect change with cluster scale, scheduler count and contention? | B |
| 6 | Does scheduling quality show up in real application performance? | E |
| 7 | Do the conflict-rate and throughput gains survive an injected post-bind data-plane latency calibrated against real-node public measurements (and a small startup failure rate)? | F |
| 8 | Do a production trace's arrival intensity and request profile support the burst operating point, and do the two synthetic scenarios cover it? | G |

---

## 2. Evaluation Dimensions and Metrics

### 2.1 Mapping simulation dimensions to cluster metrics

| Simulation dimension | Simulation metric | Cluster metric | Source |
|---|---|---|---|
| **Scheduling speed** | finished_scheduling_time | Time until the last target pod completes Bind (T_bind_total) | Binder success counter + pod lifecycle watch |
| | throughput (tasks/s) | Control-plane binding throughput (Q_bind, pods/s) | CL2 saturation window / Binder success counter |
| | — | Per-pod end-to-end scheduling latency P50/P99 (T_e2e) | `scheduler_pod_scheduling_sli_duration_seconds` |
| | — | Scheduling algorithm latency P50/P99 (T_algo) | `scheduler_scheduling_algorithm_duration_seconds` |
| **Data plane** | — | Bind-to-Ready/Failed latency (T_data) | Pod lifecycle watch |
| | — | Create-to-Ready latency and Ready throughput (T_user, Q_ready) | Pod lifecycle watch / CL2 PodStartupLatency |
| | — | Data-plane startup failure rate and rebuild amplification | Pod lifecycle watch + Deployment status |
| **Conflict rate** | conflict_rate | All-candidates-failed rate (ACF): the share of scheduling attempts whose every candidate failed, forcing a full rescheduling cycle | `parasched_all_candidates_failed_total` |
| | — | Candidate-level bind conflict rate (BCR, R_conflict) | `parasched_bind_result_total{result="conflict"}` |
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
| P1 | CPU/memory usage, scheduling quality (score + balance) | Overhead (Board D) and quality |
| P1 | Real application performance (Board E) | Direct evidence for scheduling quality |
| P2 | Sync latency, state freshness, API latency | Supporting analysis |

The collector (`collect-metrics.sh`) records every metric of a phase when the
trial ends, including the quality histograms; nothing is re-queried from
Prometheus afterwards.

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
| E2 | Vanilla (event-driven) | N | ParKour's schedulers with both mechanisms disabled |
| E3 | ParKour (event-driven) | N | Multicandidate + penalty |
| — | Godel | N | The upstream Godel release; each scheduler owns a disjoint node partition of about 1/N of the cluster |

Periodic group:

| ID | Name | Schedulers | Description |
|---|---|---|---|
| P1 | Vanilla (periodic), globSync | N | All schedulers apply the whole cluster's snapshot every G |
| P2 | sameSync (ParSync) | N | All schedulers synchronize the same partition at the same time |
| P3 | diffSync (ParSync) | N | Schedulers stagger across different partitions |
| P4 | ParKour (periodic) | N | Multicandidate + penalty, on globSync |

Why each one is present:

- **E1** is the ideal reference line: a single scheduler has no conflict and the
  best placement quality, but its throughput is capped by one instance. It shows
  at what scale parallel scheduling starts to win.
- **E2** shows how severe conflict is under event-driven synchronization with no
  mechanism at all: `QualityFirst` with K=1 (no fallback) and w=0, which is
  plain score ordering.
- **E3** measures the mechanisms under near-real-time synchronization, with K=3
  (two fallbacks) and w=0.5.
- **Godel** is the published parallel scheduler that avoids conflict by
  partitioning the nodes statically; it runs as the baseline of boards B1, B2
  and B3.
- **P1** corresponds to the simulator's globSync baseline and to traditional
  shared-state designs such as Omega: `sync_pattern=glob`, one partition.
- **P2** and **P3** correspond to the ParSync paper's sameSync and diffSync
  modes, with multicandidate and penalty disabled.
- **P4** is the complete method under periodic synchronization. It runs on
  globSync: the partition rotation of sameSync and diffSync raises the average
  staleness, and globSync keeps partition-sync cost out of the comparison.

### 4.3 Configuration tables

Event-driven group:

| Setting | E1 | E2 | E3 |
|---|---|---|---|
| Schedulers | 1 | N | N |
| K (fallbacks) | 1 (0) | 1 (0) | **3 (2)** |
| strategy | QualityFirst | QualityFirst | QualityFirst |
| penalty weight w | 0 | 0 | **0.5** |
| sync period | 0.1 s (event-driven) | 0.1 s | 0.1 s |

Periodic group:

| Setting | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| Schedulers | N | N | N | N |
| K (fallbacks) | 1 (0) | 1 (0) | 1 (0) | **3 (2)** |
| strategy | QualityFirst | QualityFirst | QualityFirst | QualityFirst |
| penalty weight w | 0 | 0 | 0 | **0.5** |
| sync_pattern | glob | same | diff | glob |
| partitions | 1 | M | M | 1 |
| sync period | G | G | G | G |

Defaults: N = 10 schedulers, M = 10 partitions, G = 1.0 s. The runner records K
as its number of fallbacks, `num_backup` = K − 1; [matrix.md](matrix.md) shows
both.

---

## 5. Board A: Parameter Sensitivity

### 5.1 Objective

Show how the conflict rate responds to the two knobs, K and w, under both
paradigms, and so whether the mechanisms' gains depend on choosing them well.
The paper's robustness figure draws the two sweeps.

### 5.2 Sweeps

Each sweep varies one knob and holds the other, under event-driven
synchronization and each of the three periodic patterns (globSync, sameSync and
diffSync), so every setting has four arms:

1. **K sweep** (board `K`): `w = 0.3`, `K in {1, 2, 3, 5}` (`num_backup` 0, 1, 2
   and 4); 16 cells.
2. **w sweep** (board `P`): `K = 5`, `w in {0, 0.1, 0.3, 0.5, 0.7}`; 20 cells.

The published configuration, K=3 and w=0.5, sits where both sweeps have
flattened: most of the ACF reduction is already realized at K=3, and every
`w >= 0.1` lies within a narrow band.

### 5.3 Setup

10,000 nodes / 10,000 pods / 10 schedulers, the HC-V high-contention scenario at
V=0.6 (24 CPU and 192 Gi per pod, 1 pod/node, heterogeneous capacity), CL2
`cl2-saturation-only.yaml` with a 500 deployments/s burst, 3 trials per cell.
Periodic cells use G=1.0 s; globSync runs one partition, sameSync and diffSync
ten.

> **Why capacity variance V > 0.** With homogeneous shard capacity (V=0), the
> baseline ACF is only a few percent, which is not enough spread for the sweeps
> to produce a distinguishable signal. V=0.6 spreads the ten shards' CPU
> capacity over 24 to 44 CPU (eight distinct values) while holding the
> cluster's total pod capacity at N, which makes LeastAllocated scoring show a
> clear gradient even on an empty cluster and lifts the baseline conflict rate
> into a range where the mechanisms' effect is measurable. Among the piloted
> values {0, 0.3, 0.6, 1.0}, V=0.6 had the lowest coefficient of variation
> across trials and still sat below the saturation point at V=1.0, so every
> published cell uses it. `run-experiment.sh --variance 0` still runs the
> homogeneous control.

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
| Capacity variance (V) | 0.6 | **0.6** |
| Paper narrative | Ordinary microservice workload | Exclusive workload (ML training, HPC, large databases) |
| Simulation analogue | pod_per_node >> 8 (conflict near 0) | pod_per_node = 1 plus node score variance |

> **Rationale.** Simulation identifies `pod_per_node` as the dominant
> determinant of the conflict rate: it is high at one pod per node, drops sharply
> at two, and becomes negligible from four upward. HC-V therefore fixes M=1 and
> adds capacity heterogeneity on top: the CPU capacity of the 10 KWOK shards is
> spread over 24 to 44 CPU by a discretized log-normal, while the cluster's total
> pod capacity stays equal to N so the strict one-pod-per-node semantics is
> preserved. This is the cluster analogue of the simulator's
> `slot_score_variance`.
>
> **CL2 configuration.** HC-V uses the dedicated `cl2-saturation-only.yaml`,
> which contains only the saturation phase (at M=1 the nodes are already fully
> saturated, so no latency phase is needed) and bursts every pod in at 500
> deployments/s, far above a single scheduler's throughput, so the scheduling
> queue saturates immediately. Per-shard capacity is injected into CL2 through
> testoverrides generated by
> `experiments/scripts/generate-hetero-config.py --variance V`. The
> low-contention scenario uses `cl2-schedule-pods.yaml`, which has a latency
> phase after the saturation phase; only the saturation phase is measured.

### 6.3 Scenarios

All three run with 10 schedulers unless the scenario varies the count, 5 trials
per cell. The Godel baseline runs at the same points with 10 schedulers (board
`godel` in [matrix.md](matrix.md)).

#### B1: Low-contention scale-out

Show that (a) the single-scheduler throughput bottleneck grows with scale, and
(b) the mechanisms match vanilla throughput where conflict is rare. Figure:
`scalability-lowcontention`.

Vary the node count:

| Scale | Nodes | Pods (29/node) |
|---|---|---|
| S | 1,000 | 29,000 |
| M | 2,000 | 58,000 |
| L | 5,000 | 145,000 |

> 10,000 nodes (290,000 pods) exceeds what a single master API server sustains
> here: the API writes generated during E1 cleanup cause API server timeouts in
> the following experiment. It is therefore excluded from the low-contention
> scenario. High contention has pods = nodes and is not affected.

Groups: E1, E2, E3, event-driven only. Report throughput and total scheduling
time; conflict rate is a reference only, as it is expected to be near zero.
Periodic synchronization is not tested here, because with almost no conflict the
paradigms are indistinguishable and the point of the scenario is the chain "a
single scheduler is not enough -> parallelism is needed -> the mechanisms do not
cost throughput".

#### B2: High-contention scale-out

Show that (a) vanilla parallel scheduling conflicts heavily, and (b) the
mechanisms cut the conflict rate and raise useful throughput. Figure:
`pareto-all-scales`; the 20,000-node runs also feed `occupancy-intervals-1col`,
and the overhead table (Board D) reads the runs at 10,000 and 20,000 nodes.

Vary the node count:

| Scale | Nodes | Pods (1/node) |
|---|---|---|
| M | 2,000 | 2,000 |
| L | 5,000 | 5,000 |
| XL | 10,000 | 10,000 |
| XXL | 20,000 | 20,000 |

Groups: E1, E2, E3 and P1, P2, P3, P4. Report throughput, conflict rate, total
conflict count and total scheduling time.

#### B3: High-contention scheduler scale-out

Show that the conflict rate worsens as scheduler instances are added, and that
the mechanisms stay robust under higher concurrency. Figure:
`scalability-schedulers`.

Fix 10,000 nodes / 10,000 pods (HC-V, V=0.6) and vary the scheduler count over
{2, 4, 6, 8, 10}.

Groups: E2, E3 (event-driven) and P1, P4 (periodic globSync). E1 is omitted
because its throughput and conflict rate do not vary with the scheduler count
and B1 already provides the reference.

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
- The mechanism settings are the published ones, K=3 and w=0.5; Board A shows
  how the result responds to each.

### 7.2 Combinations

Event-driven (Group Ab-E), based on E2, all with `strategy=QualityFirst` so the
+M / +P / +MP semantics stay clean:

| ID | Configuration | K (fallbacks) | w |
|---|---|---|---|
| Ab-E0 | E2 (baseline) | 1 (0) | 0 |
| Ab-E1 | +M | 3 (2) | 0 |
| Ab-E2 | +P | 1 (0) | 0.5 |
| Ab-E3 | +MP (E3) | 3 (2) | 0.5 |

Periodic (Group Ab-P), based on P1 (globSync), also at `strategy=QualityFirst`:

| ID | Configuration | K (fallbacks) | w |
|---|---|---|---|
| Ab-P0 | P1 (baseline) | 1 (0) | 0 |
| Ab-P1 | +M | 3 (2) | 0 |
| Ab-P2 | +P | 1 (0) | 0.5 |
| Ab-P3 | +MP (P4) | 3 (2) | 0.5 |

Figures: `ablation-quality-a` (ACF, candidate-level bind conflict rate and
throughput) and `ablation-quality-bc` (placement quality, from the quality
histograms the collector records).

### 7.3 Setup

10,000 nodes / 10,000 pods / 10 schedulers, HC-V at V=0.6. Each cell runs 5
trials. Periodic cells use G=1.0 s and one partition.

---

## 8. Board D: Overhead

### 8.1 Objective

Quantify the extra latency and resource cost of multicandidate and penalty, and
show it is small.

The board has **no runs of its own**. The collector records every metric below
in each saturation phase, and the paper's overhead table reads them from the B2
runs at 10,000 nodes: Vanilla and ParKour under each paradigm, and diffSync. Its
text also compares the periodic arms' algorithm latency at 20,000 nodes. The
same metrics exist for every other run.

### 8.2 D1: Scheduler resource cost

- CPU: `rate(container_cpu_usage_seconds_total{container="scheduler", namespace="para-system"}[1m])`
- Memory: `container_memory_rss{container="scheduler", namespace="para-system"}`

Both are range queries over the saturation window; a trial's value is each
scheduler pod's last sample in the window, summed over the pods. CPU efficiency
is throughput over scheduler CPU, in scheduled pods per second per core, which
accounts for a configuration that places more pods by doing more work.

### 8.3 D2: Per-pod scheduling cost

| Stage | Metric | Reported |
|---|---|---|
| Algorithm time | `scheduler_scheduling_algorithm_duration_seconds` | P99 |
| End to end | `scheduler_pod_scheduling_sli_duration_seconds` | P99 |
| Candidate selection | `scheduler_parasched_candidate_selection_duration_seconds` | diagnostic |

Percentiles come from histogram snapshots: the bucket counters are read at the
start of the saturation window and after its end, and the P99 is interpolated
from the difference, so the value does not depend on the scrape interval.

### 8.4 D3: Dispatcher and Binder cost

The binder's and the dispatcher's CPU are read as the schedulers' are, and the
text reports their range across the five configurations. The collector also
records diagnostics that explain a cost but are not reported:

| Component | Metric | PromQL |
|---|---|---|
| Dispatcher | Dispatch throughput | `rate(parasched_dispatch_total{result="success"}[1m])` |
| | Dispatch latency P50/P99 | `histogram_quantile(0.99, rate(parasched_dispatch_duration_seconds_bucket[5m]))` |
| | Queue depth | `parasched_dispatcher_queue_depth` |
| Binder | Bind latency P50/P99 | `histogram_quantile(0.99, rate(parasched_bind_duration_seconds_bucket[5m]))` |
| | Candidate rank distribution | `parasched_candidate_rank_accepted` |
| | All-candidates-failed rate | `rate(parasched_all_candidates_failed_total[1m])` |

What to watch for:

- Dispatch latency should stay under 1 ms (it is only an annotation patch); a
  P99 above 5 ms indicates a bottleneck.
- Bind latency should grow linearly in K (at most one extra API call per
  additional candidate).
- Queue depth should approach zero in steady state; sustained growth means too
  few Dispatcher workers.

---

## 9. Board E: Application-Layer Check

### 9.1 Objective

Deploy real applications (nginx, redis, mysql) on physical worker nodes and
measure scheduling quality through application-level performance. KWOK nodes
cannot run real containers, so this is the one thing the emulated experiments
cannot provide.

It is a supplementary check on scheduling quality. The main evaluation remains
the large-scale KWOK experiments, and the paper's discussion reports this board
as evidence against a gross regression, not as a measurement of benefit.

### 9.2 Matrix

4 methods x 3 stress profiles x 3 trials = 36 trials, with 5 scheduler
instances on three workers. Each trial deploys 18 nginx, 18 redis and 1 mysql
replicas and runs the three benchmarks concurrently for 180 s.

| Method | Paradigm | K (fallbacks) | w | Sync |
|---|---|---|---|---|
| E2 | event-driven | 1 (0) | 0 | 0.1 s |
| E3 | event-driven | 3 (2) | 0.5 | 0.1 s |
| P1 | periodic | 1 (0) | 0 | globSync, 1.0 s, one partition |
| P4 | periodic | 3 (2) | 0.5 | globSync, 1.0 s, one partition |

The stress profiles pin stress pods to the workers by `nodeName`, bypassing the
schedulers, so that the workers' free CPU differs and placement matters: `none`
places none, `mild` 0, 1 and 2 stress pods on the three workers, and `heavy` 0,
2 and 4.

### 9.3 Procedure

One-time prerequisite: every workload image must be pulled and imported into the
workers' containerd, with the workload YAML using `imagePullPolicy: Never`, so
that registry pull latency and rate limits do not pollute the measurement.

```bash
# Run once on a node with Docker and external network access
./experiments/scripts/preload-workload-images.sh
# Covers polinux/stress-ng, nginx:1.27-alpine, redis:7-alpine, mysql:8.0
```

`run-supplementary.sh` runs the matrix: it first deletes any KWOK nodes, whose
presence in the schedulers' caches would add their Filter and Score work to
every workload pod, and then runs the profiles in turn (none, mild, heavy), each
with the four methods, through `run-workload-bench.sh`. Every trial is a full
cycle, so each gives an independent placement sample:

1. Clean up the workload namespace on the workers.
2. Configure the schedulers for the method.
3. Deploy the stress pods for the profile, bound directly by `nodeName`.
4. Deploy the nginx / redis / mysql Deployments, scheduled by para-scheduler.
5. Record `placement-quality.json`, the placement-layer snapshot.
6. Run the three benchmarks concurrently for 180 s, collecting wrk /
   redis-benchmark / sysbench output plus Prometheus node resource series.
7. Cool down before the next trial.

The results are not archived; the numbers the paper quotes come from the
recorded runs.

### 9.4 Validity boundaries

- Only three physical workers, far smaller than the KWOK experiments. The
  placement space is too small to separate the scheduler's effect from placement
  chance; this board checks the mapping from scheduling quality to application
  performance, and does not carry the large-scale evaluation narrative.
- Stress pods are bound by `nodeName` and bypass para-scheduler, so this board
  does not test multicandidate under high-conflict load (B2/B3 do).
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
fixed random seed. The `Z0` result should agree with the B2-10000n data within
10% on throughput, which serves as an environment-drift check.

Method configurations match the Board B production configuration item by item
and must not be chosen independently:

| Method | Paper name | Paradigm | partitions | sync_pattern | sync_period | K (fallbacks) | w |
|---|---|---|---:|---|---:|---:|---:|
| E2 | vanilla-E | event-driven | 1 | `diff` | 0.1 s | 1 (0) | 0 |
| E3 | ParKour-E | event-driven | 1 | `diff` | 0.1 s | **3 (2)** | **0.5** |
| P1 | vanilla-P | periodic globSync | 1 | `glob` | 1.0 s | 1 (0) | 0 |
| P4 | ParKour-P | periodic globSync | 1 | `glob` | 1.0 s | **3 (2)** | **0.5** |

Three constraints:

- **Periodic uses globSync only.** The main results already establish globSync as
  the better synchronization mode; the partition rotation of sameSync/diffSync
  raises average staleness. Board F's independent variable is data-plane latency,
  and globSync keeps partition-sync cost out of it.
- **K=3 and w=0.5 are the published configuration.** Board F must reuse them, or
  the paper would present two different values of K.
- **Event-driven and periodic partition settings are configured
  independently.** Both happen to be partitions=1, but from different sources,
  and the scripts must not share one variable for them.

Matrix: 3 data-plane scenarios x 2 paradigms x 2 methods, with the repetition
count differing by paradigm. Every sub-experiment runs through
`run-module-f.sh --experiment <name>`, dry-run unless given `--execute`.

| Sub-experiment | Paradigm | Methods | Profiles | Trials | Rounds |
|---|---|---|---|---:|---:|
| F1-E | event-driven | E2, E3 | `Z0`, `Dreal` | 3 | 12 |
| F1-P | periodic globSync | P1, P4 | `Z0`, `Dreal` | **5** | 20 |
| F2-E | event-driven | E2, E3 | `Dreal-F1` | 3 | 6 |
| F2-P | periodic globSync | P1, P4 | `Dreal-F1` | **5** | 10 |
| **Total** | | | | | **48** |

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
| | `partition_staleness` | Age of the partition snapshot at installation; diagnostic only |
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

The figure reads both primary metrics at the T99 completion cut: `Q_bind` as
`9900 / T99` from the completion curve, and `ACF` from the binder's counters at
1 s resolution. The round summaries hold only the whole-round ACF, so
`pull-windowed-acf.py` rebuilds the T99 value right after the campaign, while
Prometheus still holds those counters.

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

Data: `batch_task.csv` from Alibaba Cluster Trace v2018 (765 MiB, 14,295,731
task rows over about 8.8 days). A `batch_task` row is a task group, so
pod-equivalent arrivals expand by instance: `pod_arrivals(t) =
sum(instance_num(task))` at `task.start_time = t`. Rows whose fields do not
parse, or which fall outside the trace's own reporting window, are skipped
(24,089 of them).

Arrivals are binned to the second and aggregated into windows of 1, 10, 60 and
300 s. The **active period** trims the trace's quiet head and tail: leading and
trailing stretches of 60 s windows below 10% of the trace's mean rate that last
at least an hour. Idle seconds inside the active period stay in every statistic.
The reference rate is 200 pods/s, roughly the single-scheduler throughput the
Godel paper reports.

### 11.3 Results

`trace/alibaba2018/export.py` computes, from the raw CSV:

| Quantity | Value |
|---|---:|
| Active period | 171.6 h of the trace's 211.8 h |
| Mean arrival rate over the active period | 2,271 pods/s |
| 1 s windows above 200 pods/s | 66.1% |
| 60 s windows above 200 pods/s | 97.9% |
| 60 s window rate P50 / P90 / P99 | 1,824 / 4,047 / 10,474 pods/s |
| Representative 60 s window (seconds 627,120-627,179), mean rate | 1,514 pods/s |

**Conclusion 1.** While there is load, nearly every minute's arrival rate exceeds
a single scheduler's throughput. Board B's saturation burst therefore targets a
common production operating point, not an inflated extreme.

**Conclusion 2.** Production pods are much smaller than nodes, so a real node
holds many pods — the simulator's large M and the cluster's low-contention B1.
HC-V's 1 pod/node is a deliberately constructed contention upper bound. The two
synthetic scenarios together cover the production operating point. Cluster
results under mixed request shapes and gang structure are not measured here and
are stated as a limitation.

### 11.4 Artifacts

`experiments/trace/alibaba2018/export.py` reduces the trace for the arrival-rate
figure directly from the raw CSV with no cluster dependency, and prints the
statistics above. The script's docstring gives the download URL for the trace,
which is not redistributed here; the registry pins its SHA-256.

### 11.5 Validity boundaries

- `start_time` is a task's start / runnable time, not necessarily user
  submission time; the `instance_num` expansion assumes a task's instances become
  schedulable simultaneously.
- The absolute peak windows are dominated by a few very large tasks, so the
  profile uses a representative window rather than describing the peak as
  typical.
- This board produces no throughput or conflict-rate result, so "no regression
  under real mixed requests" remains unverified.

---

## 12. Parameter Matrix

[matrix.md](matrix.md) lists every published cell with its parameters, its trial
count and the figures it feeds; this section states what those cells share and
what the runner can vary beyond them.

### 12.1 Fixed parameters

| Parameter | Value |
|---|---|
| KWOK node capacity | Ten shards at the V=0.6 tiers of `generate-hetero-config.py`: 44, 40, 36, 32, 32, 30, 28, 28, 26 and 24 CPU, 8 GiB per CPU, 110 pods |
| Strategy | `QualityFirst`, seed 42 |
| Repetitions | 5 trials on boards B and C and for Godel; 3 on board A; board F 3 event-driven and 5 periodic rounds per cell; board E 3 |

### 12.2 Load scenario parameters

| Parameter | Low contention | High contention (HC-V) | Note |
|---|---|---|---|
| Pod CPU request | 1000m | 24000m | Ordinary vs. exclusive |
| Pod memory request | 8Gi | 192Gi | Ordinary vs. exclusive |
| PODS_PER_NODE (M) | 29 | **1** | Node capacity determines conflict probability |
| Capacity variance (V) | 0.6 | **0.6** | Per-shard CPU from 24 to 44, sum fixed at 320 |
| CL2 config | cl2-schedule-pods.yaml | **cl2-saturation-only.yaml** | HC-V has no latency phase |
| CL2 saturation creation rate | 5 deployments/s | **500 deployments/s (burst)** | Burst far above single-scheduler throughput |
| CL2 latency creation rate | 50 deployments/s | — | HC-V has no latency phase |

> **Node capacity arithmetic.**
> The ten shards' CPU always sums to 320, so the cluster's CPU is 32 per node on
> average whatever V is, and memory follows at 8 GiB per CPU.
> Low contention: 1-CPU / 8 GiB pods fit 32 per node on average; the workload
> places 29 per node, so the cluster never fills.
> HC-V: every shard has between 24 and 47 CPU, so each node takes exactly
> `floor(CPU/24)=1` pod, and the cluster's pod capacity equals N.
> Run `generate-hetero-config.py --list` to see the tiers at each V.

### 12.3 Trace analysis parameters (Board G, offline)

| Parameter | Value |
|---|---|
| Table | `batch_task` (Alibaba Cluster Trace v2018) |
| Pod-equivalent arrivals | `sum(instance_num)`, binned by `start_time` at second granularity |
| Windows | 1, 10, 60 and 300 s; the figure draws 1 and 60 s |
| Active period | Trim leading and trailing 60 s windows below 10% of the mean rate, in stretches of at least 1 h |
| Reference rate | 200 pods/s, roughly the single-scheduler throughput |

### 12.4 Runner parameters

| Parameter | Published values | Runner range | Note |
|---|---|---|---|
| N_nodes | 1,000-20,000 | any | 10,000 unless a board varies it |
| N_schedulers | 10; 2-10 on B3; 5 on board E | 1 and up | |
| K (`num_backup` + 1) | 3; 1, 2, 3, 5 on the K sweep | 1 and up | Baselines use 1 |
| strategy | QualityFirst | QualityFirst / LatencyFirst / WeightedRandom / ParSync variants | |
| penalty weight w | 0.5; 0.3 on the K sweep; 0-0.7 on the w sweep | [0, 1] | Baselines use 0 |
| strategy_seed | 42 | any int64 | WeightedRandom only |
| partitions (M) | 1 (event-driven, globSync); 10 (sameSync, diffSync) | 1 and up | globSync always runs one |
| sync period (G) | 0.1 s event-driven; 1.0 s periodic | seconds | Below 0.5 s means event-driven |
| dataplane_profile | Z0, Dreal, Dreal-F1 | Z0 / Dreal / Dtail / Dreal-F1 | Board F only |
| dataplane_failure_rate | 0, 0.01 | [0, 1) | 1% is a sensitivity setting |

The run counts per board are in the Boards table of [matrix.md](matrix.md);
board E adds 36 trials.
