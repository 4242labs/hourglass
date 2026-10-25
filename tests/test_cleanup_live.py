"""REQ-4/5/6, genuine-subprocess acceptance: staged stop (SIGTERM, grace, SIGKILL),
stall-liveness enforcement, and the leftover-process cleanup sweep — against
`tests/fakeagent.py`, a real subprocess, not a mock. Signal timing is exercised for
real; these tests are slower than the unit suite by design.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hourglass.cli_utils import (
    find_marked_pids,
    register_subreaper,
    run_agent_streaming,
    staged_stop,
    sweep_run_processes,
)

FAKEAGENT = str(Path(__file__).parent / "fakeagent.py")


def _popen_env(run_id: str, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["HOURGLASS_RUN_ID"] = run_id
    if extra:
        env.update(extra)
    return env


def test_staged_stop_sigterm_only_when_agent_cooperates(tmp_path):
    """A cooperative agent exits on SIGTERM alone — no SIGKILL needed."""
    proc = subprocess.Popen(
        [sys.executable, FAKEAGENT, "--event-gap", "5", "--events", "20"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)  # let it start and block in its sleep loop
    used_sigkill = staged_stop(proc, grace_seconds=3)
    assert used_sigkill is False
    assert proc.returncode is not None


def test_staged_stop_escalates_to_sigkill_when_agent_ignores_sigterm(tmp_path):
    """REQ-4 acceptance: an agent that ignores SIGTERM is SIGKILLed after grace_seconds."""
    proc = subprocess.Popen(
        [sys.executable, FAKEAGENT, "--ignore-sigterm", "--event-gap", "5", "--events", "20"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)
    start = time.monotonic()
    used_sigkill = staged_stop(proc, grace_seconds=1)
    elapsed = time.monotonic() - start
    assert used_sigkill is True
    assert proc.returncode is not None
    assert elapsed < 5  # bounded by grace_seconds, not left hanging


def test_run_agent_streaming_enforces_hard_time_cap(tmp_path):
    """REQ-4: run_timeout_seconds caps total run time even though the fake agent itself
    would run far longer (20 events * 2s gap = 40s)."""
    run_id = "test-timecap"
    events_path = tmp_path / "run.events.jsonl"
    env = _popen_env(run_id)

    start = time.monotonic()
    result = run_agent_streaming(
        [sys.executable, FAKEAGENT, "--event-gap", "2", "--events", "20"],
        env=env,
        run_timeout_seconds=4,  # hard_stop_at = start + 4 - grace(1) - 3 margin => ~0s
        grace_seconds=1,
        stall_seconds=100,
        events_path=events_path,
    )
    elapsed = time.monotonic() - start
    assert result.stop_reason == "time cap"
    assert elapsed < 8  # must not run anywhere near the fake agent's full 40s


def test_run_agent_streaming_enforces_stall_liveness(tmp_path):
    """REQ-5: no event for stall_seconds stops the run even with run_timeout_seconds
    far from exhausted."""
    run_id = "test-stall"
    events_path = tmp_path / "run.events.jsonl"
    env = _popen_env(run_id)

    result = run_agent_streaming(
        [sys.executable, FAKEAGENT, "--event-gap", "5", "--events", "3"],
        env=env,
        run_timeout_seconds=600,
        grace_seconds=1,
        stall_seconds=2,  # shorter than the 5s event gap -> must trip first
        events_path=events_path,
    )
    assert result.stop_reason == "no activity for 2 s"


def test_run_agent_streaming_writes_events_incrementally(tmp_path):
    """REQ-8: the raw events JSONL carries one line per event, flushed as written —
    verified by re-reading the file after a normal exit."""
    run_id = "test-events"
    events_path = tmp_path / "run.events.jsonl"
    env = _popen_env(run_id)

    result = run_agent_streaming(
        [sys.executable, FAKEAGENT, "--event-gap", "0.05", "--events", "3"],
        env=env,
        run_timeout_seconds=30,
        grace_seconds=5,
        stall_seconds=10,
        events_path=events_path,
    )
    assert result.stop_reason is None
    assert result.returncode == 0
    lines = events_path.read_text().splitlines()
    assert len(lines) >= 5  # init + 3 chunks + result
    for line in lines:
        json.loads(line)  # every line is valid JSON on its own


def test_cleanup_sweep_finds_and_kills_detached_descendant(tmp_path):
    """REQ-6 acceptance: a double-forked/detached `sleep 300` descendant that re-parents
    away from the agent subprocess is still found (by env marker, not process-tree walk)
    and killed by the sweep."""
    run_id = f"test-sweep-{os.getpid()}-{int(time.time())}"
    env = _popen_env(run_id)

    proc = subprocess.Popen(
        [sys.executable, FAKEAGENT, "--spawn-detached", "--event-gap", "0.05", "--events", "1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.wait(timeout=10)  # the fake agent itself exits quickly; its sleep 300 child lingers

    time.sleep(0.3)
    marked_before = find_marked_pids(run_id)
    assert marked_before, "the detached sleep 300 child must still carry the run_id marker"

    killed = sweep_run_processes(run_id, wait_seconds=1.0)
    time.sleep(0.3)
    marked_after = find_marked_pids(run_id)
    assert marked_after == []  # every marked process is gone after the sweep


def test_cleanup_sweep_is_run_scoped_does_not_touch_other_run_ids(tmp_path):
    """The sweep's marker match must be exact-run-id, never a prefix or substring match
    that could sweep an unrelated concurrent run's processes."""
    run_id_a = f"test-sweep-a-{os.getpid()}-{int(time.time())}"
    run_id_b = f"test-sweep-a-{os.getpid()}-{int(time.time())}-extra"  # superstring of A

    proc_b = subprocess.Popen(
        [sys.executable, FAKEAGENT, "--spawn-detached", "--event-gap", "0.05", "--events", "1"],
        env=_popen_env(run_id_b),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_b.wait(timeout=10)
    time.sleep(0.3)

    sweep_run_processes(run_id_a, wait_seconds=0.5)  # must not match run_id_b's marker
    time.sleep(0.3)
    assert find_marked_pids(run_id_b), "an unrelated run_id's process must survive another run's sweep"

    sweep_run_processes(run_id_b, wait_seconds=1.0)  # cleanup after the test itself
