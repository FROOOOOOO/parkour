#!/usr/bin/env python3
"""F0 data-plane latency calibration.

The script directly assigns warm-image pause Pods to exactly three real worker
nodes and observes Assigned -> Ready transitions from one kubectl watch process.
All latency deltas use one local monotonic clock, so node clock skew is excluded.

No third-party Python packages are required.  Run --self-test first and use
--preflight-only to verify the cluster without creating Kubernetes resources.
The default mode requires real workers.  The explicit --synthetic-kwok mode is
only for measuring a manually injected KWOK delay profile; it is never treated
as a real-worker calibration.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import json
import os
import random
import shutil
import statistics
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EXPERIMENT_LABEL = "parasched.io/experiment"
RUN_LABEL = "parasched.io/run-id"
SESSION_LABEL = "parasched.io/session"
WAVE_LABEL = "parasched.io/wave"
EXPERIMENT_VALUE = "f0-dataplane-calibration"
EXPECTED_NODE_COUNT = 3
SYNTHETIC_DELAY_ANNOTATION = "pod-ready.stage.kwok.x-k8s.io/delay"
SYNTHETIC_JITTER_ANNOTATION = "pod-ready.stage.kwok.x-k8s.io/jitter-delay"
SYNTHETIC_BINS = (
    ("b0", 100, 300, 5000),
    ("b1", 300, 600, 2500),
    ("b2", 600, 1000, 1500),
    ("b3", 1000, 3000, 900),
    ("b4", 3000, 5000, 100),
)


class CalibrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    suffix = "".join(random.SystemRandom().choices(string.ascii_lowercase + string.digits, k=6))
    return f"f0-{stamp}-{suffix}"


def percentile(values: Iterable[float], q: float) -> float:
    data = sorted(float(v) for v in values)
    if not data:
        raise CalibrationError("cannot calculate a percentile from zero samples")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"invalid quantile {q}")
    if len(data) == 1:
        return data[0]
    pos = q * (len(data) - 1)
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    fraction = pos - low
    return data[low] * (1.0 - fraction) + data[high] * fraction


def calculate_profile(rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    included = [row for row in rows if not row["warmup"]]
    if not included:
        raise CalibrationError("no post-warmup samples were collected")

    expected = args.sessions * (args.waves - args.warmup_waves) * EXPECTED_NODE_COUNT * args.pods_per_node
    if len(included) != expected:
        raise CalibrationError(f"post-warmup sample count is {len(included)}, expected {expected}")

    values = [float(row["d_real_ms"]) for row in included]
    quantile_defs = [
        ("p0_1", 0.001),
        ("p50", 0.50),
        ("p75", 0.75),
        ("p90", 0.90),
        ("p99", 0.99),
        ("p99_9", 0.999),
    ]
    qv = {name: percentile(values, q) for name, q in quantile_defs}
    truncated = [v for v in values if qv["p0_1"] <= v <= qv["p99_9"]]

    node_values: dict[str, list[float]] = {}
    session_values: dict[int, list[float]] = {}
    for row in included:
        node_values.setdefault(str(row["node"]), []).append(float(row["d_real_ms"]))
        session_values.setdefault(int(row["session"]), []).append(float(row["d_real_ms"]))
    node_medians = {node: statistics.median(v) for node, v in sorted(node_values.items())}
    session_medians = {str(session): statistics.median(v) for session, v in sorted(session_values.items())}
    smallest_node_median = min(node_medians.values())
    largest_node_median = max(node_medians.values())
    node_median_spread = (
        (largest_node_median - smallest_node_median) / smallest_node_median
        if smallest_node_median > 0
        else 0.0
    )

    # KWOK v0.7.0 chooses uniformly between durationMilliseconds and
    # jitterDurationMilliseconds.  The latter is the upper endpoint, not a
    # delta.  This is verified in pkg/utils/lifecycle/lifecycle.go.
    bin_defs = [
        ("b0", "p0_1", "p50", 0.50, 5000),
        ("b1", "p50", "p75", 0.25, 2500),
        ("b2", "p75", "p90", 0.15, 1500),
        ("b3", "p90", "p99", 0.09, 900),
        ("b4", "p99", "p99_9", 0.01, 100),
    ]
    bins = []
    for bin_id, lower_name, upper_name, probability, weight in bin_defs:
        lower = max(0, int(round(qv[lower_name])))
        upper = max(lower, int(round(qv[upper_name])))
        bins.append(
            {
                "id": bin_id,
                "lower_quantile": lower_name,
                "upper_quantile": upper_name,
                "lower_ms": qv[lower_name],
                "upper_ms": qv[upper_name],
                "probability": probability,
                "weight": weight,
                "kwok_delay": {
                    "durationMilliseconds": lower,
                    "jitterDurationMilliseconds": upper,
                },
            }
        )

    result = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "run_id": args.run_id,
        "measurement": "d_real = t_ready_observed - t_assigned_observed",
        "clock": "single watcher time.monotonic_ns",
        "kwok_version": "v0.7.0",
        "kwok_jitter_semantics": (
            "uniform [durationMilliseconds, jitterDurationMilliseconds); "
            "jitterDurationMilliseconds is the upper endpoint"
        ),
        "sample_counts": {
            "raw": len(rows),
            "warmup_excluded": len(rows) - len(included),
            "included": len(included),
            "truncated_distribution_count": len(truncated),
            "below_p0_1": sum(v < qv["p0_1"] for v in values),
            "above_p99_9": sum(v > qv["p99_9"] for v in values),
        },
        "quantiles_ms": qv,
        "node_medians_ms": node_medians,
        "node_median_relative_spread": node_median_spread,
        "equal_node_weighting_required": node_median_spread > 0.20,
        "session_medians_ms": session_medians,
        "bins": bins,
    }
    synthetic_rows = [row for row in included if row.get("target_delay_ms") not in (None, "")]
    if synthetic_rows:
        targets = [float(row["target_delay_ms"]) for row in synthetic_rows]
        errors = [float(row["d_real_ms"]) - float(row["target_delay_ms"]) for row in synthetic_rows]
        result["synthetic_input"] = {
            "manual": True,
            "seed": args.synthetic_seed,
            "bins": [
                {"id": bin_id, "lower_ms": lower, "upper_ms": upper, "weight": weight}
                for bin_id, lower, upper, weight in SYNTHETIC_BINS
            ],
            "bucket_counts": {
                bin_id: sum(row.get("synthetic_bin") == bin_id for row in synthetic_rows)
                for bin_id, _, _, _ in SYNTHETIC_BINS
            },
            "target_quantiles_ms": {
                name: percentile(targets, q) for name, q in quantile_defs
            },
            "observed_minus_target_ms": {
                "median": statistics.median(errors),
                "p90": percentile(errors, 0.90),
                "p99": percentile(errors, 0.99),
                "min": min(errors),
                "max": max(errors),
            },
        }
    return result


class StreamingJSONDecoder:
    def __init__(self) -> None:
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def feed(self, chunk: str) -> list[Any]:
        self.buffer += chunk
        values: list[Any] = []
        while True:
            stripped = self.buffer.lstrip()
            if not stripped:
                self.buffer = ""
                break
            try:
                value, end = self.decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                self.buffer = stripped
                break
            values.append(value)
            self.buffer = stripped[end:]
        return values


@dataclass
class PodObservation:
    pod_name: str
    node: str
    session: int
    wave: int
    warmup: bool
    uid: str = ""
    assigned_monotonic_ns: int | None = None
    assigned_unix_ns: int | None = None
    ready_monotonic_ns: int | None = None
    ready_unix_ns: int | None = None
    failed_monotonic_ns: int | None = None
    phase: str = ""
    synthetic_bin: str = ""
    target_delay_ms: int | None = None


@dataclass
class PodWatcher:
    kubectl_prefix: list[str]
    namespace: str
    run_id: str
    observations: dict[str, PodObservation] = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    reader_error: str = ""

    def register(self, observations: list[PodObservation]) -> None:
        with self.condition:
            for observation in observations:
                self.observations[observation.pod_name] = observation

    def start(self) -> None:
        command = self.kubectl_prefix + [
            "get",
            "pods",
            "-n",
            self.namespace,
            "-l",
            f"{RUN_LABEL}={self.run_id}",
            "--watch",
            "--output-watch-events",
            "-o",
            "json",
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
        )
        self.thread = threading.Thread(target=self._read, name="f0-pod-watcher", daemon=True)
        self.thread.start()
        time.sleep(1.0)
        if self.process.poll() is not None:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise CalibrationError(f"pod watcher exited during startup: {stderr.strip()}")

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        decoder = StreamingJSONDecoder()
        utf8_decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while True:
                # TextIOWrapper.read(size) may wait for all `size` characters,
                # leaving the final watch event buffered indefinitely.  read1()
                # returns the bytes currently available from kubectl instead.
                raw_stream = self.process.stdout.buffer
                read_available = getattr(raw_stream, "read1", raw_stream.read)
                raw = read_available(4096)
                if not raw:
                    break
                chunk = utf8_decoder.decode(raw)
                for event in decoder.feed(chunk):
                    self._observe_event(event)
            tail = utf8_decoder.decode(b"", final=True)
            for event in decoder.feed(tail):
                self._observe_event(event)
        except Exception as exc:  # surfaced to the main thread by wait_for_wave
            self.reader_error = repr(exc)
        finally:
            with self.condition:
                self.condition.notify_all()

    def _observe_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        obj = event.get("object", event)
        if not isinstance(obj, dict) or obj.get("kind") != "Pod":
            return
        metadata = obj.get("metadata") or {}
        name = metadata.get("name")
        if not name:
            return
        now_monotonic = time.monotonic_ns()
        now_unix = time.time_ns()
        with self.condition:
            observation = self.observations.get(name)
            if observation is None:
                return
            observation.uid = metadata.get("uid", observation.uid)
            spec = obj.get("spec") or {}
            status = obj.get("status") or {}
            node_name = spec.get("nodeName")
            if node_name and observation.assigned_monotonic_ns is None:
                observation.assigned_monotonic_ns = now_monotonic
                observation.assigned_unix_ns = now_unix
            observation.phase = status.get("phase", observation.phase)
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions") or []
            )
            if ready and observation.ready_monotonic_ns is None:
                observation.ready_monotonic_ns = now_monotonic
                observation.ready_unix_ns = now_unix
            if observation.phase == "Failed" and observation.failed_monotonic_ns is None:
                observation.failed_monotonic_ns = now_monotonic
            self.condition.notify_all()

    def wait_for_wave(self, names: list[str], timeout_seconds: int) -> list[PodObservation]:
        deadline = time.monotonic() + timeout_seconds
        with self.condition:
            while True:
                if self.reader_error:
                    raise CalibrationError(f"pod watcher failed: {self.reader_error}")
                observations = [self.observations[name] for name in names]
                failed = [obs.pod_name for obs in observations if obs.failed_monotonic_ns is not None]
                if failed:
                    raise CalibrationError(f"Pods entered Failed phase: {', '.join(failed[:10])}")
                if all(obs.ready_monotonic_ns is not None for obs in observations):
                    return observations
                if self.process is not None and self.process.poll() is not None:
                    stderr = self.process.stderr.read() if self.process.stderr else ""
                    raise CalibrationError(f"pod watcher exited unexpectedly: {stderr.strip()}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = [obs.pod_name for obs in observations if obs.ready_monotonic_ns is None]
                    raise CalibrationError(
                        f"wave timed out with {len(pending)} Pods not Ready; examples: {pending[:10]}"
                    )
                self.condition.wait(timeout=min(1.0, remaining))

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.thread is not None:
            self.thread.join(timeout=5)


class CalibrationRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.kubectl_prefix = ["kubectl"]
        if args.kubeconfig:
            self.kubectl_prefix += ["--kubeconfig", args.kubeconfig]
        if args.context:
            self.kubectl_prefix += ["--context", args.context]
        self.nodes: list[str] = []
        self.node_objects: dict[str, dict[str, Any]] = {}
        self.namespace = f"parasched-{args.run_id}"[:63].rstrip("-")
        self.output_dir = Path(args.output_dir).expanduser().resolve() / args.run_id
        self.namespace_created = False
        self.watcher: PodWatcher | None = None
        self.rows: list[dict[str, Any]] = []
        self.cluster_info: dict[str, Any] = {}
        self.synthetic_rng = random.Random(args.synthetic_seed)

    def kubectl(
        self,
        command: list[str],
        *,
        input_object: dict[str, Any] | None = None,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        encoded = json.dumps(input_object) if input_object is not None else None
        result = subprocess.run(
            self.kubectl_prefix + command,
            input=encoded,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise CalibrationError(
                f"kubectl {' '.join(command)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result

    @staticmethod
    def _condition(node: dict[str, Any], condition_type: str) -> str:
        for condition in (node.get("status") or {}).get("conditions") or []:
            if condition.get("type") == condition_type:
                return str(condition.get("status", "Unknown"))
        return "Unknown"

    @staticmethod
    def _is_kwok(node: dict[str, Any]) -> bool:
        metadata = node.get("metadata") or {}
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        return (
            labels.get("type") == "kwok"
            or "kwok.x-k8s.io/node" in labels
            or "kwok.x-k8s.io/node" in annotations
        )

    @staticmethod
    def _is_control_plane(node: dict[str, Any]) -> bool:
        labels = (node.get("metadata") or {}).get("labels") or {}
        return (
            "node-role.kubernetes.io/control-plane" in labels
            or "node-role.kubernetes.io/master" in labels
        )

    def _node_is_candidate(self, node: dict[str, Any]) -> bool:
        spec = node.get("spec") or {}
        return (
            self._condition(node, "Ready") == "True"
            and not spec.get("unschedulable", False)
            and not self._is_kwok(node)
            and not self._is_control_plane(node)
        )

    def _node_is_synthetic_candidate(self, node: dict[str, Any]) -> bool:
        spec = node.get("spec") or {}
        return (
            self._condition(node, "Ready") == "True"
            and not spec.get("unschedulable", False)
            and self._is_kwok(node)
            and not self._is_control_plane(node)
        )

    def _image_present(self, node: dict[str, Any]) -> bool:
        wanted = self.args.image
        names = {
            image_name
            for image in (node.get("status") or {}).get("images") or []
            for image_name in image.get("names") or []
        }
        return wanted in names or any(name.endswith("/" + wanted) for name in names)

    def _prometheus_preflight(self) -> dict[str, Any]:
        if not self.args.prometheus_url:
            return {"checked": False}
        base = self.args.prometheus_url.rstrip("/")
        result: dict[str, Any] = {"checked": True, "url": base}
        try:
            with urllib.request.urlopen(base + "/-/ready", timeout=3) as response:
                result["ready"] = response.status == 200
                result["ready_body"] = response.read().decode("utf-8", "replace").strip()
            with urllib.request.urlopen(base + "/api/v1/targets?state=active", timeout=5) as response:
                payload = json.load(response)
            targets = ((payload.get("data") or {}).get("activeTargets") or [])
            result["targets_active"] = len(targets)
            result["targets_up"] = sum(target.get("health") == "up" for target in targets)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            result["ready"] = False
            result["error"] = str(exc)
        return result

    def preflight(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if shutil.which("kubectl") is None:
            raise CalibrationError("kubectl is not installed")
        if self.args.warmup_waves >= self.args.waves:
            errors.append("--warmup-waves must be smaller than --waves")
        if self.args.sessions < 1 or self.args.waves < 1 or self.args.pods_per_node < 1:
            errors.append("sessions, waves, and pods-per-node must all be positive")

        version_result = self.kubectl(["version", "-o", "json"])
        self.cluster_info["version"] = json.loads(version_result.stdout)
        context_result = self.kubectl(["config", "current-context"])
        self.cluster_info["context"] = context_result.stdout.strip()
        readyz_result = self.kubectl(["get", "--raw=/readyz"], check=False)
        self.cluster_info["readyz"] = readyz_result.stdout.strip()
        if readyz_result.returncode != 0 or readyz_result.stdout.strip() != "ok":
            errors.append("Kubernetes API /readyz is not ok")

        node_result = self.kubectl(["get", "nodes", "-o", "json"])
        node_items = json.loads(node_result.stdout).get("items") or []
        self.node_objects = {
            str((node.get("metadata") or {}).get("name")): node
            for node in node_items
            if (node.get("metadata") or {}).get("name")
        }
        if self.args.synthetic_kwok:
            candidates = sorted(
                name for name, node in self.node_objects.items() if self._node_is_synthetic_candidate(node)
            )
            candidate_description = "eligible KWOK nodes"
        else:
            candidates = sorted(name for name, node in self.node_objects.items() if self._node_is_candidate(node))
            candidate_description = "eligible real worker nodes"
        requested = [name.strip() for name in self.args.nodes.split(",") if name.strip()]
        if requested:
            if len(requested) != EXPECTED_NODE_COUNT or len(set(requested)) != EXPECTED_NODE_COUNT:
                errors.append(f"--nodes must contain exactly {EXPECTED_NODE_COUNT} unique node names")
            self.nodes = requested
        else:
            self.nodes = candidates[:EXPECTED_NODE_COUNT]
            if len(candidates) != EXPECTED_NODE_COUNT:
                errors.append(
                    f"auto-discovery found {len(candidates)} {candidate_description}; "
                    f"exactly {EXPECTED_NODE_COUNT} are required (eligible={candidates})"
                )

        for node_name in self.nodes:
            node = self.node_objects.get(node_name)
            if node is None:
                errors.append(f"requested node {node_name!r} does not exist")
                continue
            if self.args.synthetic_kwok and not self._is_kwok(node):
                errors.append(f"node {node_name!r} is not a KWOK node")
            if not self.args.synthetic_kwok and self._is_kwok(node):
                errors.append(f"node {node_name!r} is a KWOK node")
            if self._is_control_plane(node):
                errors.append(f"node {node_name!r} is a control-plane node")
            if (node.get("spec") or {}).get("unschedulable", False):
                errors.append(f"node {node_name!r} is unschedulable")
            if self._condition(node, "Ready") != "True":
                errors.append(f"node {node_name!r} is not Ready")
            for pressure in ("MemoryPressure", "DiskPressure", "PIDPressure"):
                if self._condition(node, pressure) != "False":
                    errors.append(f"node {node_name!r} reports {pressure}={self._condition(node, pressure)}")
            if not self.args.synthetic_kwok and not self.args.skip_image_check and not self._image_present(node):
                errors.append(
                    f"warm image {self.args.image!r} is not reported on node {node_name!r}; "
                    "pre-pull it before calibration or verify manually and pass --skip-image-check"
                )

        prometheus = self._prometheus_preflight()
        if prometheus.get("checked") and not prometheus.get("ready"):
            message = f"Prometheus is not ready: {prometheus.get('error') or prometheus.get('ready_body')}"
            if self.args.require_prometheus:
                errors.append(message)
            else:
                warnings.append(message)
        if prometheus.get("checked") and prometheus.get("targets_active") != prometheus.get("targets_up"):
            warnings.append(
                f"Prometheus has {prometheus.get('targets_up')}/{prometheus.get('targets_active')} active targets up"
            )

        return {
            "ok": not errors,
            "checked_at": utc_now(),
            "context": self.cluster_info.get("context"),
            "api_readyz": self.cluster_info.get("readyz"),
            "all_node_count": len(node_items),
            "node_mode": "synthetic-kwok" if self.args.synthetic_kwok else "real-worker",
            "eligible_nodes": candidates,
            "selected_nodes": self.nodes,
            "image": self.args.image,
            "image_check_skipped": self.args.skip_image_check or self.args.synthetic_kwok,
            "prometheus": prometheus,
            "errors": errors,
            "warnings": warnings,
        }

    def _write_json(self, path: Path, value: Any) -> None:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _git_value(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), *arguments],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _metadata(self, preflight: dict[str, Any]) -> dict[str, Any]:
        script_bytes = Path(__file__).read_bytes()
        return {
            "schema_version": 1,
            "run_id": self.args.run_id,
            "started_at": utc_now(),
            "namespace": self.namespace,
            "parameters": {
                key: value
                for key, value in vars(self.args).items()
                if key not in {"self_test", "preflight_only"}
            },
            "expected_samples": {
                "raw": self.args.sessions * self.args.waves * EXPECTED_NODE_COUNT * self.args.pods_per_node,
                "included": self.args.sessions
                * (self.args.waves - self.args.warmup_waves)
                * EXPECTED_NODE_COUNT
                * self.args.pods_per_node,
            },
            "git": {
                "commit": self._git_value("rev-parse", "HEAD"),
                "branch": self._git_value("branch", "--show-current"),
                "status_porcelain": self._git_value("status", "--porcelain"),
            },
            "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "cluster": self.cluster_info,
            "preflight": preflight,
            "synthetic_profile": (
                {
                    "manual": True,
                    "seed": self.args.synthetic_seed,
                    "bins": [
                        {"id": bin_id, "lower_ms": lower, "upper_ms": upper, "weight": weight}
                        for bin_id, lower, upper, weight in SYNTHETIC_BINS
                    ],
                }
                if self.args.synthetic_kwok
                else None
            ),
        }

    def _create_namespace(self) -> None:
        namespace_object = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": self.namespace,
                "labels": {EXPERIMENT_LABEL: EXPERIMENT_VALUE, RUN_LABEL: self.args.run_id},
            },
        }
        self.kubectl(["create", "-f", "-"], input_object=namespace_object)
        self.namespace_created = True

    def _pod_objects(self, session: int, wave: int) -> tuple[list[PodObservation], dict[str, Any]]:
        observations: list[PodObservation] = []
        items: list[dict[str, Any]] = []
        warmup = wave <= self.args.warmup_waves
        for node_index, node_name in enumerate(self.nodes):
            safe_node = "".join(ch if ch.isalnum() else "-" for ch in node_name.lower()).strip("-")[:16]
            for pod_index in range(1, self.args.pods_per_node + 1):
                pod_name = f"f0-s{session:02d}-w{wave:03d}-n{node_index}-{safe_node}-p{pod_index:02d}"[:63]
                synthetic_bin = ""
                target_delay_ms = None
                annotations: dict[str, str] = {}
                if self.args.synthetic_kwok:
                    selected = self.synthetic_rng.choices(
                        SYNTHETIC_BINS,
                        weights=[value[3] for value in SYNTHETIC_BINS],
                        k=1,
                    )[0]
                    synthetic_bin, lower_ms, upper_ms, _ = selected
                    target_delay_ms = self.synthetic_rng.randint(lower_ms, upper_ms)
                    annotations = {
                        SYNTHETIC_DELAY_ANNOTATION: f"{target_delay_ms}ms",
                        SYNTHETIC_JITTER_ANNOTATION: f"{target_delay_ms}ms",
                    }
                observations.append(
                    PodObservation(
                        pod_name=pod_name,
                        node=node_name,
                        session=session,
                        wave=wave,
                        warmup=warmup,
                        synthetic_bin=synthetic_bin,
                        target_delay_ms=target_delay_ms,
                    )
                )
                items.append(
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {
                            "name": pod_name,
                            "namespace": self.namespace,
                            "labels": {
                                EXPERIMENT_LABEL: EXPERIMENT_VALUE,
                                RUN_LABEL: self.args.run_id,
                                SESSION_LABEL: str(session),
                                WAVE_LABEL: str(wave),
                                "parasched.io/dataplane-profile": "calibrated-v1",
                            },
                            "annotations": annotations,
                        },
                        "spec": {
                            "nodeName": node_name,
                            "restartPolicy": "Never",
                            "terminationGracePeriodSeconds": 0,
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "containers": [
                                {
                                    "name": "pause",
                                    "image": self.args.image,
                                    "imagePullPolicy": "Never",
                                    "resources": {
                                        "requests": {"cpu": "1m", "memory": "1Mi"},
                                        "limits": {"cpu": "10m", "memory": "8Mi"},
                                    },
                                }
                            ],
                        },
                    }
                )
        return observations, {"apiVersion": "v1", "kind": "List", "items": items}

    def _delete_wave(self, session: int, wave: int) -> None:
        selector = f"{RUN_LABEL}={self.args.run_id},{SESSION_LABEL}={session},{WAVE_LABEL}={wave}"
        self.kubectl(
            [
                "delete",
                "pods",
                "-n",
                self.namespace,
                "-l",
                selector,
                "--wait=true",
                f"--timeout={self.args.delete_timeout_seconds}s",
                "--ignore-not-found=true",
            ],
            timeout=self.args.delete_timeout_seconds + 30,
        )

    @staticmethod
    def _rows_from_observations(observations: list[PodObservation], run_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for observation in observations:
            if observation.assigned_monotonic_ns is None or observation.ready_monotonic_ns is None:
                raise CalibrationError(f"incomplete observation for Pod {observation.pod_name}")
            rows.append(
                {
                    "run_id": run_id,
                    "session": observation.session,
                    "wave": observation.wave,
                    "warmup": observation.warmup,
                    "pod_name": observation.pod_name,
                    "pod_uid": observation.uid,
                    "node": observation.node,
                    "t_assigned_monotonic_ns": observation.assigned_monotonic_ns,
                    "t_ready_monotonic_ns": observation.ready_monotonic_ns,
                    "t_assigned_observed_unix_ns": observation.assigned_unix_ns,
                    "t_ready_observed_unix_ns": observation.ready_unix_ns,
                    "d_real_ms": (observation.ready_monotonic_ns - observation.assigned_monotonic_ns) / 1_000_000,
                    "phase": observation.phase,
                    "synthetic_bin": observation.synthetic_bin,
                    "target_delay_ms": observation.target_delay_ms,
                }
            )
        return rows

    def _write_summary(self, profile: dict[str, Any]) -> None:
        q = profile["quantiles_ms"]
        counts = profile["sample_counts"]
        lines = [
            f"F0 calibration run: {self.args.run_id}",
            f"Nodes: {', '.join(self.nodes)}",
            f"Raw samples: {counts['raw']}",
            f"Included samples: {counts['included']}",
            f"Warmup excluded: {counts['warmup_excluded']}",
            f"P0.1/P50/P75/P90/P99/P99.9 ms: "
            f"{q['p0_1']:.3f} / {q['p50']:.3f} / {q['p75']:.3f} / "
            f"{q['p90']:.3f} / {q['p99']:.3f} / {q['p99_9']:.3f}",
            f"Node median relative spread: {profile['node_median_relative_spread']:.2%}",
            f"Equal-node weighting required: {profile['equal_node_weighting_required']}",
            "KWOK jitter semantics: upper endpoint (verified for v0.7.0)",
        ]
        if "synthetic_input" in profile:
            synthetic = profile["synthetic_input"]
            target = synthetic["target_quantiles_ms"]
            overhead = synthetic["observed_minus_target_ms"]
            lines.extend(
                [
                    "Calibration source: manual synthetic KWOK input (not real workers)",
                    f"Synthetic seed: {synthetic['seed']}",
                    f"Target P50/P90/P99 ms: {target['p50']:.3f} / {target['p90']:.3f} / {target['p99']:.3f}",
                    f"Observed-target median/P90/P99 ms: "
                    f"{overhead['median']:.3f} / {overhead['p90']:.3f} / {overhead['p99']:.3f}",
                ]
            )
        (self.output_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, preflight: dict[str, Any]) -> Path:
        if self.output_dir.exists():
            raise CalibrationError(f"output directory already exists: {self.output_dir}")
        self.output_dir.mkdir(parents=True)
        metadata = self._metadata(preflight)
        self._write_json(self.output_dir / "metadata.json", metadata)

        raw_path = self.output_dir / "raw.csv"
        fieldnames = [
            "run_id",
            "session",
            "wave",
            "warmup",
            "pod_name",
            "pod_uid",
            "node",
            "t_assigned_monotonic_ns",
            "t_ready_monotonic_ns",
            "t_assigned_observed_unix_ns",
            "t_ready_observed_unix_ns",
            "d_real_ms",
            "phase",
            "synthetic_bin",
            "target_delay_ms",
        ]

        success = False
        try:
            self._create_namespace()
            self.watcher = PodWatcher(self.kubectl_prefix, self.namespace, self.args.run_id)
            self.watcher.start()
            with raw_path.open("w", newline="", encoding="utf-8") as raw_file:
                writer = csv.DictWriter(raw_file, fieldnames=fieldnames)
                writer.writeheader()
                raw_file.flush()
                for session in range(1, self.args.sessions + 1):
                    for wave in range(1, self.args.waves + 1):
                        print(
                            f"[{utc_now()}] session {session}/{self.args.sessions}, "
                            f"wave {wave}/{self.args.waves}: creating "
                            f"{EXPECTED_NODE_COUNT * self.args.pods_per_node} Pods",
                            flush=True,
                        )
                        observations, pod_list = self._pod_objects(session, wave)
                        assert self.watcher is not None
                        self.watcher.register(observations)
                        self.kubectl(["create", "-f", "-"], input_object=pod_list)
                        completed = self.watcher.wait_for_wave(
                            [observation.pod_name for observation in observations],
                            self.args.wave_timeout_seconds,
                        )
                        wave_rows = self._rows_from_observations(completed, self.args.run_id)
                        self.rows.extend(wave_rows)
                        writer.writerows(wave_rows)
                        raw_file.flush()
                        os.fsync(raw_file.fileno())
                        wave_values = [row["d_real_ms"] for row in wave_rows]
                        print(
                            f"  Ready: median={statistics.median(wave_values):.3f} ms, "
                            f"p90={percentile(wave_values, 0.90):.3f} ms; deleting wave",
                            flush=True,
                        )
                        self._delete_wave(session, wave)
                        time.sleep(self.args.quiet_seconds)
                    if session < self.args.sessions and self.args.session_gap_seconds > 0:
                        print(
                            f"[{utc_now()}] session {session} complete; "
                            f"waiting {self.args.session_gap_seconds}s before the next session",
                            flush=True,
                        )
                        time.sleep(self.args.session_gap_seconds)

            profile = calculate_profile(self.rows, self.args)
            self._write_json(self.output_dir / "dataplane-profile-v1.json", profile)
            self._write_summary(profile)
            metadata["completed_at"] = utc_now()
            metadata["status"] = "complete"
            self._write_json(self.output_dir / "metadata.json", metadata)
            success = True
            return self.output_dir
        except BaseException as exc:
            metadata["failed_at"] = utc_now()
            metadata["status"] = "failed"
            metadata["error"] = repr(exc)
            self._write_json(self.output_dir / "metadata.json", metadata)
            raise
        finally:
            if self.watcher is not None:
                self.watcher.stop()
            keep = (not success) and self.args.keep_namespace_on_failure
            if self.namespace_created and not keep:
                cleanup = self.kubectl(
                    [
                        "delete",
                        "namespace",
                        self.namespace,
                        "--wait=true",
                        f"--timeout={self.args.delete_timeout_seconds}s",
                        "--ignore-not-found=true",
                    ],
                    check=False,
                    timeout=self.args.delete_timeout_seconds + 30,
                )
                if cleanup.returncode != 0:
                    print(f"WARNING: namespace cleanup failed: {cleanup.stderr.strip()}", file=sys.stderr)
            elif keep:
                print(f"Keeping namespace {self.namespace} because the run failed", file=sys.stderr)


def self_test() -> None:
    assert percentile([0, 10], 0.5) == 5
    decoder = StreamingJSONDecoder()
    assert decoder.feed('{"a":1}{"b"') == [{"a": 1}]
    assert decoder.feed(':2}') == [{"b": 2}]
    fake_args = argparse.Namespace(
        sessions=1,
        waves=2,
        warmup_waves=1,
        pods_per_node=1,
        run_id="self-test",
    )
    rows = []
    for index, node in enumerate(("c", "d", "e"), start=1):
        rows.append({"warmup": True, "d_real_ms": index, "node": node, "session": 1})
        rows.append({"warmup": False, "d_real_ms": index * 100, "node": node, "session": 1})
    profile = calculate_profile(rows, fake_args)
    assert profile["sample_counts"]["included"] == 3
    for bin_value in profile["bins"]:
        delay = bin_value["kwok_delay"]
        assert delay["jitterDurationMilliseconds"] >= delay["durationMilliseconds"]
        assert delay["jitterDurationMilliseconds"] == int(round(bin_value["upper_ms"]))
    print("SELF_TEST_OK")


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Calibrate Assigned-to-Ready latency on three real Kubernetes workers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--nodes", default="", help="comma-separated real worker names; auto-discover if empty")
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--waves", type=int, default=40)
    parser.add_argument("--warmup-waves", type=int, default=5)
    parser.add_argument("--pods-per-node", type=int, default=10)
    parser.add_argument("--quiet-seconds", type=int, default=10)
    parser.add_argument("--session-gap-seconds", type=int, default=1800)
    parser.add_argument("--wave-timeout-seconds", type=int, default=120)
    parser.add_argument("--delete-timeout-seconds", type=int, default=120)
    parser.add_argument("--image", default="registry.k8s.io/pause:3.10")
    parser.add_argument("--skip-image-check", action="store_true")
    parser.add_argument(
        "--synthetic-kwok",
        action="store_true",
        help="explicitly measure manually injected delay on three KWOK nodes (not real calibration)",
    )
    parser.add_argument("--synthetic-seed", type=int, default=20260906)
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", ""))
    parser.add_argument("--context", default="")
    parser.add_argument("--prometheus-url", default="", help="optional Prometheus base URL")
    parser.add_argument("--require-prometheus", action="store_true")
    parser.add_argument("--output-dir", default=str(project_root / "experiments" / "results" / "F0"))
    parser.add_argument("--run-id", default=compact_run_id())
    parser.add_argument("--keep-namespace-on-failure", action="store_true")
    parser.add_argument("--preflight-only", action="store_true", help="check prerequisites; create no resources")
    parser.add_argument("--self-test", action="store_true", help="run offline unit checks")
    parser.add_argument(
        "--from-anchors",
        default="",
        metavar="P50,P90,P99",
        help="derive the profile directly from three upstream-CI percentile anchors in ms; "
             "creates no Kubernetes resources (see experiment-design.md 10.3)",
    )
    parser.add_argument("--anchor-label", default="", help="e.g. 'gce-5000Nodes burst'")
    parser.add_argument("--anchor-source", default="", help="e.g. 'perf-dash.k8s.io snapshot 2026-09-05, last 20 builds'")
    return parser


# Three upstream CI percentile anchors -> four weighted buckets; see
# kwok-setup/stages/calibration/README.md for the derivation.
# The probability mass is self-consistent with the percentile definitions:
# P(X<=P50)=.50, P(P50<X<=P90)=.40, P(P90<X<=P99)=.09, P(X>P99)=.01
ANCHOR_BIN_DEFS = (
    ("b0", "p50_lower", "p50", 0.50, 5000),
    ("b1", "p50", "p90", 0.40, 4000),
    ("b2", "p90", "p99", 0.09, 900),
    ("b3", "p99", "p99_upper", 0.01, 100),
)


def build_anchored_profile(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Derive the data-plane profile from three upstream-CI percentile anchors.

    Upstream CI publishes only P50/P90/P99, so the outer edges use the bounded
    approximations 0.7*P50 and 2*P99 fixed by the design doc.  No cluster access
    and no local sampling: re-measuring on this testbed would only capture KWOK's
    own transition latency, which is circular.
    """
    try:
        parts = [float(value) for value in args.from_anchors.split(",")]
    except ValueError as exc:
        raise CalibrationError(f"--from-anchors expects three numbers in ms: {exc}") from exc
    if len(parts) != 3:
        raise CalibrationError(f"--from-anchors expects P50,P90,P99 (3 values), got {len(parts)}")
    p50, p90, p99 = parts
    if not 0 < p50 <= p90 <= p99:
        raise CalibrationError(f"anchors must satisfy 0 < P50 <= P90 <= P99, got {p50}/{p90}/{p99}")

    edges = {"p50_lower": 0.7 * p50, "p50": p50, "p90": p90, "p99": p99, "p99_upper": 2.0 * p99}
    bins = []
    for bin_id, lower_name, upper_name, probability, weight in ANCHOR_BIN_DEFS:
        lower = max(0, int(round(edges[lower_name])))
        upper = max(lower, int(round(edges[upper_name])))
        bins.append(
            {
                "id": bin_id,
                "lower_quantile": lower_name,
                "upper_quantile": upper_name,
                "lower_ms": edges[lower_name],
                "upper_ms": edges[upper_name],
                "probability": probability,
                "weight": weight,
                "kwok_delay": {
                    "durationMilliseconds": lower,
                    "jitterDurationMilliseconds": upper,
                },
            }
        )
    total_weight = sum(int(item["weight"]) for item in bins)
    if total_weight != 10000:
        raise CalibrationError(f"anchor bin weights sum to {total_weight}, expected 10000")

    profile = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "run_id": args.run_id,
        "derivation": "upstream-CI anchors -> 4 weighted buckets (experiment-design.md 10.3)",
        "measurement": "none; the percentiles are upstream CI observations on real GCE/AWS nodes",
        "anchor": {
            "p50_ms": p50,
            "p90_ms": p90,
            "p99_ms": p99,
            "label": args.anchor_label,
            "source": args.anchor_source,
            "outer_edges": "lower = 0.7*P50, upper = 2*P99",
        },
        "kwok_version": "v0.7.0",
        "kwok_jitter_semantics": (
            "uniform [durationMilliseconds, jitterDurationMilliseconds); "
            "jitterDurationMilliseconds is the upper endpoint"
        ),
        "quantiles_ms": {"p50": p50, "p90": p90, "p99": p99},
        "bins": bins,
    }
    lines = [
        f"F0 anchored profile: {args.run_id}",
        f"Anchor label: {args.anchor_label or '(unset)'}",
        f"Anchor source: {args.anchor_source or '(unset)'}",
        f"Anchors P50/P90/P99 ms: {p50:.3f} / {p90:.3f} / {p99:.3f}",
        "Derivation: experiment-design.md 10.3 (lower edge 0.7*P50, upper edge 2*P99)",
        "Calibration source: upstream CI measurements on real nodes; no local sampling",
        "Buckets (ms, weight):",
    ]
    for item in bins:
        delay = item["kwok_delay"]
        lines.append(
            f"  {item['id']}: [{delay['durationMilliseconds']}, "
            f"{delay['jitterDurationMilliseconds']}] weight={item['weight']}"
        )
    return profile, lines


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.from_anchors:
        try:
            profile, summary_lines = build_anchored_profile(args)
        except CalibrationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        output_dir = Path(args.output_dir).expanduser().resolve() / args.run_id
        if output_dir.exists():
            print(f"ERROR: output directory already exists: {output_dir}", file=sys.stderr)
            return 1
        output_dir.mkdir(parents=True)
        (output_dir / "dataplane-profile-v1.json").write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (output_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        print("\n".join(summary_lines))
        print(f"CALIBRATION_COMPLETE output={output_dir}")
        return 0
    runner = CalibrationRunner(args)
    try:
        preflight = runner.preflight()
        print(json.dumps(preflight, indent=2, ensure_ascii=False))
        if not preflight["ok"]:
            print("PREFLIGHT_FAILED: no Kubernetes resources were created", file=sys.stderr)
            return 2
        if args.preflight_only:
            print("PREFLIGHT_OK: no Kubernetes resources were created")
            return 0
        output = runner.run(preflight)
        print(f"CALIBRATION_COMPLETE output={output}")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; cleanup attempted", file=sys.stderr)
        return 130
    except (CalibrationError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
