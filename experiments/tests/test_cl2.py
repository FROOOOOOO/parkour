"""The CL2 log parser: klog headers, dating, and the three kinds of message."""

from datetime import datetime, timedelta, timezone

import pytest

from common import cl2
from rawdata import START, Trial, cl2_log, klog

UTC = timezone.utc


def write(tmp_path, text):
    path = tmp_path / "cl2.log"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_header_time_keeps_microseconds(tmp_path):
    path = write(tmp_path, klog(START, "hello"))
    (line,) = cl2.lines(path, START.timestamp())
    assert (line.severity, line.message) == ("I", "hello")
    assert line.time == START.timestamp()


@pytest.mark.parametrize("stamp, reference, year", [
    ("0101 00:00:05.000000", datetime(2026, 12, 31, 23, 59, tzinfo=UTC), 2027),
    ("1231 23:59:55.000000", datetime(2027, 1, 1, 0, 1, tzinfo=UTC), 2026),
    ("0427 02:53:04.000000", datetime(2030, 5, 1, tzinfo=UTC), 2030),
])
def test_year_is_the_one_nearest_the_reference(tmp_path, stamp, reference, year):
    path = write(tmp_path, f"I{stamp}    4242 test.go:1] x\n")
    (line,) = cl2.lines(path, reference.timestamp())
    assert datetime.fromtimestamp(line.time, UTC).year == year


def test_a_phase_over_new_year_has_its_true_length(tmp_path):
    trial = Trial(1, duration=30.0, start=datetime(2026, 12, 31, 23, 59, 50, tzinfo=UTC))
    path = write(tmp_path, cl2_log(trial, pods=100))
    log = cl2.read(path, trial.start.timestamp())
    start = log.step_time(*cl2.SATURATION_START)
    end = log.step_time(*cl2.SATURATION_END)
    assert end - start == pytest.approx(30.0)


def test_read_collects_steps_timeouts_and_result(tmp_path):
    complete = cl2.read(write(tmp_path, cl2_log(Trial(1, duration=12.5), pods=100)),
                        START.timestamp())
    assert complete.step_time(*cl2.SATURATION_START) == START.timestamp()
    assert complete.step_time(*cl2.SATURATION_END) == (
        START + timedelta(seconds=12.5)).timestamp()
    assert complete.step_time(*cl2.LATENCY_START) is None
    assert (complete.timeouts, complete.result) == ([], "Success")

    timed_out = cl2.read(write(tmp_path, cl2_log(Trial(1, 20.0, not_scheduled=7,
                                                         latency_timeouts=2), pods=100)),
                         START.timestamp())
    saturation, *latency = timed_out.timeouts
    assert (saturation.namespace, saturation.controller) == (
        "test-fixture-1", "saturation-deployment-0")
    assert saturation.pods == cl2.Pods("test-fixture-1", "saturation-deployment-0",
                                       created=50, expected=50, running=40,
                                       pending_scheduled=3, not_scheduled=7)
    assert [t.controller for t in latency] == ["latency-deployment-0", "latency-deployment-1"]
    assert [cl2.is_saturation(t.controller) for t in timed_out.timeouts] == [True, False, False]
    assert timed_out.step_time(*cl2.LATENCY_END) is not None
    assert timed_out.result == "Fail"


def test_step_time_takes_the_first_marker_whose_name_contains_the_query(tmp_path):
    text = "".join([
        klog(START, 'Step "[step: 01] Creating saturation pods" ended'),
        klog(START + timedelta(seconds=1), 'Step "[step: 03] Creating saturation pods" started'),
        klog(START + timedelta(seconds=2), 'Step "[step: 09] Creating saturation pods" started'),
    ])
    log = cl2.read(write(tmp_path, text), START.timestamp())
    assert log.step_time("saturation pods", "started") == START.timestamp() + 1
    assert log.step_time("latency pods", "started") is None


def test_lines_without_a_klog_header_are_skipped(tmp_path):
    text = klog(START, "SchedulingThroughput: {") + '  "perc50": 1\n}\n' + klog(START, "end")
    assert [line.message for line in cl2.lines(write(tmp_path, text), START.timestamp())] == [
        "SchedulingThroughput: {", "end"]


def test_pod_summary_while_waiting():
    message = ("WaitForControlledPodsRunning: namespace(test-a-1), "
               "controlledBy(saturation-deployment-0): Pods: 19 out of 100 created, "
               "2 running (2 updated), 3 pending scheduled, 14 not scheduled, 0 inactive, "
               "0 terminating, 0 unknown, 0 runningButNotReady ")
    assert cl2.pods(message) == cl2.Pods("test-a-1", "saturation-deployment-0", 19, 100,
                                         2, 3, 14)
    assert cl2.pods("Step \"[step: 01] x\" started") is None


def test_command_prints_whole_seconds_or_nothing(tmp_path, capsys):
    # The runners call the command on a log that has just been written, and it
    # dates the log relative to the present; so must this test.
    trial = Trial(1, duration=9.75,
                  start=datetime.now(UTC).replace(microsecond=250000) - timedelta(minutes=10))
    path = write(tmp_path, cl2_log(trial, pods=100))
    cl2.main(["step-time", path, cl2.SATURATION_END[0], "ended"])
    assert capsys.readouterr().out == f"{int(trial.end.timestamp())}\n"
    cl2.main(["step-time", path, cl2.LATENCY_START[0], "started"])
    assert capsys.readouterr().out == "\n"
    cl2.main(["step-time", str(tmp_path / "missing.log"), "x", "started"])
    assert capsys.readouterr().out == "\n"
