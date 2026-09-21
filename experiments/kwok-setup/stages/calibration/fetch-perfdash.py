#!/usr/bin/env python3
"""Fetch pod-startup latency percentiles from the upstream Kubernetes performance dashboard.

perf-dash.k8s.io publishes, for every run of the SIG-scalability CI jobs, the ClusterLoader2
``PodStartupLatency`` measurement.  The phase ``schedule_to_run`` is the interval between the
moment ``spec.nodeName`` is set (i.e. Bind succeeded) and the moment the pod is observed in
``Running``; it is exactly the post-bind kubelet/CRI delay that board F injects through KWOK
Stages.  The pods are stateless ``pause`` pods whose image is pre-pulled by a DaemonSet, so the
numbers exclude image pull.

Three metric names are exported per job (names follow the CL2 load config phases):

* ``LoadCreatePhaseStatelessPodStartup``  - initial cluster fill, ~30 pods per node started
  concurrently (kubelet-side queueing dominates; not the 1-pod-per-node regime of HC-V);
* ``LoadHighThroughputStatelessPodStartup`` - burst creation phase at high QPS (closest to the
  saturation burst used in this paper);
* ``LoadStatelessPodStartup``              - steady scale/update phase (~10 pods/s).

Usage::

    python fetch-perfdash.py                      # default jobs/metrics, last 20 builds
    python fetch-perfdash.py --last 40 --out perfdash-summary.csv
    python fetch-perfdash.py --jobs gce-5000Nodes aws-5000Nodes --raw-dir raw/

Outputs a CSV with, per (job, metric, phase), the median and min/max over the last ``N``
builds of the P50/P90/P99 values (milliseconds), plus an optional copy of the raw JSON.
Only the standard library is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request

BASE_URL = "https://perf-dash.k8s.io/buildsdata"
DEFAULT_JOBS = ["gce-5000Nodes", "aws-5000Nodes", "gce-100Nodes-master", "aws-100Nodes"]
DEFAULT_METRICS = [
    "LoadStatelessPodStartup",
    "LoadHighThroughputStatelessPodStartup",
    "LoadCreatePhaseStatelessPodStartup",
]
PHASES = ("schedule_to_run", "pod_startup", "create_to_schedule")
PERCENTILES = ("Perc50", "Perc90", "Perc99")


def fetch(job: str, metric: str, category: str = "E2E", timeout: int = 180) -> dict:
    """Download the buildsdata JSON for one (job, metric) pair.

    Args:
        job: perf-dash job name, e.g. ``gce-5000Nodes``.
        metric: perf-dash metric name inside the ``E2E`` category.
        category: metric category name (``E2E`` for pod startup).
        timeout: socket timeout in seconds.

    Returns:
        The decoded JSON document, a dict with ``builds`` keyed by build id.

    Raises:
        urllib.error.URLError: if the dashboard is unreachable.
        ValueError: if the response is not valid JSON.
    """
    query = urllib.parse.urlencode({"jobname": job, "metriccategoryname": category, "metricname": metric})
    with urllib.request.urlopen(f"{BASE_URL}?{query}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize(doc: dict, last: int) -> list[dict]:
    """Aggregate the last ``last`` builds of one buildsdata document.

    Args:
        doc: JSON document returned by :func:`fetch`.
        last: number of most recent builds (by numeric build id) to include.

    Returns:
        One dict per phase with median/min/max of each percentile (ms) and the build count.
    """
    builds = doc.get("builds") or {}
    keys = sorted(builds, key=lambda k: int(k) if str(k).isdigit() else 0)[-last:]
    rows = []
    for phase in PHASES:
        values = {p: [] for p in PERCENTILES}
        for key in keys:
            for item in builds[key]:
                if item.get("labels", {}).get("Metric") != phase:
                    continue
                for p in PERCENTILES:
                    v = item.get("data", {}).get(p)
                    if v is not None:
                        values[p].append(float(v))
        if not values["Perc50"]:
            continue
        row = {"prow_job": doc.get("job", ""), "phase": phase, "n_builds": len(values["Perc50"])}
        for p in PERCENTILES:
            tag = p.lower().replace("perc", "p")
            row[f"{tag}_med_ms"] = round(statistics.median(values[p]))
            row[f"{tag}_min_ms"] = round(min(values[p]))
            row[f"{tag}_max_ms"] = round(max(values[p]))
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: argument list; ``None`` uses ``sys.argv``.

    Returns:
        Process exit code (0 on success, 1 if every download failed).
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", nargs="+", default=DEFAULT_JOBS)
    ap.add_argument("--metrics", nargs="+", default=DEFAULT_METRICS)
    ap.add_argument("--last", type=int, default=20, help="number of most recent builds to aggregate")
    ap.add_argument("--out", default="perfdash-summary.csv", help="summary CSV path")
    ap.add_argument("--raw-dir", default=None, help="if set, keep the raw JSON documents here")
    ap.add_argument("--from-raw", default=None,
                    help="read <job>__<metric>.json files from this directory instead of the network "
                         "(rebuilds the summary from a frozen snapshot)")
    args = ap.parse_args(argv)

    rows: list[dict] = []
    for job in args.jobs:
        for metric in args.metrics:
            try:
                if args.from_raw:
                    with open(os.path.join(args.from_raw, f"{job}__{metric}.json"), encoding="utf-8") as fh:
                        doc = json.load(fh)
                else:
                    doc = fetch(job, metric)
            except Exception as exc:  # noqa: BLE001 - report and continue with the next pair
                print(f"[warn] {job}/{metric}: {exc}", file=sys.stderr)
                continue
            if args.raw_dir and not args.from_raw:
                os.makedirs(args.raw_dir, exist_ok=True)
                with open(os.path.join(args.raw_dir, f"{job}__{metric}.json"), "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
            for row in summarize(doc, args.last):
                rows.append({"job": job, "metric": metric, **row})
                if row["phase"] == "schedule_to_run":
                    print(f"{job:20s} {metric:40s} schedule_to_run p50={row['p50_med_ms']:6d} "
                          f"p90={row['p90_med_ms']:6d} p99={row['p99_med_ms']:6d} ms (n={row['n_builds']})")
    if not rows:
        return 1
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
