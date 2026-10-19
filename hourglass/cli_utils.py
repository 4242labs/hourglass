"""Thin subprocess wrapper around the `hermes` CLI.

Isolated here so every other module can be unit-tested without ever invoking a real
subprocess: tests inject a fake `runner` callable with the same signature as `run`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(args: Sequence[str], timeout: Optional[float] = 60.0) -> CommandResult:
    """Run `args` (argv, no shell) and capture output. Never raises on a non-zero exit —
    callers decide what a given exit code means for their own command."""
    proc = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        args=list(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def hermes_args(profile: Optional[str], *rest: str) -> List[str]:
    """Build a `hermes [--profile X] <rest...>` argv list."""
    args = ["hermes"]
    if profile:
        args += ["--profile", profile]
    args += list(rest)
    return args
