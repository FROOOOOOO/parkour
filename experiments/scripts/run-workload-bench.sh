#!/bin/bash

# Real workload benchmark for Para-Sched: the application-layer check.
#
# Deploys nginx/redis/mysql to the three physical worker nodes, runs benchmarks,
# and measures application-level performance to validate scheduling quality.
#
# v3 (2026-05-08 — per-trial-redeploy redesign):
#   - Each trial now performs a FULL schedule+benchmark cycle:
#       cleanup namespace → redeploy stress+workloads → re-schedule
#       → record placement-quality → run benchmark → cooldown
#     This generates an independent placement sample per trial; prior versions
#     deployed once and ran N benchmarks against the SAME placement, which
#     measured only benchmark noise (TCP / InnoDB / wrk jitter) and not
#     scheduling-decision variance — placement randomness vs. scheduling
#     systematic-effect were inseparable at N=1 placement per (strategy,profile).
#   - Default TRIALS reduced to 3 (each trial ~5-8 min including redeploy +
#     180s benchmark + cleanup); scales linearly via --trials.
#   - placement-quality.json is now per-trial (trial-${i}/placement-quality.json).
#   - Summary aggregates application metrics (mean ± stddev) across trials.
#   - --reset-mysql-each-trial flag removed (now redundant: each trial gets a
#     fresh MySQL pod via redeploy, so sysbench prepare runs inside every trial).
#
# v2 (2026-04-22):
#   - Scaled up from 6 → 24 replicas per workload and 60s → 180s per benchmark
#     to push noise floor below the E2-vs-E3 signal
#   - Added --stress-profile {none,mild,heavy} to inject heterogeneous CPU
#     pressure across workers; forces scheduler to make non-trivial placement
#     decisions (otherwise 3 nodes × light load → decision space collapses)
#   - Added placement-quality metrics (pod/node distribution, CPU Gini
#     coefficient) computed from kubectl inventory
#
# Usage:
#   ./run-workload-bench.sh --strategy E2 --trials 3
#   ./run-workload-bench.sh --strategy E3 --trials 5 --stress-profile heavy
#
# Prerequisites:
#   - The three worker nodes are Ready: WORKER_IPS / WORKER_NAMES
#   - Para-sched components deployed (setup.sh)
#   - wrk, redis-benchmark, sysbench available on master node
#   - python3 (for placement-quality + summary aggregation)
#
# Site configuration, from the environment or experiments/site.env:
#   MASTER_IP       the master node, which serves the benchmarks' NodePorts
#   WORKER_IPS      the three physical workers' IPs (node_exporter queries)
#   WORKER_NAMES    their node names as registered with Kubernetes, same order
#   PROMETHEUS_URL  Prometheus endpoint (default: http://localhost:9091)
#
# Runs are written under the results root's D directory, as run-supplementary.sh
# records the published benchmark.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
RESULTS_DIR="$RESULTS_ROOT/D"
DEPLOY_DIR="$PROJECT_ROOT/para-scheduler/deploy/lab-cluster"
NAMESPACE="workload-bench"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Default parameters
# Sized for 3 workers × 24 CPU / 32 GB each:
#   allocatable per node ≈ 22 CPU / 30 GB (after system reserves)
#   workload request total (27 CPU, 27 GB) → ~50% util under heavy stress (54 CPU free)
#   ~6 workload pod/node average, room for scheduler to actually pick a node.
STRATEGY=""
NUM_SCHEDULERS=5
TRIALS=3                  # ↓ from 5: each trial now does full redeploy+benchmark
                          # (independent placement sample), so 3 typically suffices
                          # for mean ± stddev. Scale via --trials for tighter CI.
BENCH_DURATION=180        # ↑ from 60: dilute benchmark warm-up / tear-down noise
NGINX_REPLICAS=18         # 18×750m=13.5 CPU total; 6/node avg
REDIS_REPLICAS=18         # 18×750m=13.5 CPU total; 6/node avg
MYSQL_REPLICAS=1          # stateful, cannot scale behind NodePort (see §2 below)
STRESS_PROFILE="none"     # none | mild | heavy
WARMUP=false              # Prepend a throwaway trial whose results are discarded;
                          # mitigates host-level cold-start drift (CPU governor
                          # ramp, page cache warmup) before measured trials begin.
COOLDOWN=10               # seconds between trials (let CPU/network settle, prior
                          # workload pods fully terminate before next redeploy)
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}"

# Worker nodes (physical) — IPs for Prometheus node_exporter queries, and the
# node names they are registered under, in the same order. Checked once the
# options are read, so that --help needs no site configuration.
read -r -a WORKER_NODES <<< "${WORKER_IPS:-}"
read -r -a WORKER_NODE_NAMES <<< "${WORKER_NAMES:-}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --strategy)        STRATEGY="$2"; shift 2 ;;
        --schedulers)      NUM_SCHEDULERS="$2"; shift 2 ;;
        --trials)          TRIALS="$2"; shift 2 ;;
        --duration)        BENCH_DURATION="$2"; shift 2 ;;
        --nginx-replicas)  NGINX_REPLICAS="$2"; shift 2 ;;
        --redis-replicas)  REDIS_REPLICAS="$2"; shift 2 ;;
        --stress-profile)  STRESS_PROFILE="$2"; shift 2 ;;
        --warmup)          WARMUP=true; shift ;;
        --cooldown)        COOLDOWN="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --strategy <E2|E3|P1|P4> [options]

Required:
  --strategy STR         Scheduling strategy:
                           E2 vanilla event-driven       (K=0, p=0,   sync=event)
                           E3 proposed event-driven      (K=2, p=0.5, sync=event)
                           P1 vanilla periodic           (K=0, p=0,   sync=periodic glob/p=1, G=1s)
                           P4 proposed periodic          (K=2, p=0.5, sync=periodic glob/p=1, G=1s)
                         K/p can be overridden via env (CANDIDATE_K, PENALTY).

