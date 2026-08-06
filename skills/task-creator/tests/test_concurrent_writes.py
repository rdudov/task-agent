"""P1-1 from the third review: two legitimate lifecycle commands must both survive.

`add` held one ``BEGIN IMMEDIATE`` from discovery through publish; no other
write command did. `_write_command` closed discovery's transaction, replaced
`task.md` outside any lock, then opened a second transaction only to refresh the
row, and every process staged that replacement under the same name,
`task.md.tmp`. Two commands issued against one task therefore either lost a
field or died on a shared temporary file.

The reproduction here is deterministic rather than a race that is hoped to
recur. A wrapper process wraps ``key_spans`` -- called once per
``set_frontmatter``, after `task.md` has been read and before anything is
written, so it is exactly the read-modify-write window -- and holds there until
its peer has reached the same point or a bounded wait expires. Against the
unserialized implementation both processes reach it, so both read the same
`task.md` and the later replacement discards the other's field. Against a
serialized implementation the second process cannot reach it at all until the
first has committed, so its wait expires, the first finishes, and the second
reads what the first wrote.

Nothing here is a production hook: the instrumentation lives in the wrapper the
test writes, and the code under test is the installed script.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_rebuild_contract import SCRIPTS, make_task, repo, run  # noqa: F401
from test_review_findings import frontmatter_of

# How long a wrapper waits for its peer to reach the same window before giving
# up and proceeding. It is the whole cost of the serialized case, and it must
# stay comfortably below the tool's own busy timeout so the blocked process is
# still waiting on the lock when the wait expires.
PEER_WAIT_S = float(os.environ.get("TASKS_INDEX_TEST_PEER_WAIT_S", "4"))

BARRIER_WRAPPER = textwrap.dedent('''
    import os, sys, time
    from pathlib import Path

    sys.path.insert(0, os.environ["TASKS_INDEX_SCRIPTS"])
    import tasks_index

    barrier = Path(os.environ["PEER_BARRIER"])
    me, peer = os.environ["PEER_ME"], os.environ["PEER_OTHER"]
    wait_s = float(os.environ["PEER_WAIT_S"])
    original = tasks_index.key_spans
    reached = []

    def instrumented(block):
        """task.md has been read and nothing has been written yet."""
        if not reached:
            reached.append(True)
            (barrier / me).write_text("here", encoding="utf-8")
            limit = time.monotonic() + wait_s
            while not (barrier / peer).exists() and time.monotonic() < limit:
                time.sleep(0.01)
        return original(block)

    tasks_index.key_spans = instrumented
    raise SystemExit(tasks_index.main(sys.argv[1:]))
''')

REPLACE_WRAPPER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path

    sys.path.insert(0, os.environ["TASKS_INDEX_SCRIPTS"])
    import tasks_index

    staged = []
    real = os.replace

    def replace(src, dst):
        staged.append(str(src))
        return real(src, dst)

    os.replace = replace
    task_dir = Path(sys.argv[1])
    tasks_index.set_frontmatter(task_dir, {"title": json.dumps("first")})
    tasks_index.set_frontmatter(task_dir, {"title": json.dumps("second")})
    print(json.dumps(staged))
''')


def wrapper(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_two_lifecycle_commands_on_one_task_both_survive(repo: Path, tmp_path: Path) -> None:
    """`set-status` and `set-title`, synchronized after both have read the task.

    The review ran exactly this pair: the title command exited 0, the status
    command raised `FileNotFoundError` replacing the shared temporary file, and
    the task ended up carrying the new title while still `planned`.
    """
    directory = make_task(repo, 129, "contended", status="planned", title="Old title")
    run(repo, "reindex")

    barrier = tmp_path / "barrier"
    barrier.mkdir()
    script = wrapper(tmp_path, "barrier_wrapper.py", BARRIER_WRAPPER)

    def spawn(me: str, peer: str, *args: str) -> subprocess.Popen:
        environment = {
            **os.environ,
            "TASKS_INDEX_ROOT": str(repo),
            "TASKS_INDEX_SCRIPTS": str(SCRIPTS),
            "PEER_BARRIER": str(barrier),
            "PEER_ME": me,
            "PEER_OTHER": peer,
            "PEER_WAIT_S": str(PEER_WAIT_S),
        }
        return subprocess.Popen(
            [sys.executable, str(script), *args],
            cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    status = spawn("status", "title", "set-status", "129", "completed")
    title = spawn("title", "status", "set-title", "129", "New title")
    outcomes = {
        "set-status": status.communicate(timeout=120),
        "set-title": title.communicate(timeout=120),
    }
    codes = {"set-status": status.returncode, "set-title": title.returncode}
    report = "\n".join(f"{name} exited {codes[name]}\n  out: {out}\n  err: {err}"
                       for name, (out, err) in outcomes.items())

    assert codes == {"set-status": 0, "set-title": 0}, report

    fields = frontmatter_of(directory)
    assert fields["status"] == "completed", f"the status write was lost\n{report}"
    assert fields["title"] == "New title", f"the title write was lost\n{report}"

    listed = json.loads(run(repo, "query", "--number", "129", "--format", "json").stdout)
    assert listed[0]["status"] == "completed" and listed[0]["title"] == "New title", listed


def test_a_second_writer_sees_the_first_writers_field(repo: Path, tmp_path: Path) -> None:
    """The same contention through `set-projects --add`, whose write is a read-modify-write.

    Appending has to read the existing list first, so an unserialized `--add`
    can drop a link that was added between its read and its write -- and the
    dropped link is a durable project record the task is meant to be registered
    in.
    """
    make_task(repo, 129, "linked")
    for name in ("one", "two"):
        (repo / "data" / "projects" / name).mkdir(parents=True)
        (repo / "data" / "projects" / name / "project.md").write_text("# p\n", encoding="utf-8")
    run(repo, "reindex")

    barrier = tmp_path / "barrier"
    barrier.mkdir()
    script = wrapper(tmp_path, "barrier_wrapper.py", BARRIER_WRAPPER)

    def spawn(me: str, peer: str, *args: str) -> subprocess.Popen:
        environment = {
            **os.environ,
            "TASKS_INDEX_ROOT": str(repo),
            "TASKS_INDEX_SCRIPTS": str(SCRIPTS),
            "PEER_BARRIER": str(barrier),
            "PEER_ME": me,
            "PEER_OTHER": peer,
            "PEER_WAIT_S": str(PEER_WAIT_S),
        }
        return subprocess.Popen(
            [sys.executable, str(script), *args],
            cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    first = spawn("one", "two", "set-projects", "129", "data/projects/one/project.md", "--add")
    second = spawn("two", "one", "set-projects", "129", "data/projects/two/project.md", "--add")
    first_out = first.communicate(timeout=120)
    second_out = second.communicate(timeout=120)
    report = f"first {first.returncode} {first_out}\nsecond {second.returncode} {second_out}"

    assert (first.returncode, second.returncode) == (0, 0), report
    assert sorted(frontmatter_of(repo / "tasks" / "129-linked")["projects"]) == [
        "data/projects/one/project.md", "data/projects/two/project.md",
    ], f"an --add dropped the other writer's link\n{report}"


def test_the_staging_file_name_is_per_operation(repo: Path, tmp_path: Path) -> None:
    """Defence in depth, not the fix: two writers must not stage under one name.

    Every process used `task.md.tmp`, so the loser of a race replaced a
    temporary file the winner had already renamed away and died on
    `FileNotFoundError` instead of writing anything.
    """
    directory = make_task(repo, 129, "staged")
    script = wrapper(tmp_path, "replace_wrapper.py", REPLACE_WRAPPER)

    result = subprocess.run(
        [sys.executable, str(script), str(directory)],
        cwd=repo, capture_output=True, text=True, timeout=120,
        env={**os.environ, "TASKS_INDEX_ROOT": str(repo), "TASKS_INDEX_SCRIPTS": str(SCRIPTS)},
    )
    assert result.returncode == 0, result.stderr

    staged = json.loads(result.stdout)
    assert len(staged) == 2, staged
    assert staged[0] != staged[1], f"both writes staged under one name: {staged[0]}"
    assert not list(directory.glob("*.tmp")), "a staging file was left behind"


def test_a_write_command_does_not_block_when_nothing_contends(repo: Path) -> None:
    """Serialization must not turn an ordinary single write into a slow path."""
    import time

    make_task(repo, 129, "solo", status="planned")
    run(repo, "reindex")

    started = time.monotonic()
    for _ in range(3):
        run(repo, "set-status", "129", "in_progress")
        run(repo, "set-status", "129", "planned")
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"six uncontended writes took {elapsed:.1f}s"
    assert frontmatter_of(repo / "tasks" / "129-solo")["status"] == "planned"


@pytest.mark.parametrize("command", [
    ("set-status", "129", "completed"),
    ("set-title", "129", "Renamed"),
    ("set-projects", "129"),
])
def test_a_write_command_still_heals_the_index_it_locks(repo: Path, command) -> None:
    """Holding one lock must not cost the write commands their discovery pass."""
    make_task(repo, 129, "target", status="planned")
    run(repo, "reindex")
    make_task(repo, 400, "created-out-of-band")

    run(repo, *command)

    listed = json.loads(run(repo, "query", "--status", "all", "--format", "json").stdout)
    assert {r["id"] for r in listed} == {129, 400}, listed
