"""REQ-4 unit tests: the fixed `<kind>:<target>` destination schema, its prompt
instructions, its required-toolset mapping, and its verification rules."""
from __future__ import annotations

import pytest

from hourglass import destinations as dest_mod


def test_parse_file_destination():
    d = dest_mod.parse("file:logs/out.txt")
    assert d.kind == "file"
    assert d.target == "logs/out.txt"


def test_parse_linear_destination():
    d = dest_mod.parse("linear:ENG-123")
    assert d.kind == "linear"
    assert d.target == "ENG-123"


def test_parse_email_destination():
    d = dest_mod.parse("email:ops@example.com")
    assert d.kind == "email"
    assert d.target == "ops@example.com"


def test_parse_rejects_missing_colon():
    with pytest.raises(dest_mod.InvalidDestinationError):
        dest_mod.parse("linear")


def test_parse_rejects_unknown_kind():
    with pytest.raises(dest_mod.InvalidDestinationError):
        dest_mod.parse("slack:#general")


def test_parse_rejects_empty_target():
    with pytest.raises(dest_mod.InvalidDestinationError):
        dest_mod.parse("file:")


@pytest.mark.parametrize(
    "destination,expected_toolset",
    [
        ("file:a.txt", "file"),
        ("linear:ENG-1", "linear"),
        ("linear-issue:42L", "linear"),
        ("email:a@b.com", "gmail"),
    ],
)
def test_required_toolset_maps_each_kind(destination, expected_toolset):
    d = dest_mod.parse(destination)
    assert dest_mod.required_toolset(d) == expected_toolset


def test_instruction_for_file_names_the_resolved_absolute_path(tmp_path):
    d = dest_mod.parse("file:out.txt")
    instruction = dest_mod.instruction_for(d, tmp_path)
    assert str(tmp_path / "out.txt") in instruction


def test_instruction_for_linear_names_the_issue_id(tmp_path):
    d = dest_mod.parse("linear:ENG-7")
    instruction = dest_mod.instruction_for(d, tmp_path)
    assert "ENG-7" in instruction
    assert "linear" in instruction.lower()


def test_instruction_for_email_names_the_address(tmp_path):
    d = dest_mod.parse("email:someone@example.com")
    instruction = dest_mod.instruction_for(d, tmp_path)
    assert "someone@example.com" in instruction
    assert "gmail" in instruction.lower()


def test_verify_file_destination_missing_returns_error(tmp_path):
    d = dest_mod.parse("file:missing.txt")
    error = dest_mod.verify(d, tmp_path)
    assert error is not None
    assert "missing.txt" in error


def test_verify_file_destination_present_returns_none(tmp_path):
    (tmp_path / "present.txt").write_text("x")
    d = dest_mod.parse("file:present.txt")
    assert dest_mod.verify(d, tmp_path) is None


@pytest.mark.parametrize("destination", ["linear:ENG-1", "email:a@b.com"])
def test_verify_linear_and_email_are_not_mechanically_checked(destination, tmp_path):
    """REQ-4's own acceptance text: these two are `manual_by:`, not `cmd:` — verify()
    must never fabricate a pass/fail for a side effect this repo can't read back."""
    d = dest_mod.parse(destination)
    assert dest_mod.verify(d, tmp_path) is None
    assert dest_mod.requires_manual_verification(d) is True


def test_file_destination_does_not_require_manual_verification():
    d = dest_mod.parse("file:a.txt")
    assert dest_mod.requires_manual_verification(d) is False


def test_parse_linear_issue_destination():
    d = dest_mod.parse("linear-issue:42L")
    assert d.kind == "linear-issue"
    assert d.target == "42L"


def test_instruction_for_linear_issue_creates_new_issues_never_comments(tmp_path):
    d = dest_mod.parse("linear-issue:42L")
    instruction = dest_mod.instruction_for(d, tmp_path)
    assert "42L" in instruction
    assert "save_issue" in instruction
    assert "one NEW issue per output item" in instruction
    assert "never comment" in instruction.lower()


def test_linear_issue_destination_is_manual_verification(tmp_path):
    d = dest_mod.parse("linear-issue:42L")
    assert dest_mod.requires_manual_verification(d) is True
    assert dest_mod.verify(d, tmp_path) is None
