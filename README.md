# Hourglass

A generic, persona/task-source-agnostic wrapper for running a checklist on a schedule via
Dagu → Hermes, with a toolset gate that closes by **default-deny over the full live toolset
enumeration** (built-in, plugin, and every registered MCP server).

Contract: `hourglass-meta/specs/plan-hourglass-mechanism.md` (not in this repo — see that repo
for the full mechanism spec, obligations, requirements, and acceptance criteria this
implementation satisfies).

## Scope

This repo implements the **wrapper script only** (REQ-1 through REQ-7 of the spec). Out of
scope, per the spec's §4:

- the Linear-card task-list schema and the file task-list schema
- where instance configs live on disk and how a new instance is provisioned (this repo assumes
  an `$INSTANCE_HOME` directory is handed to it; it does not create one)

## Layout

```
hourglass/
  __init__.py
  wrapper.py       entry point: run_instance(instance_home) -> int (exit code)
  toolset.py        REQ-5 / REQ-7: live enumeration, disable-all, enable-allowlist, auto-execute ungate
  tasksource.py      REQ-3: NEXT_TASK_SOURCE parsing and config update
  runlog.py          REQ-2: run log writing in the required grep-able format
  locking.py         REQ-1: single-flight lock per instance
  config.py          instance config loading (task source, toolset allowlist, state_file, tasks)
cli.py               Dagu entry point
tests/
```

## Usage

```
python cli.py --instance-home /path/to/instance
```

Exit code is forwarded to Dagu: 0 on a clean run, non-zero on any Hermes session failure or a
rejected toolset-gate check (REQ-5).
