"""REQ-7: the `limits` block is required and validated before any toolset or agent
change — missing fields, non-positive/non-integer values, and the two inequalities
(stall_seconds < run_timeout_seconds; grace_seconds + 3 < run_timeout_seconds) all
raise `LimitsValidationError` at load/override time, never a crash mid-run."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hourglass import config as config_mod
from hourglass.config import LimitsValidationError, Task


BASE_YAML = """
profile: hg-test
persona_skill: macgyver
task_source: 42L-1
limits:
{limits_block}
allowlist:
  builtin: []
  plugin: []
  mcp: {{}}
gated_live_tools: {{}}
state_file: null
tasks: []
"""


def _load_with_limits(limits_yaml: str):
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "config.yaml").write_text(BASE_YAML.format(limits_block=limits_yaml))
        return config_mod.load(home)


def test_missing_limits_block_raises():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "config.yaml").write_text(
            "profile: hg-test\npersona_skill: m\ntask_source: t\n"
            "allowlist: {builtin: [], plugin: [], mcp: {}}\n"
            "gated_live_tools: {}\nstate_file: null\ntasks: []\n"
        )
        with pytest.raises(LimitsValidationError, match="missing required 'limits' block"):
            config_mod.load(home)


@pytest.mark.parametrize("field_name", ["run_timeout_seconds", "max_turns", "stall_seconds"])
def test_missing_required_field_raises(field_name):
    fields = {"run_timeout_seconds": 1200, "max_turns": 50, "stall_seconds": 420}
    del fields[field_name]
    limits_yaml = "\n".join(f"  {k}: {v}" for k, v in fields.items())
    with pytest.raises(LimitsValidationError, match=f"missing required limits field '{field_name}'"):
        _load_with_limits(limits_yaml)


@pytest.mark.parametrize("bad_value", ["0", "-5", "1.5", "true", "'abc'"])
def test_non_positive_or_non_integer_value_raises(bad_value):
    limits_yaml = f"  run_timeout_seconds: {bad_value}\n  max_turns: 50\n  stall_seconds: 420\n"
    with pytest.raises(LimitsValidationError, match="must be a positive integer"):
        _load_with_limits(limits_yaml)


def test_stall_seconds_must_be_less_than_run_timeout_seconds():
    limits_yaml = "  run_timeout_seconds: 400\n  max_turns: 50\n  stall_seconds: 400\n"
    with pytest.raises(LimitsValidationError, match="stall_seconds"):
        _load_with_limits(limits_yaml)


def test_grace_seconds_plus_3_must_be_less_than_run_timeout_seconds():
    limits_yaml = (
        "  run_timeout_seconds: 33\n  max_turns: 50\n  stall_seconds: 10\n  grace_seconds: 30\n"
    )
    with pytest.raises(LimitsValidationError, match="grace_seconds"):
        _load_with_limits(limits_yaml)


def test_grace_seconds_defaults_to_30_when_absent():
    limits_yaml = "  run_timeout_seconds: 1200\n  max_turns: 50\n  stall_seconds: 420\n"
    cfg = _load_with_limits(limits_yaml)
    assert cfg.limits.grace_seconds == 30


def test_valid_limits_block_loads_cleanly():
    limits_yaml = (
        "  run_timeout_seconds: 1200\n  max_turns: 50\n  stall_seconds: 420\n  grace_seconds: 30\n"
    )
    cfg = _load_with_limits(limits_yaml)
    assert cfg.limits.run_timeout_seconds == 1200
    assert cfg.limits.max_turns == 50
    assert cfg.limits.stall_seconds == 420
    assert cfg.limits.grace_seconds == 30


def test_task_level_override_field_by_field_win():
    """A per-task override only needs to carry the field(s) it changes; the rest are
    inherited from the instance default before re-validation."""
    limits_yaml = (
        "  run_timeout_seconds: 1200\n  max_turns: 50\n  stall_seconds: 420\n  grace_seconds: 30\n"
    )
    cfg = _load_with_limits(limits_yaml)
    task = Task(task_id="t1", limits_override={"max_turns": 10})
    effective = cfg.effective_limits(task)
    assert effective.max_turns == 10
    assert effective.run_timeout_seconds == 1200  # inherited, unchanged


def test_task_level_override_invalid_inequality_raises():
    limits_yaml = (
        "  run_timeout_seconds: 1200\n  max_turns: 50\n  stall_seconds: 420\n  grace_seconds: 30\n"
    )
    cfg = _load_with_limits(limits_yaml)
    task = Task(task_id="t1", limits_override={"stall_seconds": 1200})
    with pytest.raises(LimitsValidationError, match="stall_seconds"):
        cfg.effective_limits(task)


def test_no_override_returns_instance_default_without_revalidating():
    limits_yaml = (
        "  run_timeout_seconds: 1200\n  max_turns: 50\n  stall_seconds: 420\n  grace_seconds: 30\n"
    )
    cfg = _load_with_limits(limits_yaml)
    task = Task(task_id="t1")
    effective = cfg.effective_limits(task)
    assert effective == cfg.limits
