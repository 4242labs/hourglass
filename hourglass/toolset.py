"""REQ-5 / REQ-7: the default-deny toolset gate.

Enumerates every built-in toolset, every plugin toolset, and every registered MCP server's
full tool set live (never a frozen list), disables all of it unconditionally, then enables
only the instance's declared allowlist. Verifies the result matches the allowlist exactly
before the caller is allowed to invoke the Hermes session.

MCP servers are never registered or de-registered by this module (REQ-7 step 2's explicit
constraint: `hermes mcp remove` purges cached OAuth tokens and is never called here). Gating
an MCP server means excluding/including its sub-tools one at a time via `server:tool` form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .cli_utils import CommandResult, hermes_args, run as default_run

Runner = Callable[..., CommandResult]

# Toolsets that independently demonstrated arbitrary-destination egress or un-reverified
# subprocess inheritance (spec REQ-7 rule 5). Never allowed alongside a gated destination
# that has any auto_execute: false task present this run.
EXCLUSIVITY_GUARDED = frozenset(
    {"cronjob", "delegation", "terminal", "web", "browser", "code_execution", "computer_use"}
)


class ToolsetGateError(RuntimeError):
    """Raised when the gate cannot be established or verified. The caller must abort the
    run without invoking the Hermes session — never proceed with an over-broad grant."""


@dataclass(frozen=True)
class Allowlist:
    """An instance's declared, positive toolset grant. Never an exclusion list."""

    builtin: Set[str] = field(default_factory=set)
    plugin: Set[str] = field(default_factory=set)
    # server name -> set of tool names to grant (never "all" as a string; always explicit,
    # so the allowlist stays a true enumeration, not a shortcut back to an exclusion list).
    mcp: Dict[str, Set[str]] = field(default_factory=dict)
    # toolset/server name -> tool names considered "live-action" (irreversible) for that
    # destination. Used by the auto-execute ungate (REQ-7 step 4).
    gated_live_tools: Dict[str, Set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveEnumeration:
    """Everything `hermes tools list` + `hermes mcp list` + `hermes mcp test <server>`
    report for this build, right now. Never cached across runs."""

    builtin: Set[str]
    plugin: Set[str]
    mcp_servers: Dict[str, List[str]]  # server name -> full list of its tool names


_BUILTIN_LINE = re.compile(r"^\s*[✓✗]\s+(?:enabled|disabled)\s+(\S+)\s")
_MCP_LIST_ROW = re.compile(r"^\s*(\S+)\s+.*?([✓✗])\s*(enabled|disabled)\s*$")
_MCP_EXCLUDED_COUNT = re.compile(r"-(\d+)\s*excluded")


def enumerate_live(profile: str, runner: Runner = default_run) -> LiveEnumeration:
    """REQ-7 step 1: live-enumerate every toolset this build can expose for `profile`.
    Raises ToolsetGateError if any of the three enumeration commands fail — an enumeration
    we can't trust is not an enumeration."""
    builtin_plugin = runner(hermes_args(profile, "tools", "list"))
    if not builtin_plugin.ok:
        raise ToolsetGateError(
            f"'hermes --profile {profile} tools list' failed (exit {builtin_plugin.returncode}): "
            f"{builtin_plugin.stderr.strip()}"
        )
    builtin, plugin = _parse_builtin_plugin(builtin_plugin.stdout)

    mcp_list = runner(hermes_args(profile, "mcp", "list"))
    if not mcp_list.ok:
        raise ToolsetGateError(
            f"'hermes --profile {profile} mcp list' failed (exit {mcp_list.returncode}): "
            f"{mcp_list.stderr.strip()}"
        )
    server_names = _parse_mcp_server_names(mcp_list.stdout)

    mcp_servers: Dict[str, List[str]] = {}
    for name in server_names:
        test = runner(hermes_args(profile, "mcp", "test", name))
        if not test.ok:
            raise ToolsetGateError(
                f"'hermes --profile {profile} mcp test {name}' failed "
                f"(exit {test.returncode}): {test.stderr.strip()}"
            )
        mcp_servers[name] = _parse_mcp_tool_names(test.stdout)

    return LiveEnumeration(builtin=builtin, plugin=plugin, mcp_servers=mcp_servers)


def _parse_builtin_plugin(stdout: str) -> tuple:
    builtin: Set[str] = set()
    plugin: Set[str] = set()
    section = None
    for line in stdout.splitlines():
        if "Built-in toolsets" in line:
            section = builtin
            continue
        if "Plugin toolsets" in line:
            section = plugin
            continue
        m = _BUILTIN_LINE.match(line)
        if m and section is not None:
            section.add(m.group(1))
    return builtin, plugin


def _parse_mcp_server_names(stdout: str) -> List[str]:
    names: List[str] = []
    started = False
    for line in stdout.splitlines():
        if "Name" in line and "Transport" in line:
            started = True
            continue
        if not started:
            continue
        stripped = line.strip()
        if not stripped or set(stripped) <= {"─", "-"}:
            continue
        m = _MCP_LIST_ROW.match(line)
        if m:
            names.append(m.group(1))
    return names


def _parse_mcp_tool_names(stdout: str) -> List[str]:
    """Parse `hermes mcp test <server>` tool listing: one tool name per line, left-most
    whitespace-delimited token, skipping the header/status lines above the list."""
    names: List[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Testing", "Transport:", "Auth:", "✓", "✗")):
            continue
        token = stripped.split()[0] if stripped.split() else ""
        if token and re.match(r"^[a-z][a-z0-9_]*$", token):
            names.append(token)
    return names


def apply_gate(
    profile: str,
    enumeration: LiveEnumeration,
    allowlist: Allowlist,
    task_destinations: Dict[str, bool],
    runner: Runner = default_run,
) -> None:
    """REQ-7 steps 2-5: disable everything, enable only the allowlist, ungate live-action
    tools only where an auto_execute task touches that destination this run.

    `task_destinations` maps a destination name (toolset or MCP server name) to whether
    *any* task touching it this run has auto_execute: true (REQ-7 step 4's run-scoped rule).
    """
    _check_exclusivity(allowlist, task_destinations)

    # Step 2: disable every built-in/plugin toolset, unconditionally.
    for name in sorted(enumeration.builtin | enumeration.plugin):
        result = runner(hermes_args(profile, "tools", "disable", name))
        if not result.ok:
            raise ToolsetGateError(
                f"could not disable builtin toolset '{name}' (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    # Step 2: disable every sub-tool of every registered MCP server, unconditionally.
    # Never `hermes mcp remove` — that purges cached OAuth tokens (spec §3 decision 2).
    for server, tools in enumeration.mcp_servers.items():
        _disable_mcp_tools(profile, server, tools, runner)

    # Step 3: enable only the declared allowlist.
    for name in sorted(allowlist.builtin | allowlist.plugin):
        if name not in (enumeration.builtin | enumeration.plugin):
            raise ToolsetGateError(
                f"allowlist declares unknown toolset '{name}' — not in this build's live "
                f"enumeration; refusing to proceed on a possibly-misspelled grant"
            )
        result = runner(hermes_args(profile, "tools", "enable", name))
        if not result.ok:
            raise ToolsetGateError(
                f"could not enable allowlisted toolset '{name}' (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    for server, granted_tools in allowlist.mcp.items():
        if server not in enumeration.mcp_servers:
            raise ToolsetGateError(
                f"allowlist declares unregistered MCP server '{server}' — register it with "
                f"'hermes mcp add' before this instance can run, this wrapper never registers "
                f"one for you"
            )
        unknown = granted_tools - set(enumeration.mcp_servers[server])
        if unknown:
            raise ToolsetGateError(
                f"allowlist declares unknown tool(s) for server '{server}': {sorted(unknown)}"
            )
        # Step 4: the live-action subset only gets enabled when some task touching this
        # destination this run has auto_execute: true.
        live_tools = allowlist.gated_live_tools.get(server, set())
        ungated = task_destinations.get(server, False)
        to_enable = granted_tools if ungated else (granted_tools - live_tools)
        _enable_mcp_tools(profile, server, sorted(to_enable), runner)


def _check_exclusivity(allowlist: Allowlist, task_destinations: Dict[str, bool]) -> None:
    """REQ-7 rule 5: the allowlist may not include a cronjob/delegation/terminal/web/browser/
    code_execution/computer_use toolset in the same run as any gated destination that still
    has an auto_execute: false task present."""
    any_gated_not_fully_ungated = any(
        dest in allowlist.gated_live_tools and not ungated
        for dest, ungated in task_destinations.items()
    )
    if not any_gated_not_fully_ungated:
        return
    offending = sorted(allowlist.builtin & EXCLUSIVITY_GUARDED)
    if offending:
        raise ToolsetGateError(
            f"allowlist includes {offending} alongside a gated destination with an "
            f"auto_execute: false task present — refused per REQ-7 rule 5"
        )


def _disable_mcp_tools(profile: str, server: str, tools: List[str], runner: Runner) -> None:
    for tool in tools:
        result = runner(hermes_args(profile, "tools", "disable", f"{server}:{tool}"))
        if not result.ok:
            raise ToolsetGateError(
                f"could not exclude '{server}:{tool}' (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )


def _enable_mcp_tools(profile: str, server: str, tools: List[str], runner: Runner) -> None:
    for tool in tools:
        result = runner(hermes_args(profile, "tools", "enable", f"{server}:{tool}"))
        if not result.ok:
            raise ToolsetGateError(
                f"could not include '{server}:{tool}' (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )


def verify_gate(
    profile: str,
    allowlist: Allowlist,
    enumeration: LiveEnumeration,
    task_destinations: Dict[str, bool],
    runner: Runner = default_run,
) -> None:
    """REQ-5: re-read the live state after `apply_gate` and confirm the enabled set equals
    exactly the declared allowlist across all three categories, zero extra entries anywhere.
    Raises ToolsetGateError (hard failure, abort the run) on any mismatch."""
    post = enumerate_live(profile, runner)

    extra_builtin = post.builtin - allowlist.builtin
    # Only toolsets the allowlist didn't ask to enable should remain enabled-by-this-check;
    # `enumerate_live` reports the *existence* of every toolset, not its enabled state, so we
    # re-check actual enabled state via `tools list` directly here.
    state = runner(hermes_args(profile, "tools", "list"))
    enabled_builtin, enabled_plugin = _parse_enabled_only(state.stdout)

    stray_builtin = enabled_builtin - allowlist.builtin
    stray_plugin = enabled_plugin - allowlist.plugin
    if stray_builtin or stray_plugin:
        raise ToolsetGateError(
            f"post-apply verification failed: toolsets enabled beyond the declared allowlist "
            f"(builtin: {sorted(stray_builtin)}, plugin: {sorted(stray_plugin)})"
        )

    mcp_list = runner(hermes_args(profile, "mcp", "list"))
    enabled_counts = _parse_mcp_excluded_counts(mcp_list.stdout)
    for server, total_tools in post.mcp_servers.items():
        granted = allowlist.mcp.get(server, set())
        live_tools = allowlist.gated_live_tools.get(server, set())
        ungated = task_destinations.get(server, False)
        expected_enabled = len(granted if ungated else (granted - live_tools))
        expected_excluded = len(total_tools) - expected_enabled
        actual_excluded = enabled_counts.get(server, 0)
        if actual_excluded != expected_excluded:
            raise ToolsetGateError(
                f"post-apply verification failed for MCP server '{server}': expected "
                f"{expected_excluded} tools excluded, found {actual_excluded}"
            )
    _ = extra_builtin  # enumeration-level existence diff kept for future diagnostics only


def _parse_enabled_only(stdout: str) -> tuple:
    builtin: Set[str] = set()
    plugin: Set[str] = set()
    section = None
    for line in stdout.splitlines():
        if "Built-in toolsets" in line:
            section = builtin
            continue
        if "Plugin toolsets" in line:
            section = plugin
            continue
        if "✓ enabled" not in line:
            continue
        m = _BUILTIN_LINE.match(line)
        if m and section is not None:
            section.add(m.group(1))
    return builtin, plugin


def _parse_mcp_excluded_counts(stdout: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    started = False
    for line in stdout.splitlines():
        if "Name" in line and "Transport" in line:
            started = True
            continue
        if not started:
            continue
        stripped = line.strip()
        if not stripped or set(stripped) <= {"─", "-"}:
            continue
        m = _MCP_LIST_ROW.match(line)
        if not m:
            continue
        name = m.group(1)
        excluded_m = _MCP_EXCLUDED_COUNT.search(line)
        counts[name] = int(excluded_m.group(1)) if excluded_m else 0
    return counts
