from __future__ import annotations

from pathlib import Path

from task_contract import parse_task_markdown_contract, render_task_contract_overlay


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
