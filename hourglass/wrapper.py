"""The run orchestration itself: REQ-1 through REQ-8 wired together into one
`run_instance(instance_home, task_id=None) -> int` entry point. Ordering (spec §4):
task selection + limits validation -> live toolset enumeration -> allowlist diff/apply
-> deadline computation + env export -> one fresh streamed invocation, enforced by the
staged stop/liveness clock -> cleanup sweep -> full log write -> (clean exit only)
task-source update apply.
"""
from __future__ import annotations

import datetime as _dt
import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from . import config as config_mod
from . import destinations as dest_mod
from . import persona as persona_mod
from . import tasksource
from .cli_utils import (
    AgentRunResult,
    hermes_args,
    register_subreaper,
    run as default_run,
    run_agent_streaming,
    sweep_run_processes,
)
from .locking import RunAlreadyActiveError, instance_lock
from .runlog import RunLog
from .toolset import (
    Allowlist,
    ToolsetGateError,
    apply_gate,
    enumerate_live,
    verify_gate,
)

EXIT_OK = 0
EXIT_LOCKED = 75  # EX_TEMPFAIL-ish: a legitimate skip, not a failure Dagu should alert on
EXIT_GATE_FAILED = 2
EXIT_SESSION_FAILED = 1
EXIT_STOPPED = 124  # REQ-4/REQ-5/REQ-6: time-cap, stall, or external-SIGTERM stop


def run_instance(
    instance_home: Path,
    task_id: Optional[str] = None,
    runner=default_run,
    popen=subprocess.Popen,
) -> int:
    instance_home = Path(instance_home)
    try:
        with instance_lock(instance_home):
            return _run_once(instance_home, task_id, runner, popen)
    except RunAlreadyActiveError as exc:
        print(f"hourglass: {exc}")
        return EXIT_LOCKED


def _select_task(cfg, task_id: Optional[str]):
    """REQ-1: exactly the named task runs, selected before any toolset or agent change.
    Returns (task_or_None, error_message_or_None). A single-task config runs without
    `--task`; a multi-task config without an explicit id is a gate failure."""
    if not cfg.tasks:
        return None, None  # legacy no-task-list instances still run (destinations=[])
    if task_id is not None:
        matches = [t for t in cfg.tasks if t.task_id == task_id]
        if not matches:
            return None, f"unknown task id '{task_id}'"
        return matches[0], None
    if len(cfg.tasks) == 1:
        return cfg.tasks[0], None
    return None, "no task id supplied and the config declares more than one task"


