"""What the collector records when a trial ends.

A fake Prometheus answers the collector's queries with known values: histogram
counters at the window's start and end for the instant queries, and a few pod
series for the range queries; every other query gets an empty result.

Two things must come out of a saturation phase without a later re-query: the
quality histograms (quality.json), and the metrics the overhead table is
reduced from, which must reach the archive record through process-results.py
and reduce.py unchanged.
"""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from common import registry as reg
from rawdata import Trial, write_run

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(EXPERIMENTS, "scripts")
START, END, PAD = 1_000_000, 1_000_100, 30

SCORE = "scheduler_parasched_selected_node_score"
RANK = "parasched_candidate_rank_accepted"
ALGO = "scheduler_scheduling_algorithm_duration_seconds"
E2E = "scheduler_pod_scheduling_sli_duration_seconds"
#: Counter values at the window's start and at its padded end:
#: (cumulative buckets, sum, count).
COUNTERS = {
    SCORE: {START: ({"90": 10, "100": 40, "+Inf": 40}, 3700.0, 40),
            END + PAD: ({"90": 110, "100": 540, "+Inf": 540}, 52_200.0, 540)},
    RANK: {START: ({"0.0": 30, "1.0": 38, "2.0": 40, "+Inf": 40}, 12.0, 40),
           END + PAD: ({"0.0": 430, "1.0": 518, "2.0": 538, "+Inf": 540}, 170.0, 540)},
    # 100 attempts in the window; the 99th percentile falls 90% of the way
    # through the (0.25 s, 0.5 s] bucket, and the (20 s, 40 s] bucket for e2e.
    ALGO: {START: ({"0.1": 5, "0.25": 5, "0.5": 5, "+Inf": 5}, 0.5, 5),
           END + PAD: ({"0.1": 55, "0.25": 95, "0.5": 105, "+Inf": 105}, 20.5, 105)},
    E2E: {START: ({"10": 0, "20": 0, "40": 0, "+Inf": 0}, 0.0, 0),
          END + PAD: ({"10": 10, "20": 90, "40": 100, "+Inf": 100}, 2000.0, 100)},
}
#: Range-query series per (metric, container): each pod's samples in the window.
RANGES = {
    ("container_cpu_usage_seconds_total", "scheduler"): {
        "para-scheduler-0": (0.5, 1.5), "para-scheduler-1": (0.75, 1.25)},
    ("container_memory_rss", "scheduler"): {
        "para-scheduler-0": (1e9, 1.6e9), "para-scheduler-1": (1e9, 1.65e9)},
    ("container_cpu_usage_seconds_total", "binder"): {"para-binder-0": (0.2, 0.48)},
    ("container_cpu_usage_seconds_total", "dispatcher"): {"para-dispatcher-0": (0.1, 0.305)},
}


def answer(query: str, when: int) -> list:
    for metric, points in COUNTERS.items():
        if metric not in query or when not in points:
            continue
        buckets, total, count = points[when]
        if query.startswith("sum by (le)"):
            return [{"metric": {"le": le}, "value": [when, str(v)]} for le, v in buckets.items()]
        value = total if f"{metric}_sum" in query else count
        return [{"metric": {}, "value": [when, str(value)]}]
    return []


def answer_range(query: str) -> list:
    for (metric, container), pods in RANGES.items():
        if metric in query and f'container="{container}"' in query:
            return [{"metric": {"pod": pod},
                     "values": [[START + 15 * i, str(v)] for i, v in enumerate(samples)]}
                    for pod, samples in pods.items()]
    return []


