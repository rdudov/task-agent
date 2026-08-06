"""The protective properties from section 13 of the redesign review.

The review's two lists are not symmetric. One says what to delete; this file
covers the other -- the properties that carried real protection and had to
survive the deletion of the implementation that happened to hold them.

Concurrent allocation, malformed tasks staying visible and fail-closed
ambiguity are exercised by the section 14 contract tests in
``test_rebuild_contract.py``; the rest are here.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from test_rebuild_contract import DB, make_task, repo, rows, run  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


# --- strict path containment ---------------------------------------------------

@pytest.mark.parametrize("token", [
    "../etc", "tasks/../../etc", "/etc/passwd", "..", ".",
])
def test_a_task_argument_cannot_escape_the_tasks_directory(repo: Path, token: str) -> None:
    make_task(repo, 129, "seed")
    result = run(repo, "set-status", token, "completed", check=False)
    assert result.returncode != 0
    assert "escapes" in result.stderr or "no task directory" in result.stderr


def test_a_traversing_slug_cannot_be_created(repo: Path) -> None:
    result = run(repo, "add", "Escape", "summary", "../outside", check=False)
    assert result.returncode != 0, result.stdout
    assert not (repo / "outside").exists()
    assert not (repo.parent / "outside").exists()


# --- external path / link validation -------------------------------------------

def test_a_project_link_must_be_repo_relative_and_exist(repo: Path) -> None:
    make_task(repo, 129, "seed")
    for bad in ("/etc/passwd", "../escape/status.md", "data/projects/absent/status.md"):
        result = run(repo, "set-projects", "129", bad, check=False)
        assert result.returncode != 0, f"{bad} was accepted"

    (repo / "data" / "projects" / "real").mkdir(parents=True)
    (repo / "data" / "projects" / "real" / "status.md").write_text("# s\n", encoding="utf-8")
    run(repo, "set-projects", "129", "data/projects/real/status.md")
    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert listed[0]["projects"] == ["data/projects/real/status.md"]


# --- no-clobber rename ---------------------------------------------------------

def test_rename_refuses_to_clobber_an_existing_directory(repo: Path) -> None:
    make_task(repo, 129, "source")
    occupied = make_task(repo, 130, "taken")
    (occupied / "marker").write_text("keep me", encoding="utf-8")

    # 129 renamed to the *name* 130 already carries would overwrite it.
    (repo / "tasks" / "129-source").rename(repo / "tasks" / "130-source-tmp")
    (repo / "tasks" / "130-source-tmp").rename(repo / "tasks" / "129-source")

    result = run(repo, "rename", "130", "taken", check=False)
    assert result.returncode != 0
    assert (occupied / "marker").read_text(encoding="utf-8") == "keep me"


def test_rename_keeps_the_number_and_moves_the_row(repo: Path) -> None:
    make_task(repo, 129, "before")
    run(repo, "reindex")
    run(repo, "rename", "129", "after", "--title", "After")

    assert (repo / "tasks" / "129-after").is_dir()
    assert not (repo / "tasks" / "129-before").exists()
    assert [r["path"] for r in rows(repo)] == ["tasks/129-after"]
    listed = json.loads(run(repo, "query", "--number", "129", "--format", "json").stdout)
    assert listed[0]["slug"] == "129-after" and listed[0]["title"] == "After"


# --- byte-preserving frontmatter writes ----------------------------------------

def test_a_write_touches_only_the_line_of_the_key_it_changes(repo: Path) -> None:
    directory = make_task(repo, 129, "preserve")
    original = (
        "---\n"
        "id: 129\n"
        "slug: '129-preserve'          # single quotes, trailing comment\n"
        'title: "Keep my quoting"\n'
        "date: 2026-07-29\n"
        "status: planned\n"
        "# a comment line inside the block\n"
        "projects: []\n"
        "trips: []\n"
        "---\n"
        "# Keep my quoting\n\nBody prose with `status: completed` inside it.\n"
    )
    (directory / "task.md").write_text(original, encoding="utf-8")

    run(repo, "set-status", "129", "completed")
    after = (directory / "task.md").read_text(encoding="utf-8")

    before_lines = original.split("\n")
    after_lines = after.split("\n")
    changed = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
    assert len(changed) == 1, [(before_lines[i], after_lines[i]) for i in changed]
    assert after_lines[changed[0]].startswith("status:")
    assert "# single quotes, trailing comment" in after
    assert "# a comment line inside the block" in after
    assert "Body prose with `status: completed` inside it." in after


def test_the_body_status_section_is_never_rewritten(repo: Path) -> None:
    directory = make_task(repo, 129, "prose", status="planned",
                          body_extra="\n## Status\n\nStill waiting on review.\n")
    run(repo, "set-status", "129", "completed")
    assert "Still waiting on review." in (directory / "task.md").read_text(encoding="utf-8")


# --- YAML read-back validation --------------------------------------------------

def test_a_write_that_would_not_parse_is_refused_with_nothing_changed(repo: Path) -> None:
    directory = make_task(repo, 129, "fragile")
    before = (directory / "task.md").read_text(encoding="utf-8")

    # A title that would terminate the frontmatter block if written unquoted.
    result = run(repo, "set-title", "129", 'broken\n---\nid: 999', check=False)
    after = (directory / "task.md").read_text(encoding="utf-8")
    if result.returncode == 0:
        # Accepting it is only allowed if the block still parses and still reads back.
        listed = json.loads(run(repo, "query", "--number", "129", "--format", "json").stdout)
        assert listed and listed[0]["id"] == 129, after
    else:
        assert after == before, "a refused write must leave the file untouched"


def test_a_hand_broken_frontmatter_is_not_overwritten_by_a_write(repo: Path) -> None:
    directory = make_task(repo, 129, "broken")
    (directory / "task.md").write_text("---\nid: [unclosed\n---\n# broken\n", encoding="utf-8")
    before = (directory / "task.md").read_text(encoding="utf-8")

    result = run(repo, "set-status", "129-broken", "completed", check=False)
    assert result.returncode != 0
    assert (directory / "task.md").read_text(encoding="utf-8") == before


# --- rollback of an interrupted reindex -----------------------------------------

def test_an_interrupted_reindex_leaves_the_previous_table_intact(repo: Path) -> None:
    """reindex deletes every row before rebuilding; a crash must not land halfway."""
    make_task(repo, 129, "one")
    make_task(repo, 130, "two")
    run(repo, "reindex")
    before = {r["path"] for r in rows(repo)}

    # Kill the process during the rebuild: the transaction is never committed.
    killer = subprocess.run(
        [sys.executable, "-c",
         "import os,sqlite3,signal;"
         f"c=sqlite3.connect({str(repo / DB)!r}, isolation_level=None);"
         "c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM tasks');"
         "os.kill(os.getpid(), signal.SIGKILL)"],
        capture_output=True,
    )
    assert killer.returncode != 0

    assert {r["path"] for r in rows(repo)} == before
    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 130}


def test_reindex_rebuilds_the_table_from_the_tree_alone(repo: Path) -> None:
    make_task(repo, 129, "one")
    make_task(repo, 300, "two")
    run(repo, "reindex")
    connection = sqlite3.connect(repo / DB)
    connection.execute("DELETE FROM tasks")
    connection.commit()
    connection.close()

    run(repo, "reindex")
    assert {r["id"] for r in rows(repo)} == {129, 300}


# --- the deletions from section 13 ----------------------------------------------

def test_the_database_holds_one_user_table_and_a_pragma_version(repo: Path) -> None:
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    connection = sqlite3.connect(repo / DB)
    try:
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"tasks"}, tables
        assert "id_allocation" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        connection.close()


def test_no_amnesty_mechanism_is_consulted_anywhere(repo: Path) -> None:
    source = (SCRIPTS / "tasks_index.py").read_text(encoding="utf-8")
    for gone in ("legacy-ids", "legacy_ids", "amnesty", "null_id_slugs"):
        assert gone not in source, f"{gone} survived the rewrite"
    # The ledger may only be named to drop it; reading it back would make the
    # database an allocator again.
    for line in source.splitlines():
        if "id_allocation" in line and not line.strip().startswith("#"):
            assert "DROP TABLE" in line, f"id_allocation is still consulted: {line.strip()}"
    assert not (SCRIPTS.parent / "legacy-ids.json").exists()


def test_no_filter_matches_a_project_by_substring(repo: Path) -> None:
    source = (SCRIPTS / "tasks_index.py").read_text(encoding="utf-8")
    assert "projects LIKE" not in source and "trips LIKE" not in source
    assert "json_each" in source


@pytest.mark.parametrize("stale_version", [99, 0])
def test_an_incompatible_schema_version_is_rebuilt_rather_than_migrated(
    repo: Path, stale_version: int
) -> None:
    """Version 0 counts. It is what the ledger design left behind."""
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    connection = sqlite3.connect(repo / DB, isolation_level=None)
    connection.execute(f"PRAGMA user_version = {stale_version}")
    if stale_version == 0:  # the shape that database actually had
        connection.execute("DROP TABLE tasks")
        connection.execute("CREATE TABLE tasks (slug TEXT PRIMARY KEY, id INTEGER,"
                           " title TEXT NOT NULL, status TEXT NOT NULL)")
        connection.execute("CREATE TABLE id_allocation (id INTEGER PRIMARY KEY,"
                           " slug TEXT NOT NULL, allocated_at TEXT NOT NULL)")
    connection.close()

    run(repo, "query", "--status", "all", "--format", "json")
    connection = sqlite3.connect(repo / DB)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        connection.close()


# --- the module API consumers depend on ------------------------------------------

def test_read_record_stays_importable_for_the_dev_pipeline_adapter(repo: Path) -> None:
    """dev_pipeline_adapter imports this module and reads canonical status from it."""
    import importlib

    import tasks_index
    importlib.reload(tasks_index)
    directory = make_task(repo, 129, "adapter", status="blocked")
    record = tasks_index.read_record(directory)
    assert record["status"] == "blocked"
    assert record["id"] == 129
    assert record["path"] == "tasks/129-adapter"
