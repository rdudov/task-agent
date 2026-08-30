"""The fifteen mandatory tests from section 14 of the task-index redesign review.

Every test drives the real script through ``TASKS_INDEX_ROOT`` against a
throwaway tree, so the code under test is the one that runs in production.

The design under test is one rebuildable table keyed by an explicit
repo-relative path, with a single ``discover_and_sync`` shared by ``query``,
``add``, ``reindex`` and the write commands. The load-bearing property is the
opposite of the lookup-only design these tests replace: the index is no longer
allowed to be stale, because the number allocator reads it.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TOOL = SCRIPTS / "tasks_index.py"
DB = ".state/tasks-index.db"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".state").mkdir()
    return tmp_path


def run(repo: Path, *args: str, check: bool = True, env: dict | None = None):
    environment = {**os.environ, "TASKS_INDEX_ROOT": str(repo)}
    if env:
        environment.update(env)
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=repo, env=environment, timeout=180,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def make_task(repo: Path, number: int, slug_suffix: str, *, status: str = "completed",
              title: str | None = None, projects: list[str] | None = None,
              trips: list[str] | None = None, body_extra: str = "") -> Path:
    """Create a task directory out of band, the way a restore or another agent would."""
    name = f"{number:03d}-{slug_suffix}"
    directory = repo / "tasks" / name
    directory.mkdir(parents=True)
    # Built without dedent: an un-indented body_extra would defeat it and leave
    # the whole frontmatter block indented, which is a broken fixture rather
    # than a broken task.
    frontmatter = (
        "---\n"
        f"id: {number}\n"
        f'slug: "{name}"\n'
        f'title: "{title or slug_suffix}"\n'
        "date: 2026-07-29\n"
        f'status: "{status}"\n'
        f"projects: {json.dumps(projects or [])}\n"
        f"trips: {json.dumps(trips or [])}\n"
        "---\n"
        f"# {title or slug_suffix}\n{body_extra}"
    )
    (directory / "task.md").write_text(frontmatter, encoding="utf-8")
    (directory / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return directory


def rows(repo: Path) -> list[dict]:
    connection = sqlite3.connect(repo / DB)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute("SELECT * FROM tasks")]
    finally:
        connection.close()


def added_number(result: subprocess.CompletedProcess) -> int:
    return int(json.loads(result.stdout)["id"])


# 1 ---------------------------------------------------------------------------
def test_01_allocation_survives_losing_the_database(repo: Path) -> None:
    """rm DB; query; add issues max(disk)+1.

    This is the property the deleted ledger did not have: identity is
    reconstructible from the task tree alone.
    """
    make_task(repo, 129, "first")
    make_task(repo, 300, "highest")
    run(repo, "reindex")
    (repo / DB).unlink()

    run(repo, "query", "--format", "json")
    result = run(repo, "add", "After restore", "summary", "--json")
    assert added_number(result) == 301


# 2 ---------------------------------------------------------------------------
def test_02_query_discovers_a_directory_created_outside_the_table(repo: Path) -> None:
    make_task(repo, 129, "indexed")
    run(repo, "reindex")
    make_task(repo, 130, "created-out-of-band")

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 130}


# 3 ---------------------------------------------------------------------------
def test_03_add_discovers_an_unindexed_high_id_before_allocating(repo: Path) -> None:
    """The specific failure the review names: add and query must share discovery.

    An add that allocated from a table it had not synced would reissue 400.
    """
    make_task(repo, 129, "indexed")
    run(repo, "reindex")
    make_task(repo, 400, "unindexed-high")

    result = run(repo, "add", "Next", "summary", "--json")
    assert added_number(result) == 401


# 4 ---------------------------------------------------------------------------
def test_04_query_refreshes_a_row_whose_fingerprint_changed(repo: Path) -> None:
    directory = make_task(repo, 129, "changing", status="planned")
    run(repo, "reindex")

    text = (directory / "task.md").read_text(encoding="utf-8")
    (directory / "task.md").write_text(
        text.replace('status: "planned"', 'status: "completed"'), encoding="utf-8"
    )
    os.utime(directory / "task.md", (1, 1))  # a different fingerprint, not a later one

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert listed[0]["status"] == "completed"


# 5 ---------------------------------------------------------------------------
def test_05_query_returns_an_explicit_repo_relative_path(repo: Path) -> None:
    make_task(repo, 129, "pathful")

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert listed[0]["path"] == "tasks/129-pathful"
    assert not Path(listed[0]["path"]).is_absolute()


# 6 ---------------------------------------------------------------------------
def test_06_sixteen_concurrent_adds_get_sixteen_distinct_numbers(repo: Path) -> None:
    make_task(repo, 129, "seed")
    run(repo, "reindex")

    def allocate(index: int) -> int:
        return added_number(run(repo, "add", f"Concurrent {index}", "summary", "--json"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        numbers = sorted(pool.map(allocate, range(16)))

    assert numbers == list(range(130, 146)), numbers


# 7 ---------------------------------------------------------------------------
def test_07_crash_after_directory_rename_recovers_on_next_query(repo: Path) -> None:
    """Filesystem and SQLite share no transaction; recovery is part of the contract."""
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    make_task(repo, 130, "renamed-but-uncommitted")  # the directory the crash left behind

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 130}
    assert added_number(run(repo, "add", "Next", "summary", "--json")) == 131


# 8 ---------------------------------------------------------------------------
def test_08_rolled_back_insert_does_not_reissue_the_number(repo: Path) -> None:
    """The directory survives a rolled-back transaction and still owns its number."""
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    make_task(repo, 130, "directory-without-row")

    connection = sqlite3.connect(repo / DB)
    try:  # the row the rollback discarded is genuinely absent
        assert connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE id = 130"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    assert added_number(run(repo, "add", "Next", "summary", "--json")) == 131


# 9 ---------------------------------------------------------------------------
def test_09_rename_after_crash_reconciles_by_unique_id(repo: Path) -> None:
    """Old path gone, new path with the same id: update the row, never insert a second."""
    directory = make_task(repo, 129, "old-name")
    run(repo, "reindex")

    moved = repo / "tasks" / "129-new-name"
    directory.rename(moved)
    text = (moved / "task.md").read_text(encoding="utf-8")
    (moved / "task.md").write_text(
        text.replace('slug: "129-old-name"', 'slug: "129-new-name"'), encoding="utf-8"
    )

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert len(listed) == 1, listed
    assert listed[0]["path"] == "tasks/129-new-name"
    assert [r["path"] for r in rows(repo)] == ["tasks/129-new-name"]


# 10 --------------------------------------------------------------------------
def test_10_a_row_whose_directory_is_missing_is_not_silently_deleted(repo: Path) -> None:
    """Automatic deletion would mask a partial restore."""
    import shutil

    make_task(repo, 129, "seed")
    make_task(repo, 130, "will-disappear")
    run(repo, "reindex")
    shutil.rmtree(repo / "tasks" / "130-will-disappear")

    run(repo, "query", "--status", "all", "--format", "json")
    assert 130 in {r["id"] for r in rows(repo)}

    report = run(repo, "check", check=False)
    assert "130" in report.stdout + report.stderr


# 11 --------------------------------------------------------------------------
def test_11_a_malformed_task_stays_visible(repo: Path) -> None:
    make_task(repo, 129, "healthy")
    broken = repo / "tasks" / "130-malformed"
    broken.mkdir()
    (broken / "task.md").write_text("---\nid: [unclosed\n---\n# broken\n", encoding="utf-8")
    (broken / "plan.md").write_text("# Plan\n", encoding="utf-8")

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert "tasks/130-malformed" in {r["path"] for r in listed}

    report = run(repo, "check", check=False)
    assert "130-malformed" in report.stdout + report.stderr


# 12 --------------------------------------------------------------------------
def test_12_project_filter_compares_json_elements_exactly(repo: Path) -> None:
    """LIKE '%...%' answers a question nobody asked.

    Filtering for a value that is merely a substring of a stored element must
    match nothing: the filter names an element, not a fragment of one.
    """
    make_task(repo, 129, "alpha", projects=["data/projects/example-project/project.md"])
    make_task(repo, 130, "beta", projects=["data/projects/second-example/status.md"])

    exact = json.loads(run(
        repo, "query", "--status", "all",
        "--project", "data/projects/example-project/project.md", "--format", "json",
    ).stdout)
    assert [r["id"] for r in exact] == [129], exact

    fragment = json.loads(run(
        repo, "query", "--status", "all",
        "--project", "data/projects/example", "--format", "json",
    ).stdout)
    assert fragment == [], f"substring matched an element it does not equal: {fragment}"


# 13 --------------------------------------------------------------------------
def test_13_read_only_mode_is_explicit_and_reports_staleness(repo: Path) -> None:
    """Lazy discovery means the default query writes. A read-only caller must say so."""
    make_task(repo, 129, "indexed")
    run(repo, "reindex")
    make_task(repo, 130, "created-after-index")

    result = run(repo, "query", "--status", "all", "--format", "json", "--no-discover")
    listed = json.loads(result.stdout)
    assert {r["id"] for r in listed} == {129}, "--no-discover must not sync"
    assert "stale" in result.stderr.lower(), result.stderr

    assert {r["id"] for r in json.loads(
        run(repo, "query", "--status", "all", "--format", "json").stdout
    )} == {129, 130}


# 14 --------------------------------------------------------------------------
def test_14_two_directories_claiming_one_id_is_a_blocking_diagnosable_issue(repo: Path) -> None:
    """No amnesty, no guessing.

    The legacy duplicates were resolved in the data by task 522, so a repeated
    number is now an error with nowhere to hide. Discovery must name both paths
    and refuse to allocate rather than pick one.
    """
    make_task(repo, 129, "first-claim")
    duplicate = repo / "tasks" / "129-second-claim"
    duplicate.mkdir()
    (duplicate / "task.md").write_text(textwrap.dedent("""\
        ---
        id: 129
        slug: "129-second-claim"
        title: "second"
        date: 2026-07-29
        status: "completed"
        projects: []
        trips: []
        ---
        # second
        """), encoding="utf-8")
    (duplicate / "plan.md").write_text("# Plan\n", encoding="utf-8")

    # An amnesty list must not excuse it, because no such mechanism may exist.
    legacy = repo / "skills" / "task-creator"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "legacy-ids.json").write_text(json.dumps({
        "duplicate_ids": {"129": ["129-first-claim", "129-second-claim"]},
        "null_id_slugs": [],
    }), encoding="utf-8")

    report = run(repo, "check", check=False)
    combined = report.stdout + report.stderr
    assert report.returncode != 0, combined
    assert "129-first-claim" in combined and "129-second-claim" in combined

    refused = run(repo, "add", "Next", "summary", "--json", check=False)
    assert refused.returncode != 0, refused.stdout

    lookup = run(repo, "query", "--number", "129", "--format", "json", check=False)
    assert lookup.returncode != 0 or len(json.loads(lookup.stdout or "[]")) == 2


# 15 --------------------------------------------------------------------------
def test_15_restore_from_tasks_only_ends_in_a_successful_add(repo: Path) -> None:
    """The review's restore gate, in miniature: no .state/ at all."""
    import shutil

    make_task(repo, 129, "one")
    make_task(repo, 250, "two")
    make_task(repo, 251, "three", status="planned")
    run(repo, "reindex")

    shutil.rmtree(repo / ".state")  # restore brought back tasks/ and data/ only

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 250, 251}
    assert {r["path"] for r in listed} == {
        "tasks/129-one", "tasks/250-two", "tasks/251-three",
    }

    assert added_number(run(repo, "add", "After restore", "summary", "--json")) == 252
    run(repo, "check")