Optional:
  --schedulers NUM       Number of scheduler instances (default: 5)
  --trials NUM           Number of independent placement+benchmark cycles
                         (default: 3). Each trial does a full redeploy →
                         re-schedule → benchmark, yielding one independent
                         placement sample per trial. Use ≥5 for tighter
                         confidence intervals on mean ± stddev.
  --duration SEC         Per-trial benchmark duration (default: 180)
  --nginx-replicas NUM   nginx replicas (default: 18)
  --redis-replicas NUM   redis replicas (default: 18)
  --warmup               Prepend one throwaway trial (results discarded)
                         before the measured trials. Useful for host-level
                         cold-start (CPU governor ramp, page cache warmup).
  --cooldown SEC         Seconds to wait between trials (default: 10) for
                         prior workload pods to fully terminate and node
                         CPU/network to settle before next redeploy.
  --stress-profile STR   Background CPU stress distribution across 3 workers:
                           none  (default): no stress pods; backward-compat
                           mild : node[0]=0, node[1]=1, node[2]=2 stress pods
                                  (per node available CPU: 22/20/18; diff ≈4 CPU)
                           heavy: node[0]=0, node[1]=2, node[2]=4 stress pods
                                  (per node available CPU: 22/18/14; diff ≈8 CPU)
                         Stress pods are pinned via .spec.nodeName (bypassing
                         the scheduler). They make placement decisions matter —
                         E3's penalty mechanism should prefer the quieter node.

Trial design (v3):
  Each trial = independent schedule+benchmark cycle:
    1. Clean prior workload namespace
    2. Redeploy stress + nginx + redis + mysql → para-scheduler picks placement
    3. Record placement-quality.json into trial-N/
    4. Sysbench prepare (fresh mysql pod each trial)
    5. Run wrk + redis-benchmark + sysbench concurrently for \$BENCH_DURATION s
    6. Cooldown
  Summary aggregates per-trial app metrics (mean ± stddev across N trials).

Resource budget (on 3 × 24C32G workers):
  Allocatable per node  ≈ 22 CPU / 30 GB (after system reserves)
  Workload total request = 27 CPU / 27 GB (nginx+redis+mysql @ 750m/1CPU)
  Under heavy stress: workload/available = 27/54 = 50% utilization,
  leaving a ~8-CPU gradient between node[0] (22 free) and node[2] (14 free)
  for the scheduler's placement decision to actually matter.

Validates scheduling quality on a 3-worker physical cluster. run-supplementary.sh
runs the published matrix. K, p and the strategy can be overridden via env:
  CANDIDATE_K, PENALTY, STRATEGY_NAME
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$STRATEGY" ]; then
    echo "Error: --strategy is required (E2|E3|P1|P4)"
    exit 1
fi

: "${MASTER_IP:?set MASTER_IP in experiments/site.env or the environment}"
if [ ${#WORKER_NODES[@]} -ne 3 ] || [ ${#WORKER_NODE_NAMES[@]} -ne 3 ]; then
    echo "Error: set WORKER_IPS and WORKER_NAMES, three each, in experiments/site.env or the environment"
    exit 1
fi

case $STRESS_PROFILE in
    none|mild|heavy) ;;
    *) echo "Error: --stress-profile must be none|mild|heavy"; exit 1 ;;
esac

EXPERIMENT_DIR="$RESULTS_DIR/D1-${STRATEGY}-${STRESS_PROFILE}_${TIMESTAMP}"
mkdir -p "$EXPERIMENT_DIR"

echo "============================================"
echo "Real Workload Benchmark (Board E, v3 per-trial-redeploy)"
echo "============================================"
echo "Strategy:         $STRATEGY"
echo "Schedulers:       $NUM_SCHEDULERS"
echo "Trials:           $TRIALS  (each = full redeploy + benchmark)"
echo "Duration:         ${BENCH_DURATION}s per trial benchmark"
echo "Replicas:         nginx=${NGINX_REPLICAS}, redis=${REDIS_REPLICAS}, mysql=${MYSQL_REPLICAS}"
echo "Stress profile:   $STRESS_PROFILE"
echo "Cooldown:         ${COOLDOWN}s between trials"
echo "Warmup:           $WARMUP"
echo "Results dir:      $EXPERIMENT_DIR"
echo "============================================"
echo ""

echo "Worker nodes:"
for i in 0 1 2; do
    echo "  ${WORKER_NODES[$i]} → ${WORKER_NODE_NAMES[$i]}"
done
echo ""

# Save config
cat > "$EXPERIMENT_DIR/config.json" <<EOF
{
  "experiment": "D1-${STRATEGY}-${STRESS_PROFILE}",
  "strategy": "$STRATEGY",
  "num_schedulers": $NUM_SCHEDULERS,
  "trials": $TRIALS,
  "trial_design": "per-trial-redeploy (v3)",
  "bench_duration": $BENCH_DURATION,
  "cooldown": $COOLDOWN,
  "warmup": $WARMUP,
  "nginx_replicas": $NGINX_REPLICAS,
  "redis_replicas": $REDIS_REPLICAS,
  "mysql_replicas": $MYSQL_REPLICAS,
  "stress_profile": "$STRESS_PROFILE",
  "worker_nodes": $(printf '%s\n' "${WORKER_NODES[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))"),
  "worker_node_names": $(printf '%s\n' "${WORKER_NODE_NAMES[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))"),
  "timestamp": "$TIMESTAMP"
}
EOF

# ============================================================
#  Step 1: Configure schedulers based on strategy (ONCE — scheduler
#  configuration persists across trials; only the workload deployment
#  is recreated per trial to generate independent placement samples).
# ============================================================
echo "Step 1: Configuring schedulers for strategy $STRATEGY..."

PARA_NS="para-system"

# Sync defaults — event-driven strategies use these as-is; periodic ones override below.
SYNC_MODE="event"
SYNC_PERIOD="0.1"
SYNC_PARTITIONS=1
SYNC_PATTERN="diff"
ENABLE_PARSYNC="false"

