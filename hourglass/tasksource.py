"""REQ-3: parse a session's captured stdout for a `NEXT_TASK_SOURCE:` proposal, by an
explicit prefix match — never by positional "last line", because `hermes chat -Q` always
appends a trailing `session_id: <id>` footer after the agent's own answer (spec §2)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_PATTERN = re.compile(r"^NEXT_TASK_SOURCE: (.+)$")

_LINEAR_CARD_RE = re.compile(r"^[A-Z0-9]+-\d+$")


def parse_next_task_source(stdout: str) -> Optional[str]:
    """Return the captured value of the last line matching `^NEXT_TASK_SOURCE: (.+)$`,
    or None if no line matches. The trailing `session_id:` footer line never matches this
    pattern, so it is never mistaken for the data line regardless of its position."""
    matches = [m.group(1).strip() for m in map(_PATTERN.match, stdout.splitlines()) if m]
    return matches[-1] if matches else None


def is_valid_task_source(value: str) -> bool:
    """A proposed task-source value must be either an existing file path or a
    Linear-card-ID-shaped string (`<TEAM>-<number>`). Anything else (empty, unparseable)
    is treated as no update per REQ-3's edge case."""
    if not value:
        return False
    if _LINEAR_CARD_RE.match(value):
        return True
    return Path(value).exists()
