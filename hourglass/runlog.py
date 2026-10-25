"""REQ-2/REQ-8: the run log's required, grep-able format: one line per task,
`- task: <task_id> status: (done|failed|skipped)`, plus REQ-8's full per-run record
(task id, session id, model, resolved limits, timing, tool_use count, outcome, stop
reason) and the raw events JSONL file, flushed per event as it is written.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

Status = Literal["done", "failed", "skipped"]


class RunLog:
    """Accumulates one run's log lines, then writes them to
    `$INSTANCE_HOME/logs/<run_id>.md` in REQ-2/REQ-8's required format. The events
    JSONL file (`logs/<run_id>.events.jsonl`) is written incrementally by
    `cli_utils.run_agent_streaming` directly, not buffered here."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._header_lines: List[str] = []
        self._task_lines: List[str] = []
        self._warning_lines: List[str] = []
        self._meta_lines: List[str] = []
        self.started_at: Optional[_dt.datetime] = None

    def set_header(self, started_at: Optional[_dt.datetime] = None) -> None:
        self.started_at = started_at or _dt.datetime.now(_dt.timezone.utc)
        self._header_lines = [
            f"# Run {self.run_id}",
            "",
            f"Started: {self.started_at.isoformat()}",
            "",
        ]

    def record_task(self, task_id: str, status: Status) -> None:
        if status not in ("done", "failed", "skipped"):
            raise ValueError(f"invalid status {status!r}: must be done|failed|skipped")
        self._task_lines.append(f"- task: {task_id} status: ({status})")

    def record_warning(self, message: str) -> None:
        self._warning_lines.append(f"- warning: {message}")

    def record_run_detail(
        self,
        *,
        task_id: Optional[str],
        session_id: Optional[str],
        model: Optional[str],
        limits: Dict[str, Any],
        ended_at: _dt.datetime,
        tool_use_count: int,
        outcome: str,
        stop_reason: Optional[str],
    ) -> None:
        """REQ-8: the full per-run record fields, beyond the one-line-per-task summary."""
        started = self.started_at or ended_at
        elapsed = (ended_at - started).total_seconds()
        lines = [
            f"- task_id: {task_id}",
            f"- session_id: {session_id}",
            f"- model: {model}",
            f"- limits: {json.dumps(limits, sort_keys=True)}",
            f"- started: {started.isoformat()}",
            f"- ended: {ended_at.isoformat()}",
            f"- elapsed_seconds: {elapsed:.1f}",
            f"- tool_use_count: {tool_use_count}",
            f"- outcome: {outcome}",
            f"- stop_reason: {stop_reason if stop_reason else '-'}",
        ]
        self._meta_lines.extend(lines)

    def write(self, instance_home: Path) -> Path:
        logs_dir = instance_home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"{self.run_id}.md"
        lines = list(self._header_lines) + self._task_lines
        if self._meta_lines:
            lines += [""] + self._meta_lines
        if self._warning_lines:
            lines += [""] + self._warning_lines
        path.write_text("\n".join(lines) + "\n")
        return path

    def events_path(self, instance_home: Path) -> Path:
        return instance_home / "logs" / f"{self.run_id}.events.jsonl"