case $STRATEGY in
    E2)
        CANDIDATE_K="${CANDIDATE_K:-0}"; PENALTY="${PENALTY:-0.0}"
        ;;
    E3)
        # Proposed event-driven (ParKour: K=2, p=0.5); env can override K/p
        CANDIDATE_K="${CANDIDATE_K:-2}"; PENALTY="${PENALTY:-0.5}"
        ;;
    P1)
        # Vanilla periodic (canonical: K=0, p=0). globSync chosen for the small
        # physical cluster: with 3 workers / 5 schedulers, partitions>1 collapses
        # partition-grain selection (each partition has ≤1 worker), so glob is
        # the meaningful periodic baseline here.
        CANDIDATE_K="${CANDIDATE_K:-0}"; PENALTY="${PENALTY:-0.0}"
        SYNC_MODE="periodic"; SYNC_PERIOD="1.0"; SYNC_PARTITIONS=1
        SYNC_PATTERN="glob"; ENABLE_PARSYNC="true"
        ;;
    P4)
        # Proposed periodic (K=2, p=0.5) — same sync infra as P1 (glob, partitions=1)
        # so paradigm comparison varies only multicandidate + penalty terms.
        CANDIDATE_K="${CANDIDATE_K:-2}"; PENALTY="${PENALTY:-0.5}"
        SYNC_MODE="periodic"; SYNC_PERIOD="1.0"; SYNC_PARTITIONS=1
        SYNC_PATTERN="glob"; ENABLE_PARSYNC="true"
        ;;
    *)
        echo "Error: unknown strategy $STRATEGY (use E2|E3|P1|P4)"
        exit 1
        ;;
esac

STRATEGY_NAME="${STRATEGY_NAME:-QualityFirst}"

# expected-nodes drives ParSync partition assignment (dispatcher/binder need it).
# With partitions=1 it's effectively a no-op, but is required when partitions>1.
EXPECTED_NODES=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | xargs)
[ -z "$EXPECTED_NODES" ] && EXPECTED_NODES=4

kubectl apply -f "$DEPLOY_DIR/adoption-stats.yaml" 2>/dev/null

# 1a. Scale scheduler count
CURRENT_SCHED_COUNT=$(kubectl -n "$PARA_NS" get deployments -l app=para-scheduler --no-headers 2>/dev/null | wc -l)
echo "  Current schedulers: $CURRENT_SCHED_COUNT, target: $NUM_SCHEDULERS"

if [ "$CURRENT_SCHED_COUNT" -gt "$NUM_SCHEDULERS" ]; then
    for i in $(seq "$NUM_SCHEDULERS" $((CURRENT_SCHED_COUNT - 1))); do
        kubectl -n "$PARA_NS" delete deployment "para-scheduler-${i}" --ignore-not-found --wait=true --timeout=60s 2>/dev/null || true
        kubectl -n "$PARA_NS" delete service "scheduler-metrics-${i}" --ignore-not-found 2>/dev/null || true
    done
    EXCESS_DEADLINE=$(($(date +%s) + 60))
    while true; do
        LIVE_SCHED=$(kubectl -n "$PARA_NS" get pods -l app=para-scheduler --no-headers 2>/dev/null | wc -l)
        [ "$LIVE_SCHED" -le "$NUM_SCHEDULERS" ] && break
        [ "$(date +%s)" -ge "$EXCESS_DEADLINE" ] && break
        sleep 3
    done
fi

if [ "$CURRENT_SCHED_COUNT" -lt "$NUM_SCHEDULERS" ]; then
    for i in $(seq "$CURRENT_SCHED_COUNT" $((NUM_SCHEDULERS - 1))); do
        SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
            < "$DEPLOY_DIR/scheduler-template.yaml" | kubectl apply -f - 2>/dev/null
        SCHED_INDEX="$i" SCHED_NODE_PORT=$((30090 + i)) \
            envsubst '$SCHED_INDEX $SCHED_NODE_PORT' \
            < "$DEPLOY_DIR/scheduler-metrics-svc-template.yaml" | kubectl apply -f - 2>/dev/null
    done
fi

# 1b. Patch scheduler args
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    SCHED_ARGS="[\"--config=/etc/scheduler/scheduler-config.yaml\",\"--v=3\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-name=sched-${i}\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-candidate-k=$CANDIDATE_K\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-strategy.name=$STRATEGY_NAME\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-strategy.penalty-weight=$PENALTY\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-enable-parsync=$ENABLE_PARSYNC\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-num-partitions=$SYNC_PARTITIONS\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-sync-period=${SYNC_PERIOD}s\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-namespace=para-system\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-stats-name=default\""
    SCHED_ARGS="$SCHED_ARGS]"
    kubectl -n "$PARA_NS" patch deployment para-scheduler-${i} --type=json \
        -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$SCHED_ARGS}]" \
        2>/dev/null
done

# 1c. Dispatcher args
SCHED_NAMES=""
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    [ -n "$SCHED_NAMES" ] && SCHED_NAMES="$SCHED_NAMES,"
    SCHED_NAMES="${SCHED_NAMES}sched-${i}"
done
DISPATCHER_ARGS="[\"--scheduler-names=$SCHED_NAMES\",\"--sync-mode=$SYNC_MODE\",\"--workers=4\",\"--metrics-addr=:8081\",\"--kube-api-qps=10000\",\"--kube-api-burst=10000\""
if [ "$SYNC_MODE" = "periodic" ]; then
    DISPATCHER_ARGS="$DISPATCHER_ARGS,\"--sync-pattern=$SYNC_PATTERN\",\"--sync-period=${SYNC_PERIOD}s\",\"--num-partitions=$SYNC_PARTITIONS\",\"--expected-nodes=$EXPECTED_NODES\""
fi
DISPATCHER_ARGS="$DISPATCHER_ARGS]"
kubectl -n "$PARA_NS" patch deployment para-dispatcher --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$DISPATCHER_ARGS}]" \
    2>/dev/null