class FakePrometheus(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        query = params.get("query", [""])[0]
        if url.path.endswith("/query_range"):
            data = {"resultType": "matrix", "result": answer_range(query)}
        else:
            data = {"resultType": "vector",
                    "result": answer(query, int(float(params.get("time", ["0"])[0])))}
        body = json.dumps({"status": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def prometheus(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrometheus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("NO_PROXY", "no_proxy"):  # never route the fake through a proxy
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def collect(output, prometheus):
    """Run collect-metrics.sh for one phase's window into `output`."""

    result = subprocess.run(
        ["bash", os.path.join(SCRIPTS, "collect-metrics.sh"), "--output", str(output),
         "--start", str(START), "--end", str(END), "--snap-end-pad", str(PAD),
         "--prometheus-url", prometheus],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_quality():
    return {
        "selected_node_score": {"count": 500.0, "sum": 48_500.0, "mean": 97.0},
        "candidate_rank_accepted": {"count": 500.0,
                                    "buckets": {"0.0": 400.0, "1.0": 480.0, "2.0": 498.0,
                                                "+Inf": 500.0}},
    }


def check(quality):
    want = expected_quality()
    for key, value in want["selected_node_score"].items():
        assert quality["selected_node_score"][key] == value
    rank = quality["candidate_rank_accepted"]
    assert rank["count"] == want["candidate_rank_accepted"]["count"]
    assert rank["buckets"] == want["candidate_rank_accepted"]["buckets"]
    assert rank["rank_gt0_fraction"] == 0.2


needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl") and shutil.which("python3")),
    reason="the collector needs bash, curl and python3")


def test_pull_takes_the_difference_over_the_padded_window(tmp_path, prometheus):
    pull = load_script("pull_quality", "pull-quality-metrics.py")
    metrics = tmp_path / "trial-1" / "metrics-saturation"
    metrics.mkdir(parents=True)
    (metrics / "meta.json").write_text(json.dumps({
        "prometheus_url": prometheus,
        "time_range": {"start": str(START), "end": str(END), "step": "15s"},
        "snapshot_end": str(END + PAD)}), encoding="utf-8")
    pull.process_trial(str(tmp_path / "trial-1"), None, force=False)
    check(json.loads((metrics / "quality.json").read_text(encoding="utf-8")))


@needs_shell
def test_collector_captures_quality_for_the_saturation_phase(tmp_path, prometheus):
    outputs = {}
    for phase in ("saturation", "latency"):
        outputs[phase] = tmp_path / "trial-1" / f"metrics-{phase}"
        collect(outputs[phase], prometheus)
    check(json.loads((outputs["saturation"] / "quality.json").read_text(encoding="utf-8")))
    assert not (outputs["latency"] / "quality.json").exists()


@needs_shell
def test_collector_records_what_the_overhead_table_needs(tmp_path, prometheus):
    # A recorded run of an overhead cell, whose saturation metrics are then
    # replaced by what the collector itself writes.
    declared = copy.deepcopy(reg.cell_index(reg.build())[("B2", "B2-10000n-P4")])
    declared["trials"] = 1
    run_dir = write_run(str(tmp_path), "B2", declared, [Trial(1, 48.25, acf=30, bind_conflicts=400)])
    metrics = os.path.join(run_dir, "trial-1", "metrics-saturation")
    shutil.rmtree(metrics)
    collect(metrics, prometheus)

    reducer = load_script("reduce_archive", "reduce.py")
    process_results = reducer._load_script("process_results", "process-results.py")
    registry = {"schema": reg.SCHEMA, "boards": reg.BOARDS, "cells": [declared], "inputs": {}}
    payload, _ = reducer.reduce_overhead(process_results, registry, str(tmp_path))
    # Latencies: the 99th percentile of the window's bucket differences, in ms.
    # Resources: each pod's last sample in the window, summed over the pods.
    assert payload["cells"] == [{"cell": "B2-10000n-P4", "trials": [{
        "trial": 1, "algo_p99_ms": 475.0, "e2e_p99_ms": 38_000.0,
        "scheduler_cpu_total": 2.75, "scheduler_mem_rss_total": 3_250_000_000,
        "binder_cpu": 0.48, "dispatcher_cpu": 0.305}]}]
