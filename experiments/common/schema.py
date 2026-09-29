"""Record format of the committed figure-data archive, `experiments/archive/`.

The archive holds the per-trial records that the figure export reads, limited to
the fields it reads, for every trial of every cell that feeds a figure or the
overhead table. It is the first artifact downstream of which every stage is
repository code, so a reader can re-run the outlier filter, the aggregation and
the plotting without the raw results. Raw results (CL2 output, Prometheus dumps, logs) are not
archived; `manifest.json` records a digest of the raw files each record was
reduced from, so whoever holds them can confirm the archive is theirs.

Layout:

    archive/
      boards/<board>.json   scheduler runs: B1 B2 B3 K P ablation
      godel.json            the Godel baseline runs that feed a figure
      quality.json          per-trial placement-quality histograms (ablation)
      occupancy.json        per-trial occupancy intervals of the B2 runs plotted
      overhead.json         per-trial resource use and latency of the overhead table's runs
      dataplane.json        the anchored module-F rounds with their windowed ACF
      manifest.json         file hashes, raw-input digests, registry and reducer hashes

Every file is canonical JSON (see `common/data.canonical_json`), so the same
records always produce the same bytes.

An archive reduced for some boards only (`reduce.py --boards`) holds their
files and the record groups whose boards are all among them; its manifest lists
exactly those files.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Iterable

from common.data import canonical_json, write_text_atomic

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = "parkour-archive/v1"
ARCHIVE_DIR = os.path.join(_EXPERIMENTS, "archive")

RUN_BOARDS = ("B1", "B2", "B3", "K", "P", "ablation")
#: Every board the registry declares: the scheduler boards, the Godel baseline
#: and the module-F campaign.
ALL_BOARDS = RUN_BOARDS + ("godel", "F")
FILES = {
    "godel": "godel.json",
    "quality": "quality.json",
    "occupancy": "occupancy.json",
    "overhead": "overhead.json",
    "dataplane": "dataplane.json",
    "manifest": "manifest.json",
}
#: The boards each record group other than the board files is reduced from.
GROUP_BOARDS = {
    "godel": ("godel",),
    "quality": ("ablation",),
    "occupancy": ("B2",),
    "overhead": ("B2",),
    "dataplane": ("F",),
}

#: Per-trial fields of a scheduler run, named as process-results.py names them:
#: pods, seconds, pods/s, and rates as fractions.
RUN_TRIAL_FIELDS = (
    "trial", "expected_pods", "scheduled_pods", "scheduling_duration_s",
    "throughput_pods_per_s", "acf_rate", "acf_count", "bind_conflict_rate",
    "bind_conflict_count", "is_timeout",
)
#: The Godel baseline also keeps the throughput its collector reported, for the
#: advisory comparison with the CL2 throughput the figures use.
GODEL_TRIAL_FIELDS = RUN_TRIAL_FIELDS + ("collector_throughput_pods_per_s",)

#: Recorded run parameters copied into the archive. Everything else a run's
#: configuration may hold, such as monitoring endpoints, stays out.
PUBLIC_PARAMS = (
    "num_nodes", "num_schedulers", "num_backup", "strategy", "strategy_seed",
    "conflict_penalty", "sync_period", "num_partitions", "sync_pattern",
    "pods_per_node", "cpu_request", "memory_request", "capacity_variance",
)

#: Per-round fields of a module-F round.
ROUND_FIELDS = (
    "run_id", "round_dir", "experiment", "order", "trial", "method", "profile",
    "kept", "censored", "q_bind_at_t99", "throughput_raw_pods_per_s", "acf_rate",
    "tail_fraction", "injection_observed", "injection_pass", "injection_failed",
    "failed_semantics_valid", "t99_acf_rate", "windowed_acf_verified",
)

#: Occupancy metrics the figure plots, per trial and interval.
OCCUPANCY_METRICS = ("acf_rate_estimate", "mean_useful_placement_throughput")

#: Per-trial fields of the overhead table, named as process-results.py names
#: them. Latencies are P99s in milliseconds, from the saturation phase's
#: histogram snapshots; CPU is in cores and memory (resident set) in bytes,
#: each summed over the component's pods at the end of the saturation phase.
OVERHEAD_TRIAL_FIELDS = (
    "trial", "algo_p99_ms", "e2e_p99_ms", "scheduler_cpu_total",
    "scheduler_mem_rss_total", "binder_cpu", "dispatcher_cpu",
)
#: The saturation-phase metric files, as collect-metrics.sh names them, that
#: those fields are computed from.
OVERHEAD_SOURCES = (
    "snap_algo_latency.json", "snap_e2e_latency.json", "scheduler_cpu.json",
    "scheduler_memory_rss.json", "binder_cpu.json", "dispatcher_cpu.json",
)


def board_file(board: str) -> str:
    """Archive-relative name of a board file. Always '/'-separated: these names
    are manifest keys, and must not depend on the platform that wrote them."""

    return f"boards/{board}.json"


def archive_files(boards: Iterable[str] | None = None) -> list[str]:
    """Every archive file other than the manifest, as archive-relative paths.

    With `boards`, the files of an archive reduced for those boards only: their
    board files, and each record group whose boards are all among them.
    """

    held = set(ALL_BOARDS if boards is None else boards)
    return ([board_file(board) for board in RUN_BOARDS if board in held]
            + [FILES[key] for key, needs in GROUP_BOARDS.items() if set(needs) <= held])


def held_boards(files: Iterable[str]) -> tuple[str, ...]:
    """The boards an archive of `files` was reduced for; `archive_files` inverted."""

    names = set(files)
    held = {board for board in RUN_BOARDS if board_file(board) in names}
    for key, needs in GROUP_BOARDS.items():
        if FILES[key] in names:
            held.update(needs)
    return tuple(board for board in ALL_BOARDS if board in held)


def render(payload: dict[str, Any]) -> str:
    return canonical_json(payload) + "\n"


def write(archive_dir: str, relative: str, payload: dict[str, Any]) -> str:
    return write_text_atomic(os.path.join(archive_dir, relative), render(payload))


def read(archive_dir: str, relative: str) -> dict[str, Any]:
    path = os.path.join(archive_dir, relative)
    if not os.path.exists(path):
        raise SystemExit(f"missing archive file: {path}")
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema") != SCHEMA:
        raise SystemExit(f"{path}: schema {payload.get('schema')!r}, expected {SCHEMA}")
    return payload


def load(archive_dir: str = ARCHIVE_DIR, boards: Iterable[str] | None = None) -> dict[str, Any]:
    """Every archive file, keyed as `boards/<board>` or by its short name; with
    `boards`, the files of an archive of those boards only, and the manifest."""

    names = set(archive_files(boards))
    archive = {f"boards/{board}": read(archive_dir, board_file(board))
               for board in RUN_BOARDS if board_file(board) in names}
    for key, name in FILES.items():
        if key == "manifest" or name in names:
            archive[key] = read(archive_dir, name)
    return archive


# ---------------------------------------------------------------------------
#  Digests
# ---------------------------------------------------------------------------

def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


#: Source files, relative to `experiments/`, whose content determines the archive.
#: Their digest is recorded so that an archive built by other code shows as such.
REDUCER_SOURCES = (
    "scripts/reduce.py",
    "scripts/process-results.py",
    "scripts/analyze-temporal-occupancy.py",
    "common/cl2.py",
    "common/data.py",
    "common/registry.py",
    "common/schema.py",
)


def reducer_sha256() -> str:
    """Digest of the reducer's source, in the form `tree_digest` gives files.

    Line endings are normalised first: the repository stores the source with LF,
    and a Windows checkout may hold it with CRLF, which must not read as a
    different reducer.
    """

    lines = []
    for relative in REDUCER_SOURCES:
        with open(os.path.join(_EXPERIMENTS, relative), "rb") as handle:
            source = handle.read().replace(b"\r\n", b"\n")
        lines.append(f"{relative}\t{hashlib.sha256(source).hexdigest()}\n")
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


def tree_digest(root: str, paths: Iterable[str]) -> str:
    """One digest over files named relative to `root`, independent of order.

    Paths are relative to `root`, so the digest describes content and layout
    below it rather than where the raw results happen to be kept.
    """

    lines = sorted(
        f"{os.path.relpath(path, root).replace(os.sep, '/')}\t{file_sha256(path)}\n"
        for path in paths
    )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
