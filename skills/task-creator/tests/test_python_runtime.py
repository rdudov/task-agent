from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "skills" / "task-creator" / "scripts" / "python_runtime.sh"


def resolve_python(env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; task_agent_python', "bash", str(RUNTIME)],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def test_repository_virtualenv_is_the_default_runtime() -> None:
    result = resolve_python({"TASK_AGENT_PYTHON": ""})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(ROOT / ".venv" / "bin" / "python")


def test_explicit_runtime_is_validated_and_precedes_the_virtualenv(tmp_path: Path) -> None:
    explicit = tmp_path / "python"
    explicit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    explicit.chmod(0o755)
    result = resolve_python({"TASK_AGENT_PYTHON": str(explicit)})
    assert result.returncode == 0
    assert result.stdout.strip() == str(explicit)

    missing = resolve_python({"TASK_AGENT_PYTHON": str(tmp_path / "missing")})
    assert missing.returncode != 0
    assert "not executable" in missing.stderr
