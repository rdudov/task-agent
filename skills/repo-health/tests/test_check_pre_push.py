from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pre_push.py"
SPEC = importlib.util.spec_from_file_location("check_pre_push_tested", SCRIPT)
assert SPEC and SPEC.loader
check_pre_push = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_pre_push)


def test_private_history_marker_is_rejected_but_neutral_example_passes(tmp_path: Path) -> None:
    leaked = tmp_path / "leaked.md"
    private_name = "moex" + "-strategy-lab"
    leaked.write_text(f"data/projects/{private_name}/project.md\n", encoding="utf-8")
    neutral = tmp_path / "neutral.md"
    neutral.write_text("data/projects/example-project/project.md\n", encoding="utf-8")

    assert "private task/project history" in check_pre_push.scan_file(
        tmp_path, leaked.name, allow_local_artifacts=False
    )[0]
    assert check_pre_push.scan_file(tmp_path, neutral.name, allow_local_artifacts=False) == []


def test_only_local_heads_and_selected_remote_refs_are_allowed(tmp_path: Path) -> None:
    refs = "\n".join(
        [
            "refs/heads/main",
            "refs/remotes/origin/main",
            "refs/remotes/companion/task-708",
            "refs/tags/private-snapshot",
        ]
    )
    with mock.patch.object(check_pre_push, "run_git", return_value=refs):
        assert check_pre_push.unexpected_refs("origin", tmp_path) == [
            "refs/remotes/companion/task-708",
            "refs/tags/private-snapshot",
        ]