# 1d. Binder args
BINDER_ARGS="[\"--sync-mode=$SYNC_MODE\",\"--workers=8\",\"--assumed-pod-ttl=30s\",\"--stats-name=default\",\"--stats-flush-period=1s\",\"--metrics-addr=:8080\",\"--kube-api-qps=10000\",\"--kube-api-burst=10000\""
if [ "$SYNC_MODE" = "periodic" ]; then
    BINDER_ARGS="$BINDER_ARGS,\"--num-partitions=$SYNC_PARTITIONS\",\"--sync-period=${SYNC_PERIOD}s\",\"--snapshot-flush-interval=100ms\""
fi
BINDER_ARGS="$BINDER_ARGS]"
kubectl -n "$PARA_NS" patch deployment para-binder --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$BINDER_ARGS}]" \
    2>/dev/null

# 1e. Restart
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    kubectl -n "$PARA_NS" rollout restart deployment/para-scheduler-${i} 2>/dev/null
done
kubectl -n "$PARA_NS" rollout restart deployment/para-dispatcher 2>/dev/null
kubectl -n "$PARA_NS" rollout restart deployment/para-binder 2>/dev/null

# 1f. Clean stale snapshot ConfigMaps
kubectl -n "$PARA_NS" delete configmap -l app=parasched-snapshot --ignore-not-found 2>/dev/null || true
for i in $(seq 0 20); do
    kubectl -n "$PARA_NS" delete configmap "parasched-snapshot-${i}" --ignore-not-found 2>/dev/null || true
done

# 1g. Wait for rollout
echo "  Waiting for component rollout..."
kubectl -n "$PARA_NS" rollout status deployment/para-binder --timeout=60s 2>/dev/null
kubectl -n "$PARA_NS" rollout status deployment/para-dispatcher --timeout=60s 2>/dev/null
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    kubectl -n "$PARA_NS" rollout status deployment/para-scheduler-${i} --timeout=60s 2>/dev/null
done

# ============================================================
#  Helpers used by the per-trial loop
# ============================================================

# Cleanup workload namespace (idempotent — safe to call when ns absent).
cleanup_workload_namespace() {
    local ns_phase
    ns_phase=$(kubectl get ns "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$ns_phase" = "Terminating" ]; then
        local deadline=$(($(date +%s) + 120))
        while kubectl get ns "$NAMESPACE" &>/dev/null; do
            if [ "$(date +%s)" -ge "$deadline" ]; then
                kubectl get ns "$NAMESPACE" -o json 2>/dev/null \
                    | jq '.spec.finalizers = []' \
                    | kubectl replace --raw "/api/v1/namespaces/$NAMESPACE/finalize" -f - 2>/dev/null || true
                sleep 5
                break
            fi
            sleep 3
        done
    elif [ "$ns_phase" = "Active" ]; then
        kubectl delete namespace "$NAMESPACE" --ignore-not-found 2>/dev/null || true
        local deadline=$(($(date +%s) + 120))
        while kubectl get ns "$NAMESPACE" &>/dev/null; do
            [ "$(date +%s)" -ge "$deadline" ] && break
            sleep 3
        done
    fi
}

# Deploy stress pods (per --stress-profile) + nginx/redis/mysql workloads.
# Triggers ONE scheduling decision (the workload pods land on para-scheduler).
deploy_stress_and_workloads() {
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

    # Stress pods (deployed BEFORE workloads so scheduler sees the heterogeneous
    # resource pressure when placing workload pods). Pinned with .spec.nodeName so
    # they bypass para-scheduler and land deterministically on the target node.
    local stress_counts
    case $STRESS_PROFILE in
        none)   stress_counts=(0 0 0) ;;
        mild)   stress_counts=(0 1 2) ;;
        heavy)  stress_counts=(0 2 4) ;;
    esac

    local total_stress=0
    for c in "${stress_counts[@]}"; do total_stress=$((total_stress + c)); done

    if [ "$total_stress" -gt 0 ]; then
        echo "  Deploying stress pods (profile=$STRESS_PROFILE, total=$total_stress)..."
        for idx in 0 1 2; do
            local cnt=${stress_counts[$idx]}
            local node=${WORKER_NODE_NAMES[$idx]}
            [ "$cnt" -eq 0 ] && continue
            for i in $(seq 0 $((cnt - 1))); do
                cat <<EOF | kubectl apply -f - 2>/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: stress-n${idx}-${i}
  namespace: $NAMESPACE
  labels: {app: stress, group: workload-bench-stress, node-idx: "${idx}"}
spec:
  nodeName: ${node}
  tolerations:
  - operator: Exists
  restartPolicy: Always
  containers:
  - name: stress
    image: polinux/stress-ng:latest
    imagePullPolicy: Never
    args: ["--cpu", "2", "--cpu-load", "100", "--timeout", "0"]
    resources:
      requests: {cpu: "2000m", memory: "512Mi"}
      limits:   {cpu: "2000m", memory: "1Gi"}
EOF
            done
        done
        echo "  Waiting 15s for stress pods to reach steady-state CPU load..."
        sleep 15
    fi

    # nginx (18 replicas, 750m CPU request each → ~6 pods/node × 750m = 4.5 CPU/node)
    echo "  Deploying nginx ($NGINX_REPLICAS replicas)..."
    cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: $NAMESPACE
  labels: {app: nginx, group: workload-bench}
