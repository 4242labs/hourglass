"""REQ-2: the run log's required, grep-able format: one line per task,
`- task: <task_id> status: (done|failed|skipped)`.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import List, Literal, Optional

Status = Literal["done", "failed", "skipped"]


class RunLog:
    """Accumulates one run's log lines, then writes them to
    `$INSTANCE_HOME/logs/<run_id>.md` in REQ-2's required format."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._header_lines: List[str] = []
        self._task_lines: List[str] = []
        self._warning_lines: List[str] = []

    def set_header(self, started_at: Optional[_dt.datetime] = None) -> None:
        started_at = started_at or _dt.datetime.now(_dt.timezone.utc)
        self._header_lines = [
            f"# Run {self.run_id}",
            "",
            f"Started: {started_at.isoformat()}",
            "",
        ]

    def record_task(self, task_id: str, status: Status) -> None:
        if status not in ("done", "failed", "skipped"):
            raise ValueError(f"invalid status {status!r}: must be done|failed|skipped")
        self._task_lines.append(f"- task: {task_id} status: ({status})")

    def record_warning(self, message: str) -> None:
        self._warning_lines.append(f"- warning: {message}")

    def write(self, instance_home: Path) -> Path:
        logs_dir = instance_home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"{self.run_id}.md"
        lines = list(self._header_lines) + self._task_lines
        if self._warning_lines:
            lines += [""] + self._warning_lines
        path.write_text("\n".join(lines) + "\n")
        return path
