# hourglass

[![Project Status: WIP](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)
[![Maintenance](https://img.shields.io/badge/maintenance-passively--maintained-yellowgreen.svg)](CONTRIBUTING.md)

> Run a checklist on a schedule, with a toolset gate that starts from zero and grants nothing it wasn't told to.

Hourglass is a wrapper for Dagu-scheduled, Hermes-executed task checklists. It resolves a
pluggable task source (a Linear card or a file), gates the Hermes agent's toolset down to an
explicit, live-enumerated allowlist before every run, invokes one fresh session per run, writes
a grep-able log, and applies any agent-proposed task-source update only after a clean exit.

The security model is structural, not enumerative: every run disables **every** toolset the
installed Hermes build can expose — built-in, plugin, and every registered MCP server's full
tool set — then re-enables only what the instance explicitly declared. A toolset nobody has
heard of yet, including one a future Hermes release adds, starts disabled by construction. MCP
servers are gated by excluding their sub-tools one at a time; the wrapper never de-registers a
server, so OAuth credentials are never purged just to turn a destination off for one run.

## How it works

1. **Resolve the task source** (file path or Linear card ID) from the instance's config.
2. **Enumerate the live toolset** — `hermes tools list`, `hermes mcp list`, and `hermes mcp test
   <server>` for every registered server — never a frozen list.
3. **Disable everything**, then **enable only the instance's declared allowlist**. Re-verify the
   result matches exactly; abort the run rather than proceed over-broad.
4. **Ungate** a destination's live-action tool only when some task touching it this run has
   `auto_execute: true`; otherwise leave it excluded for the whole run.
5. **Invoke one fresh `hermes chat` session**, never resumed, guarded by a per-instance
   single-flight lock so an overlapping schedule fire is skipped, not double-run.
6. **Write the run log** — one grep-able `- task: <id> status: (done|failed|skipped)` line per
   task — and, on a clean exit only, parse a `NEXT_TASK_SOURCE:`-prefixed line from the session's
   output and apply it for the *next* run.

## Layout

```
hourglass/
  __init__.py
  wrapper.py       entry point: run_instance(instance_home) -> int (exit code)
  toolset.py       live enumeration, disable-all, enable-allowlist, auto-execute ungate
  tasksource.py    NEXT_TASK_SOURCE parsing and config update
  runlog.py        run log writing in the required grep-able format
  locking.py       single-flight lock per instance
  config.py        instance config loading (task source, toolset allowlist, state_file, tasks)
cli.py             Dagu entry point
tests/
```

## Scope

This repo is the wrapper only. Deliberately out of scope: the task-list schema (what exactly a
Linear-card or file task list looks like) and where/how an instance's config and logs are
provisioned on disk. Both are separate, future pieces — a PR trying to also design either of
them here is scope creep, not a contribution.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py --instance-home /path/to/instance
```

`$INSTANCE_HOME` holds `config.yaml` (profile, persona skill, task source, toolset allowlist,
optional state file, resolved tasks) and accumulates `logs/<run_id>.md` per run. Exit code is
forwarded unmodified — safe to wire straight into a Dagu step.

## Testing

```bash
.venv/bin/python -m pytest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributors

<!-- contributors:start -->
<a href="https://github.com/42piratas" title="42piratas"><img src="https://avatars.githubusercontent.com/u/18232600?v=4&s=64" width="64" height="64" alt="42piratas" /></a>
<!-- contributors:end -->

## License

Open source — [AGPL-3.0](LICENSE). Commercial — contact ahoy@42labs.io.

---
If it earned its keep, [coffee is appreciated](https://buymeacoffee.com/42piratas). ☕