spec:
  replicas: $NGINX_REPLICAS
  selector:
    matchLabels: {app: nginx}
  template:
    metadata:
      labels: {app: nginx, group: workload-bench}
    spec:
      schedulerName: para-scheduler
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: DoesNotExist
      containers:
      - name: nginx
        image: nginx:1.27-alpine
        imagePullPolicy: Never
        ports:
        - containerPort: 80
        resources:
          requests: {cpu: "750m",  memory: 512Mi}
          limits:   {cpu: "1500m", memory: 1Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: nginx
  namespace: $NAMESPACE
spec:
  type: NodePort
  selector: {app: nginx}
  ports:
  - port: 80
    targetPort: 80
    nodePort: 30200
EOF

    # redis (18 replicas, 750m CPU + 1Gi each)
    echo "  Deploying redis ($REDIS_REPLICAS replicas)..."
    cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: $NAMESPACE
  labels: {app: redis, group: workload-bench}
spec:
  replicas: $REDIS_REPLICAS
  selector:
    matchLabels: {app: redis}
  template:
    metadata:
      labels: {app: redis, group: workload-bench}
    spec:
      schedulerName: para-scheduler
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: DoesNotExist
      containers:
      - name: redis
        image: redis:7-alpine
        imagePullPolicy: Never
        ports:
        - containerPort: 6379
        resources:
          requests: {cpu: "750m",  memory: 1Gi}
          limits:   {cpu: "1500m", memory: 2Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: $NAMESPACE
spec:
  type: NodePort
  selector: {app: redis}
  ports:
  - port: 6379
    targetPort: 6379
    nodePort: 30201
EOF

    # mysql (1 replica — stateful; multi-replica behind NodePort doesn't work
    # because sysbench prepare writes tables to one replica, but run may be
    # load-balanced to another replica that has no tables → "Table doesn't exist")
    echo "  Deploying mysql ($MYSQL_REPLICAS replica — stateful, not scaled)..."
    cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql
  namespace: $NAMESPACE
  labels: {app: mysql, group: workload-bench}
spec:
  replicas: $MYSQL_REPLICAS
  selector:
    matchLabels: {app: mysql}
  template:
    metadata:
      labels: {app: mysql, group: workload-bench}
    spec:
      schedulerName: para-scheduler
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: DoesNotExist
      containers:
      - name: mysql
        image: mysql:8.0
        imagePullPolicy: Never
        ports:
        - containerPort: 3306
        env:
        - name: MYSQL_ROOT_PASSWORD
          value: "benchmarkpass"
        - name: MYSQL_DATABASE
          value: "sbtest"
        resources:
          requests: {cpu: 500m, memory: 512Mi}
          limits:   {cpu: "2", memory: 1Gi}
---
apiVersion: v1
kind: Service
metadata:
  name: mysql
  namespace: $NAMESPACE
spec:
  type: NodePort
  selector: {app: mysql}
  ports:
  - port: 3306
    targetPort: 3306
    nodePort: 30202
EOF

    echo "  Waiting for all workloads to be running..."
    kubectl -n "$NAMESPACE" rollout status deployment/nginx --timeout=180s
    kubectl -n "$NAMESPACE" rollout status deployment/redis --timeout=180s
    kubectl -n "$NAMESPACE" rollout status deployment/mysql --timeout=300s
}

# Record placement-quality metrics for the current placement.
# Output: $1/placement-quality.json + $1/pod-distribution.txt
record_placement_quality() {
    local out_dir="$1"
    kubectl -n "$NAMESPACE" get pods -l group=workload-bench -o wide > "$out_dir/pod-distribution.txt"

    NS="$NAMESPACE" python3 <<'PYEOF' > "$out_dir/placement-quality.json"
import json, os, subprocess, statistics, sys

ns = os.environ["NS"]

def kubectl_json(args):
    out = subprocess.check_output(["kubectl"] + args, text=True)
    return json.loads(out)

# Workload pods (benchmarked pods only, exclude stress)
pods = kubectl_json(["-n", ns, "get", "pods",
                     "-l", "group=workload-bench", "-o", "json"])["items"]
nodes = kubectl_json(["get", "nodes", "-o", "json"])["items"]

# Worker-only node set (exclude control-plane per our nodeAffinity)
worker_names = set()
for n in nodes:
    labels = n["metadata"].get("labels", {})
    if "node-role.kubernetes.io/control-plane" not in labels:
        worker_names.add(n["metadata"]["name"])

pod_count_per_node = {}
per_type = {"nginx": {}, "redis": {}, "mysql": {}}
cpu_requested_per_node = {}
mem_requested_per_node = {}

for p in pods:
    node = p["spec"].get("nodeName", "<none>")
    app = p["metadata"]["labels"].get("app", "<unknown>")
    pod_count_per_node[node] = pod_count_per_node.get(node, 0) + 1
    if app in per_type:
        per_type[app][node] = per_type[app].get(node, 0) + 1
    for c in p["spec"].get("containers", []):
        req = c.get("resources", {}).get("requests", {})
        cpu = req.get("cpu", "0")
        mem = req.get("memory", "0")
        if cpu.endswith("m"):
            cpu_val = float(cpu[:-1]) / 1000
        else:
            cpu_val = float(cpu)
        mem_val = 0.0
        if mem.endswith("Ki"):
            mem_val = float(mem[:-2]) / 1024
        elif mem.endswith("Mi"):
            mem_val = float(mem[:-2])
        elif mem.endswith("Gi"):
            mem_val = float(mem[:-2]) * 1024
        cpu_requested_per_node[node] = cpu_requested_per_node.get(node, 0) + cpu_val
        mem_requested_per_node[node] = mem_requested_per_node.get(node, 0) + mem_val

for w in worker_names:
    pod_count_per_node.setdefault(w, 0)
    cpu_requested_per_node.setdefault(w, 0.0)
    mem_requested_per_node.setdefault(w, 0.0)
    for t in per_type:
        per_type[t].setdefault(w, 0)

def gini(values):
    values = sorted(values)
    n = len(values)
    if n == 0 or sum(values) == 0:
        return 0.0
    cum = 0
    for i, v in enumerate(values, 1):
        cum += i * v
    return (2 * cum) / (n * sum(values)) - (n + 1) / n

counts = list(pod_count_per_node.values())
cpu_vals = list(cpu_requested_per_node.values())

result = {
    "pod_count_per_node": pod_count_per_node,
    "pod_count_stddev": statistics.pstdev(counts) if counts else 0.0,
    "pod_count_gini": gini(counts),
    "cpu_requested_per_node": cpu_requested_per_node,
    "cpu_requested_stddev": statistics.pstdev(cpu_vals) if cpu_vals else 0.0,
    "cpu_requested_gini": gini(cpu_vals),
    "mem_requested_per_node_mib": mem_requested_per_node,
    "per_type_distribution": per_type,
    "num_worker_nodes": len(worker_names),
    "total_workload_pods": sum(counts),
}
print(json.dumps(result, indent=2))
PYEOF
}

# Wait until MySQL accepts connections (skipped silently if mysql client absent).
wait_for_mysql_ready() {
    if ! command -v mysql &>/dev/null; then
        return 0
    fi
    local deadline=$(($(date +%s) + 120))
    while true; do
        if mysql -h "$MASTER_IP" -P 30202 -u root --password=benchmarkpass \
                 -e "SELECT 1" &>/dev/null; then
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "  WARNING: MySQL not ready after 120s, proceeding anyway."
            return 0
        fi
        sleep 3
    done
}

# Drop & re-prepare sysbench tables. Required after each redeploy because
# the new MySQL pod starts with an empty database.
sysbench_reset() {
    local out_dir="$1"
    local label="${2:-prepare}"
    command -v sysbench &>/dev/null || return 0
    sysbench oltp_read_write \
        --mysql-host="$MASTER_IP" --mysql-port=30202 \
        --mysql-user=root --mysql-password=benchmarkpass \
        --mysql-db=sbtest \
        --tables=4 --table-size=10000 \
        cleanup > "$out_dir/sysbench-${label}-cleanup.txt" 2>&1 || true
    sysbench oltp_read_write \
        --mysql-host="$MASTER_IP" --mysql-port=30202 \
        --mysql-user=root --mysql-password=benchmarkpass \
        --mysql-db=sbtest \
        --tables=4 --table-size=10000 \
        prepare > "$out_dir/sysbench-${label}-prepare.txt" 2>&1 || true
}

# Run the three concurrent benchmarks (wrk, redis-benchmark, sysbench run).
# Output: $1/{nginx-wrk.txt, redis-benchmark.csv, sysbench-run.txt,
#              cpu-util-*.json, mem-util-*.json}
run_benchmark_suite() {
    local out_dir="$1"
    local bench_start=$(date +%s)
    echo "  Launching nginx + redis + mysql benchmarks concurrently (${BENCH_DURATION}s)..."

    local wrk_pid="" redis_pid="" sysbench_pid=""

    if command -v wrk &>/dev/null; then
        wrk -t4 -c100 -d${BENCH_DURATION}s --latency "http://${MASTER_IP}:30200/" \
            > "$out_dir/nginx-wrk.txt" 2>&1 &
        wrk_pid=$!
    fi

    if command -v redis-benchmark &>/dev/null; then
        local local_ops=$((BENCH_DURATION * 10000))
        redis-benchmark -h "$MASTER_IP" -p 30201 \
            -t SET,GET -n "$local_ops" -c 50 --csv \
            > "$out_dir/redis-benchmark.csv" 2>&1 &
        redis_pid=$!
    fi

    if command -v sysbench &>/dev/null; then
        sysbench oltp_read_write \
            --mysql-host="$MASTER_IP" --mysql-port=30202 \
            --mysql-user=root --mysql-password=benchmarkpass \
            --mysql-db=sbtest \
            --tables=4 --table-size=10000 \
            --threads=8 --time="$BENCH_DURATION" \
            --report-interval=10 \
            run > "$out_dir/sysbench-run.txt" 2>&1 &
        sysbench_pid=$!
    fi

    [ -n "$wrk_pid" ]      && wait "$wrk_pid"      2>/dev/null || true
    [ -n "$redis_pid" ]    && wait "$redis_pid"    2>/dev/null || true
    [ -n "$sysbench_pid" ] && wait "$sysbench_pid" 2>/dev/null || true
    echo "  All benchmarks completed."

    local bench_end=$(date +%s)

    # Collect Prometheus node util snapshots
    echo "  Collecting resource utilization from Prometheus..."
    for node_ip in "${WORKER_NODES[@]}"; do
        local node_label
        node_label=$(echo "$node_ip" | sed 's/\./-/g')
        curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
            --data-urlencode "query=1 - avg(rate(node_cpu_seconds_total{instance=\"${node_ip}:9100\",mode=\"idle\"}[1m]))" \
            --data-urlencode "time=$bench_end" \
            -o "$out_dir/cpu-util-${node_label}.json" 2>/dev/null || true
        curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
            --data-urlencode "query=1 - (node_memory_MemAvailable_bytes{instance=\"${node_ip}:9100\"} / node_memory_MemTotal_bytes{instance=\"${node_ip}:9100\"})" \
            --data-urlencode "time=$bench_end" \
            -o "$out_dir/mem-util-${node_label}.json" 2>/dev/null || true
    done
}

# ============================================================
#  Step 2: Optional warmup (host-level cold-start mitigation)
# ============================================================
if [ "$WARMUP" = true ]; then
    echo ""
    echo "Step 2 (warmup): One throwaway redeploy + benchmark (results discarded)..."
    WARMUP_DIR="$EXPERIMENT_DIR/trial-warmup"
    mkdir -p "$WARMUP_DIR"

    cleanup_workload_namespace
    deploy_stress_and_workloads
    record_placement_quality "$WARMUP_DIR"
    wait_for_mysql_ready
    sysbench_reset "$WARMUP_DIR" "prepare-warmup"
    run_benchmark_suite "$WARMUP_DIR"

    echo "  Warmup complete; discarding $WARMUP_DIR."
    rm -rf "$WARMUP_DIR"
    sleep "$COOLDOWN"
fi

# ============================================================
#  Step 3: Per-trial schedule + benchmark loop (v3)
#  Each iteration is an independent placement sample.
# ============================================================
echo ""
echo "Step 3: Running $TRIALS independent schedule+benchmark trials..."

for trial in $(seq 1 "$TRIALS"); do
    echo ""
    echo "  =========================================="
    echo "  Trial $trial/$TRIALS — redeploy + benchmark"
    echo "  =========================================="
    TRIAL_DIR="$EXPERIMENT_DIR/trial-${trial}"
    mkdir -p "$TRIAL_DIR"

    echo "  [3.1] Cleaning prior workload namespace..."
    cleanup_workload_namespace

    echo "  [3.2] Redeploying stress + workloads (triggers new placement)..."
    deploy_stress_and_workloads

    echo "  [3.3] Recording placement-quality.json..."
    record_placement_quality "$TRIAL_DIR"
    echo "       per-type distribution:"
    python3 -c "
import json
with open('$TRIAL_DIR/placement-quality.json') as f: d = json.load(f)
print('       ' + json.dumps(d['per_type_distribution']))
print('       pod_count_stddev =', round(d['pod_count_stddev'], 3))
print('       pod_count_gini   =', round(d['pod_count_gini'], 4))
" 2>/dev/null || true

    echo "  [3.4] Waiting for MySQL to accept connections..."
    wait_for_mysql_ready

    echo "  [3.5] Sysbench prepare (fresh tables on new MySQL pod)..."
    sysbench_reset "$TRIAL_DIR" "prepare-trial-${trial}"

    echo "  [3.6] Running benchmark suite (${BENCH_DURATION}s)..."
    run_benchmark_suite "$TRIAL_DIR"

    echo "  Trial $trial complete. Cooldown ${COOLDOWN}s..."
    sleep "$COOLDOWN"
done

# ============================================================
#  Step 4: Summary — aggregate per-trial app metrics (mean ± stddev)
# ============================================================
echo ""
echo "Step 4: Generating results summary (mean ± stddev across $TRIALS trials)..."

EXP_DIR="$EXPERIMENT_DIR" TRIALS_N="$TRIALS" python3 <<'PYEOF' > "$EXPERIMENT_DIR/summary.json"
import csv, json, os, re, statistics, sys

exp_dir = os.environ["EXP_DIR"]
N = int(os.environ["TRIALS_N"])

def parse_nginx(path):
    """Parse wrk output: returns dict with requests_per_sec, latency_p50/p90/p99."""
    if not os.path.isfile(path): return None
    out = {"requests_per_sec": None, "latency_avg_ms": None,
           "latency_p50_ms": None, "latency_p90_ms": None,
           "latency_p99_ms": None}
    txt = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"Requests/sec:\s+([\d.]+)", txt)
    if m: out["requests_per_sec"] = float(m.group(1))
    # Latency (Avg / Stdev / Max / +-Stdev)
    m = re.search(r"Latency\s+([\d.]+)(\w+)\s+([\d.]+)(\w+)", txt)
    if m:
        v, u = float(m.group(1)), m.group(2)
        out["latency_avg_ms"] = v * 1000 if u == "s" else (v if u == "ms" else v / 1000)
    # percentile section
    for pct, key in (("50%", "latency_p50_ms"), ("90%", "latency_p90_ms"),
                     ("99%", "latency_p99_ms")):
        m = re.search(r"\s+" + re.escape(pct) + r"\s+([\d.]+)(\w+)", txt)
        if m:
            v, u = float(m.group(1)), m.group(2)
            out[key] = v * 1000 if u == "s" else (v if u == "ms" else v / 1000)
    return out