def _run_once(instance_home: Path, task_id: Optional[str], runner, popen) -> int:
    run_id = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:8]}"
    log = RunLog(run_id)
    log.set_header()

    # REQ-7: a config-load-time limits failure (missing/invalid instance-level block)
    # must also exit EXIT_GATE_FAILED, before any toolset or agent change — not an
    # uncaught exception.
    try:
        cfg = config_mod.load(instance_home)
    except config_mod.LimitsValidationError as exc:
        log.record_warning(f"limits validation failed: {exc}")
        log.write(instance_home)
        print(f"hourglass: {task_id or '<none>'} failed: {exc}")
        return EXIT_GATE_FAILED

    # REQ-1: task selection happens before any toolset or agent change.
    selected_task, select_error = _select_task(cfg, task_id)
    if select_error is not None:
        log.record_warning(f"task selection failed: {select_error}")
        log.write(instance_home)
        print(f"hourglass: {task_id or '<none>'} failed: {select_error}")
        return EXIT_GATE_FAILED

    run_tasks = [selected_task] if selected_task is not None else list(cfg.tasks)

    # REQ-7: limits are required and validated before any toolset or agent change.
    try:
        limits = cfg.effective_limits(selected_task)
    except config_mod.LimitsValidationError as exc:
        log.record_warning(f"limits validation failed: {exc}")
        log.write(instance_home)
        print(f"hourglass: {task_id or '<none>'} failed: {exc}")
        return EXIT_GATE_FAILED

    register_subreaper()  # REQ-6, Linux only; no-op elsewhere

    # Live enumeration, never a frozen list.
    try:
        enumeration = enumerate_live(cfg.profile, runner)
    except ToolsetGateError as exc:
        log.record_warning(f"toolset enumeration failed: {exc}")
        log.write(instance_home)
        return EXIT_GATE_FAILED

    allowlist = Allowlist(
        builtin=cfg.allowlist_builtin,
        plugin=cfg.allowlist_plugin,
        mcp=cfg.allowlist_mcp,
        gated_live_tools=cfg.gated_live_tools,
    )
    # Only the selected task's destination computes the gate (REQ-1: its own destination
    # allowlist computation is independent of any sibling task not running this time).
    task_destinations = config_mod.task_destinations_ungated(run_tasks)

    try:
        apply_gate(cfg.profile, enumeration, allowlist, task_destinations, runner)
        verify_gate(cfg.profile, allowlist, enumeration, task_destinations, runner)
    except ToolsetGateError as exc:
        log.record_warning(f"toolset gate failed: {exc}")
        log.write(instance_home)
        return EXIT_GATE_FAILED

    state_snippet = ""
    if cfg.state_file is not None and cfg.state_file.exists():
        state_snippet = cfg.state_file.read_text()

    # REQ-3: the agent's deadline = start + 90% of the cap, computed and exported before
    # invocation. Rounded down to whole seconds.
    start_epoch = _dt.datetime.now(_dt.timezone.utc).timestamp()
    deadline_offset = int(limits.run_timeout_seconds * 0.9)
    deadline_epoch = int(start_epoch) + deadline_offset
    hard_deadline_epoch = int(start_epoch) + limits.run_timeout_seconds

    prompt = _build_prompt(cfg, run_tasks, state_snippet, deadline_offset)

    try:
        with persona_mod.persona_skill(cfg.profile, Path(cfg.persona_skill)) as skill_name:
            args = hermes_args(
                cfg.profile,
                "chat",
                "-q",
                prompt,
                "-Q",
                "--format",
                "stream-json",
                "--max-turns",
                str(limits.max_turns),
                "--run-budget",
                str(limits.run_timeout_seconds),
                skills=skill_name,
            )
            env = dict(os.environ)
            env["HOURGLASS_RUN_ID"] = run_id
            env["HOURGLASS_DEADLINE_EPOCH"] = str(deadline_epoch)
            env["HOURGLASS_HARD_DEADLINE_EPOCH"] = str(hard_deadline_epoch)

            # REQ-6: an external SIGTERM delivered to Hourglass itself is treated the
            # same as the REQ-4 time-cap path — the agent subprocess is staged-stopped
            # and the run is recorded "Hourglass terminated", not left to die uncleanly
            # alongside Hourglass's own default SIGTERM disposition.
            external_stop = {"flag": False}
            previous_handler = signal.getsignal(signal.SIGTERM)

            def _on_sigterm(signum, frame):  # noqa: ANN001 - signal handler signature
                external_stop["flag"] = True

            signal.signal(signal.SIGTERM, _on_sigterm)
            try:
                agent_result: AgentRunResult = run_agent_streaming(
                    args,
                    env=env,
                    run_timeout_seconds=limits.run_timeout_seconds,
                    grace_seconds=limits.grace_seconds,
                    stall_seconds=limits.stall_seconds,
                    events_path=log.events_path(instance_home),
                    external_stop_flag=lambda: external_stop["flag"],
                    popen=popen,
                )
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
    except persona_mod.PersonaLoadError as exc:
        log.record_warning(f"persona load failed: {exc}")
        log.write(instance_home)
        return EXIT_GATE_FAILED

    # REQ-6: sweep leftover run-marked processes on every exit path, before returning.
    sweep_run_processes(run_id)

    ended_at = _dt.datetime.now(_dt.timezone.utc)
    stopped_early = agent_result.stop_reason is not None
    clean_exit = agent_result.returncode == 0 and not stopped_early

    if stopped_early:
        outcome = "stopped"
    elif clean_exit:
        outcome = "done"
    else:
        outcome = "failed"

    for task in run_tasks:
        status = "done" if clean_exit else "failed"
        log.record_task(task.task_id, status)

    log.record_run_detail(
        task_id=run_tasks[0].task_id if run_tasks else None,
        session_id=agent_result.session_id,
        model=agent_result.model,
        limits={
            "run_timeout_seconds": limits.run_timeout_seconds,
            "max_turns": limits.max_turns,
            "stall_seconds": limits.stall_seconds,
            "grace_seconds": limits.grace_seconds,
            "deadline_epoch": deadline_epoch,
            "hard_deadline_epoch": hard_deadline_epoch,
        },
        ended_at=ended_at,
        tool_use_count=agent_result.tool_use_count,
        outcome=outcome,
        stop_reason=agent_result.stop_reason,
    )
    log.write(instance_home)

    stderr_reason = agent_result.stop_reason or (
        "ok" if clean_exit else f"agent exit {agent_result.returncode}"
    )
    task_label = run_tasks[0].task_id if run_tasks else "<none>"
    print(f"hourglass: {task_label} {outcome}: {stderr_reason}")

    if stopped_early:
        return EXIT_STOPPED

    if not clean_exit:
        log.record_warning(f"agent exit {agent_result.returncode}: {agent_result.final_text.strip()}")
        log.write(instance_home)
        return EXIT_SESSION_FAILED

    # REQ-4: each task's declared destination gets its own verification pass.
    for task in run_tasks:
        if not task.destination:
            continue
        try:
            dest = dest_mod.parse(task.destination)
        except dest_mod.InvalidDestinationError as exc:
            log.record_warning(f"task {task.task_id}: invalid destination: {exc}")
            continue
        if dest_mod.requires_manual_verification(dest):
            log.record_warning(
                f"task {task.task_id}: destination '{task.destination}' requires manual "
                f"verification (no read-back API owned by this repo, per REQ-4 manual_by:)"
            )
            continue
        error = dest_mod.verify(dest, instance_home)
        if error:
            log.record_warning(f"task {task.task_id}: {error}")
    log.write(instance_home)

    if cfg.state_file is not None:
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_file.write_text(f"last_run_id: {run_id}\n")

    # REQ-3/REQ-8: parse NEXT_TASK_SOURCE by prefix match from the result event's own
    # text, apply only on a clean exit.
    proposed = tasksource.parse_next_task_source(agent_result.final_text)
    if proposed is not None:
        if tasksource.is_valid_task_source(proposed):
            cfg.set_task_source(proposed)
        else:
            log.record_warning(f"unparseable NEXT_TASK_SOURCE value: {proposed!r}")
            log.write(instance_home)

    return EXIT_OK


def _build_prompt(cfg, run_tasks, state_snippet: str, deadline_offset_seconds: int) -> str:
    lines = [
        f"Task source: {cfg.task_source}",
        "",
        f"You have {deadline_offset_seconds} seconds before you should wrap up and "
        f"finish your answer (90% of this run's time cap). A hard external cutoff "
        f"follows shortly after that if you have not finished.",
    ]
    if state_snippet:
        lines += ["", "Prior run state:", state_snippet]
    for task in run_tasks:
        if task.prompt:
            lines += ["", f"Task {task.task_id}:", task.prompt]
        if not task.destination:
            continue
        try:
            dest = dest_mod.parse(task.destination)
        except dest_mod.InvalidDestinationError:
            continue  # surfaced instead by the post-run REQ-4 verification pass
        lines += ["", f"Task {task.task_id} destination instruction:",
                   dest_mod.instruction_for(dest, cfg.instance_home)]
    return "\n".join(lines)
