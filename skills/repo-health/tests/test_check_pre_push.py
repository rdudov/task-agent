from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pre_push.py"
SPEC = importlib.util.spec_from_file_location("check_pre_push_tested", SCRIPT)
assert SPEC and SPEC.loader
check_pre_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_pre_push)


def test_private_history_marker_is_rejected_but_neutral_example_passes(
    tmp_path: Path, monkeypatch
) -> None:
    leaked = tmp_path / "leaked.md"
    private_name = "private-example-project"
    markers = tmp_path / "markers"
    markers.write_text(f"# local only\n{private_name}\n", encoding="utf-8")
    monkeypatch.setenv(check_pre_push.PRIVATE_HISTORY_MARKERS_ENV, str(markers))
    leaked.write_text(f"data/projects/{private_name}/project.md\n", encoding="utf-8")
    neutral = tmp_path / "neutral.md"
    neutral.write_text("data/projects/example-project/project.md\n", encoding="utf-8")

    assert "private task/project history" in check_pre_push.scan_file(
        tmp_path, leaked.name, allow_local_artifacts=False
    )[0]
    assert check_pre_push.scan_file(tmp_path, neutral.name, allow_local_artifacts=False) == []


def test_explicit_missing_private_marker_file_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        check_pre_push.PRIVATE_HISTORY_MARKERS_ENV,
        str(tmp_path / "missing"),
    )
    with pytest.raises(RuntimeError, match="marker file is missing"):
        check_pre_push.private_history_markers(tmp_path)


def test_only_local_heads_and_selected_remote_refs_are_allowed(tmp_path: Path) -> None:
    refs = "\n".join(
        [
            "refs/heads/main",
            "refs/remotes/origin/main",
            "refs/remotes/companion/task-708",
            "refs/tags/release-1",
            "refs/notes/review",
            "refs/stash",
            "refs/replace/private-object",
        ]
    )
    with mock.patch.object(check_pre_push, "run_git", return_value=refs):
        assert check_pre_push.unexpected_refs("origin", tmp_path) == [
            "refs/remotes/companion/task-708",
            "refs/replace/private-object",
        ]
