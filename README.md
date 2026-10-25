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

1. **Resolve the task** — `--task <id>` selects exactly one task from a multi-task
   instance's declared list; `--ssh-task` reads the id from `SSH_ORIGINAL_COMMAND`
   (set by a forced-command SSH key). Neither flag is required for a single-task
   instance. Selection happens before any toolset or agent change.
2. **Validate the run's limits** — `run_timeout_seconds`, `max_turns`, and `stall_seconds`
   are required on every instance; a per-task `limits:` block may override any of them.
   Missing fields, non-positive values, or `stall_seconds >= run_timeout_seconds` abort
   the run before anything else happens.
3. **Enumerate the live toolset** — `hermes tools list`, `hermes mcp list`, and `hermes mcp test
   <server>` for every registered server — never a frozen list.
4. **Disable everything**, then **enable only the instance's declared allowlist**. Re-verify the
   result matches exactly; abort the run rather than proceed over-broad.
5. **Ungate** a destination's live-action tool only when some task touching it this run has
   `auto_execute: true`; otherwise leave it excluded for the whole run.
6. **Invoke one fresh `hermes chat --format stream-json` session**, never resumed, guarded by
   a per-instance single-flight lock so an overlapping schedule fire is skipped, not
   double-run. `--max-turns` caps tool-call iterations; the session is also told its own
   90%-of-cap deadline so it can wrap up before Hourglass's own enforcement kicks in.
7. **Enforce the cap independently of the agent** — a hard wall-clock deadline (REQ-4) and a
   stall timeout with no new event (REQ-5) each trigger a staged stop: SIGTERM, wait
   `grace_seconds`, then SIGKILL if the process is still alive.
8. **Sweep leftover processes** on every exit path (including an external SIGTERM to
   Hourglass itself) — every process carrying this run's `HOURGLASS_RUN_ID` marker in its
   own environment is found and killed, even one that re-parented away via a double fork.
   On Linux, Hourglass registers itself as a child subreaper so such descendants stay
   reachable; macOS has no equivalent facility and relies on the marker sweep alone.
9. **Write the run log** — one grep-able `- task: <id> status: (done|failed|skipped)` line per
   task, plus the full per-run record (session id, model, resolved limits, timing, tool-use
   count, outcome, stop reason) — and, on a clean exit only, parse a `NEXT_TASK_SOURCE:`-prefixed
   line from the session's own result text and apply it for the *next* run.

## Destinations

Each task in `config.yaml` may declare where its output goes, as `<kind>:<target>`:

| Destination | What the session is told to do | Needs on the allowlist | Verified by |
|:--|:--|:--|:--|
| `file:<path>` | write the output to that path (relative = instance home) | `file` | wrapper, `path.exists()` |
| `linear:<issue>` | add a comment to that existing issue | `linear` MCP comment tool | manual, flagged in the run log |
| `linear-issue:<team>` | create one **new** issue per output item in that team; project, status and title come from the task source; zero items creates nothing | `linear` MCP `save_issue` | manual, flagged in the run log |
| `email:<address>` | send the output to that address | `gmail` MCP send tool | manual, flagged in the run log |

The destination only tells the session what to do. Granting the tool is still the
instance's own allowlist, and the auto-execute gate above still applies to it.

## Layout

```
hourglass/
  __init__.py
  wrapper.py       entry point: run_instance(instance_home, task_id=None) -> int (exit code)
  cli_utils.py      hermes subprocess wrapper, streaming agent runner, staged stop,
                    cleanup sweep, Linux subreaper registration
  toolset.py       live enumeration, disable-all, enable-allowlist, auto-execute ungate
  tasksource.py    NEXT_TASK_SOURCE parsing and config update
  runlog.py        run log writing in the required grep-able format + per-run record
  locking.py       single-flight lock per instance
  config.py        instance config loading (task source, limits, toolset allowlist,
                    state_file, tasks)
cli.py             Dagu entry point: --instance-home, --task, --ssh-task
tests/
```

## Limits

Every instance's `config.yaml` carries a required `limits:` block:

```yaml
limits:
  run_timeout_seconds: 1200   # hard wall-clock cap for the whole run
  max_turns: 50                # tool-call iteration cap passed to hermes chat --max-turns
  stall_seconds: 420            # SIGTERM if no new agent event for this long
  grace_seconds: 30             # optional, default 30: SIGTERM-to-SIGKILL grace window
```

`stall_seconds` must be less than `run_timeout_seconds`; `grace_seconds + 3` must also be
less than `run_timeout_seconds` (the 3s is Hourglass's own margin before its hard stop). A
task may override any field under its own `limits:` key; only the overridden fields need
to be present, the rest are inherited from the instance default.

## Scope

This repo is the wrapper only. Deliberately out of scope: the task-list schema (what exactly a
Linear-card or file task list looks like) and where/how an instance's config and logs are
provisioned on disk. Both are separate, future pieces — a PR trying to also design either of
them here is scope creep, not a contribution.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py --instance-home /path/to/instance
.venv/bin/python cli.py --instance-home /path/to/instance --task some-task-id
.venv/bin/python cli.py --instance-home /path/to/instance --ssh-task   # reads SSH_ORIGINAL_COMMAND
```

`--task` and `--ssh-task` are mutually exclusive. Neither is required for a single-task
instance; a multi-task instance requires one of them. A task id must match `[a-z0-9-]+`.

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
