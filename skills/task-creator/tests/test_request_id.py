import json
import os
import subprocess
import sys
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "scripts" / "tasks_index.py"


def run(repo: Path, *args: str):
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=repo,
        env={**os.environ, "TASKS_INDEX_ROOT": str(repo)},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_request_id_replays_the_original_allocation_after_index_rebuild(tmp_path):
    (tmp_path / "tasks").mkdir()
    first = run(tmp_path, "add", "Remote task", "request", "telegram", "--json",
                "--request-id", "remote-request-envelope-1")
    second = run(tmp_path, "add", "Different title", "different", "other", "--json",
                 "--request-id", "remote-request-envelope-1")
    assert second["id"] == first["id"]
    assert second["path"] == first["path"]
    assert second["reused"] is True
    assert len(list((tmp_path / "tasks").glob("[0-9]*"))) == 1

    (tmp_path / ".state" / "tasks-index.db").unlink()
    third = run(tmp_path, "add", "Third title", "third", "third", "--json",
                "--request-id", "remote-request-envelope-1")
    assert third["path"] == first["path"]
    assert third["reused"] is True


def test_distinct_request_ids_allocate_distinct_tasks(tmp_path):
    (tmp_path / "tasks").mkdir()
    first = run(tmp_path, "add", "One", "request", "one", "--json", "--request-id", "one")
    second = run(tmp_path, "add", "Two", "request", "two", "--json", "--request-id", "two")
    assert first["id"] != second["id"]
