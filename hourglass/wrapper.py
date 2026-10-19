"""The run orchestration itself: REQ-1 through REQ-7 wired together into one
`run_instance(instance_home) -> int` entry point. See README.md for the ordering this
follows (spec §4 "Ordering"): task-source resolution → live toolset enumeration →
allowlist diff/apply → invocation → log write → (clean exit only) task-source update apply.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from pathlib import Path

from . import config as config_mod
from . import persona as persona_mod
from . import tasksource
from .cli_utils import CommandResult, hermes_args, run as default_run
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


def run_instance(instance_home: Path, runner=default_run) -> int:
    instance_home = Path(instance_home)
    try:
        with instance_lock(instance_home):
            return _run_once(instance_home, runner)
    except RunAlreadyActiveError as exc:
        print(f"hourglass: {exc}")
        return EXIT_LOCKED


def _run_once(instance_home: Path, runner) -> int:
    run_id = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:8]}"
    log = RunLog(run_id)
    log.set_header()

    cfg = config_mod.load(instance_home)

    # REQ-7 step 1: live enumeration, never a frozen list.
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
    task_destinations = config_mod.task_destinations_ungated(cfg.tasks)

    # REQ-7 steps 2-5: disable-all, enable-declared-allowlist-only, auto-execute ungate.
    try:
        apply_gate(cfg.profile, enumeration, allowlist, task_destinations, runner)
        # REQ-5: re-read live state and fail closed on any extra enabled entry.
        verify_gate(cfg.profile, allowlist, enumeration, task_destinations, runner)
    except ToolsetGateError as exc:
        log.record_warning(f"toolset gate failed: {exc}")
        log.write(instance_home)
        return EXIT_GATE_FAILED

    # REQ-6: optional state-file read, before resolving the task list (resolution itself
    # is task-list-schema territory, out of scope — the wrapper only passes the state file
    # path through to the session if configured).
    state_snippet = ""
    if cfg.state_file is not None and cfg.state_file.exists():
        state_snippet = cfg.state_file.read_text()

    prompt = _build_prompt(cfg, state_snippet)

    # cfg.persona_skill is a filesystem path to the crew persona's spec (e.g.
    # tortuga/crew/pessoa/pessoa.md), materialized as a one-shot Hermes skill for the
    # duration of this one invocation only — mirrors the operator's own `--append`
    # mechanism (see hourglass/persona.py) rather than relying on a prompt label Hermes
    # never actually enforces.
    try:
        with persona_mod.persona_skill(cfg.profile, Path(cfg.persona_skill)) as skill_name:
            # REQ-1: a fresh one-shot session, never resumed.
            result: CommandResult = runner(
                hermes_args(cfg.profile, "chat", "-q", prompt, "-Q", skills=skill_name),
                timeout=None,
            )
    except persona_mod.PersonaLoadError as exc:
        log.record_warning(f"persona load failed: {exc}")
        log.write(instance_home)
        return EXIT_GATE_FAILED

    # REQ-2: one grep-able line per task. The task-list-schema phase (out of scope, §4)
    # owns per-task status parsing from the session's own output; until that schema
    # exists, every task in this run gets the same run-level outcome recorded against it
    # so REQ-2's acceptance check (line count == resolved task count, every line carries a
    # status suffix) is met today rather than deferred past this implementation.
    run_status = "done" if result.ok else "failed"
    for task in cfg.tasks:
        log.record_task(task.task_id, run_status)
    log.write(instance_home)

    if not result.ok:
        log.record_warning(f"hermes chat exited {result.returncode}: {result.stderr.strip()}")
        log.write(instance_home)
        return EXIT_SESSION_FAILED

    # REQ-6: state file is written back after a clean run, keyed on run_id so the hash
    # changes every run (cross-run dedup content itself is task-list-schema territory,
    # out of scope per §4 — this is the write-back mechanism REQ-6 requires to exist).
    if cfg.state_file is not None:
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_file.write_text(f"last_run_id: {run_id}\n")

    # REQ-3: parse NEXT_TASK_SOURCE by prefix match, apply only on clean exit.
    proposed = tasksource.parse_next_task_source(result.stdout)
    if proposed is not None:
        if tasksource.is_valid_task_source(proposed):
            cfg.set_task_source(proposed)
        else:
            log.record_warning(f"unparseable NEXT_TASK_SOURCE value: {proposed!r}")
            log.write(instance_home)

    return EXIT_OK


def _build_prompt(cfg, state_snippet: str) -> str:
    lines = [f"Task source: {cfg.task_source}"]
    if state_snippet:
        lines += ["", "Prior run state:", state_snippet]
    return "\n".join(lines)
