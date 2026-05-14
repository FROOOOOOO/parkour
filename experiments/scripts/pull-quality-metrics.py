#!/usr/bin/env python3
"""
Re-query Prometheus for scheduling-quality metrics that the original
collect-metrics.sh did not snapshot.

Two metrics are pulled per trial via histogram snapshot diff (start vs end):

  scheduler_parasched_selected_node_score  (buckets 0/10/.../100)
      -> mean (sum/count) is the primary signal; P50 saturates at 100
         under the coarse 10-wide buckets, so we rely on mean.
  parasched_candidate_rank_accepted        (buckets 0/1/.../10)
      -> mean and rank>0 fraction; rank>0 fraction is the share of pods
         that needed multi-candidate fallback.

The script reads each trial's meta.json (written by collect-metrics.sh) for
the Prometheus URL and time window, queries the histogram _sum / _count /
_bucket counters at start and end as instant queries, then writes
`quality.json` next to the existing snap_*.json files.

Caveats:
  - Only works while Prometheus still has data in its retention window.
    If retention has expired, the script will report 'no data' and skip.
  - Idempotent: if quality.json already exists, the trial is skipped
    unless --force is given.

Usage:
    # Single experiment group, all trials
    python pull-quality-metrics.py experiments/results/ablation/AbE3-MP_*

    # Multiple groups via glob
    python pull-quality-metrics.py 'experiments/results/ablation/Ab*'

    # Force overwrite, custom Prometheus URL
    python pull-quality-metrics.py --force --prometheus-url http://x:9091 <dir>
"""

import argparse
import glob
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from typing import Optional


