#!/usr/bin/env python3
"""A real subprocess standing in for `hermes chat --format stream-json` in REQ-4/5/6
tests (the spec requires a real subprocess here, not a mock, so SIGTERM/SIGKILL timing
is genuinely exercised).

Usage: fakeagent.py [--ignore-sigterm] [--event-gap SECONDS] [--events N]
                     [--spawn-detached] [--spawn-env-clear]

Emits `--events` JSON lines (one per --event-gap seconds) to stdout, each one a minimal
stream-json "text" event, then a final "result" event, then exits 0 — unless killed
first. `--ignore-sigterm` installs a no-op SIGTERM handler so the test can prove the
staged stop escalates to SIGKILL. `--spawn-detached` / `--spawn-env-clear` spawn a
`sleep 300` child the cleanup sweep must independently find and kill.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ignore-sigterm", action="store_true")
    parser.add_argument("--event-gap", type=float, default=0.1)
    parser.add_argument("--events", type=int, default=3)
    parser.add_argument("--spawn-detached", action="store_true")
    parser.add_argument("--spawn-env-clear", action="store_true")
    args = parser.parse_args()

    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    if args.spawn_detached:
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,  # setsid-equivalent: detaches from this process's group
            env=dict(os.environ),  # marker-carrying env is inherited
        )

    if args.spawn_env_clear:
        subprocess.Popen(["/usr/bin/env", "-i", sys.executable, "-c", "import time; time.sleep(300)"])

    print(
        json.dumps({"type": "system", "subtype": "init", "model": "fake-model", "session_id": "fake-session"}),
        flush=True,
    )
    for i in range(args.events):
        time.sleep(args.event_gap)
        print(json.dumps({"type": "text", "text": f"chunk-{i}"}), flush=True)

    print(json.dumps({"type": "result", "session_id": "fake-session", "text": "done"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
