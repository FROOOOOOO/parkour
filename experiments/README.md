# Cluster Experiments

This directory contains scripts and configuration for running ParKour experiments on a Kubernetes cluster simulated with KWOK.

[experiment-design.md](experiment-design.md) describes what each experiment board asks, which baselines it compares, and how the metrics are defined. This file covers how to run them.

No measurement results are distributed with this repository. `experiments/results/` is generated locally and is excluded from version control.

## Prerequisites

| Component | Notes |
|---|---|
| Kubernetes 1.33 cluster | Master + worker nodes; kubeadm recommended |
| KWOK 0.7+ | For creating virtual nodes; deployed as systemd services |
| ClusterLoader2 | Built from `perf-tests/`; placed at `bin/clusterloader` |
| Prometheus + Pushgateway | For metric collection; set `PROMETHEUS_URL` env var |
| kubectl | Configured to reach the cluster |

## Directory Structure

```
experiments/
├── experiment-design.md         What each board measures, and why
├── board-A-optima.yaml          Parameter sweep conclusions (you fill these in)
│
├── scripts/
│   │  Running experiments
│   ├── run-experiment.sh            Main experiment runner (one configuration, N trials)
│   ├── batch-run.sh                 Batch runner, reads board-A-optima.yaml
│   ├── run-sensitivity.sh           Board A parameter sensitivity sweep
│   ├── run-supplementary.sh         Supplementary experiments
│   ├── run-module-f.sh              Board F data-plane latency injection matrix
│   ├── run-module-f{1-p,2-e,2-p}.sh Board F sub-experiment entry points
│   ├── run-board-h.sh               Board H synchronization-freshness measurement
│   ├── run-workload-bench.sh        Board E real-application benchmark
│   ├── run-supplementary-workload-bench.sh  Board E supplementary runs
│   ├── run-godel-baseline.sh        Godel baseline runner
│   ├── batch-run-godel.sh           Godel batch runner
│   │
│   │  Cluster and workload setup
│   ├── create-nodes.sh              Create KWOK virtual nodes
│   ├── schedule-pods.sh             Schedule pods via CL2 (standalone, for debugging)
│   ├── cleanup-cluster.sh           Restore a clean state after an aborted run
│   ├── etcd-maintenance.sh          etcd defragmentation and compaction
│   ├── preload-workload-images.sh   Preload Board E workload images onto workers
│   ├── generate-hetero-config.py    Heterogeneous node capacity testoverrides
│   ├── calibrate-dataplane.py       Derive the Board F latency profile from CI anchors
│   ├── injection-fidelity-gate.py   Board F injection fidelity gate (must PASS)
│   │
│   │  Collection and analysis
│   ├── collect-metrics.sh           Fetch Prometheus metrics after a run
│   ├── collect-metrics-godel.sh     Same, for the Godel baseline
│   ├── process-results.py           Aggregate result JSON into CSV/JSON summaries
│   ├── apply-outlier-filter.py      Outlier filtering and summary tables
│   ├── pull-quality-metrics.py      Scheduling-quality metrics extraction
│   ├── analyze-temporal-occupancy.py  Bucket results by workload-fill occupancy
│   └── plot-*.py                    Paper figure generation (see below)
│
├── kwok-setup/
│   ├── kwok-node.yaml               KWOK virtual node template
│   ├── kwok-deployment.yaml         KWOK shard deployment
│   ├── kwok-config.yaml             KWOK behavior configuration
│   ├── cl2-schedule-pods.yaml       ClusterLoader2 pod scheduling workload
│   ├── cl2-saturation-only.yaml     CL2 saturation-only workload (high contention)
│   ├── systemd/                     KWOK systemd service files (kwok0-kwok9)
│   └── stages/calibration/          Board F latency calibration: public CI snapshot
│
├── trace/alibaba2018/
│   └── plot_arrival_figure.py       Board G arrival-rate figure from the raw trace
│
└── manifests/
    ├── control-plane/               etcd, kube-apiserver, controller, scheduler configs
    └── monitoring/                  Prometheus, Grafana, kube-state-metrics configs
```

