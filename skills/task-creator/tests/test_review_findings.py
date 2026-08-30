"""The findings from the repeat review of the one-table index (task 525).

Each test names the finding it closes and reproduces the reported condition
against the real script through ``TASKS_INDEX_ROOT``. Every one of them fails
against the implementation as it stood at the end of task 522 -- a test that is
green on both sides of a repair proves nothing about the repair.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from test_rebuild_contract import DB, SCRIPTS, make_task, repo, rows, run  # noqa: F401

sys.path.insert(0, str(SCRIPTS))


def write_task(directory: Path, frontmatter: str, body: str = "# t\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "task.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    (directory / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return directory


def frontmatter_of(directory: Path) -> dict:
    text = (directory / "task.md").read_text(encoding="utf-8")
    assert text.startswith("---")
    return yaml.safe_load(text[3:text.find("\n---", 3)])


# --- P1-1: a numbered task whose directory carries no number --------------------

@pytest.mark.parametrize("name", ["archive", "restore-point", "2026-05-26-openclaw-file-tools"])
def test_p1_1_a_directory_without_its_number_stops_allocation(repo: Path, name: str) -> None:
    """`tasks/archive/task.md` with `id: 90000` made the next add create 90001.

    The prefix check only fired when ``PREFIX_RE`` matched, so a directory with
    no numeric prefix at all had no blocking issue, joined ``MAX(id)`` and moved
    the allocator wherever its frontmatter pointed. That is the same failure the
    ``2026-05-26-...`` rename was meant to end.
    """
    make_task(repo, 129, "healthy")
    write_task(repo / "tasks" / name, textwrap.dedent("""\
        id: 90000
        slug: "archive"
        title: "An unpacked archive"
        date: 2026-05-26
        status: "completed"
        projects: []
        trips: []
        """))

    refused = run(repo, "add", "Next", "summary", "--json", check=False)
    assert refused.returncode != 0, f"add allocated anyway: {refused.stdout}"
    assert not list((repo / "tasks").glob("90001-*")), "the allocator followed the archive"

    lookup = run(repo, "query", "--number", "129", "--format", "json", check=False)
    assert lookup.returncode != 0, lookup.stdout

    report = run(repo, "check", check=False)
    assert report.returncode != 0
    assert name in report.stdout + report.stderr


def test_p1_1_a_date_named_directory_is_never_its_own_task_number(repo: Path) -> None:
    """`2026-05-26-...` with `id: 2026` agrees with its prefix and still lies.

    The year matching the frontmatter is exactly how an unpacked archive
    directory once presented itself as task 2026. A date-shaped name carries no
    task number no matter which number the frontmatter claims.
    """
    make_task(repo, 129, "healthy")
    write_task(repo / "tasks" / "2026-05-26-openclaw-file-tools", textwrap.dedent("""\
        id: 2026
        slug: "2026-05-26-openclaw-file-tools"
        title: "Archived under its date"
        date: 2026-05-26
        status: "completed"
        projects: []
        trips: []
        """))

    refused = run(repo, "add", "Next", "summary", "--json", check=False)
    assert refused.returncode != 0, f"add allocated anyway: {refused.stdout}"
    assert not list((repo / "tasks").glob("2027-*"))


def test_p1_1_a_well_named_task_still_allocates(repo: Path) -> None:
    """The guard must not turn every ordinary tree into a blocked one."""
    make_task(repo, 129, "healthy")
    make_task(repo, 130, "also-healthy")
    assert json.loads(run(repo, "add", "Next", "summary", "--json").stdout)["id"] == 131
    run(repo, "check")


# --- P1-2: valid multiline YAML must be writable -------------------------------

BLOCK_LIST_TASK = textwrap.dedent("""\
    id: 423
    slug: "423-example-research-cycle"
    title: "Run example research cycle"
    date: 2026-07-22
    status: "completed"
    projects:
      - data/projects/example-project/project.md
    trips: []
    """)


def test_p1_2_a_block_list_projects_can_be_rewritten(repo: Path) -> None:
    """The real shape of 44 of 419 live tasks, task 423 among them.

    `set_frontmatter` replaced the `projects:` line and left `  - data/...`
    behind, so the candidate parsed as a list whose first element was a mapping
    and the write was refused. The one supported write path could not touch a
    legitimate task.
    """
    directory = write_task(repo / "tasks" / "423-example-research-cycle",
                           BLOCK_LIST_TASK)
    for name in ("example-project", "second-example"):
        (repo / "data" / "projects" / name).mkdir(parents=True)
        (repo / "data" / "projects" / name / "project.md").write_text("# p\n", encoding="utf-8")

    run(repo, "set-projects", "423", "data/projects/second-example/project.md")

    fields = frontmatter_of(directory)
    assert fields["projects"] == ["data/projects/second-example/project.md"]
    assert fields["trips"] == [] and fields["id"] == 423
    assert fields["title"] == "Run example research cycle"
    listed = json.loads(run(repo, "query", "--number", "423", "--format", "json").stdout)
    assert listed[0]["projects"] == ["data/projects/second-example/project.md"]


def test_p1_2_a_block_scalar_status_detail_can_be_rewritten(repo: Path) -> None:
    directory = write_task(repo / "tasks" / "129-block-scalar", textwrap.dedent("""\
        id: 129
        slug: "129-block-scalar"
        title: "Block scalar"
        date: 2026-07-29
        status: "blocked"
        status_detail: |
          waiting on review
          and on the second reviewer
        projects: []
        trips: []
        """))

    run(repo, "set-status", "129", "blocked", "--detail", "verification_gap")

    fields = frontmatter_of(directory)
    assert fields["status_detail"] == "verification_gap"
    assert fields["projects"] == [] and fields["trips"] == []
    assert "and on the second reviewer" not in (directory / "task.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("style", ["|", ">"])
def test_p2_1_a_block_scalar_ending_in_a_hash_line_is_replaced_whole(
    repo: Path, style: str
) -> None:
    """Third review, P2-1: an indented `#` line inside a block scalar is data.

    `key_spans` backs trailing `#` lines out of a node span so that comments
    between two keys stay with the file. Inside a literal or folded scalar there
    is no comment to preserve, so the pushback left the tail of the old value
    behind: `set-status` exited 0 and the file still carried
    `  # private old detail` under a `status_detail: null`. The node this
    function claims to replace was not replaced.
    """
    directory = write_task(repo / "tasks" / "129-hash-tail", textwrap.dedent(f"""\
        id: 129
        slug: "129-hash-tail"
        title: "Hash tail"
        date: 2026-07-29
        status: "blocked"
        status_detail: {style}
          waiting on review
          # private old detail
        projects: []
        trips: []
        """))

    run(repo, "set-status", "129", "completed")

    after = (directory / "task.md").read_text(encoding="utf-8")
    assert "# private old detail" not in after, after
    assert "waiting on review" not in after, after
    fields = frontmatter_of(directory)
    assert fields["status"] == "completed"
    assert not fields.get("status_detail")
    assert fields["projects"] == [] and fields["trips"] == [] and fields["id"] == 129


def test_p2_1_a_real_comment_after_a_block_scalar_is_still_preserved(repo: Path) -> None:
    """The pushback exists for a reason and must survive the repair.

    A `#` line at column 0 after a block scalar is a genuine YAML comment: the
    scalar ended at the dedent. It belongs to the file, not to the key above it.
    """
    directory = write_task(repo / "tasks" / "129-real-comment", textwrap.dedent("""\
        id: 129
        slug: "129-real-comment"
        title: "Real comment"
        date: 2026-07-29
        status: "blocked"
        status_detail: |
          waiting on review
        # a genuine comment between two keys
        projects: []
        trips: []
        """))

    run(repo, "set-status", "129", "completed")

    after = (directory / "task.md").read_text(encoding="utf-8")
    assert "# a genuine comment between two keys" in after, after
    assert "waiting on review" not in after, after
    assert frontmatter_of(directory)["status"] == "completed"


def test_p1_2_unrelated_keys_and_comments_stay_byte_stable(repo: Path) -> None:
    """Replacing a node span is not re-serializing the document."""
    directory = write_task(repo / "tasks" / "129-stable", textwrap.dedent("""\
        id: 129
        slug: '129-stable'          # single quotes, trailing comment
        title: "Keep my quoting"
        date: 2026-07-29
        status: planned
        # a comment line between two keys
        projects:
          - data/projects/keep/project.md
          - data/projects/drop/project.md
        # a comment before the last key
        trips: []
        """))
    for name in ("keep", "drop"):
        (repo / "data" / "projects" / name).mkdir(parents=True)
        (repo / "data" / "projects" / name / "project.md").write_text("# p\n", encoding="utf-8")

    run(repo, "set-projects", "129", "data/projects/keep/project.md")
    after = (directory / "task.md").read_text(encoding="utf-8")

    assert "slug: '129-stable'          # single quotes, trailing comment" in after
    assert "# a comment line between two keys" in after
    assert "# a comment before the last key" in after
    assert "data/projects/drop/project.md" not in after
    fields = frontmatter_of(directory)
    assert fields["projects"] == ["data/projects/keep/project.md"]
    assert fields["trips"] == []


def test_p1_2_a_block_list_as_the_last_key_can_be_rewritten(repo: Path) -> None:
    """No following key to stop at: the span ends at the block terminator."""
    directory = write_task(repo / "tasks" / "129-last-key", textwrap.dedent("""\
        id: 129
        slug: "129-last-key"
        title: "Last key"
        date: 2026-07-29
        status: "planned"
        projects: []
        trips:
          - data/trips/kyoto/trip.md
        """))
    (repo / "data" / "trips" / "kyoto").mkdir(parents=True)
    (repo / "data" / "trips" / "kyoto" / "trip.md").write_text("# t\n", encoding="utf-8")

    run(repo, "set-trips", "129")
    assert frontmatter_of(directory)["trips"] == []
    assert "data/trips/kyoto" not in (directory / "task.md").read_text(encoding="utf-8")


def test_p1_2_a_write_that_would_not_parse_is_still_refused(repo: Path) -> None:
    """The candidate-parse gate is the safety net and must survive the repair."""
    directory = write_task(repo / "tasks" / "129-fragile", BLOCK_LIST_TASK.replace("423", "129"))
    (repo / "data" / "projects" / "example-project").mkdir(parents=True)
    (repo / "data" / "projects" / "example-project" / "project.md").write_text(
        "# p\n", encoding="utf-8")
    before = (directory / "task.md").read_text(encoding="utf-8")

    result = run(repo, "set-title", "129", "broken\n---\nid: 999", check=False)
    if result.returncode != 0:
        assert (directory / "task.md").read_text(encoding="utf-8") == before
    else:
        assert frontmatter_of(directory)["id"] == 129


# --- P2-1: check must not crash on a missing task.md ---------------------------

def test_p2_1_check_reports_a_missing_task_md_instead_of_a_traceback(repo: Path) -> None:
    make_task(repo, 129, "healthy")
    make_task(repo, 130, "gutted")
    run(repo, "reindex")
    (repo / "tasks" / "130-gutted" / "task.md").unlink()

    report = run(repo, "check", check=False)
    combined = report.stdout + report.stderr
    assert report.returncode == 1, combined
    assert "Traceback" not in combined and "FileNotFoundError" not in combined
    assert "task.md is missing" in combined


# --- P2-2: the documented link CLI must exist ----------------------------------

def test_p2_2_set_projects_add_appends_without_duplicating(repo: Path) -> None:
    directory = make_task(repo, 129, "linked")
    for name in ("one", "two"):
        (repo / "data" / "projects" / name).mkdir(parents=True)
        (repo / "data" / "projects" / name / "project.md").write_text("# p\n", encoding="utf-8")

    run(repo, "set-projects", "129", "data/projects/one/project.md")
    run(repo, "set-projects", "129", "data/projects/two/project.md", "--add")
    run(repo, "set-projects", "129", "data/projects/one/project.md", "--add")

    assert frontmatter_of(directory)["projects"] == [
        "data/projects/one/project.md", "data/projects/two/project.md",
    ]
    run(repo, "set-projects", "129")
    assert frontmatter_of(directory)["projects"] == []


def test_p2_2_a_durable_record_can_be_looked_up_by_its_short_name(repo: Path) -> None:
    """`--project example-project` is what the skill and human docs promise."""
    make_task(repo, 129, "alpha", projects=["data/projects/example-project/project.md"])
    make_task(repo, 130, "beta", projects=["data/projects/second-example/status.md"])

    listed = json.loads(run(
        repo, "query", "--status", "all", "--project", "example-project", "--format", "json",
    ).stdout)
    assert [r["id"] for r in listed] == [129], listed

    # A short name is a record name, not a fragment to match against.
    assert json.loads(run(
        repo, "query", "--status", "all", "--project", "example", "--format", "json",
    ).stdout) == []
    assert json.loads(run(
        repo, "query", "--status", "all", "--project", "data/projects/example", "--format", "json",
    ).stdout) == []


def test_p2_2_generated_body_links_resolve_from_the_task_directory(repo: Path) -> None:
    (repo / "data" / "projects" / "lab").mkdir(parents=True)
    (repo / "data" / "projects" / "lab" / "project.md").write_text("# p\n", encoding="utf-8")

    created = json.loads(run(
        repo, "add", "Linked", "summary", "--project", "data/projects/lab/project.md", "--json",
    ).stdout)
    directory = repo / created["path"]
    body = (directory / "task.md").read_text(encoding="utf-8")

    target = next(line for line in body.splitlines() if "](" in line and "lab" in line)
    link = target[target.index("](") + 2: target.rindex(")")]
    assert (directory / link).resolve() == (repo / "data/projects/lab/project.md").resolve(), link


# --- P2-4: a partially shaped version-1 database is not trusted ----------------

@pytest.mark.parametrize("mutilate", [
    "ALTER TABLE tasks DROP COLUMN trips_json",
    "ALTER TABLE tasks DROP COLUMN indexed_at",
])
def test_p2_4_a_version_1_table_missing_a_required_column_is_rebuilt(
    repo: Path, mutilate: str
) -> None:
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    connection = sqlite3.connect(repo / DB, isolation_level=None)
    connection.execute(mutilate)
    connection.close()

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert [r["id"] for r in listed] == [129]
    connection = sqlite3.connect(repo / DB)
    try:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(tasks)")}
        assert {"trips_json", "indexed_at"} <= columns
    finally:
        connection.close()


def test_p2_4_a_version_1_table_without_the_unique_identity_is_rebuilt(repo: Path) -> None:
    """Without UNIQUE(id) a repeated number reaches the table instead of `issues`."""
    make_task(repo, 129, "seed")
    run(repo, "reindex")
    connection = sqlite3.connect(repo / DB, isolation_level=None)
    connection.execute("ALTER TABLE tasks RENAME TO tasks_old")
    connection.execute("CREATE TABLE tasks (path TEXT PRIMARY KEY, id INTEGER, slug TEXT NOT NULL,"
                       " title TEXT NOT NULL, date TEXT, status TEXT NOT NULL,"
                       " status_detail TEXT, projects_json TEXT NOT NULL DEFAULT '[]',"
                       " trips_json TEXT NOT NULL DEFAULT '[]', task_mtime_ns INTEGER NOT NULL,"
                       " task_size INTEGER NOT NULL, indexed_at TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO tasks (path, id, slug, title, date, status, status_detail, "
        "projects_json, trips_json, task_mtime_ns, task_size, indexed_at) "
        "SELECT path, id, slug, title, date, status, status_detail, projects_json, "
        "trips_json, task_mtime_ns, task_size, indexed_at FROM tasks_old"
    )
    connection.execute("DROP TABLE tasks_old")
    connection.close()

    run(repo, "query", "--status", "all", "--format", "json")
    connection = sqlite3.connect(repo / DB)
    connection.row_factory = sqlite3.Row
    try:
        indexes = [dict(row) for row in connection.execute("PRAGMA index_list(tasks)")]
        on_id = [index for index in indexes if index["unique"] and [
            info[2] for info in connection.execute(f'PRAGMA index_info("{index["name"]}")')
        ] == ["id"]]
        assert on_id, indexes
    finally:
        connection.close()


# --- P2-5: a transition must not carry an old qualifier forward ----------------

def test_p2_5_a_status_transition_clears_a_qualifier_it_was_not_given(repo: Path) -> None:
    directory = make_task(repo, 129, "qualified", status="planned")
    run(repo, "set-status", "129", "blocked", "--detail", "waiting")
    assert frontmatter_of(directory)["status_detail"] == "waiting"

    run(repo, "set-status", "129", "completed")

    assert not frontmatter_of(directory).get("status_detail")
    listed = json.loads(run(repo, "query", "--number", "129", "--format", "json").stdout)
    assert listed[0]["status"] == "completed" and listed[0]["status_detail"] is None
    assert "waiting" not in run(repo, "query", "--number", "129", "--format", "compact").stdout


def test_p2_5_an_empty_title_is_refused_rather_than_announced(repo: Path) -> None:
    directory = make_task(repo, 129, "titled", title="The real title")
    result = run(repo, "set-title", "129", "   ", check=False)
    assert result.returncode != 0, result.stdout
    assert frontmatter_of(directory)["title"] == "The real title"


# --- default context discovery: the recency window ------------------------------

def dated(repo: Path, number: int, suffix: str, date: str, *, status: str = "completed") -> Path:
    directory = make_task(repo, number, suffix, status=status)
    text = (directory / "task.md").read_text(encoding="utf-8")
    (directory / "task.md").write_text(text.replace("date: 2026-07-29", f"date: {date}"),
                                       encoding="utf-8")
    return directory


@pytest.mark.parametrize("window,expect_cutoff", [
    ("10d", "2026-07-19"), ("1d", "2026-07-28"), ("0d", "2026-07-29"),
    ("2w", "2026-07-15"), ("1w", "2026-07-22"), ("2026-07-19", "2026-07-19"),
])
def test_discovery_window_parses_relative_and_absolute_bounds(window, expect_cutoff) -> None:
    import datetime
    import tasks_index

    assert tasks_index.since_cutoff(window, datetime.date(2026, 7, 29)) == expect_cutoff


@pytest.mark.parametrize("bad", ["10", "ten days", "10x", "", "2026-13-01", "-5d"])
def test_an_unparseable_window_is_refused_rather_than_guessed(repo: Path, bad: str) -> None:
    make_task(repo, 129, "seed")
    result = run(repo, "query", "--since", bad, "--format", "compact", check=False)
    assert result.returncode != 0, result.stdout
    assert "--since" in result.stderr


def test_the_window_includes_every_status_not_only_the_active_ones(repo: Path) -> None:
    """The point of the change: recent *completed* work is the likely exemplar.

    `--status active` returned old open work from unrelated projects while the
    task the user meant was finished last week.
    """
    dated(repo, 129, "recent-completed", "2026-07-28", status="completed")
    dated(repo, 130, "recent-cancelled", "2026-07-28", status="cancelled")
    dated(repo, 131, "recent-blocked", "2026-07-27", status="blocked")
    dated(repo, 132, "old-planned", "2026-01-05", status="planned")

    listed = json.loads(run(repo, "query", "--since", "3650d", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 130, 131, 132}

    active = json.loads(run(repo, "query", "--status", "active", "--format", "json").stdout)
    assert {r["id"] for r in active} == {131, 132}, "fixture must distinguish the two"


def test_the_window_boundary_is_inclusive_and_excludes_the_day_before(repo: Path) -> None:
    import datetime
    today = datetime.date.today()
    dated(repo, 129, "on-the-cutoff", (today - datetime.timedelta(days=10)).isoformat())
    dated(repo, 130, "just-inside", (today - datetime.timedelta(days=9)).isoformat())
    dated(repo, 131, "just-outside", (today - datetime.timedelta(days=11)).isoformat())

    listed = json.loads(run(repo, "query", "--since", "10d", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 130}, listed


def test_the_window_orders_by_recency_not_by_task_number(repo: Path) -> None:
    """Tasks 001-022 are recent work carrying low numbers; id order is not chronology."""
    dated(repo, 500, "old-but-high-numbered", "2026-07-20")
    dated(repo, 3, "recent-but-low-numbered", "2026-07-28")
    dated(repo, 400, "middle", "2026-07-24")

    listed = json.loads(run(repo, "query", "--since", "3650d", "--format", "json").stdout)
    assert [r["id"] for r in listed] == [3, 400, 500], listed


def test_the_window_order_is_deterministic_within_one_day(repo: Path) -> None:
    dated(repo, 7, "same-day-low", "2026-07-28")
    dated(repo, 9, "same-day-high", "2026-07-28")
    dated(repo, 8, "same-day-middle", "2026-07-28")

    for _ in range(3):
        listed = json.loads(run(repo, "query", "--since", "3650d", "--format", "json").stdout)
        assert [r["id"] for r in listed] == [9, 8, 7], listed


def test_a_task_without_a_date_cannot_be_recent_but_stays_visible(repo: Path) -> None:
    directory = make_task(repo, 129, "undated")
    text = (directory / "task.md").read_text(encoding="utf-8")
    (directory / "task.md").write_text(text.replace("date: 2026-07-29\n", ""), encoding="utf-8")
    dated(repo, 130, "dated", "2026-07-28")

    windowed = json.loads(run(repo, "query", "--since", "3650d", "--format", "json").stdout)
    assert {r["id"] for r in windowed} == {130}
    everything = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in everything} == {129, 130}


def test_the_compact_header_states_the_window_and_the_order(repo: Path) -> None:
    dated(repo, 129, "recent", "2026-07-28")
    header = run(repo, "query", "--since", "3650d", "--format", "compact").stdout.splitlines()[0]
    assert "since " in header and "newest first" in header, header


def test_the_default_listing_order_is_unchanged_by_the_new_window(repo: Path) -> None:
    """`--since` adds an order; it must not change the one every other caller sees."""
    dated(repo, 500, "old-but-high-numbered", "2026-07-20")
    dated(repo, 3, "recent-but-low-numbered", "2026-07-28")

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert [r["id"] for r in listed] == [500, 3], listed


def test_no_task_content_or_full_text_search_is_stored_in_the_index(repo: Path) -> None:
    """The table stays a rebuildable metadata index; content recovery stays in tasks/."""
    make_task(repo, 129, "seed", body_extra="\n## Summary\nA distinctive body sentence.\n")
    run(repo, "reindex")

    connection = sqlite3.connect(repo / DB)
    try:
        objects = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert objects == {"tasks"}, objects
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert "A distinctive body sentence." not in dump
    assert "fts" not in dump.lower() and "VIRTUAL TABLE" not in dump.upper()

    source = (SCRIPTS / "tasks_index.py").read_text(encoding="utf-8")
    assert "fts4" not in source.lower() and "fts5" not in source.lower()


def test_p2_5_rename_normalizes_the_slug_and_refuses_an_empty_one(repo: Path) -> None:
    make_task(repo, 129, "before")
    refused = run(repo, "rename", "129", "Ещё Одна", check=False)  # normalizes to nothing
    assert refused.returncode != 0, refused.stdout
    assert (repo / "tasks" / "129-before").is_dir(), \
        "a slug that normalizes to nothing must be refused, not collapsed to 129-task"

    run(repo, "rename", "129", "A Better Slug")
    assert (repo / "tasks" / "129-a-better-slug").is_dir()
