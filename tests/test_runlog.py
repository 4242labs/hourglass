"""REQ-2: run log's required, grep-able format."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from hourglass.runlog import RunLog


def test_write_produces_grep_able_task_lines():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        log = RunLog("run-001")
        log.set_header()
        log.record_task("t1", "done")
        log.record_task("t2", "failed")
        log.record_task("t3", "skipped")
        path = log.write(home)

        text = path.read_text()
        task_lines = [l for l in text.splitlines() if l.startswith("- task:")]
        assert len(task_lines) == 3
        for line in task_lines:
            assert re.search(r"status: \(done|failed|skipped\)$", line)


def test_invalid_status_rejected():
    log = RunLog("run-002")
    try:
        log.record_task("t1", "maybe")  # type: ignore[arg-type]
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_write_path_matches_required_location():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        log = RunLog("run-003")
        log.set_header()
        path = log.write(home)
        assert path == home / "logs" / "run-003.md"
