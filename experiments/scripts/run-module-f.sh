#!/usr/bin/env bash

# Unified experiment entry point for Module F.
#
# Current implementation (parameters match the Board B production configuration; see experiment-design.md, Board F):
#   Event-driven (3 repetitions, low between-trial variance): E2 (vanilla-E, K=0 / w=0), E3 (ParKour-E, K=2 / w=0.5)
#     3 runs each under Z0 and Dreal, 12 rounds total (F1-E);
#     the same two methods under Dreal-F1 (anchored four-bucket latency + 1% startup failure), 3 runs each, 6 rounds total (F2-E).
#   Periodic (globSync, partitions=1; 5 repetitions, high between-trial variance): P1 (vanilla-P), P4 (ParKour-P, K=2 / w=0.5)
#     5 runs each under Z0 and Dreal, 20 rounds total (F1-P);
#     the same two methods under Dreal-F1, 5 runs each, 10 rounds total (F2-P).
#   48 rounds in total. The repetition count can be overridden with --trials.
#   The E2-G (native Godel) runner code path is kept but has been removed from the formal matrix.
#
#   Invalid rounds are classified before any retry: only "the injection-rate gate failed to cover 1%"
#   is retryable (at most 2 attempts per round, 4 per matrix); every other hard failure aborts immediately.
#
# Safety conventions:
#   1. Dry run by default: it only prints a fixed-seed execution plan.
#   2. Only an explicit --execute touches the cluster and runs experiments.
#   3. It never rewrites existing runners, CL2 templates or the base kwok-config.yaml.
#   4. In execute mode it creates only temporary runners and templates inside the result directory, and temporarily takes over the KWOK shards.
#   5. An EXIT trap stops the temporary KWOK processes and restores the original systemd service state.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

EXPERIMENT="F1-E"
MODE="dry-run"
PLAN_SEED=20260907
# Empty means "use the default for the synchronization paradigm" (3 for event-driven, 5 for periodic).
TRIALS=""
NUM_NODES=10000
NUM_SCHEDULERS=10
TARGET_PODS=10000
PODS_PER_NODE=1
CPU_REQUEST="24000m"
MEMORY_REQUEST="192Gi"
VARIANCE="0.6"
# Synchronization parameters match the Board B production configuration: event-driven uses partitions=1 + diff, periodic uses partitions=1 + glob.
# The event and periodic settings are independent and must not share one set of variables (an earlier implementation had the event mode pick up the periodic partitions).
EVENT_SYNC_PERIOD="0.1"
EVENT_PARTITIONS=1
EVENT_SYNC_PATTERN="diff"
PERIODIC_SYNC_PERIOD="1.0"
PERIODIC_PARTITIONS=1
PERIODIC_SYNC_PATTERN="glob"
E3_K=2
E3_STRATEGY="QualityFirst"
E3_PENALTY="0.5"
P4_K=2
P4_STRATEGY="QualityFirst"
P4_PENALTY="0.5"
PROMETHEUS_URL="http://<MONITORING_IP>:9091"
RUN_ID=""
OUTPUT_ROOT="$PROJECT_ROOT/experiments/results/module-f"
FROM_ORDER=1
TO_ORDER=""
ONLY_ORDER=""
# Bounded retry for invalid rounds. Only a random miss such as "the injection-rate gate failed to cover 1%" is retryable;
# hard failures abort immediately, otherwise a bad configuration burns hours and exhausts the budget.
RETRY_PER_ORDER=2
RETRY_BUDGET_TOTAL=4
# Dreal-F1 CL2 saturation timeout (soft threshold and hard operationTimeout).
# Kept equal to the shared config's totalPods/DENSITY_TEST_THROUGHPUT = 1000 s
# so F2 rounds censor at the same wall-clock cap the F1 rounds ran under.  A
# round that reaches the cap is no longer discarded: with at least
# MIN_COMPLETE_PODS scheduled it is recorded as valid and *censored* (10.6.2),
# because under K=0 + periodic sync + data-plane latency the last few Pods
# can spend minutes in a bind-conflict loop that says nothing about steady
# state and everything about the saturation tail.
F2_SATURATION_TIMEOUT_SECONDS=1000
MIN_COMPLETE_PODS=9900
RETRY_BUDGET_USED=0
KEEP_RUNTIME=false
TOTAL_ROUNDS=0

# Anchor-derived profile (10.3).  The synthetic f0-synthetic-20260906-s1 and
# every gate report scored against it are deprecated by 10.2 and must not be
# reachable as a default.
PROFILE_JSON="$PROJECT_ROOT/experiments/results/F0/f0-anchored-20260912/dataplane-profile-v1.json"
GATE_REPORT="$PROJECT_ROOT/experiments/results/injection-fidelity-gate/ifg-anchored-20260912-r3/gate-report.json"
KWOK_BIN="${KWOK_BIN:-$HOME/go/bin/kwok}"
KWOK_CONFIG="$PROJECT_ROOT/experiments/kwok-setup/kwok-config.yaml"
KWOK_SETUP="$PROJECT_ROOT/experiments/kwok-setup"
E2_G_RUNNER="$SCRIPT_DIR/run-godel-baseline.sh"
PARA_RUNNER="$SCRIPT_DIR/run-experiment.sh"
PARA_NAMESPACE="para-system"
ADOPTION_STATS_MANIFEST="$PROJECT_ROOT/para-scheduler/deploy/lab-cluster/adoption-stats.yaml"

RUN_ROOT=""
RUNTIME_DIR=""
PLAN_FILE=""
SYSTEMD_TAKEN_OVER=false
SYSTEMD_STATE_FILE=""
ACTIVE_KWOK_PIDS=()
WATCHER_PID=""
CPU_MONITOR_PID=""
FAILURE_REAPER_PID=""
SUDO_KEEPALIVE_PID=""
BINDER_RESET_ACTIVE=false
BINDER_RESTORE_REPLICAS=""
SMOKE_NAMESPACE=""
SMOKE_NODE_SELECTOR=""
SMOKE_PODS=2000

usage() {
    cat <<'EOF'
Usage:
  ./experiments/scripts/run-module-f.sh [options]

Experiment selection:
  --experiment F1-E|F1-P|F2-E|F2-P  Select the sub-experiment (default: F1-E)

Execution mode:
  --dry-run               Only validate statically and print the fixed-seed plan (default)
  --self-test             Generate the temporary Stage/runner/watcher and syntax-check them, without contacting the cluster
  --injection-smoke-test  Create pre-bound pods and verify only Dreal-F1's 1% failure injection (bypasses the scheduler)
  --smoke-pods N          Pod count for the smoke test, default 2000. Note that 2000 has too little power to
                          separate 0.64% from 1% (the CI is too wide); use 10000 to qualify a systematic shortfall
  --execute               Actually run the core matrix; this is the only mode that touches the cluster

Reproducibility parameters:
  --run-id ID             Result directory name; a timestamp is generated by default
  --seed N                Seed for the random ordering of all conditions within a trial
  --from-order N          Start execution at round N of the plan
  --to-order N            Stop execution after round N of the plan (default: the matrix's last round)
  --only-order N          Execute only round N of the plan (for debugging; not a formal full matrix)
  --f2-saturation-timeout-seconds N
                          Soft threshold and hard timeout for CL2 saturation in Dreal-F1 rounds (default 1000,
                          matching the shared configuration's totalPods/DENSITY_TEST_THROUGHPUT).
                          A timeout no longer invalidates the round: scheduled >= --min-complete-pods makes it valid,
                          marks it censored, and records makespan as right-censored
  --min-complete-pods N   Minimum scheduled pods for a timed-out round to count as valid (default 9900 = T99)
  --profile-json PATH     Dreal four-bucket anchor profile JSON
  --gate-report PATH      Injection fidelity gate report; must be PASS
  --prometheus-url URL    Prometheus address
  --output-root PATH      Module F result root directory
  --keep-runtime          Keep the generated temporary runner and watcher (for debugging)
  -h, --help              Show this help

Examples:
  # Safe preview; does not run the experiment
  ./experiments/scripts/run-module-f.sh --experiment F1-E --dry-run

  # Formally execute the F1-P matrix
  ./experiments/scripts/run-module-f.sh --experiment F1-P --execute \
    --run-id F1-P-20260909-formal

  # Verify F2-E failure injection only; does not start the scheduling comparison matrix
  ./experiments/scripts/run-module-f2-e.sh --injection-smoke-test \
    --run-id F2-E-injection-smoke-20260909
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

log() {
    printf '[module-f] %s\n' "$*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment) EXPERIMENT="$2"; shift 2 ;;
        --dry-run) MODE="dry-run"; shift ;;
        --self-test) MODE="self-test"; shift ;;
        --injection-smoke-test) MODE="injection-smoke-test"; shift ;;
        --smoke-pods) SMOKE_PODS="$2"; shift 2 ;;
        --execute) MODE="execute"; shift ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --seed) PLAN_SEED="$2"; shift 2 ;;
        --trials) TRIALS="$2"; shift 2 ;;
        --from-order) FROM_ORDER="$2"; shift 2 ;;
        --to-order) TO_ORDER="$2"; shift 2 ;;
        --only-order) ONLY_ORDER="$2"; shift 2 ;;
        --f2-saturation-timeout-seconds) F2_SATURATION_TIMEOUT_SECONDS="$2"; shift 2 ;;
        --min-complete-pods) MIN_COMPLETE_PODS="$2"; shift 2 ;;
        --profile-json) PROFILE_JSON="$2"; shift 2 ;;
        --gate-report) GATE_REPORT="$2"; shift 2 ;;
        --prometheus-url) PROMETHEUS_URL="$2"; shift 2 ;;
        --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
        --keep-runtime) KEEP_RUNTIME=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ "$F2_SATURATION_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
    || die "--f2-saturation-timeout-seconds must be a positive integer, got: $F2_SATURATION_TIMEOUT_SECONDS"
[[ "$MIN_COMPLETE_PODS" =~ ^[1-9][0-9]*$ ]] && (( MIN_COMPLETE_PODS <= 10000 )) \
    || die "--min-complete-pods must be an integer in 1..10000, got: $MIN_COMPLETE_PODS"

# Between-trial variance is markedly larger on the periodic side than the event-driven side, hence the different default repetition counts.
if [[ -z "$TRIALS" ]]; then
    case "$EXPERIMENT" in
        F1-E|F2-E) TRIALS=3 ;;
        F1-P|F2-P) TRIALS=5 ;;
    esac
fi
[[ "$TRIALS" =~ ^[0-9]+$ && "$TRIALS" -ge 1 ]] || die "--trials must be a positive integer, got '$TRIALS'"

case "$EXPERIMENT" in
    F1-E) TOTAL_ROUNDS=$((2 * 2 * TRIALS)) ;;
    F1-P) TOTAL_ROUNDS=$((2 * 2 * TRIALS)) ;;
    F2-E) TOTAL_ROUNDS=$((2 * TRIALS)) ;;
    F2-P) TOTAL_ROUNDS=$((2 * TRIALS)) ;;
    *) die "--experiment must be one of F1-E, F1-P, F2-E, F2-P" ;;
esac

[[ -n "$TO_ORDER" ]] || TO_ORDER="$TOTAL_ROUNDS"

[[ "$PLAN_SEED" =~ ^[0-9]+$ ]] || die "--seed must be a non-negative integer"
[[ "$FROM_ORDER" =~ ^[0-9]+$ ]] || die "--from-order must be a positive integer"
[[ "$TO_ORDER" =~ ^[0-9]+$ ]] || die "--to-order must be a positive integer"
(( TO_ORDER >= FROM_ORDER )) || die "--to-order must not be smaller than --from-order"
(( TO_ORDER <= TOTAL_ROUNDS )) || die "--to-order must not exceed the $EXPERIMENT total round count $TOTAL_ROUNDS"
if [[ -n "$ONLY_ORDER" ]]; then
    [[ "$ONLY_ORDER" =~ ^[0-9]+$ ]] || die "--only-order must be a positive integer"
    (( ONLY_ORDER <= TOTAL_ROUNDS )) || die "--only-order must not exceed the $EXPERIMENT total round count $TOTAL_ROUNDS"
fi

static_preflight() {
    local command_name path
    for command_name in python3 awk realpath sed grep; do
        command -v "$command_name" >/dev/null 2>&1 || die "missing command: $command_name"
    done

    for path in \
        "$E2_G_RUNNER" \
        "$PARA_RUNNER" \
        "$SCRIPT_DIR/collect-metrics.sh" \
        "$SCRIPT_DIR/collect-metrics-godel.sh" \
        "$SCRIPT_DIR/process-results.py" \
        "$ADOPTION_STATS_MANIFEST" \
        "$KWOK_CONFIG" \
        "$KWOK_SETUP/kwok-deployment.yaml" \
        "$KWOK_SETUP/kwok-deployment-godel.yaml" \
        "$PROFILE_JSON" \
        "$GATE_REPORT"; do
        [[ -f "$path" ]] || die "missing file: $path"
    done

    python3 - "$PROFILE_JSON" "$GATE_REPORT" <<'PY'
import json
import pathlib
import sys

profile_path, gate_path = map(pathlib.Path, sys.argv[1:])
profile = json.loads(profile_path.read_text(encoding="utf-8"))
gate = json.loads(gate_path.read_text(encoding="utf-8"))

if profile.get("kwok_version") != "v0.7.0":
    raise SystemExit(f"profile KWOK version is not v0.7.0: {profile.get('kwok_version')}")
semantics = profile.get("kwok_jitter_semantics", "")
if "upper endpoint" not in semantics:
    raise SystemExit(f"unverified jitter upper-bound semantics: {semantics!r}")

# Section 10.3 derives exactly four weighted buckets from the three upstream
# CI anchors.  The former five-bucket rule dates from the synthetic profile
# deprecated in 10.2 and rejects every anchor-derived profile.
bins = profile.get("bins", [])
if len(bins) != 4:
    raise SystemExit(f"Dreal must have 4 buckets (see the anchor derivation), got {len(bins)}")
if sum(int(item["weight"]) for item in bins) != 10000:
    raise SystemExit("Dreal four-bucket weights do not sum to 10000")
previous_upper = None
for item in bins:
    delay = item.get("kwok_delay", {})
    lower = int(delay["durationMilliseconds"])
    upper = int(delay["jitterDurationMilliseconds"])
    if lower < 0 or upper <= lower:
        raise SystemExit(f"invalid latency bucket {item.get('id')}: [{lower}, {upper})")
    # The buckets must tile the support with no gap or overlap; otherwise the
    # target CDF the gate scores against is not the distribution injected.
    if previous_upper is not None and lower != previous_upper:
        raise SystemExit(
            f"latency buckets are not contiguous: {item.get('id')} lower bound {lower} != previous upper bound {previous_upper}"
        )
    previous_upper = upper

def bucket_key(item):
    return (item.get("id"), float(item["lower_ms"]), float(item["upper_ms"]), int(item["weight"]))


# Establish that the report describes *this* profile before reading its
# verdict.  --profile-json and --gate-report are separate flags, so
# overriding only the first silently pairs a new profile with a report that
# validated a different one.
gate_bins = [bucket_key(item) for item in gate.get("target_bins", [])]
if gate_bins != [bucket_key(item) for item in bins]:
    raise SystemExit(
        f"gate report {gate.get('run_id')} targets different buckets than profile {profile.get('run_id')}, "
        "so it did not validate this profile; rerun injection-fidelity-gate.py against the same profile"
    )

if gate.get("result") != "PASS":
    # Name the report: --gate-report is easy to leave pointing at a superseded
    # run, and the verdict alone does not say which file was read.
    raise SystemExit(
        f"injection fidelity gate is not PASS: {gate.get('result')} (report {gate.get('run_id')}, "
        f"read from {gate_path})"
    )
workload = gate.get("workload", {})
if int(workload.get("pods", 0)) != 10000 or int(workload.get("shards", 0)) != 10:
    raise SystemExit(
        f"gate report is not a formal 10000-pod / 10-shard gate, got "
        f"{workload.get('pods')} Pod / {workload.get('shards')} shard"
    )

print("static preflight: PASS")
print(f"  profile run_id: {profile.get('run_id')}")
print(f"  jitter semantics: {semantics}")
print(f"  fidelity gate: {gate.get('run_id')} = PASS")
print(f"  classification: {gate.get('classification', 'unknown')}")
PY
}

generate_plan() {
    local output=$1
    python3 - "$PLAN_SEED" "$TRIALS" "$EXPERIMENT" > "$output" <<'PY'
import csv
import random
import sys

seed = int(sys.argv[1])
trials = int(sys.argv[2])
experiment = sys.argv[3]
rng = random.Random(seed)
if experiment == "F1-E":
    methods = ("E2", "E3")
    profiles = ("Z0", "Dreal")
elif experiment == "F1-P":
    methods = ("P1", "P4")
    profiles = ("Z0", "Dreal")
elif experiment == "F2-E":
    methods = ("E2", "E3")
    profiles = ("Dreal-F1",)
elif experiment == "F2-P":
    methods = ("P1", "P4")
    profiles = ("Dreal-F1",)
else:
    raise SystemExit(f"unsupported experiment: {experiment}")
conditions = [(method, profile) for method in methods for profile in profiles]

writer = csv.writer(sys.stdout, lineterminator="\n")
writer.writerow(["order", "trial", "method", "profile"])
order = 0
for trial in range(1, trials + 1):
    trial_conditions = list(conditions)
    rng.shuffle(trial_conditions)
    for method, profile in trial_conditions:
        order += 1
        writer.writerow([order, trial, method, profile])
PY
}

