from __future__ import annotations

from pathlib import Path

from codex_multi_agent import (
    DEFAULT_CODEX_MODEL,
    ROLE_MODELS,
    aggregate_required_evidence_status,
    build_role_prompt,
    ensure_agents_dir,
    model_attempts,
    REQUIRED_AGENT_PROMPTS,
    run_codex,
    review_counts_as_approved,
    validate_review_against_contract,
)


def test_validate_review_against_contract_rejects_approved_with_blocked_required_evidence() -> None:
    task_contract = {
        "non_negotiable_constraints": ["Use Silero only"],
        "required_live_evidence": [
            {"id": "direct_roundtrip", "description": "Real direct round-trip", "required": True}
        ],
        "completion_policy": {
            "require_all_required_live_evidence_passed": True,
            "require_forbidden_substitutions_absent": True,
            "require_mandatory_constraints_reported": True,
        },
    }
    review_json = {
        "review_decision": "approved",
        "mandatory_constraints_satisfied": True,
        "forbidden_substitutions_detected": [],
        "required_live_evidence": [
            {"id": "direct_roundtrip", "status": "blocked", "details": "runtime unavailable"}
        ],
        "approval_blockers": [],
    }

    errors = validate_review_against_contract(review_json, task_contract)

    assert errors
    assert review_counts_as_approved(review_json, task_contract) is False


def test_aggregate_required_evidence_status_tracks_passed_across_tasks(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "multi-agent"
    logs_dir = artifacts_dir / "logs"
    tasks_dir = artifacts_dir / "tasks"
    logs_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)

    task_file = tasks_dir / "task_2_3.md"
    task_file.write_text("# task", encoding="utf-8")
    (logs_dir / "task_2_3.code_review.iter1.out.md").write_text(
        """```json
{
  "review_decision": "approved",
  "mandatory_constraints_satisfied": true,
  "forbidden_substitutions_detected": [],
  "required_live_evidence": [
    {
      "id": "direct_roundtrip",
      "status": "passed",
      "details": "ok"
    }
  ],
  "approval_blockers": []
}
```""",
        encoding="utf-8",
    )

    statuses = aggregate_required_evidence_status(
        [task_file],
        artifacts_dir,
        {
            "required_live_evidence": [
                {"id": "direct_roundtrip", "description": "Real direct round-trip", "required": True},
                {"id": "remote_roundtrip", "description": "Real remote round-trip", "required": True},
            ]
        },
    )

    assert statuses["direct_roundtrip"] == "passed"
    assert statuses["remote_roundtrip"] == "missing"


def test_role_prompt_includes_semantic_fit_and_push_completion_rules(tmp_path: Path, monkeypatch) -> None:
    import codex_multi_agent

    root = tmp_path / "repo"
    task_dir = root / "tasks" / "001-example"
    artifacts_dir = task_dir / "multi-agent"
    task_dir.mkdir(parents=True)
    artifacts_dir.mkdir()
    task_file = task_dir / "task.md"
    task_file.write_text("# Example\n", encoding="utf-8")
    monkeypatch.setattr(codex_multi_agent, "repo_root", lambda: root)

    prompt = build_role_prompt(
        "Role prompt",
        role_name="developer",
        task_dir=task_dir,
        artifacts_dir=artifacts_dir,
        task_file=task_file,
        extra_inputs=[],
        runner="agent",
    )

    assert "CURSOR AGENT CLI" in prompt
    assert "Pipeline runner: agent" in prompt
    assert "Preserve the semantic target of the request" in prompt
    assert "substituting a nearby effect" in prompt
    assert "committed and pushed after verification" in prompt
    assert "record the reason and current repository state" in prompt


def test_ensure_agents_dir_clones_missing_prompt_repo(tmp_path: Path, monkeypatch) -> None:
    import codex_multi_agent

    agents_dir = tmp_path / "agents"
    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        agents_dir.mkdir(parents=True)
        for prompt in REQUIRED_AGENT_PROMPTS:
            (agents_dir / prompt).write_text("prompt", encoding="utf-8")

        class Result:
            returncode = 0
            stdout = "cloned"
            stderr = ""

        return Result()

    monkeypatch.setattr(codex_multi_agent.subprocess, "run", fake_run)

    result = ensure_agents_dir(agents_dir, "https://example.test/agents.git")

    assert result == agents_dir.resolve()
    assert calls == [["git", "clone", "https://example.test/agents.git", str(agents_dir.resolve())]]


def test_ensure_agents_dir_reports_clone_failure(tmp_path: Path, monkeypatch) -> None:
    import codex_multi_agent
    import pytest

    def fake_run(command, capture_output, text, check):
        class Result:
            returncode = 128
            stdout = ""
            stderr = "repository not found"

        return Result()

    monkeypatch.setattr(codex_multi_agent.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        ensure_agents_dir(tmp_path / "missing-agents", "https://example.test/missing.git")

    assert "repository not found" in str(exc.value)


def test_ensure_agents_dir_rejects_incomplete_existing_prompt_repo(tmp_path: Path) -> None:
    import pytest

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / REQUIRED_AGENT_PROMPTS[0]).write_text("prompt", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        ensure_agents_dir(agents_dir, "https://example.test/agents.git")

    assert "Agents prompt directory is incomplete" in str(exc.value)
    assert REQUIRED_AGENT_PROMPTS[1] in str(exc.value)


def test_role_models_use_current_codex_default() -> None:
    assert DEFAULT_CODEX_MODEL == "gpt-5.5"
    assert ROLE_MODELS
    assert set(ROLE_MODELS.values()) == {DEFAULT_CODEX_MODEL}
    assert "gpt-5.3-codex" not in ROLE_MODELS.values()


def test_model_attempts_deduplicates_current_fallbacks() -> None:
    assert model_attempts("gpt-5.3-codex")[:2] == ["gpt-5.3-codex", DEFAULT_CODEX_MODEL]
    assert model_attempts(DEFAULT_CODEX_MODEL)[0] == DEFAULT_CODEX_MODEL
    assert model_attempts(DEFAULT_CODEX_MODEL).count(DEFAULT_CODEX_MODEL) == 1


def test_run_codex_retries_unsupported_model_with_current_default(monkeypatch, tmp_path: Path) -> None:
    import codex_multi_agent

    calls = []

    def fake_run(command, capture_output, text, check, env, cwd):
        calls.append(command)

        class Result:
            stdout = ""

        result = Result()
        if "gpt-5.3-codex" in command:
            result.returncode = 1
            result.stderr = (
                '{"type":"error","status":400,"error":{"message":'
                '"The gpt-5.3-codex model is not supported when using Codex with a ChatGPT account."}}'
            )
        else:
            result.returncode = 0
            result.stderr = ""
            result.stdout = "ok"
        return result

    monkeypatch.setattr(codex_multi_agent.subprocess, "run", fake_run)

    output = run_codex("prompt", "gpt-5.3-codex", "danger-full-access", tmp_path)

    assert output.startswith("Model fallback applied")
    assert output.endswith("ok")
    assert calls[0][calls[0].index("--model") + 1] == "gpt-5.3-codex"
    assert calls[1][calls[1].index("--model") + 1] == DEFAULT_CODEX_MODEL
