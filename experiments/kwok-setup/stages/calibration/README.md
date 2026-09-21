# Data-Plane Latency Calibration: Public Data Sources

> Snapshot date: 2026-09-05
> Script: `fetch-perfdash.py` (standard library only); summary: `perfdash-summary.csv`
> (median and range over each job's 20 most recent builds)

The data-plane latency injection module needs a pod-startup latency distribution
measured on real nodes. Rather than calibrating against this testbed's own
workers, it anchors on public measurements from upstream Kubernetes scalability
CI, which runs on real nodes at scales this testbed cannot reach.

## 1. Sources and Definitions

- Source: perf-dash.k8s.io (SIG-scalability ClusterLoader2 CI results), JSON endpoint
  `https://perf-dash.k8s.io/buildsdata?jobname=<job>&metriccategoryname=E2E&metricname=<metric>`.
- Jobs (real nodes, upstream default configuration):

  | perf-dash job | prow job | Scale |
  |---|---|---|
  | `gce-5000Nodes` | `ci-kubernetes-e2e-gce-scale-performance-5000` | 5,000-node GCE |
  | `aws-5000Nodes` | `ci-kubernetes-e2e-kops-aws-scale-amazonvpc-using-cl2` | 5,000-node AWS (kops) |
  | `gce-100Nodes-master` | `ci-kubernetes-e2e-gce-scale-performance-100` | 100-node GCE |
  | `aws-100Nodes` | `ci-kubernetes-e2e-kops-aws-small-scale-amazonvpc-using-cl2` | 100-node AWS |

- Phase definitions (`clusterloader2/pkg/measurement/common/slos/pod_startup_latency.go`):
  `schedule` = the watch first observes a non-empty `spec.nodeName` (i.e. Bind completed);
  `run` = the watch first observes `phase=Running`.
  **`schedule_to_run` is therefore exactly the post-Bind kubelet/CRI startup latency**,
  differing from the module's `T_data = t_ready - t_bind` only by readiness, which is
  negligible for pause-class pods.
- Workload: stateless `pause:3.9` pods with images pre-pulled by a DaemonSet (CL2 load
  config), so image pull is excluded — the same accounting as the upstream Pod Startup
  SLO (P99 <= 5 s).
- The three metrics correspond to the three phases of the CL2 load test:
  `LoadCreatePhase*` = cluster fill (roughly 30 pods per node starting concurrently,
  dominated by kubelet queueing); `LoadHighThroughput*` = high-QPS burst creation;
  `LoadStateless*` = steady-state scale/update (roughly 10 pods/s).

## 2. `schedule_to_run` Summary (median over the 20 most recent builds, ms)

| Job | Steady P50 / P90 / P99 | Burst P50 / P90 / P99 | Fill phase P50 / P90 / P99 |
|---|---|---|---|
| gce-5000Nodes | 840 / 993 / 1450 | **830 / 1109 / 2384** | 11533 / 82247 / 256653 |
| aws-5000Nodes | 787 / 844 / 964 | 763 / 829 / 1167 | 989 / 2331 / 129788 |
| gce-100Nodes-master | 775 / 873 / 989 | 770 / 925 / 1127 | 923 / 1823 / 5312 |
| aws-100Nodes | 801 / 880 / 1016 | 772 / 889 / 1056 | 939 / 1401 / 2262 |

How to read this:

- When only one pod starts per node at a time (the steady and burst phases), the
  Bind-to-Running median is around 0.8 s and P99 around 1.0-2.4 s, with little variation
  across cloud platform or scale. That is exactly the regime the high-contention
  scenario (1 pod/node) operates in.
- The fill-phase long tail comes from kubelet queueing when roughly 30 pods start per
  node at once. It is not the high-contention operating point; it is used only to define
  the heavy-tail sensitivity profile.
- In the 5,000-node burst phase, `create_to_schedule` reaches 20-40 s, showing that the
  scheduling queue is already saturated in that phase — consistent with treating it as
  the burst operating point.

Corroborating measurements (not calibration inputs): GKE v1.33 on B200 GPU nodes,
100 pods, startup latency P50/P90/P99 = 1.8/2.1/2.3 s (arXiv:2506.23628, scheduling
included); GKE 1.25 with a patched Cilium at 5,000 nodes, P99 around 6 s
(cilium/cilium#22023).

## 3. Profile Anchors Used

| Profile | Anchor source | P50 / P90 / P99 (ms) | Purpose |
|---|---|---|---|
| `Z0` | — | 0 | Existing fast Stage; control |
| `Dreal` | `gce-5000Nodes` burst phase (heaviest tail among comparable phases) | 830 / 1109 / 2384 | Main experiment: burst operating point on real nodes |
| `Dtail` | `gce-100Nodes-master` fill phase (measured heavy tail, P99 exceeds the 5 s SLO) | 923 / 1823 / 5312 | Sensitivity: does the benefit hold when the data plane is markedly slower |

Only three percentile anchors are available, not a full ECDF, so the Stage generates
latencies from four weighted buckets (uniform within each bucket):

| Bucket | Range | Probability | weight |
|---|---|---:|---:|
| b0 | `[0.7*P50, P50]` | 0.50 | 5,000 |
| b1 | `(P50, P90]` | 0.40 | 4,000 |
| b2 | `(P90, P99]` | 0.09 | 900 |
| b3 | `(P99, 2*P99]` | 0.01 | 100 |

The fidelity gate is defined against that target accordingly: the KS distance between
the measured post-injection `T_data` and the piecewise-uniform target must be <= 0.08,
and the P50/P90/P99 error must be <= `max(50 ms, 10%)`. The gate therefore verifies
that the injector reproduces the *configured* distribution, not that it reproduces this
testbed's own real distribution.

## 4. Optional Local Cross-Check

The master node runs a real kubelet and containerd. With the cluster idle, temporarily
removing the control-plane taint and binding `pause` pods directly by `nodeName`
(`imagePullPolicy: Never`, no probes or volumes) yields a single-node local `T_data`
distribution, which can be checked against the 0.5-2.5 s band above. Because it shares
a machine with the control plane and covers only one node, it serves as a sanity check
and does not replace the table above as the injection anchor.

## 5. Reproduction

`raw/` holds the 12 raw JSON responses fetched on 2026-09-05 (about 1.8 MB). Every
number in this document and in the paper comes from that snapshot, which can be rebuilt
offline:

```bash
python fetch-perfdash.py --from-raw raw/ --last 20 --out perfdash-summary.csv
```

Fetching again over the network (`python fetch-perfdash.py --last 20 --raw-dir raw/`)
yields a different "most recent 20" window: the 100-node jobs produce new builds daily,
and on a second fetch the same day the gce-100Nodes-master fill-phase P99 had already
moved from 5312 to 5241 ms. If the snapshot is refreshed, the numbers in this document
and in the experiment design must be updated together, with the fetch date and build
count recorded.
