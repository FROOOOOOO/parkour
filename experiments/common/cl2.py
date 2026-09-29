#!/usr/bin/env python3
"""Read ClusterLoader2 logs. This is the one CL2 log parser in the repository.

ClusterLoader2 (CL2) logs through klog, so every line starts with a header

    I0427 02:53:04.806007  417667 simple_test_executor.go:162] <message>

holding the severity, the month and day, and the UTC time of day to the
microsecond, but not the year. Three kinds of message matter:

- step markers, `Step "[step: 03] Creating saturation pods" started`, which
  bound each phase of a test;
- pod summaries from `WaitForControlledPodsRunning`, one per namespace and
  controller, logged while the pods are being placed;
- timeouts: the same summary, logged as an error once a controller's wait
  deadline expires, which says how many of its pods were never scheduled.

The runners call this module when a trial ends, to find the saturation window
the metric collectors query; the reduction reads the same log afterwards to time
that phase. One parser means the two agree on where a phase starts and ends.

A klog line is dated by the year that puts it nearest a reference time: when
the trial started, as its runner recorded it, or the present when the log has
just been written. A test that runs over New Year is therefore dated correctly,
and reading a log years later gives the same times as reading it on the day.

Usage, as the runners call it (standard library only):

    python3 experiments/common/cl2.py step-time <cl2.log> "<step name>" started|ended

prints the first marker of the named step as whole Unix seconds, or an empty
line when the log has no such marker.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

#: (step name, event) of the markers that bound each phase. A marker matches
#: when its step name contains the given name.
SATURATION_START = ("Creating saturation pods", "started")
SATURATION_END = ("Waiting for saturation pods to be running", "ended")
LATENCY_START = ("Creating latency pods", "started")
LATENCY_END = ("Waiting for latency pods to be running", "ended")
SATURATION_DELETION = ("Deleting saturation pods", "started")

#: The saturation phase's controllers are saturation-deployment-<n>; the latency
#: phase has its own, whose pods are not part of the saturation workload.
SATURATION_CONTROLLER_PREFIX = "saturation-"

_HEADER = re.compile(
    r"^([A-Z])(\d{2})(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d+)\s+\d+\s+\S+\]\s+(.*)$"
)
_STEP = re.compile(r'Step\s+"\[step:\s*\d+\]\s+(.+?)"\s+(started|ended)')
_PODS = re.compile(
    r"namespace\(([^)]+)\), controlledBy\(([^)]+)\).*?"
    r"Pods:\s*(\d+)\s+out\s+of\s+(\d+)\s+created,\s*"
    r"(\d+)\s+running\s+\((\d+)\s+updated\),\s*"
    r"(\d+)\s+pending\s+scheduled,\s*"
    r"(\d+)\s+not\s+scheduled"
)
_NAMESPACE = re.compile(r"namespace\(([^)]+)\)")
_CONTROLLER = re.compile(r"controlledBy\(([^)]+)\)")
_RESULT = re.compile(r"Status:\s*(Success|Fail)")


@dataclass(frozen=True)
class Line:
    """One klog line."""

    severity: str  # I, W, E or F
    time: float  # Unix seconds, UTC
    message: str


@dataclass(frozen=True)
class Pods:
    """One `WaitForControlledPodsRunning` summary of a controller's pods."""

    namespace: str
    controller: str
    created: int
    expected: int  # the controller's replica count ("out of N created")
    running: int
    pending_scheduled: int  # bound to a node but not yet running
    not_scheduled: int


@dataclass(frozen=True)
class Timeout:
    """A controller whose wait deadline expired, with its last pod summary."""

    namespace: str | None
    controller: str | None
    pods: Pods | None


def is_saturation(controller: str | None) -> bool:
    """Whether a controller belongs to the saturation phase."""

    return bool(controller) and controller.startswith(SATURATION_CONTROLLER_PREFIX)


