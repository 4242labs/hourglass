"""Fixture builders: fake `hermes tools list` / `mcp list` / `mcp test` outputs, shaped per
the spec's §2 evidence rows (counts: 26 built-in + 1 plugin; linear 68 tools; gmail 19).
Not a verbatim capture — the real CLI's exact formatting may differ in whitespace, but the
section markers and ✓/✗ enabled/disabled tokens this wrapper parses on are load-bearing and
match what §2 quotes directly.
"""
from __future__ import annotations

BUILTIN_NAMES = [
    "computer_use", "cronjob", "delegation", "connections", "clarify", "memory",
    "session_search", "skills", "todo", "file", "web", "browser", "code_execution",
    "terminal", "tts", "stt_disabled_placeholder",  # placeholder kept distinct from real stt
]
# Trim/pad to exactly 26 distinct names for test determinism. "cronjob" is kept as a real
# name at index 0 so tests exercising the exclusivity guard (REQ-7 rule 5) can reference it.
_ALL_26 = ["cronjob"] + [f"builtin_{i:02d}" for i in range(25)]
DEFAULT_ENABLED_17 = set(_ALL_26[:17])
DEFAULT_DISABLED_9 = set(_ALL_26[17:])


def tools_list_output(enabled_builtin, enabled_plugin=("a2a",)):
    lines = ["Built-in toolsets:"]
    for name in _ALL_26:
        mark = "✓ enabled" if name in enabled_builtin else "✗ disabled"
        lines.append(f"  {mark}  {name}  some description")
    lines.append("")
    lines.append("Plugin toolsets:")
    for name in ("a2a",):
        mark = "✓ enabled" if name in enabled_plugin else "✗ disabled"
        lines.append(f"  {mark}  {name}  plugin description")
    return "\n".join(lines)


LINEAR_TOOLS = [f"linear_tool_{i:02d}" for i in range(68)]
GMAIL_TOOLS = [f"gmail_tool_{i:02d}" for i in range(19)]


def mcp_test_output(tool_names):
    lines = [f"Testing server...", "Transport: stdio", "Auth: oauth"]
    for name in tool_names:
        lines.append(f"{name}  some tool description")
    return "\n".join(lines)


def mcp_list_output(servers):
    """servers: dict server_name -> (excluded_count, enabled_bool)"""
    lines = ["Name       Transport   Status", "-----------------------------"]
    for name, (excluded, enabled) in servers.items():
        status = "✓ enabled" if enabled else "✗ disabled"
        excl = f"-{excluded} excluded" if excluded else "all"
        lines.append(f"{name}  stdio  {excl}  {status}")
    return "\n".join(lines)