## Cluster Setup

### 1. Deploy KWOK Virtual Nodes

KWOK runs as a set of systemd services (one per shard). Install the services from `kwok-setup/systemd/`, then start them:

```bash
# Copy service files
sudo cp experiments/kwok-setup/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start KWOK shards
sudo systemctl start kwok kwok{1..9}
```

Apply the KWOK node template to create virtual nodes:

```bash
bash experiments/scripts/create-nodes.sh --nodes 10000
```

### 2. Build and Deploy ParKour Components

```bash
# Build Dispatcher and Binder
cd para-scheduler && go build ./cmd/dispatcher ./cmd/binder

# Build k8s-scheduler (on Linux or via cross-compilation)
cd k8s-scheduler && make build

# Build container images
bash experiments/build-images.sh

# Deploy CRDs and components
kubectl apply -f para-sched-api/config/
kubectl apply -f para-scheduler/deploy/lab-cluster/
```

### 3. Build ClusterLoader2

```bash
cd perf-tests/clusterloader2
go build -o ../../bin/clusterloader ./cmd/clusterloader
```

### 4. Deploy Monitoring

```bash
bash experiments/manifests/monitoring/deploy-prometheus.sh
bash experiments/manifests/monitoring/deploy-grafana.sh
```

Set your Prometheus Pushgateway URL:

```bash
export PROMETHEUS_URL=http://<your-prometheus-host>:9091
```

---

## Running Experiments

### Single Experiment

```bash
bash experiments/scripts/run-experiment.sh \
  --name MyExperiment \
  --nodes 10000 \
  --schedulers 10 \
  --backup 2 \
  --strategy QualityFirst \
  --penalty 0.3 \
  --sync-period 1.0 \
  --partitions 10 \
  --sync-pattern diff \
  --trials 3
```

### Example Configurations

```bash
# E1: Event-driven baseline (no optimization)
bash experiments/scripts/run-experiment.sh \
  --name E1-baseline --nodes 10000 --schedulers 10 \
  --backup 0 --penalty 0 --sync-period 0.1 --trials 3

# E3: Event-driven + multicandidate + penalty
bash experiments/scripts/run-experiment.sh \
  --name E3-proposed --nodes 10000 --schedulers 10 \
  --backup 2 --strategy QualityFirst --penalty 0.3 \
  --sync-period 0.1 --trials 3

# P1: ParSync glob sync (all-partition global broadcast)
bash experiments/scripts/run-experiment.sh \
  --name P1-glob --nodes 10000 --schedulers 10 \
  --backup 2 --strategy QualityFirst --penalty 0.3 \
  --sync-period 1.0 --partitions 10 --sync-pattern glob --trials 3

# P4: ParSync diff sync + LatencyFirst
bash experiments/scripts/run-experiment.sh \
  --name P4-latency --nodes 10000 --schedulers 10 \
  --backup 2 --strategy LatencyFirst \
  --sync-period 1.0 --partitions 10 --sync-pattern diff --trials 3
```

### Batch Run

`batch-run.sh` reads the E3 and P4 parameters from [board-A-optima.yaml](board-A-optima.yaml), which ships with `__TBD__` placeholders. Run the Board A sweep first, write the conclusions into that file, then:

```bash
bash experiments/scripts/batch-run.sh --dry-run   # preview the round list
bash experiments/scripts/batch-run.sh
```

### Board A: Parameter Sensitivity

The sweep is greedy and ordered: K, then strategy, then the penalty weight. Each step takes the previous step's optimum.

