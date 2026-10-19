"""REQ-5/REQ-7 toolset gate tests: live enumeration, disable-all, enable-allowlist,
verify-exact-match, exclusivity guard, and the fail-closed paths from REQ-5's acceptance.
"""
from __future__ import annotations

from hourglass.toolset import (
    Allowlist,
    LiveEnumeration,
    ToolsetGateError,
    apply_gate,
    enumerate_live,
    verify_gate,
)
from hourglass.cli_utils import CommandResult
from tests.fixtures import (
    _ALL_26,
    DEFAULT_ENABLED_17,
    GMAIL_TOOLS,
    LINEAR_TOOLS,
    mcp_list_output,
    mcp_test_output,
    tools_list_output,
)


class FakeHermes:
    """Stateful fake standing in for the real `hermes` CLI: tracks enabled builtin/plugin
    toolsets and, per MCP server, which sub-tools are currently excluded — mirroring exactly
    the two gating primitives the spec's §2 verified (whole-unit disable for built-ins,
    `server:tool` exclusion for MCP servers, never `mcp remove`)."""

    def __init__(self, enabled_builtin=None, mcp_servers=None):
        self.enabled_builtin = set(enabled_builtin or DEFAULT_ENABLED_17)
        self.enabled_plugin = {"a2a"}
        # server -> set of excluded tool names
        self.mcp_servers = mcp_servers or {"linear": set(LINEAR_TOOLS), "gmail": set(GMAIL_TOOLS)}
        self.mcp_excluded = {name: set() for name in self.mcp_servers}
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        cmd = list(args[1:])
        if len(cmd) >= 2 and cmd[0] == "--profile":
            cmd = cmd[2:]
        if cmd[:2] == ["tools", "list"]:
            return CommandResult(args, 0, tools_list_output(self.enabled_builtin, self.enabled_plugin), "")
        if cmd[:2] == ["mcp", "list"]:
            servers = {
                name: (len(self.mcp_excluded[name]), True) for name in self.mcp_servers
            }
            return CommandResult(args, 0, mcp_list_output(servers), "")
        if cmd[:2] == ["mcp", "test"]:
            server = cmd[2]
            return CommandResult(args, 0, mcp_test_output(sorted(self.mcp_servers[server])), "")
        if cmd[:2] == ["tools", "disable"]:
            return self._toggle(cmd[2], exclude=True)
        if cmd[:2] == ["tools", "enable"]:
            return self._toggle(cmd[2], exclude=False)
        raise AssertionError(f"unexpected command: {args}")

    def _toggle(self, name, exclude):
        if ":" in name:
            server, tool = name.split(":", 1)
            if server not in self.mcp_servers:
                return CommandResult([], 1, "", f"MCP server '{server}' not found in config")
            if exclude:
                self.mcp_excluded[server].add(tool)
            else:
                self.mcp_excluded[server].discard(tool)
            return CommandResult([], 0, "", "")
        if name not in (set(_ALL_26) | {"a2a"}):
            return CommandResult([], 0, f"✗ Unknown toolset '{name}'", "")
        if exclude:
            self.enabled_builtin.discard(name)
            self.enabled_plugin.discard(name)
        else:
            if name == "a2a":
                self.enabled_plugin.add(name)
            else:
                self.enabled_builtin.add(name)
        return CommandResult([], 0, "", "")


def test_enumerate_live_reports_all_three_categories():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    assert enum.builtin == set(_ALL_26)
    assert enum.plugin == {"a2a"}
    assert set(enum.mcp_servers) == {"linear", "gmail"}
    assert len(enum.mcp_servers["linear"]) == 68
    assert len(enum.mcp_servers["gmail"]) == 19


def test_enumerate_live_raises_on_command_failure():
    def broken(args, timeout=None):
        return CommandResult(args, 1, "", "boom")

    try:
        enumerate_live("probe", broken)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass


def test_apply_gate_disables_everything_then_enables_only_allowlist():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(builtin={"builtin_00", "builtin_01"}, mcp={"linear": {LINEAR_TOOLS[0]}})
    apply_gate("probe", enum, allow, task_destinations={}, runner=fake)

    assert fake.enabled_builtin == {"builtin_00", "builtin_01"}
    assert fake.enabled_plugin == set()  # a2a not on the allowlist -> disabled
    # linear: every tool excluded except the one granted
    assert fake.mcp_excluded["linear"] == set(LINEAR_TOOLS) - {LINEAR_TOOLS[0]}
    # gmail: fully excluded, nothing granted
    assert fake.mcp_excluded["gmail"] == set(GMAIL_TOOLS)
    # never calls mcp remove
    assert all("remove" not in c for c in fake.calls)


def test_apply_gate_rejects_unknown_allowlist_entry():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(builtin={"not_a_real_toolset"})
    try:
        apply_gate("probe", enum, allow, task_destinations={}, runner=fake)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass


def test_apply_gate_rejects_unregistered_mcp_server():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(mcp={"notion": {"some_tool"}})
    try:
        apply_gate("probe", enum, allow, task_destinations={}, runner=fake)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass


