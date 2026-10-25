"""Thin subprocess wrapper around the `hermes` CLI, plus REQ-4/5/6's process-lifecycle
primitives: the streaming agent runner (liveness clock, staged stop), the leftover-
process sweep, and Linux subreaper registration.

Isolated here so every other module can be unit-tested without ever invoking a real
subprocess: tests inject a fake `runner` callable with the same signature as `run`.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import platform
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(args: Sequence[str], timeout: Optional[float] = 60.0) -> CommandResult:
    """Run `args` (argv, no shell) and capture output. Never raises on a non-zero exit —
    callers decide what a given exit code means for their own command."""
    proc = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        args=list(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def hermes_args(
    profile: Optional[str], *rest: str, skills: Optional[str] = None
) -> List[str]:
    """Build a `hermes [--profile X] [--skills Y] <rest...>` argv list. `skills` loads a
    persona materialized by `hourglass.persona.persona_skill` for the duration of one
    invocation; omit it for the toolset-gate commands, which carry no persona."""
    args = ["hermes"]
    if profile:
        args += ["--profile", profile]
    if skills:
        args += ["--skills", skills]
    args += list(rest)
    return args


# --------------------------------------------------------------------------------------
# REQ-4/REQ-5/REQ-6: process lifecycle — staged stop, liveness, and the cleanup sweep.
# --------------------------------------------------------------------------------------

PR_SET_CHILD_SUBREAPER = 36  # Linux prctl(2) constant, uapi/linux/prctl.h


def register_subreaper() -> bool:
    """Linux only: register this process as a child subreaper (REQ-6) so a double-forked
    descendant that re-parents away from its immediate parent is still reachable. No-op,
    returns False, on any non-Linux platform (macOS has no equivalent facility, spec §2)."""
    if platform.system() != "Linux":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        result = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        return result == 0
    except (OSError, AttributeError):  # pragma: no cover - defensive, platform-dependent
        return False


def _all_pids() -> List[int]:
    if platform.system() == "Linux":
        try:
            return [int(name) for name in os.listdir("/proc") if name.isdigit()]
        except OSError:  # pragma: no cover
            return []
    # macOS (and any other non-Linux POSIX): enumerate via `ps`.
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=10
        ).stdout
        return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []


def _pid_carries_marker(pid: int, run_id: str) -> bool:
    marker = f"HOURGLASS_RUN_ID={run_id}"
    if platform.system() == "Linux":
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            return False
        return marker.encode() in raw.split(b"\x00")
    # macOS: `ps eww -p <pid>` prints the process's environment on the same line as its
    # command (live-verified, spec §2). No /proc on Darwin.
    try:
        out = subprocess.run(
            ["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return marker in out


def find_marked_pids(run_id: str) -> List[int]:
    """Every live process whose own environment carries `HOURGLASS_RUN_ID=<run_id>`,
    excluding this process itself (which also carries the marker for its own child
    processes to inherit, but must never signal itself)."""
    self_pid = os.getpid()
    marker = f"HOURGLASS_RUN_ID={run_id}"
    if platform.system() == "Linux":
        found = []
        for pid in _all_pids():
            if pid == self_pid:
                continue
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if marker.encode() in raw.split(b"\x00"):
                found.append(pid)
        return found
    # macOS: one `ps eww -A` call enumerates every process's own environment on the same
    # line as its command — far cheaper than one `ps` invocation per candidate pid.
    try:
        out = subprocess.run(
            ["ps", "eww", "-A", "-o", "pid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    found = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Exact-value match: the marker's value ends at a whitespace boundary or line
        # end, never a substring match that could also hit a run_id that is a superstring
        # of this one (e.g. run_id_a's sweep must not also match run_id_a-extra's marker).
        idx = stripped.find(marker)
        if idx == -1:
            continue
        end = idx + len(marker)
        if end < len(stripped) and not stripped[end].isspace():
            continue
        try:
            pid = int(stripped.split()[0])
        except (ValueError, IndexError):  # pragma: no cover
            continue
        if pid != self_pid:
            found.append(pid)
    return found


def _send_signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race-safe
        pass


def sweep_run_processes(run_id: str, wait_seconds: float = 2.0) -> List[int]:
    """REQ-6: double-scan sweep closing the snapshot-then-kill race (GitHub Actions
    runner issue #4601, spec §2) — re-scans the process table after SIGTERM and again
    after the wait, so a process spawned between scans is still caught. Returns the pids
    that were still alive and SIGKILLed at the end (empty on the happy path)."""
    first_scan = find_marked_pids(run_id)
    for pid in first_scan:
        _send_signal(pid, signal.SIGTERM)
    time.sleep(wait_seconds)
    second_scan = find_marked_pids(run_id)
    for pid in second_scan:
        _send_signal(pid, signal.SIGKILL)
    return second_scan


def staged_stop(proc: "subprocess.Popen", grace_seconds: float) -> bool:
    """REQ-4: SIGTERM, wait up to `grace_seconds`, then SIGKILL if still running. Returns
    True iff SIGKILL was actually needed (the process did not exit on SIGTERM alone)."""
    if proc.poll() is not None:
        return False
    _send_signal(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    if proc.poll() is None:
        _send_signal(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        return True
    return False


# --------------------------------------------------------------------------------------
# REQ-3/REQ-4/REQ-5/REQ-8: the streaming agent runner.
# --------------------------------------------------------------------------------------


@dataclass
class AgentRunResult:
    returncode: int
    final_text: str
    session_id: Optional[str]
    model: Optional[str]
    event_count: int
    tool_use_count: int
    stop_reason: Optional[str]  # None (clean) | "time cap" | "no activity for Ns" | "Hourglass terminated"
    used_sigkill: bool


Runner = Callable[..., CommandResult]


def run_agent_streaming(
    args: Sequence[str],
    *,
    env: Dict[str, str],
    run_timeout_seconds: int,
    grace_seconds: int,
    stall_seconds: int,
    events_path: Path,
    external_stop_flag: Optional[Callable[[], bool]] = None,
    popen=subprocess.Popen,
) -> AgentRunResult:
    """REQ-3/4/5/8: launch `args` (a `hermes chat ... --format stream-json` argv),
    stream its stdout to `events_path` (one JSON line per event, flushed per line so a
    killed run's partial history survives on disk), and enforce the hard time cap
    (REQ-4) and liveness (REQ-5) independently of agent cooperation.

    `external_stop_flag`, if given, is polled each loop iteration; when it returns True
    the run is stopped the same way as a time-cap stop, reason "Hourglass terminated"
    (REQ-6's external-SIGTERM-to-Hourglass-itself path).
    """
    start = time.monotonic()
    hard_stop_at = start + run_timeout_seconds - grace_seconds - 3  # REQ-4's 3s margin
    last_event_at = start

    proc = popen(
        list(args),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events_path.parent.mkdir(parents=True, exist_ok=True)
    event_count = 0
    tool_use_count = 0
    session_id: Optional[str] = None
    model: Optional[str] = None
    final_text = ""
    stop_reason: Optional[str] = None
    used_sigkill = False
    poll_interval = 0.5  # cap on how long a real pipe read blocks before re-checking limits

    with open(events_path, "a") as events_file:
        while True:
            if proc.stdout is None:  # pragma: no cover - defensive
                break
            # REQ-5 needs the stall/time-cap checks below to run even while no event has
            # arrived; a bare blocking readline() on a real pipe would starve them until
            # the next line shows up. select() bounds the wait so the loop always comes
            # back around. Fakes without a real fd (unit tests) skip straight to readline.
            readable = True
            if hasattr(proc.stdout, "fileno"):
                try:
                    ready, _, _ = select.select([proc.stdout], [], [], poll_interval)
                    readable = bool(ready)
                except (OSError, ValueError):  # pragma: no cover - closed/invalid fd race
                    readable = True
            line = proc.stdout.readline() if readable else ""
            if line:
                events_file.write(line if line.endswith("\n") else line + "\n")
                events_file.flush()
                event_count += 1
                last_event_at = time.monotonic()
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover - malformed line
                    evt = {}
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    model = evt.get("model")
                    session_id = evt.get("session_id") or session_id
                elif etype == "tool_use":
                    tool_use_count += 1
                elif etype == "result":
                    final_text = evt.get("text", final_text)
                    session_id = evt.get("session_id") or session_id
            elif proc.poll() is not None:
                break  # process exited and no more buffered output

            now = time.monotonic()
            if external_stop_flag is not None and external_stop_flag():
                stop_reason = "Hourglass terminated"
                used_sigkill = staged_stop(proc, grace_seconds)
                break
            if now >= hard_stop_at:
                stop_reason = "time cap"
                used_sigkill = staged_stop(proc, grace_seconds)
                break
            if now - last_event_at >= stall_seconds:
                stop_reason = f"no activity for {stall_seconds} s"
                used_sigkill = staged_stop(proc, grace_seconds)
                break
            if not line:
                time.sleep(0.05)

        proc.wait()

    return AgentRunResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        final_text=final_text,
        session_id=session_id,
        model=model,
        event_count=event_count,
        tool_use_count=tool_use_count,
        stop_reason=stop_reason,
        used_sigkill=used_sigkill,
    )

