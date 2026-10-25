"""Instance configuration: persona, task source, declared toolset allowlist, optional
state file, and the resolved task list. Loaded from `$INSTANCE_HOME/config.yaml`.

The wrapper/task-list schema itself is out of scope per the spec's §4 — this module reads
whatever minimal shape the config needs to carry for the mechanism (persona, task_source,
allowlist, state_file) without inventing the full task-list schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - fallback keeps this module import-safe without PyYAML
    yaml = None


class LimitsValidationError(ValueError):
    """Raised when a `limits` block (instance- or task-level) is missing a required
    field, has a non-positive/non-integer value, or violates a stated inequality. The
    caller must abort before any toolset or agent change (REQ-7)."""


@dataclass(frozen=True)
class Limits:
    run_timeout_seconds: int
    max_turns: int
    stall_seconds: int
    grace_seconds: int = 30  # Kubernetes' own terminationGracePeriodSeconds default (§2 citation)


_REQUIRED_LIMITS_FIELDS = ("run_timeout_seconds", "max_turns", "stall_seconds")


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_limits(data: Dict[str, Any], context: str) -> None:
    for field_name in _REQUIRED_LIMITS_FIELDS:
        if field_name not in data:
            raise LimitsValidationError(f"{context}: missing required limits field '{field_name}'")
        if not _is_positive_int(data[field_name]):
            raise LimitsValidationError(
                f"{context}: limits field '{field_name}' must be a positive integer, got {data[field_name]!r}"
            )
    if "grace_seconds" in data and not _is_positive_int(data["grace_seconds"]):
        raise LimitsValidationError(
            f"{context}: limits field 'grace_seconds' must be a positive integer, got {data['grace_seconds']!r}"
        )
    run_timeout = data["run_timeout_seconds"]
    stall = data["stall_seconds"]
    if stall >= run_timeout:
        raise LimitsValidationError(
            f"{context}: stall_seconds ({stall}) must be < run_timeout_seconds ({run_timeout})"
        )
    grace = data.get("grace_seconds", 30)
    if grace + 3 >= run_timeout:
        raise LimitsValidationError(
            f"{context}: grace_seconds ({grace}) + 3 must be < run_timeout_seconds ({run_timeout})"
        )


@dataclass
class Task:
    task_id: str
    destination: Optional[str] = None  # toolset or MCP server name this task's output uses
    auto_execute: bool = False
    prompt: str = ""
    limits_override: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceConfig:
    instance_home: Path
    profile: str
    persona_skill: str
    task_source: str
    limits: Limits
    allowlist_builtin: Set[str] = field(default_factory=set)
    allowlist_plugin: Set[str] = field(default_factory=set)
    allowlist_mcp: Dict[str, Set[str]] = field(default_factory=dict)
    gated_live_tools: Dict[str, Set[str]] = field(default_factory=dict)
    state_file: Optional[Path] = None
    tasks: List[Task] = field(default_factory=list)

    @property
    def config_path(self) -> Path:
        return self.instance_home / "config.yaml"

    def set_task_source(self, value: str) -> None:
        self.task_source = value
        self._persist()

    def effective_limits(self, task: Optional[Task]) -> Limits:
        """REQ-7: a per-task `limits` override wins field-by-field over the instance
        default; re-validated only when an override is actually present (the
        instance-level block is already validated at load time)."""
        merged: Dict[str, Any] = {
            "run_timeout_seconds": self.limits.run_timeout_seconds,
            "max_turns": self.limits.max_turns,
            "stall_seconds": self.limits.stall_seconds,
            "grace_seconds": self.limits.grace_seconds,
        }
        if task is not None and task.limits_override:
            merged.update(task.limits_override)
            _validate_limits(merged, context=f"task '{task.task_id}' effective limits")
        return Limits(**merged)

    def _persist(self) -> None:
        data = _to_raw(self)
        _write_config(self.config_path, data)


def load(instance_home: Path) -> InstanceConfig:
    path = instance_home / "config.yaml"
    raw = _read_config(path)
    return _from_raw(instance_home, raw)


def _read_config(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    if yaml is not None:
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _write_config(path: Path, data: Dict[str, Any]) -> None:
    if yaml is not None:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    else:
        path.write_text(json.dumps(data, indent=2))


def _from_raw(instance_home: Path, raw: Dict[str, Any]) -> InstanceConfig:
    allow = raw.get("allowlist", {}) or {}
    mcp_raw = allow.get("mcp", {}) or {}
    gated_raw = raw.get("gated_live_tools", {}) or {}
    tasks_raw = raw.get("tasks", []) or []
    state_file = raw.get("state_file")

    limits_raw = raw.get("limits")
    if limits_raw is None:
        raise LimitsValidationError("instance config: missing required 'limits' block")
    _validate_limits(limits_raw, context="instance limits")
    limits = Limits(
        run_timeout_seconds=limits_raw["run_timeout_seconds"],
        max_turns=limits_raw["max_turns"],
        stall_seconds=limits_raw["stall_seconds"],
        grace_seconds=limits_raw.get("grace_seconds", 30),
    )

    return InstanceConfig(
        instance_home=instance_home,
        profile=raw["profile"],
        persona_skill=raw["persona_skill"],
        task_source=raw["task_source"],
        limits=limits,
        allowlist_builtin=set(allow.get("builtin", [])),
        allowlist_plugin=set(allow.get("plugin", [])),
        allowlist_mcp={k: set(v) for k, v in mcp_raw.items()},
        gated_live_tools={k: set(v) for k, v in gated_raw.items()},
        state_file=Path(state_file) if state_file else None,
        tasks=[
            Task(
                task_id=t["task_id"],
                destination=t.get("destination"),
                auto_execute=bool(t.get("auto_execute", False)),
                prompt=t.get("prompt", ""),
                limits_override=dict(t.get("limits") or {}),
            )
            for t in tasks_raw
        ],
    )


def _to_raw(cfg: InstanceConfig) -> Dict[str, Any]:
    return {
        "profile": cfg.profile,
        "persona_skill": cfg.persona_skill,
        "task_source": cfg.task_source,
        "limits": {
            "run_timeout_seconds": cfg.limits.run_timeout_seconds,
            "max_turns": cfg.limits.max_turns,
            "stall_seconds": cfg.limits.stall_seconds,
            "grace_seconds": cfg.limits.grace_seconds,
        },
        "allowlist": {
            "builtin": sorted(cfg.allowlist_builtin),
            "plugin": sorted(cfg.allowlist_plugin),
            "mcp": {k: sorted(v) for k, v in cfg.allowlist_mcp.items()},
        },
        "gated_live_tools": {k: sorted(v) for k, v in cfg.gated_live_tools.items()},
        "state_file": str(cfg.state_file) if cfg.state_file else None,
        "tasks": [
            {
                "task_id": t.task_id,
                "destination": t.destination,
                "auto_execute": t.auto_execute,
                "prompt": t.prompt,
                "limits": dict(t.limits_override) if t.limits_override else None,
            }
            for t in cfg.tasks
        ],
    }


def task_destinations_ungated(tasks: List[Task]) -> Dict[str, bool]:
    """REQ-7 step 4: run-scoped ungate. For each destination's required toolset touched by
    any task this run, True iff *any* task touching it has auto_execute: true (OR across
    the whole run, not per task — spec's explicit edge case).

    `task.destination` is REQ-4's fixed schema (`<kind>:<target>`, see destinations.py);
    this resolves each one to the toolset/MCP-server name the gate actually keys on
    (destinations.required_toolset), never the raw destination string itself.
    """
    from . import destinations as dest_mod  # local import: avoids a cycle at module load

    result: Dict[str, bool] = {}
    for task in tasks:
        if not task.destination:
            continue
        try:
            dest = dest_mod.parse(task.destination)
            toolset_name = dest_mod.required_toolset(dest)
        except dest_mod.InvalidDestinationError:
            # An invalid destination string is surfaced by wrapper.py's own REQ-4
            # verification pass after the run; it must not crash gate computation
            # before the run even starts, nor silently grant anything for it here.
            continue
        result[toolset_name] = result.get(toolset_name, False) or task.auto_execute
    return result
