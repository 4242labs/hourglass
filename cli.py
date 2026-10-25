#!/usr/bin/env python3
"""Dagu entry point: `python cli.py --instance-home /path/to/instance [--task T | --ssh-task]`.

Exit code is forwarded to Dagu unmodified — 0 on a clean run, non-zero on a gate failure,
a session failure, or a legitimate skip (a prior run for this instance still active).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from hourglass.wrapper import EXIT_GATE_FAILED, run_instance

_TASK_ID_RE = re.compile(r"^[a-z0-9-]+$")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hourglass wrapper: one scheduled run.")
    parser.add_argument(
        "--instance-home",
        required=True,
        type=Path,
        help="directory holding this instance's config.yaml, logs/, and scripts/",
    )
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument(
        "--task",
        default=None,
        help="task id to run (required when the instance declares more than one task)",
    )
    task_group.add_argument(
        "--ssh-task",
        action="store_true",
        help="read the task id from the SSH_ORIGINAL_COMMAND environment variable "
        "(set by a forced-command SSH key); the value must match [a-z0-9-]",
    )
    args = parser.parse_args(argv)

    task_id = args.task
    if args.ssh_task:
        raw = os.environ.get("SSH_ORIGINAL_COMMAND")
        if not raw or not _TASK_ID_RE.match(raw.strip()):
            print(f"hourglass: --ssh-task: SSH_ORIGINAL_COMMAND is unset or invalid: {raw!r}")
            return EXIT_GATE_FAILED
        task_id = raw.strip()
    elif task_id is not None and not _TASK_ID_RE.match(task_id):
        print(f"hourglass: --task: invalid task id {task_id!r} (must match [a-z0-9-]+)")
        return EXIT_GATE_FAILED

    return run_instance(args.instance_home, task_id=task_id)


if __name__ == "__main__":
    sys.exit(main())