@dataclass
class Log:
    """What a test's log says: its step markers in order, its timeouts, and
    CL2's own verdict (`Success`, `Fail`, or `Unknown` when it printed none)."""

    steps: list[tuple[str, str, float]] = field(default_factory=list)
    timeouts: list[Timeout] = field(default_factory=list)
    result: str = "Unknown"

    def step_time(self, name: str, event: str) -> float | None:
        """Time of the first `event` marker of a step whose name contains `name`."""

        for step_name, step_event, when in self.steps:
            if is_step((step_name, step_event), (name, event)):
                return when
        return None


def _headers(path: str) -> Iterator[re.Match[str]]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        for text in handle:
            match = _HEADER.match(text)
            if match:
                yield match


def _time(header: re.Match[str], anchor: datetime) -> float:
    """Unix time of a klog header, in the year that puts it nearest `anchor`."""

    month = int(header.group(2))
    year = anchor.year
    if month - anchor.month > 6:
        year -= 1
    elif anchor.month - month > 6:
        year += 1
    stamp = datetime(year, month, int(header.group(3)), int(header.group(4)),
                     int(header.group(5)), int(header.group(6)),
                     int((header.group(7) + "000000")[:6]), tzinfo=timezone.utc)
    return stamp.timestamp()


def lines(path: str, reference: float) -> Iterator[Line]:
    """The klog lines of a log, dated relative to `reference` (Unix seconds).

    Lines without a klog header, such as the continuation lines of a multi-line
    message, are skipped.
    """

    anchor = datetime.fromtimestamp(reference, timezone.utc)
    for header in _headers(path):
        yield Line(header.group(1), _time(header, anchor), header.group(8))


def step(message: str) -> tuple[str, str] | None:
    """(step name, event) when a message is a step marker."""

    match = _STEP.match(message)
    return (match.group(1), match.group(2)) if match else None


def is_step(marker: tuple[str, str] | None, spec: tuple[str, str]) -> bool:
    """Whether a step marker is the one `spec` names, as in `SATURATION_START`."""

    return marker is not None and marker[1] == spec[1] and spec[0] in marker[0]


def pods(message: str) -> Pods | None:
    """The pod summary a message carries, if any."""

    match = _PODS.search(message)
    if not match:
        return None
    namespace, controller, *counts = match.groups()
    created, expected, running, _updated, pending, not_scheduled = map(int, counts)
    return Pods(namespace, controller, created, expected, running, pending, not_scheduled)


def read(path: str, reference: float) -> Log:
    """Parse a whole log; `reference` is as for `lines`."""

    anchor = datetime.fromtimestamp(reference, timezone.utc)
    log = Log()
    for header in _headers(path):
        message = header.group(8)
        marker = step(message)
        if marker:
            log.steps.append((marker[0], marker[1], _time(header, anchor)))
            continue
        if ("WaitForControlledPodsRunning" in message
                and "context deadline exceeded" in message):
            namespace = _NAMESPACE.search(message)
            controller = _CONTROLLER.search(message)
            log.timeouts.append(Timeout(namespace.group(1) if namespace else None,
                                        controller.group(1) if controller else None,
                                        pods(message)))
            continue
        result = _RESULT.match(message)
        if result:
            log.result = result.group(1)
    return log


def trial_reference(timing: dict[str, Any] | None) -> float:
    """Reference time for a recorded trial's log: the start its runner wrote to
    `timing.json`, or the present when there is none."""

    start = ((timing or {}).get("overall") or {}).get("start")
    return float(start) if start else time.time()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    step_time = commands.add_parser(
        "step-time", help="print a step marker's time as whole Unix seconds")
    step_time.add_argument("log", help="the trial's cl2.log")
    step_time.add_argument("name", help="the step name, or part of it")
    step_time.add_argument("event", choices=("started", "ended"))
    args = parser.parse_args(argv)

    when = None
    if os.path.isfile(args.log):
        when = read(args.log, time.time()).step_time(args.name, args.event)
    print("" if when is None else int(when))
    return 0


if __name__ == "__main__":
    sys.exit(main())
