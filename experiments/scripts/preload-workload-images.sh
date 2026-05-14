#!/bin/bash

# Pre-pull workload images and distribute them to worker nodes C/D/E, so that
# D1 / Board-E experiments can use imagePullPolicy=Never (matching the
# para-scheduler/* components convention) — eliminates DockerHub rate limits
# and first-pod startup latency from scheduling-quality measurements.
#
# Images handled:
#   polinux/stress-ng:latest   (used by --stress-profile mild/heavy)
#   nginx:1.27-alpine          (D1 workload)
#   redis:7-alpine             (D1 workload)
#   mysql:8.0                  (D1 workload)
#
# Usage:
#   ./preload-workload-images.sh                # pull + distribute all
#   ./preload-workload-images.sh --pull-only    # only docker pull locally, skip scp
#   ./preload-workload-images.sh --images nginx:1.27-alpine   # subset
#   SSH_USER=ubuntu ./preload-workload-images.sh              # override ssh user
#
# Prerequisites:
#   - Run on a node with docker + outbound network (typically node A or B)
#   - SSH key-based login to each worker (no password prompts)
#   - sudo NOPASSWD for the SSH user on workers (for `ctr` commands)
#
# Design notes:
#   - Workers use containerd (kubeadm v1.33 default). Images are loaded via
#     `ctr -n k8s.io images import` — the k8s.io namespace is what CRI/kubelet
#     reads from; the `default` containerd namespace is invisible to kubelet.
#   - We use docker (not ctr) on the pull side because the node running this
#     script is assumed to have docker already (for build-images.sh).
#   - Tar files are scp'd to /tmp/ on each worker, imported, then removed.
#   - Idempotent: re-running on an already-loaded cluster only re-verifies.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ========== Config ==========
DEFAULT_IMAGES=(
    "polinux/stress-ng:latest"
    "nginx:1.27-alpine"
    "redis:7-alpine"
    "mysql:8.0"
)
DEFAULT_WORKERS=(
    "${MASTER_IP:-<MASTER_IP>}"
    "${MASTER_IP:-<MASTER_IP>}"
    "<WORKER_IP>"
)
SSH_USER="${SSH_USER:-clsd}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=10}"

IMAGES=()
WORKERS=()
PULL_ONLY=false

# ========== Parse args ==========
while [[ $# -gt 0 ]]; do
    case $1 in
        --images)
            shift
            while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do
                IMAGES+=("$1"); shift
            done
            ;;
        --workers)
            shift
            while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do
                WORKERS+=("$1"); shift
            done
            ;;
        --pull-only) PULL_ONLY=true; shift ;;
        -h|--help)
            head -35 "$0" | tail -33
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

[ ${#IMAGES[@]}  -eq 0 ] && IMAGES=("${DEFAULT_IMAGES[@]}")
[ ${#WORKERS[@]} -eq 0 ] && WORKERS=("${DEFAULT_WORKERS[@]}")

echo "============================================"
echo "Pre-load workload images onto K8s workers"
echo "============================================"
echo "Images:   ${IMAGES[*]}"
echo "Workers:  ${WORKERS[*]}"
echo "SSH user: $SSH_USER"
echo "Pull only: $PULL_ONLY"
echo ""

# ========== Sanity check ==========
if ! command -v docker &>/dev/null; then
    echo "Error: docker not found on this host."
    echo "Run this script on a node with docker + outbound network (e.g. node A or B)."
    exit 1
fi

# ========== 1. Pull images locally ==========
echo "Step 1: Pulling images via docker..."
for img in "${IMAGES[@]}"; do
    echo "  docker pull $img"
    docker pull "$img"
done
echo ""

if [ "$PULL_ONLY" = true ]; then
    echo "--pull-only specified; skipping distribution. Done."
    exit 0
fi

# ========== 2. Save to tar + distribute + import ==========
echo "Step 2: Distributing to workers via docker save + ctr import..."

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

sanitize() {
    # "polinux/stress-ng:latest" -> "polinux--stress-ng--latest"
    echo "$1" | tr '/:' '--'
}

FAILED=()
for img in "${IMAGES[@]}"; do
    safe=$(sanitize "$img")
    tarfile="$TMPDIR/${safe}.tar"
    echo "  [$img]"
    echo "    docker save -> $tarfile"
    docker save -o "$tarfile" "$img"
    size=$(du -h "$tarfile" | cut -f1)
    echo "    tar size: $size"

    for worker in "${WORKERS[@]}"; do
        echo "    → $worker:"
        # shellcheck disable=SC2086
        if ! scp $SSH_OPTS "$tarfile" "${SSH_USER}@${worker}:/tmp/${safe}.tar"; then
            echo "      scp FAILED"
            FAILED+=("$img@$worker[scp]")
            continue
        fi
        # Import into containerd's k8s.io namespace (what kubelet/CRI sees).
        # `ctr` may need sudo depending on cluster setup; try sudo first, fall
        # back to no-sudo for permissive environments.
        if ! ssh $SSH_OPTS "${SSH_USER}@${worker}" \
             "sudo -n ctr -n k8s.io images import /tmp/${safe}.tar && sudo -n rm -f /tmp/${safe}.tar"; then
            echo "      ctr import FAILED (check sudo NOPASSWD for ctr on $worker)"
            FAILED+=("$img@$worker[ctr]")
            continue
        fi
        echo "      imported OK"
    done
done

# ========== 3. Verify on each worker ==========
echo ""
echo "Step 3: Verifying images are visible to kubelet..."
for worker in "${WORKERS[@]}"; do
    echo "  [$worker]"
    for img in "${IMAGES[@]}"; do
        # `crictl images` is the CRI-spec-aware tool; fall back to ctr if absent.
        if ssh $SSH_OPTS "${SSH_USER}@${worker}" \
               "sudo -n crictl images --no-trunc 2>/dev/null | grep -q '${img%:*}' \
             || sudo -n ctr -n k8s.io images ls | grep -q '${img}'"; then
            echo "    ✓ $img"
        else
            echo "    ✗ $img NOT FOUND"
            FAILED+=("$img@$worker[verify]")
        fi
    done
done

# ========== Summary ==========
echo ""
echo "============================================"
if [ ${#FAILED[@]} -eq 0 ]; then
    echo "All images pre-loaded successfully."
    echo "Workload YAMLs can now use 'imagePullPolicy: Never'."
else
    echo "Pre-load completed with ${#FAILED[@]} failure(s):"
    for f in "${FAILED[@]}"; do echo "  - $f"; done
    echo ""
    echo "Common fixes:"
    echo "  - ssh ${SSH_USER}@<worker> 'sudo -n true'  → if prompts for password,"
    echo "    add: ${SSH_USER} ALL=(ALL) NOPASSWD: /usr/bin/ctr, /usr/bin/crictl, /bin/rm"
    echo "    to /etc/sudoers.d/ctr-preload on each worker."
    exit 1
fi
echo "============================================"
