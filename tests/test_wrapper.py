"""End-to-end orchestration test: wires REQ-1..REQ-7 together against a fake `hermes`
runner and a real config.yaml/instance_home on disk."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hourglass.cli_utils import CommandResult
from hourglass.wrapper import EXIT_GATE_FAILED, EXIT_OK, EXIT_SESSION_FAILED, run_instance
from tests.fixtures import GMAIL_TOOLS, LINEAR_TOOLS, mcp_list_output, mcp_test_output, tools_list_output
from tests.test_toolset import FakeHermes, _ALL_26


def _write_persona_fixture(home: Path) -> Path:
    """A standalone persona file, not under HERMES_HOME — mirrors a real crew persona
    (e.g. tortuga/crew/pessoa/pessoa.md) that lives entirely outside Hermes's own tree."""
    persona_path = home / "fixture-persona.md"
    persona_path.write_text("# Agent: FIXTURE\n\nA minimal persona for wrapper e2e tests.\n")
    return persona_path


CONFIG_YAML = """
profile: hg-e2e
persona_skill: {persona_skill}
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
        while len(cmd) >= 2 and cmd[0] in ("--profile", "--skills"):
            cmd = cmd[2:]
        if cmd[:1] == ["chat"]:
            self.calls.append(list(args))
            return CommandResult(args, self.chat_returncode, self.chat_stdout, "")
        return super().__call__(args, timeout)


def _write_config(home: Path, extra: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    (home / "config.yaml").write_text(CONFIG_YAML.format(persona_skill=persona_path) + extra)


TASKS_CONFIG_YAML = """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: {state_file}
tasks:
  - task_id: t1
    destination: null
    auto_execute: false
    prompt: do thing one
  - task_id: t2
    destination: null
    auto_execute: false
    prompt: do thing two
  - task_id: t3
    destination: null
    auto_execute: false
    prompt: do thing three
"""


def _write_tasks_config(home: Path, state_file) -> None:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    (home / "config.yaml").write_text(
        TASKS_CONFIG_YAML.format(persona_skill=persona_path, state_file=state_file)
    )


def test_req2_run_log_has_one_grepable_line_per_resolved_task():
    """REQ-2 acceptance: grep -c '^- task:' must equal the resolved task list's own task
    count, and every line must carry a status: (done|failed|skipped) suffix."""
    import re

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_tasks_config(home, state_file="null")
        fake = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc = run_instance(home, runner=fake)
        assert rc == EXIT_OK

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        task_lines = [l for l in text.splitlines() if l.startswith("- task:")]
        assert len(task_lines) == 3  # matches the resolved task list's count exactly
        for line in task_lines:
            assert re.search(r"status: \(done|failed|skipped\)$", line)


def test_req6_state_file_is_written_back_and_hash_changes_each_run():
    """REQ-6 acceptance: an instance with state_file set must have its hash change after
    each of two runs."""
    import hashlib

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        state_path = home / "state" / "dedup.state"
        _write_tasks_config(home, state_file=f'"{state_path}"')
        fake1 = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc1 = run_instance(home, runner=fake1)
        assert rc1 == EXIT_OK
        assert state_path.exists()
        hash1 = hashlib.md5(state_path.read_bytes()).hexdigest()

        fake2 = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")
        rc2 = run_instance(home, runner=fake2)
        assert rc2 == EXIT_OK
        hash2 = hashlib.md5(state_path.read_bytes()).hexdigest()

        assert hash1 != hash2  # changed both times, per REQ-6 acceptance


def test_chat_invocation_carries_skills_flag_for_persona():
    """Hourglass must actually load the persona via --skills, not just label it in the
    prompt — the bug this fix addresses."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc = run_instance(home, runner=fake)

        assert rc == EXIT_OK
        chat_calls = [c for c in fake.calls if "chat" in c]
        assert len(chat_calls) == 1
        assert "--skills" in chat_calls[0]
        skills_idx = chat_calls[0].index("--skills")
        skill_name = chat_calls[0][skills_idx + 1]
        assert skill_name.startswith("aa-persona-")


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


DESTINATION_CONFIG_YAML = """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks:
  - task_id: t1
    destination: "file:{dest_rel}"
    auto_execute: false
    prompt: write a file
"""


def _write_destination_config(home: Path, dest_rel: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    (home / "config.yaml").write_text(
        DESTINATION_CONFIG_YAML.format(persona_skill=persona_path, dest_rel=dest_rel)
    )


def test_req4_destination_instruction_reaches_the_chat_prompt():
    """REQ-4: the wrapper must tell the session the exact file path to write to, not
    just label the destination in config."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_destination_config(home, dest_rel="out/result.txt")
        fake = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc = run_instance(home, runner=fake)
        assert rc == EXIT_OK

        chat_calls = [c for c in fake.calls if "chat" in c]
        assert len(chat_calls) == 1
        q_idx = chat_calls[0].index("-q")
        prompt = chat_calls[0][q_idx + 1]
        assert str(home / "out" / "result.txt") in prompt
        assert "destination" in prompt.lower()


def test_req4_missing_file_destination_is_logged_as_a_warning_not_a_run_failure():
    """REQ-4 verification: a file destination the session never actually wrote to is a
    recorded warning in the run log, not a silently-passed check and not a hard failure
    (the session itself already reported its own per-task status via REQ-2)."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_destination_config(home, dest_rel="out/never-written.txt")
        fake = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc = run_instance(home, runner=fake)
        assert rc == EXIT_OK  # a missing destination artifact doesn't fail the run itself

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "never-written.txt" in text
        assert "warning" in text.lower()


def test_req4_linear_destination_is_flagged_manual_never_silently_passed():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = DESTINATION_CONFIG_YAML.format(
            persona_skill=persona_path, dest_rel="unused"
        ).replace('"file:{dest_rel}"'.format(dest_rel="unused"), '"linear:ENG-9"')
        (home / "config.yaml").write_text(config)
        fake = ChatCapableFakeHermes(chat_stdout="done\nsession_id: x\n")

        rc = run_instance(home, runner=fake)
        assert rc == EXIT_OK

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "manual verification" in text.lower()
        assert "ENG-9" in text  # the specific destination target is named, not generic
