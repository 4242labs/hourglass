#!/usr/bin/env python3
"""Dagu entry point: `python cli.py --instance-home /path/to/instance`.

Exit code is forwarded to Dagu unmodified — 0 on a clean run, non-zero on a gate failure,
a session failure, or a legitimate skip (a prior run for this instance still active).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hourglass.wrapper import run_instance


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hourglass wrapper: one scheduled run.")
    parser.add_argument(
        "--instance-home",
        required=True,
        type=Path,
        help="directory holding this instance's config.yaml, logs/, and scripts/",
    )
    args = parser.parse_args(argv)
    return run_instance(args.instance_home)


if __name__ == "__main__":
    sys.exit(main())