```bash
# Step 1: K sweep (--paradigm defaults to "both"; use event or periodic to split)
bash experiments/scripts/run-sensitivity.sh --step K

# Step 2: strategy sweep, passing the chosen K
bash experiments/scripts/run-sensitivity.sh --step strategy --k-star <K>

# Step 3: penalty-weight sweep
bash experiments/scripts/run-sensitivity.sh --step p --k-star <K> --strategy-star <STRATEGY>

# Step 4 (only if strategy* is not QualityFirst): confirmation round
bash experiments/scripts/run-sensitivity.sh --step confirm \
  --k-star <K> --strategy-star <STRATEGY> --p-star <P>
```

`--step` takes `K`, `strategy`, `p` or `confirm`; `--paradigm` takes `event`, `periodic` or `both`. Add `--dry-run` to preview the round list.

Write the resulting `K*` / `strategy*` / `p*` for each paradigm into `board-A-optima.yaml`.

### Board F: Data-Plane Latency Injection

Board F injects a post-bind startup latency distribution derived from public Kubernetes scalability-CI measurements. Derive the profile and clear the fidelity gate before running the matrix:

```bash
# 1. Derive the four-bucket profile from the three CI percentile anchors (in ms).
#    The Dreal anchors are in kwok-setup/stages/calibration/README.md; this step
#    creates no Kubernetes resources.
python experiments/scripts/calibrate-dataplane.py \
  --from-anchors 830,1109,2384 \
  --anchor-source 'perf-dash.k8s.io snapshot 2026-09-05, last 20 builds' \
  --output-dir <profile-dir>

# 2. Verify that the injector reproduces the configured distribution.
#    The report must say PASS before the matrix will run.
python experiments/scripts/injection-fidelity-gate.py \
  --profile <profile.json> --pods 10000 --shards 10 --output-dir <gate-dir>

# 3. Run a sub-experiment. Every entry point is dry-run by default.
bash experiments/scripts/run-module-f.sh --experiment F1-E            # preview
bash experiments/scripts/run-module-f.sh --experiment F1-E --execute \
  --profile-json <profile.json> --gate-report <gate-report.json>
bash experiments/scripts/run-module-f1-p.sh --execute
bash experiments/scripts/run-module-f2-e.sh --execute
bash experiments/scripts/run-module-f2-p.sh --execute
```

`run-module-f.sh --self-test` generates the temporary Stage, runner and watcher and syntax-checks them without contacting the cluster.

### Board H: Synchronization-Channel Freshness

Board H needs an instrumented scheduler image and must run after the boards it depends on have finished. It is dry-run by default and preflights the Stage environment, the image and the component readiness.

```bash
bash experiments/scripts/run-board-h.sh              # preview
bash experiments/scripts/run-board-h.sh --execute    # run 3 rounds
```

### Board E: Real-Workload Benchmark

```bash
# Once, on a node with Docker and external network access
bash experiments/scripts/preload-workload-images.sh

bash experiments/scripts/run-workload-bench.sh
```

---

## All run-experiment.sh Flags

### Scheduling Method

| Flag | Default | Description |
|---|---|---|
| `--backup NUM` | `2` | Backup candidates B; K = B+1 (0 = disable multicandidate) |
| `--strategy STR` | `QualityFirst` | `QualityFirst`, `LatencyFirst`, `WeightedRandom`, `QualityFirstParSync`, `LatencyFirstParSync` |
| `--penalty FLOAT` | `0.3` | Penalty weight p ∈ [0, 1] (0 = disable) |
| `--strategy-seed INT` | `42` | PRNG seed for WeightedRandom / ParSync strategies |

### Sync Mode

| Flag | Default | Description |
|---|---|---|
| `--sync-period FLOAT` | `1.0` | Full sync period G in seconds; <0.5 → event mode |
| `--partitions NUM` | `10` | Number of partitions M |
| `--sync-pattern STR` | `diff` | `glob` (P1), `same` (P2), `diff` (P3/P4) |

**Sync mode auto-detection**: `sync-period >= 0.5` → ParSync periodic mode; `sync-period < 0.5` → event-driven mode.

### Cluster and Workload

