#!/bin/bash

set -e

# ============================================
# Resolve project root from script location
# ============================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Project root: $PROJECT_ROOT"

# ============================================
# Project paths
# ============================================
CL2_DIR="$PROJECT_ROOT/perf-tests/clusterloader2"
CONFIG_DIR="$PROJECT_ROOT/experiments/kwok-setup"
BINARY_DIR="$PROJECT_ROOT/bin"
BINARY_PATH="$BINARY_DIR/clusterloader"

mkdir -p "$BINARY_DIR"

# ============================================
# Defaults
# ============================================
NODE_NUM=10
VARIANCE="0"   # Capacity-variance level for HC-V experiments (see generate-hetero-config.py)

# ============================================
# Argument parsing
# ============================================
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--nodes)
            NODE_NUM="$2"
            shift 2
            ;;
        --variance)
            VARIANCE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [-n|--nodes <node_num>] [--variance <V>]"
            echo ""
            echo "Options:"
            echo "  -n, --nodes NUM    Number of nodes to create (default: 10)"
            echo "  --variance V       Capacity-variance level: 0 | 0.3 | 0.6 | 1.0 (default: 0)"
            echo "                       V=0 reproduces legacy HC-1 (32 CPU / 256 Gi per shard)"
            echo "                       V>0 yields heterogeneous shards per generate-hetero-config.py"
            echo "  -h, --help         Show this help message"
            echo ""
            echo "Example:"
            echo "  $0 --nodes 5000 --variance 0.6"
            exit 0
            ;;
        *)
            echo "Unknown parameter: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# ============================================
# Validate node count is a multiple of 10 (one per KWOK shard)
# ============================================
if (( NODE_NUM % 10 != 0 )); then
    echo "Error: Number of nodes ($NODE_NUM) must be a multiple of 10 (for 10 KWOK shards)"
    echo "Example: $0 --nodes 1000"
    exit 1
fi

NODES_PER_SHARD=$((NODE_NUM / 10))
echo "KWOK sharding: $NODE_NUM nodes / 10 shards = $NODES_PER_SHARD nodes per shard"

# ============================================
# Validate required directories and files
# ============================================
if [ ! -d "$CL2_DIR" ]; then
    echo "Error: ClusterLoader2 directory not found: $CL2_DIR"
    echo "Please ensure perf-tests submodule is initialized:"
    echo "  git submodule update --init --recursive"
    exit 1
fi

if [ ! -d "$CONFIG_DIR" ]; then
    echo "Error: Config directory not found: $CONFIG_DIR"
    exit 1
fi

if [ ! -f "$CONFIG_DIR/cl2-create-nodes.yaml" ]; then
    echo "Error: Config file not found: $CONFIG_DIR/cl2-create-nodes.yaml"
    exit 1
fi

# ============================================
# Build ClusterLoader2
# ============================================
echo "Checking ClusterLoader2 binary..."

NEED_COMPILE=false

if [ ! -f "$BINARY_PATH" ]; then
    echo "Binary not found, need to compile."
    NEED_COMPILE=true
elif [ "$CL2_DIR/cmd/clusterloader.go" -nt "$BINARY_PATH" ]; then
    echo "Source code is newer than binary, need to recompile."
    NEED_COMPILE=true
fi

if [ "$NEED_COMPILE" = true ]; then
    echo "Compiling ClusterLoader2..."
    cd "$CL2_DIR" || exit 1
    
    go build -o "$BINARY_PATH" ./cmd/clusterloader.go || {
        echo "Compilation failed!"
        exit 1
    }
    
    echo "Compilation completed: $BINARY_PATH"
else
    echo "Using existing binary: $BINARY_PATH"
fi

# ============================================
# Locate kubeconfig
# ============================================
if [ -n "$KUBECONFIG" ]; then
    KUBECONFIG_PATH="$KUBECONFIG"
elif [ -f "$HOME/.kube/config" ]; then
    KUBECONFIG_PATH="$HOME/.kube/config"
else
    echo "Error: kubeconfig not found!"
    echo "Please ensure:"
    echo "  1. KUBECONFIG environment variable is set, or"
    echo "  2. $HOME/.kube/config exists"
    exit 1
fi

echo "Using kubeconfig: $KUBECONFIG_PATH"

# ============================================
# Generate CL2 testoverrides (inject per-shard CPU/Memory based on variance)
# ============================================
OVERRIDES_FILE=""
CL2_EXTRA_ARGS=()
if [ "$VARIANCE" != "0" ] || [ "${FORCE_WRITE_OVERRIDES:-0}" = "1" ]; then
    GEN_SCRIPT="$SCRIPT_DIR/generate-hetero-config.py"
    if [ ! -f "$GEN_SCRIPT" ]; then
        echo "Error: $GEN_SCRIPT not found"
        exit 1
    fi
    OVERRIDES_FILE=$(mktemp /tmp/cl2-nodes-overrides-XXXXXX.yaml)
    python "$GEN_SCRIPT" --variance "$VARIANCE" --out "$OVERRIDES_FILE" || {
        echo "Error: failed to generate testoverrides for variance=$VARIANCE"
        rm -f "$OVERRIDES_FILE"
        exit 1
    }
    echo "Using capacity variance V=$VARIANCE (overrides: $OVERRIDES_FILE)"
    CL2_EXTRA_ARGS+=(--testoverrides="$OVERRIDES_FILE")
else
    echo "Using capacity variance V=0 (homogeneous shards, legacy HC-1 behavior)"
fi

# ============================================
# Run ClusterLoader2
# ============================================
echo "============================================"
echo "Creating $NODE_NUM KWOK nodes (V=$VARIANCE)..."
echo "============================================"

"$BINARY_PATH" \
  --testconfig="$CONFIG_DIR/cl2-create-nodes.yaml" \
  --provider=local \
  --provider-configs=ROOT_KUBECONFIG="$KUBECONFIG_PATH" \
  --kubeconfig="$KUBECONFIG_PATH" \
  --v=2 \
  --enable-exec-service=false \
  --enable-prometheus-server=false \
  --nodes=$NODE_NUM \
  "${CL2_EXTRA_ARGS[@]}" 2>&1

CL2_RC=$?
[ -n "$OVERRIDES_FILE" ] && rm -f "$OVERRIDES_FILE"

if [ "$CL2_RC" -ne 0 ]; then
    echo ""
    echo "============================================"
    echo "Node creation FAILED (CL2 exit=$CL2_RC)"
    echo "============================================"
    exit "$CL2_RC"
fi

echo ""
echo "============================================"
echo "Node creation completed!"
echo "============================================"