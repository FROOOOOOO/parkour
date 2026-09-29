#!/usr/bin/env python3
"""Pull the windowed ACF of module-F rounds from Prometheus.

The data-plane figure reports each round's all-candidates-failed rate (ACF) at
the T99 completion cut: over the part of the round in which its first 99% of
binds completed, not over the whole round. The round summaries run-module-f.sh
writes hold only the whole-round rate, so this script rebuilds the prefix rates
from the binder's counters at 1 s resolution. Run it right after the campaign,
while Prometheus still holds those samples: once they have expired, nothing can
recompute these values.

The binder's counters start at zero with its process, once per round, so the
rate over the prefix [round start, T] is

    ACF(T) = acf(T) / (bind_success(T) + acf(T))

with the denominator convention of the whole-round rate. Completion landmarks
are read off the bind-success counter itself (the first sample reaching a given
share of the round's final bind count), so no alignment with the CL2 completion
curve is needed.

A round's values are marked verified only when its counters end at the counts
its round-summary.json recorded, to within one, and never decrease over the
window; a decrease means the window reaches into another binder's samples. The
export reads the verified values only.

The output, `acf-windowed.csv`, goes next to `module-f/` by default, which is
where `reduce.py --anchored` reads it.

Usage:
    python experiments/scripts/pull-windowed-acf.py
    python experiments/scripts/pull-windowed-acf.py --results <root> --runs <run-id> ...
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Iterator

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common.data import write_text_atomic  # noqa: E402

#: Shares of the round's final bind count at which the prefix rate is reported.
#: The figure uses 0.99.
LANDMARKS = (0.50, 0.90, 0.95, 0.99, 0.999, 1.0)
#: The query opens this long before the round's CL2 window, so that it covers
#: the round's binder from its first sample.
PRE_ROLL_S = 120
SUCCESS = 'parasched_bind_result_total{component="binder",result="success"}'
ACF = 'parasched_all_candidates_failed_total{component="binder"}'


def landmark_key(share: float) -> str:
    """Column prefix of a landmark: t50, t90, t95, t99, t99_9, t100."""

    return f"t{share * 100:g}".replace(".", "_")


FIELDS = (["run_id", "round", "method", "verified", "final_success", "final_acf",
           "summary_success", "summary_acf", "summary_acf_rate"]
          + [f"{landmark_key(share)}_{column}" for share in LANDMARKS
             for column in ("offset_s", "success", "acf", "acf_rate")])


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def query_range(prometheus: str, expr: str, start: int, end: int,
                step: int = 1) -> dict[int, float]:
    """{epoch: value} of the first series a range query returns; {} when the
    query fails or returns no series."""

    query = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    try:
        with urllib.request.urlopen(f"{prometheus}/api/v1/query_range?{query}",
                                    timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))["data"]["result"]
    except (OSError, ValueError, KeyError) as error:
        print(f"    query failed: {error}", file=sys.stderr)
        return {}
    if not result:
        return {}
    return {int(point[0]): float(point[1]) for point in result[0]["values"]}


def rounds(results: str, runs: list[str] | None) -> Iterator[tuple[str, str, str]]:
    """(run id, round directory name, path) of every round of the chosen runs."""

    base = os.path.join(results, "module-f")
    if not os.path.isdir(base):
        raise SystemExit(f"no module-F results under {results} (expected {base})")
    if runs is None:
        runs = sorted(name for name in os.listdir(base)
                      if os.path.isdir(os.path.join(base, name)))
    for run in runs:
        run_path = os.path.join(base, run)
        if not os.path.isdir(run_path):
            raise SystemExit(f"no module-F run {run!r} under {base}")
        for name in sorted(os.listdir(run_path)):
            path = os.path.join(run_path, name)
            if name.startswith("round-") and os.path.isdir(path):
                yield run, name, path


def analyse(path: str, prometheus: str | None) -> tuple[dict[str, Any] | None, str]:
    """One round's windowed ACF row, or None and the reason it has none."""

    meta_path = os.path.join(path, "metrics-bind", "meta.json")
    summary_path = os.path.join(path, "round-summary.json")
    if not (os.path.isfile(meta_path) and os.path.isfile(summary_path)):
        return None, "no metrics-bind/meta.json or round-summary.json"
    meta, summary = _read_json(meta_path), _read_json(summary_path)
    url = prometheus or meta.get("prometheus_url")
    if not url:
        return None, "no Prometheus URL recorded; pass --prometheus-url"
    control = summary.get("control_plane", {})
    start = int(meta["time_range"]["start"]) - PRE_ROLL_S
    # The collector's snapshot end; a censored round keeps counting past it.
    end = int(meta.get("snapshot_end", meta["time_range"]["end"]))

    success = query_range(url, SUCCESS, start, end)
    acf = query_range(url, ACF, start, end)
    shared = sorted(set(success) & set(acf))
    if not shared:
        return None, "Prometheus holds no binder counters for the round's window"

    final_success, final_acf = success[shared[-1]], acf[shared[-1]]
    matches = (abs(final_success - control.get("bind_success", -1)) <= 1
               and abs(final_acf - control.get("acf_count", -1)) <= 1)
    monotone = all(success[a] <= success[b] and acf[a] <= acf[b]
                   for a, b in zip(shared, shared[1:]))
    row: dict[str, Any] = {
        "method": control.get("candidate_k"),
        "verified": matches and monotone,
        "final_success": final_success,
        "final_acf": final_acf,
        "summary_success": control.get("bind_success"),
        "summary_acf": control.get("acf_count"),
        "summary_acf_rate": control.get("acf_rate"),
    }
    for share in LANDMARKS:
        target = final_success * share
        t = next((t for t in shared if success[t] >= target), shared[-1])
        placed, failed = success[t], acf[t]
        key = landmark_key(share)
        row[f"{key}_offset_s"] = t - shared[0]
        row[f"{key}_success"] = placed
        row[f"{key}_acf"] = failed
        row[f"{key}_acf_rate"] = failed / (placed + failed) if (placed + failed) else 0.0
    return row, ""


