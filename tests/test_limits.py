"""REQ-2/REQ-3: tool-call iteration cap, agent deadline export, --run-budget wiring."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hourglass import config as config_mod
from hourglass.config import LimitsValidationError, Task
from hourglass.wrapper import EXIT_GATE_FAILED, EXIT_OK, EXIT_SESSION_FAILED, run_instance
from tests.fixtures import FakePopen


def _write_persona_fixture(home: Path) -> Path:
    persona_path = home / "fixture-persona.md"
    persona_path.write_text("# Agent: FIXTURE\n\nA minimal persona for limits tests.\n")
    return persona_path


CONFIG_YAML = """
profile: hg-limits
persona_skill: {persona_skill}
task_source: 42L-2212
limits:
  run_timeout_seconds: {run_timeout_seconds}
  max_turns: {max_turns}
  stall_seconds: {stall_seconds}
  grace_seconds: {grace_seconds}
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks:
  - task_id: t1
    destination: null
    auto_execute: false
    prompt: do the thing
"""


def _write_config(
    home: Path,
    run_timeout_seconds=1200,
    max_turns=50,
    stall_seconds=420,
    grace_seconds=30,
) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    path = home / "config.yaml"
    path.write_text(
        CONFIG_YAML.format(
            persona_skill=persona_path,
            run_timeout_seconds=run_timeout_seconds,
            max_turns=max_turns,
            stall_seconds=stall_seconds,
            grace_seconds=grace_seconds,
        )
    )
    return path


class GatePassingFakeRunner:
    """Fake `hermes tools/mcp ...` runner — always passes the gate, no MCP servers."""

    def __call__(self, args, timeout=None):
        from hourglass.cli_utils import CommandResult

        cmd = list(args[1:])
        if cmd[:2] == ["--profile", "hg-limits"]:
            cmd = cmd[2:]
        if cmd[:2] == ["tools", "list"]:
            return CommandResult(args, 0, "Built-in toolsets:\n  ✓ enabled  builtin_00  d\n\nPlugin toolsets:\n", "")
        if cmd[:2] == ["mcp", "list"]:
            return CommandResult(args, 0, "Name       Transport   Status\n-----------------------------\n", "")
        if cmd[:2] == ["tools", "disable"] or cmd[:2] == ["tools", "enable"]:
            return CommandResult(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")


def test_max_turns_flag_reaches_built_argv():
    """REQ-2: the built argv for the agent invocation carries --max-turns <N>."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home, max_turns=37)
        captured_args = {}

        def fake_popen(args, **kwargs):
            captured_args["args"] = list(args)
            return FakePopen(['{"type": "result", "session_id": "s", "text": "done"}'])

        rc = run_instance(home, runner=GatePassingFakeRunner(), popen=fake_popen)
        assert rc == EXIT_OK
        assert "--max-turns" in captured_args["args"]
        idx = captured_args["args"].index("--max-turns")
        assert captured_args["args"][idx + 1] == "37"


def test_run_budget_flag_reaches_built_argv():
    """REQ-3: --run-budget <run_timeout_seconds> is passed as a cooperative hint."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home, run_timeout_seconds=1200)
        captured_args = {}

        def fake_popen(args, **kwargs):
            captured_args["args"] = list(args)
            return FakePopen(['{"type": "result", "session_id": "s", "text": "done"}'])

        rc = run_instance(home, runner=GatePassingFakeRunner(), popen=fake_popen)
        assert rc == EXIT_OK
        idx = captured_args["args"].index("--run-budget")
        assert captured_args["args"][idx + 1] == "1200"


def test_deadline_exported_in_env_1200s_cap():
    """REQ-3: 1200s cap -> 1080s (90%) deadline in the prompt and env; hard deadline at 1200s."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home, run_timeout_seconds=1200)
        captured = {}

        def fake_popen(args, env=None, **kwargs):
            captured["args"] = list(args)
            captured["env"] = env
            return FakePopen(['{"type": "result", "session_id": "s", "text": "done"}'])

        before = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp()
        rc = run_instance(home, runner=GatePassingFakeRunner(), popen=fake_popen)
        after = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp()
        assert rc == EXIT_OK

        q_idx = captured["args"].index("-q")
        prompt = captured["args"][q_idx + 1]
        assert "1080 seconds" in prompt

        deadline = int(captured["env"]["HOURGLASS_DEADLINE_EPOCH"])
        hard_deadline = int(captured["env"]["HOURGLASS_HARD_DEADLINE_EPOCH"])
        assert abs(deadline - (before + 1080)) <= 2
        assert abs(hard_deadline - (before + 1200)) <= 2
        assert captured["env"]["HOURGLASS_RUN_ID"]


def test_deadline_exported_59s_cap():
    """REQ-3 edge case: 59s cap -> 53s (90%) deadline, still 1-second precision."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home, run_timeout_seconds=59, stall_seconds=10, grace_seconds=5)
        captured = {}

        def fake_popen(args, env=None, **kwargs):
            captured["args"] = list(args)
            captured["env"] = env
            return FakePopen(['{"type": "result", "session_id": "s", "text": "done"}'])

        rc = run_instance(home, runner=GatePassingFakeRunner(), popen=fake_popen)
        assert rc == EXIT_OK
        q_idx = captured["args"].index("-q")
        prompt = captured["args"][q_idx + 1]
        assert "53 seconds" in prompt


def test_step_cap_failure_recorded_with_real_exit_code_and_text():
    """REQ-2: a fake agent exiting 1 with iteration-cap wording is recorded with that
    exit code and that text, not a synthesized label."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)

        def fake_popen(args, **kwargs):
            return FakePopen(
                ['{"type": "result", "session_id": "s", "text": "Stopped early due to iteration limit"}'],
                returncode=1,
            )

        rc = run_instance(home, runner=GatePassingFakeRunner(), popen=fake_popen)
        assert rc == EXIT_SESSION_FAILED

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "Stopped early due to iteration limit" in text
        assert "agent exit 1" in text
