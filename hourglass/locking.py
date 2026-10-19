"""REQ-1: a fresh one-shot Hermes session per run, with single-flight protection so Dagu
firing while a prior run is still active for the same instance never invokes a second
concurrent `hermes chat` for that profile."""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RunAlreadyActiveError(RuntimeError):
    """Raised when a lock for this instance is already held by another process."""


@contextmanager
def instance_lock(instance_home: Path) -> Iterator[None]:
    """Hold an exclusive, PID-stamped lock file for the duration of one run. Uses
    O_CREAT|O_EXCL so acquisition is atomic across concurrent wrapper invocations — no
    read-then-write race. Raises RunAlreadyActiveError without blocking: a skipped run
    is correct behavior here, not a bug to retry past."""
    lock_path = instance_home / "run.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise RunAlreadyActiveError(
                f"a run is already active for {instance_home} ({lock_path} exists)"
            ) from exc
        raise
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
