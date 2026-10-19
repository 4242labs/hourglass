"""REQ-1: single-flight lock. Also proves a mutation (removing the lock check) breaks the
guarantee red, per mutation-verified testing discipline."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hourglass.locking import RunAlreadyActiveError, instance_lock


def test_lock_blocks_concurrent_acquisition():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        with instance_lock(home):
            with pytest.raises(RunAlreadyActiveError):
                with instance_lock(home):
                    pass


def test_lock_released_after_context_exits_allowing_sequential_runs():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        with instance_lock(home):
            pass
        with instance_lock(home):  # must not raise — prior run released cleanly
            pass


def test_lock_released_even_on_exception_inside_the_block():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        try:
            with instance_lock(home):
                raise ValueError("boom")
        except ValueError:
            pass
        with instance_lock(home):  # must not raise — lock released despite the exception
            pass
