from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from task_contract import (
    COMPLETION_REVIEW_QUESTION,
    delivered_candidate,
    historical_completion_review_materials,
    parse_task_markdown_contract,
    render_task_contract_overlay,
    unsatisfied_review_verdict,
    verification_gate_result,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], text=True
    ).strip()


def test_completion_review_does_not_predeclare_future_live_evidence() -> None:
    assert "Judge the two prose policy families only" in COMPLETION_REVIEW_QUESTION
    assert "do not require a future terminal delivery receipt" in COMPLETION_REVIEW_QUESTION


def test_parse_task_markdown_contract_extracts_hard_constraints_and_evidence(tmp_path: Path) -> None:
    task_md = tmp_path / "task.md"
    task_md.write_text(
        """# Example

## Hard Constraints
- Use Silero only
- Do not keep flite fallback

## Required Verification

### A. Direct TTS e2e with ASR back-transcription
- call real /v1/synthesis
- compare ASR text against source text

### B. Remote assistant e2e with ASR back-transcription
- send a real voice message through the configured transport

## Review Gates
- Reject if any live synthesis path still uses flite

## Acceptance Criteria
- [ ] direct TTS e2e verifies generated voice through ASR back-transcription
- [ ] real remote assistant e2e verifies generated voice through ASR back-transcription
""",
        encoding="utf-8",
    )

    contract = parse_task_markdown_contract(task_md)

    assert "Use Silero only" in contract["non_negotiable_constraints"]
    assert "Do not keep flite fallback" in contract["non_negotiable_constraints"]
    assert any(item["id"] == "a_direct_tts_e2e_with_asr_back_transcription" for item in contract["required_live_evidence"])
    assert any(item["id"] == "b_remote_assistant_e2e_with_asr_back_transcription" for item in contract["required_live_evidence"])
    assert "Reject if any live synthesis path still uses flite" in contract["review_gates"]
    assert len(contract["acceptance_criteria"]) == 2


def test_render_task_contract_overlay_includes_completion_rule() -> None:
    overlay = render_task_contract_overlay(
        {
            "non_negotiable_constraints": ["Use Silero only"],
            "forbidden_substitutions": ["flite"],
            "required_live_evidence": [
                {"id": "direct_roundtrip", "description": "Real TTS -> ASR", "required": True}
            ],
            "acceptance_criteria": ["direct round-trip passes"],
            "review_gates": ["Reject if flite is used"],
            "completion_policy": {},
        }
    )

    assert "=== TASK EXECUTION CONTRACT ===" in overlay
    assert "Use Silero only" in overlay
    assert "flite" in overlay
    assert "direct_roundtrip" in overlay
    assert "Do not mark the task approved or completed" in overlay


def test_verification_uses_exact_gate_heading_and_latest_result() -> None:
    verification = """# Verification

## live_probe_extra
- Result: **OK**

## live_probe
- Result: **FAIL**

## live_probe repaired
- Result: **PASSED**
"""
    assert verification_gate_result(verification, "live_probe") == "PASSED"
    assert verification_gate_result(verification, "missing") is None


def test_required_review_verdict_must_be_authors_own_unambiguous_line(tmp_path: Path) -> None:
    contract = {
        "review_verdict": {"path": "findings.md", "allowed": ["approved", "rework"]},
        "completion_policy": {"require_review_verdict": True},
    }
    (tmp_path / "findings.md").write_text(
        "Verdict: approved\n\nVerdict: rework\n", encoding="utf-8"
    )
    assert unsatisfied_review_verdict(contract, tmp_path)
    (tmp_path / "findings.md").write_text("Verdict: approved\n", encoding="utf-8")
    assert unsatisfied_review_verdict(contract, tmp_path) == []


def test_historical_review_materials_revalidate_the_recorded_git_object(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    source = repository / "source.py"
    source.write_text("accepted = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "accepted"], check=True)
    accepted_head = _git(repository, "rev-parse", "HEAD")
    accepted = delivered_candidate(repository, accepted_head)
    accepted_digest = "sha256:" + hashlib.sha256(
        b"accepted = True\n"
    ).hexdigest()

    source.write_text("successor = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "successor"], check=True)
    subject = {"delivered_candidate": accepted, "runtime_dependencies": []}
    context = {
        "artifacts": [
            {"path": str(tmp_path / "subject.json"), "digest": "sha256:subject"},
            {"path": str(source), "digest": accepted_digest},
        ]
    }

    assert historical_completion_review_materials(subject, context) == [source]

    context["artifacts"][1]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="material digest differs"):
        historical_completion_review_materials(subject, context)


def test_historical_review_materials_match_accept_time_deletion_filter(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    kept = repository / "kept.py"
    removed = repository / "removed.py"
    kept.write_text("version = 1\n", encoding="utf-8")
    removed.write_text("remove = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "base"], check=True)
    kept.write_text("version = 2\n", encoding="utf-8")
    removed.unlink()
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "accepted"], check=True)
    head = _git(repository, "rev-parse", "HEAD")
    digest = "sha256:" + hashlib.sha256(b"version = 2\n").hexdigest()
    subject = {
        "delivered_candidate": delivered_candidate(repository, head),
        "runtime_dependencies": [],
    }
    context = {
        "artifacts": [
            {"path": str(tmp_path / "subject.json"), "digest": "sha256:subject"},
            {"path": str(kept), "digest": digest},
        ]
    }

    assert historical_completion_review_materials(subject, context) == [kept]