# ---------------------------------------------------------------------------
#  Prometheus query helpers
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    """GET a URL and parse JSON. Returns None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"    WARN: query failed: {e}", file=sys.stderr)
        return None


def query_instant(prom_url: str, query: str, ts: int) -> Optional[dict]:
    """Run an instant query. Returns the parsed JSON response or None."""
    qs = urllib.parse.urlencode({"query": query, "time": str(ts)})
    return _http_get_json(f"{prom_url}/api/v1/query?{qs}")


def _scalar_value(result: Optional[dict]) -> Optional[float]:
    """Pull the single scalar value from a Prometheus instant result."""
    if not result:
        return None
    arr = result.get("data", {}).get("result", [])
    if not arr:
        return None
    try:
        return float(arr[0]["value"][1])
    except (KeyError, ValueError, IndexError):
        return None


def _bucket_map(result: Optional[dict]) -> dict[str, float]:
    """Pull a {le -> count} dict from a Prometheus instant result."""
    out: dict[str, float] = {}
    if not result:
        return out
    for r in result.get("data", {}).get("result", []):
        le = r.get("metric", {}).get("le", "")
        try:
            out[le] = float(r["value"][1])
        except (KeyError, ValueError, IndexError):
            continue
    return out


def _bucket_diff(pre: dict[str, float], post: dict[str, float]) -> dict[str, float]:
    """Per-le bucket diff with counter-reset guard."""
    out = {}
    for le, hi in post.items():
        delta = hi - pre.get(le, 0.0)
        out[le] = hi if delta < 0 else delta
    return out


def _percentile_from_buckets(buckets: dict[str, float], q: float) -> Optional[float]:
    """Linear-interpolation quantile over a bucketed counter, mirroring
    Prometheus histogram_quantile semantics on a single histogram."""
    items = []
    for le, count in buckets.items():
        try:
            v = float("inf") if le == "+Inf" else float(le)
        except ValueError:
            continue
        items.append((v, count))
    items.sort(key=lambda x: x[0])
    if not items:
        return None
    total = items[-1][1]
    if total <= 0:
        return None
    target = q * total
    prev_le, prev_count = 0.0, 0.0
    for le, count in items:
        if count >= target:
            if count == prev_count:
                return le
            if math.isinf(le):
                return prev_le
            frac = (target - prev_count) / (count - prev_count)
            return prev_le + frac * (le - prev_le)
        prev_le, prev_count = le, count
    return items[-2][0] if len(items) >= 2 else None


def _rank_gt0_fraction(buckets: dict[str, float]) -> Optional[float]:
    """Fraction of samples with rank > 0 from a candidate_rank histogram.

    Bucket le=0 contains samples with rank <= 0 (i.e. rank=0). The total
    is the +Inf bucket. (1 - bucket[0]/total) is the fraction that
    required at least one fallback step.
    """
    if not buckets:
        return None
    # Prometheus serialises the le=0 bucket key as either "0" or "0.0"
    # depending on histogram type / serialisation path; tolerate both.
    bucket_0 = buckets.get("0.0", buckets.get("0"))
    bucket_inf = buckets.get("+Inf")
    if bucket_0 is None or bucket_inf is None or bucket_inf <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - bucket_0 / bucket_inf))


# ---------------------------------------------------------------------------
#  Per-metric snapshot
# ---------------------------------------------------------------------------

def snapshot_histogram(
    prom_url: str,
    metric_name: str,
    selector: str,
    t_start: int,
    t_end: int,
) -> dict:
    """Snapshot a histogram counter pair at start and end times,
    return mean / p50 / p90 / p99 / count / sum."""

    bucket_q = f"sum by (le) ({metric_name}_bucket{selector})"
    sum_q = f"sum({metric_name}_sum{selector})"
    count_q = f"sum({metric_name}_count{selector})"

    pre_b = _bucket_map(query_instant(prom_url, bucket_q, t_start))
    post_b = _bucket_map(query_instant(prom_url, bucket_q, t_end))
    pre_s = _scalar_value(query_instant(prom_url, sum_q, t_start)) or 0.0
    post_s = _scalar_value(query_instant(prom_url, sum_q, t_end)) or 0.0
    pre_c = _scalar_value(query_instant(prom_url, count_q, t_start)) or 0.0
    post_c = _scalar_value(query_instant(prom_url, count_q, t_end)) or 0.0

    diff_b = _bucket_diff(pre_b, post_b)
    diff_sum = post_s - pre_s
    diff_count = post_c - pre_c
    if diff_sum < 0:
        diff_sum = post_s
    if diff_count < 0:
        diff_count = post_c

    mean = (diff_sum / diff_count) if diff_count > 0 else None
    p50 = _percentile_from_buckets(diff_b, 0.50)
    p90 = _percentile_from_buckets(diff_b, 0.90)
    p99 = _percentile_from_buckets(diff_b, 0.99)

    return {
        "metric": metric_name,
        "count": diff_count,
        "sum": round(diff_sum, 6),
        "mean": round(mean, 6) if mean is not None else None,
        "p50": round(p50, 6) if p50 is not None else None,
        "p90": round(p90, 6) if p90 is not None else None,
        "p99": round(p99, 6) if p99 is not None else None,
        "buckets": {le: round(v, 3) for le, v in diff_b.items()},
    }


# ---------------------------------------------------------------------------
#  Trial-level driver
# ---------------------------------------------------------------------------

def process_trial(
    trial_dir: str,
    prom_url_override: Optional[str],
    force: bool,
) -> Optional[dict]:
    """Pull quality metrics for a single trial directory."""
    metrics_dir = os.path.join(trial_dir, "metrics-saturation")
    meta_path = os.path.join(metrics_dir, "meta.json")
    out_path = os.path.join(metrics_dir, "quality.json")

    if not os.path.isfile(meta_path):
        print(f"  SKIP {trial_dir}: no meta.json")
        return None

    if os.path.isfile(out_path) and not force:
        print(f"  SKIP {trial_dir}: quality.json exists (use --force)")
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    prom_url = prom_url_override or meta.get("prometheus_url")
    if not prom_url:
        print(f"  SKIP {trial_dir}: no prometheus_url")
        return None

    try:
        t_start = int(meta["time_range"]["start"])
        # Use snapshot_end (= end + snap_end_pad) when available; that
        # matches what existing snap_*.json files were computed against.
        t_end = int(meta.get("snapshot_end", meta["time_range"]["end"]))
    except (KeyError, ValueError) as e:
        print(f"  SKIP {trial_dir}: bad time_range ({e})")
        return None

    print(f"  PULL {trial_dir} ({t_end - t_start}s window @ {prom_url})")

    score_data = snapshot_histogram(
        prom_url,
        "scheduler_parasched_selected_node_score",
        "",
        t_start,
        t_end,
    )

    rank_data = snapshot_histogram(
        prom_url,
        "parasched_candidate_rank_accepted",
        "",
        t_start,
        t_end,
    )
    rank_data["rank_gt0_fraction"] = _rank_gt0_fraction(rank_data.get("buckets") or {})
    if rank_data["rank_gt0_fraction"] is not None:
        rank_data["rank_gt0_fraction"] = round(rank_data["rank_gt0_fraction"], 6)

    out = {
        "trial_dir": os.path.relpath(trial_dir),
        "time_range": {"start": t_start, "end": t_end},
        "selected_node_score": score_data,
        "candidate_rank_accepted": rank_data,
    }

    if score_data["count"] in (0, None) and rank_data["count"] in (0, None):
        print("    WARN: both metrics returned zero samples — Prometheus retention "
              "may have expired or scheduler had no traffic in this window")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return out


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

def expand_trial_dirs(patterns: list[str]) -> list[str]:
    """Given experiment-group dirs OR trial dirs (or globs of either),
    return a flat sorted list of trial-* directories."""
    seen = set()
    out = []
    for pat in patterns:
        # Glob expansion (no-op if pat is a literal existing path).
        candidates = glob.glob(pat) or ([pat] if os.path.isdir(pat) else [])
        for c in candidates:
            if not os.path.isdir(c):
                continue
            base = os.path.basename(c.rstrip(os.sep))
            if base.startswith("trial-"):
                trials = [c]
            else:
                trials = sorted(glob.glob(os.path.join(c, "trial-*")))
            for t in trials:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pull scheduling-quality metrics from Prometheus for "
                    "experiment trials whose meta.json is on disk.")
    ap.add_argument("paths", nargs="+",
                    help="Experiment-group dirs, trial dirs, or globs of either.")
    ap.add_argument("--prometheus-url", default=None,
                    help="Override Prometheus URL (default: read from meta.json).")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing quality.json files.")
    args = ap.parse_args()

    trials = expand_trial_dirs(args.paths)
    if not trials:
        print("No trial directories found.", file=sys.stderr)
        return 1

    print(f"Processing {len(trials)} trial(s)")
    n_ok = 0
    for t in trials:
        try:
            if process_trial(t, args.prometheus_url, args.force) is not None:
                n_ok += 1
        except Exception as e:
            print(f"  ERROR {t}: {e}", file=sys.stderr)

    print(f"Done. {n_ok}/{len(trials)} trials produced quality.json")
    return 0 if n_ok > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
