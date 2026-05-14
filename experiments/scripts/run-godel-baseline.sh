#!/bin/bash

# Automated experiment runner for Godel baseline (E1 / E2).
#
# Stripped-down parallel of run-experiment.sh: skips para-sched-only steps
# (dispatcher patching, ParSync label warm-up, scheduler/penalty/strategy flags)
# while reusing the same KWOK + CL2 + Prometheus infrastructure.
#
# Usage:
#   ./run-godel-baseline.sh --name b2-godel-5k-N10 --nodes 5000 --schedulers 10 --trials 3
#   ./run-godel-baseline.sh --name b2-godel-5k-N1  --nodes 5000 --schedulers 1  --trials 3
#
# Defaults align with experiment-design.md §6.2 HC-V (M=1, V=0.6) scenario.

set -e

# Bypass any HTTP(S) proxy for in-cluster traffic (kubectl, curl to Prometheus).
# Necessary when shell-level HTTPS_PROXY points at e.g. Clash on 127.0.0.1:7890
# that isn't running — kubectl would otherwise fail with proxyconnect errors.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}<YOUR_CLUSTER_SUBNET>/24,127.0.0.1,localhost,kubernetes.default,kubernetes.default.svc,.svc,.svc.cluster.local"
export no_proxy="$NO_PROXY"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RESULTS_DIR="$PROJECT_ROOT/experiments/results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Defaults — HC-V (M=1, V=0.6) is the baseline scenario for E1/E2.
EXPERIMENT_NAME=""
NUM_NODES=5000
NUM_SCHEDULERS=10
NUM_TRIALS=3
PODS_PER_NODE=1
CPU_REQUEST="24000m"      # HC-V pod: 24 CPU on heterogeneous shards [24,47]
MEMORY_REQUEST="192Gi"
VARIANCE="0.6"            # HC-V capacity-variance level
PROMETHEUS_URL="http://${MONITORING_IP:-<MONITORING_IP>}:9091"
COLLECT_LOGS=false
PRESERVE_NODES=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --name)            EXPERIMENT_NAME="$2";  shift 2 ;;
        --nodes)           NUM_NODES="$2";        shift 2 ;;
        --schedulers)      NUM_SCHEDULERS="$2";   shift 2 ;;
        --trials)          NUM_TRIALS="$2";       shift 2 ;;
        --pods-per-node)   PODS_PER_NODE="$2";    shift 2 ;;
        --cpu-request)     CPU_REQUEST="$2";      shift 2 ;;
        --memory-request)  MEMORY_REQUEST="$2";   shift 2 ;;
        --variance)        VARIANCE="$2";         shift 2 ;;
        --prometheus-url)  PROMETHEUS_URL="$2";   shift 2 ;;
        --collect-logs)    COLLECT_LOGS=true;     shift ;;
        --preserve-nodes)  PRESERVE_NODES=true;   shift ;;
        -h|--help)
            cat <<EOF
Usage: $0 --name <NAME> [options]

Required:
  --name NAME           Experiment name (used in results dir)

Cluster shape:
  --nodes N             KWOK node count                 (default: 5000)
  --schedulers N        Number of Godel scheduler instances (1=E1, 10=E2, default: 10)
  --trials N            Independent trials per config   (default: 3)

Workload (HC-V defaults):
  --pods-per-node N     Pods per node                   (default: 1)
  --cpu-request V       CPU per pod                     (default: 24000m)
  --memory-request V    Memory per pod                  (default: 192Gi)
  --variance V          Capacity variance               (default: 0.6)

