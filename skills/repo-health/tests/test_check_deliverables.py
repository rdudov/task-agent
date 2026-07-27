import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load_checker_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "check_deliverables.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("check_deliverables_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


checker = _load_checker_module()


class DeliverablesCheckTests(unittest.TestCase):
    def _task(self, tmp: str, registered=None, files=None) -> Path:
        task_dir = Path(tmp) / "001-example"
        deliverables = task_dir / "deliverables"
        deliverables.mkdir(parents=True)
        for name, content in (files or {}).items():
            (deliverables / name).write_text(content, encoding="utf-8")
        if registered is not None:
            (deliverables / "manifest.json").write_text(
                json.dumps({"deliverables": registered}), encoding="utf-8"
            )
        return task_dir

    def test_task_without_deliverables_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-example"
            task_dir.mkdir()
            verdict = checker.check_deliverables(task_dir)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["registered"], [])

    def test_files_without_a_manifest_are_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=None, files={"report.md": "content"})
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("no manifest.json", verdict["errors"][0])

    def test_valid_registration_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp, registered=["a.md", "b.md"], files={"a.md": "x", "b.md": "yy"}
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertTrue(verdict["ok"], verdict["errors"])
        self.assertEqual(verdict["registered"], ["a.md", "b.md"])
        self.assertEqual(verdict["total_bytes"], 3)

    def test_registration_order_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp, registered=["z.md", "a.md"], files={"a.md": "x", "z.md": "y"}
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertEqual(verdict["registered"], ["z.md", "a.md"])

    def test_missing_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["gone.md"], files={})
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])

    def test_empty_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["e.md"], files={"e.md": ""})
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("empty", verdict["errors"][0])

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["../task.md"], files={})
            (task_dir / "task.md").write_text("secret", encoding="utf-8")
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("bare basename", verdict["errors"][0])

    def test_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["link.md"], files={"real.md": "x"})
            (task_dir / "deliverables" / "link.md").symlink_to(
                task_dir / "deliverables" / "real.md"
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("symlink", verdict["errors"][0])

    def test_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["nested"], files={})
            (task_dir / "deliverables" / "nested").mkdir()
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("regular file", verdict["errors"][0])

    def test_duplicate_registration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["a.md", "a.md"], files={"a.md": "x"})
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])
        self.assertIn("Registered twice", verdict["errors"][0])

    def test_file_count_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {f"f{i}.md": "x" for i in range(4)}
            task_dir = self._task(tmp, registered=sorted(files), files=files)
            verdict = checker.check_deliverables(task_dir, max_files=3)
        self.assertFalse(verdict["ok"])

    def test_byte_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=["big.md"], files={"big.md": "x" * 100})
            verdict = checker.check_deliverables(task_dir, max_bytes=10)
        self.assertFalse(verdict["ok"])

    def test_malformed_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=[], files={})
            (task_dir / "deliverables" / "manifest.json").write_text("{oops", encoding="utf-8")
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])

    def test_non_array_deliverables_field_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, registered=[], files={})
            (task_dir / "deliverables" / "manifest.json").write_text(
                json.dumps({"deliverables": "report.md"}), encoding="utf-8"
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertFalse(verdict["ok"])

    def test_unregistered_file_is_a_warning_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp, registered=["a.md"], files={"a.md": "x", "stray.md": "y"}
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertTrue(verdict["ok"])
        self.assertIn("stray.md", verdict["warnings"][0])

    def test_registered_internal_record_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp, registered=["findings.md"], files={"findings.md": "notes"}
            )
            verdict = checker.check_deliverables(task_dir)
        self.assertTrue(verdict["ok"])
        self.assertTrue(any("internal task record" in w for w in verdict["warnings"]))


if __name__ == "__main__":
    unittest.main()
