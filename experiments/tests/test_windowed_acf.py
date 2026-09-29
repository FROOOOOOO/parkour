"""pull-windowed-acf.py against a fake Prometheus.

The fake serves the binder's two counters for synthetic module-F rounds, each in
a window of its own: one whose counters end at the counts its summary recorded,
one whose summary disagrees with them, one whose window reaches a previous
binder's samples, and one that Prometheus no longer holds. The script must
compute the prefix rates at each landmark, verify only the first, and write a
file that reduce.py reads back into the archive's data-plane records.
"""

import copy
import csv
import importlib.util
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from common import registry as reg
from rawdata import Round, write_round

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(EXPERIMENTS, "scripts")
CELL = copy.deepcopy(reg.cell_index(reg.build())[("F", "F1-E-E3-Z0")])

#: (seconds after the window's start, bind successes, all-candidates-failed) at
#: each scrape of a round's binder, whose counters appear 60 s before the window.
SCRAPES = [(-60, 0, 0), (-45, 0, 0), (-30, 0, 0), (-15, 0, 0), (0, 0, 0),
           (15, 5000, 10), (30, 9000, 60), (45, 9950, 100), (60, 10000, 120)]
#: A previous binder's last sample, 15 s before this round's binder appears.
PREVIOUS = (-75, 8000, 300)
#: Counter samples by the query's start: {"success"|"acf": [(epoch, value)]}.
SERIES: dict[int, dict[str, list[tuple[int, float]]]] = {}


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_script("pull_windowed_acf", "pull-windowed-acf.py")
reducer = load_script("reduce_archive", "reduce.py")


class FakePrometheus(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        query = params["query"][0]
        start, end, step = (int(params[key][0]) for key in ("start", "end", "step"))
        samples = SERIES.get(start, {}).get(
            "success" if "parasched_bind_result_total" in query else "acf", [])
        values, current = [], None
        for when in range(start, end + 1, step):
            current = next((v for t, v in reversed(samples) if t <= when), current)
            if current is not None:
                values.append([when, str(current)])
        result = [{"metric": {"component": "binder"}, "values": values}] if values else []
        body = json.dumps({"status": "success",
                           "data": {"resultType": "matrix", "result": result}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def prometheus(monkeypatch):
    SERIES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrometheus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("NO_PROXY", "no_proxy"):  # never route the fake through a proxy
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def add_round(root, run_id, number, kind, prometheus_url):
    """Write one round and serve its counters: `verified`, `mismatch` (the
    summary records five more binds), `restart` (the window reaches the
    previous binder) or `expired` (no samples left)."""

    start = 1_000_000 + 10_000 * number
    item = Round(run_id, f"round-0{number}-trial-0{number}-E3-Z0", CELL, order=number,
                 trial=number, kept=True, throughput=200.0, acf_rate=0.012, t99_acf_rate=0.0,
                 bind_success=10_005 if kind == "mismatch" else 10_000, acf_count=120,
                 window=(start, start + 200, start + 230))
    write_round(root, item, prometheus_url)
    if kind != "expired":
        scrapes = ([PREVIOUS] if kind == "restart" else []) + SCRAPES
        SERIES[start - tool.PRE_ROLL_S] = {
            "success": [(start + t, float(s)) for t, s, _ in scrapes],
            "acf": [(start + t, float(a)) for t, _, a in scrapes]}
    return item


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == tool.FIELDS
        return {row["round"]: row for row in reader}


def test_prefix_rates_and_verification(tmp_path, prometheus, capsys):
    root = str(tmp_path)
    items = {kind: add_round(root, "F1-E-run", n, kind, prometheus)
             for n, kind in enumerate(("verified", "mismatch", "restart", "expired"), 1)}

    # Three rounds the campaign counted lack a verified value, so it fails.
    assert tool.main(["--results", root]) == 1
    assert "3 round(s) the campaign counted" in capsys.readouterr().err

    rows = read_rows(os.path.join(root, "acf-windowed.csv"))
    assert set(rows) == {items[k].round_dir for k in ("verified", "mismatch", "restart")}
    good = rows[items["verified"].round_dir]
    assert good["verified"] == "True" and good["method"] == "2"
    # Offsets count from the binder's first sample, 60 s before the window.
    for key, offset, placed, failed in (("t50", 75, 5000, 10), ("t90", 90, 9000, 60),
                                        ("t95", 105, 9950, 100), ("t99", 105, 9950, 100),
                                        ("t99_9", 120, 10000, 120), ("t100", 120, 10000, 120)):
        assert (int(good[f"{key}_offset_s"]), float(good[f"{key}_success"]),
                float(good[f"{key}_acf"])) == (offset, placed, failed)
        assert float(good[f"{key}_acf_rate"]) == failed / (placed + failed)
    assert rows[items["mismatch"].round_dir]["verified"] == "False"
    assert rows[items["restart"].round_dir]["verified"] == "False"

    # reduce.py reads the file back: only the verified round carries a value.
    payload, _ = reducer.reduce_dataplane(root)
    archived = {row["round_dir"]: row for row in payload["rounds"]}
    assert archived[items["verified"].round_dir]["t99_acf_rate"] == 100 / 10_050
    assert archived[items["verified"].round_dir]["windowed_acf_verified"] is True
    for kind in ("mismatch", "restart", "expired"):
        assert archived[items[kind].round_dir]["t99_acf_rate"] is None
        assert archived[items[kind].round_dir]["windowed_acf_verified"] is False


def test_url_override_runs_and_no_overwrite(tmp_path, prometheus):
    root = str(tmp_path)
    # The collector recorded a Prometheus that is gone; --prometheus-url overrides it.
    add_round(root, "F1-E-run", 1, "verified", "http://127.0.0.1:9")
    add_round(root, "F1-E-pilot", 2, "verified", "http://127.0.0.1:9")
    out = str(tmp_path / "acf.csv")
    args = ["--results", root, "--runs", "F1-E-run", "--prometheus-url", prometheus,
            "--out", out]
    assert tool.main(args) == 0
    assert list(read_rows(out)) == ["round-01-trial-01-E3-Z0"]

    # A second pull may find the samples gone, so it never replaces a file silently.
    with pytest.raises(SystemExit, match="--force"):
        tool.main(args)
    assert tool.main(args + ["--force"]) == 0
    with pytest.raises(SystemExit, match="no module-F run"):
        tool.main(["--results", root, "--runs", "F1-E-missing", "--out", out, "--force"])