Other:
  --prometheus-url URL  Prometheus URL                  (default: http://${MONITORING_IP:-<MONITORING_IP>}:9091)
  --collect-logs        Save component logs per trial
  --preserve-nodes      Skip KWOK node create/delete (reuse existing)
EOF
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$EXPERIMENT_NAME" ]; then
    echo "ERROR: --name is required"; exit 1
fi
if [ "$NUM_SCHEDULERS" -lt 1 ] || [ "$NUM_SCHEDULERS" -gt 10 ]; then
    echo "ERROR: --schedulers must be in [1, 10] (godel deploy/lab-cluster MAX_SCHEDULERS=10)"; exit 1
fi

# ============================================================
# Paths and shared variables
# ============================================================
DEPLOY_DIR="$PROJECT_ROOT/godel-scheduler/deploy/lab-cluster"
NAMESPACE="godel-system"
CL2_BIN="$PROJECT_ROOT/bin/clusterloader"
CONFIG_DIR="$PROJECT_ROOT/experiments/kwok-setup"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}"

EXPERIMENT_DIR="$RESULTS_DIR/${TIMESTAMP}-${EXPERIMENT_NAME}-godel-N${NUM_SCHEDULERS}-${NUM_NODES}n"
mkdir -p "$EXPERIMENT_DIR"

cat > "$EXPERIMENT_DIR/config.json" <<EOF
{
  "experiment_name": "${EXPERIMENT_NAME}",
  "baseline": "godel",
  "num_nodes": ${NUM_NODES},
  "num_schedulers": ${NUM_SCHEDULERS},
  "num_trials": ${NUM_TRIALS},
  "pods_per_node": ${PODS_PER_NODE},
  "cpu_request": "${CPU_REQUEST}",
  "memory_request": "${MEMORY_REQUEST}",
  "variance": "${VARIANCE}",
  "prometheus_url": "${PROMETHEUS_URL}",
  "timestamp": "${TIMESTAMP}"
}
EOF

echo "============================================"
echo "Godel Baseline Experiment: ${EXPERIMENT_NAME}"
echo "============================================"
echo "  Results:    ${EXPERIMENT_DIR}"
echo "  Nodes:      ${NUM_NODES}    Schedulers: ${NUM_SCHEDULERS}    Trials: ${NUM_TRIALS}"
echo "  Workload:   ppn=${PODS_PER_NODE} cpu=${CPU_REQUEST} mem=${MEMORY_REQUEST} variance=${VARIANCE}"
echo ""

# ============================================================
# Helper: parse a unix timestamp from a CL2 log step marker.
# (Identical to run-experiment.sh's parse_cl2_ts.)
# ============================================================
parse_cl2_ts() {
    local log_file=$1 step_re=$2 event=$3
    local line
    line=$(grep -E "Step.*${step_re}.*${event}" "$log_file" | head -1)
    [ -z "$line" ] && { echo ""; return; }
    local ts_field time_field
    ts_field=$(echo "$line" | awk '{print $1}')
    time_field=$(echo "$line" | awk '{print $2}')
    local month=${ts_field:1:2} day=${ts_field:3:2}
    local time=${time_field%%.*}
    local year
    year=$(date +%Y)
    TZ=UTC date -d "${year}-${month}-${day} ${time}" +%s 2>/dev/null || echo ""
}

collect_phase_metrics() {
    local trial_dir=$1 phase=$2 start=$3 end=$4
    if [ -z "$start" ] || [ -z "$end" ] || [ "$start" -ge "$end" ]; then
        echo "    WARN: could not parse ${phase} phase timestamps, skipping"
        return
    fi
    echo "    ${phase}: $(date -d @${start} +%H:%M:%S) → $(date -d @${end} +%H:%M:%S) ($((end - start))s)"
    "$SCRIPT_DIR/collect-metrics-godel.sh" \
        --output "$trial_dir/metrics-${phase}" \
        --start "$start" \
        --end "$end" \
        --snap-end-pad 30 \
        --prometheus-url "$PROMETHEUS_URL" \
        || echo "    WARN: metrics collection failed for ${phase} phase (non-fatal)"
}

