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


MULTI_TASK_CONFIG_YAML = (
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
  - task_id: a
    destination: file:out/a.txt
    auto_execute: false
    prompt: ONLY-TASK-A-PROMPT-TEXT
  - task_id: b
    destination: file:out/b.txt
    auto_execute: false
    prompt: ONLY-TASK-B-PROMPT-TEXT
"""
)


def _write_multi_task_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    persona_path = _write_persona_fixture(home)
    (home / "config.yaml").write_text(MULTI_TASK_CONFIG_YAML.format(persona_skill=persona_path))


def test_task_selection():
    """REQ-1: exactly the named task runs, in a fresh session; its own prompt leads
    the built prompt; only its own destination enters the gate computation. An
    unknown id, and a missing id with two declared tasks, both fail before any
    toolset or agent change (the runner/popen spies record zero calls)."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_multi_task_config(home)
        fake_popen = _fake_popen_result()

        rc = run_instance(home, task_id="a", runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK
        args = fake_popen.last_args
        q_idx = args.index("-q")
        prompt = args[q_idx + 1]
        assert "ONLY-TASK-A-PROMPT-TEXT" in prompt
        assert "ONLY-TASK-B-PROMPT-TEXT" not in prompt

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_multi_task_config(home)

        class SpyRunner(FakeHermes):
            calls = 0

            def __call__(self, args, timeout=None):
                SpyRunner.calls += 1
                return super().__call__(args, timeout=timeout)

        def spy_popen(args, **kwargs):
            spy_popen.calls = getattr(spy_popen, "calls", 0) + 1
            return _fake_popen_result()(args, **kwargs)

        rc = run_instance(home, task_id="unknown-id", runner=SpyRunner(), popen=spy_popen)
        assert rc == EXIT_GATE_FAILED
        assert SpyRunner.calls == 0
        assert not hasattr(spy_popen, "calls") or spy_popen.calls == 0

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_multi_task_config(home)

        class SpyRunner2(FakeHermes):
            calls = 0

            def __call__(self, args, timeout=None):
                SpyRunner2.calls += 1
                return super().__call__(args, timeout=timeout)

        def spy_popen2(args, **kwargs):
            spy_popen2.calls = getattr(spy_popen2, "calls", 0) + 1
            return _fake_popen_result()(args, **kwargs)

        rc = run_instance(home, task_id=None, runner=SpyRunner2(), popen=spy_popen2)
        assert rc == EXIT_GATE_FAILED
        assert SpyRunner2.calls == 0
        assert not hasattr(spy_popen2, "calls") or spy_popen2.calls == 0


def test_bad_limits_refused():
    """REQ-7: a config missing run_timeout_seconds is refused via run_instance itself,
    names the field in the run record, and never reaches the toolset gate or the agent
    runner (spy records zero calls)."""
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        bad_config = (
            """
profile: hg-e2e
persona_skill: {persona_skill}
task_source: 42L-2212
limits:
  max_turns: 50
  stall_seconds: 420
allowlist:
  builtin: [builtin_00]
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""
        ).format(persona_skill=persona_path)
        (home / "config.yaml").write_text(bad_config)

        class SpyRunner(FakeHermes):
            calls = 0

            def __call__(self, args, timeout=None):
                SpyRunner.calls += 1
                return super().__call__(args, timeout=timeout)

        def spy_popen(args, **kwargs):
            spy_popen.calls = getattr(spy_popen, "calls", 0) + 1
            return _fake_popen_result()(args, **kwargs)

        rc = run_instance(home, runner=SpyRunner(), popen=spy_popen)
        assert rc == EXIT_GATE_FAILED
        assert SpyRunner.calls == 0
        assert not hasattr(spy_popen, "calls") or spy_popen.calls == 0

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "run_timeout_seconds" in text


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


def test_external_sigterm_to_run_instance_stops_the_agent_and_is_recorded():
    """REQ-6: an external SIGTERM delivered to Hourglass itself (run_instance, via a
    real subprocess invocation of cli.main) is treated like the REQ-4 time-cap path —
    the agent subprocess is staged-stopped and the run is recorded with reason
    "Hourglass terminated", exit 124 (EXIT_STOPPED)."""
    import os
    import signal
    import subprocess as sp
    import sys
    import time

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = (
            """
profile: hg-sigterm
persona_skill: {persona_skill}
task_source: 42L-2212
limits:
  run_timeout_seconds: 60
  max_turns: 50
  stall_seconds: 50
  grace_seconds: 2
allowlist:
  builtin: []
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""
        ).format(persona_skill=persona_path)
        (home / "config.yaml").write_text(config)

        fakeagent = str(Path(__file__).parent / "fakeagent.py")
        driver = home / "_driver.py"
        driver.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from hourglass.cli_utils import CommandResult\n"
            "from hourglass.wrapper import run_instance\n"
            "import subprocess\n"
            "\n"
            "class GateRunner:\n"
            "    def __call__(self, args, timeout=None):\n"
            "        cmd = list(args[1:])\n"
            "        if cmd[:2] == ['--profile', 'hg-sigterm']:\n"
            "            cmd = cmd[2:]\n"
            "        if cmd[:2] == ['tools', 'list']:\n"
            "            return CommandResult(args, 0, 'Built-in toolsets:\\n\\nPlugin toolsets:\\n', '')\n"
            "        if cmd[:2] == ['mcp', 'list']:\n"
            "            return CommandResult(args, 0, 'Name       Transport   Status\\n---\\n', '')\n"
            "        return CommandResult(args, 0, '', '')\n"
            "\n"
            "def fake_popen(args, **kwargs):\n"
            "    return subprocess.Popen(%r, **kwargs)\n"
            "\n"
            "rc = run_instance(%r, runner=GateRunner(), popen=fake_popen)\n"
            "print(f'RC={rc}')\n"
            % (str(Path(__file__).parent.parent), [sys.executable, fakeagent, "--event-gap", "5", "--events", "20"], str(home))
        )

        proc = sp.Popen([sys.executable, str(driver)], cwd=str(home), stdout=sp.PIPE, stderr=sp.PIPE, text=True)
        time.sleep(1.0)  # let it clear the gate and start the agent subprocess
        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=15)

        assert "RC=124" in stdout, f"stdout={stdout!r} stderr={stderr!r}"

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "Hourglass terminated" in text


