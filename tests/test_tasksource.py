"""REQ-3: prefix-match parsing, not positional "last line" — proves the exact failure
mode §2 recorded (the `-Q` trailing `session_id:` footer) and the fix together."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hourglass.tasksource import is_valid_task_source, parse_next_task_source


def test_parses_next_task_source_ignoring_trailing_session_id_footer():
    stdout = (
        "Here is my answer to the task.\n"
        "NEXT_TASK_SOURCE: /tmp/probe_next_source.txt\n"
        "\n"
        "session_id: abc123\n"
    )
    assert parse_next_task_source(stdout) == "/tmp/probe_next_source.txt"


def test_naive_last_line_would_have_captured_the_footer_instead():
    """Documents the exact bug REQ-3 closes: a positional parser reading stdout's literal
    last non-empty line would get `session_id: abc123`, not the data line."""
    stdout = (
        "NEXT_TASK_SOURCE: /tmp/probe_next_source.txt\n"
        "\n"
        "session_id: abc123\n"
    )
    naive_last_line = [l for l in stdout.splitlines() if l.strip()][-1]
    assert naive_last_line == "session_id: abc123"
    assert parse_next_task_source(stdout) == "/tmp/probe_next_source.txt"


def test_no_update_when_no_matching_line_present():
    assert parse_next_task_source("just an answer\nsession_id: abc123\n") is None


def test_takes_last_match_when_multiple_lines_match():
    stdout = "NEXT_TASK_SOURCE: first\nNEXT_TASK_SOURCE: second\nsession_id: x\n"
    assert parse_next_task_source(stdout) == "second"


def test_valid_task_source_accepts_existing_file_path():
    with tempfile.NamedTemporaryFile() as f:
        assert is_valid_task_source(f.name) is True


def test_valid_task_source_accepts_linear_card_id_shape():
    assert is_valid_task_source("42L-2212") is True


def test_valid_task_source_rejects_empty_or_nonexistent():
    assert is_valid_task_source("") is False
    assert is_valid_task_source("/definitely/not/a/real/path/at/all") is False