def parse_redis(path):
    """Parse redis-benchmark CSV: returns {SET_rps, GET_rps}."""
    if not os.path.isfile(path): return None
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2: continue
            test = row[0].strip().strip('"').upper()
            try:
                rps = float(row[1].strip().strip('"'))
            except ValueError:
                continue
            if test in ("SET", "GET"):
                out[f"{test}_rps"] = rps
    return out or None

def parse_sysbench(path):
    """Parse sysbench OLTP run: returns {tps, qps, latency_avg_ms, latency_p95_ms}."""
    if not os.path.isfile(path): return None
    out = {}
    txt = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"transactions:\s+\d+\s+\(([\d.]+)\s+per sec", txt)
    if m: out["tps"] = float(m.group(1))
    m = re.search(r"queries:\s+\d+\s+\(([\d.]+)\s+per sec", txt)
    if m: out["qps"] = float(m.group(1))
    m = re.search(r"\bavg:\s+([\d.]+)", txt)
    if m: out["latency_avg_ms"] = float(m.group(1))
    m = re.search(r"95th percentile:\s+([\d.]+)", txt)
    if m: out["latency_p95_ms"] = float(m.group(1))
    return out or None

def aggregate(values):
    """Return {mean, stddev, n, values} from a list of floats; None if empty."""
    vals = [v for v in values if v is not None]
    if not vals: return None
    return {
        "mean":   round(statistics.mean(vals), 3),
        "stddev": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
        "n":      len(vals),
        "values": [round(v, 3) for v in vals],
    }

