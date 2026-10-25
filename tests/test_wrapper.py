"""End-to-end orchestration test: wires REQ-1..REQ-8 together against a fake `popen`
(matching cli_utils.run_agent_streaming's contract) and a real config.yaml/instance_home
on disk."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hourglass.cli_utils import CommandResult
from hourglass.wrapper import EXIT_GATE_FAILED, EXIT_OK, EXIT_SESSION_FAILED, run_instance
from tests.fixtures import FakePopen
from tests.test_toolset import FakeHermes


def _write_persona_fixture(home: Path) -> Path:
    """A standalone persona file, not under HERMES_HOME — mirrors a real crew persona
    (e.g. tortuga/crew/pessoa/pessoa.md) that lives entirely outside Hermes's own tree."""
    persona_path = home / "fixture-persona.md"
    persona_path.write_text("# Agent: FIXTURE\n\nA minimal persona for wrapper e2e tests.\n")
    return persona_path


LIMITS_YAML = """limits:
  run_timeout_seconds: 1200
  max_turns: 50
  stall_seconds: 420
  grace_seconds: 30
"""


def _fake_popen_result(text="done", session_id="x", returncode=0):
    """Build a `popen` callable returning one init event + one result event."""
    events = [
        '{"type": "system", "subtype": "init", "model": "fake-model", "session_id": "%s"}' % session_id,
        '{"type": "result", "session_id": "%s", "text": %s}' % (session_id, _json_str(text)),
    ]

    def fake_popen(args, **kwargs):
        fake_popen.last_args = list(args)
        fake_popen.last_env = kwargs.get("env")
        return FakePopen(events, returncode=returncode)

    fake_popen.last_args = None
    fake_popen.last_env = None
    return fake_popen


def _json_str(s: str) -> str:
    import json

    return json.dumps(s)


CONFIG_YAML = (
    """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
"""
    + LIMITS_YAML
    + """
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""
)


def _write_config(home: Path, extra: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    (home / "config.yaml").write_text(CONFIG_YAML.format(persona_skill=persona_path) + extra)


TASKS_CONFIG_YAML = (
    """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
"""
    + LIMITS_YAML
    + """
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
"""
)


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
        fake_popen = _fake_popen_result(text="done")

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        task_lines = [l for l in text.splitlines() if l.startswith("- task:")]
        assert len(task_lines) == 1  # matches the resolved task list's count exactly
        for line in task_lines:
            assert re.search(r"status: \(done|failed|skipped\)$", line)


def test_req6_state_file_is_written_back_and_hash_changes_each_run():
    """REQ-6 acceptance: an instance with state_file set must have its hash change after
    each of two runs (run_id embedded each time)."""
    import hashlib

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        state_path = home / "state" / "dedup.state"
        _write_tasks_config(home, state_file=f'"{state_path}"')

        rc1 = run_instance(home, runner=FakeHermes(), popen=_fake_popen_result())
        assert rc1 == EXIT_OK
        assert state_path.exists()
        hash1 = hashlib.md5(state_path.read_bytes()).hexdigest()

        rc2 = run_instance(home, runner=FakeHermes(), popen=_fake_popen_result())
        assert rc2 == EXIT_OK
        hash2 = hashlib.md5(state_path.read_bytes()).hexdigest()

        assert hash1 != hash2  # changed both times, per REQ-6 acceptance


def test_chat_invocation_carries_skills_flag_for_persona():
    """Hourglass must actually load the persona via --skills, not just label it in the
    prompt — the bug this fix addresses."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)

        assert rc == EXIT_OK
        args = fake_popen.last_args
        assert "--skills" in args
        skills_idx = args.index("--skills")
        skill_name = args[skills_idx + 1]
        assert skill_name.startswith("aa-persona-")


def test_clean_run_returns_ok_and_writes_log():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        fake_popen = _fake_popen_result(text="done\nNEXT_TASK_SOURCE: 42L-9999")

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)

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
        fake_popen = _fake_popen_result(text="NEXT_TASK_SOURCE: 42L-9999", returncode=1)

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)

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

        popen_calls = []

        def fail_if_called(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("popen must not be invoked after a gate failure")

        rc = run_instance(home, runner=broken_enumerate, popen=fail_if_called)
        assert rc == EXIT_GATE_FAILED
        assert popen_calls == []


def test_second_concurrent_run_is_skipped_not_double_invoked():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_config(home)
        popen_calls = []

        def fail_if_called(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("popen must not be invoked while the lock is held")

        from hourglass.locking import instance_lock
        results = []
        with instance_lock(home):
            # A second invocation while the lock is held must be skipped, never double-run.
            rc = run_instance(home, runner=FakeHermes(), popen=fail_if_called)
            results.append(rc)
        assert results == [75]  # EXIT_LOCKED
        assert popen_calls == []


DESTINATION_CONFIG_YAML = (
    """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
"""
    + LIMITS_YAML
    + """
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
)


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
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK

        args = fake_popen.last_args
        q_idx = args.index("-q")
        prompt = args[q_idx + 1]
        assert str(home / "out" / "result.txt") in prompt
        assert "destination" in prompt.lower()


def test_req4_missing_file_destination_is_logged_as_a_warning_not_a_run_failure():
    """REQ-4 verification: a file destination the session never actually wrote to is a
    recorded warning in the run log, not a silently-passed check and not a hard failure
    (the session itself already reported its own per-task status via REQ-2)."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_destination_config(home, dest_rel="out/never-written.txt")
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
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
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "manual verification" in text.lower()
        assert "ENG-9" in text  # the specific destination target is named, not generic


def test_req4_linear_issue_destination_reaches_prompt_and_is_flagged_manual():
    """`linear-issue:<team>` (one new issue per output item): the session must be told to
    create new issues in that team, and the run log must flag it manual, never passed."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = DESTINATION_CONFIG_YAML.format(
            persona_skill=persona_path, dest_rel="unused"
        ).replace('"file:unused"', '"linear-issue:42L"')
        (home / "config.yaml").write_text(config)
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK

        args = fake_popen.last_args
        prompt = args[args.index("-q") + 1]
        assert "save_issue" in prompt
        assert "team '42L'" in prompt

        text = next((home / "logs").glob("*.md")).read_text()
        assert "manual verification" in text.lower()
        assert "linear-issue:42L" in text


def test_req4_malformed_destination_is_skipped_in_prompt_and_logged_as_a_warning():
    """New try/except path this PR introduces (wrapper.py's prompt-building and post-run
    verification loops): a malformed destination string must not crash the run, must not
    appear as a silent no-op instruction in the prompt, and must surface as an explicit
    warning in the run log so it's never mistaken for a passed check."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = DESTINATION_CONFIG_YAML.format(
            persona_skill=persona_path, dest_rel="unused"
        ).replace('"file:{dest_rel}"'.format(dest_rel="unused"), '"not-a-valid-destination"')
        (home / "config.yaml").write_text(config)
        fake_popen = _fake_popen_result()

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK  # a malformed destination doesn't fail the run itself

        args = fake_popen.last_args
        q_idx = args.index("-q")
        prompt = args[q_idx + 1]
        assert "not-a-valid-destination" not in prompt  # no destination instruction leaked in

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "invalid destination" in text.lower()
        assert "not-a-valid-destination" in text
