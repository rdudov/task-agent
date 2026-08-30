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


def test_private_history_marker_matching_is_case_insensitive() -> None:
    assert check_pre_push.private_history_match(
        "Private-Example-Project",
        ("private-example-project",),
    ) == "private-example-project"


def test_private_history_regular_expression_preserves_authored_case() -> None:
    marker = r"re:\bAtlas\b"

    assert check_pre_push.private_history_match("The Atlas client", (marker,)) == marker
    assert check_pre_push.private_history_match("atlas(value)", (marker,)) is None


def test_private_history_regular_expression_can_match_an_identifier_format() -> None:
    marker = r"re:\b[0-9a-f]{16}\b"
    example_identifier = "1a12" + "34567890abcd"

    assert check_pre_push.private_history_match(f"message {example_identifier}", (marker,)) == marker
    assert check_pre_push.private_history_match("message msg0000000000001", (marker,)) is None


def test_invalid_private_history_regular_expression_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "markers"
    path.write_text("re:[unterminated\n", encoding="utf-8")
    monkeypatch.setenv(check_pre_push.PRIVATE_HISTORY_MARKERS_ENV, str(path))

    with pytest.raises(RuntimeError, match="Invalid private-history regular expression"):
        check_pre_push.private_history_markers(tmp_path)


def test_explicit_missing_private_marker_file_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        check_pre_push.PRIVATE_HISTORY_MARKERS_ENV,
        str(tmp_path / "missing"),
    )
    with pytest.raises(RuntimeError, match="marker file is missing"):
        check_pre_push.private_history_markers(tmp_path)


def test_absent_marker_file_announces_that_the_name_check_did_not_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A missing marker list must never read as a silent pass.

    `.state/` is deployment-local, so a fresh clone has no marker file at all.
    The guard used to return an empty tuple and compare nothing while still
    printing "passed" -- a gate that lies in the operator's favour.
    """
    monkeypatch.delenv(check_pre_push.PRIVATE_HISTORY_MARKERS_ENV, raising=False)
    assert not (tmp_path / check_pre_push.PRIVATE_HISTORY_MARKERS_PATH).exists()

    markers = check_pre_push.private_history_markers(tmp_path)
    assert markers == ()

    notice = check_pre_push.private_history_notice(markers, tmp_path)
    assert notice is not None
    assert "did NOT run" in notice
    assert str(tmp_path / check_pre_push.PRIVATE_HISTORY_MARKERS_PATH) in notice


def test_marker_file_with_only_comments_also_announces(tmp_path: Path, monkeypatch) -> None:
    """An empty list is empty however it got that way."""
    path = tmp_path / "markers"
    path.write_text("# nothing configured yet\n\n", encoding="utf-8")
    monkeypatch.setenv(check_pre_push.PRIVATE_HISTORY_MARKERS_ENV, str(path))

    markers = check_pre_push.private_history_markers(tmp_path)
    assert markers == ()
    assert check_pre_push.private_history_notice(markers, tmp_path) is not None


def test_loaded_markers_produce_no_notice(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "markers"
    path.write_text("private-example-project\n", encoding="utf-8")
    monkeypatch.setenv(check_pre_push.PRIVATE_HISTORY_MARKERS_ENV, str(path))

    markers = check_pre_push.private_history_markers(tmp_path)
    assert markers == ("private-example-project",)
    assert check_pre_push.private_history_notice(markers, tmp_path) is None


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