wait_for_godel_components() {
    echo "  Waiting for godel components ready..."
    kubectl -n "$NAMESPACE" rollout status deployment/binder --timeout=120s
    kubectl -n "$NAMESPACE" rollout status deployment/dispatcher --timeout=120s
    kubectl -n "$NAMESPACE" rollout status deployment/controller-manager --timeout=120s
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" rollout status "deployment/godel-scheduler-${i}" --timeout=120s
    done

    # Wait for the schedulers CRD to reflect the live scheduler count.
    # Reset between trials deletes these entries; we must wait for them to re-register
    # before dispatching, otherwise the dispatcher's scheduler-maintainer may see
    # fewer than NUM_SCHEDULERS and route unevenly for the first few seconds.
    local deadline=$(($(date +%s) + 60))
    while true; do
        local registered
        registered=$(kubectl get schedulers.scheduling.godel.kubewharf.io --no-headers 2>/dev/null | wc -l)
        if [ "$registered" -ge "$NUM_SCHEDULERS" ]; then
            echo "  ${registered}/${NUM_SCHEDULERS} schedulers registered to CRD."
            break
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "  WARNING: only ${registered}/${NUM_SCHEDULERS} registered after 60s, proceeding"
            break
        fi
        sleep 2
    done
}

reset_godel_state() {
    echo "  Resetting state for next trial..."
    "$DEPLOY_DIR/setup.sh" --clean || echo "  WARNING: setup.sh --clean failed (non-fatal)"
}

# ============================================================
# Step 0: Verify godel deployment matches NUM_SCHEDULERS
# ============================================================
echo "Step 0: Verifying godel deployment..."
CURRENT_N=$(kubectl -n "$NAMESPACE" get deployments -l app=godel-scheduler --no-headers 2>/dev/null | wc -l)
if [ "$CURRENT_N" -ne "$NUM_SCHEDULERS" ]; then
    echo "  Current scheduler count = ${CURRENT_N}, target = ${NUM_SCHEDULERS}. Scaling..."
    "$DEPLOY_DIR/setup.sh" --scale "$NUM_SCHEDULERS"
else
    echo "  Already at N=${NUM_SCHEDULERS}."
fi
echo ""

# ============================================================
# Step 1: Create KWOK nodes (skip if --preserve-nodes and count matches)
# ============================================================
echo "Step 1: Ensuring ${NUM_NODES} KWOK nodes..."
CURRENT_KWOK=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
if [ "$PRESERVE_NODES" = true ] && [ "$CURRENT_KWOK" -eq "$NUM_NODES" ]; then
    echo "  --preserve-nodes: ${CURRENT_KWOK} nodes already present, skipping create."
elif [ "$CURRENT_KWOK" -eq "$NUM_NODES" ]; then
    echo "  ${CURRENT_KWOK} KWOK nodes already present, skipping create."
else
    if [ "$CURRENT_KWOK" -gt 0 ]; then
        echo "  Found ${CURRENT_KWOK} stale KWOK nodes — deleting before fresh create."
        kubectl delete nodes -l type=kwok --ignore-not-found --wait=true --timeout=300s 2>/dev/null || true
    fi
    "$SCRIPT_DIR/create-nodes.sh" --nodes "$NUM_NODES" --variance "$VARIANCE"
fi
echo ""

# ============================================================
# Trial loop
# ============================================================
TOTAL_START_TS=$(date +%s)

