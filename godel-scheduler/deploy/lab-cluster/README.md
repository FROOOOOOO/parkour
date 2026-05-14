# Godel Baseline Deployment (Lab Cluster)

Self-contained deployment for the **vanilla Godel** baseline used by
`experiments/experiment-design.md` §4.4 — specifically E1 (single Godel
scheduler) and E2 (Godel-vanilla, N=10 parallel schedulers).

This deploys **upstream Godel** unmodified. It is **independent** of the
para-scheduler deployment in
[`para-scheduler/deploy/lab-cluster/`](../../../para-scheduler/deploy/lab-cluster/),
running in its own `godel-system` namespace.

## Prerequisites

1. Lab cluster reachable via `kubectl` (B node <MASTER_IP> as master).
2. `godel-local:latest` image present in the control-plane node's containerd:
   ```bash
   cd godel-scheduler && make docker-images
   ```
3. `kustomize`, `envsubst` (gettext-base) on the host that runs `setup.sh`.

## Usage

```bash
# Initial deploy (default N=10 schedulers — E2 configuration)
./setup.sh

# E1: single scheduler
./setup.sh --scale 1

# Back to E2: N=10
./setup.sh --scale 10

# Reset between experiment trials (kwok-pods + scheduler CRD entries cleared,
# all components rollout-restarted)
./setup.sh --clean

# Full teardown (namespace + RBAC + CRDs)
./setup.sh --teardown
./setup.sh --teardown --keep-crd      # preserve CRDs for next deploy
```

## Layout

```
deploy/lab-cluster/
├── README.md                            this file
├── setup.sh                             deploy / --clean / --teardown / --scale
├── namespace.yaml                       godel-system
├── rbac.yaml                            SA + ClusterRole + Binding
├── scheduler-config.yaml                godel-scheduler-config ConfigMap
├── binder-config.yaml                   godel-binder-config ConfigMap
├── binder.yaml                          Binder Deployment (port :10451)
├── dispatcher.yaml                      Dispatcher Deployment
├── controller-manager.yaml              Controller Manager Deployment
├── scheduler-template.yaml              Scheduler Deployment (envsubst ${SCHED_INDEX})
├── metrics-services.yaml                Binder/Dispatcher NodePort
└── scheduler-metrics-svc-template.yaml  Per-scheduler NodePort (envsubst)
```

## NodePort allocation (avoid collision with para-scheduler 30080-30105)

| Component | NodePort | Container port |
|-----------|----------|----------------|
| Binder | 30200 | 10451 |
| Dispatcher | 30201 | 10351 |
| godel-sched-0 .. godel-sched-9 | 30210 .. 30219 | 10251 each |

All endpoints expose Prometheus metrics + healthz at `/metrics` / `/healthz` (insecure HTTP).

## Multi-scheduler routing

Each scheduler instance receives a unique `--godel-scheduler-name=godel-sched-${i}`
so it registers its own entry in `schedulers.scheduling.godel.kubewharf.io`.
The dispatcher auto-discovers these via its scheduler-maintainer and load-balances
incoming Pods (matched by `spec.schedulerName: godel-scheduler`) across all
N registered schedulers.

For E1 vs E2 the **only** difference is the scheduler instance count:

| Baseline | N | Command |
|----------|---|---------|
| E1 (single Godel) | 1 | `./setup.sh --scale 1` |
| E2 (Godel-vanilla) | 10 | `./setup.sh --scale 10` |

## CL2 workload routing

To route a CL2 workload to Godel, the Pod spec must set:
```yaml
spec:
  schedulerName: godel-scheduler
```
A ready-made testoverride lives at `experiments/scripts/cl2-saturation-godel.yaml`
(added in a subsequent step).

## Smoke test after deploy

```bash
kubectl -n godel-system get pods -o wide
kubectl get schedulers.scheduling.godel.kubewharf.io
# Should list godel-sched-0 .. godel-sched-9 for N=10

kubectl run godel-smoke --image=nginx:alpine \
  --overrides='{"spec":{"schedulerName":"godel-scheduler"}}' --restart=Never
kubectl get pod godel-smoke -o wide
kubectl delete pod godel-smoke --wait=false
```
