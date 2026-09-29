"""The JSON envelope that carries measured data from export to plotting.

Figure scripts hold no measurements. Every number a figure draws is produced by
an export step that reads the committed archive under `experiments/archive/` (or,
for the trace figure, the public trace) and writes one JSON file per figure. The
envelope mirrors the one the simulation side already uses in
`simulation/common/matrix.py`: a schema version, a `meta` block describing
provenance, and a payload.

    {
      "schema_version": 1,
      "meta": {
        "figure":       "robustness",
        "source":       "cluster",
        "boards":       ["K", "P"],
        "filter":       "duration-outlier-v1",
        "generator":    "export-figure-data.py",
        "generated_at": "2026-09-21T12:00:00+00:00"
      },
      "data": { ... }
    }

`meta` deliberately records no absolute paths: the file is meant to travel with
a figure, not to describe the machine that produced it.

Exported files are generated artifacts under `experiments/work/`, which is not
version controlled. What is version controlled is a hash of each figure's `data`
block (`data_sha256`), so a change in what a figure draws cannot go unnoticed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

#: Where the export step writes, and where the figure scripts read. Generated,
#: and excluded from version control.
DATA_DIRNAME = os.path.join("work", "figure-data")


def data_dir(experiments_root: str) -> str:
    return os.path.join(experiments_root, DATA_DIRNAME)


def path_for(experiments_root: str, figure: str) -> str:
    return os.path.join(data_dir(experiments_root), f"{figure}.json")


def canonical_json(obj: Any, *, indent: int | None = 2) -> str:
    """Serialise deterministically: sorted keys, ASCII, and no NaN or infinity.

    Raises:
        ValueError: If `obj` contains a non-finite float, which JSON cannot
            represent and which would otherwise be written as invalid `NaN`.
    """

    separators = (",", ": ") if indent is not None else (",", ":")
    return json.dumps(obj, indent=indent, sort_keys=True, ensure_ascii=True,
                      allow_nan=False, separators=separators)


def data_sha256(obj: Any) -> str:
    """Hash of a JSON value, independent of file formatting and key order."""

    return hashlib.sha256(canonical_json(obj, indent=None).encode("utf-8")).hexdigest()


def write_text_atomic(path: str, text: str) -> str:
    """Write `text` to `path` so that a crash never leaves a truncated file.

    The newline is forced to LF: these files are hashed, and Windows text mode
    would otherwise rewrite it and change the hash on another machine.
    """

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                             dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def write(
    path: str,
    *,
    figure: str,
    source: str,
    data: Any,
    generator: str,
    boards: list[str] | None = None,
    filter_version: str | None = None,
    notes: str | None = None,
) -> str:
    """Write one figure's data with a provenance header. Returns `path`."""

    meta: dict[str, Any] = {
        "figure": figure,
        "source": source,
        "generator": generator,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if boards:
        meta["boards"] = boards
    if filter_version:
        meta["filter"] = filter_version
    if notes:
        meta["notes"] = notes

    payload = {"schema_version": SCHEMA_VERSION, "meta": meta, "data": data}
    return write_text_atomic(path, canonical_json(payload) + "\n")


def load(path: str, *, figure: str) -> Any:
    """Read one figure's data, checking the envelope before returning it.

    The error messages name the export command, because the usual reason for a
    miss is that the figure has not been exported yet.
    """

    if not os.path.exists(path):
        raise SystemExit(
            f"missing input data: {path}\n"
            f"Run the export step for '{figure}' first; see experiments/README.md."
        )

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SystemExit(
            f"{path}: schema_version {version!r}, expected {SCHEMA_VERSION}. "
            "Re-run the export step."
        )

    recorded = payload.get("meta", {}).get("figure")
    if recorded != figure:
        raise SystemExit(
            f"{path}: holds data for figure {recorded!r}, not {figure!r}."
        )

    if "data" not in payload:
        raise SystemExit(f"{path}: envelope has no 'data' block.")

    return payload["data"]
