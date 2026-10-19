"""Instance config load/persist round-trip, and REQ-7 step 4's run-scoped ungate rule."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hourglass import config as config_mod
from hourglass.config import Task, task_destinations_ungated


CONFIG_YAML = """
profile: hg-test-instance
persona_skill: macgyver
task_source: 42L-2212
allowlist:
  builtin: [file]
  plugin: []
  mcp:
    linear: [create_comment, get_issue]
gated_live_tools:
  linear: [create_comment]
state_file: null
tasks:
  - task_id: t1
    destination: linear
    auto_execute: false
    prompt: do the thing
"""


def test_load_round_trip():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "config.yaml").write_text(CONFIG_YAML)
        cfg = config_mod.load(home)

        assert cfg.profile == "hg-test-instance"
        assert cfg.allowlist_builtin == {"file"}
        assert cfg.allowlist_mcp == {"linear": {"create_comment", "get_issue"}}
        assert cfg.gated_live_tools == {"linear": {"create_comment"}}
        assert cfg.state_file is None
        assert len(cfg.tasks) == 1
        assert cfg.tasks[0].task_id == "t1"


def test_set_task_source_persists_and_does_not_affect_current_run_tasks():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "config.yaml").write_text(CONFIG_YAML)
        cfg = config_mod.load(home)
        original_tasks = list(cfg.tasks)

        cfg.set_task_source("/tmp/next.txt")

        reloaded = config_mod.load(home)
        assert reloaded.task_source == "/tmp/next.txt"
        # REQ-3: current run's own resolved task list is never affected by its own update.
        assert cfg.tasks == original_tasks


def test_task_destinations_ungated_is_or_across_the_run():
    """REQ-7 edge case: mixing auto_execute true/false for the same destination in one
    run ungates for the whole run (OR, not per-task). Destinations use REQ-4's fixed
    `<kind>:<target>` schema and resolve to their required toolset/MCP-server name."""
    tasks = [
        Task(task_id="a", destination="linear:ENG-1", auto_execute=False),
        Task(task_id="b", destination="linear:ENG-2", auto_execute=True),
        Task(task_id="c", destination="email:ops@example.com", auto_execute=False),
    ]
    result = task_destinations_ungated(tasks)
    assert result == {"linear": True, "gmail": False}


def test_task_destinations_ungated_ignores_tasks_with_no_destination():
    tasks = [Task(task_id="a", destination=None, auto_execute=True)]
    assert task_destinations_ungated(tasks) == {}


def test_task_destinations_ungated_silently_skips_a_malformed_destination():
    """New try/except path this PR introduces: a malformed destination string must not
    crash gate computation before the run even starts, nor silently grant anything for
    it — it's surfaced instead by wrapper.py's post-run REQ-4 verification pass."""
    tasks = [
        Task(task_id="a", destination="not-a-valid-destination", auto_execute=True),
        Task(task_id="b", destination="linear:ENG-1", auto_execute=False),
    ]
    result = task_destinations_ungated(tasks)
    assert result == {"linear": False}  # malformed entry contributes nothing
