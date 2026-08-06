"""Tests for skills/repo-health/scripts/check_repo_health.py.

`check_tasks` and `check_secret_like_content` both take their root as an
argument, so every test drives the real function against a throwaway tree and
never touches the repository's own `tasks/`, `.state/`, or Git index.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repo_health.py"
_spec = importlib.util.spec_from_file_location("check_repo_health", SCRIPT)
health = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(health)


def seed_tree(root: Path, *, with_database: bool) -> Path:
    """A tree with one task directory, with or without the index database.

    No database at all is what a backup that restored `tasks/` and not `.state/`
    leaves behind. Since the redesign that is a recoverable state, not a loss:
    the table -- task numbers included -- is rebuilt from `tasks/` by the next
    command that touches it.
    """
    task_dir = root / "tasks" / "001-alpha"
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text("---\nid: 1\n---\n# Alpha\n", encoding="utf-8")
    (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    if with_database:
        (root / ".state").mkdir()
        (root / ".state" / "tasks-index.db").write_bytes(b"")
    return root


def test_a_missing_index_database_is_not_a_health_problem(tmp_path: Path):
    """Fails against 4051213, which reported a missing database as unrecoverable.

    That was true of the `id_allocation` ledger the file used to carry. The
    ledger is gone and `tasks/` is the source of truth for task numbers again,
    so demanding a restore here would report a loss that has not happened.
    """
    seed_tree(tmp_path, with_database=False)

    assert health.check_tasks(tmp_path, allow_empty_tasks=False) == []


def test_a_present_index_database_is_not_inspected(tmp_path: Path):
    """It is a rebuildable cache; its contents are `tasks_index.py check`'s business."""
    seed_tree(tmp_path, with_database=True)

    assert health.check_tasks(tmp_path, allow_empty_tasks=False) == []


def test_a_missing_task_artifact_is_still_a_health_problem(tmp_path: Path):
    """Control: dropping the ledger check must not blind the structural checks."""
    seed_tree(tmp_path, with_database=False)
    (tmp_path / "tasks" / "001-alpha" / "plan.md").unlink()

    errors = health.check_tasks(tmp_path, allow_empty_tasks=False)

    assert len(errors) == 1 and "missing plan.md" in errors[0]


def test_a_template_without_tasks_is_accepted(tmp_path: Path):
    """A sanitized copy carries no `tasks/`. Control against over-firing."""
    (tmp_path / "tasks").mkdir()

    assert health.check_tasks(tmp_path, allow_empty_tasks=True) == []


def test_a_hidden_directory_under_tasks_is_not_a_task(tmp_path: Path):
    """`tasks_index.py` stages new tasks in a hidden directory before publishing.

    Fails against 4051213, which reported a stray `tasks/.claude` as a task
    missing both `task.md` and `plan.md`.
    """
    seed_tree(tmp_path, with_database=False)
    (tmp_path / "tasks" / ".add-1234-abcd").mkdir()

    assert health.check_tasks(tmp_path, allow_empty_tasks=False) == []


# A literal that matches the "api token" pattern. Written in halves so this file
# never itself carries a contiguous token-shaped string.
LEAKED_TOKEN = "sk-" + "livekey" + "0123456789abcdefghij"


def seed_repository(root: Path) -> Path:
    """A throwaway Git repository ignoring `tasks/` exactly like this one does."""
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / ".gitignore").write_text("tasks/\ndata/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    return root


def track(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)
    return path


def test_a_secret_in_a_tracked_file_still_fails_the_gate(tmp_path: Path):
    """The negative control for narrowing the scope.

    Narrowing to what Git carries is only legitimate while the gate still fires
    on the thing it exists to catch. Without this test, "the scan passes on the
    current tree" would be indistinguishable from having turned the scan off.
    """
    seed_repository(tmp_path)
    track(tmp_path, "src/settings.py", f'API = "{LEAKED_TOKEN}"\n')

    errors = health.check_secret_like_content(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("src/settings.py: possible secret-like content")


def test_a_secret_in_a_new_untracked_file_still_fails_the_gate(tmp_path: Path):
    """A file a task just created is not tracked yet and is exactly its output."""
    seed_repository(tmp_path)
    (tmp_path / "notes.md").write_text(f"token: {LEAKED_TOKEN}\n", encoding="utf-8")

    errors = health.check_secret_like_content(tmp_path)

    assert len(errors) == 1 and errors[0].startswith("notes.md:")


def test_a_finished_task_snapshot_no_longer_blocks_every_other_task(tmp_path: Path):
    """Fails against the run that reported fifteen errors, none from the tree.

    `tasks/` and `data/` are `.gitignore`d in full, so nothing under them can
    reach a remote. Reporting a captured `runner.log` or a copied `.env.example`
    from a task that closed weeks ago blocked the closure of every *current*
    task, and the only offered remedy -- deleting the evidence of finished
    work -- is what this repository keeps those directories for.
    """
    seed_repository(tmp_path)
    snapshot = tmp_path / "tasks" / "683-old" / "review-source"
    snapshot.mkdir(parents=True)
    (snapshot / "runner.log").write_text(f"captured: {LEAKED_TOKEN}\n", encoding="utf-8")

    assert health.check_secret_like_content(tmp_path) == []


def test_an_empty_assignment_is_not_a_secret(tmp_path: Path):
    """Fails against the `\\s*` copy of the pattern, which matched a newline.

    `EXAMPLE_API_TOKEN=` with no value, followed by the next variable's name, is
    a committed template, not a leak. The old copy read the following line as the
    value and reported two `.env.example` files.
    """
    seed_repository(tmp_path)
    track(
        tmp_path,
        ".env.example",
        "EXAMPLE_API_TOKEN=\nNEXT_SETTING=\n",
    )

    assert health.check_secret_like_content(tmp_path) == []


def test_an_unestablishable_scope_is_reported_rather_than_passed(tmp_path: Path):
    """A gate that cannot see its subject must say so, not return success."""
    errors = health.check_secret_like_content(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("repository scope for the secret scan cannot be established")


def test_the_syntax_check_writes_nothing_into_the_tree(tmp_path: Path):
    """`py_compile` left a `__pycache__` behind and aborted on a read-only tree."""
    script = tmp_path / "skills" / "demo" / "scripts" / "tool.py"
    script.parent.mkdir(parents=True)
    script.write_text("value = 1\n", encoding="utf-8")

    assert health.check_scripts(tmp_path) == []
    assert not (script.parent / "__pycache__").exists()


def test_a_script_that_does_not_parse_is_still_reported(tmp_path: Path):
    """Control: dropping `py_compile` must not drop the check it performed."""
    script = tmp_path / "skills" / "demo" / "scripts" / "broken.py"
    script.parent.mkdir(parents=True)
    script.write_text("def broken(:\n", encoding="utf-8")

    errors = health.check_scripts(tmp_path)

    assert len(errors) == 1 and "Python syntax check failed" in errors[0]
