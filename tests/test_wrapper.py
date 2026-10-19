"""End-to-end orchestration test: wires REQ-1..REQ-7 together against a fake `hermes`
runner and a real config.yaml/instance_home on disk."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hourglass.cli_utils import CommandResult
from hourglass.wrapper import EXIT_GATE_FAILED, EXIT_OK, EXIT_SESSION_FAILED, run_instance
from tests.fixtures import GMAIL_TOOLS, LINEAR_TOOLS, mcp_list_output, mcp_test_output, tools_list_output
from tests.test_toolset import FakeHermes, _ALL_26


CONFIG_YAML = """
profile: hg-e2e
persona_skill: macgyver
task_source: 42L-2212
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""


class ChatCapableFakeHermes(FakeHermes):
    """Extends the toolset-gate fake with a `chat -q ... -Q` response."""

    def __init__(self, chat_stdout="answer\nsession_id: abc\n", chat_returncode=0, **kw):
        super().__init__(**kw)
        self.chat_stdout = chat_stdout
        self.chat_returncode = chat_returncode

    def __call__(self, args, timeout=None):
        cmd = list(args[1:])
        if len(cmd) >= 2 and cmd[0] == "--profile":
            cmd = cmd[2:]
        if cmd[:1] == ["chat"]:
            self.calls.append(list(args))
            return CommandResult(args, self.chat_returncode, self.chat_stdout, "")
        return super().__call__(args, timeout)


def _write_config(home: Path, extra: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG_YAML.format() + extra)


def test_clean_run_returns_ok_and_writes_log():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake = ChatCapableFakeHermes(chat_stdout="done\nNEXT_TASK_SOURCE: 42L-9999\nsession_id: x\n")

        rc = run_instance(home, runner=fake)

        assert rc == EXIT_OK
        logs = list((home / "logs").glob("*.md"))
        assert len(logs) == 1
        # REQ-3: task-source update applied only after a clean exit.
        from hourglass import config as config_mod
        cfg = config_mod.load(home)
        assert cfg.task_source == "42L-9999"


def test_session_failure_returns_nonzero_and_does_not_apply_update():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake = ChatCapableFakeHermes(
            chat_stdout="NEXT_TASK_SOURCE: 42L-9999\nsession_id: x\n",
            chat_returncode=1,
        )

        rc = run_instance(home, runner=fake)

        assert rc == EXIT_SESSION_FAILED
        from hourglass import config as config_mod
        cfg = config_mod.load(home)
        assert cfg.task_source == "42L-2212"  # unchanged


def test_gate_failure_short_circuits_before_invoking_chat():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)

        def broken_enumerate(args, timeout=None):
            return CommandResult(args, 1, "", "boom")

        rc = run_instance(home, runner=broken_enumerate)
        assert rc == EXIT_GATE_FAILED


def test_second_concurrent_run_is_skipped_not_double_invoked():
    import threading

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake = ChatCapableFakeHermes()

        from hourglass.locking import instance_lock
        results = []
        with instance_lock(home):
            # A second invocation while the lock is held must be skipped, never double-run.
            rc = run_instance(home, runner=fake)
            results.append(rc)
        assert results == [75]  # EXIT_LOCKED
        assert not any(c[1:2] == ["chat"] for c in fake.calls)