def aggregate_metric(parsed_per_trial, key):
    return aggregate([(p or {}).get(key) for p in parsed_per_trial])

# Collect per-trial parsed metrics
nginx_trials, redis_trials, sysbench_trials = [], [], []
placement_trials = []
for i in range(1, N + 1):
    tdir = os.path.join(exp_dir, f"trial-{i}")
    nginx_trials.append(parse_nginx(os.path.join(tdir, "nginx-wrk.txt")))
    redis_trials.append(parse_redis(os.path.join(tdir, "redis-benchmark.csv")))
    sysbench_trials.append(parse_sysbench(os.path.join(tdir, "sysbench-run.txt")))
    pq_path = os.path.join(tdir, "placement-quality.json")
    if os.path.isfile(pq_path):
        try:
            placement_trials.append(json.load(open(pq_path)))
        except Exception:
            placement_trials.append(None)
    else:
        placement_trials.append(None)

summary = {
    "trials": N,
    "nginx": {
        "requests_per_sec": aggregate_metric(nginx_trials, "requests_per_sec"),
        "latency_avg_ms":   aggregate_metric(nginx_trials, "latency_avg_ms"),
        "latency_p50_ms":   aggregate_metric(nginx_trials, "latency_p50_ms"),
        "latency_p90_ms":   aggregate_metric(nginx_trials, "latency_p90_ms"),
        "latency_p99_ms":   aggregate_metric(nginx_trials, "latency_p99_ms"),
    },
    "redis": {
        "SET_rps": aggregate_metric(redis_trials, "SET_rps"),
        "GET_rps": aggregate_metric(redis_trials, "GET_rps"),
    },
    "mysql": {
        "tps":              aggregate_metric(sysbench_trials, "tps"),
        "qps":              aggregate_metric(sysbench_trials, "qps"),
        "latency_avg_ms":   aggregate_metric(sysbench_trials, "latency_avg_ms"),
        "latency_p95_ms":   aggregate_metric(sysbench_trials, "latency_p95_ms"),
    },
    "placement": {
        "pod_count_stddev":   aggregate([(p or {}).get("pod_count_stddev")   for p in placement_trials]),
        "pod_count_gini":     aggregate([(p or {}).get("pod_count_gini")     for p in placement_trials]),
        "cpu_requested_gini": aggregate([(p or {}).get("cpu_requested_gini") for p in placement_trials]),
    },
}
print(json.dumps(summary, indent=2))
PYEOF

