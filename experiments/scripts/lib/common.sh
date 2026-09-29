# Shared by the experiment runners and batch drivers. Source it; do not run it:
#
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$SCRIPT_DIR/lib/common.sh"
#
# shellcheck shell=bash

# ============================================================
# Site configuration
# ============================================================
# Values that belong to one lab's cluster are not written into the scripts.
# They come from the environment or from experiments/site.env, which sets the
# ones the environment leaves unset; experiments/site.env.example lists them:
#
#   PROMETHEUS_URL   Prometheus endpoint the metric collectors query
#   CLUSTER_SUBNET   node subnet, kept off any shell-level HTTP(S) proxy
#   ETCD_POD         etcd static pod that etcd-maintenance.sh maintains
#   MASTER_IP, WORKER_IPS, WORKER_NAMES, SSH_USER
#                    the physical nodes of the application-layer benchmark

EXPERIMENTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_ENV="${SITE_ENV:-$EXPERIMENTS_DIR/site.env}"
if [ -f "$SITE_ENV" ]; then
    # shellcheck source=/dev/null
    . "$SITE_ENV"
fi

# Bypass any shell-level HTTP(S) proxy (e.g. a local proxy on 127.0.0.1:7890)
# for the cluster network and in-cluster names. Without this, kubectl and curl
# honor HTTPS_PROXY and fail with "proxyconnect" errors when the proxy is
# offline. Child processes inherit the setting.
export_cluster_no_proxy() {
    export NO_PROXY="${NO_PROXY:+$NO_PROXY,}${CLUSTER_SUBNET:+$CLUSTER_SUBNET,}127.0.0.1,localhost,kubernetes.default,kubernetes.default.svc,.svc,.svc.cluster.local"
    export no_proxy="$NO_PROXY"
}

# ============================================================
# Where runs are written
# ============================================================
# The batch drivers write each run into its board's directory under the
# results root, which is the layout the analysis reads (reduce.py --results
# <root>): the directory experiments/registry.json names for a registry board,
# the group's own name for runs outside the registry. Warmup runs go to a
# scratch directory under the root that is removed after each warmup.
RESULTS_ROOT="${RESULTS_ROOT:-$EXPERIMENTS_DIR/results}"

# registry_dir BOARD — the results directory of a registry board.
registry_dir() {
    local directory
    directory=$(python3 "$EXPERIMENTS_DIR/common/registry.py" --directory "$1") || return 1
    echo "$RESULTS_ROOT/$directory"
}

# warmup_dir — where throwaway warmup runs are written, and removed from.
warmup_dir() {
    echo "${RESULTS_ROOT:?}/warmup"
}

# ============================================================
# Cluster lifecycle
# ============================================================
# The drivers and runners each kept their own copies of these; the commands a
# helper issues are the ones those copies issued. Where a pipeline's status
# decides what is reported, it is read from PIPESTATUS: `if ! cmd | tail` tests
# tail's status, which is always 0, and so never sees cmd fail.

# delete_kwok_leases — delete the node leases that deleted KWOK nodes leave
# behind. Recreated nodes get new lease UIDs that KWOK does not know, and the
# mismatch fails lease renewal and marks the nodes NotReady.
delete_kwok_leases() {
    local leases
    leases=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    if [ -n "$leases" ]; then
        echo "$leases" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
    fi
}

# wait_for_clean_cluster — wait for terminating CL2 namespaces (test-*) to go;
# after 180 s, clear their finalizers so that they do.
wait_for_clean_cluster() {
    local limit=180 start terminating cl2_ns elapsed ns
    start=$(date +%s)
    while true; do
        # A failed query reads as nothing terminating, as it always has.
        terminating=$(kubectl get ns --field-selector=status.phase=Terminating -o jsonpath='{.items[*].metadata.name}' 2>/dev/null) || true
        cl2_ns=""
        for ns in $terminating; do
            case "$ns" in test-*) cl2_ns="$cl2_ns $ns" ;; esac
        done
        cl2_ns=$(echo "$cl2_ns" | xargs)
        [ -z "$cl2_ns" ] && return 0
        elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$limit" ]; then
            echo "  WARNING: ${limit}s timeout — force-finalizing remaining CL2 namespaces..."
            for ns in $cl2_ns; do
                kubectl get ns "$ns" -o json 2>/dev/null \
                    | jq '.spec.finalizers = []' \
                    | kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
            done
            sleep 5; return 0
        fi
        echo "  Waiting for $(echo "$cl2_ns" | wc -w | xargs) CL2 namespaces to terminate... (${elapsed}s/${limit}s)"
        sleep 10
    done
}

# defrag_etcd — compact and defragment etcd after an experiment. Compacting
# first matters: the API server compacts only every 5 minutes, and defrag can
# reclaim nothing that has not been compacted.
defrag_etcd() {
    local rc
    echo "----------------------------------------"
    echo "etcd compact + defrag (post-experiment maintenance)"
    echo "----------------------------------------"
    bash "$EXPERIMENTS_DIR/scripts/etcd-maintenance.sh" --full 2>&1 | tail -15
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: etcd maintenance failed (rc=$rc); continuing."
    fi
}

# Warmup. A throwaway one-trial run, at full saturation, whenever the next
# experiment's node count, capacity variance or scheduler count differs from
# the last warmup's: node creation and a changed scheduler count leave caches
# cold, and the first real trial would otherwise pay for it. WARMUP_RUNNER picks
# the runner, para (run-experiment.sh, the default) or godel.
LAST_WARMUP_NODES=""
LAST_WARMUP_VARIANCE=""
LAST_WARMUP_SCHEDS=""