print_plan() {
    local plan=$1
    python3 - "$plan" "$EXPERIMENT" <<'PY'
import csv
import sys

rows = list(csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")))
experiment = sys.argv[2]
print(f"{experiment} execution order (all conditions randomly rotated within a trial):")
for row in rows:
    print(f"  {int(row['order']):02d}. trial={row['trial']}  {row['method']}-{row['profile']}")
PY
}

cluster_preflight() {
    local unit stale_pods terminating kwok_version
    for unit in kwok kwok{1..9}; do
        systemctl cat "$unit" >/dev/null 2>&1 || die "missing systemd unit: $unit"
        [[ "$(systemctl show "$unit" -p LoadState --value)" != "masked" ]] \
            || die "$unit is masked; its original state cannot be recorded and restored safely"
    done

    command -v kubectl >/dev/null 2>&1 || die "kubectl not found"
    command -v curl >/dev/null 2>&1 || die "curl not found"
    command -v sudo >/dev/null 2>&1 || die "sudo not found"
    [[ -x "$KWOK_BIN" ]] || die "KWOK is not executable: $KWOK_BIN"
    [[ -x "$PROJECT_ROOT/bin/clusterloader" ]] || die "missing bin/clusterloader"
    if ! sudo -n true >/dev/null 2>&1; then
        [[ -t 0 ]] || die "sudo is not authenticated; run sudo -v in an interactive terminal before starting --execute"
        log "execute mode needs to manage the KWOK services temporarily; authenticate at the sudo prompt (the password is never written to the script)"
        sudo -v || die "sudo authentication failed"
    fi

    kubectl get namespace default >/dev/null 2>&1 || die "Kubernetes API is unreachable"
    curl -fsS --max-time 5 "$PROMETHEUS_URL/-/ready" >/dev/null \
        || die "Prometheus is not ready: $PROMETHEUS_URL"

    kwok_version="$($KWOK_BIN --version 2>&1 | tr -d '\r')"
    [[ "$kwok_version" == *"v0.7.0"* ]] || die "KWOK v0.7.0 is required, got: $kwok_version"

    stale_pods="$(kubectl get pods --all-namespaces -l group=saturation --no-headers 2>/dev/null | wc -l)"
    [[ "$stale_pods" -eq 0 ]] || die "found $stale_pods stale group=saturation pods; clean them up first, this script will not delete them on its own"
    terminating="$(kubectl get namespaces --no-headers 2>/dev/null | awk '$2=="Terminating" {n++} END {print n+0}')"
    [[ "$terminating" -eq 0 ]] || die "found $terminating Terminating namespaces; wait for them to clear before running"

    log "cluster preflight: PASS (API, Prometheus, KWOK v0.7.0, sudo, stale workload)"
}

render_stage_file() {
    local output=$1
    python3 - "$PROFILE_JSON" "$output" <<'PY'
import json
import pathlib
import sys

profile_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
profile = json.loads(profile_path.read_text(encoding="utf-8"))

status_template = '''      {{ $now := Now }}

      conditions:
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: Initialized
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: Ready
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: ContainersReady
      {{ range .spec.readinessGates }}
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: {{ .conditionType | Quote }}
      {{ end }}

      containerStatuses:
      {{ range .spec.containers }}
      - image: {{ .image | Quote }}
        name: {{ .name | Quote }}
        ready: true
        restartCount: 0
        started: true
        state:
          running:
            startedAt: {{ $now | Quote }}
      {{ end }}

      hostIP: {{ NodeIPWith .spec.nodeName | Quote }}
      podIP: {{ PodIPWith .spec.nodeName ( or .spec.hostNetwork false ) ( or .metadata.uid "" ) ( or .metadata.name "" ) ( or .metadata.namespace "" ) | Quote }}
      phase: Running
      startTime: {{ $now | Quote }}'''

documents = []
for item in profile["bins"]:
    delay = item["kwok_delay"]
    documents.append(f'''apiVersion: kwok.x-k8s.io/v1alpha1
kind: Stage
metadata:
  name: module-f-dreal-{item["id"]}
  labels:
    parasched.io/experiment: module-f
spec:
  resourceRef:
    apiGroup: v1
    kind: Pod
  selector:
    matchExpressions:
    - key: '.metadata.labels["parasched.io/dataplane-profile"]'
      operator: In
      values: ['calibrated-v1']
    - key: '.metadata.deletionTimestamp'
      operator: DoesNotExist
    - key: '.spec.nodeName'
      operator: Exists
    - key: '.status.podIP'
      operator: DoesNotExist
  weight: {int(item["weight"])}
  delay:
    durationMilliseconds: {int(delay["durationMilliseconds"])}
    jitterDurationMilliseconds: {int(delay["jitterDurationMilliseconds"])}
  next:
    statusTemplate: |
{status_template}
''')

# KWOK does not merge its built-in fast Pod stages once a custom Pod stage
# configuration is supplied.  Keep the v0.7.0 delete stage in this generated
# module-F-only bundle so CL2 cleanup can complete normally.
documents.append('''apiVersion: kwok.x-k8s.io/v1alpha1
kind: Stage
metadata:
  name: module-f-pod-delete
  labels:
    parasched.io/experiment: module-f
spec:
  resourceRef:
    apiGroup: v1
    kind: Pod
  selector:
    matchExpressions:
    - key: '.metadata.deletionTimestamp'
      operator: Exists
  next:
    finalizers:
      empty: true
    delete: true
''')

output_path.write_text("---\n".join(documents), encoding="utf-8")
PY
}

render_failure_stage_file() {
    local output=$1
    python3 - "$PROFILE_JSON" "$output" <<'PY'
import json
import pathlib
import sys

profile_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
profile = json.loads(profile_path.read_text(encoding="utf-8"))

success_template = '''      {{ $now := Now }}

      conditions:
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: Initialized
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: Ready
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: ContainersReady
      {{ range .spec.readinessGates }}
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: {{ .conditionType | Quote }}
      {{ end }}

      containerStatuses:
      {{ range .spec.containers }}
      - image: {{ .image | Quote }}
        name: {{ .name | Quote }}
        ready: true
        restartCount: 0
        started: true
        state:
          running:
            startedAt: {{ $now | Quote }}
      {{ end }}

      hostIP: {{ NodeIPWith .spec.nodeName | Quote }}
      podIP: {{ PodIPWith .spec.nodeName ( or .spec.hostNetwork false ) ( or .metadata.uid "" ) ( or .metadata.name "" ) ( or .metadata.namespace "" ) | Quote }}
      phase: Running
      startTime: {{ $now | Quote }}'''

# This is the v0.7.0 chaos failure shape, specialized to an injected
# post-bind sandbox/container-start failure.  podIP is set so neither the
# success nor failure Stage can select the terminal Pod a second time.
failure_template = '''      {{ $now := Now }}

      conditions:
      - lastTransitionTime: {{ $now | Quote }}
        status: "True"
        type: Initialized
      - lastTransitionTime: {{ $now | Quote }}
        message: Injected post-bind kubelet/CRI startup failure
        reason: DataPlaneStartupFailed
        status: "False"
        type: Ready
      - lastTransitionTime: {{ $now | Quote }}
        message: Injected post-bind kubelet/CRI startup failure
        reason: DataPlaneStartupFailed
        status: "False"
        type: ContainersReady
      {{ range .spec.readinessGates }}
      - lastTransitionTime: {{ $now | Quote }}
        message: Injected post-bind kubelet/CRI startup failure
        reason: DataPlaneStartupFailed
        status: "False"
        type: {{ .conditionType | Quote }}
      {{ end }}

      containerStatuses:
      {{ range .spec.containers }}
      - image: {{ .image | Quote }}
        name: {{ .name | Quote }}
        ready: false
        restartCount: 0
        started: false
        state:
          terminated:
            exitCode: 1
            finishedAt: {{ $now | Quote }}
            message: Injected post-bind kubelet/CRI startup failure
            reason: DataPlaneStartupFailed
            startedAt: {{ $now | Quote }}
      {{ end }}

      hostIP: {{ NodeIPWith .spec.nodeName | Quote }}
      podIP: {{ PodIPWith .spec.nodeName ( or .spec.hostNetwork false ) ( or .metadata.uid "" ) ( or .metadata.name "" ) ( or .metadata.namespace "" ) | Quote }}
      phase: Failed
      startTime: {{ $now | Quote }}'''

documents = []
success_total = 0
failure_total = 0
for item in profile["bins"]:
    delay = item["kwok_delay"]
    source_weight = int(item["weight"])
    failure_weight = source_weight // 100
    success_weight = source_weight - failure_weight
    success_total += success_weight
    failure_total += failure_weight
    selector = '''    matchExpressions:
    - key: '.metadata.labels["parasched.io/dataplane-profile"]'
      operator: In
      values: ['calibrated-f1-v1']
    - key: '.metadata.deletionTimestamp'
      operator: DoesNotExist
    - key: '.spec.nodeName'
      operator: Exists
    - key: '.status.podIP'
      operator: DoesNotExist'''
    common = f'''spec:
  resourceRef:
    apiGroup: v1
    kind: Pod
  selector:
{selector}
  delay:
    durationMilliseconds: {int(delay["durationMilliseconds"])}
    jitterDurationMilliseconds: {int(delay["jitterDurationMilliseconds"])}'''
    documents.append(f'''apiVersion: kwok.x-k8s.io/v1alpha1
kind: Stage
metadata:
  name: module-f-dreal-f1-{item["id"]}-success
  labels:
    parasched.io/experiment: module-f
    parasched.io/profile: dreal-f1
{common}
  weight: {success_weight}
  next:
    statusTemplate: |
{success_template}
''')
    documents.append(f'''apiVersion: kwok.x-k8s.io/v1alpha1
kind: Stage
metadata:
  name: module-f-dreal-f1-{item["id"]}-failure
  labels:
    parasched.io/experiment: module-f
    parasched.io/profile: dreal-f1
{common}
  weight: {failure_weight}
  next:
    event:
      type: Warning
      reason: DataPlaneStartupFailed
      message: Injected post-bind kubelet/CRI startup failure
    statusTemplate: |
{failure_template}
''')

if success_total != 9900 or failure_total != 100:
    raise SystemExit(
        f"Dreal-F1 weight split is invalid: success={success_total}, failure={failure_total}"
    )

documents.append('''apiVersion: kwok.x-k8s.io/v1alpha1
kind: Stage
metadata:
  name: module-f-pod-delete
  labels:
    parasched.io/experiment: module-f
    parasched.io/profile: dreal-f1
spec:
  resourceRef:
    apiGroup: v1
    kind: Pod
  selector:
    matchExpressions:
    - key: '.metadata.deletionTimestamp'
      operator: Exists
  next:
    finalizers:
      empty: true
    delete: true
''')

output_path.write_text("---\n".join(documents), encoding="utf-8")
PY
}

render_f2_cl2_config() {
    local output=$1 source="$KWOK_SETUP/cl2-saturation-only.yaml"
    python3 - "$source" "$output" "$F2_SATURATION_TIMEOUT_SECONDS" <<'PY'
import pathlib
import sys

source, output, timeout_seconds = sys.argv[1:]
source, output = pathlib.Path(source), pathlib.Path(output)
timeout_seconds = int(timeout_seconds)
text = source.read_text(encoding="utf-8")
# The shared config derives both the soft PodStartupLatency threshold and the
# hard WaitForControlledPodsRunning operationTimeout from
# totalPods / DENSITY_TEST_THROUGHPUT, i.e. 10000 / 10 = 1000 s.  That encodes
# an assumption of at least 10 Pods/s.  Under Dreal-F1 the vanilla periodic
# baseline (P1, K=0) measured 9.997 Pods/s and was cut off at exactly 1000 s
# with 9999/10000 scheduled, so the instrument, not the system under test,
# decided the verdict.  Raise both for the F2 copy; the soft threshold has to
# move too, or a Pod that waits past 1000 s raises a latency violation and the
# run fails anyway.  Only Dreal-F1 rounds use this copy.
timeout_markers = (
    "{{$saturationDeploymentTimeout := DivideFloat $totalPods $DENSITY_TEST_THROUGHPUT}}\n"
    "# Hard timeout: clamp between [600s, 3600s].\n"
    "{{$saturationDeploymentHardTimeout := MaxInt 600 (MinInt $saturationDeploymentTimeout 3600)}}\n"
)
if text.count(timeout_markers) != 1:
    raise SystemExit("CL2 saturation timeout markers changed; refusing to generate F2 copy")
text = text.replace(
    timeout_markers,
    f"# Module F2 runtime copy: both timeouts pinned to {timeout_seconds}s\n"
    f"# (--f2-saturation-timeout-seconds) instead of totalPods/DENSITY_TEST_THROUGHPUT.\n"
    f"{{{{$saturationDeploymentTimeout := {timeout_seconds}}}}}\n"
    f"{{{{$saturationDeploymentHardTimeout := {timeout_seconds}}}}}\n",
)

namespace_marker = "namespace:\n  number: {{$namespaces}}\n"
if text.count(namespace_marker) != 1:
    raise SystemExit("CL2 saturation namespace marker changed; refusing to generate F2 copy")
text = text.replace(
    namespace_marker,
    namespace_marker + "  deleteAutomanagedNamespaces: false\n",
)
start = "\n- name: Deleting saturation pods\n"
end = "\n- name: Collecting measurements\n"
if text.count(start) != 1 or text.count(end) != 1:
    raise SystemExit("CL2 saturation cleanup markers changed; refusing to generate F2 copy")
prefix, remainder = text.split(start, 1)
_, suffix = remainder.split(end, 1)
generated = prefix + "\n\n# Module F2 runtime copy: workload deletion is deferred until the\n" \
    "# pre-cleanup failure snapshot has been captured by the runtime runner.\n" + end + suffix
output.write_text(generated, encoding="utf-8")
PY
}

prepare_runtime_runners() {
    python3 - "$E2_G_RUNNER" "$PARA_RUNNER" "$RUNTIME_DIR" "$SCRIPT_DIR" <<'PY'
import pathlib
import stat
import sys

e2g_source, para_source, runtime_dir, real_script_dir = map(pathlib.Path, sys.argv[1:])
runtime_dir.mkdir(parents=True, exist_ok=True)

def common_patch(text: str) -> str:
    lines = text.splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith('SCRIPT_DIR=')]
    if len(matches) != 1:
        raise SystemExit(f"unexpected runner SCRIPT_DIR marker count: {len(matches)}")
    lines[matches[0]] = f'SCRIPT_DIR="{real_script_dir}"'
    return "\n".join(lines) + "\n"

def make_node_cleanup_nonblocking(text: str, runner: str) -> str:
    # The shared runners can wait the full 300 seconds even after all matching
    # Node objects have disappeared.  Module F has already ended its measuring
    # window here, so keep deletion but do not spend five minutes waiting for
    # client-side watch completion.
    lines = text.splitlines()
    changed = 0
    for index, line in enumerate(lines):
        if "kubectl delete nodes -l type=kwok" not in line:
            continue
        updated = line.replace("--wait=true --timeout=300s", "--wait=false")
        if updated != line:
            lines[index] = updated
            changed += 1
    if changed != 2:
        raise SystemExit(f"{runner} unexpected node cleanup marker count: {changed}")
    return "\n".join(lines) + "\n"

def neutralize_kwok_systemd_control(text: str, runner: str, expected: int) -> str:
    # Module F owns the KWOK lifecycle: it stops the systemd units and starts
    # its own shards with the round's Stage config.  The shared runners restart
    # those units to refresh their Watch connection before node creation, which
    # is right when a runner owns KWOK but puts a *second* controller on the
    # same nodes here.  Both then race to apply their Stages, and the systemd
    # shards carry KWOK's built-in defaults, which make Pods Ready immediately
    # and silently bypass the Dreal delay injection this board measures
    # (observed 2026-09-12).  The matrix already starts its shards fresh right
    # before invoking the runner, so the refresh the restart provided is not
    # lost.  Only the runtime copy is patched; the shared scripts that other
    # boards depend on are left untouched.
    lines = text.splitlines()
    changed = 0
    for index, line in enumerate(lines):
        if not line.strip().startswith("sudo systemctl restart "):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = (
            f'{indent}echo "    (module-F runtime copy: systemd KWOK restart suppressed; '
            f'the matrix owns the shards)"'
        )
        changed += 1
    if changed != expected:
        raise SystemExit(
            f"{runner} unexpected systemd KWOK restart count: {changed} (expected {expected}); "
            "the upstream runner has changed; refusing to generate an unreliable patch"
        )
    return "\n".join(lines) + "\n"


def add_failure_snapshot_hook(text: str, runner: str) -> str:
    lines = text.splitlines()
    matches = [i for i, line in enumerate(lines) if line.startswith("# Step 6: Final cleanup")]
    if len(matches) != 1:
        raise SystemExit(f"{runner} unexpected final-cleanup marker count: {len(matches)}")
    hook = r'''
if [ -n "${MODULE_F_FAILURE_SNAPSHOT_SCRIPT:-}" ]; then
    echo ""
    echo "Module F: capturing Dreal-F1 failure evidence before cleanup..."

    # One-pod-per-node leaves zero capacity headroom: every Failed Pod holds its
    # node until the reaper removes it, so the ReplicaSet's replacement can sit
    # Pending and the census lands on 9999/10000.  Control-plane metrics were
    # already captured at BIND_END_TS, so waiting here cannot affect Q_bind/ACF;
    # it only lets the data-plane census converge before the snapshot.
    MODULE_F_SETTLE_TARGET="${MODULE_F_FAILURE_TARGET:-10000}"
    MODULE_F_SETTLE_DEADLINE=$(( $(date +%s) + ${MODULE_F_FAILURE_SETTLE_TIMEOUT:-300} ))
    while :; do
        MODULE_F_RUNNING_NOW=$(kubectl get pods --all-namespaces \
            -l "${MODULE_F_FAILURE_SELECTOR:-group=saturation}" \
            -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null \
            | grep -c '^Running$' || true)
        if [ "${MODULE_F_RUNNING_NOW:-0}" -ge "$MODULE_F_SETTLE_TARGET" ]; then
            echo "Module F: data-plane settled at ${MODULE_F_RUNNING_NOW}/${MODULE_F_SETTLE_TARGET}"
            break
        fi
        if [ "$(date +%s)" -ge "$MODULE_F_SETTLE_DEADLINE" ]; then
            echo "Module F: settle timeout at ${MODULE_F_RUNNING_NOW:-0}/${MODULE_F_SETTLE_TARGET}; capturing anyway"
            break
        fi
        echo "Module F: waiting for data-plane census (${MODULE_F_RUNNING_NOW:-0}/${MODULE_F_SETTLE_TARGET})..."
        sleep 10
    done

    MODULE_F_FAILURE_NAMESPACES_FILE="$(dirname "${MODULE_F_FAILURE_SNAPSHOT_OUTPUT:?}")/failure-snapshot-namespaces.txt"
    kubectl get pods --all-namespaces \
        -l "${MODULE_F_FAILURE_SELECTOR:-group=saturation}" \
        -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' \
        | sort -u > "$MODULE_F_FAILURE_NAMESPACES_FILE"
    set +e
    python3 "$MODULE_F_FAILURE_SNAPSHOT_SCRIPT" \
        --output "${MODULE_F_FAILURE_SNAPSHOT_OUTPUT:?}" \
        --target "${MODULE_F_FAILURE_TARGET:-10000}" \
        --min-complete "${MODULE_F_MIN_COMPLETE:-9900}" \
        --expected-profile "${MODULE_F_FAILURE_EXPECTED_PROFILE:-calibrated-f1-v1}" \
        --selector "${MODULE_F_FAILURE_SELECTOR:-group=saturation}" \
        --failure-ledger "${MODULE_F_FAILURE_LEDGER:?}" \
        --cohort-mode replicasets \
        --require-final-ready
    MODULE_F_FAILURE_SNAPSHOT_RC=$?
    set -e
    if [ "$MODULE_F_FAILURE_SNAPSHOT_RC" -ne 0 ]; then
        echo "Module F: Dreal-F1 failure evidence gate FAILED (exit=$MODULE_F_FAILURE_SNAPSHOT_RC)."
    fi

    # The F2 CL2 runtime config deliberately keeps its automanaged namespaces
    # alive until the snapshot above.  Delete exactly those recorded namespaces
    # now, before the runner removes the KWOK nodes.
    MODULE_F_FAILURE_CLEANUP_RC=0
    if [ -s "$MODULE_F_FAILURE_NAMESPACES_FILE" ]; then
        mapfile -t MODULE_F_FAILURE_NAMESPACES < "$MODULE_F_FAILURE_NAMESPACES_FILE"
        set +e
        kubectl delete namespace "${MODULE_F_FAILURE_NAMESPACES[@]}" \
            --wait=true --timeout=300s
        MODULE_F_FAILURE_CLEANUP_RC=$?
        set -e
        if [ "$MODULE_F_FAILURE_CLEANUP_RC" -ne 0 ]; then
            echo "Module F: Dreal-F1 namespace cleanup FAILED (exit=$MODULE_F_FAILURE_CLEANUP_RC)."
        fi
    fi
fi
'''.strip("\n").splitlines()
    lines[matches[0]:matches[0]] = hook
    return "\n".join(lines) + "\n"

para = common_patch(para_source.read_text(encoding="utf-8"))
marker = '    echo "PODS_PER_NODE: ${local_ppn}" > "$CL2_OVERRIDES_FILE"'
if para.count(marker) != 1:
    raise SystemExit("the Para-Sched runner's CL2 override marker has changed; refusing to generate an unreliable patch")
injection = marker + '''
    if [ -n "${MODULE_F_DEPLOYMENT_SPEC:-}" ]; then
        echo "SATURATION_DEPLOYMENT_SPEC: ${MODULE_F_DEPLOYMENT_SPEC}" >> "$CL2_OVERRIDES_FILE"
        echo "LATENCY_DEPLOYMENT_SPEC: ${MODULE_F_DEPLOYMENT_SPEC}" >> "$CL2_OVERRIDES_FILE"
    fi'''
para = para.replace(marker, injection)

cl2_config_marker = '        CL2_CONFIG="$CONFIG_DIR/cl2-saturation-only.yaml"'
if para.count(cl2_config_marker) != 1:
    raise SystemExit("the Para-Sched runner's saturation CL2 config marker has changed")
para = para.replace(
    cl2_config_marker,
    '        CL2_CONFIG="${MODULE_F_CL2_CONFIG:-$CONFIG_DIR/cl2-saturation-only.yaml}"',
)

e2g = common_patch(e2g_source.read_text(encoding="utf-8"))
old_sat = '        echo "SATURATION_DEPLOYMENT_SPEC: kwok-deployment-godel.yaml"'
old_lat = '        echo "LATENCY_DEPLOYMENT_SPEC: kwok-deployment-godel.yaml"'
if e2g.count(old_sat) != 1 or e2g.count(old_lat) != 1:
    raise SystemExit("the E2-G runner's deployment override marker has changed; refusing to generate an unreliable patch")
e2g = e2g.replace(old_sat, '        echo "SATURATION_DEPLOYMENT_SPEC: ${MODULE_F_DEPLOYMENT_SPEC:-kwok-deployment-godel.yaml}"')
e2g = e2g.replace(old_lat, '        echo "LATENCY_DEPLOYMENT_SPEC: ${MODULE_F_DEPLOYMENT_SPEC:-kwok-deployment-godel.yaml}"')
if e2g.count(cl2_config_marker) != 1:
    raise SystemExit("the E2-G runner's saturation CL2 config marker has changed")
e2g = e2g.replace(
    cl2_config_marker,
    '        CL2_CONFIG="${MODULE_F_CL2_CONFIG:-$CONFIG_DIR/cl2-saturation-only.yaml}"',
)

e2g = make_node_cleanup_nonblocking(e2g, "E2-G runner")
para = make_node_cleanup_nonblocking(para, "Para-Sched runner")
# run-experiment.sh has exactly two: Step 1a's pre-node-creation refresh and the
# NotReady remediation loop.  run-godel-baseline.sh has none; asserting 0 makes
# it fail loudly if one is ever added upstream.
e2g = neutralize_kwok_systemd_control(e2g, "E2-G runner", 0)
para = neutralize_kwok_systemd_control(para, "Para-Sched runner", 2)
e2g = add_failure_snapshot_hook(e2g, "E2-G runner")
para = add_failure_snapshot_hook(para, "Para-Sched runner")

# The shared runners intentionally continue after a CL2 failure so that their
# normal metric collection and cleanup still run.  Module F needs strict trial
# validity, so only the generated runtime copies propagate the saved CL2 code.
strict_exit = '''

# module-F runtime copy: preserve CL2 failure after collection and cleanup.
# A Dreal-F1 evidence-gate failure is returned only when CL2 itself succeeded,
# so the original workload failure cause is never overwritten.
if [ "${CL2_EXIT:-0}" -ne 0 ]; then
    exit "$CL2_EXIT"
fi
if [ "${MODULE_F_FAILURE_CLEANUP_RC:-0}" -ne 0 ]; then
    exit "$MODULE_F_FAILURE_CLEANUP_RC"
fi
exit "${MODULE_F_FAILURE_SNAPSHOT_RC:-0}"
'''
e2g += strict_exit
para += strict_exit

for name, content in (("run-e2g-runtime.sh", e2g), ("run-para-runtime.sh", para)):
    path = runtime_dir / name
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
PY
}

