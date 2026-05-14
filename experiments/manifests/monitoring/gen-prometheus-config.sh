#!/bin/bash
# Generate para-sched scrape config snippet to append to existing prometheus.yml.
#
# Usage:
#   ./gen-prometheus-config.sh [NUM_SCHEDULERS]
#   ./gen-prometheus-config.sh 10 >> prometheus.yml
#
# Default: 10 scheduler instances.
# Binder: NodePort 30080 (HTTP), Dispatcher: NodePort 30081 (HTTP),
# Schedulers: NodePort 30090+i (HTTPS, kube-scheduler secure port).

set -euo pipefail

NUM_SCHEDULERS=${1:-10}
MASTER_IP="${MASTER_IP:-<MASTER_IP>}"

cat <<EOF
  # ---- Para-Sched: Binder (NodePort 30080) ----
  - job_name: "parasched-binder"
    metrics_path: /metrics
    static_configs:
      - targets: ["${MASTER_IP}:30080"]
        labels:
          component: "binder"
    relabel_configs:
      - source_labels: [__address__]
        target_label: instance
        replacement: "parasched-binder"

  # ---- Para-Sched: Dispatcher (NodePort 30081) ----
  - job_name: "parasched-dispatcher"
    metrics_path: /metrics
    static_configs:
      - targets: ["${MASTER_IP}:30081"]
        labels:
          component: "dispatcher"
    relabel_configs:
      - source_labels: [__address__]
        target_label: instance
        replacement: "parasched-dispatcher"

  # ---- Para-Sched: Scheduler instances (NodePort 30090+i, HTTPS) ----
EOF

for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    PORT=$((30090 + i))
    cat <<EOF
  - job_name: "parasched-scheduler-${i}"
    scheme: https
    metrics_path: /metrics
    tls_config:
      insecure_skip_verify: true
    authorization:
      credentials_file: /etc/prometheus/tls/token
    static_configs:
      - targets: ["${MASTER_IP}:${PORT}"]
        labels:
          component: "scheduler"
    relabel_configs:
      - source_labels: [__address__]
        target_label: instance
        replacement: "sched-${i}"
EOF
done
