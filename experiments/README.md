# Cluster Experiments

The cluster half of the ParKour evaluation: the scripts that measure it on a
Kubernetes cluster whose nodes KWOK emulates, the minimal data behind the paper's
cluster figures and its overhead table, and the code that turns that data back
into them.

Three documents describe the experiments themselves:

- [experiment-design.md](experiment-design.md): what each experiment board asks,
  which baselines it compares, and how the metrics are defined;
- [matrix.md](matrix.md): every published cell with the parameters its runs used,
  generated from [registry.json](registry.json), which declares the matrix once;
- [reconciliation.md](reconciliation.md): where this repository's output differs
  from the camera-ready paper, and why.

## Three Ways to Use This Directory

| You have | You can | Section |
|---|---|---|
| This repository | Verify the archive and rebuild the eight cluster figures and the overhead table from it; with the public Alibaba trace, the trace figure too | [1](#1-rebuild-the-figures-from-the-archive) |
| This repository and a cluster | Re-run the published matrix or part of it, reduce your runs to an archive of your own, and draw the figures they feed from it | [2](#2-re-measure-on-a-cluster) |
| This repository and the recorded raw results | Rebuild the committed archive and confirm it byte for byte | [3](#3-rebuild-the-archive-from-recorded-results) |

Raw measurement results are not distributed. The per-trial records the figures
and the table need are committed under [archive/](archive/), licensed CC BY 4.0.

## Directory Structure

```
experiments/
├── experiment-design.md         What each board measures, and why
├── registry.json                The experiment matrix behind the figures, declared once
├── matrix.md                    That matrix as tables, generated from registry.json
├── reconciliation.md            Where the output differs from the camera-ready paper, and why
├── site.env.example             Values that belong to your cluster (copy to site.env)
├── requirements.txt             Python dependencies of the analysis and figures
│
├── archive/                     Minimal per-trial data behind the figures (CC BY 4.0)
│
├── common/                      Shared by the analysis scripts and the figures
│   ├── registry.py              Declares the matrix; generates registry.json
│   ├── schema.py                Record format of the archive
│   ├── cl2.py                   The CL2 log parser, for the runners and the reduction
│   ├── verify.py                Validation of the archive against the registry
│   ├── trials.py                The trial outlier filter — one implementation
│   ├── data.py                  The JSON envelope figure data travels in
│   ├── style.py                 Font, embedding and palette shared by figures
│   └── charts.py                Drawing helpers used by more than one figure
│
├── scripts/
│   │  Running experiments
│   ├── run-experiment.sh            Main experiment runner (one configuration, N trials)
│   ├── batch-run.sh                 Batch runner: the registry boards
│   ├── run-godel-baseline.sh        Godel baseline runner
│   ├── batch-run-godel.sh           Godel batch runner: the registry's Godel cells
│   ├── run-module-f.sh              Board F data-plane latency injection matrix
│   ├── run-supplementary.sh         Application-layer benchmark (the paper's discussion)
│   ├── run-workload-bench.sh        One application-benchmark configuration
│   ├── lib/common.sh                Site configuration and cluster helpers the scripts share
│   │
│   │  Cluster and workload setup
│   ├── create-nodes.sh              Create KWOK virtual nodes
│   ├── cleanup-cluster.sh           Restore a clean state after an aborted run
│   ├── etcd-maintenance.sh          etcd defragmentation and compaction
│   ├── preload-workload-images.sh   Preload the application benchmark's images onto workers
│   ├── generate-hetero-config.py    Heterogeneous node capacity testoverrides
│   ├── calibrate-dataplane.py       Derive the Board F latency profile from CI anchors
│   ├── injection-fidelity-gate.py   Board F injection fidelity gate (must PASS)
│   │
│   │  Collection and analysis
│   ├── collect-metrics.sh           Fetch a phase's Prometheus metrics when a trial ends
│   ├── collect-metrics-godel.sh     Same, for the Godel baseline
│   ├── pull-quality-metrics.py      Quality histograms; the collector runs it
│   ├── pull-windowed-acf.py         Board F's windowed ACF; run it right after the campaign
│   ├── process-results.py           Per-trial summaries (summary.csv) of recorded runs
│   ├── apply-outlier-filter.py      Filtered summary tables from those summaries
│   ├── analyze-temporal-occupancy.py  Bucket results by workload-fill occupancy
│   ├── reduce.py                    Recorded results -> an archive
│   ├── verify-results.py            Validate an archive against the registry
│   ├── export-figure-data.py        An archive -> one JSON per figure and table
│   └── generate-docs.py             registry.json -> matrix.md and the table below
│
├── figures/
│   ├── plot-<figure>.py         One script per paper figure; reads only JSON
│   ├── table-overhead.py        The overhead table's rows; reads only JSON
│   └── figure-data.lock.json    Hash of each figure's exported data
│
├── tests/                       Archive, export, CL2 parser, reduction, collector and driver tests
│
├── kwok-setup/
│   ├── install-kwok-services.sh     Install the KWOK shard services
│   ├── kwok-node.yaml               KWOK virtual node template
│   ├── kwok-deployment.yaml         KWOK shard deployment
│   ├── kwok-config.yaml             KWOK behavior configuration
│   ├── cl2-create-nodes.yaml        ClusterLoader2 node creation
│   ├── cl2-schedule-pods.yaml       CL2 workload with a latency phase (low contention)
│   ├── cl2-saturation-only.yaml     CL2 saturation-only workload (high contention)
│   ├── systemd/                     KWOK systemd service files (kwok, kwok1-kwok9)
│   └── stages/calibration/          Board F latency calibration: public CI snapshot
│
├── trace/alibaba2018/
│   └── export.py                    Board G: raw trace -> arrival-rate figure data
│
└── manifests/
    ├── control-plane/               The tuned etcd, kube-apiserver, controller and scheduler
    └── monitoring/                  Prometheus, Grafana, kube-state-metrics configs
```

---

## 1. Rebuild the Figures from the Archive

Every cluster figure except the trace figure, and the overhead table, is rebuilt
from the committed archive, from this repository alone:

```bash
pip install -r experiments/requirements.txt

python experiments/scripts/verify-results.py            # the archive against the registry
python experiments/scripts/export-figure-data.py --all  # the archive -> work/figure-data/
python experiments/figures/plot-robustness.py           # one figure; every plot-<figure>.py likewise
python experiments/figures/table-overhead.py            # the overhead table's rows, and the numbers its text quotes
```

The pipeline mirrors the simulation side, where `run.py` produces a cache and
`plot.py` only draws:

```text
registry.json + archive/  ->  export-figure-data.py  ->  work/figure-data/<element>.json
                          ->  figures/plot-<figure>.py, figures/table-overhead.py  ->  figures/output/
```

[registry.json](registry.json) declares the matrix: every cell's parameters, the
series it belongs to, and the figures it feeds. [archive/](archive/) holds the
per-trial records of those cells. The export is the only place that selects and
aggregates measurements. **The figure scripts hold no numbers of their own**:
each reads one JSON file and renders it, and the trial outlier filter, which
decides what a published number contains, has a single implementation in
[`common/trials.py`](common/trials.py).

<!-- BEGIN GENERATED: paper-elements -->
| Paper element | Boards | Cells | Runs | Rendered by |
| --- | --- | ---: | ---: | --- |
| `ablation-quality-a` | ablation | 8 | 40 | [`figures/plot-ablation-quality-a.py`](figures/plot-ablation-quality-a.py) |
| `ablation-quality-bc` | ablation | 8 | 40 | [`figures/plot-ablation-quality-bc.py`](figures/plot-ablation-quality-bc.py) |
| `dataplane-sensitivity` | F | 12 | 48 | [`figures/plot-dataplane-sensitivity.py`](figures/plot-dataplane-sensitivity.py) |
| `occupancy-intervals-1col` | B2 | 4 | 20 | [`figures/plot-occupancy-intervals-1col.py`](figures/plot-occupancy-intervals-1col.py) |
| `overhead` | B2 | 7 | 35 | [`figures/table-overhead.py`](figures/table-overhead.py) |
| `pareto-all-scales` | B2, godel | 32 | 160 | [`figures/plot-pareto-all-scales.py`](figures/plot-pareto-all-scales.py) |
| `robustness` | K, P | 36 | 108 | [`figures/plot-robustness.py`](figures/plot-robustness.py) |
| `scalability-lowcontention` | B1, godel | 12 | 60 | [`figures/plot-scalability-lowcontention.py`](figures/plot-scalability-lowcontention.py) |
| `scalability-schedulers` | B3, godel | 25 | 125 | [`figures/plot-scalability-schedulers.py`](figures/plot-scalability-schedulers.py) |
| `trace-arrival-rate` | public input `alibaba-cluster-trace-v2018`, pinned by SHA-256 | — | — | [`figures/plot-trace-arrival-rate.py`](figures/plot-trace-arrival-rate.py) |
<!-- END GENERATED: paper-elements -->

`--figure NAME` exports one element instead of all of them, and `--check` fails
unless the exported data matches the hashes in
[`figures/figure-data.lock.json`](figures/figure-data.lock.json). Every figure
script takes `--data` to read a different input, `--output-dir` to write
elsewhere, and `--show` to display the result.

These figures differ from the camera-ready ones in three deliberate ways, all
listed in [reconciliation.md](reconciliation.md): transcription rounding that the
camera-ready plotters carried; the Godel baseline's throughput, which is
computed from the CL2 log as for every other arm; and one single-scheduler
point, where latency-phase timeouts had been counted against the saturation
phase. The overhead table equals the camera-ready one; four numbers its text
quotes round differently from the data, and the same record lists them.

### The trace figure (Board G)

Board G is offline: instead of a cluster run it reduces the Alibaba Cluster
Trace v2018, which is public and therefore pinned rather than archived: the
registry records its source and the SHA-256 of `batch_task.csv`. The export
script's docstring gives the download. Its first run streams `batch_task.csv`
into a cached per-second series and reuses that cache afterwards.

```bash
python experiments/scripts/verify-results.py --trace <trace-dir>/batch_task.csv
python experiments/trace/alibaba2018/export.py --data-dir <trace-dir>
python experiments/figures/plot-trace-arrival-rate.py
```

The export also prints the trace statistics the paper quotes. The simulation
figures are produced separately; see [simulation/](../simulation).

---

## 2. Re-measure on a Cluster

### Prerequisites

| Component | Notes |
|---|---|
| Kubernetes 1.33 cluster | kubeadm; a control-plane node for the scheduling components, an infrastructure node for KWOK and Prometheus |
| KWOK 0.7 | Emulates the nodes; runs as ten systemd services, one per shard |
| ClusterLoader2 | Built from `perf-tests/` into `bin/clusterloader` |
| Prometheus | The collectors query it; set `PROMETHEUS_URL` in `experiments/site.env` |
| kubectl, jq, curl, bc | kubectl configured to reach the cluster |
| Go and Docker | To build the ParKour components, the Godel baseline and their images |
| kustomize, envsubst | The Godel baseline's deployment script |
| Python 3.9+ | `pip install -r experiments/requirements.txt` for the analysis and figures |

The application-layer benchmark additionally needs three physical worker nodes.

### Cluster Setup

**KWOK.** Install the shard services and start them; the runners create and
delete the virtual nodes themselves.

```bash
sudo bash experiments/kwok-setup/install-kwok-services.sh
sudo systemctl enable --now kwok kwok{1..9}

bash experiments/scripts/create-nodes.sh --nodes 1000   # optional smoke test; runs purge nodes as needed
```

**Control plane.** Large runs need a control plane tuned for them.
[manifests/control-plane/](manifests/control-plane/) holds the one the published
runs used: raise etcd's storage quota, the API server's in-flight limits and the
controller manager's and scheduler's API QPS as `etcd.yaml`,
`kube-apiserver.yaml`, `kube-controller-manager.yaml` and `kube-scheduler.yaml`
set them in `/etc/kubernetes/manifests/` (`kubeadm-config-new.yaml` holds the
same settings for a fresh `kubeadm init`). The API server keeps events in a
separate etcd on port 2479 (`--etcd-servers-overrides`); start it on the
control-plane node first:

```bash
bash experiments/manifests/control-plane/deploy-etcd-for-events.sh
```

These manifests carry placeholders where the published runs had the lab's
values: `<MASTER_IP>` for the control-plane node's address in `etcd.yaml`,
`kube-apiserver.yaml` and `kubeadm-config-new.yaml`, and `<HOSTNAME>` for its
node name in `etcd.yaml`. Replace them with your node's before using the files.

The cluster's pod network was flannel v0.27.2, installed after `kubeadm init`
from its unmodified manifest,
[`flannel/kube-flannel.yml`](manifests/control-plane/flannel/kube-flannel.yml)
(`kubectl apply -f` it); `kubeadm-config-new.yaml` sets the 10.244.0.0/16 pod
range it expects. The tuned `kube-controller-manager.yaml` then turns node-CIDR
allocation off, because KWOK's thousands of virtual nodes would exhaust that
range.

**ParKour components.** Build the images, load them into containerd on the
node the components run on, and deploy them with the lab-cluster setup script:

```bash
bash experiments/build-images.sh
docker save para-scheduler/binder:latest para-scheduler/dispatcher:latest \
    para-scheduler/kube-scheduler:latest \
  | ssh <user>@<MASTER_IP> 'sudo ctr -n k8s.io images import -'

bash para-scheduler/deploy/lab-cluster/setup.sh 10     # deploy with 10 schedulers
```

`setup.sh --clean` resets component state between runs (the runners call it),
`--scale N` changes the scheduler count, and `--teardown` removes everything.

**Godel baseline.** Three figures compare with upstream Godel, which runs in a
namespace of its own, `godel-system`, next to the ParKour components. Build its
image, load it on the same node and deploy it; `run-godel-baseline.sh` then
scales it to each cell's scheduler count and resets it between trials. The
image build prunes stopped containers and dangling images on the machine it
runs on.

```bash
(cd godel-scheduler && make docker-images)      # builds godel-local:latest
docker save godel-local:latest | ssh <user>@<MASTER_IP> 'sudo ctr -n k8s.io images import -'

bash godel-scheduler/deploy/lab-cluster/setup.sh   # deploy with 10 schedulers
```

[godel-scheduler/deploy/lab-cluster/](../godel-scheduler/deploy/lab-cluster/README.md)
describes the deployment and the NodePorts its metrics are scraped on.

**ClusterLoader2.**

```bash
(cd perf-tests/clusterloader2 && go build -o ../../bin/clusterloader ./cmd/clusterloader)
```

**Monitoring.** `manifests/monitoring/prometheus.yml` scrapes the control plane,
the binder, the dispatcher and the scheduler instances of ParKour and of Godel;
`gen-prometheus-config.sh` regenerates the ParKour scrape jobs for another
scheduler count, with the address in `MASTER_IP`. The file carries placeholders
as the control-plane manifests do: `<MASTER_IP>` for the control-plane node,
whose NodePorts expose the components' metrics, and `<WORKER_IP>` for the
application-layer benchmark's three physical workers. Replace them before
deploying: the deploy script mounts the file as it is.

Prometheus runs in Docker on the infrastructure node, started from
`manifests/monitoring/`, whose files its deploy script mounts by relative path.
It also mounts the credentials it scrapes the control plane with, which you
provide there: a token of the `monitoring` service account, under the file name
the script mounts, and copies of the control plane's CA and client
certificates, with the keys made readable by the container. `.gitignore` keeps
both out of version control.

```bash
M=experiments/manifests/monitoring
kubectl create namespace monitoring
kubectl apply -f $M/monitoring-sa.yaml
kubectl -n monitoring create token monitoring --duration=2400h > $M/token-260411-100d

mkdir -p $M/pki
ssh <user>@<MASTER_IP> 'sudo tar -C /etc/kubernetes/pki -cf - ca.crt etcd/ca.crt \
    apiserver-etcd-client.crt apiserver-etcd-client.key \
    apiserver-kubelet-client.crt apiserver-kubelet-client.key' | tar -C $M/pki -xf -
for key in apiserver-etcd-client apiserver-kubelet-client; do
  mv "$M/pki/$key.key" "$M/pki/$key.key.copy" && chmod 644 "$M/pki/$key.key.copy"
done

(cd $M && bash deploy-prometheus.sh)   # Prometheus on port 9091
(cd $M && bash deploy-grafana.sh)      # optional: Grafana on port 3001, dashboard in grafana.json
```

**Site configuration.** The scripts take the values that belong to your cluster
from the environment or from `experiments/site.env`. Copy
[site.env.example](site.env.example) to `experiments/site.env` and set:

- `PROMETHEUS_URL`: the Prometheus endpoint the metric collectors query;
- `CLUSTER_SUBNET`: the node subnet, kept off any shell-level HTTP(S) proxy;
- `ETCD_POD`: the etcd static pod the batch drivers compact and defragment
  after each experiment;
- `MASTER_IP`, `WORKER_IPS`, `WORKER_NAMES` and `SSH_USER`: the physical
  nodes of the application-layer benchmark, needed by that benchmark only.

A variable set in the environment takes precedence over the file.

### Run the Published Matrix

`batch-run.sh` runs the boards behind the paper's figures from
[registry.json](registry.json): every cell with the parameters and trial count
declared there ([matrix.md](matrix.md) lists them), into the board's directory
under the results root, each run stamped with its registry cell.
`batch-run-godel.sh` does the same for the Godel baseline cells.

```bash
bash experiments/scripts/batch-run.sh --group registry --dry-run   # preview: B1 B2 B3 K P ablation
bash experiments/scripts/batch-run.sh --group registry
bash experiments/scripts/batch-run.sh --group B2 --trials 3        # one board, fewer trials
bash experiments/scripts/batch-run-godel.sh --group all
```

`batch-run.sh` also takes `--group B1`, `B2`, `B3`, `K`, `P`, `ablation`, and
`C-event` or `C-periodic` for one paradigm of the ablation. `--results-root DIR`
writes somewhere other than `experiments/results`. Before the first run at a
new scale the drivers insert a throwaway warmup run, and after every run they
wait for the cluster to be clean and defragment etcd.

### Run One Cell

The dry-run plan prints each cell's complete command. To run one cell alone,
copy its line; for the event-driven ParKour cell at 10,000 nodes:

```bash
bash experiments/scripts/run-experiment.sh --name B2-10000n-E3 --nodes 10000 --schedulers 10 \
  --backup 2 --penalty 0.5 --strategy QualityFirst \
  --sync-period 0.1 --partitions 1 --sync-pattern diff \
  --trials 5 --results-dir experiments/results/B2 --preserve-nodes --variance 0.6 \
  --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi --strategy-seed 42 \
  --board B2 --cell B2-10000n-E3
```

### Board F: Data-Plane Latency Injection

Board F injects a post-bind startup latency distribution derived from public
Kubernetes scalability-CI measurements. Derive the profile and clear the
fidelity gate before running the matrix:

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

# 3. Run a sub-experiment. It is dry-run unless given --execute. F1-P, F2-E
#    and F2-P run the same way, with --experiment F1-P, F2-E or F2-P.
bash experiments/scripts/run-module-f.sh --experiment F1-E            # preview
bash experiments/scripts/run-module-f.sh --experiment F1-E --execute \
  --profile-json <profile.json> --gate-report <gate-report.json>

# 4. Right after the campaign, while Prometheus still holds the rounds' samples,
#    pull the ACF the figure reports into experiments/results/acf-windowed.csv.
python experiments/scripts/pull-windowed-acf.py
```

Each sub-experiment writes `experiments/results/module-f/<run-id>/`, one
directory per round (`--output-root` moves it). The round summaries hold the
ACF over the whole round only. The figure reports it at the T99 completion cut,
which comes from the binder's counters at 1 s resolution and cannot be
recomputed once Prometheus has dropped them: `pull-windowed-acf.py` rebuilds
it, marks a round verified only when the counters reproduce the round's
summary, and never replaces its output without `--force`. `--runs` limits it to
the campaign's runs.

`run-module-f.sh --self-test` generates the temporary Stage, runner and watcher
and syntax-checks them without contacting the cluster.

### Application-Layer Benchmark

The check the paper's discussion reports runs Nginx, Redis and MySQL on three
physical workers, which `MASTER_IP`, `WORKER_IPS` and `WORKER_NAMES` in
`site.env` name: Vanilla and ParKour in each paradigm, under three CPU-contention
profiles, 12 configurations × 3 trials.

```bash
# Once, on a node with Docker and external network access (needs SSH_USER too)
bash experiments/scripts/preload-workload-images.sh

bash experiments/scripts/run-supplementary.sh --dry-run    # preview the 12 configurations
bash experiments/scripts/run-supplementary.sh              # ~5 h; runs go to <results root>/D
```

Its results are not archived: the numbers the discussion quotes come from the
recorded runs.

### Results

A run is saved to `<results-dir>/<name>_<timestamp>/`. The batch drivers use one
directory per board under the results root, as the registry names them
(`experiments/results/B2/B2-2000n-E3_<timestamp>/`), which is the layout
`reduce.py --results` reads. Each trial produces:

```
<name>_<timestamp>/
├── config.json          Experiment parameters (and registry cell, from the drivers)
└── trial-1/
    ├── timing.json      Wall-clock timing per phase
    ├── cl2.log          ClusterLoader2 log
    ├── junit.xml        CL2 JUnit summary
    └── metrics-saturation/
        ├── scheduler_cpu.json, snap_algo_latency.json, ...   Prometheus metrics of the phase
        └── quality.json   Node-score and candidate-rank histograms
```

Everything a figure or the table needs is collected when the trial ends, and
nothing is re-queried from Prometheus afterwards.

### From Your Runs to the Figures

`reduce.py` turns a results root into an archive of your own, which the rest of
the pipeline reads exactly as it reads the committed one:

```bash
python experiments/scripts/reduce.py --results experiments/results \
  --anchored experiments/results --out <your-archive>
python experiments/scripts/verify-results.py --archive <your-archive>
python experiments/scripts/export-figure-data.py --all --archive <your-archive>
python experiments/figures/plot-robustness.py           # and every other figure; table-overhead.py for the table
```

`--anchored` names the module-F package: a directory holding
`module-f/<run-id>/` for exactly the campaign's runs, and the
`acf-windowed.csv` pulled for them. With the runner's defaults that is
`experiments/results`, once aborted or superseded runs are moved out of
`module-f/`.

A figure needs only the boards that feed it, which the table in section 1
lists. After re-running some boards, reduce, verify and export just those:

```bash
python experiments/scripts/reduce.py --results experiments/results \
  --boards B2 godel --out <your-archive>
python experiments/scripts/verify-results.py --archive <your-archive> --boards B2 godel
python experiments/scripts/export-figure-data.py --archive <your-archive> \
  --figure pareto-all-scales --figure overhead
```

Such an archive holds those boards' records and the records derived from them
alone (here also the occupancy and overhead records, which come from B2). The
verification checks it against those boards and names the paper elements it
can rebuild; exporting a figure that needs another board fails, naming the
board.

Your numbers are your own, so the export will not match the lock file; compare
the figures with the published ones instead. The per-trial summaries give a
quick look before any reduction:

```bash
# One row per trial, into each board's summary.csv
for board in B1 B2 B3 K P ablation; do
  python experiments/scripts/process-results.py experiments/results/$board/ \
    -o experiments/results/$board/summary
done
# The figures' outlier filter over those summaries, as tables
python experiments/scripts/apply-outlier-filter.py --results experiments/results/ --stdout
# The occupancy analysis of the B2 runs
python experiments/scripts/analyze-temporal-occupancy.py --scope b2 \
  --results experiments/results/ --json-out <occupancy.json>
```

`apply-outlier-filter.py` writes its tables to
`experiments/work/filtered-eval-data.md` unless given `--out`.

### run-experiment.sh Flags

#### Scheduling Method

| Flag | Default | Description |
|---|---|---|
| `--backup NUM` | `2` | Backup candidates B; K = B+1 (0 = disable multicandidate) |
| `--strategy STR` | `QualityFirst` | `QualityFirst`, `LatencyFirst`, `WeightedRandom`, `QualityFirstParSync`, `LatencyFirstParSync` |
| `--penalty FLOAT` | `0.3` | Penalty weight w ∈ [0, 1] (0 = disable); the published cells use 0.5 or 0 |
| `--strategy-seed INT` | `42` | PRNG seed for WeightedRandom / ParSync strategies |

#### Sync Mode

| Flag | Default | Description |
|---|---|---|
| `--sync-period FLOAT` | `1.0` | Full sync period G in seconds; <0.5 → event mode |
| `--partitions NUM` | `10` | Number of partitions M; globSync always runs one |
| `--sync-pattern STR` | `diff` | `glob` (P1, P4), `same` (P2), `diff` (P3) |

**Sync mode auto-detection**: `sync-period >= 0.5` → ParSync periodic mode;
`sync-period < 0.5` → event-driven mode.

#### Cluster and Workload

| Flag | Default | Description |
|---|---|---|
| `--nodes NUM` | `10000` | Number of KWOK virtual nodes |
| `--schedulers NUM` | `10` | Number of scheduler instances |
| `--pods-per-node NUM` | `29` | Saturation workload: pods per node |
| `--cpu-request STR` | `1000m` | Pod CPU request |
| `--memory-request STR` | `8Gi` | Pod memory request |
| `--variance V` | `0` | Node capacity variance: `0` (homogeneous) or `0.3`, `0.6`, `1.0`; the published cells use `0.6` |
| `--trials NUM` | `1` | Number of independent trials |

#### Infrastructure

| Flag | Default | Description |
|---|---|---|
| `--prometheus-url URL` | `$PROMETHEUS_URL` | Prometheus endpoint the collector queries |
| `--binder-workers NUM` | `8` | Binder worker goroutines |
| `--collect-logs` | off | Save per-trial scheduler/binder/dispatcher logs |
| `--preserve-nodes` | off | Skip node create/delete steps between runs |
| `--results-dir DIR` | `experiments/results` | Directory the run directory is created in |
| `--board B --cell C` | none | The registry cell the run measures, recorded in `config.json` |

---

## 3. Rebuild the Archive from Recorded Results

Only whoever holds the recorded results can do this. `reduce.py` reads a results
root with one directory per board, as the registry names them, and the anchored
module-F package; it writes the archive and never writes into the results:

```bash
python experiments/scripts/reduce.py --results <results-root> --anchored <module-f-package>
python experiments/scripts/reduce.py --results <results-root> --anchored <module-f-package> --check
```

`--check` writes nothing and fails unless the archive it rebuilds matches the
committed one byte for byte. The reduction itself is tested without the raw
results: the tests write small synthetic ones in the runners' layout
([tests/rawdata.py](tests/rawdata.py)) and check every record family, and a
whole reduced archive, against what they describe.

When the registry changes, regenerate the documents rendered from it:

```bash
python experiments/common/registry.py           # registry.json
python experiments/scripts/generate-docs.py     # matrix.md and the table in section 1
```

CI ([`.github/workflows/experiments.yml`](../.github/workflows/experiments.yml))
checks that the registry and the documents rendered from it are current,
validates the archive, compares the exported data with the lock, runs
[tests/](tests/), and renders every archived figure and the overhead table.