render_workload_template() {
    local method=$1 profile=$2 output=$3 source profile_label
    if [[ "$method" == "E2-G" ]]; then
        source="$KWOK_SETUP/kwok-deployment-godel.yaml"
    else
        source="$KWOK_SETUP/kwok-deployment.yaml"
    fi
    case "$profile" in
        Dreal) profile_label="calibrated-v1" ;;
        Dreal-F1) profile_label="calibrated-f1-v1" ;;
        Z0) profile_label="zero-v1" ;;
        *) die "unknown data-plane profile: $profile" ;;
    esac

    python3 - "$source" "$output" "$profile_label" <<'PY'
import pathlib
import sys

source, output = map(pathlib.Path, sys.argv[1:3])
profile = sys.argv[3]
text = source.read_text(encoding="utf-8")
marker = "        type: kwok"
if text.count(marker) != 1:
    raise SystemExit(f"unexpected 'type: kwok' marker count in the pod template: {text.count(marker)}")
text = text.replace(marker, marker + f"\n        parasched.io/dataplane-profile: {profile}")
output.write_text(text, encoding="utf-8")
PY
}

write_watcher_helper() {
    local output=$1
    cat > "$output" <<'PY'
#!/usr/bin/env python3
import argparse
import base64
import csv
import datetime as dt
import json
import os
import pathlib
import queue
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import urllib3

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", required=True)
parser.add_argument("--expected-profile", required=True)
parser.add_argument("--target", type=int, required=True)
parser.add_argument("--kubectl", default="kubectl")
args = parser.parse_args()

out = pathlib.Path(args.output_dir)
out.mkdir(parents=True, exist_ok=True)
events_path = out / "lifecycle.jsonl"
csv_path = out / "lifecycle.csv"
summary_path = out / "lifecycle-summary.json"
marker_path = out / "bind-complete.json"
ready_marker_path = out / "ready-complete.json"
ready_path = out / "watcher-ready.json"
log_path = out / "watcher-errors.log"

states = {}
stop = False
child = None
watch_response = None
api_http = None
api_server = None
credential_files = []
normal_timeouts = 0
abnormal_reconnects = 0
early_eof_count = 0
gone_410_count = 0
watch_errors = 0
recovered_transitions = 0
queue_overflows = 0
timing_valid = True
timing_invalid_reasons = []
watch_connection_seconds = []
bound_count = 0
ready_count = 0
bound_start_mono_ns = None
bound_start_wall_ns = None
ready_start_mono_ns = None
ready_start_wall_ns = None
event_queue = queue.Queue(maxsize=131072)
worker_sentinel = object()
events_file = events_path.open("w", encoding="utf-8", buffering=1)

def now_pair():
    return time.monotonic_ns(), time.time_ns()

def build_api_client():
    global api_http, api_server
    command = [args.kubectl, "config", "view", "--raw", "--minify", "-o", "json"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl config view failed: {result.stderr.strip()}")
    config = json.loads(result.stdout)
    cluster = config["clusters"][0]["cluster"]
    user = config["users"][0]["user"]
    api_server = cluster["server"].rstrip("/")

    context = ssl.create_default_context()
    ca_data = cluster.get("certificate-authority-data")
    if ca_data:
        context.load_verify_locations(cadata=base64.b64decode(ca_data).decode("utf-8"))
    elif cluster.get("certificate-authority"):
        context.load_verify_locations(cafile=cluster["certificate-authority"])
    elif cluster.get("insecure-skip-tls-verify"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    cert_data = user.get("client-certificate-data")
    key_data = user.get("client-key-data")
    cert_path = user.get("client-certificate")
    key_path = user.get("client-key")
    if cert_data and key_data:
        for suffix, encoded in ((".crt", cert_data), (".key", key_data)):
            handle = tempfile.NamedTemporaryFile(
                mode="wb", prefix=".module-f-watch-", suffix=suffix,
                dir=out, delete=False,
            )
            try:
                handle.write(base64.b64decode(encoded))
            finally:
                handle.close()
            os.chmod(handle.name, 0o600)
            credential_files.append(handle.name)
        cert_path, key_path = credential_files[-2:]
    if cert_path and key_path:
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)

    headers = {"Accept": "application/json"}
    if user.get("token"):
        headers["Authorization"] = "Bearer " + user["token"]
    api_http = urllib3.PoolManager(ssl_context=context, maxsize=1, headers=headers)

def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)

def log_error(message):
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {message}\n")

def invalidate_timing(reason):
    global timing_valid
    timing_valid = False
    if reason not in timing_invalid_reasons:
        timing_invalid_reasons.append(reason)
        log_error(f"timing invalid: {reason}")

def is_ready(obj):
    for condition in obj.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return True
    return False

def emit(kind, state, event_type, source, mono_ns, wall_ns):
    record = {
        "event": kind,
        "watch_event": event_type,
        "source": source,
        "uid": state["uid"],
        "namespace": state["namespace"],
        "name": state["name"],
        "node": state.get("node", ""),
        "profile": state.get("profile", ""),
        "monotonic_ns": mono_ns,
        "wall_time_ns": wall_ns,
    }
    events_file.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")

def maybe_write_bind_marker(end_mono, end_wall):
    if marker_path.exists() or bound_count != args.target:
        return
    start_mono = bound_start_mono_ns
    start_wall = bound_start_wall_ns
    if start_mono is None or start_wall is None or end_mono <= start_mono:
        invalidate_timing("invalid Bind marker boundaries")
        return
    atomic_json(marker_path, {
        "target": args.target,
        "start_monotonic_ns": start_mono,
        "end_monotonic_ns": end_mono,
        "duration_seconds": (end_mono - start_mono) / 1e9,
        "q_bind_pods_per_second": args.target / ((end_mono - start_mono) / 1e9),
        "start_epoch_seconds": start_wall / 1e9,
        "end_epoch_seconds": end_wall / 1e9,
    })

def maybe_write_ready_marker(end_mono, end_wall):
    global stop, watch_response
    if ready_marker_path.exists() or ready_count != args.target:
        return
    start_mono = ready_start_mono_ns
    start_wall = ready_start_wall_ns
    if start_mono is None or start_wall is None or end_mono <= start_mono:
        invalidate_timing("invalid Ready marker boundaries")
        return
    atomic_json(ready_marker_path, {
        "target": args.target,
        "start_monotonic_ns": start_mono,
        "end_monotonic_ns": end_mono,
        "duration_seconds": (end_mono - start_mono) / 1e9,
        "q_ready_pods_per_second": args.target / ((end_mono - start_mono) / 1e9),
        "start_epoch_seconds": start_wall / 1e9,
        "end_epoch_seconds": end_wall / 1e9,
    })
    # F1 measurements end at Ready drain.  Stop before CL2 cleanup so deletion
    # activity cannot delay or rewrite the lifecycle summary.
    stop = True
    if watch_response is not None:
        watch_response.close()

def process_object(obj, event_type, source, mono_ns=None, wall_ns=None):
    global recovered_transitions, bound_count, ready_count
    global bound_start_mono_ns, bound_start_wall_ns
    global ready_start_mono_ns, ready_start_wall_ns
    meta = obj.get("metadata", {})
    uid = meta.get("uid")
    if not uid:
        return
    if mono_ns is None or wall_ns is None:
        mono_ns, wall_ns = now_pair()
    labels = meta.get("labels", {}) or {}
    state = states.get(uid)
    if state is None:
        state = states[uid] = {
            "uid": uid,
            "namespace": meta.get("namespace", ""),
            "name": meta.get("name", ""),
            "profile": labels.get("parasched.io/dataplane-profile", ""),
            "node": "",
            "create_mono_ns": mono_ns,
            "create_wall_ns": wall_ns,
            "bind_mono_ns": None,
            "bind_wall_ns": None,
            "ready_mono_ns": None,
            "ready_wall_ns": None,
            "failed_mono_ns": None,
            "failed_wall_ns": None,
            "deleted_mono_ns": None,
            "deleted_wall_ns": None,
            "first_source": source,
            "timing_valid": source == "watch" and event_type == "ADDED",
        }
        emit("created", state, event_type, source, mono_ns, wall_ns)
        if source != "watch" or event_type != "ADDED":
            recovered_transitions += 1
            invalidate_timing(f"first observation for {uid} came from {source}/{event_type}")

    node = obj.get("spec", {}).get("nodeName", "") or ""
    if node and state["bind_mono_ns"] is None:
        state["node"] = node
        state["bind_mono_ns"] = mono_ns
        state["bind_wall_ns"] = wall_ns
        emit("bound", state, event_type, source, mono_ns, wall_ns)
        if source != "watch":
            recovered_transitions += 1
            state["timing_valid"] = False
            invalidate_timing(f"Bind for {uid} recovered from {source}")
        bound_count += 1
        if bound_start_mono_ns is None or state["create_mono_ns"] < bound_start_mono_ns:
            bound_start_mono_ns = state["create_mono_ns"]
            bound_start_wall_ns = state["create_wall_ns"]
        maybe_write_bind_marker(mono_ns, wall_ns)

    if is_ready(obj) and state["ready_mono_ns"] is None:
        state["ready_mono_ns"] = mono_ns
        state["ready_wall_ns"] = wall_ns
        emit("ready", state, event_type, source, mono_ns, wall_ns)
        if source != "watch":
            recovered_transitions += 1
            state["timing_valid"] = False
            invalidate_timing(f"Ready for {uid} recovered from {source}")
        ready_count += 1
        if ready_start_mono_ns is None or state["create_mono_ns"] < ready_start_mono_ns:
            ready_start_mono_ns = state["create_mono_ns"]
            ready_start_wall_ns = state["create_wall_ns"]
        maybe_write_ready_marker(mono_ns, wall_ns)

    if obj.get("status", {}).get("phase") == "Failed" and state["failed_mono_ns"] is None:
        state["failed_mono_ns"] = mono_ns
        state["failed_wall_ns"] = wall_ns
        emit("failed", state, event_type, source, mono_ns, wall_ns)
        if source != "watch":
            recovered_transitions += 1
            state["timing_valid"] = False
            invalidate_timing(f"Failed for {uid} recovered from {source}")

    if event_type == "DELETED" and state["deleted_mono_ns"] is None:
        state["deleted_mono_ns"] = mono_ns
        state["deleted_wall_ns"] = wall_ns
        emit("deleted", state, event_type, source, mono_ns, wall_ns)

def list_current(source):
    command = [args.kubectl, "get", "pods", "--all-namespaces", "-l", "group=saturation", "-o", "json"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl list failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    for obj in payload.get("items", []):
        process_object(obj, "SYNC", source)
    return payload.get("metadata", {}).get("resourceVersion", "")

def worker_loop():
    global watch_errors, stop
    while True:
        item = event_queue.get()
        try:
            if item is worker_sentinel:
                return
            event, mono_ns, wall_ns = item
            process_object(
                event.get("object", {}),
                event.get("type", "UNKNOWN"),
                "watch",
                mono_ns,
                wall_ns,
            )
        except Exception as exc:
            watch_errors += 1
            invalidate_timing(f"worker processing failed: {exc}")
            stop = True
            if child is not None and child.poll() is None:
                child.terminate()
        finally:
            event_queue.task_done()

def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

def finalize():
    fields = [
        "uid", "namespace", "name", "node", "profile", "first_source",
        "timing_valid",
        "create_monotonic_ns", "bind_monotonic_ns", "ready_monotonic_ns",
        "failed_monotonic_ns", "deleted_monotonic_ns",
        "t_control_ms", "t_data_ms", "t_user_ms",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for state in sorted(states.values(), key=lambda item: item["create_mono_ns"]):
            bind = state.get("bind_mono_ns")
            ready = state.get("ready_mono_ns")
            create = state["create_mono_ns"]
            writer.writerow({
                "uid": state["uid"], "namespace": state["namespace"], "name": state["name"],
                "node": state.get("node", ""), "profile": state.get("profile", ""),
                "first_source": state.get("first_source", ""),
                "timing_valid": state.get("timing_valid", False) and timing_valid,
                "create_monotonic_ns": create,
                "bind_monotonic_ns": bind or "", "ready_monotonic_ns": ready or "",
                "failed_monotonic_ns": state.get("failed_mono_ns") or "",
                "deleted_monotonic_ns": state.get("deleted_mono_ns") or "",
                "t_control_ms": (bind - create) / 1e6 if bind and state.get("timing_valid") and timing_valid else "",
                "t_data_ms": (ready - bind) / 1e6 if bind and ready and state.get("timing_valid") and timing_valid else "",
                "t_user_ms": (ready - create) / 1e6 if ready and state.get("timing_valid") and timing_valid else "",
            })

    bound = [s for s in states.values() if s.get("bind_mono_ns") is not None]
    ready = [s for s in states.values() if s.get("ready_mono_ns") is not None]
    failed = [s for s in states.values() if s.get("failed_mono_ns") is not None]
    profiles = {}
    for state in states.values():
        profiles[state.get("profile", "")] = profiles.get(state.get("profile", ""), 0) + 1
    data_ms = [
        (s["ready_mono_ns"] - s["bind_mono_ns"]) / 1e6
        for s in ready
        if s.get("bind_mono_ns") and s.get("timing_valid") and timing_valid
    ]
    user_ms = [
        (s["ready_mono_ns"] - s["create_mono_ns"]) / 1e6
        for s in ready
        if s.get("timing_valid") and timing_valid
    ]

    summary = {
        "target": args.target,
        "expected_profile": args.expected_profile,
        "counts": {"created": len(states), "bound": len(bound), "ready": len(ready), "failed": len(failed)},
        "profile_counts": profiles,
        "profile_mismatch_count": sum(1 for s in states.values() if s.get("profile") != args.expected_profile),
        "watch": {
            "normal_timeouts": normal_timeouts,
            "abnormal_reconnects": abnormal_reconnects,
            "early_eof_count": early_eof_count,
            "gone_410_count": gone_410_count,
            "errors": watch_errors,
            "recovered_transitions": recovered_transitions,
            "queue_overflows": queue_overflows,
            "timing_valid": timing_valid,
            "timing_invalid_reasons": timing_invalid_reasons,
            "connection_durations_seconds": watch_connection_seconds,
        },
        "t_data_ms": {"p50": percentile(data_ms, .50), "p90": percentile(data_ms, .90), "p99": percentile(data_ms, .99)},
        "t_user_ms": {"p50": percentile(user_ms, .50), "p90": percentile(user_ms, .90), "p99": percentile(user_ms, .99)},
        "completed_bind_target": len(bound) >= args.target,
        "completed_ready_target": len(ready) >= args.target,
    }
    if marker_path.exists():
        summary["bind"] = json.loads(marker_path.read_text(encoding="utf-8"))
    if ready_marker_path.exists():
        summary["ready"] = json.loads(ready_marker_path.read_text(encoding="utf-8"))
    atomic_json(summary_path, summary)

def on_signal(signum, frame):
    global stop, watch_response
    stop = True
    if watch_response is not None:
        watch_response.close()

signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

worker = None
try:
    # The watcher is started before the workload, so this LIST should normally
    # be empty.  A non-empty initial LIST is retained for diagnostics but makes
    # the round's event timing invalid because historical transition times
    # cannot be reconstructed from object state.
    resource_version = list_current("initial")
    build_api_client()
    worker = threading.Thread(target=worker_loop, name="lifecycle-worker", daemon=True)
    worker.start()
    atomic_json(ready_path, {
        "ready": True,
        "resource_version": resource_version,
        "epoch_seconds": time.time(),
        "queue_capacity": event_queue.maxsize,
    })

    while not stop:
        # Read the API stream directly.  Going through `kubectl get --raw`
        # inserts an extra process and pipe which can buffer a 10k Pod burst and
        # collapse the observed Bind-to-Ready interval.
        params = {
            "watch": "1",
            "allowWatchBookmarks": "true",
            "timeoutSeconds": "60",
            "labelSelector": "group=saturation",
        }
        if resource_version:
            params["resourceVersion"] = resource_version
        url = api_server + "/api/v1/pods?" + urllib.parse.urlencode(params)
        watch_started = time.monotonic()
        saw_410 = False
        buffer = b""
        try:
            watch_response = api_http.request(
                "GET", url, preload_content=False, retries=False,
                timeout=urllib3.Timeout(connect=10.0, read=90.0),
            )
            if watch_response.status != 200:
                raw = watch_response.read().decode("utf-8", errors="replace")
                try:
                    status_obj = json.loads(raw)
                except Exception:
                    status_obj = {}
                code = status_obj.get("code", watch_response.status)
                if code == 410:
                    gone_410_count += 1
                    saw_410 = True
                    invalidate_timing("watch resourceVersion expired (410 Gone)")
                else:
                    watch_errors += 1
                    invalidate_timing(f"watch HTTP status={watch_response.status}: {raw[:500]}")
                stop = True
            else:
                for chunk in watch_response.stream(amt=16384, decode_content=True):
                    if stop:
                        break
                    mono_ns, wall_ns = now_pair()
                    buffer += chunk
                    lines = buffer.split(b"\n")
                    buffer = lines.pop()
                    for raw_line in lines:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            event_type = event.get("type", "UNKNOWN")
                            obj = event.get("object", {})
                            rv = obj.get("metadata", {}).get("resourceVersion")
                            if rv:
                                resource_version = rv
                            if event_type == "BOOKMARK":
                                continue
                            if event_type == "ERROR":
                                code = obj.get("code")
                                if code == 410:
                                    gone_410_count += 1
                                    saw_410 = True
                                    invalidate_timing("watch resourceVersion expired (410 Gone)")
                                else:
                                    watch_errors += 1
                                    invalidate_timing(f"watch ERROR event code={code}: {obj.get('message', '')}")
                                stop = True
                                watch_response.close()
                                break
                            event_queue.put_nowait((event, mono_ns, wall_ns))
                        except queue.Full:
                            queue_overflows += 1
                            invalidate_timing("lifecycle event queue overflow")
                            stop = True
                            watch_response.close()
                            break
                        except Exception as exc:
                            watch_errors += 1
                            invalidate_timing(f"watch event parse failed: {exc}")
                            log_error(f"event prefix={line[:500]!r}")
                            stop = True
                            watch_response.close()
                            break
        except Exception as exc:
            if not stop:
                watch_errors += 1
                invalidate_timing(f"direct watch failed: {exc}")
                stop = True
        finally:
            elapsed = time.monotonic() - watch_started
            watch_connection_seconds.append(round(elapsed, 6))
            if watch_response is not None:
                watch_response.close()
                watch_response.release_conn()
                watch_response = None
        if stop:
            break
        if saw_410:
            break
        if elapsed >= 45.0:
            # A server-side timeout is expected after roughly 60 seconds.
            normal_timeouts += 1
            continue
        abnormal_reconnects += 1
        early_eof_count += 1
        detail = f"watch ended early after {elapsed:.3f}s"
        invalidate_timing(detail)
        stop = True
finally:
    if watch_response is not None:
        watch_response.close()
    if worker is not None:
        event_queue.join()
        event_queue.put(worker_sentinel)
        worker.join(timeout=30)
        if worker.is_alive():
            invalidate_timing("lifecycle worker did not stop within 30 seconds")
    events_file.close()
    finalize()
    for credential_file in credential_files:
        try:
            pathlib.Path(credential_file).unlink()
        except FileNotFoundError:
            pass
PY
    chmod +x "$output"
}

write_failure_snapshot_helper() {
    local output=$1
    cat > "$output" <<'PY'
#!/usr/bin/env python3
"""Capture authoritative Dreal-F1 state directly from the Kubernetes API."""

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--target", type=int)
    parser.add_argument("--expected-profile")
    parser.add_argument("--expected-failure-rate", type=float, default=0.01)
    parser.add_argument("--selector", default="group=saturation")
    parser.add_argument("--cohort-mode", choices=("replicasets", "all"), default="replicasets")
    parser.add_argument("--require-final-ready", action="store_true")
    parser.add_argument("--require-initial-bind-timestamps", action="store_true")
    # Completion floor for a censored round: at or above it the shortfall is
    # reported as complete=false and warned about, below it the round fails.
    parser.add_argument("--min-complete", type=int, default=9900)
    parser.add_argument("--failure-ledger")
    parser.add_argument("--reap-failed", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args()
    if args.reap_failed:
        if not args.failure_ledger or not args.expected_profile:
            parser.error("--reap-failed requires --failure-ledger and --expected-profile")
    elif not args.output or args.target is None or not args.expected_profile:
        parser.error("snapshot mode requires --output, --target and --expected-profile")
    return args


def command_json(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def condition_status(pod, condition_type):
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == condition_type:
            return condition.get("status"), condition.get("lastTransitionTime")
    return None, None


def parse_timestamp(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def wilson_interval(successes, total, z=1.959963984540054):
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def controller_owner(pod, kind):
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") == kind and owner.get("controller", False):
            return owner.get("uid")
    return None


def load_failure_ledger(path):
    if not path:
        return [], []
    ledger = pathlib.Path(path)
    if not ledger.is_file():
        return [], [f"failure ledger does not exist: {ledger}"]
    pods = []
    errors = []
    for line_number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            pod = record.get("pod") or {}
            if not (pod.get("metadata") or {}).get("uid"):
                raise ValueError("record has no Pod UID")
            pods.append(pod)
        except Exception as exc:
            errors.append(f"failure ledger line {line_number}: {exc}")
    return pods, errors


def events_ledger_path(ledger_path):
    """Sibling of the Pod ledger; derived so the bash side needs no new flag."""
    ledger = pathlib.Path(ledger_path)
    return ledger.with_name(ledger.stem + "-events" + ledger.suffix)


def load_failure_events(path):
    """Events the reaper captured while they still existed.

    The API Server runs with --event-ttl=10m.  Failures land in the first
    minute of injection, but the end-of-round snapshot can run ten or more
    minutes later once the vanilla periodic baseline spends its tail in a
    bind-conflict loop -- by then every DataPlaneStartupFailed Event has been
    garbage-collected and a fully correct round used to fail its gate.
    """
    if not path:
        return [], []
    ledger = pathlib.Path(path)
    if not ledger.is_file():
        return [], []
    events, errors = [], []
    for line_number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            event = record.get("event") or {}
            if not (event.get("metadata") or {}).get("uid"):
                raise ValueError("record has no Event UID")
            events.append(event)
        except Exception as exc:
            errors.append(f"failure events ledger line {line_number}: {exc}")
    return events, errors


def reap_failed(args):
    """Persist terminal failure evidence, then release its occupied node."""
    ledger = pathlib.Path(args.failure_ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.touch(exist_ok=True)
    captured, ledger_errors = load_failure_ledger(ledger)
    if ledger_errors:
        for error in ledger_errors:
            print(error, file=sys.stderr, flush=True)
        return 2
    captured_uids = {(pod.get("metadata") or {}).get("uid") for pod in captured}
    events_ledger = events_ledger_path(ledger)
    events_ledger.touch(exist_ok=True)
    captured_events, _ = load_failure_events(events_ledger)
    captured_event_uids = {(event.get("metadata") or {}).get("uid") for event in captured_events}
    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(f"failure reaper ready: ledger={ledger}", flush=True)
    consecutive_errors = 0
    while not stop_requested:
        try:
            payload = command_json([
                args.kubectl, "get", "pods", "--all-namespaces",
                "-l", args.selector, "--field-selector", "status.phase=Failed", "-o", "json",
            ])
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            print(f"failure reaper list error ({consecutive_errors}): {exc}", file=sys.stderr, flush=True)
            if consecutive_errors >= 30:
                return 2
            time.sleep(max(args.poll_seconds, 0.1))
            continue

        for pod in payload.get("items") or []:
            meta = pod.get("metadata") or {}
            labels = meta.get("labels") or {}
            if labels.get("parasched.io/dataplane-profile") != args.expected_profile:
                continue
            uid = meta.get("uid")
            namespace = meta.get("namespace")
            name = meta.get("name")
            if not uid or not namespace or not name:
                continue
            if uid not in captured_uids:
                record = {
                    "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "pod": pod,
                }
                with ledger.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                captured_uids.add(uid)
                print(f"captured Failed Pod {namespace}/{name} uid={uid}", flush=True)
            result = subprocess.run(
                [
                    args.kubectl, "delete", "pod", name, "-n", namespace,
                    "--ignore-not-found=true", "--wait=false", "--grace-period=0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0:
                print(
                    f"failure reaper delete error for {namespace}/{name}: {result.stderr.strip()}",
                    file=sys.stderr,
                    flush=True,
                )

        # Capture the failure Events now, while they are still within the API
        # Server's TTL.  Events outlive the Pods the reaper just deleted, so
        # one sweep per poll is enough; anything not yet emitted is picked up
        # on a later poll.  A failed sweep must not stop the reaper.
        try:
            events_payload = command_json([
                args.kubectl, "get", "events", "--all-namespaces",
                "--field-selector", "reason=DataPlaneStartupFailed", "-o", "json",
            ])
        except Exception as exc:
            print(f"failure reaper event list error: {exc}", file=sys.stderr, flush=True)
            events_payload = {}
        new_events = []
        for event in events_payload.get("items") or []:
            event_uid = (event.get("metadata") or {}).get("uid")
            involved_uid = (event.get("involvedObject") or {}).get("uid")
            if not event_uid or event_uid in captured_event_uids or involved_uid not in captured_uids:
                continue
            new_events.append(event)
            captured_event_uids.add(event_uid)
        if new_events:
            with events_ledger.open("a", encoding="utf-8") as handle:
                for event in new_events:
                    record = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event}
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        time.sleep(max(args.poll_seconds, 0.1))
    print(
        f"failure reaper stopped: captured={len(captured_uids)} events={len(captured_event_uids)}",
        flush=True,
    )
    return 0


def main():
    args = parse_args()
    if args.reap_failed:
        return reap_failed(args)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        pods_payload = command_json([
            args.kubectl, "get", "pods", "--all-namespaces", "-l", args.selector, "-o", "json"
        ])
        live_pods = pods_payload.get("items") or []
        replicasets = []
        if args.cohort_mode == "replicasets":
            replicasets = command_json([
                args.kubectl, "get", "replicasets", "--all-namespaces", "-l", args.selector, "-o", "json"
            ]).get("items") or []
        events_payload = command_json([
            args.kubectl, "get", "events", "--all-namespaces",
            "--field-selector", "reason=DataPlaneStartupFailed", "-o", "json"
        ])
        events = events_payload.get("items") or []
    except Exception as exc:
        output.write_text(json.dumps({"pass": False, "errors": [str(exc)]}, indent=2) + "\n", encoding="utf-8")
        print(str(exc), file=sys.stderr)
        return 2

    historical_failed_pods, ledger_errors = load_failure_ledger(args.failure_ledger)
    ledger_events, events_ledger_errors = load_failure_events(
        events_ledger_path(args.failure_ledger) if args.failure_ledger else None
    )
    ledger_errors = list(ledger_errors) + list(events_ledger_errors)
    # Live Events first, then anything the reaper preserved that the TTL has
    # since removed; dedup on the Event's own UID.
    live_event_count = len(events)
    events_by_uid = {}
    for event in events + ledger_events:
        uid = (event.get("metadata") or {}).get("uid")
        if uid and uid not in events_by_uid:
            events_by_uid[uid] = event
    events = list(events_by_uid.values())
    pods_by_uid = {}
    for pod in historical_failed_pods + live_pods:
        uid = (pod.get("metadata") or {}).get("uid")
        if uid:
            pods_by_uid[uid] = pod
    pods = list(pods_by_uid.values())
    phase_counts = Counter((pod.get("status") or {}).get("phase") or "Unknown" for pod in pods)
    ready_pods = [pod for pod in live_pods if condition_status(pod, "Ready")[0] == "True"]
    failed_pods = [pod for pod in pods if (pod.get("status") or {}).get("phase") == "Failed"]
    pending_pods = [pod for pod in live_pods if (pod.get("status") or {}).get("phase") not in ("Running", "Succeeded", "Failed")]
    bound_pods = [pod for pod in pods if (pod.get("spec") or {}).get("nodeName")]
    matching_profile = []
    mismatched_profile = []
    for pod in pods:
        profile = ((pod.get("metadata") or {}).get("labels") or {}).get("parasched.io/dataplane-profile")
        (matching_profile if profile == args.expected_profile else mismatched_profile).append(pod)

    semantic_errors = []
    for pod in failed_pods:
        meta = pod.get("metadata") or {}
        ready_status, _ = condition_status(pod, "Ready")
        containers_ready_status, _ = condition_status(pod, "ContainersReady")
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        terminated = [(item.get("state") or {}).get("terminated") for item in statuses]
        valid_terminated = bool(terminated) and all(
            state and int(state.get("exitCode", -1)) == 1 and state.get("reason") == "DataPlaneStartupFailed"
            for state in terminated
        )
        if ready_status != "False" or containers_ready_status != "False" or not valid_terminated:
            semantic_errors.append(f"{meta.get('namespace')}/{meta.get('name')}")

    pod_uids = {(pod.get("metadata") or {}).get("uid") for pod in pods}
    relevant_events = [
        event for event in events
        if ((event.get("involvedObject") or {}).get("uid") in pod_uids)
    ]
    event_occurrences = 0
    for event in relevant_events:
        event_occurrences += int(
            event.get("count") or ((event.get("series") or {}).get("count")) or 1
        )

    initial_cohort = []
    cohort_warnings = []
    if args.cohort_mode == "all":
        initial_cohort = sorted(
            pods,
            key=lambda pod: ((pod.get("metadata") or {}).get("creationTimestamp") or "", (pod.get("metadata") or {}).get("uid") or ""),
        )[:args.target]
    else:
        rs_targets = {
            ((item.get("metadata") or {}).get("namespace"), (item.get("metadata") or {}).get("uid")): int((item.get("spec") or {}).get("replicas") or 0)
            for item in replicasets
        }
        pods_by_owner = defaultdict(list)
        for pod in pods:
            namespace = (pod.get("metadata") or {}).get("namespace")
            owner_uid = controller_owner(pod, "ReplicaSet")
            if owner_uid:
                pods_by_owner[(namespace, owner_uid)].append(pod)
        for owner, wanted in sorted(rs_targets.items()):
            candidates = sorted(
                pods_by_owner.get(owner, []),
                key=lambda pod: ((pod.get("metadata") or {}).get("creationTimestamp") or "", (pod.get("metadata") or {}).get("uid") or ""),
            )
            initial_cohort.extend(candidates[:wanted])
            if len(candidates) < wanted:
                cohort_warnings.append(f"ReplicaSet {owner[0]}/{owner[1]} has {len(candidates)}/{wanted} Pods")

    initial_bound = [pod for pod in initial_cohort if (pod.get("spec") or {}).get("nodeName")]
    create_times = []
    bind_times = []
    for pod in initial_cohort:
        create_time = parse_timestamp((pod.get("metadata") or {}).get("creationTimestamp"))
        scheduled_status, scheduled_time = condition_status(pod, "PodScheduled")
        if create_time is not None:
            create_times.append(create_time)
        if scheduled_status == "True":
            parsed = parse_timestamp(scheduled_time)
            if parsed is not None:
                bind_times.append(parsed)
    q_bind_initial = None
    bind_duration_seconds = None
    if len(create_times) == args.target and len(bind_times) == args.target:
        bind_duration_seconds = max(bind_times) - min(create_times)
        if bind_duration_seconds > 0:
            q_bind_initial = args.target / bind_duration_seconds

    finished_count = len(ready_pods) + len(failed_pods)
    failure_rate = len(failed_pods) / finished_count if finished_count else None
    failure_ci = wilson_interval(len(failed_pods), finished_count)
    errors = list(ledger_errors)
    warnings = []
    # A censored round (CL2 hit its cap with the tail still in a bind-conflict
    # loop) may legitimately stop short of the target.  Down to --min-complete
    # that is a documented shortfall, not a failed round; the completion
    # curve in round-summary.json carries the tail separately.
    complete = True

    def shortfall(count, what):
        nonlocal complete
        message = f"{what} is {count}/{args.target}"
        if count >= args.min_complete:
            complete = False
            warnings.append(message + f" (censored; at or above --min-complete {args.min_complete})")
        else:
            errors.append(message)

    if len(mismatched_profile) != 0:
        errors.append(f"{len(mismatched_profile)} Pods do not have profile {args.expected_profile}")
    if len(pods) < args.target:
        errors.append(f"observed only {len(pods)}/{args.target} Pods")
    if len(initial_cohort) != args.target:
        errors.append(f"initial cohort has {len(initial_cohort)}/{args.target} Pods")
    if len(initial_bound) != args.target:
        shortfall(len(initial_bound), "initial cohort Bound count")
    if args.require_final_ready and len(ready_pods) < args.target:
        shortfall(len(ready_pods), "final Ready count")
    if args.require_initial_bind_timestamps and q_bind_initial is None:
        errors.append(f"initial cohort has only {len(bind_times)}/{args.target} usable PodScheduled timestamps")
    if not args.require_final_ready and finished_count < args.target:
        shortfall(finished_count, "Ready-or-Failed count")
    if not failed_pods:
        errors.append("no Failed Pod was observed")
    if semantic_errors:
        errors.append(f"{len(semantic_errors)} Failed Pods have invalid failure status semantics")
    if not relevant_events:
        # The Event is corroboration for the container terminal state, which
        # the same Stage transition writes onto the Pod itself.  With every
        # failed Pod's terminal state already validated, a missing Event means
        # the TTL beat both the reaper and the snapshot, not that the failure
        # never happened.
        if failed_pods and not semantic_errors:
            warnings.append(
                "no DataPlaneStartupFailed Event survived for selected Pods; "
                f"container terminal state validated all {len(failed_pods)} Failed Pods "
                "(Event TTL likely expired before capture)"
            )
        else:
            errors.append("no DataPlaneStartupFailed Event was observed for selected Pods")
    if failure_ci is None or not (failure_ci[0] <= args.expected_failure_rate <= failure_ci[1]):
        errors.append(f"Wilson 95% CI {failure_ci} does not cover configured rate {args.expected_failure_rate}")

    uid_path = output.with_name(output.stem + "-initial-cohort-uids.txt")
    uid_path.write_text(
        "\n".join((pod.get("metadata") or {}).get("uid") or "" for pod in initial_cohort) + "\n",
        encoding="utf-8",
    )
    evidence_path = output.with_name(output.stem + "-nonready-pods.json")
    evidence_path.write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": failed_pods + pending_pods}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    events_path = output.with_name(output.stem + "-events.json")
    events_path.write_text(json.dumps({"items": relevant_events}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {
        "pass": not errors,
        "selector": args.selector,
        "expected_profile": args.expected_profile,
        "expected_failure_rate": args.expected_failure_rate,
        "live_pods": len(live_pods),
        "observed_pods": len(pods),
        "profile_matches": len(matching_profile),
        "profile_mismatches": len(mismatched_profile),
        "phase_counts": dict(sorted(phase_counts.items())),
        "bound": len(bound_pods),
        "ready": len(ready_pods),
        "failed": len(failed_pods),
        "finished_ready_or_failed": finished_count,
        "observed_failure_rate": failure_rate,
        "failure_rate_wilson_95ci": failure_ci,
        "failed_semantics_valid": len(failed_pods) - len(semantic_errors),
        "failed_semantics_invalid": len(semantic_errors),
        "failure_event_objects": len(relevant_events),
        "failure_event_occurrences": event_occurrences,
        "failure_events_live": live_event_count,
        "failure_events_from_ledger": len(ledger_events),
        "failure_ledger": args.failure_ledger,
        "failure_events_ledger": str(events_ledger_path(args.failure_ledger)) if args.failure_ledger else None,
        "historical_failed_pods": len(historical_failed_pods),
        "complete": complete,
        "min_complete": args.min_complete,
        "warnings": warnings,
        "initial_cohort": {
            "mode": args.cohort_mode,
            "pods": len(initial_cohort),
            "bound": len(initial_bound),
            "failed": sum((pod.get("status") or {}).get("phase") == "Failed" for pod in initial_cohort),
            "scheduled_timestamp_samples": len(bind_times),
            "bind_duration_seconds": bind_duration_seconds,
            "q_bind_pods_per_second": q_bind_initial,
            "uid_file": str(uid_path),
            "warnings": cohort_warnings,
        },
        "a_rebuild": len(bound_pods) / args.target,
        "nonready_evidence_file": str(evidence_path),
        "event_evidence_file": str(events_path),
        "errors": errors,
    }
    output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY
    chmod +x "$output"
}

take_over_systemd_kwok() {
    local unit state
    local -a stubborn=()
    SYSTEMD_STATE_FILE="$RUNTIME_DIR/kwok-systemd-initial.tsv"
    : > "$SYSTEMD_STATE_FILE"
    for unit in kwok kwok{1..9}; do
        printf '%s\t%s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)" >> "$SYSTEMD_STATE_FILE"
    done
    sudo systemctl stop kwok kwok{1..9}
    # No masking here.  These units have FragmentPath=/etc/systemd/system, and
    # `systemctl mask --runtime` writes to /run/systemd/system, which systemd
    # ranks *below* /etc — the command prints "Created symlink" and has no
    # effect whatsoever (measured 2026-09-12: LoadState stayed `loaded` and the
    # units restarted happily).  Plain `systemctl mask` cannot help either,
    # because the symlink path it needs is occupied by the real unit file.
    # Verify the stop instead of trusting a barrier that does not exist.
    for unit in kwok kwok{1..9}; do
        state="$(systemctl is-active "$unit" 2>/dev/null || true)"
        if [[ "$state" == "active" || "$state" == "activating" ]]; then
            stubborn+=("$unit=$state")
        fi
    done
    ((${#stubborn[@]} == 0)) \
        || die "the following system KWOK units failed to stop: ${stubborn[*]}; the matrix must own KWOK exclusively and refuses to measure under two controllers"
    SYSTEMD_TAKEN_OVER=true
    log "stopped system KWOK 0..9 and verified none is active; the original state has been recorded"
}

assert_single_kwok_owner() {
    # Exactly one KWOK controller per shard selector, and it must be one this
    # matrix started.  Two controllers on the same nodes race to apply their
    # Stages: the systemd shards carry KWOK's built-in defaults, transition
    # Pods to Ready immediately, and silently bypass the Dreal delay injection,
    # which makes the round measure Z0 behaviour while claiming to be Dreal.
    # Checked every round because the cause need not be systemd: any script,
    # timer or stray process that adds a controller produces the same corruption.
    local context=$1
    python3 - "$context" "${ACTIVE_KWOK_PIDS[@]}" <<'PY'
import os
import pathlib
import sys

context = sys.argv[1]
expected = {int(pid) for pid in sys.argv[2:]}
# No builtin-generic annotation here: the experiment host runs Python 3.8, and
# a module-level `found: dict[str, list[int]]` is evaluated at runtime and
# raises TypeError there.  py_compile does not catch it -- only executing does.
found = {}
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        argv = [
            part
            for part in (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            if part
        ]
    except OSError:
        continue
    if not argv or "kwok" not in os.path.basename(argv[0]):
        continue
    selector = next(
        (arg.split("=")[-1] for arg in argv if arg.startswith("--manage-nodes-with-annotation-selector=")),
        "",
    )
    if selector:
        found.setdefault(selector, []).append(int(entry.name))

problems = []
for selector in sorted(found):
    pids = sorted(found[selector])
    strays = [pid for pid in pids if pid not in expected]
    if len(pids) > 1:
        problems.append(f"  selector {selector}: {len(pids)} controllers pids={pids}, not started by this matrix={strays}")
    elif strays:
        problems.append(f"  selector {selector}: the single controller pid={pids[0]} was not started by this matrix")
missing = sorted(expected - {pid for pids in found.values() for pid in pids})
if missing:
    problems.append(f"  shard pids started by this matrix but now gone={missing}")

if problems:
    sys.stderr.write(f"KWOK controller ownership check failed ({context}):\n")
    sys.stderr.write("\n".join(problems) + "\n")
    sys.stderr.write(
        "  Multiple KWOK controllers over the same nodes make Stages preempt each other: a systemd shard uses\n"
        "  the built-in default Stage and marks pods Ready immediately, bypassing Dreal's data-plane latency injection and invalidating this round.\n"
    )
    sys.exit(1)
PY
}

stop_kwok_shards() {
    local pid
    if ((${#ACTIVE_KWOK_PIDS[@]})); then
        for pid in "${ACTIVE_KWOK_PIDS[@]}"; do
            kill "$pid" 2>/dev/null || true
        done
        for pid in "${ACTIVE_KWOK_PIDS[@]}"; do
            wait "$pid" 2>/dev/null || true
        done
    fi
    ACTIVE_KWOK_PIDS=()
}

drain_kwok_nodes() {
    # Module F changed node deletion to --wait=false so it does not wait 5 more minutes after the measurement window.
    # The cost is that a KWOK shard started at this moment would pull the nodes that are still being deleted into its
    # in-memory set, then retry patching those ghost nodes forever (measured at roughly 41 errors/s per shard, while
    # repeatedly creating leases for them), burning CPU and saturating the API server, polluting every later round.
    # The nodes must therefore be drained before any shard starts.
    local deadline=$((SECONDS + ${KWOK_DRAIN_TIMEOUT:-300})) remaining
    while :; do
        remaining=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
        (( remaining == 0 )) && break
        if (( SECONDS >= deadline )); then
            log "warning: timed out waiting for KWOK node deletion, $remaining remain; starting shards anyway (later rounds may be polluted)"
            break
        fi
        sleep 5
    done
    # Leftover kwok- leases outlive their nodes, and a restarted shard retries forever on the UID mismatch.
    # The scripts that predate Module F (cleanup-cluster.sh, run-experiment.sh Step 6) have always cleaned these up.
    kubectl -n kube-node-lease get leases -o name 2>/dev/null \
        | grep '/kwok-' \
        | xargs -r kubectl -n kube-node-lease delete --ignore-not-found >/dev/null 2>&1 || true
}

restore_systemd_kwok() {
    local unit state
    [[ "$SYSTEMD_TAKEN_OVER" == true ]] || return 0
    stop_kwok_shards
    drain_kwok_nodes
    # Takeover no longer masks (see take_over_systemd_kwok), but earlier runs
    # left /run/systemd/system/kwok*.service -> /dev/null symlinks behind, so
    # keep clearing them: harmless when absent, and it stops a stale barrier
    # from confusing a later `systemctl status`.
    sudo systemctl unmask --runtime kwok kwok{1..9} >/dev/null 2>&1 || true
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
    while IFS=$'\t' read -r unit state; do
        if [[ "$state" == "active" || "$state" == "activating" ]]; then
            # restart rather than start: the unit may already be running and holding the previous round's stale node set.
            sudo systemctl restart "$unit" >/dev/null 2>&1 || true
        else
            sudo systemctl stop "$unit" >/dev/null 2>&1 || true
        fi
    done < "$SYSTEMD_STATE_FILE"
    SYSTEMD_TAKEN_OVER=false
    log "restored the KWOK systemd active/inactive state from before the experiment"
}

restore_binder_after_interrupted_reset() {
    [[ "$BINDER_RESET_ACTIVE" == true ]] || return 0
    if [[ "$BINDER_RESTORE_REPLICAS" =~ ^[0-9]+$ ]]; then
        kubectl -n "$PARA_NAMESPACE" scale deployment/para-binder \
            --replicas="$BINDER_RESTORE_REPLICAS" --timeout=60s >/dev/null 2>&1 || true
        log "AdoptionStats reset was interrupted; restored para-binder replicas to $BINDER_RESTORE_REPLICAS on a best-effort basis"
    fi
    BINDER_RESET_ACTIVE=false
    BINDER_RESTORE_REPLICAS=""
}

cleanup_smoke_objects() {
    if [[ -n "$SMOKE_NAMESPACE" ]]; then
        kubectl delete namespace "$SMOKE_NAMESPACE" --ignore-not-found --wait=true --timeout=180s >/dev/null 2>&1 || true
        SMOKE_NAMESPACE=""
    fi
    if [[ -n "$SMOKE_NODE_SELECTOR" ]]; then
        kubectl delete nodes -l "$SMOKE_NODE_SELECTOR" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
        SMOKE_NODE_SELECTOR=""
    fi
}

cleanup() {
    local exit_code=$?
    if [[ -n "$SUDO_KEEPALIVE_PID" ]]; then kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true; fi
    if [[ -n "$CPU_MONITOR_PID" ]]; then kill "$CPU_MONITOR_PID" 2>/dev/null || true; fi
    if [[ -n "$WATCHER_PID" ]]; then kill -TERM "$WATCHER_PID" 2>/dev/null || true; fi
    if [[ -n "$FAILURE_REAPER_PID" ]]; then kill -TERM "$FAILURE_REAPER_PID" 2>/dev/null || true; fi
    cleanup_smoke_objects
    stop_kwok_shards
    restore_systemd_kwok
    restore_binder_after_interrupted_reset
    if [[ "$KEEP_RUNTIME" != true && -n "$RUNTIME_DIR" && -d "$RUNTIME_DIR" ]]; then
        rm -rf -- "$RUNTIME_DIR"
    fi
    exit "$exit_code"
}
trap cleanup EXIT
# A signal must end the experiment.  Let EXIT run the cleanup exactly once;
# calling cleanup directly from INT/TERM would return to the interrupted matrix.
trap 'exit 130' INT
trap 'exit 143' TERM

start_sudo_keepalive() {
    # A full matrix can run for hours; refresh the sudo timestamp periodically so the EXIT trap can still restore the services.
    (
        while sudo -n true >/dev/null 2>&1; do
            sleep 50
        done
    ) &
    SUDO_KEEPALIVE_PID=$!
}

start_kwok_shards() {
    local profile=$1 round_dir=$2 stage_file=$3 shard selector log_file
    local -a command
    stop_kwok_shards
    # The previous round's node deletion is non-blocking, so drain before starting shards or ghost nodes carry into this round.
    drain_kwok_nodes
    mkdir -p "$round_dir/kwok-logs"
    for shard in $(seq 0 9); do
        selector="fake${shard}"
        log_file="$round_dir/kwok-logs/shard-$(printf '%02d' "$shard").log"
        command=(
            "$KWOK_BIN"
            "--kubeconfig=${KUBECONFIG:-$HOME/.kube/config}"
            "--config=$KWOK_CONFIG"
        )
        if [[ "$profile" == "Dreal" || "$profile" == "Dreal-F1" ]]; then
            command+=("--config=$stage_file")
        fi
        command+=(
            "--manage-all-nodes=false"
            "--manage-nodes-with-annotation-selector=kwok.x-k8s.io/node=${selector}"
            "--manage-nodes-with-label-selector="
            "--manage-single-node="
            "--node-lease-duration-seconds=100"
        )
        "${command[@]}" > "$log_file" 2>&1 &
        ACTIVE_KWOK_PIDS+=("$!")
    done
    sleep 3
    for shard in $(seq 0 9); do
        kill -0 "${ACTIVE_KWOK_PIDS[$shard]}" 2>/dev/null \
            || die "KWOK shard $shard failed to start; see $round_dir/kwok-logs"
    done
    printf '%s\n' "${ACTIVE_KWOK_PIDS[@]}" > "$round_dir/kwok-pids.txt"
    assert_single_kwok_owner "profile=$profile round, after shard startup" \
        || die "KWOK controller ownership is wrong; aborted (diagnostics printed above)"
}

start_cpu_monitor() {
    local round_dir=$1
    local csv="$round_dir/kwok-cpu.csv"
    (
        local clk_tck previous_uptime previous_epoch now_uptime now_epoch shard pid ticks
        local delta_seconds delta_ticks cores percent
        local -a previous_ticks
        clk_tck="$(getconf CLK_TCK)"
        previous_uptime="$(awk '{print $1}' /proc/uptime)"
        previous_epoch="$(date +%s.%N)"
        for shard in $(seq 0 9); do
            pid="${ACTIVE_KWOK_PIDS[$shard]}"
            ticks="$(awk '{printf "%.0f\n", $14 + $15}' "/proc/$pid/stat" 2>/dev/null || true)"
            previous_ticks[$shard]="${ticks:-}"
        done
        echo "interval_start_epoch,interval_end_epoch,shard,pid,cpu_cores_used,cpu_percent_single_core"
        while true; do
            sleep 5
            now_uptime="$(awk '{print $1}' /proc/uptime)"
            now_epoch="$(date +%s.%N)"
            delta_seconds="$(awk -v start="$previous_uptime" -v end="$now_uptime" 'BEGIN {printf "%.9f", end-start}')"
            for shard in $(seq 0 9); do
                pid="${ACTIVE_KWOK_PIDS[$shard]}"
                ticks="$(awk '{printf "%.0f\n", $14 + $15}' "/proc/$pid/stat" 2>/dev/null || true)"
                if [[ -n "$ticks" && -n "${previous_ticks[$shard]:-}" ]]; then
                    delta_ticks=$((ticks - previous_ticks[$shard]))
                    read -r cores percent < <(
                        awk -v ticks="$delta_ticks" -v hz="$clk_tck" -v seconds="$delta_seconds" \
                            'BEGIN {cores=ticks/hz/seconds; printf "%.6f %.3f\n", cores, cores*100}'
                    )
                    printf '%s,%s,%s,%s,%s,%s\n' \
                        "$previous_epoch" "$now_epoch" "$shard" "$pid" "$cores" "$percent"
                else
                    printf '%s,%s,%s,%s,NaN,NaN\n' \
                        "$previous_epoch" "$now_epoch" "$shard" "$pid"
                fi
                previous_ticks[$shard]="${ticks:-}"
            done
            previous_uptime="$now_uptime"
            previous_epoch="$now_epoch"
        done
    ) > "$csv" &
    CPU_MONITOR_PID=$!
}

stop_cpu_monitor() {
    if [[ -n "$CPU_MONITOR_PID" ]]; then
        kill "$CPU_MONITOR_PID" 2>/dev/null || true
        wait "$CPU_MONITOR_PID" 2>/dev/null || true
        CPU_MONITOR_PID=""
    fi
}

start_lifecycle_watcher() {
    local round_dir=$1 expected_profile=$2
    rm -f "$round_dir/lifecycle/watcher-ready.json"
    python3 "$RUNTIME_DIR/watch-pod-lifecycle.py" \
        --output-dir "$round_dir/lifecycle" \
        --expected-profile "$expected_profile" \
        --target "$TARGET_PODS" \
        > "$round_dir/lifecycle-watcher.log" 2>&1 &
    WATCHER_PID=$!

    local deadline=$((SECONDS + 30))
    while [[ ! -f "$round_dir/lifecycle/watcher-ready.json" ]]; do
        if ! kill -0 "$WATCHER_PID" 2>/dev/null; then
            wait "$WATCHER_PID" 2>/dev/null || true
            WATCHER_PID=""
            log "WARN: lifecycle watcher failed to start; control-plane measurement continues"
            return 1
        fi
        if (( SECONDS >= deadline )); then
            kill -TERM "$WATCHER_PID" 2>/dev/null || true
            wait "$WATCHER_PID" 2>/dev/null || true
            WATCHER_PID=""
            log "WARN: lifecycle watcher was not ready within 30 s; control-plane measurement continues"
            return 1
        fi
        sleep 0.2
    done
    return 0
}

stop_lifecycle_watcher() {
    if [[ -n "$WATCHER_PID" ]]; then
        kill -TERM "$WATCHER_PID" 2>/dev/null || true
        wait "$WATCHER_PID" 2>/dev/null || true
        WATCHER_PID=""
    fi
}

start_failure_reaper() {
    local round_dir=$1 expected_profile=$2
    local ledger="$round_dir/failure-ledger.jsonl"
    rm -f "$ledger"
    python3 "$RUNTIME_DIR/capture-failure-snapshot.py" \
        --reap-failed \
        --failure-ledger "$ledger" \
        --expected-profile "$expected_profile" \
        --selector "group=saturation" \
        > "$round_dir/failure-reaper.log" 2>&1 &
    FAILURE_REAPER_PID=$!

    local deadline=$((SECONDS + 30))
    while [[ ! -f "$ledger" ]]; do
        if ! kill -0 "$FAILURE_REAPER_PID" 2>/dev/null; then
            wait "$FAILURE_REAPER_PID" 2>/dev/null || true
            FAILURE_REAPER_PID=""
            die "Dreal-F1 failure reaper failed to start; see $round_dir/failure-reaper.log"
        fi
        (( SECONDS < deadline )) \
            || die "Dreal-F1 failure reaper did not become ready within 30 seconds"
        sleep 0.2
    done
    log "Dreal-F1 failure reaper ready: failure evidence is persisted first, then the Failed pod is deleted to release its node"
}

stop_failure_reaper() {
    local reaper_rc=0
    if [[ -n "$FAILURE_REAPER_PID" ]]; then
        if kill -0 "$FAILURE_REAPER_PID" 2>/dev/null; then
            kill -TERM "$FAILURE_REAPER_PID" 2>/dev/null || true
        fi
        wait "$FAILURE_REAPER_PID" 2>/dev/null || reaper_rc=$?
        FAILURE_REAPER_PID=""
    fi
    return "$reaper_rc"
}

check_api_errors() {
    local start=$1 end=$2 output=$3
    python3 - "$PROMETHEUS_URL" "$start" "$end" "$output" <<'PY'
import json
import pathlib
import sys
import urllib.parse
import urllib.request

base, start, end, output = sys.argv[1:]
query = 'sum by (code) (increase(apiserver_request_total{code=~"429|5.."}[5s]))'
# The lifecycle collector watches Pods.  Node/Lease watcher terminations during
# the existing runner's node cleanup are unrelated and must not pollute this
# gate.
terminated_query = 'sum(apiserver_terminated_watchers_total{resource="pods"})'
report = {
    "request_error_query": query,
    "terminated_watcher_query": terminated_query,
    "start": float(start),
    "end": float(end),
    "sustained_request_errors": False,
    "pass": False,
}

def request(endpoint, params):
    url = base.rstrip("/") + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload.get("data", {}).get("result", [])

def scalar_at(epoch):
    result = request("/api/v1/query", {"query": terminated_query, "time": epoch})
    if len(result) != 1 or "value" not in result[0]:
        raise RuntimeError("apiserver_terminated_watchers_total is unavailable")
    return float(result[0]["value"][1])

try:
    series = request("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": 5})
    report["request_error_series"] = series
    for item in series:
        consecutive = 0
        for _, raw in item.get("values", []):
            value = float(raw)
            consecutive = consecutive + 1 if value > 0 else 0
            if consecutive >= 3:
                report["sustained_request_errors"] = True
    terminated_start = scalar_at(start)
    terminated_end = scalar_at(end)
    terminated_delta = max(0.0, terminated_end - terminated_start)
    report["terminated_watchers_start"] = terminated_start
    report["terminated_watchers_end"] = terminated_end
    report["terminated_watchers_delta"] = terminated_delta
    report["pass"] = not report["sustained_request_errors"] and terminated_delta == 0
except Exception as exc:
    report["error"] = str(exc)
pathlib.Path(output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
if not report["pass"]:
    raise SystemExit(1)
PY
}

collect_original_control_plane() {
    local experiment_name=$1 round_dir=$2 method=$3
    local source_dir="" processing_dir=""

    source_dir="$(find "$PROJECT_ROOT/experiments/results" -maxdepth 1 -type d \
        -name "*${experiment_name}*" -printf '%T@\t%p\n' \
        | sort -nr | head -n 1 | cut -f2-)"
    [[ -n "$source_dir" && -d "$source_dir/trial-1" ]] || {
        log "WARN: could not find the original runner result directory containing the experiment name: $experiment_name"
        return 1
    }

    mkdir -p "$round_dir/control-plane-source" "$round_dir/metrics-bind"
    cp "$source_dir/config.json" "$round_dir/control-plane-source/config.json"
    for item in timing.json cl2.log junit.xml; do
        [[ -f "$source_dir/trial-1/$item" ]] \
            && cp "$source_dir/trial-1/$item" "$round_dir/control-plane-source/$item"
    done
    if [[ -d "$source_dir/trial-1/metrics-saturation" ]]; then
        cp -a "$source_dir/trial-1/metrics-saturation/." "$round_dir/metrics-bind/"
    fi

    # E3 and the original result processor use a nested "parameters" config,
    # while the unmodified Godel/E2-G runner writes equivalent fields flat.  Build
    # a temporary read-only adapter instead of changing either original runner.
    processing_dir="$(mktemp -d /tmp/module-f-control-plane-XXXXXX)"
    python3 - "$source_dir/config.json" "$processing_dir/config.json" "$experiment_name" <<'PY'
import json
import pathlib
import sys

source, output, experiment_name = map(pathlib.Path, sys.argv[1:])
config = json.loads(source.read_text(encoding="utf-8"))
if isinstance(config.get("parameters"), dict):
    normalized = config
else:
    normalized = {
        "name": config.get("experiment_name", str(experiment_name)),
        "timestamp": config.get("timestamp", ""),
        "num_trials": int(config.get("num_trials", 1)),
        "parameters": {
            "num_nodes": int(config.get("num_nodes", 0)),
            "num_schedulers": int(config.get("num_schedulers", 0)),
            "num_backup": 0,
            "strategy": "godel-vanilla",
            "conflict_penalty": 0,
            "sync_period": 0,
            "num_partitions": 0,
            "sync_pattern": "none",
            "pods_per_node": int(config.get("pods_per_node", 0)),
            "cpu_request": config.get("cpu_request", ""),
            "memory_request": config.get("memory_request", ""),
            "capacity_variance": config.get("variance", ""),
        },
    }
output.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
PY
    ln -s "$source_dir/trial-1" "$processing_dir/trial-1"
    if ! python3 "$SCRIPT_DIR/process-results.py" "$processing_dir" \
        --output "$round_dir/control-plane" \
        > "$round_dir/process-control-plane.log" 2>&1; then
        rm -rf -- "$processing_dir"
        return 1
    fi
    rm -rf -- "$processing_dir"
    python3 - "$round_dir/control-plane.json" "$source_dir" \
        "$round_dir/metrics-bind/snap_summary.json" "$method" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source_result_dir = sys.argv[2]
snapshot_path = pathlib.Path(sys.argv[3])
method = sys.argv[4]
records = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(records, list) or len(records) != 1:
    raise SystemExit(f"expected exactly one processed trial, got {len(records) if isinstance(records, list) else type(records).__name__}")
record = records[0]
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
binding = snapshot.get("binding") or {}
if method in ("E2", "E3", "P1", "P4"):
    # Para-Sched's shared Binder exposes the exact exhaustion event.
    acf_count = binding.get("all_candidates_failed")
    acf_source = "parasched_all_candidates_failed_total snapshot delta"
elif method == "E2-G":
    # Unmodified Godel has no candidate-list exhaustion counter.  With its
    # single selected node, each failed binding attempt returns the Pod for a
    # new scheduling cycle, which is the equivalent ACF event for this baseline.
    acf_count = binding.get("failure")
    acf_source = "binder_binding_pod_attempts{result=\"failure\",baseline=\"godel\"} snapshot delta"
else:
    raise SystemExit(f"unsupported method for ACF adaptation: {method}")
if not isinstance(acf_count, (int, float)) or acf_count < 0:
    raise SystemExit(f"missing or invalid ACF source for {method}: {acf_count!r}")
scheduled = record.get("scheduled_pods")
if not isinstance(scheduled, (int, float)) or scheduled <= 0:
    raise SystemExit(f"invalid scheduled_pods for ACF denominator: {scheduled!r}")
acf_count = int(round(acf_count))
record["acf_count"] = acf_count
record["acf_rate"] = round(acf_count / (scheduled + acf_count), 6)
record["acf_source"] = acf_source
record["acf_denominator"] = "CL2 scheduled_pods + ACF events"
record["measurement_mode"] = "original-cl2-prometheus"
record["source_result_dir"] = source_result_dir
path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

validate_round() {
    local method=$1 profile=$2 trial=$3 order=$4 round_dir=$5 runner_exit=$6 api_pass=$7
    python3 - "$EXPERIMENT" "$method" "$profile" "$trial" "$order" "$round_dir" "$PROFILE_JSON" "$runner_exit" "$api_pass" "$NUM_SCHEDULERS" "$MIN_COMPLETE_PODS" <<'PY'
import csv
import json
import math
import pathlib
import re
import sys

experiment, method, profile, trial, order, round_dir, profile_path, runner_exit, api_pass, expected_schedulers, min_complete = sys.argv[1:]
min_complete = int(min_complete)
root = pathlib.Path(round_dir)

def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

lifecycle = load(root / "lifecycle" / "lifecycle-summary.json")
metrics = load(root / "metrics-bind" / "snap_summary.json")
control = load(root / "control-plane.json")
config = load(root / "control-plane-source" / "config.json") or {}
target_profile = load(pathlib.Path(profile_path))
failure_snapshot = load(root / "failure-snapshot.json")
failures = []
warnings = []
# Set when CL2 hit its saturation cap with the tail still looping but at
# least min_complete Pods scheduled: the round is valid, its makespan is
# right-censored, and the completion curve below carries the tail (10.6.2).
censored = False
scheduled_pods = None
parameters = config.get("parameters") if isinstance(config.get("parameters"), dict) else config
if int(parameters.get("num_schedulers") or 0) != int(expected_schedulers):
    failures.append(f"runner config num_schedulers={parameters.get('num_schedulers')}; expected {expected_schedulers}")
# These expectations and the E3_K/P4_K/EVENT_*/PERIODIC_* definitions at the top of the script are two independent sources; changing one requires changing the other.
if method == "E2":
    if int(parameters.get("num_backup") or 0) != 0:
        failures.append(f"E2 num_backup={parameters.get('num_backup')}; expected 0")
    if float(parameters.get("conflict_penalty") or 0) != 0:
        failures.append(f"E2 conflict_penalty={parameters.get('conflict_penalty')}; expected 0")
    if parameters.get("strategy") != "QualityFirst":
        failures.append(f"E2 strategy={parameters.get('strategy')}; expected QualityFirst")
elif method == "E3":
    if int(parameters.get("num_backup") or 0) != 2:
        failures.append(f"E3 num_backup={parameters.get('num_backup')}; expected 2")
    if float(parameters.get("conflict_penalty") or 0) != 0.5:
        failures.append(f"E3 conflict_penalty={parameters.get('conflict_penalty')}; expected 0.5")
    if parameters.get("strategy") != "QualityFirst":
        failures.append(f"E3 strategy={parameters.get('strategy')}; expected QualityFirst")
elif method == "P1":
    if int(parameters.get("num_backup") or 0) != 0:
        failures.append(f"P1 num_backup={parameters.get('num_backup')}; expected 0")
    if float(parameters.get("conflict_penalty") or 0) != 0:
        failures.append(f"P1 conflict_penalty={parameters.get('conflict_penalty')}; expected 0")
    if parameters.get("strategy") != "QualityFirst":
        failures.append(f"P1 strategy={parameters.get('strategy')}; expected QualityFirst")
elif method == "P4":
    if int(parameters.get("num_backup") or 0) != 2:
        failures.append(f"P4 num_backup={parameters.get('num_backup')}; expected 2")
    if float(parameters.get("conflict_penalty") or 0) != 0.5:
        failures.append(f"P4 conflict_penalty={parameters.get('conflict_penalty')}; expected 0.5")
    if parameters.get("strategy") != "QualityFirst":
        failures.append(f"P4 strategy={parameters.get('strategy')}; expected QualityFirst")
if method in ("E2", "E3"):
    if float(parameters.get("sync_period") or 0) != 0.1:
        failures.append(f"{method} sync_period={parameters.get('sync_period')}; expected 0.1")
    if int(parameters.get("num_partitions") or 0) != 1:
        failures.append(f"{method} num_partitions={parameters.get('num_partitions')}; expected 1")
    if parameters.get("sync_pattern") != "diff":
        failures.append(f"{method} sync_pattern={parameters.get('sync_pattern')}; expected diff")
if method in ("P1", "P4"):
    if float(parameters.get("sync_period") or 0) != 1.0:
        failures.append(f"{method} sync_period={parameters.get('sync_period')}; expected 1.0")
    if int(parameters.get("num_partitions") or 0) != 1:
        failures.append(f"{method} num_partitions={parameters.get('num_partitions')}; expected 1")
    if parameters.get("sync_pattern") != "glob":
        failures.append(f"{method} sync_pattern={parameters.get('sync_pattern')}; expected glob")
if not lifecycle:
    warnings.append("lifecycle diagnostic did not produce lifecycle-summary.json")
else:
    if not lifecycle.get("completed_bind_target"):
        warnings.append("lifecycle diagnostic did not observe 10000 Bind transitions")
    if not lifecycle.get("completed_ready_target"):
        warnings.append("lifecycle diagnostic did not observe 10000 Ready transitions")
    target = int(lifecycle.get("target", 10000))
    counts = lifecycle.get("counts", {})
    for name in ("created", "bound", "ready"):
        if counts.get(name) != target:
            warnings.append(f"lifecycle diagnostic {name}={counts.get(name)}; expected {target}")
    if counts.get("failed", 0) != 0:
        warnings.append(f"lifecycle diagnostic observed failed Pods: {counts.get('failed')}")
    if lifecycle.get("profile_mismatch_count") != 0:
        warnings.append(f"lifecycle diagnostic profile mismatch: {lifecycle.get('profile_mismatch_count')}")
    watch = lifecycle.get("watch", {})
    for name in ("abnormal_reconnects", "early_eof_count", "gone_410_count", "errors", "recovered_transitions", "queue_overflows"):
        if watch.get(name, 0) != 0:
            warnings.append(f"lifecycle diagnostic {name}={watch.get(name)}")
    if not watch.get("timing_valid", False):
        warnings.append(f"lifecycle diagnostic timing invalid: {watch.get('timing_invalid_reasons', [])}")
if not metrics:
    failures.append("missing the original runner's Prometheus snapshot")
if control is None:
    failures.append("missing the original CL2/Prometheus processing result")
elif not isinstance(control, dict):
    failures.append(f"invalid original CL2/Prometheus processing result format: expected object, got {type(control).__name__}")
else:
    if control.get("measurement_mode") != "original-cl2-prometheus":
        failures.append("control-plane measurement source is not original-cl2-prometheus")
    scheduled_pods = int(control.get("scheduled_pods") or 0)
    cl2_incomplete = control.get("test_status") != "Success" or bool(control.get("is_timeout"))
    if cl2_incomplete and scheduled_pods >= min_complete:
        # The cap cut off the saturation tail, not the measurement: Q_bind and
        # ACF below cover [start, cap] exactly as the runner collected them.
        censored = True
        warnings.append(
            f"CL2 saturation window censored at its cap: scheduled_pods={scheduled_pods}/10000 "
            f"(≥ min_complete {min_complete}); makespan is right-censored, tail loop excluded"
        )
    else:
        if cl2_incomplete:
            failures.append(f"original CL2 test did not complete successfully: status={control.get('test_status')} timeout={control.get('is_timeout')}")
        if scheduled_pods != 10000:
            failures.append(f"original CL2 scheduled_pods={scheduled_pods}; expected 10000")
    if int(control.get("bind_success") or 0) != 10000:
        warnings.append(f"Prometheus raw bind_success={control.get('bind_success')}; R_conflict denominator uses CL2's completed 10000 Pods, matching the original processor")
    if not isinstance(control.get("throughput_pods_per_s"), (int, float)) or control.get("throughput_pods_per_s") <= 0:
        failures.append("original CL2 produced no valid Q_bind")
    if not isinstance(control.get("bind_conflict_rate"), (int, float)):
        failures.append("Prometheus produced no valid R_conflict")
    if not isinstance(control.get("acf_count"), int) or control.get("acf_count") < 0:
        failures.append("Prometheus snapshot produced no valid ACF counts")
    if not isinstance(control.get("acf_rate"), (int, float)) or not 0 <= control.get("acf_rate") <= 1:
        failures.append("Prometheus snapshot produced no valid ACF ratio")
    if not control.get("acf_source"):
        failures.append("ACF metric has no traceable source")
if int(runner_exit) != 0:
    # On a censored round the non-zero exit is CL2's cap propagated by the
    # runtime copy's strict_exit; the collection and cleanup behind it ran.
    (warnings if censored else failures).append(f"runner exit={runner_exit}" + (" (censored round)" if censored else ""))
if profile == "Dreal-F1":
    reaper_exit_path = root / "failure-reaper-exit.txt"
    try:
        reaper_exit = int(reaper_exit_path.read_text(encoding="utf-8").strip())
    except Exception:
        reaper_exit = None
    if reaper_exit != 0:
        failures.append(f"Dreal-F1 failure reaper exit={reaper_exit}")
    if not failure_snapshot:
        failures.append("missing the pre-cleanup Dreal-F1 failure-snapshot.json")
    elif not failure_snapshot.get("pass"):
        failures.append(f"Dreal-F1 failure-injection gate failed: {failure_snapshot.get('errors', [])}")
if api_pass != "true":
    warnings.append("API Server stability diagnostic failed; retained as warning by measurement-first policy")

runner_log = (root / "runner.log").read_text(encoding="utf-8", errors="replace") if (root / "runner.log").exists() else ""
cl2_codes = [int(value) for value in re.findall(r"WARNING: CL2 exited(?: with code)?\s+(\d+)", runner_log)]
cl2_exit_code = cl2_codes[-1] if cl2_codes else (0 if int(runner_exit) == 0 else None)
if cl2_exit_code not in (None, 0):
    (warnings if censored else failures).append(f"CL2 exit={cl2_exit_code}" + (" (censored round)" if censored else ""))

# Completion curve, rebuilt from CL2's per-namespace WaitForControlledPodsRunning
# reports.  Under K=0 + periodic sync + data-plane latency the last 1% of Pods
# can take longer than the first 99%, so a single makespan number conflates
# steady-state throughput with the saturation tail; the landmarks keep them
# separable (T99 window vs T100 - T99 tail) without re-querying Prometheus.
_WAIT_LINE = re.compile(
    r"^I(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{6}).*?namespace\((?P<ns>[^)]+)\).*?"
    r"Pods: \d+ out of \d+ created, (?P<run>\d+) running"
)
_running_by_ns = {}
_curve = []
for _line in runner_log.splitlines():
    if "wait_for_pods.go:122" not in _line:
        continue
    _m = _WAIT_LINE.match(_line)
    if not _m:
        continue
    _mon, _day, _h, _mi, _s, _us = (int(x) for x in _m.groups()[:6])
    _t = ((_mon * 31 + _day) * 24 + _h) * 3600 + _mi * 60 + _s + _us / 1e6
    _running_by_ns[_m.group("ns")] = int(_m.group("run"))
    _curve.append((_t, sum(_running_by_ns.values())))


def _first_at(target):
    for _t, _v in _curve:
        if _v >= target:
            return round(_t - _curve[0][0], 3)
    return None


completion_curve = {"source": "cl2 WaitForControlledPodsRunning per-namespace reports", "samples": len(_curve)}
if _curve:
    _peak = max(_v for _, _v in _curve)
    completion_curve.update({
        "peak_running": _peak,
        "observed_seconds": round(_curve[-1][0] - _curve[0][0], 3),
        "t90": _first_at(9000), "t95": _first_at(9500), "t99": _first_at(9900),
        "t99_9": _first_at(9990), "t99_99": _first_at(9999), "t100": _first_at(10000),
    })
    _t99, _t100 = completion_curve["t99"], completion_curve["t100"]
    completion_curve["q_bind_at_t99"] = round(9900 / _t99, 3) if _t99 else None
    completion_curve["tail_seconds"] = round(_t100 - _t99, 3) if (_t99 is not None and _t100 is not None) else None
    completion_curve["tail_right_censored"] = _t100 is None
    completion_curve["tail_fraction"] = (
        round(completion_curve["tail_seconds"] / _t100, 4) if completion_curve["tail_seconds"] is not None and _t100 else None
    )
    if _t99 is None:
        warnings.append(f"completion curve never reached 9900 Pods (peak {_peak}); T99 window undefined")
else:
    warnings.append("completion curve unavailable: no WaitForControlledPodsRunning reports in runner.log")

cpu_window = {"name": "create_to_ready", "threshold_cores": 0.8, "interval_seconds": 5, "shards": {}}
cpu_by_shard = {}
cpu_path = root / "kwok-cpu.csv"
source_timing = load(root / "control-plane-source" / "timing.json") or {}
saturation_window = source_timing.get("saturation") or {}
window_start = saturation_window.get("start")
window_end = saturation_window.get("end")
if cpu_path.exists() and window_start is not None and window_end is not None:
    for row in csv.DictReader(cpu_path.open(encoding="utf-8")):
        try:
            interval_start = float(row["interval_start_epoch"])
            interval_end = float(row["interval_end_epoch"])
            value = float(row["cpu_cores_used"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value) or interval_end < float(window_start) or interval_start > float(window_end):
            continue
        shard = row["shard"]
        cpu_by_shard.setdefault(shard, []).append((interval_start, value))

def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] * (high - position) + ordered[high] * (position - low)

for shard, samples in sorted(cpu_by_shard.items(), key=lambda item: int(item[0])):
    samples.sort()
    values = [item[1] for item in samples]
    consecutive = longest = 0
    for value in values:
        consecutive = consecutive + 1 if value >= 0.8 else 0
        longest = max(longest, consecutive)
    cpu_window["shards"][shard] = {
        "samples": len(values),
        "p95_cores": percentile(values, .95),
        "max_cores": max(values),
        "longest_consecutive_at_or_above_threshold": longest,
    }
cpu_window["start_epoch_seconds"] = window_start
cpu_window["end_epoch_seconds"] = window_end
cpu_window["pass"] = len(cpu_window["shards"]) == 10 and all(
    item["p95_cores"] < 0.8 and item["longest_consecutive_at_or_above_threshold"] < 3
    for item in cpu_window["shards"].values()
)
if len(cpu_window["shards"]) != 10:
    warnings.append(f"KWOK CPU monitoring covered {len(cpu_window['shards'])}/10 shards in Create-to-Ready window")
elif not cpu_window["pass"]:
    warnings.append("KWOK shard CPU diagnostic exceeded threshold")

alive = (root / "kwok-alive-end.txt").read_text(encoding="utf-8").splitlines() if (root / "kwok-alive-end.txt").exists() else []
if len(alive) != 10 or any(line.split("\t")[-1] != "alive" for line in alive):
    warnings.append("at least one KWOK shard exited before the round finished")

fidelity = {"applicable": profile in ("Dreal", "Dreal-F1"), "evaluated": False, "pass": None}
lifecycle_csv = root / "lifecycle" / "lifecycle.csv"
lifecycle_timing_valid = bool(lifecycle and (lifecycle.get("watch") or {}).get("timing_valid"))
initial_cohort_control = {"applicable": profile == "Dreal-F1", "evaluated": False}
if profile == "Dreal-F1" and failure_snapshot:
    uid_file = pathlib.Path((failure_snapshot.get("initial_cohort") or {}).get("uid_file") or "")
    if lifecycle_timing_valid and lifecycle_csv.exists() and uid_file.is_file():
        cohort_uids = {line.strip() for line in uid_file.read_text(encoding="utf-8").splitlines() if line.strip()}
        cohort_rows = []
        with lifecycle_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("uid") not in cohort_uids:
                    continue
                if str(row.get("timing_valid", "")).lower() != "true":
                    continue
                try:
                    create_ns = int(row["create_monotonic_ns"])
                    bind_ns = int(row["bind_monotonic_ns"])
                except (KeyError, TypeError, ValueError):
                    continue
                cohort_rows.append((create_ns, bind_ns))
        expected = len(cohort_uids)
        if expected and len(cohort_rows) == expected:
            duration = (max(bind for _, bind in cohort_rows) - min(create for create, _ in cohort_rows)) / 1e9
            if duration > 0:
                initial_cohort_control = {
                    "applicable": True,
                    "evaluated": True,
                    "pods": expected,
                    "duration_seconds": duration,
                    "q_bind_pods_per_second": expected / duration,
                    "source": "lifecycle watch restricted to ReplicaSet-derived initial cohort UIDs",
                }
        if not initial_cohort_control["evaluated"]:
            initial_cohort_control["reason"] = f"usable lifecycle rows {len(cohort_rows)}/{expected}"
    else:
        initial_cohort_control["reason"] = "lifecycle timing invalid/unavailable or initial-cohort UID file missing"
    if not initial_cohort_control["evaluated"]:
        warnings.append("the first cohort's watcher Q_bind is unavailable; keeping the original CL2 Q_bind as the primary measure")
if profile in ("Dreal", "Dreal-F1") and lifecycle_timing_valid and target_profile and lifecycle_csv.exists():
    values = []
    with lifecycle_csv.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("t_data_ms"):
                values.append(float(row["t_data_ms"]))
    values.sort()
    bins = []
    for item in target_profile["bins"]:
        delay = item["kwok_delay"]
        bins.append((float(delay["durationMilliseconds"]), float(delay["jitterDurationMilliseconds"]), float(item["weight"]) / 10000.0))

    def target_cdf(x):
        total = 0.0
        for lower, upper, weight in bins:
            if x >= upper:
                total += weight
            elif x > lower:
                total += weight * (x - lower) / (upper - lower)
        return min(1.0, total)

    ks = 0.0
    count = len(values)
    if count:
        for index, value in enumerate(values, 1):
            expected = target_cdf(value)
            ks = max(ks, abs(index / count - expected), abs((index - 1) / count - expected))

    quantile_targets = {"p50": bins[0][1], "p90": bins[2][1], "p99": bins[3][1]}
    observed = lifecycle.get("t_data_ms", {})
    quantiles = {}
    quantiles_pass = True
    for name, target in quantile_targets.items():
        actual = observed.get(name)
        threshold = max(50.0, 0.10 * target)
        passed = actual is not None and abs(actual - target) <= threshold
        quantiles[name] = {"target_ms": target, "observed_ms": actual, "threshold_ms": threshold, "pass": passed}
        quantiles_pass = quantiles_pass and passed
    fidelity.update({"evaluated": True, "samples": count, "ks_distance": ks, "ks_threshold": 0.08, "quantiles": quantiles, "pass": count >= 10000 and ks <= 0.08 and quantiles_pass})
    if not fidelity["pass"]:
        warnings.append("lifecycle diagnostic could not confirm per-round Dreal injection fidelity")
elif profile in ("Dreal", "Dreal-F1"):
    fidelity["reason"] = "lifecycle diagnostic timing unavailable; rely on the preregistered 2000-Pod injection gate"
    warnings.append("per-pod Dreal fidelity was not recomputed for this round; the control-plane result is still usable")

summary = {
    "experiment": experiment, "order": int(order), "trial": int(trial),
    "method": method, "dataplane_profile": profile,
    "runner_exit": int(runner_exit), "cl2_exit_code": cl2_exit_code, "lifecycle": lifecycle,
    "measurement_mode": "original-cl2-prometheus", "control_plane": control,
    "control_plane_metrics": metrics, "injection_fidelity": fidelity,
    "failure_injection": failure_snapshot,
    "initial_cohort_control": initial_cohort_control,
    "kwok_cpu": cpu_window,
    "censored": censored,
    "scheduled_pods": scheduled_pods,
    "min_complete_pods": min_complete,
    "completion_curve": completion_curve,
    "valid": not failures, "failures": failures, "warnings": warnings,
}
(root / "round-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
if failures:
    print("round verdict: INVALID", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    raise SystemExit(1)
print("round verdict: VALID" + (" (censored: CL2 was truncated at the limit, makespan is right-censored)" if censored else ""))
for warning in warnings:
    print(f"  WARN: {warning}")
if completion_curve.get("t99") is not None:
    print(
        f"  completion: T99={completion_curve['t99']}s  T100={completion_curve.get('t100')}s  "
        f"tail={completion_curve.get('tail_seconds')}s  Q_bind@T99={completion_curve.get('q_bind_at_t99')}"
    )
print(f"  Q_bind={control['throughput_pods_per_s']:.2f} pods/s (CL2)")
print(f"  R_conflict={control['bind_conflict_rate']} (Prometheus)")
print(f"  ACF={control['acf_rate']} ({control['acf_count']} events; Prometheus snapshot)")
if profile in ("Dreal", "Dreal-F1") and fidelity.get("evaluated"):
    print(f"  injection KS={fidelity.get('ks_distance'):.4f}")
PY
}

write_run_metadata() {
    local git_commit classification
    git_commit="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    classification="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("classification","unknown"))' "$GATE_REPORT")"
    python3 - "$RUN_ROOT/metadata.json" "$RUN_ID" "$PLAN_SEED" "$git_commit" "$PROFILE_JSON" "$GATE_REPORT" "$classification" "$EXPERIMENT" "$TRIALS" "$TOTAL_ROUNDS" "$NUM_SCHEDULERS" <<'PY'
import json, pathlib, sys
output, run_id, seed, commit, profile, gate, classification, experiment, trials, total_rounds, schedulers = sys.argv[1:]
if experiment in ("F1-E", "F2-E"):
    methods = ["E2", "E3"]
    method_config = {
        "E2": {"implementation": "ParKour/Para-Sched", "mode": "event", "K": 0, "strategy": "QualityFirst", "penalty": 0, "sync_period_seconds": 0.1, "partitions": 1, "sync_pattern": "diff", "paper_name": "vanilla-E"},
        "E3": {"implementation": "ParKour/Para-Sched", "mode": "event", "K": 2, "strategy": "QualityFirst", "penalty": 0.5, "sync_period_seconds": 0.1, "partitions": 1, "sync_pattern": "diff", "paper_name": "ParKour-E"},
    }
    acf_sources = {
        "E2": "parasched_all_candidates_failed_total; exact Para-Sched candidate-list exhaustion counter",
        "E3": "parasched_all_candidates_failed_total",
    }
elif experiment in ("F1-P", "F2-P"):
    methods = ["P1", "P4"]
    method_config = {
        "P1": {"implementation": "ParKour/Para-Sched", "mode": "periodic", "K": 0, "strategy": "QualityFirst", "penalty": 0, "sync_period_seconds": 1.0, "partitions": 1, "sync_pattern": "glob", "paper_name": "vanilla-P"},
        "P4": {"implementation": "ParKour/Para-Sched", "mode": "periodic", "K": 2, "strategy": "QualityFirst", "penalty": 0.5, "sync_period_seconds": 1.0, "partitions": 1, "sync_pattern": "glob", "paper_name": "ParKour-P"},
    }
    acf_sources = {"P1": "parasched_all_candidates_failed_total", "P4": "parasched_all_candidates_failed_total"}
else:
    raise SystemExit(f"unsupported experiment: {experiment}")
profiles = ["Dreal-F1"] if experiment in ("F2-E", "F2-P") else ["Z0", "Dreal"]
metadata = {
    "schema_version": 2,
    "experiment": experiment,
    "run_id": run_id,
    "plan_seed": int(seed),
    "matrix": {"methods": methods, "profiles": profiles, "trials_per_group": int(trials), "total_rounds": int(total_rounds)},
    "environment": {"kwok_nodes": 10000, "target_pods": 10000, "schedulers": int(schedulers), "workload": "HC-V", "variance": 0.6, "cpu_request": "24000m", "memory_request": "192Gi", "pods_per_node": 1, "burst_deployments_per_second": 500},
    "methods": method_config,
    "measurement": {
        "primary": "original CL2 + Prometheus pipeline",
        "q_bind_source": "CL2 scheduled pods / CL2 saturation duration",
        "r_conflict_source": "Prometheus candidate-level bind conflict and bind success deltas",
        "acf_definition": "all candidates failed and a fresh scheduling cycle is required",
        "acf_source_by_method": acf_sources,
        "acf_denominator": "CL2 scheduled pods + ACF events",
        "lifecycle_watcher": "non-blocking diagnostic only",
    },
    "dataplane": {
        "profile_json": profile,
        "gate_report": gate,
        "classification": classification,
        "name_retained": "Dreal",
        "failure_rate": 0.01 if experiment in ("F2-E", "F2-P") else 0,
        "failure_semantics": "post-bind terminal Failed; module-F evidence ledger records the failure before deleting the terminal Pod to release node capacity; controller creates replacement" if experiment in ("F2-E", "F2-P") else None,
        "failure_gate": "pre-cleanup Kubernetes API snapshot merged with the module-F failure ledger + Wilson 95% CI" if experiment in ("F2-E", "F2-P") else None,
        "warning": "The current Dreal file comes from a synthetic F0. Until it is replaced by a profile calibrated on real workers, it must not be described as a real data-plane calibration result.",
    },
    "git_commit": commit,
}
pathlib.Path(output).write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

reset_adoption_stats_for_para() {
    local round_dir=$1
    local before="$round_dir/artifacts/adoption-stats-before-reset.json"
    local after="$round_dir/artifacts/adoption-stats-after-reset.json"

    log "resetting AdoptionStats before the Para-Sched round so it does not inherit the previous round's statistics"
    kubectl get adoptionstats.scheduling.parscheduler.io default -o json > "$before" 2>/dev/null \
        || printf '{}\n' > "$before"

    BINDER_RESTORE_REPLICAS="$(kubectl -n "$PARA_NAMESPACE" get deployment para-binder \
        -o jsonpath='{.spec.replicas}')"
    [[ "$BINDER_RESTORE_REPLICAS" =~ ^[0-9]+$ ]] \
        || die "cannot read the current para-binder replica count"
    BINDER_RESET_ACTIVE=true

    kubectl -n "$PARA_NAMESPACE" scale deployment/para-binder --replicas=0 --timeout=60s >/dev/null
    kubectl -n "$PARA_NAMESPACE" wait --for=delete pod -l app=para-binder --timeout=90s >/dev/null \
        || die "timed out waiting for para-binder to stop before resetting AdoptionStats"

    kubectl delete adoptionstats.scheduling.parscheduler.io default \
        --ignore-not-found --wait=true --timeout=60s >/dev/null
    kubectl apply -f "$ADOPTION_STATS_MANIFEST" >/dev/null
    kubectl get adoptionstats.scheduling.parscheduler.io default -o json > "$after"

    python3 - "$after" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
obj = json.loads(path.read_text(encoding="utf-8"))
status = obj.get("status") or {}
for field in ("totalBindings", "successCount", "failureCount"):
    if int(status.get(field, 0) or 0) != 0:
        raise SystemExit(f"AdoptionStats reset verification failed: {field}={status.get(field)}")
for field in ("globalCounts", "partitionCounts", "nodeCounts"):
    if status.get(field):
        raise SystemExit(f"AdoptionStats reset verification failed: {field} is not empty")
print("AdoptionStats reset verification: PASS (empty status)")
PY
}

round_dir_for() {
    local order=$1 trial=$2 method=$3 profile=$4 attempt=${5:-1} name
    name="$(printf 'round-%02d-trial-%02d-%s-%s' "$order" "$trial" "$method" "$profile")"
    (( attempt > 1 )) && name="${name}-retry$((attempt - 1))"
    printf '%s/%s\n' "$RUN_ROOT" "$name"
}

# Decide whether an invalid round is "retryable" or a "hard failure".
# The only retryable case: the injection-rate gate's Wilson CI did not cover the configured value, while CL2 exited normally and the failure semantics were valid.
classify_round_failure() {
    local round_dir=$1
    python3 - "$round_dir/round-summary.json" "$round_dir/failure-snapshot.json" <<'PY'
import json, pathlib, re, sys


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


summary = load(sys.argv[1])
snapshot = load(sys.argv[2])
failures = summary.get("failures") or []

if not failures:
    print("hard"); raise SystemExit
# When a gate failure makes the runner exit non-zero, cl2_exit_code is often null; only an explicit non-zero value counts as a CL2 failure.
# A genuine CL2 problem leaves "CL2 exit=" / "original CL2 test did not complete successfully" in failures, which the loop below catches.
cl2_exit = summary.get("cl2_exit_code")
if cl2_exit is not None and cl2_exit != 0:
    print("hard"); raise SystemExit
if int(snapshot.get("failed_semantics_invalid") or 0) != 0:
    print("hard"); raise SystemExit

for item in failures:
    text = str(item)
    if text.startswith("runner exit="):
        continue  # derived from the gate failure; it does not determine the nature on its own
    if "failure-injection gate failed" in text:
        inner = re.findall(r"'([^']*)'", text) or [text]
        if all("Wilson 95% CI" in e and "does not cover" in e for e in inner):
            continue
    print("hard"); raise SystemExit
print("retryable")
PY
}

run_one_round() {
    local order=$1 trial=$2 method=$3 profile=$4 attempt=${5:-1}
    local round_name round_dir expected_profile template template_relative template_reference_base stage_file runner name runner_exit failure_reaper_exit=0
    local start_epoch end_epoch measurement_start measurement_end api_pass=true shard pid state
    local -a module_env

    round_dir="$(round_dir_for "$order" "$trial" "$method" "$profile" "$attempt")"
    round_name="$(basename "$round_dir")"
    mkdir -p "$round_dir/lifecycle" "$round_dir/artifacts"
    log "[$order/$TOTAL_ROUNDS] trial=$trial $method-$profile"

    case "$profile" in
        Dreal) expected_profile="calibrated-v1" ;;
        Dreal-F1) expected_profile="calibrated-f1-v1" ;;
        Z0) expected_profile="zero-v1" ;;
        *) die "unknown data-plane profile: $profile" ;;
    esac
    template="$round_dir/artifacts/workload-template.yaml"
    render_workload_template "$method" "$profile" "$template"
    if [[ "$profile" == "Dreal-F1" ]]; then
        stage_file="$RUN_ROOT/artifacts/pod-ready-dreal-f1.yaml"
        # CL2 resolves objectTemplatePath relative to the directory containing
        # the active test config.  F2 uses a runtime CL2 copy, not KWOK_SETUP.
        template_reference_base="$RUNTIME_DIR"
    else
        stage_file="$RUN_ROOT/artifacts/pod-ready-dreal.yaml"
        template_reference_base="$KWOK_SETUP"
    fi
    template_relative="$(realpath --relative-to="$template_reference_base" "$template")"

    python3 - "$round_dir/round-metadata.json" "$order" "$trial" "$method" "$profile" "$expected_profile" "$template_relative" <<'PY'
import json, pathlib, sys
path, order, trial, method, profile, label, template = sys.argv[1:]
value = {"order": int(order), "trial": int(trial), "method": method, "profile": profile, "pod_profile_label": label, "deployment_spec_relative_to_active_cl2_config": template, "status": "RUNNING"}
pathlib.Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
PY

    if [[ "$method" != "E2-G" ]]; then
        reset_adoption_stats_for_para "$round_dir"
    fi

    start_kwok_shards "$profile" "$round_dir" "$stage_file"
    start_cpu_monitor "$round_dir"
    if ! start_lifecycle_watcher "$round_dir" "$expected_profile"; then
        log "the lifecycle watcher is diagnostic only and does not stop this round"
    fi
    if [[ "$profile" == "Dreal-F1" ]]; then
        start_failure_reaper "$round_dir" "$expected_profile"
    fi
    start_epoch="$(date +%s)"

    name="${EXPERIMENT}-${method}-${profile}-t${trial}-${RUN_ID}"
    if [[ "$method" == "E2-G" ]]; then
        runner="$RUNTIME_DIR/run-e2g-runtime.sh"
        local -a command=(
            "$runner" --name "$name" --nodes "$NUM_NODES" --schedulers "$NUM_SCHEDULERS"
            --trials 1 --pods-per-node "$PODS_PER_NODE" --cpu-request "$CPU_REQUEST"
            --memory-request "$MEMORY_REQUEST" --variance "$VARIANCE"
            --prometheus-url "$PROMETHEUS_URL" --collect-logs
        )
    else
        runner="$RUNTIME_DIR/run-para-runtime.sh"
        local backup strategy penalty sync_period partitions sync_pattern
        case "$method" in
            E2)
                backup=0; strategy="QualityFirst"; penalty=0
                sync_period="$EVENT_SYNC_PERIOD"; partitions="$EVENT_PARTITIONS"; sync_pattern="$EVENT_SYNC_PATTERN"
                ;;
            E3)
                backup="$E3_K"; strategy="$E3_STRATEGY"; penalty="$E3_PENALTY"
                sync_period="$EVENT_SYNC_PERIOD"; partitions="$EVENT_PARTITIONS"; sync_pattern="$EVENT_SYNC_PATTERN"
                ;;
            P1)
                backup=0; strategy="QualityFirst"; penalty=0
                sync_period="$PERIODIC_SYNC_PERIOD"; partitions="$PERIODIC_PARTITIONS"; sync_pattern="$PERIODIC_SYNC_PATTERN"
                ;;
            P4)
                backup="$P4_K"; strategy="$P4_STRATEGY"; penalty="$P4_PENALTY"
                sync_period="$PERIODIC_SYNC_PERIOD"; partitions="$PERIODIC_PARTITIONS"; sync_pattern="$PERIODIC_SYNC_PATTERN"
                ;;
            *) die "Para-Sched method not implemented: $method" ;;
        esac
        local -a command=(
            "$runner" --name "$name" --nodes "$NUM_NODES" --schedulers "$NUM_SCHEDULERS"
            --trials 1 --pods-per-node "$PODS_PER_NODE" --cpu-request "$CPU_REQUEST"
            --memory-request "$MEMORY_REQUEST" --variance "$VARIANCE"
            --sync-period "$sync_period" --partitions "$partitions" --sync-pattern "$sync_pattern"
            --backup "$backup" --strategy "$strategy"
            --penalty "$penalty" --prometheus-url "$PROMETHEUS_URL" --collect-logs
        )
    fi

    module_env=("MODULE_F_DEPLOYMENT_SPEC=$template_relative")
    if [[ "$profile" == "Dreal-F1" ]]; then
        module_env+=(
            "MODULE_F_CL2_CONFIG=$RUNTIME_DIR/cl2-saturation-f2-e.yaml"
            "MODULE_F_FAILURE_SNAPSHOT_SCRIPT=$RUNTIME_DIR/capture-failure-snapshot.py"
            "MODULE_F_FAILURE_SNAPSHOT_OUTPUT=$round_dir/failure-snapshot.json"
            "MODULE_F_FAILURE_TARGET=$TARGET_PODS"
            "MODULE_F_MIN_COMPLETE=$MIN_COMPLETE_PODS"
            "MODULE_F_FAILURE_EXPECTED_PROFILE=$expected_profile"
            "MODULE_F_FAILURE_SELECTOR=group=saturation"
            "MODULE_F_FAILURE_LEDGER=$round_dir/failure-ledger.jsonl"
        )
    fi
    printf '%q ' env "${module_env[@]}" "${command[@]}" > "$round_dir/command.sh"
    printf '\n' >> "$round_dir/command.sh"
    set +e
    env "${module_env[@]}" "${command[@]}" 2>&1 | tee "$round_dir/runner.log"
    runner_exit=${PIPESTATUS[0]}
    set -e
    if [[ "$profile" == "Dreal-F1" ]]; then
        set +e
        stop_failure_reaper
        failure_reaper_exit=$?
        set -e
        printf '%s\n' "$failure_reaper_exit" > "$round_dir/failure-reaper-exit.txt"
    fi

    if [[ "$method" != "E2-G" ]]; then
        # Record immutable runtime evidence that methods inside the same
        # Para-Sched comparison used the same deployments/images.
        kubectl -n "$PARA_NAMESPACE" get deployments -o json \
            > "$round_dir/artifacts/para-deployments.json" 2>/dev/null || true
        kubectl -n "$PARA_NAMESPACE" get pods -o json \
            > "$round_dir/artifacts/para-pods.json" 2>/dev/null || true
    fi

    end_epoch="$(date +%s)"
    stop_lifecycle_watcher
    stop_cpu_monitor
    : > "$round_dir/kwok-alive-end.txt"
    for shard in $(seq 0 9); do
        pid="${ACTIVE_KWOK_PIDS[$shard]}"
        if kill -0 "$pid" 2>/dev/null; then state=alive; else state=dead; fi
        printf '%s\t%s\t%s\n' "$shard" "$pid" "$state" >> "$round_dir/kwok-alive-end.txt"
    done

    # Ownership must still hold now, not only when the shards were started.
    # The pre-round check cannot see a controller that appears while the runner
    # is creating nodes, which is precisely when the neutralised systemd
    # restart used to fire.  A violation here means this round measured a
    # racing pair of controllers, so stop instead of folding it into the matrix
    # and discovering it only at the next round's entry check.
    if assert_single_kwok_owner "profile=$profile round, at round end" \
            > "$round_dir/kwok-owner-end.txt" 2>&1; then
        printf 'OK\texactly one controller per selector, all started by this matrix\n' \
            > "$round_dir/kwok-owner-end.txt"
    else
        cat "$round_dir/kwok-owner-end.txt" >&2
        die "KWOK controller ownership was wrong at the end of this round; the round's data is polluted, see $round_dir/kwok-owner-end.txt"
    fi

    collect_original_control_plane "$name" "$round_dir" "$method" \
        > "$round_dir/collect-control-plane.log" 2>&1 || true
    read -r measurement_start measurement_end < <(python3 - "$round_dir/control-plane-source/timing.json" "$start_epoch" "$end_epoch" <<'PY'
import json
import math
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
fallback_start, fallback_end = map(int, sys.argv[2:])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    window = value["saturation"]
    start = math.floor(float(window["start"]))
    end = math.ceil(float(window["end"]))
    if end <= start:
        end = start + 1
except Exception:
    start, end = fallback_start, fallback_end
print(start, end)
PY
)
    if ! check_api_errors "$measurement_start" "$measurement_end" "$round_dir/api-server-errors.json"; then
        api_pass=false
    fi

    if ! validate_round "$method" "$profile" "$trial" "$order" "$round_dir" "$runner_exit" "$api_pass"; then
        python3 - "$round_dir/round-metadata.json" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); d["status"]="INVALID"; p.write_text(json.dumps(d,indent=2)+"\n")
PY
        log "round $order verdict INVALID (attempt $attempt): $round_dir"
        return 1
    fi
    python3 - "$round_dir/round-metadata.json" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); d["status"]="VALID"; p.write_text(json.dumps(d,indent=2)+"\n")
PY
    if [[ "$method" != "E2-G" ]]; then
        BINDER_RESET_ACTIVE=false
        BINDER_RESTORE_REPLICAS=""
    fi
    stop_kwok_shards
    sleep 10
}

summarize_matrix() {
    python3 - "$RUN_ROOT" "$PLAN_SEED" "$EXPERIMENT" "$TRIALS" "$TOTAL_ROUNDS" <<'PY'
import csv
import json
import pathlib
import random
import statistics
import sys

root = pathlib.Path(sys.argv[1])
seed = int(sys.argv[2])
experiment = sys.argv[3]
trials = int(sys.argv[4])
total_rounds = int(sys.argv[5])
rng = random.Random(seed)
rounds = []
for path in sorted(root.glob("round-*/round-summary.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("valid"):
        rounds.append(data)

rows = []
for item in rounds:
    lifecycle = item.get("lifecycle") or {}
    control = item.get("control_plane") or {}
    failure = item.get("failure_injection") or {}
    initial = item.get("initial_cohort_control") or {}
    t_data = lifecycle.get("t_data_ms") or {}
    initial_q_bind = initial.get("q_bind_pods_per_second")
    q_bind = initial_q_bind if experiment == "F2-E" and isinstance(initial_q_bind, (int, float)) else control["throughput_pods_per_s"]
    rows.append({
        "order": item["order"], "trial": item["trial"], "method": item["method"],
        "profile": item["dataplane_profile"],
        "q_bind_pods_per_second": q_bind,
        "q_bind_source": "initial cohort PodScheduled timestamps" if q_bind == initial_q_bind and initial_q_bind is not None else "original CL2",
        "conflict_rate": control["bind_conflict_rate"],
        "acf_count": control["acf_count"],
        "acf_rate": control["acf_rate"],
        "acf_source": control["acf_source"],
        "t_data_p50_ms": t_data.get("p50"),
        "t_data_p90_ms": t_data.get("p90"),
        "t_data_p99_ms": t_data.get("p99"),
        "failed_pods": failure.get("failed"),
        "observed_failure_rate": failure.get("observed_failure_rate"),
        "a_rebuild": failure.get("a_rebuild"),
        "ready_pods": failure.get("ready"),
    })

if rows:
    with (root / "matrix-results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)

groups = {}
for row in rows:
    groups.setdefault((row["method"], row["profile"]), []).append(row)

def bootstrap_mean_ci(values, samples=10000):
    if not values:
        return None
    estimates = []
    for _ in range(samples):
        draw = [rng.choice(values) for _ in values]
        estimates.append(statistics.mean(draw))
    estimates.sort()
    return [estimates[int(.025 * (samples - 1))], estimates[int(.975 * (samples - 1))]]

group_summary = {}
for key, values in groups.items():
    label = "-".join(key)
    q_values = [v["q_bind_pods_per_second"] for v in values]
    c_values = [v["conflict_rate"] for v in values if v["conflict_rate"] is not None]
    acf_values = [v["acf_rate"] for v in values if v["acf_rate"] is not None]
    failure_rate_values = [v["observed_failure_rate"] for v in values if v["observed_failure_rate"] is not None]
    rebuild_values = [v["a_rebuild"] for v in values if v["a_rebuild"] is not None]
    group_summary[label] = {
        "n": len(values),
        "q_bind_mean": statistics.mean(q_values),
        "q_bind_bootstrap_95ci": bootstrap_mean_ci(q_values),
        "conflict_rate_mean": statistics.mean(c_values),
        "conflict_rate_bootstrap_95ci": bootstrap_mean_ci(c_values),
        "acf_rate_mean": statistics.mean(acf_values),
        "acf_rate_bootstrap_95ci": bootstrap_mean_ci(acf_values),
        "observed_failure_rate_mean": statistics.mean(failure_rate_values) if failure_rate_values else None,
        "a_rebuild_mean": statistics.mean(rebuild_values) if rebuild_values else None,
        "raw_trials": values,
    }

if experiment in ("F1-E", "F2-E"):
    comparison_specs = [("E2", "E3")]
elif experiment in ("F1-P", "F2-P"):
    comparison_specs = [("P1", "P4")]
else:
    raise SystemExit(f"unsupported experiment: {experiment}")

comparison_profiles = ("Dreal-F1",) if experiment in ("F2-E", "F2-P") else ("Z0", "Dreal")

comparisons = {}
for baseline_method, proposed_method in comparison_specs:
    comparison_name = f"{proposed_method}_vs_{baseline_method}"
    comparisons[comparison_name] = {}
    for profile in comparison_profiles:
        baseline_rows = groups.get((baseline_method, profile), [])
        proposed_rows = groups.get((proposed_method, profile), [])
        baseline_by_trial = {v["trial"]: v for v in baseline_rows}
        proposed_by_trial = {v["trial"]: v for v in proposed_rows}
        throughput_gains = []
        conflict_reductions = []
        acf_reductions = []
        paired_trials = []
        for trial in sorted(set(baseline_by_trial) & set(proposed_by_trial)):
            baseline = baseline_by_trial[trial]
            proposed = proposed_by_trial[trial]
            throughput_gain = proposed["q_bind_pods_per_second"] / baseline["q_bind_pods_per_second"] - 1
            conflict_reduction = 1 - proposed["conflict_rate"] / baseline["conflict_rate"] if baseline["conflict_rate"] else None
            acf_reduction = 1 - proposed["acf_rate"] / baseline["acf_rate"] if baseline["acf_rate"] else None
            throughput_gains.append(throughput_gain)
            if conflict_reduction is not None:
                conflict_reductions.append(conflict_reduction)
            if acf_reduction is not None:
                acf_reductions.append(acf_reduction)
            paired_trials.append({"trial": trial, "throughput_gain": throughput_gain, "conflict_reduction": conflict_reduction, "acf_reduction": acf_reduction})
        if not paired_trials:
            continue
        comparisons[comparison_name][profile] = {
            "n_pairs": len(paired_trials),
            "formal_complete": len(paired_trials) == trials,
            "throughput_gain_mean": statistics.mean(throughput_gains),
            "throughput_gain_bootstrap_95ci": bootstrap_mean_ci(throughput_gains),
            "conflict_reduction_mean": statistics.mean(conflict_reductions) if conflict_reductions else None,
            "conflict_reduction_bootstrap_95ci": bootstrap_mean_ci(conflict_reductions),
            "acf_reduction_mean": statistics.mean(acf_reductions) if acf_reductions else None,
            "acf_reduction_bootstrap_95ci": bootstrap_mean_ci(acf_reductions),
            "paired_trials": paired_trials,
        }
    comparison = comparisons[comparison_name]
    if "Z0" in comparison and "Dreal" in comparison:
        z = comparison["Z0"]
        d = comparison["Dreal"]
        comparison["gain_retention"] = {
            "throughput": d["throughput_gain_mean"] / z["throughput_gain_mean"] if z["throughput_gain_mean"] and z["throughput_gain_mean"] > 0 else None,
            "conflict_reduction": d["conflict_reduction_mean"] / z["conflict_reduction_mean"] if z["conflict_reduction_mean"] and z["conflict_reduction_mean"] > 0 else None,
            "preregistered_threshold": 0.8,
        }

summary = {"experiment": experiment, "valid_rounds": len(rows), "expected_rounds": total_rounds, "groups": group_summary, "comparisons": comparisons, "complete": len(rows) == total_rounds}
(root / "matrix-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2, ensure_ascii=False))
PY
}

self_test() {
    local test_root template
    test_root="$(mktemp -d /tmp/module-f-self-test-XXXXXX)"
    RUNTIME_DIR="$test_root/runtime"
    mkdir -p "$RUNTIME_DIR"

    render_stage_file "$test_root/pod-ready-dreal.yaml"
    render_failure_stage_file "$test_root/pod-ready-dreal-f1.yaml"
    render_f2_cl2_config "$RUNTIME_DIR/cl2-saturation-f2-e.yaml"
    prepare_runtime_runners
    write_watcher_helper "$RUNTIME_DIR/watch-pod-lifecycle.py"
    write_failure_snapshot_helper "$RUNTIME_DIR/capture-failure-snapshot.py"
    if [[ "$EXPERIMENT" == "F1-E" ]]; then
        render_workload_template E2 Z0 "$test_root/e2-z0.yaml"
        render_workload_template E2 Dreal "$test_root/e2-dreal.yaml"
        render_workload_template E3 Dreal "$test_root/e3-dreal.yaml"
    elif [[ "$EXPERIMENT" == "F1-P" ]]; then
        render_workload_template P1 Z0 "$test_root/p1-z0.yaml"
        render_workload_template P1 Dreal "$test_root/p1-dreal.yaml"
        render_workload_template P4 Z0 "$test_root/p4-z0.yaml"
        render_workload_template P4 Dreal "$test_root/p4-dreal.yaml"
    elif [[ "$EXPERIMENT" == "F2-E" ]]; then
        render_workload_template E2 Dreal-F1 "$test_root/e2-dreal-f1.yaml"
        render_workload_template E3 Dreal-F1 "$test_root/e3-dreal-f1.yaml"
    else
        render_workload_template P1 Dreal-F1 "$test_root/p1-dreal-f1.yaml"
        render_workload_template P4 Dreal-F1 "$test_root/p4-dreal-f1.yaml"
    fi

    bash -n "$RUNTIME_DIR/run-e2g-runtime.sh"
    bash -n "$RUNTIME_DIR/run-para-runtime.sh"
    python3 -m py_compile "$RUNTIME_DIR/watch-pod-lifecycle.py"
    python3 -m py_compile "$RUNTIME_DIR/capture-failure-snapshot.py"
    python3 - "$test_root" "$EXPERIMENT" "$TRIALS" "$TOTAL_ROUNDS" "$PROFILE_JSON" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
experiment = sys.argv[2]
trials = int(sys.argv[3])
total_rounds = int(sys.argv[4])
# Derive the expected Stage counts from the profile rather than hardcoding
# them.  The previous fixed numbers encoded the five-bucket synthetic profile
# and rejected every anchor-derived one (10.2, 10.3); deriving them means the
# self-test cannot drift from the profile again.
bin_count = len(json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8"))["bins"])
stage = (root / "pod-ready-dreal.yaml").read_text(encoding="utf-8")
failure_stage = (root / "pod-ready-dreal-f1.yaml").read_text(encoding="utf-8")
if stage.count("kind: Stage") != bin_count + 1:
    raise SystemExit(
        f"Stage document count is not {bin_count + 1} "
        f"({bin_count} Ready stages plus pod-delete)"
    )
if stage.count("weight:") != bin_count:
    raise SystemExit(f"Ready Stage weight count is not {bin_count}")
if stage.count("name: module-f-pod-delete") != 1:
    raise SystemExit("module-f-pod-delete is missing or duplicated")
delete_doc = stage.split("name: module-f-pod-delete", 1)[1]
if "key: '.metadata.deletionTimestamp'" not in delete_doc or "operator: Exists" not in delete_doc:
    raise SystemExit("pod-delete selector does not require deletionTimestamp")
if "delete: true" not in delete_doc or "empty: true" not in delete_doc:
    raise SystemExit("pod-delete action is incomplete")
if failure_stage.count("kind: Stage") != 2 * bin_count + 1:
    raise SystemExit(
        f"Dreal-F1 Stage document count is not {2 * bin_count + 1} "
        f"({bin_count} success/failure pairs plus pod-delete)"
    )
if failure_stage.count("weight:") != 2 * bin_count:
    raise SystemExit(f"Dreal-F1 weighted Stage count is not {2 * bin_count}")
import re
success_weights = [int(value) for value in re.findall(r"name: module-f-dreal-f1-\S+-success[\s\S]*?weight: (\d+)", failure_stage)]
failure_weights = [int(value) for value in re.findall(r"name: module-f-dreal-f1-\S+-failure[\s\S]*?weight: (\d+)", failure_stage)]
# The totals stay fixed at the 1% failure rate regardless of bucket count.
if len(success_weights) != bin_count or sum(success_weights) != 9900:
    raise SystemExit(f"Dreal-F1 success weights are invalid: {success_weights}")
if len(failure_weights) != bin_count or sum(failure_weights) != 100:
    raise SystemExit(f"Dreal-F1 failure weights are invalid: {failure_weights}")
for marker in ("calibrated-f1-v1", "phase: Failed", "exitCode: 1", "reason: DataPlaneStartupFailed", "type: Warning"):
    if marker not in failure_stage:
        raise SystemExit(f"Dreal-F1 failure Stage is missing {marker}")
if experiment == "F1-E":
    checks = (("e2-z0.yaml", "zero-v1"), ("e2-dreal.yaml", "calibrated-v1"), ("e3-dreal.yaml", "calibrated-v1"))
elif experiment == "F1-P":
    checks = (("p1-z0.yaml", "zero-v1"), ("p1-dreal.yaml", "calibrated-v1"), ("p4-z0.yaml", "zero-v1"), ("p4-dreal.yaml", "calibrated-v1"))
elif experiment == "F2-E":
    checks = (("e2-dreal-f1.yaml", "calibrated-f1-v1"), ("e3-dreal-f1.yaml", "calibrated-f1-v1"))
elif experiment == "F2-P":
    checks = (("p1-dreal-f1.yaml", "calibrated-f1-v1"), ("p4-dreal-f1.yaml", "calibrated-f1-v1"))
else:
    raise SystemExit(f"unsupported experiment: {experiment}")
for filename, expected_profile in checks:
    if f"parasched.io/dataplane-profile: {expected_profile}" not in (root / filename).read_text(encoding="utf-8"):
        raise SystemExit(f"{filename} workload is missing the profile label {expected_profile}")
expected_rounds = {"F1-E": 2 * 2 * trials, "F1-P": 2 * 2 * trials, "F2-E": 2 * trials, "F2-P": 2 * trials}[experiment]
if total_rounds != expected_rounds:
    raise SystemExit(f"matrix total_rounds={total_rounds}; expected {expected_rounds}")
for name in ("run-e2g-runtime.sh", "run-para-runtime.sh"):
    runner = (root / "runtime" / name).read_text(encoding="utf-8")
    if runner.count('exit "$CL2_EXIT"') != 1 or runner.count('exit "${MODULE_F_FAILURE_SNAPSHOT_RC:-0}"') != 1:
        raise SystemExit(f"{name} does not preserve CL2/failure-snapshot exit status")
    if runner.count("capturing Dreal-F1 failure evidence before cleanup") != 1:
        raise SystemExit(f"{name} does not capture Dreal-F1 evidence before cleanup")
    if runner.count("MODULE_F_CL2_CONFIG") != 1:
        raise SystemExit(f"{name} does not accept the module-F CL2 config copy")
    node_delete_lines = [line for line in runner.splitlines() if "kubectl delete nodes -l type=kwok" in line]
    if len(node_delete_lines) != 2 or any("--wait=false" not in line for line in node_delete_lines):
        raise SystemExit(f"{name} does not use module-F nonblocking node cleanup")
    # The matrix owns KWOK (10.7.4).  A runtime copy that can restart the
    # systemd units puts a second controller on the same nodes, and the
    # systemd shards' built-in Stages then bypass the delay injection.
    if any(line.strip().startswith("sudo systemctl restart ") for line in runner.splitlines()):
        raise SystemExit(f"{name} still contains sudo systemctl restart, which would contend with the matrix for KWOK ownership")
    expected_suppressed = 2 if name == "run-para-runtime.sh" else 0
    if runner.count("systemd KWOK restart suppressed") != expected_suppressed:
        raise SystemExit(f"{name} neutralized systemd KWOK restart count is not {expected_suppressed}")
watcher = (root / "runtime" / "watch-pod-lifecycle.py").read_text(encoding="utf-8")
if "resync_count" in watcher or 'list_current("resync")' in watcher:
    raise SystemExit("watcher still contains timing-invalid resync behavior")
if "event_queue.put_nowait" not in watcher:
    raise SystemExit("watcher reader is not decoupled through a bounded queue")
snapshot = (root / "runtime" / "capture-failure-snapshot.py").read_text(encoding="utf-8")
for marker in ("wilson_interval", "DataPlaneStartupFailed", "initial_cohort", "a_rebuild", "--reap-failed", "failure ledger", "--min-complete", "failure_events_from_ledger", "events_ledger_path"):
    if marker not in snapshot:
        raise SystemExit(f"failure snapshot helper is missing {marker}")
cl2 = (root / "runtime" / "cl2-saturation-f2-e.yaml").read_text(encoding="utf-8")
if "- name: Deleting saturation pods" in cl2 or "- name: Waiting for saturation pods to be deleted" in cl2:
    raise SystemExit("F2 CL2 runtime copy still deletes workload before the failure snapshot")
if cl2.count("deleteAutomanagedNamespaces: false") != 1:
    raise SystemExit("F2 CL2 runtime copy does not retain automanaged namespaces for the failure snapshot")
for name in ("run-e2g-runtime.sh", "run-para-runtime.sh"):
    runner = (root / "runtime" / name).read_text(encoding="utf-8")
    if runner.count("failure-snapshot-namespaces.txt") != 1:
        raise SystemExit(f"{name} does not record F2 namespaces before the failure snapshot")
    if runner.count('kubectl delete namespace "${MODULE_F_FAILURE_NAMESPACES[@]}"') != 1:
        raise SystemExit(f"{name} does not clean retained F2 namespaces after the failure snapshot")
if "- name: Collecting measurements" not in cl2:
    raise SystemExit("F2-E CL2 runtime copy lost the final metrics collection step")
PY
    rm -rf -- "$test_root"
    RUNTIME_DIR=""
    echo "$EXPERIMENT SELF-TEST: PASS (Kubernetes/Prometheus not contacted, no experiment run)"
}

injection_smoke_test() {
    [[ "$EXPERIMENT" == "F2-E" ]] || die "--injection-smoke-test only applies to F2-E"
    cluster_preflight
    [[ -n "$RUN_ID" ]] || RUN_ID="F2-E-injection-smoke-$(date +%Y%m%d-%H%M%S)"
    [[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "--run-id may contain only letters, digits, dots, underscores and hyphens"
    RUN_ROOT="$OUTPUT_ROOT/$RUN_ID"
    [[ ! -e "$RUN_ROOT" ]] || die "result directory already exists: $RUN_ROOT (choose another --run-id to avoid overwriting)"
    RUNTIME_DIR="$RUN_ROOT/_runtime"
    mkdir -p "$RUNTIME_DIR" "$RUN_ROOT/artifacts" "$RUN_ROOT/kwok-logs"

    local smoke_token nodes_file workload_file stage_file snapshot_file deadline ready failed observed snapshot_rc
    smoke_token="mf2e-$(date +%Y%m%d%H%M%S)-$$"
    SMOKE_NAMESPACE="$smoke_token"
    SMOKE_NODE_SELECTOR="parasched.io/f2e-smoke=$smoke_token"
    nodes_file="$RUN_ROOT/artifacts/smoke-nodes.json"
    workload_file="$RUN_ROOT/artifacts/smoke-deployments.json"
    stage_file="$RUN_ROOT/artifacts/pod-ready-dreal-f1.yaml"
    snapshot_file="$RUN_ROOT/failure-snapshot.json"

    render_failure_stage_file "$stage_file"
    write_failure_snapshot_helper "$RUNTIME_DIR/capture-failure-snapshot.py"
    cp "${BASH_SOURCE[0]}" "$RUN_ROOT/artifacts/run-module-f.sh"
    python3 - "$nodes_file" "$workload_file" "$SMOKE_NAMESPACE" "$smoke_token" "$SMOKE_PODS" <<'PY'
import json
import pathlib
import sys

nodes_path, workload_path = map(pathlib.Path, sys.argv[1:3])
namespace, token, pod_count = sys.argv[3], sys.argv[4], int(sys.argv[5])
nodes = []
for shard in range(10):
    name = f"{token}-node-{shard}"
    nodes.append({
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": name,
            "annotations": {"kwok.x-k8s.io/node": f"fake{shard}", "node.alpha.kubernetes.io/ttl": "0"},
            "labels": {
                "type": "kwok",
                "kubernetes.io/hostname": name,
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                "parasched.io/f2e-smoke": token,
                "parasched.io/shard-id": str(shard),
            },
        },
        "spec": {"taints": [{"key": "kwok.x-k8s.io/node", "value": "fake", "effect": "NoSchedule"}]},
    })
deployments = []
for shard in range(10):
    replicas = pod_count // 10 + (1 if shard < pod_count % 10 else 0)
    name = f"smoke-shard-{shard}"
    labels = {
        "app": name,
        "parasched.io/f2e-smoke": token,
        "parasched.io/dataplane-profile": "calibrated-f1-v1",
    }
    deployments.append({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeName": f"{token}-node-{shard}",
                    "restartPolicy": "Always",
                    "tolerations": [{"key": "kwok.x-k8s.io/node", "operator": "Exists", "effect": "NoSchedule"}],
                    "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.9"}],
                },
            },
        }
    })
nodes_path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": nodes}, indent=2) + "\n", encoding="utf-8")
workload_path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": deployments}, indent=2) + "\n", encoding="utf-8")
PY

    start_sudo_keepalive
    take_over_systemd_kwok
    start_kwok_shards Dreal-F1 "$RUN_ROOT" "$stage_file"
    kubectl create namespace "$SMOKE_NAMESPACE" >/dev/null
    kubectl create -f "$nodes_file" >/dev/null
    kubectl create -f "$workload_file" >/dev/null
    log "created 10 pre-bound Deployments (target $SMOKE_PODS Ready); waiting for failure replacement to finish"

    deadline=$(( $(date +%s) + 240 ))
    while true; do
        read -r ready failed observed < <(kubectl -n "$SMOKE_NAMESPACE" get pods -l "parasched.io/f2e-smoke=$smoke_token" -o json | python3 -c 'import json,sys; pods=json.load(sys.stdin).get("items", []); ready=sum(any(c.get("type")=="Ready" and c.get("status")=="True" for c in (p.get("status") or {}).get("conditions", [])) for p in pods); failed=sum((p.get("status") or {}).get("phase")=="Failed" for p in pods); print(ready, failed, len(pods))')
        log "failure-injection smoke: Ready=$ready/$SMOKE_PODS Failed=$failed observed=$observed"
        (( ready >= SMOKE_PODS )) && break
        (( $(date +%s) < deadline )) || die "failure-injection smoke timed out waiting for replacement pods to become Ready: Ready=$ready/$SMOKE_PODS Failed=$failed"
        sleep 3
    done

    set +e
    "$RUNTIME_DIR/capture-failure-snapshot.py" \
        --output "$snapshot_file" \
        --target "$SMOKE_PODS" \
        --expected-profile calibrated-f1-v1 \
        --selector "parasched.io/f2e-smoke=$smoke_token" \
        --cohort-mode replicasets \
        --require-final-ready | tee "$RUN_ROOT/failure-snapshot.txt"
    snapshot_rc=${PIPESTATUS[0]}
    set -e

    cleanup_smoke_objects
    stop_kwok_shards
    restore_systemd_kwok
    if (( snapshot_rc != 0 )); then
        die "F2-E failure-injection smoke test did not pass; evidence: $snapshot_file"
    fi
    log "F2-E failure-injection smoke test: PASS; evidence: $snapshot_file"
}

main() {
    static_preflight

    if [[ "$MODE" == "self-test" ]]; then
        self_test
        return 0
    fi
    if [[ "$MODE" == "injection-smoke-test" ]]; then
        injection_smoke_test
        return 0
    fi

    local temp_plan=""
    if [[ "$MODE" == "dry-run" ]]; then
        temp_plan="$(mktemp /tmp/module-f-plan-XXXXXX.csv)"
        generate_plan "$temp_plan"
        echo
        echo "$EXPERIMENT fixed environment:"
        echo "  10000 KWOK nodes / 10000 Pods / 10 schedulers / HC-V V=0.6"
        if [[ "$EXPERIMENT" == "F1-E" || "$EXPERIMENT" == "F2-E" ]]; then
            echo "  E2 = vanilla-E：event mode, M=1/diff, sync-period=0.1s, K=0, penalty=0"
            echo "  E3 = ParKour-E：event mode, M=1/diff, sync-period=0.1s, K=2, QualityFirst, penalty=0.5"
        else
            echo "  P1 = vanilla-P：periodic globSync, G=1.0s, M=1, K=0, penalty=0"
            echo "  P4 = ParKour-P：periodic globSync, G=1.0s, M=1, K=2, QualityFirst, penalty=0.5"
        fi
        if [[ "$EXPERIMENT" == "F2-E" || "$EXPERIMENT" == "F2-P" ]]; then
            echo "  Dreal-F1 = the current four-bucket latency profile + 1% post-bind terminal startup failure"
        else
            echo "  Z0 = KWOK fast; Dreal = the current four-bucket latency profile"
        fi
        echo
        print_plan "$temp_plan"
        rm -f "$temp_plan"
        echo
        echo "DRY RUN: the Kubernetes cluster was neither contacted nor modified, and no round was run."
        return 0
    fi

    cluster_preflight
    [[ -n "$RUN_ID" ]] || RUN_ID="$EXPERIMENT-$(date +%Y%m%d-%H%M%S)"
    [[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "--run-id may contain only letters, digits, dots, underscores and hyphens"
    RUN_ROOT="$OUTPUT_ROOT/$RUN_ID"
    [[ ! -e "$RUN_ROOT" ]] || die "result directory already exists: $RUN_ROOT (choose another --run-id to avoid overwriting)"
    RUNTIME_DIR="$RUN_ROOT/_runtime"
    PLAN_FILE="$RUN_ROOT/execution-plan.csv"
    mkdir -p "$RUNTIME_DIR" "$RUN_ROOT/artifacts"

    generate_plan "$PLAN_FILE"
    cp "${BASH_SOURCE[0]}" "$RUN_ROOT/artifacts/run-module-f.sh"
    render_stage_file "$RUN_ROOT/artifacts/pod-ready-dreal.yaml"
    render_failure_stage_file "$RUN_ROOT/artifacts/pod-ready-dreal-f1.yaml"
    cp "$PROFILE_JSON" "$RUN_ROOT/artifacts/dataplane-profile-v1.json"
    cp "$GATE_REPORT" "$RUN_ROOT/artifacts/injection-gate-report.json"
    prepare_runtime_runners
    render_f2_cl2_config "$RUNTIME_DIR/cl2-saturation-f2-e.yaml"
    write_watcher_helper "$RUNTIME_DIR/watch-pod-lifecycle.py"
    write_failure_snapshot_helper "$RUNTIME_DIR/capture-failure-snapshot.py"
    write_run_metadata
    print_plan "$PLAN_FILE"
    start_sudo_keepalive
    take_over_systemd_kwok

    local order trial method profile round_dir kind
    local -i attempt rc order_retries
    while IFS=, read -r order trial method profile; do
        [[ "$order" == "order" ]] && continue
        (( order >= FROM_ORDER )) || continue
        (( order <= TO_ORDER )) || continue
        if [[ -n "$ONLY_ORDER" && "$order" -ne "$ONLY_ORDER" ]]; then continue; fi

        order_retries=0
        while :; do
            attempt=$((order_retries + 1))
            # `|| rc=$?` rather than wrapping the call in `set +e` / `set -e`:
            # shell options are global, not function-scoped, and run_one_round
            # re-enables errexit internally (it brackets the runner call with
            # its own set +e / set -e).  That clobbered the caller's set +e, so
            # `return 1` from an INVALID round tripped errexit at this call site
            # and killed the matrix before rc was ever read -- the retry policy
            # of 10.6.5 could never run.  An OR-list is exempt from errexit
            # regardless of what the callee did to the option.
            rc=0
            run_one_round "$order" "$trial" "$method" "$profile" "$attempt" || rc=$?
            (( rc == 0 )) && break

            round_dir="$(round_dir_for "$order" "$trial" "$method" "$profile" "$attempt")"
            kind="$(classify_round_failure "$round_dir")"
            if [[ "$kind" != "retryable" ]]; then
                die "round $order failed hard; aborting immediately without retry: $round_dir"
            fi
            if (( order_retries >= RETRY_PER_ORDER )); then
                die "round $order reached the per-round retry limit of $RETRY_PER_ORDER; the injection rate stays low and needs manual investigation"
            fi
            if (( RETRY_BUDGET_USED >= RETRY_BUDGET_TOTAL )); then
                die "the matrix retry budget of $RETRY_BUDGET_TOTAL is exhausted; too many invalid rounds, manual investigation needed"
            fi
            order_retries=$((order_retries + 1))
            RETRY_BUDGET_USED=$((RETRY_BUDGET_USED + 1))
            log "round $order failed the injection-rate gate and is retryable; retry $order_retries/$RETRY_PER_ORDER (matrix total $RETRY_BUDGET_USED/$RETRY_BUDGET_TOTAL)"
        done
    done < "$PLAN_FILE"

    summarize_matrix | tee "$RUN_ROOT/matrix-summary.txt"
    log "$EXPERIMENT finished: $RUN_ROOT"
}

main