def _valid(path: str) -> bool:
    """Whether the campaign counted the round, as its summary records."""

    try:
        return bool(_read_json(os.path.join(path, "round-summary.json")).get("valid"))
    except (OSError, ValueError):
        return False


def _rate(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", default=os.path.join(_EXPERIMENTS, "results"),
                        help="results root holding module-f/<run-id>/round-*/ "
                             "(default: experiments/results)")
    parser.add_argument("--runs", nargs="+", metavar="RUN_ID",
                        help="the campaign's runs (default: every run under module-f/)")
    parser.add_argument("--out",
                        help="output CSV (default: <results>/acf-windowed.csv, "
                             "where reduce.py --anchored <results> reads it)")
    parser.add_argument("--prometheus-url",
                        help="default: the URL each round's collector recorded in "
                             "metrics-bind/meta.json")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing output")
    args = parser.parse_args(argv)

    out = args.out or os.path.join(args.results, "acf-windowed.csv")
    if os.path.exists(out) and not args.force:
        raise SystemExit(f"{out} exists, and its values cannot be pulled again once "
                         "Prometheus has dropped the samples; pass --force to replace it")

    rows, unusable = [], []
    for run, name, path in rounds(args.results, args.runs):
        row, reason = analyse(path, args.prometheus_url)
        if row is None:
            print(f"skip {run}/{name}: {reason}", file=sys.stderr)
        else:
            row["run_id"], row["round"] = run, name
            rows.append(row)
            flag = "" if row["verified"] else "  UNVERIFIED"
            print(f"{run}/{name}: whole={_rate(row['summary_acf_rate'])} "
                  f"t99={_rate(row['t99_acf_rate'])}{flag}")
        if (row is None or not row["verified"]) and _valid(path):
            unusable.append(f"{run}/{name}")
    if not rows:
        print("no round could be analysed; nothing written", file=sys.stderr)
        return 1

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    write_text_atomic(out, buffer.getvalue())
    verified = sum(1 for row in rows if row["verified"])
    print(f"\nwrote {out}: {len(rows)} rounds, {verified} verified")
    if unusable:
        print(f"{len(unusable)} round(s) the campaign counted have no verified windowed "
              f"ACF, which the figure needs: {', '.join(unusable)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