def test_live_action_tool_ungated_only_when_auto_execute_present():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    create_comment = LINEAR_TOOLS[0]
    allow = Allowlist(
        mcp={"linear": {create_comment, LINEAR_TOOLS[1]}},
        gated_live_tools={"linear": {create_comment}},
    )

    # No auto_execute task touching "linear" this run -> live tool stays excluded.
    apply_gate("probe", enum, allow, task_destinations={"linear": False}, runner=fake)
    assert create_comment in fake.mcp_excluded["linear"]
    assert LINEAR_TOOLS[1] not in fake.mcp_excluded["linear"]

    # Reset and re-run with auto_execute True -> live tool is enabled for the whole run.
    fake2 = FakeHermes()
    enum2 = enumerate_live("probe", fake2)
    apply_gate("probe", enum2, allow, task_destinations={"linear": True}, runner=fake2)
    assert create_comment not in fake2.mcp_excluded["linear"]


def test_exclusivity_guard_rejects_cronjob_with_gated_destination_present():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(
        builtin={"cronjob"} & set(_ALL_26) | {_ALL_26[0]},
        mcp={"linear": {LINEAR_TOOLS[0]}},
        gated_live_tools={"linear": {LINEAR_TOOLS[0]}},
    )
    # Map cronjob onto a real enumerated name for this fake build.
    allow = Allowlist(
        builtin={_ALL_26[0]},
        mcp={"linear": {LINEAR_TOOLS[0]}},
        gated_live_tools={"linear": {LINEAR_TOOLS[0]}},
    )
    # Directly exercise the guard with the real "cronjob" name present in the allowlist.
    from hourglass.toolset import _check_exclusivity

    guarded_allow = Allowlist(
        builtin={"cronjob"},
        mcp={"linear": {LINEAR_TOOLS[0]}},
        gated_live_tools={"linear": {LINEAR_TOOLS[0]}},
    )
    try:
        _check_exclusivity(guarded_allow, task_destinations={"linear": False})
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass

    # When the gated destination is fully ungated this run, cronjob is allowed alongside it.
    _check_exclusivity(guarded_allow, task_destinations={"linear": True})


def test_exclusivity_guard_composed_with_mixed_auto_execute_tasks_end_to_end():
    """R1-LOW-1: the composed path — config.task_destinations_ungated's OR across mixed
    auto_execute tasks for the same destination, feeding straight into
    _check_exclusivity — not just each half tested in isolation."""
    from hourglass.config import Task, task_destinations_ungated
    from hourglass.toolset import _check_exclusivity

    guarded_allow = Allowlist(
        builtin={"cronjob"},
        mcp={"linear": {LINEAR_TOOLS[0]}},
        gated_live_tools={"linear": {LINEAR_TOOLS[0]}},
    )

    # One task on "linear" has auto_execute: false, a sibling task on the same
    # destination has auto_execute: true -> OR resolves to True for the whole run,
    # so cronjob is allowed alongside it (REQ-7 edge case: run-scoped, not task-scoped).
    mixed_tasks = [
        Task(task_id="a", destination="linear", auto_execute=False),
        Task(task_id="b", destination="linear", auto_execute=True),
    ]
    destinations = task_destinations_ungated(mixed_tasks)
    assert destinations == {"linear": True}
    _check_exclusivity(guarded_allow, task_destinations=destinations)  # must not raise

    # Flip both to auto_execute: false -> OR resolves to False -> cronjob rejected.
    all_false_tasks = [
        Task(task_id="a", destination="linear", auto_execute=False),
        Task(task_id="b", destination="linear", auto_execute=False),
    ]
    destinations_false = task_destinations_ungated(all_false_tasks)
    assert destinations_false == {"linear": False}
    try:
        _check_exclusivity(guarded_allow, task_destinations=destinations_false)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass


def test_verify_gate_passes_on_exact_match():
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(builtin={"builtin_00"}, mcp={"linear": {LINEAR_TOOLS[0]}})
    apply_gate("probe", enum, allow, task_destinations={}, runner=fake)
    verify_gate("probe", allow, enum, task_destinations={}, runner=fake)  # must not raise


def test_verify_gate_fails_closed_on_stray_builtin_toolset():
    """REQ-5 acceptance (a): a profile with one undeclared built-in toolset left enabled
    (e.g. a freshly created profile's default `cronjob`) must fail closed."""
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(builtin={"builtin_00"})
    apply_gate("probe", enum, allow, task_destinations={}, runner=fake)
    # Simulate a stray: something re-enables an undeclared toolset behind the gate's back.
    # "cronjob" is a real enumerated name (index 0) never declared on this allowlist.
    fake.enabled_builtin.add("cronjob")
    fake.enabled_builtin.add("builtin_00")  # keep declared one too
    try:
        verify_gate("probe", allow, enum, task_destinations={}, runner=fake)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass


def test_verify_gate_fails_closed_on_undeclared_mcp_server_not_fully_excluded():
    """REQ-5 acceptance (b): an MCP server registered but not declared must fail closed."""
    fake = FakeHermes()
    enum = enumerate_live("probe", fake)
    allow = Allowlist(builtin={"builtin_00"})  # linear/gmail not declared at all
    apply_gate("probe", enum, allow, task_destinations={}, runner=fake)
    # Simulate a stray grant: one linear tool left enabled despite not being declared.
    fake.mcp_excluded["linear"].discard(LINEAR_TOOLS[0])
    try:
        verify_gate("probe", allow, enum, task_destinations={}, runner=fake)
        assert False, "expected ToolsetGateError"
    except ToolsetGateError:
        pass
