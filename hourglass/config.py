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


@dataclass
class Task:
    task_id: str
    destination: Optional[str] = None  # toolset or MCP server name this task's output uses
    auto_execute: bool = False
    prompt: str = ""


@dataclass
class InstanceConfig:
    instance_home: Path
    profile: str
    persona_skill: str
    task_source: str
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
    return InstanceConfig(
        instance_home=instance_home,
        profile=raw["profile"],
        persona_skill=raw["persona_skill"],
        task_source=raw["task_source"],
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
            )
            for t in tasks_raw
        ],
    )


def _to_raw(cfg: InstanceConfig) -> Dict[str, Any]:
    return {
        "profile": cfg.profile,
        "persona_skill": cfg.persona_skill,
        "task_source": cfg.task_source,
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
