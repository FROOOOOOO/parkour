# Godel Baseline Deployment (Lab Cluster)

Self-contained deployment of the **Godel** baseline the cluster experiments
compare with ([experiment-design.md §4.2](../../../experiments/experiment-design.md#42-baselines)):
N Godel schedulers in parallel, with N from 2 to 10 in the published cells (the
registry's `godel` board, listed in
[matrix.md](../../../experiments/matrix.md#board-godel)).
`experiments/scripts/run-godel-baseline.sh` scales it to each cell's N and
resets it between trials.

This deploys **upstream Godel** unmodified. It is **independent** of the
para-scheduler deployment in
[`para-scheduler/deploy/lab-cluster/`](../../../para-scheduler/deploy/lab-cluster/),
running in its own `godel-system` namespace.

## Prerequisites

1. A cluster reachable via `kubectl`; the components run on its control-plane node.
2. The `godel-local:latest` image in the control-plane node's containerd. The
   build prunes stopped containers and dangling images on the machine it runs on:
   ```bash
   cd godel-scheduler && make docker-images
   docker save godel-local:latest | ssh <user>@<MASTER_IP> 'sudo ctr -n k8s.io images import -'
   ```
3. `kustomize`, `envsubst` (gettext-base) on the host that runs `setup.sh`.

## Usage

```bash
# Initial deploy (10 schedulers)
./setup.sh

# Change the scheduler count; run-godel-baseline.sh does this for each cell
./setup.sh --scale 4

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

All endpoints expose Prometheus metrics + healthz at `/metrics` / `/healthz`
(insecure HTTP); `experiments/manifests/monitoring/prometheus.yml` scrapes them.

## Multi-scheduler routing

Each scheduler instance receives a unique `--godel-scheduler-name=godel-sched-${i}`
so it registers its own entry in `schedulers.scheduling.godel.kubewharf.io`.
The dispatcher auto-discovers these via its scheduler-maintainer and load-balances
incoming Pods (matched by `spec.schedulerName: godel-scheduler`) across all
N registered schedulers. The published cells differ only in N, which
`./setup.sh --scale N` sets.

## CL2 workload routing

To route a CL2 workload to Godel, the Pod spec must set:
```yaml
spec:
  schedulerName: godel-scheduler
```
`run-godel-baseline.sh` points ClusterLoader2 at
[`experiments/kwok-setup/kwok-deployment-godel.yaml`](../../../experiments/kwok-setup/kwok-deployment-godel.yaml),
whose pods set it.

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
