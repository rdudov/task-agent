"""The lookup rule has one owner, and that owner states the rule completely.

This repository carried the rule three times: `AGENTS.md`, the always-on Cursor
rule, and `skills/task-artifacts/SKILL.md`. All three drifted, and all three
opened with `tasks/INDEX.md` -- a file no script in this repository generates,
so an agent that followed them found an empty catalog and concluded there was no
prior work. Three copies is how that survived: no single file was wrong on its
own terms.

So the checks below are of two kinds. The owner must state both halves of the
escalation rule in one clause, name the catalog that actually exists, and say
what the window means. Every other entry point must reach the owner by name and
carry no lookup order of its own.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

OWNER = "skills/context-discovery/SKILL.md"

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
    "skills/task-executor/SKILL.md",
]

# Surfaces that describe the recency window to a reader.
SINCE_SURFACES = [OWNER, "skills/task-creator/SKILL.md"]


def normalized(text: str) -> str:
    """One line of lowercase text, so a rule wrapped across lines reads as one."""
    return re.sub(r"\s+", " ", text).lower()


def surface(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_entry_points_route_to_the_owner(name: str) -> None:
    assert "context-discovery" in normalized(surface(name)), (
        f"{name} does not name the lookup owner, so it either carries its own "
        f"copy of the rule or drops it")


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
    """`tasks/INDEX.md` is not generated here; pointing at it returns an empty catalog."""
    text = normalized(surface(OWNER))
    assert "tasks_index.py query" in text, "the owner does not name the index query"
    assert "tasks/index.md" not in text, (
        "the owner points at tasks/INDEX.md, which no script in this repository writes")


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
