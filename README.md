# ParKour: Conflict-Aware Parallel Scheduling for Large-Scale Kubernetes Clusters

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

ParKour is a research prototype that improves scheduling throughput and reduces resource-conflict rates in large-scale Kubernetes clusters running multiple scheduler instances in parallel. It combines three mechanisms:

1. **Multicandidate Selection** — each scheduling decision produces a ranked list of K candidate nodes. On a bind failure, the Binder retries the next candidate without re-invoking the scheduler.
2. **Conflict-Rate Penalty** — each node's score is adjusted by its observed historical conflict rate, discouraging schedulers from converging on the same nodes.
3. **ParSync Partition Synchronization** — a time-driven, rotational sync strategy (from [ATC'21](https://www.usenix.org/conference/atc21/presentation/feng-yihui)) that divides nodes into M partitions and refreshes each partition on a bounded-staleness schedule.

---

## Architecture

```mermaid
flowchart TD
    lib["scheduler-lib\nmulticandidate · stats · parsync · types"]

    subgraph ps["para-scheduler"]
        disp["Dispatcher\nassigns pods to schedulers\nmanages partitions (ParSync)"]
        binder["Binder\nconflict check · pod binding\nsnapshot publisher (ParSync)"]
    end

    subgraph ks["k8s-scheduler — K8s v1.33 fork"]
        sched["Filter → Score → SelectCandidates → Reserve"]
    end

    api(["Kubernetes API Server"])

    lib -->|imported by| ps
    lib -->|imported by| ks
    disp <-->|"watch pods / assign annotation"| api
    binder <-->|"bind / report outcomes"| api
    sched <-->|"filter / write K candidates"| api
    binder -. "ConfigMap snapshots (ParSync)" .-> api
    api -. "watch snapshots (ParSync)" .-> sched
```

The Dispatcher assigns incoming pods to scheduler instances and (in ParSync mode) manages partition configuration via the `ParSyncConfig` CRD. Each scheduler selects K candidate nodes using `scheduler-lib`. The Binder binds pods using the candidate list, detects conflicts, reports outcomes back, and (in ParSync mode) publishes per-partition node snapshots to ConfigMaps for schedulers to consume.

For the full design, see [docs/system-design.md](docs/system-design.md).

---

## Repository Structure

```
parkour/
├── scheduler-lib/          Core algorithm library (no K8s dependencies)
│   ├── multicandidate/     Candidate selection + strategies
│   ├── parsync/            Partition sync engine
│   ├── stats/              Conflict-rate tracking
│   ├── storage/            Storage abstraction
│   └── types/              Shared types + provider interfaces
│
├── para-scheduler/         Dispatcher + Binder processes
│   ├── cmd/dispatcher/     Dispatcher entry point
│   ├── cmd/binder/         Binder entry point
│   └── pkg/                Supporting packages
│       ├── dispatcher/     Pod assignment + ParSync coordination
│       ├── binder/         Binding + conflict detection + snapshot publisher
│       ├── cache/          Lightweight NodeInfo tracking
│       ├── annotation/     Candidate-list annotation serialization
│       └── metrics/        Prometheus metrics
│
├── k8s-scheduler/          K8s v1.33 fork with ParKour integration
│   └── pkg/scheduler/
│       ├── scheduler_parasched.go   Adapter + provider implementations
│       ├── parasched_sync.go        ParSync consumer + AdoptionStats watcher
│       └── schedule_one.go          Single-pod scheduling loop (modified)
│
├── para-sched-api/         CRD type definitions + generated clients
│   └── apis/v1/            ParSyncConfig / SchedulerAssignment / AdoptionStats
│
├── godel-scheduler/        Reference: KubeWharf Godel Scheduler (git subtree)
│
├── perf-tests/             K8s perf-tests fork (ClusterLoader2)
│
├── simulation/             Python discrete-event simulator
│   ├── paraScheduling.py   Simulator core
│   ├── scheduler.py        Per-scheduler state model
│   ├── exp.py              Multi-parameter sweep driver
│   ├── exp_softmax_temperature.py  Temperature sensitivity analysis
│   └── exp_fig*.py         Figure reproduction scripts
│
├── experiments/            Cluster experiment infrastructure
│   ├── scripts/            run-experiment.sh and supporting scripts
│   ├── kwok-setup/         KWOK node templates + ClusterLoader2 manifests
│   └── manifests/          Control-plane + monitoring configs
│
├── docs/
│   └── system-design.md    Architecture and algorithm reference
│
├── Makefile
└── LICENSE                 Apache 2.0
```

---

## Environment Requirements

### Simulation (Python)

- Python 3.9+
- `numpy`, `matplotlib`, `seaborn`

```bash
pip install numpy matplotlib seaborn
```

### Cluster Experiments (Go / Kubernetes)

| Component | Version |
|---|---|
| Go | 1.22+ |
| Kubernetes | 1.33+ |
| KWOK | 0.7+ |
| ClusterLoader2 | from `perf-tests/` |
| Prometheus | any recent version |
| kubectl | matching cluster version |

---

## Quick Start: Simulation

The simulator models parallel scheduling in a discrete-event environment. It is useful for fast exploration of how scheduler count, sync period, candidate count, and conflict-rate penalty interact.

```bash
cd simulation

# Run a parameter sweep (results cached in data/results.json)
python exp.py

# Re-run ignoring the cache
python exp.py --rerun

# Reproduce paper figures (reads cached results)
python exp_fig3.py
python exp_fig4.py
python exp_fig5.py
python exp_fig5_merged.py
python exp_fig6.py

# Show figure interactively after saving
python exp_fig3.py --show
```

See [simulation/README.md](simulation/README.md) for full usage and parameter reference.

---

## Quick Start: Cluster Experiments

Cluster experiments use a real (or KWOK-simulated) K8s 1.33 cluster with Prometheus metrics.

### 1. Build

```bash
# Algorithm library
cd scheduler-lib && go build ./...

# Dispatcher and Binder
cd para-scheduler && go build ./cmd/dispatcher ./cmd/binder

# k8s-scheduler fork (requires Linux or cross-compilation)
cd k8s-scheduler && make build
```

### 2. Deploy

```bash
# Apply CRDs and RBAC
kubectl apply -f para-sched-api/config/

# Deploy Dispatcher, Binder, and Schedulers
# (edit experiments/manifests/ to match your cluster)
kubectl apply -f para-scheduler/deploy/lab-cluster/
```

### 3. Run

```bash
# Set your Prometheus pushgateway endpoint
export PROMETHEUS_URL=http://<your-prometheus-host>:9091

bash experiments/scripts/run-experiment.sh \
  --nodes 10000 \
  --schedulers 10 \
  --sync-mode periodic \
  --sync-period 3.0 \
  --partitions 10 \
  --strategy QualityFirst \
  --penalty 0.3 \
  --candidates 3
```

See [experiments/README.md](experiments/README.md) for the full setup guide, KWOK configuration, and all available flags.

---

## Configuration Reference

### Dispatcher (`para-scheduler/cmd/dispatcher`)

| Flag | Default | Description |
|---|---|---|
| `--scheduler-names` | `sched-0,sched-1` | Comma-separated scheduler instance names |
| `--sync-mode` | `event` | `event` (event-driven) or `periodic` (ParSync) |
| `--sync-pattern` | `diff` | `glob`, `same`, or `diff` (ParSync only) |
| `--sync-period` | `3s` | Full sync period G (ParSync only) |
| `--num-partitions` | `3` | Number of node partitions M (ParSync only) |
| `--expected-nodes` | `0` | Node-count gate for /ready endpoint (0 = disabled) |
| `--workers` | `2` | Number of dispatch workers |
| `--kube-api-qps` | `10000` | API server QPS limit |
| `--kube-api-burst` | `10000` | API server burst limit |

### Binder (`para-scheduler/cmd/binder`)

| Flag | Default | Description |
|---|---|---|
| `--sync-mode` | `event` | `event` or `periodic` |
| `--sync-period` | `3s` | Full sync period G |
| `--num-partitions` | `3` | Number of partitions M |
| `--workers` | `4` | Number of bind workers |
| `--snapshot-flush-interval` | `100ms` | Snapshot publish batch window (ParSync only) |
| `--stats-flush-period` | `1s` | AdoptionStats CRD flush period |
| `--assumed-pod-ttl` | `30s` | TTL for assumed pods in cache |

### Scheduler (`k8s-scheduler/cmd/kube-scheduler`)

ParKour-specific flags are prefixed with `--parasched-`:

| Flag | Default | Description |
|---|---|---|
| `--parasched-name` | `""` | Scheduler instance name; enables ParKour when set |
| `--parasched-enable-parsync` | `false` | Enable ParSync partition sync mode |
| `--parasched-k-candidates` | `3` | Number of candidates K to select |
| `--parasched-strategy` | `QualityFirst` | Scoring strategy: `QualityFirst`, `LatencyFirst`, `WeightedRandom` |
| `--parasched-penalty` | `0.3` | Conflict-rate penalty weight p ∈ [0, 1] |

---

## Documentation

- [docs/system-design.md](docs/system-design.md) — Architecture, algorithms, data types, and design decisions
- [simulation/README.md](simulation/README.md) — Simulation environment and usage
- [experiments/README.md](experiments/README.md) — Cluster experiment setup and reproduction steps

---

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Acknowledgments

- [Godel Scheduler](https://github.com/kubewharf/godel-scheduler) — production parallel scheduler (architectural inspiration)
- [Kubernetes](https://github.com/kubernetes/kubernetes)
- [perf-tests / ClusterLoader2](https://github.com/kubernetes/perf-tests)
- [KWOK](https://kwok.sigs.k8s.io/) — Kubernetes Without Kubelet
- [ATC'21 ParSync paper](https://www.usenix.org/conference/atc21/presentation/feng-yihui) — partitioned synchronization algorithm