for TRIAL in $(seq 1 "$NUM_TRIALS"); do
    [ "$NUM_TRIALS" -gt 1 ] && { echo ""; echo "==== Trial $TRIAL/$NUM_TRIALS ===="; }
    TRIAL_DIR="$EXPERIMENT_DIR/trial-${TRIAL}"
    mkdir -p "$TRIAL_DIR"

    # Step 3: wait for components
    echo "Step 3: Waiting for components..."
    wait_for_godel_components

    # Step 3a: API settle — verify no leftover saturation pods, API responsive
    echo "  Waiting for API server to settle..."
    SETTLE_DEADLINE=$(($(date +%s) + 60))
    while true; do
        REMAINING=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
        API_OK=false
        timeout 5 kubectl get ns default >/dev/null 2>&1 && API_OK=true
        if [ "$REMAINING" -eq 0 ] && [ "$API_OK" = true ]; then break; fi
        if [ "$(date +%s)" -ge "$SETTLE_DEADLINE" ]; then echo "  Settle timeout — proceeding."; break; fi
        sleep 5
    done

    START_TS=$(date +%s)

    # Step 4: Run CL2 with godel-flavored kwok-deployment template
    CL2_OVERRIDES_FILE=$(mktemp /tmp/cl2-overrides-godel-XXXXXX.yaml)
    {
        echo "PODS_PER_NODE: ${PODS_PER_NODE}"
        echo "SATURATION_POD_CPU: \"${CPU_REQUEST}\""
        echo "SATURATION_POD_MEMORY: \"${MEMORY_REQUEST}\""
        # Point CL2 to the godel-flavored deployment template for BOTH
        # saturation and latency phases. cl2-schedule-pods.yaml (used when
        # ppn > 1, i.e. B1 Low-contention) creates latency pods via a separate
        # LATENCY_DEPLOYMENT_SPEC param — without this override, latency pods
        # would default to kwok-deployment.yaml whose schedulerName=para-scheduler,
        # leaving them Pending until the 15-min timeout (Godel doesn't claim them).
        # cl2-saturation-only.yaml (used when ppn=1) ignores the latency override
        # harmlessly.
        echo "SATURATION_DEPLOYMENT_SPEC: kwok-deployment-godel.yaml"
        echo "LATENCY_DEPLOYMENT_SPEC: kwok-deployment-godel.yaml"
        # Throughput thresholds — mirror run-experiment.sh
        if [ "$PODS_PER_NODE" -le 1 ]; then
            echo "DENSITY_TEST_THROUGHPUT: 10"
        elif [ "$PODS_PER_NODE" -le 5 ]; then
            echo "DENSITY_TEST_THROUGHPUT: 20"
        fi
    } > "$CL2_OVERRIDES_FILE"

    if [ "$PODS_PER_NODE" -le 1 ]; then
        CL2_CONFIG="$CONFIG_DIR/cl2-saturation-only.yaml"
        echo "Step 4: Scheduling pods (HC-1/HC-V saturation-only mode)..."
    else
        CL2_CONFIG="$CONFIG_DIR/cl2-schedule-pods.yaml"
        echo "Step 4: Scheduling pods (batch mode)..."
    fi

    set +o errexit
    set -o pipefail
    "$CL2_BIN" \
        --testconfig="$CL2_CONFIG" \
        --provider=local \
        --provider-configs=ROOT_KUBECONFIG="$KUBECONFIG_PATH" \
        --kubeconfig="$KUBECONFIG_PATH" \
        --v=2 \
        --enable-exec-service=false \
        --enable-prometheus-server=false \
        --nodes="$NUM_NODES" \
        --testoverrides="$CL2_OVERRIDES_FILE" \
        2>&1 | tee "$TRIAL_DIR/cl2.log"
    CL2_EXIT=$?
    set +o pipefail
    set -o errexit

    [ "$CL2_EXIT" -ne 0 ] && echo "  WARNING: CL2 exited $CL2_EXIT (test failures, continuing)"
    rm -f "$CL2_OVERRIDES_FILE"

    if [ -f "junit.xml" ]; then
        cp junit.xml "$TRIAL_DIR/junit.xml"; rm -f junit.xml
    fi

    END_TS=$(date +%s)

    # Step 5: Parse CL2 log timestamps + per-phase metric collection
    echo ""
    echo "Step 5: Collecting metrics..."
    CL2_LOG="$TRIAL_DIR/cl2.log"
    SAT_START=$(parse_cl2_ts "$CL2_LOG" "Creating saturation pods" "started")
    SAT_END=$(parse_cl2_ts "$CL2_LOG" "Waiting for saturation pods to be running" "ended")
    LAT_START=$(parse_cl2_ts "$CL2_LOG" "Creating latency pods" "started")
    LAT_END=$(parse_cl2_ts "$CL2_LOG" "Waiting for latency pods to be running" "ended")

    collect_phase_metrics "$TRIAL_DIR" "saturation" "$SAT_START" "$SAT_END"
    collect_phase_metrics "$TRIAL_DIR" "latency"    "$LAT_START" "$LAT_END"

    if [ "$COLLECT_LOGS" = true ]; then
        echo "  Collecting component logs..."
        LOGS_DIR="$TRIAL_DIR/logs"
        mkdir -p "$LOGS_DIR"
        SINCE_TIME=$(date -u -d "@$START_TS" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$START_TS" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
        for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
            kubectl -n "$NAMESPACE" logs "deployment/godel-scheduler-${i}" \
                --since-time="$SINCE_TIME" --tail=-1 > "$LOGS_DIR/scheduler-${i}.log" 2>&1 || true
        done
        kubectl -n "$NAMESPACE" logs deployment/binder     --since-time="$SINCE_TIME" --tail=-1 > "$LOGS_DIR/binder.log"     2>&1 || true
        kubectl -n "$NAMESPACE" logs deployment/dispatcher --since-time="$SINCE_TIME" --tail=-1 > "$LOGS_DIR/dispatcher.log" 2>&1 || true
    fi

    cat > "$TRIAL_DIR/timing.json" <<TIMING
{
  "trial": $TRIAL,
  "overall": {"start": $START_TS, "end": $END_TS, "duration": $((END_TS - START_TS))},
  "saturation": {"start": ${SAT_START:-null}, "end": ${SAT_END:-null}},
  "latency": {"start": ${LAT_START:-null}, "end": ${LAT_END:-null}}
}
TIMING

    echo "  Trial $TRIAL completed in $((END_TS - START_TS))s."

    if [ "$TRIAL" -lt "$NUM_TRIALS" ]; then
        echo ""
        reset_godel_state
    fi
done

# ============================================================
# Step 6: Final cleanup
# ============================================================
echo ""
echo "Step 6: Final cleanup..."

kubectl delete deployments -l group=saturation --all-namespaces --ignore-not-found=true --wait=false 2>/dev/null || true
kubectl delete deployments -l group=latency    --all-namespaces --ignore-not-found=true --wait=false 2>/dev/null || true

echo "  Force-deleting remaining test pods..."
CL2_NAMESPACES=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep '^test-' || true)
for ns in $CL2_NAMESPACES; do
    kubectl delete pods --all -n "$ns" --force --grace-period=0 --ignore-not-found=true 2>/dev/null &
done
wait

echo "  Waiting for test pods to be fully removed..."
POD_WAIT_DEADLINE=$(($(date +%s) + 180))
while true; do
    REMAINING=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
    [ "$REMAINING" -eq 0 ] && { echo "  All test pods removed."; break; }
    [ "$(date +%s)" -ge "$POD_WAIT_DEADLINE" ] && { echo "  Cleanup timeout, ${REMAINING} remaining."; break; }
    sleep 10
done

if [ "$PRESERVE_NODES" = true ]; then
    echo "  --preserve-nodes: keeping KWOK nodes for next experiment."
else
    echo "  Deleting KWOK nodes..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    KWOK_LEASES=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    [ -n "$KWOK_LEASES" ] && echo "$KWOK_LEASES" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
fi

echo "  Cleaning CL2 test namespaces..."
kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep '^test-' \
    | xargs -r -I{} kubectl delete ns {} --ignore-not-found=true --wait=false 2>/dev/null || true

reset_godel_state

TOTAL_END_TS=$(date +%s)
echo ""
echo "============================================"
echo "Experiment complete in $((TOTAL_END_TS - TOTAL_START_TS))s."
echo "Results: ${EXPERIMENT_DIR}"
echo "============================================"
