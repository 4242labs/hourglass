"""Test-wide isolation: no test may ever write into the real ~/.hermes. Every test gets
its own throwaway HERMES_HOME, matching how `hourglass/persona.py` resolves profile
skill directories at runtime."""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_hermes_home(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HERMES_HOME", d)
        yield d
