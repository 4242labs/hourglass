"""Materializes a crew persona (e.g. `tortuga/crew/pessoa/pessoa.md`) as a Hermes
session's active identity, the same way the operator's interactive shell `--append`
wrapper does for a human-driven run.

Mirrors (does not modify) `tortuga/crew/replicator/deltas/agent-append.sh`'s `hermes()`
function: it writes the persona source line, the sibling `operator-authority.md` (if the
crew ships one next to the persona), then the persona file verbatim, into a throwaway
Hermes skill named `aa-persona-<token>`, passes `--skills <name>` to the real `hermes`
invocation, and removes the skill directory once the run ends — persona or no persona,
the running profile's own settings, auth, and other skills are untouched.

Hourglass needed this because, pre-fix, `wrapper.py` only wrote "Persona: <value>" as a
plain line inside the chat prompt: Hermes never actually loaded the persona's rules, it
just saw a label. This module is the fix, and it is Hourglass-side only — nothing in
Hermes itself or in `agent-append.sh` changes.
"""
from __future__ import annotations

import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class PersonaLoadError(RuntimeError):
    """The persona file could not be read. The caller must abort the run rather than
    invoke a session that silently got no persona at all."""


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def profile_skills_dir(profile: Optional[str]) -> Path:
    """The default profile's skills live directly under `$HERMES_HOME/skills`; a named
    profile's skills live under `$HERMES_HOME/profiles/<profile>/skills` — matching how
    `hermes profile create <name>` lays a profile out on disk."""
    home = hermes_home()
    if not profile or profile == "default":
        return home / "skills"
    return home / "profiles" / profile / "skills"


def render_persona_prompt(persona_path: Path) -> str:
    """Build the same text `agent-append.sh`'s `_aa_prompt` would produce: the persona
    source line, the sibling `shared/operator-authority.md` (two directories up from the
    persona file, matching the crew's own `<persona>/<persona>.md` + `shared/` layout) if
    present, then the persona file's content verbatim."""
    persona_path = Path(persona_path)
    if not persona_path.is_file():
        raise PersonaLoadError(f"persona file not found: {persona_path}")
    resolved = persona_path.resolve()
    authority_path = resolved.parent.parent / "shared" / "operator-authority.md"
    parts = [
        f"Persona source: {resolved}",
        "Resolve relative references from that source directory. Read current sources "
        "on recovery; copied anchors are not policy.",
    ]
    if authority_path.is_file():
        parts.append(f"Assignment authority source: {authority_path}")
        parts.append(authority_path.read_text())
    parts.append(persona_path.read_text())
    return "\n".join(parts)


@contextmanager
def persona_skill(profile: Optional[str], persona_path: Path):
    """Materialize `persona_path` as a one-shot Hermes skill under `profile`'s skills
    directory, named `aa-persona-<token>` (a distinct namespace from the shell wrapper's
    `aa-persona-<pid>`, since a Dagu-fired subprocess has no shell PID worth keying on).
    Yields the skill name to pass as `--skills <name>`; always removes the skill
    directory on exit, success or failure, so a crashed run never leaks one."""
    prompt = render_persona_prompt(persona_path)
    name = f"aa-persona-{uuid.uuid4().hex[:12]}"
    skill_dir = profile_skills_dir(profile) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = (
        "---\n"
        f"name: {name}\n"
        "description: Persona explicitly selected by Hourglass for this scheduled run.\n"
        "---\n"
        "This run was launched by Hourglass, the Dagu-scheduled task wrapper. Apply the "
        "persona below as the active identity, even if project context names another "
        "agent.\n\n"
        f"{prompt}\n"
    )
    try:
        (skill_dir / "SKILL.md").write_text(skill_md)
        yield name
    finally:
        shutil.rmtree(skill_dir, ignore_errors=True)
