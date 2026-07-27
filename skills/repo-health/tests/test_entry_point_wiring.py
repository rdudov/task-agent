"""Negative tests for the Claude Code wiring check.

The check exists to protect one invariant: a rule has exactly one canonical
copy and every entry point reaches it by reference. Wiring that merely
*resolves* does not prove that — a copied rule file resolves perfectly and then
drifts.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def _load_health_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "check_repo_health.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("check_repo_health_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


health = _load_health_module()


class EntryPointWiringTests(unittest.TestCase):
    def _tree(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".cursor" / "rules").mkdir(parents=True)
        (root / ".cursor" / "rules" / "example.mdc").write_text("canonical", encoding="utf-8")
        (root / "AGENTS.md").write_text("project rules", encoding="utf-8")
        (root / "skills").mkdir()
        (root / ".claude" / "imports").mkdir(parents=True)
        (root / "CLAUDE.md").write_text(
            "@AGENTS.md\n@.claude/imports/example.md\n", encoding="utf-8"
        )
        return root

    def test_correct_wiring_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").symlink_to("../skills")
            self.assertEqual(health.check_agent_entry_points(root), [])

    def test_copied_rule_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").write_text(
                "a copy that will drift", encoding="utf-8"
            )
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("not a copy of the rule" in e for e in errors), errors)

    def test_symlink_to_the_wrong_canonical_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to(root / "AGENTS.md")
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("instead of .cursor/rules/example.mdc" in e for e in errors), errors)

    def test_dangling_import_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to("../../nowhere.mdc")
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(errors)

    def test_missing_import_line_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("no import line" in e for e in errors), errors)

    def test_non_md_import_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "CLAUDE.md").write_text(
                "@AGENTS.md\n@.cursor/rules/example.mdc\n", encoding="utf-8"
            )
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("ignored unless it ends in .md" in e for e in errors), errors)

    def test_skills_directory_instead_of_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").mkdir()
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("not a separate directory" in e for e in errors), errors)

    def test_skills_symlink_to_the_wrong_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "elsewhere").mkdir()
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").symlink_to("../elsewhere")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("instead of skills/" in e for e in errors), errors)

    def test_dangling_skills_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            (root / ".claude" / "skills").symlink_to("../nonexistent")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("dangling symlink" in e for e in errors), errors)

    def test_missing_skills_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "imports" / "example.md").symlink_to(
                "../../.cursor/rules/example.mdc"
            )
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("missing symlink to skills/" in e for e in errors), errors)

    def test_missing_import_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / ".claude" / "skills").symlink_to("../skills")
            errors = health.check_agent_entry_points(root)
        self.assertTrue(any("missing symlink" in e for e in errors), errors)

    def test_repository_without_claude_md_is_not_forced_into_the_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            self.assertEqual(health.check_agent_entry_points(root), [])


if __name__ == "__main__":
    unittest.main()
