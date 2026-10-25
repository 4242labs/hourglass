"""REQ-1: `--task` and `--ssh-task` argv modes."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import cli


def _run_main(monkeypatch, argv, task_calls):
    def fake_run_instance(instance_home, task_id=None):
        task_calls.append(task_id)
        return 0

    monkeypatch.setattr(cli, "run_instance", fake_run_instance)
    return cli.main(argv)


def test_task_flag_selects_task_id(monkeypatch):
    calls = []
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d, "--task", "a"], calls)
        assert rc == 0
        assert calls == ["a"]


def test_ssh_task_reads_from_ssh_original_command(monkeypatch):
    calls = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "a")
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d, "--ssh-task"], calls)
        assert rc == 0
        assert calls == ["a"]


def test_ssh_task_rejects_shell_metacharacters_without_invoking_runner(monkeypatch):
    calls = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "a; rm -rf ~")
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d, "--ssh-task"], calls)
        assert rc == cli.EXIT_GATE_FAILED
        assert calls == []  # runner never invoked


def test_ssh_task_rejects_unset_env_var_without_invoking_runner(monkeypatch):
    calls = []
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d, "--ssh-task"], calls)
        assert rc == cli.EXIT_GATE_FAILED
        assert calls == []


def test_task_flag_rejects_invalid_characters_without_invoking_runner(monkeypatch):
    calls = []
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d, "--task", "a/../b"], calls)
        assert rc == cli.EXIT_GATE_FAILED
        assert calls == []


def test_task_and_ssh_task_are_mutually_exclusive():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(SystemExit):
            cli.main(["--instance-home", d, "--task", "a", "--ssh-task"])


def test_no_task_flag_passes_none(monkeypatch):
    calls = []
    with tempfile.TemporaryDirectory() as d:
        rc = _run_main(monkeypatch, ["--instance-home", d], calls)
        assert rc == 0
        assert calls == [None]