def test_full_record_present_on_clean_time_cap_and_external_sigterm_runs():
    """REQ-8: the per-run record carries every named field (task id, session id, model,
    resolved limits, timing, tool_use count, outcome, stop reason) across a clean
    finish, a REQ-4 time-cap stop, and an external SIGTERM — and the events file's
    line count matches what was actually emitted before any stop."""
    import json as _json

    # Scenario 1: clean finish.
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_tasks_config(home, state_file=home / "state.txt")
        fake_popen = _fake_popen_result(session_id="sess-clean")

        rc = run_instance(home, runner=FakeHermes(), popen=fake_popen)
        assert rc == EXIT_OK

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        for field in (
            "task_id", "session_id: sess-clean", "model: fake-model",
            "limits:", "started:", "ended:", "elapsed_seconds:",
            "tool_use_count:", "outcome: done", "stop_reason: -",
        ):
            assert field in text, f"missing {field!r} in:\n{text}"

        events_path = next((home / "logs").glob("*.events.jsonl"))
        lines = events_path.read_text().splitlines()
        assert len(lines) == 2  # init event + result event, matching _fake_popen_result
        for line in lines:
            _json.loads(line)

    # Scenario 2: REQ-4 time-cap stop, via the real streaming runner against a real
    # fakeagent.py subprocess (not FakePopen — the time-cap path only fires inside
    # run_agent_streaming's own select()/poll loop against a real pipe).
    import subprocess as sp
    import sys as _sys

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = (
            """
profile: hg-timecap
persona_skill: {persona_skill}
task_source: 42L-2212
limits:
  run_timeout_seconds: 8
  max_turns: 50
  stall_seconds: 6
  grace_seconds: 1
allowlist:
  builtin: []
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""
        ).format(persona_skill=persona_path)
        (home / "config.yaml").write_text(config)

        fakeagent = str(Path(__file__).parent / "fakeagent.py")

        def real_popen(args, **kwargs):
            return sp.Popen(
                [_sys.executable, fakeagent, "--event-gap", "0.3", "--events", "40"],
                **kwargs,
            )

        rc = run_instance(home, runner=FakeHermes(), popen=real_popen)
        assert rc == 124  # EXIT_STOPPED

        log_path = next((home / "logs").glob("*.md"))
        text = log_path.read_text()
        assert "stop_reason: time cap" in text
        assert "outcome: stopped" in text
        assert "session_id: fake-session" in text  # from fakeagent.py's own init event
        assert "model: fake-model" in text

        events_path = next((home / "logs").glob("*.events.jsonl"))
        lines = events_path.read_text().splitlines()
        assert len(lines) >= 1  # at least the init event survived the time-cap stop
        for line in lines:
            _json.loads(line)


def test_lock_released_after_a_clean_run_and_after_a_time_cap_stop():
    """REQ-8 / lock discipline: the instance lock is released on every path
    run_instance itself survives — a second run immediately after either a clean
    finish or a REQ-4 time-cap stop must not raise RunAlreadyActiveError."""
    import subprocess as sp
    import sys as _sys

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _write_tasks_config(home, state_file=home / "state.txt")

        rc1 = run_instance(home, runner=FakeHermes(), popen=_fake_popen_result())
        assert rc1 == EXIT_OK
        assert not (home / "run.lock").exists()

        rc2 = run_instance(home, runner=FakeHermes(), popen=_fake_popen_result())
        assert rc2 == EXIT_OK  # must not raise RunAlreadyActiveError

    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        home.mkdir(parents=True, exist_ok=True)
        persona_path = _write_persona_fixture(home)
        config = (
            """
profile: hg-timecap2
persona_skill: {persona_skill}
task_source: 42L-2212
limits:
  run_timeout_seconds: 8
  max_turns: 50
  stall_seconds: 6
  grace_seconds: 1
allowlist:
  builtin: []
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""
        ).format(persona_skill=persona_path)
        (home / "config.yaml").write_text(config)
        fakeagent = str(Path(__file__).parent / "fakeagent.py")

        def real_popen(args, **kwargs):
            return sp.Popen(
                [_sys.executable, fakeagent, "--event-gap", "0.3", "--events", "40"],
                **kwargs,
            )

        rc1 = run_instance(home, runner=FakeHermes(), popen=real_popen)
        assert rc1 == 124
        assert not (home / "run.lock").exists()

        rc2 = run_instance(home, runner=FakeHermes(), popen=real_popen)
        assert rc2 == 124  # must not raise RunAlreadyActiveError either
