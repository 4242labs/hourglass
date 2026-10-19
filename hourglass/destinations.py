"""REQ-4: each task's output lands at its own declared destination.

The spec (§4) deliberately left "what exact field/API call constitutes a Linear
comment or an email sent" undecided, naming the schema itself as a separate,
not-yet-designed piece. This module is that schema, now fixed concretely:

  destination: "file:<absolute-or-instance-relative-path>"
  destination: "linear:<issue-id-or-identifier>"
  destination: "email:<address>"

A task's `destination` field is one of the three forms above. The wrapper does
not perform the destination write itself — per §3 decision 1, the session is
the only actor with the granted toolset for that destination; the wrapper's
job is (a) tell the session, in the prompt, exactly which tool call and target
satisfies this task's destination, and (b) verify the result where verification
is mechanically possible without inventing an API call this spec doesn't own.

Verification is destination-type-specific per REQ-4's edge case (gated
destination -> draft form instead of live form):
  - file: directly checkable — `path.exists()` after the run.
  - linear / email: no fixed command can verify a live external side effect
    without a corresponding read-back API call this repo doesn't implement;
    verification for these two is `manual_by:` exactly as REQ-4 states, and
    this module says so explicitly rather than silently pretending a check ran.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VALID_KINDS = ("file", "linear", "email")


@dataclass(frozen=True)
class Destination:
    kind: str  # "file" | "linear" | "email"
    target: str  # path string | Linear issue id | email address


class InvalidDestinationError(ValueError):
    pass


def parse(destination: str) -> Destination:
    if not destination or ":" not in destination:
        raise InvalidDestinationError(
            f"destination {destination!r} must be '<kind>:<target>' (kind one of {VALID_KINDS})"
        )
    kind, _, target = destination.partition(":")
    if kind not in VALID_KINDS:
        raise InvalidDestinationError(
            f"unknown destination kind {kind!r} in {destination!r}; must be one of {VALID_KINDS}"
        )
    if not target:
        raise InvalidDestinationError(f"destination {destination!r} has an empty target")
    return Destination(kind=kind, target=target)


def required_toolset(dest: Destination) -> str:
    """The toolset/MCP-server name this destination's live action needs granted
    for the run (REQ-7's per-destination auto-execute ungate operates on this
    name). Declaring the allowlist remains the operator's explicit choice
    (OB-9) — this is only the name to grant, never an auto-inferred grant."""
    return {"file": "file", "linear": "linear", "email": "gmail"}[dest.kind]


def instruction_for(dest: Destination, instance_home: Path) -> str:
    """Prompt text telling the session the exact, concrete action this task's
    destination requires — the fixed schema REQ-4 needed, not a vague label."""
    if dest.kind == "file":
        path = Path(dest.target)
        if not path.is_absolute():
            path = instance_home / path
        return (
            f"This task's output destination is a file. Write the task's full output "
            f"to exactly this path: {path}. Create parent directories if needed."
        )
    if dest.kind == "linear":
        return (
            f"This task's output destination is a Linear comment. Use the `linear` "
            f"MCP tool to add a comment with the task's output to issue '{dest.target}' "
            f"(save_comment or the equivalent comment-creation tool for that issue). "
            f"Do not create a new issue; the target issue already exists."
        )
    if dest.kind == "email":
        return (
            f"This task's output destination is an email. Use the `gmail` MCP tool to "
            f"send an email with the task's output as the body to exactly this address: "
            f"{dest.target}. Use a clear, short subject line summarizing the task."
        )
    raise InvalidDestinationError(f"unhandled destination kind {dest.kind!r}")  # pragma: no cover


def verify(dest: Destination, instance_home: Path) -> Optional[str]:
    """Returns None if verified (or not mechanically verifiable by design), or an
    error string if a verifiable destination's artifact is provably missing.
    REQ-4's edge case (gated destination -> draft form) is the task-list-schema's
    concern, not this check's: this only confirms the destination type's own
    live-form artifact exists where that can be checked without an unowned API."""
    if dest.kind == "file":
        path = Path(dest.target)
        if not path.is_absolute():
            path = instance_home / path
        if not path.exists():
            return f"file destination not found after run: {path}"
        return None
    # linear / email: `manual_by:` per REQ-4 — no read-back API in this repo's scope.
    return None


def requires_manual_verification(dest: Destination) -> bool:
    return dest.kind in ("linear", "email")