# Human-readable summary.txt
cat > "$EXPERIMENT_DIR/summary.txt" <<SUMMARY
Board E Benchmark Summary (v3 per-trial-redeploy)
==================================================
Experiment:     D1-${STRATEGY}-${STRESS_PROFILE}
Strategy:       $STRATEGY
Schedulers:     $NUM_SCHEDULERS
Trials:         $TRIALS  (each = independent redeploy + benchmark)
Duration:       ${BENCH_DURATION}s per trial benchmark
Stress profile: $STRESS_PROFILE
Replicas:       nginx=$NGINX_REPLICAS, redis=$REDIS_REPLICAS, mysql=$MYSQL_REPLICAS

Aggregated metrics (mean ± stddev across $TRIALS trials, see summary.json):
$(python3 -c "
import json
with open('$EXPERIMENT_DIR/summary.json') as f: s = json.load(f)
def fmt(d):
    if d is None: return '       (no data)'
    return f\"       mean={d['mean']:.2f}  stddev={d['stddev']:.2f}  n={d['n']}  values={d['values']}\"
def section(name, group):
    print(f'  [{name}]')
    for k, v in group.items():
        print(f'    {k}:')
        print(fmt(v))
section('nginx (wrk)', s['nginx'])
section('redis (redis-benchmark)', s['redis'])
section('mysql (sysbench)', s['mysql'])
section('placement (per-trial)', s['placement'])
" 2>/dev/null || echo "  (summary.json parsing failed)")

Per-trial placement (pod count per node):
$(for trial in $(seq 1 "$TRIALS"); do
    pq="$EXPERIMENT_DIR/trial-${trial}/placement-quality.json"
    if [ -f "$pq" ]; then
        echo "  Trial $trial:"
        python3 -c "
import json
d = json.load(open('$pq'))
print('    pod_count_per_node = ' + json.dumps(d['pod_count_per_node']))
print('    per_type           = ' + json.dumps(d['per_type_distribution']))
" 2>/dev/null
    fi
done)

SUMMARY

# Append raw per-trial outputs (for debugging)
if [ -f "$EXPERIMENT_DIR/trial-1/nginx-wrk.txt" ]; then
    echo "Raw nginx (wrk) per trial:" >> "$EXPERIMENT_DIR/summary.txt"
    for trial in $(seq 1 "$TRIALS"); do
        echo "  Trial $trial:" >> "$EXPERIMENT_DIR/summary.txt"
        grep -E "Requests/sec|Latency|Transfer" "$EXPERIMENT_DIR/trial-${trial}/nginx-wrk.txt" \
            >> "$EXPERIMENT_DIR/summary.txt" 2>/dev/null || true
    done
    echo "" >> "$EXPERIMENT_DIR/summary.txt"
fi

if [ -f "$EXPERIMENT_DIR/trial-1/redis-benchmark.csv" ]; then
    echo "Raw redis (redis-benchmark) per trial:" >> "$EXPERIMENT_DIR/summary.txt"
    for trial in $(seq 1 "$TRIALS"); do
        echo "  Trial $trial:" >> "$EXPERIMENT_DIR/summary.txt"
        head -5 "$EXPERIMENT_DIR/trial-${trial}/redis-benchmark.csv" \
            >> "$EXPERIMENT_DIR/summary.txt" 2>/dev/null || true
    done
    echo "" >> "$EXPERIMENT_DIR/summary.txt"
fi

if [ -f "$EXPERIMENT_DIR/trial-1/sysbench-run.txt" ]; then
    echo "Raw mysql (sysbench) per trial:" >> "$EXPERIMENT_DIR/summary.txt"
    for trial in $(seq 1 "$TRIALS"); do
        echo "  Trial $trial:" >> "$EXPERIMENT_DIR/summary.txt"
        grep -A5 "SQL statistics\|Latency\|transactions:" "$EXPERIMENT_DIR/trial-${trial}/sysbench-run.txt" \
            >> "$EXPERIMENT_DIR/summary.txt" 2>/dev/null || true
    done
fi

# ============================================================
#  Step 5: Final cleanup
# ============================================================
echo ""
echo "Step 5: Cleaning up workloads + stress pods..."
cleanup_workload_namespace
echo "  Namespace cleanup complete."

echo ""
echo "============================================"
echo "Benchmark completed!"
echo "Results saved to: $EXPERIMENT_DIR"
echo "Summary:          $EXPERIMENT_DIR/summary.txt"
echo "Aggregated JSON:  $EXPERIMENT_DIR/summary.json"
echo "============================================"
