"""hourglass/persona.py: materializing a crew persona as a one-shot Hermes skill."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hourglass.persona import (
    PersonaLoadError,
    hermes_home,
    persona_skill,
    profile_skills_dir,
    render_persona_prompt,
)


def test_hermes_home_honors_env_var(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/custom-home")
    assert hermes_home() == Path("/tmp/custom-home")


def test_hermes_home_defaults_to_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert hermes_home() == Path.home() / ".hermes"


def test_profile_skills_dir_default_profile(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/h")
    assert profile_skills_dir(None) == Path("/tmp/h/skills")
    assert profile_skills_dir("default") == Path("/tmp/h/skills")


def test_profile_skills_dir_named_profile(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/h")
    assert profile_skills_dir("pessoa") == Path("/tmp/h/profiles/pessoa/skills")


def test_render_persona_prompt_missing_file_raises():
    with pytest.raises(PersonaLoadError):
        render_persona_prompt(Path("/nonexistent/persona.md"))


def test_render_persona_prompt_includes_source_line_and_body():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        persona = home / "crew" / "pessoa" / "pessoa.md"
        persona.parent.mkdir(parents=True)
        persona.write_text("# Agent: PESSOA\n\nBody text.\n")

        prompt = render_persona_prompt(persona)

        assert "Persona source:" in prompt
        assert str(persona.resolve()) in prompt
        assert "# Agent: PESSOA" in prompt
        assert "Body text." in prompt


def test_render_persona_prompt_includes_sibling_operator_authority():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        persona = home / "crew" / "pessoa" / "pessoa.md"
        persona.parent.mkdir(parents=True)
        persona.write_text("# Agent: PESSOA\n")
        authority = home / "crew" / "shared" / "operator-authority.md"
        authority.parent.mkdir(parents=True)
        authority.write_text("# Operator assignments and persona defaults\n")

        prompt = render_persona_prompt(persona)

        assert "Assignment authority source:" in prompt
        assert "# Operator assignments and persona defaults" in prompt


def test_render_persona_prompt_omits_authority_when_absent():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        persona = home / "lone-persona.md"
        persona.write_text("# Agent: LONE\n")

        prompt = render_persona_prompt(persona)

        assert "Assignment authority source:" not in prompt


def test_persona_skill_writes_and_removes_skill_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as hermes_home_dir, tempfile.TemporaryDirectory() as crew_dir:
        monkeypatch.setenv("HERMES_HOME", hermes_home_dir)
        persona = Path(crew_dir) / "pessoa.md"
        persona.write_text("# Agent: PESSOA\n")

        skills_root = Path(hermes_home_dir) / "profiles" / "pessoa" / "skills"
        with persona_skill("pessoa", persona) as name:
            skill_dir = skills_root / name
            assert skill_dir.is_dir()
            skill_md = (skill_dir / "SKILL.md").read_text()
            assert name in skill_md
            assert "# Agent: PESSOA" in skill_md
            assert "Hourglass" in skill_md

        assert not skill_dir.exists()  # cleaned up on exit


def test_persona_skill_cleans_up_even_on_exception(monkeypatch):
    with tempfile.TemporaryDirectory() as hermes_home_dir, tempfile.TemporaryDirectory() as crew_dir:
        monkeypatch.setenv("HERMES_HOME", hermes_home_dir)
        persona = Path(crew_dir) / "pessoa.md"
        persona.write_text("# Agent: PESSOA\n")

        skills_root = Path(hermes_home_dir) / "profiles" / "pessoa" / "skills"
        skill_dir = None
        with pytest.raises(RuntimeError):
            with persona_skill("pessoa", persona) as name:
                skill_dir = skills_root / name
                assert skill_dir.is_dir()
                raise RuntimeError("boom")

        assert skill_dir is not None and not skill_dir.exists()


def test_persona_skill_missing_persona_raises_before_materializing(monkeypatch):
    with tempfile.TemporaryDirectory() as hermes_home_dir:
        monkeypatch.setenv("HERMES_HOME", hermes_home_dir)
        with pytest.raises(PersonaLoadError):
            with persona_skill("pessoa", Path("/nonexistent/persona.md")):
                pass  # pragma: no cover - must never reach here

        skills_root = Path(hermes_home_dir) / "profiles" / "pessoa" / "skills"
        assert not skills_root.exists()
