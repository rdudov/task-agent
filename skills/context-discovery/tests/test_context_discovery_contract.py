"""The lookup rule has one owner, and no other surface answers the same question.

This repository carried the rule three times: `AGENTS.md`, the always-on Cursor
rule, and `skills/task-artifacts/SKILL.md`. All three drifted, and all three
opened with `tasks/INDEX.md` -- a catalog no script in this repository generates,
so an agent that followed them found nothing and concluded there was no prior
work. Three copies is how that survived: no single file was wrong on its own
terms.

So the checks below are of three kinds. The owner must state both halves of the
escalation rule in one clause, name the catalog that actually exists, and say
what the window means. Every entry point must reach the owner by its path. And
-- the check that was missing the first time -- no file in the tree may name a
task catalog this repository does not build, and no entry point may carry a step
of the procedure. Asserting only that an entry point mentions the owner is
compatible with that same file contradicting it one paragraph later, which is
exactly what happened.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

OWNER = "skills/context-discovery/SKILL.md"
OWNER_PATH_REFERENCE = "skills/context-discovery/skill.md"

# The condition, and the scope it escalates to. Neither is a word that generic
# prose about searching would contain by accident.
CONDITION = "no useful match"
SCOPE = "all markdown task artifacts below"
WHOLE_TREE = "whole tree"
FRAMING = "reading order, not a search boundary"

# Entry points. Each states the trigger and routes; none states the procedure.
ENTRY_POINTS = [
    "AGENTS.md",
    ".cursor/rules/context-discovery.mdc",
    "skills/task-artifacts/SKILL.md",
    "docs/task-execution.md",
]

# Steps that belong to the owner alone. An entry point repeating one of these has
# started keeping its own copy of the procedure again.
OWNER_ONLY_STEPS = [
    CONDITION,
    FRAMING,
    "--no-discover",
    "sibling repositories only after",
]

# A catalog file no script here writes. Every earlier copy of the rule opened by
# sending the reader to it.
ABSENT_CATALOG = "tasks/index.md"

TEXT_SUFFIXES = {".md", ".mdc", ".py", ".sh", ".json", ".toml", ".yml", ".yaml", ".txt"}

# Surfaces that describe the recency window to a reader.
SINCE_SURFACES = [OWNER, "skills/task-creator/SKILL.md"]


def normalized(text: str) -> str:
    """One line of lowercase text, so a rule wrapped across lines reads as one."""
    return re.sub(r"\s+", " ", text).lower()


def surface(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def tracked_text_files() -> list[str]:
    """Every text file Git publishes, which is the whole surface a reader gets."""
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, text=True, check=True).stdout
    return [name for name in listing.split("\0")
            if name and Path(name).suffix in TEXT_SUFFIXES]


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_entry_points_route_to_the_owner_by_path(name: str) -> None:
    """Naming the skill is not enough: a reader needs the file that owns the rule."""
    assert OWNER_PATH_REFERENCE in normalized(surface(name)), (
        f"{name} does not send the reader to {OWNER}, so it either carries its own "
        f"copy of the rule or drops it")


@pytest.mark.parametrize("name", ENTRY_POINTS)
@pytest.mark.parametrize("step", OWNER_ONLY_STEPS)
def test_entry_points_carry_no_step_of_the_procedure(name: str, step: str) -> None:
    assert step not in normalized(surface(name)), (
        f"{name} states `{step}`, which belongs to {OWNER}. An entry point owns the "
        f"trigger; a second statement of a step is how the three copies drifted apart")


def test_no_published_file_names_a_catalog_this_repository_does_not_build() -> None:
    """The owner alone is not enough: the contradiction lived in a neighbouring file.

    `tasks/INDEX.md` was declared canonical in `AGENTS.md`, used as a restore
    check, and offered as a template, while nothing wrote it. This is the check
    that fails when any of that comes back, in any file, including this one's own
    repository documentation.
    """
    this_file = str(Path(__file__).resolve().relative_to(REPO))
    offenders = [name for name in tracked_text_files()
                 if name != this_file
                 and ABSENT_CATALOG in normalized((REPO / name).read_text(encoding="utf-8"))]
    assert not offenders, (
        f"{', '.join(offenders)} name(s) tasks/INDEX.md; no script in this repository "
        f"writes it, so a reader sent there finds an empty catalog. The task catalog "
        f"is queried through skills/task-creator/scripts/tasks_index.py")


def test_the_escalation_condition_and_scope_are_one_instruction() -> None:
    """Split the two halves apart and each can be deleted without the other noticing."""
    text = normalized(surface(OWNER))
    assert CONDITION in text, "the owner does not say when to escalate past the recent window"
    condition = text.index(CONDITION)
    scope = text.index(SCOPE, condition)
    assert scope - condition < 160, (
        f"the condition and the whole-tree scope are {scope - condition} characters "
        f"apart; they must read as one instruction")
    assert WHOLE_TREE in text, "the owner does not say the escalation covers the whole tree"


def test_the_window_is_named_a_reading_order_not_a_boundary() -> None:
    assert FRAMING in normalized(surface(OWNER)), (
        "the owner does not say the window is a reading order rather than a search "
        "boundary, which is the sentence that stops the next agent from re-scoping it")


def test_the_owner_names_the_catalog_this_repository_actually_builds() -> None:
    assert "tasks_index.py query" in normalized(surface(OWNER)), (
        "the owner does not name the index query")


@pytest.mark.parametrize("name", SINCE_SURFACES)
def test_the_since_window_is_documented_as_a_creation_date(name: str) -> None:
    """A task created three weeks ago and finished yesterday is outside `--since 10d`."""
    text = normalized(surface(name))
    assert "--since" in text, f"{name} no longer describes --since"
    assert "creation date, not" in text, (
        f"{name} describes --since without saying `date` is the creation date and not "
        f"when work finished")
    assert "completed yesterday" in text, (
        f"{name} states the caveat without the case that makes it concrete")
