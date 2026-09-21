"""Generic execution support for self-contained figure matrices."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import ModelConstants, SimulationConfig, TRIAL_SEEDS
from core import simulate

CACHE_SCHEMA = "simulation-new/figure-cache-v1"


@dataclass(frozen=True)
class FigureCase:
    """One seed-independent figure work point."""

    case_id: str
    config: SimulationConfig
    num_schedulers: int


def make_case(config: SimulationConfig, num_schedulers: int) -> FigureCase:
    """Build a content-addressed case whose identity includes scheduler count."""

    payload = {
        "config": config.case_dict(),
        "num_schedulers": num_schedulers,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return FigureCase(f"case-{digest[:16]}", config, num_schedulers)


def registry_hash(cases: Iterable[FigureCase]) -> str:
    """Hash a complete ordered figure registry."""

    payload = [
        {
            "case_id": case.case_id,
            "config": case.config.case_dict(),
            "num_schedulers": case.num_schedulers,
        }
        for case in sorted(cases, key=lambda item: item.case_id)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_hash(paths: Iterable[Path]) -> str:
    """Hash source files in stable path order."""

    digest = hashlib.sha256()
    for path in sorted((item.resolve() for item in paths), key=str):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_one(
    case: FigureCase,
    seed: int,
    trial: int,
    constants: ModelConstants,
    include_cycle_metrics: bool,
) -> dict:
    """Execute one deterministic figure trial."""

    trial_constants = replace(constants, num_schedulers=case.num_schedulers)
    config = replace(case.config, trial_seed=seed)
    result = simulate(
        config,
        constants=trial_constants,
        include_cycle_metrics=include_cycle_metrics,
    )
    return {
        "case_id": case.case_id,
        "config": config.to_dict(),
        "num_schedulers": case.num_schedulers,
        "seed": seed,
        "trial": trial,
        **result,
    }


def _write_atomic(path: Path, payload: dict) -> None:
    """Write deterministic JSON without exposing partial cache contents."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload["runs"].sort(key=lambda run: (run["case_id"], run["seed"]))
    payload["meta"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            stream.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run_matrix(
    *,
    matrix_kind: str,
    cases: list[FigureCase],
    constants: ModelConstants,
    output: Path,
    source_digest: str,
    jobs: int = 1,
    resume: bool = False,
    include_cycle_metrics: bool = False,
) -> dict:
    """Run and checkpoint one figure-owned experiment matrix."""

    if jobs <= 0:
        raise ValueError("jobs must be positive")
    constants.validate()
    digest = registry_hash(cases)
    now = datetime.now(timezone.utc).isoformat()
    expected_meta = {
        "matrix_kind": matrix_kind,
        "fixed_constants": constants.to_dict(),
        "seeds": list(TRIAL_SEEDS),
        "case_count": len(cases),
        "run_count": len(cases) * len(TRIAL_SEEDS),
        "registry_sha256": digest,
        "source_sha256": source_digest,
        "cycle_metrics_included": include_cycle_metrics,
    }
    if output.exists() and resume:
        payload = json.loads(output.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CACHE_SCHEMA:
            raise ValueError("cannot resume a cache with a different schema")
        for key, value in expected_meta.items():
            if payload.get("meta", {}).get(key) != value:
                raise ValueError(f"cannot resume: meta.{key} differs")
    elif output.exists():
        raise FileExistsError(
            f"output exists; use --resume or remove it explicitly: {output}"
        )
    else:
        payload = {
            "schema_version": CACHE_SCHEMA,
            "meta": {**expected_meta, "created_at": now, "updated_at": now},
            "runs": [],
        }

    existing = {
        (run["case_id"], int(run["seed"])) for run in payload["runs"]
    }
    if len(existing) != len(payload["runs"]):
        raise ValueError("cache contains duplicate case/seed keys")
    pending = [
        (case, seed, trial)
        for case in cases
        for trial, seed in enumerate(TRIAL_SEEDS)
        if (case.case_id, seed) not in existing
    ]

    if jobs == 1:
        for case, seed, trial in pending:
            payload["runs"].append(
                _run_one(
                    case, seed, trial, constants, include_cycle_metrics
                )
            )
            _write_atomic(output, payload)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(
                    _run_one,
                    case,
                    seed,
                    trial,
                    constants,
                    include_cycle_metrics,
                ): (case.case_id, seed)
                for case, seed, trial in pending
            }
            for future in as_completed(futures):
                payload["runs"].append(future.result())
                _write_atomic(output, payload)
    if not pending:
        _write_atomic(output, payload)
    return payload
