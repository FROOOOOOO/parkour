# Cluster Experiments

This directory contains scripts and configuration for running ParKour experiments on a Kubernetes cluster simulated with KWOK.

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
├── scripts/
│   ├── run-experiment.sh        Main experiment runner
│   ├── batch-run.sh             Batch runner for multiple configurations
│   ├── run-sensitivity.sh       Parameter sensitivity sweep
│   ├── run-supplementary.sh     Supplementary experiments
│   ├── create-nodes.sh          Create KWOK virtual nodes
│   ├── collect-metrics.sh       Fetch Prometheus metrics after a run
│   ├── generate-hetero-config.py  Generate heterogeneous node capacity configs
│   ├── plot-fig-*.py            Figure generation from cluster results
│   └── process-results.py       Aggregate and summarize result JSON files
│
├── kwok-setup/
│   ├── kwok-node.yaml           KWOK virtual node template
│   ├── kwok-deployment.yaml     KWOK shard deployment
│   ├── kwok-config.yaml         KWOK behavior configuration
│   ├── cl2-schedule-pods.yaml   ClusterLoader2 pod scheduling workload
│   └── systemd/                 KWOK systemd service files (kwok0–kwok9)
│
└── manifests/
    ├── control-plane/           etcd, kube-apiserver, controller, scheduler configs
    └── monitoring/              Prometheus, Grafana, kube-state-metrics configs
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

```bash
bash experiments/scripts/batch-run.sh
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

Generate figures from aggregated results:

```bash
python experiments/scripts/plot-fig-scalability-3panel.py
python experiments/scripts/plot-fig-ablation.py
```