| Flag | Default | Description |
|---|---|---|
| `--nodes NUM` | `10000` | Number of KWOK virtual nodes |
| `--schedulers NUM` | `10` | Number of scheduler instances |
| `--pods-per-node NUM` | `29` | Saturation workload: pods per node |
| `--cpu-request STR` | `1000m` | Pod CPU request |
| `--memory-request STR` | `8Gi` | Pod memory request |
| `--variance V` | `0` | Node capacity variance: `0` (homogeneous) or `0.3`, `0.6`, `1.0` |
| `--trials NUM` | `1` | Number of independent trials |

### Infrastructure

| Flag | Default | Description |
|---|---|---|
| `--prometheus-url URL` | `$PROMETHEUS_URL` | Prometheus Pushgateway URL |
| `--collect-logs` | off | Save per-trial scheduler/binder/dispatcher logs |
| `--preserve-nodes` | off | Skip node create/delete steps between runs |

---

## Results

Experiment results are saved to `experiments/results/<name>_<timestamp>/`. Each trial produces:

```
<name>_<timestamp>/
├── config.json          Experiment parameters
└── trial-1/
    ├── timing.json      Wall-clock timing per phase
    ├── cl2.log          ClusterLoader2 log
    ├── junit.xml        CL2 JUnit summary
    └── metrics-saturation/
        ├── algo_latency_p50.json
        ├── algo_latency_p99.json
        └── ...
```

Aggregate results with:

```bash
python experiments/scripts/process-results.py experiments/results/
```

Optional post-processing:

```bash
# Outlier filtering and summary tables (reads experiments/results/ directly)
python experiments/scripts/apply-outlier-filter.py --stdout

# Bucket a run's conflicts and throughput by workload-fill occupancy
python experiments/scripts/analyze-temporal-occupancy.py \
  --results experiments/results/ --json-out <occupancy.json>
```

---

## Figures

Each script below produces one figure of the paper. They are the only plotting
scripts kept in this repository; the figures themselves are not distributed.
Output goes to `paper/figs/`, which is created on demand.

| Script | Figure | Input |
|---|---|---|
| `plot-fig-scalability-3panel.py` | `scalability-lowcontention`, `scalability-schedulers` | Boards B1, B2, B3 plus the Godel baseline |
| `plot-pareto.py` | `pareto-all-scales` | Board B2 summary plus the Godel baseline |
| `plot-fig-ablation-quality.py` | `ablation-quality-a`, `ablation-quality-bc` | Board C ablation results |
| `plot-fig-robustness.py` | `robustness` | Board A K and p sweeps |
| `plot-fig-dataplane-sensitivity.py` | `dataplane-sensitivity` | Board F matrices |
| `plot-fig-occupancy-intervals.py` | `occupancy-intervals-1col` | `analyze-temporal-occupancy.py` output |
| `trace/alibaba2018/plot_arrival_figure.py` | `trace-arrival-rate` | The raw Alibaba trace (see below) |

```bash
python experiments/scripts/plot-fig-scalability-3panel.py
python experiments/scripts/plot-pareto.py
python experiments/scripts/plot-fig-ablation-quality.py
python experiments/scripts/plot-fig-robustness.py
python experiments/scripts/plot-fig-dataplane-sensitivity.py
python experiments/scripts/plot-fig-occupancy-intervals.py --data <occupancy.json>
```

Several of these scripts carry the aggregate values reported in the paper as
module-level constants, so they reproduce the published figures without a
cluster. The scripts that read `experiments/results/` need you to run the
corresponding board first.

The simulation figures are produced separately; see [simulation/](../simulation).

### Board G: Trace Arrival Profile

`trace/alibaba2018/plot_arrival_figure.py` computes the arrival-rate figure
directly from the Alibaba Cluster Trace v2018, which is not redistributed here.
The script's docstring gives the download command. On its first run it parses
`batch_task.csv` into a cached series and reuses that cache afterwards.

```bash
python experiments/trace/alibaba2018/plot_arrival_figure.py --data-dir <trace-dir>
```