# warmup_at_nodes NODES [SCHEDS] — one warmup run; its results are discarded.
warmup_at_nodes() {
    local nodes=$1 scheds=${2:-10} variance="${CURRENT_VARIANCE:-$VARIANCE}" runner args rc
    echo "----------------------------------------"
    echo "Warmup at ${nodes} nodes, N=${scheds}, V=${variance} (full saturation, results discarded)"
    echo "----------------------------------------"
    if [ "${WARMUP_RUNNER:-para}" = godel ]; then
        runner=run-godel-baseline.sh
        args=(--name "godel-warmup-${nodes}n-N${scheds}" --results-dir "$(warmup_dir)"
              --nodes "$nodes" --schedulers "$scheds" --trials 1
              --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi
              --variance "$variance" --prometheus-url "$PROMETHEUS_URL")
    else
        runner=run-experiment.sh
        args=(--name "B-warmup-${nodes}n-N${scheds}" --results-dir "$(warmup_dir)"
              --nodes "$nodes" --schedulers "$scheds"
              --backup 0 --penalty 0.0 --strategy "QualityFirst"
              --sync-period 0.1 --partitions 1 --sync-pattern diff --trials 1
              --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi
              --variance "$variance")
    fi
    "$EXPERIMENTS_DIR/scripts/$runner" "${args[@]}" --preserve-nodes 2>&1 | tail -30
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
        echo "  Warmup OK (${nodes}n N=${scheds} V=${variance})"
    else
        echo "  Warmup FAILED at ${nodes}n N=${scheds} V=${variance} (rc=$rc; continuing — real experiments will still run)"
    fi
    rm -rf "$(warmup_dir)"
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
    LAST_WARMUP_NODES="$nodes"
    LAST_WARMUP_VARIANCE="$variance"
    LAST_WARMUP_SCHEDS="$scheds"
}

# maybe_warmup_before NODES [SCHEDS] — warm up if the node count, variance or
# scheduler count differs from the last warmup's. With SKIP_WARMUP=true it only
# records the state.
maybe_warmup_before() {
    local nodes=$1 scheds=${2:-10} variance="${CURRENT_VARIANCE:-$VARIANCE}"
    if [ "$SKIP_WARMUP" = true ]; then
        LAST_WARMUP_NODES="$nodes"
        LAST_WARMUP_VARIANCE="$variance"
        LAST_WARMUP_SCHEDS="$scheds"
        return 0
    fi
    if [ "$LAST_WARMUP_NODES" != "$nodes" ] \
       || [ "$LAST_WARMUP_VARIANCE" != "$variance" ] \
       || [ "$LAST_WARMUP_SCHEDS" != "$scheds" ]; then
        echo ">> Scale/V/N change: last=(${LAST_WARMUP_NODES:-<none>}n V=${LAST_WARMUP_VARIANCE:-<none>} N=${LAST_WARMUP_SCHEDS:-<none>}), next=(${nodes}n V=${variance} N=${scheds}) — inserting warmup."
        warmup_at_nodes "$nodes" "$scheds"
    fi
}

# final_cleanup SETUP_SCRIPT — at the end of a batch: delete every KWOK node and
# its lease, reset the scheduler components with `SETUP_SCRIPT --clean`, and
# defragment etcd after the deletion storm, which leaves tombstones the
# per-experiment defrag never sees.
final_cleanup() {
    local setup_sh=$1
    echo "========================================"
    echo "Final cleanup (script end)"
    echo "========================================"
    echo "Deleting all KWOK nodes..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    delete_kwok_leases
    echo "Invoking $setup_sh --clean to reset the scheduler components..."
    if [ -x "$setup_sh" ]; then
        bash "$setup_sh" --clean 2>&1 | tail -10 || true
    else
        echo "  (setup.sh not found at $setup_sh, skipping)"
    fi
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "Cluster is clean. Script ending."
}

# settle_briefly SECONDS [INDENT] — wait until no CL2 test pods remain and the
# API server answers within 5 s, polling every 5 s, for at most SECONDS. For the
# short pauses around trials, when only one trial's pods are being collected.
settle_briefly() {
    local indent=${2:-  } deadline pods api
    deadline=$(( $(date +%s) + $1 ))
    while true; do
        pods=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
        api=false
        if timeout 5 kubectl get ns default >/dev/null 2>&1; then api=true; fi
        if [ "$pods" -eq 0 ] && [ "$api" = true ]; then
            echo "${indent}Cluster settled."
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "${indent}Settle timeout — proceeding."
            return 0
        fi
        sleep 5
    done
}

# settle_fully SECONDS — after an experiment's cleanup: wait until no KWOK nodes
# and no CL2 test pods remain and the API server answers within 5 s, polling
# every 10 s with progress, for at most SECONDS. After a large cleanup etcd can
# still be working through deletions when the objects are already gone.
settle_fully() {
    local limit=$1 start elapsed nodes pods api
    start=$(date +%s)
    while true; do
        elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$limit" ]; then
            echo "  Settle timeout (${limit}s) — proceeding anyway."
            return 0
        fi
        nodes=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
        pods=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
        api=false
        if timeout 5 kubectl get ns default >/dev/null 2>&1; then api=true; fi
        if [ "$nodes" -eq 0 ] && [ "$pods" -eq 0 ] && [ "$api" = true ]; then
            echo "  Cluster settled (${elapsed}s): 0 KWOK nodes, 0 test pods, API responsive."
            return 0
        fi
        echo "  Settling... (${elapsed}s) kwok_nodes=$nodes test_pods=$pods api_ok=$api"
        sleep 10
    done
}
