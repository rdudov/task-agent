"""Real filesystem/runner regressions for standard review convergence."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import review_admission as review
import review_progress
import task_phases
import task_runner


def put(task, path, value):
    p = task / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value) if not isinstance(value, str) else value)


def setup_task(tmp_path, waiting=False):
    task = tmp_path / "tasks" / "001-result"
    task.mkdir(parents=True)
    put(task, "task.md", '---\nid: 1\nstatus: blocked\nstatus_detail: '
        + ('missing_user_input' if waiting else 'review_pending') + '\n---\n# Result\n')
    put(task, "plan.md", "# Plan\n")
    put(task, "status.json", {"state": "blocked", "blocker": "missing_user_input" if waiting else None})
    put(task, "task_contract.json", {"version": 1})
    put(task, "task_lifecycle.json", {"run_generation": 1})
    put(task, "deliverables/manifest.json", {"deliverables": ["result.png"]})
    put(task, "deliverables/result.png", "image bytes")
    put(task, "completion-answer.txt", "Please choose the margins.")
    # Binding is real; no provider is called to validate filesystem decisions.
    record = review.admit_launch(task, workflow="standard", author_runner="claude",
        access_grant={"sandbox_mode": "workspace-write", "grants_write": False,
                      "granted_directories": [], "target_repositories": []},
        contract={"gate_status": "gated"}, which=lambda _: "/test/provider")
    review.commit_admission(task, record)
    review.confirm_admission(task)
    return task


def begin_review(task, attempt="attempt-1"):
    candidate = review_progress.snapshot(task, [], include_evidence=True)
    candidate["attempt"] = attempt
    task_phases.record_phase(task, "review", cause={"review_candidate": candidate})
    put(task, ".runner/review-admission.json", {"admission_id": attempt, "classification": {"work_class": "review"}})
    return candidate


def publication(task, findings=None):
    value = {"schema_version": 1, "round": len(review.review_rounds(task)) + 1,
             "decision": "rework", "findings": findings if findings is not None else [{"id": "F1", "summary": "Missing proof"}]}
    put(task, "findings.md", "# Findings\nVerdict: rework\n")
    put(task, "review-findings.json", value)
    return value


@pytest.mark.parametrize("payload", [None, [], {"schema_version": 2}, {"round": 0}, {"decision": "approved"}, {"findings": []},
    {"findings": ["not object"]}, {"findings": [{"id": "", "summary": "problem"}]},
    {"findings": [{"id": str(i), "summary": "problem"} for i in range(21)]}])
def test_invalid_rework_never_appends_and_never_returns_author(tmp_path, payload):
    task = setup_task(tmp_path, waiting=True)
    begin_review(task)
    value = publication(task)
    if payload is None:
        (task / "review-findings.json").unlink()
    else:
        put(task, "review-findings.json", {**value, **payload} if isinstance(payload, dict) else payload)
    entry = task_runner.record_standard_review_round(task, "codex")
    assert entry is None
    assert review.review_rounds(task) == []
    action = review.standard_review_action(task)
    assert action["action"] == "stop_review_infrastructure"
    assert action["waiting_for_user"] is True
    assert len(review.infrastructure_obligations(task)) == 1
    task_runner.record_standard_review_round(task, "codex")
    assert len(review.infrastructure_obligations(task)) == 1


def test_wait_survives_reviewer_status_and_process_restart(tmp_path):
    task = setup_task(tmp_path, waiting=True)
    begin_review(task)
    publication(task)
    task_runner.record_standard_review_round(task, "codex")
    put(task, "status.json", {"state": "completed", "current_step": "Reviewer done"})
    assert review.standard_review_action(task)["action"] == "wait_user"
    assert len(review.review_rounds(task)) == 1
    # The answer is read again entirely from durable files, no cached controller.
    import subprocess
    code = 'import json,sys;from pathlib import Path;import review_admission;print(json.dumps(review_admission.standard_review_action(Path(sys.argv[1]))))'
    output = subprocess.check_output([sys.executable, "-c", code, str(task)],
        env={**os.environ, "PYTHONPATH": str(Path(review.__file__).parent)}, text=True)
    assert json.loads(output)["action"] == "wait_user"


def test_same_candidate_no_prose_or_timestamp_basis_and_new_user_continues(tmp_path):
    task = setup_task(tmp_path)
    begin_review(task)
    publication(task)
    task_runner.record_standard_review_round(task, "codex")
    assert review.standard_review_action(task)["action"] == "rework"
    task_phases.record_phase(task, "rework")
    for path in ("findings.md", "verification.md", "trace.md", "completion-answer.txt"):
        put(task, path, "New prose: please review again, yesterday's issue is fixed.")
    os.utime(task / "deliverables/result.png", None)
    put(task, "deliverables/manifest.json", '{ "deliverables" : ["result.png"] }')
    assert review.standard_review_action(task)["action"] == "stop_no_progress"
    put(task, "task_lifecycle.json", {"run_generation": 2})
    assert review.standard_review_action(task)["action"] == "review"
    assert task.name == "001-result"


@pytest.mark.parametrize("path", ["requirements.md", "task_contract.json", "deliverables/result.png"])
def test_observable_changes_allow_review(tmp_path, path):
    task = setup_task(tmp_path)
    begin_review(task)
    publication(task)
    task_runner.record_standard_review_round(task, "codex")
    task_phases.record_phase(task, "rework")
    put(task, path, '{"changed":true}')
    assert review.standard_review_action(task)["action"] == "review"


def test_evidence_bytes_must_change_and_be_tied_to_prior_finding(tmp_path):
    task = setup_task(tmp_path)
    put(task, "live-evidence/proof.json", "old")
    begin_review(task)
    publication(task, [{"id": "F1", "summary": "Need proof", "evidence": ["live-evidence/proof.json"]}])
    task_runner.record_standard_review_round(task, "codex")
    task_phases.record_phase(task, "rework")
    put(task, "live-evidence/unrelated.json", "new unrelated proof")
    assert review.standard_review_action(task)["action"] == "stop_no_progress"
    os.utime(task / "live-evidence/proof.json", None)
    assert review.standard_review_action(task)["action"] == "stop_no_progress"
    put(task, "live-evidence/proof.json", "new actual proof")
    action = review.standard_review_action(task)
    assert action["action"] == "review"
    assert "finding:F1:live-evidence/proof.json" in action["changed_basis"]


def test_real_useful_rework_has_no_round_budget(tmp_path):
    task = setup_task(tmp_path)
    for index in range(25):
        begin_review(task, f"review-{index}")
        publication(task)
        assert task_runner.record_standard_review_round(task, "codex")["round"] == index + 1
        assert review.standard_review_action(task)["action"] == "rework"
        task_phases.record_phase(task, "rework")
        put(task, "deliverables/result.png", f"useful revision {index}")
        assert review.standard_review_action(task)["action"] == "review"


@pytest.mark.parametrize("unsafe", ["missing", "symlink", "directory", "malformed"])
def test_invalid_candidate_is_never_equal(tmp_path, unsafe):
    task = setup_task(tmp_path)
    begin_review(task)
    if unsafe == "missing":
        (task / "deliverables/result.png").unlink()
    elif unsafe == "symlink":
        (task / "deliverables/result.png").unlink()
        (task / "deliverables/result.png").symlink_to(task / "task.md")
    elif unsafe == "directory":
        (task / "deliverables/result.png").unlink()
        (task / "deliverables/result.png").mkdir()
    else:
        put(task, "deliverables/manifest.json", "[")
    assert review.standard_review_action(task)["action"] == "stop_invalid_candidate"


def test_generic_dev_pipeline_decision_keeps_its_own_finding_shape(tmp_path):
    task = setup_task(tmp_path)
    decision = {"decision": "rework_required", "findings": [{"id": f"F{i}", "severity": "critical"} for i in range(25)]}
    assert len(review.record_review_round(task, event_id="pipeline", decision=decision)["finding_ids"]) == 25


def test_new_user_input_after_protocol_failure_can_obtain_new_review(tmp_path):
    task = setup_task(tmp_path, waiting=True)
    begin_review(task)
    publication(task, [])
    assert task_runner.record_standard_review_round(task, "codex") is None
    task_phases.record_phase(task, "rework")
    put(task, "task_lifecycle.json", {"run_generation": 2})
    assert review.standard_review_action(task)["action"] == "review"


def test_approval_of_available_work_does_not_complete_a_user_wait(tmp_path):
    import task_completion
    task = setup_task(tmp_path, waiting=True)
    begin_review(task)
    put(task, "findings.md", "# Available work\nVerdict: approved\n")
    put(task, "review-findings.json", {"schema_version": 1, "round": 1, "decision": "approved", "findings": []})
    assert task_runner.record_standard_review_round(task, "codex")["decision"] == "approved"
    assert review.standard_review_action(task)["action"] == "wait_user"
    ready, reason = task_completion.completion_ready(task, defer_task_status=True)
    assert ready is False
    assert reason.gate == "missing_user_input"
    assert task_completion.independent_review_blocker(task) is None  # approved available part


def test_blocked_without_missing_user_input_is_not_a_user_wait(tmp_path):
    task = setup_task(tmp_path)
    begin_review(task)
    publication(task)
    task_runner.record_standard_review_round(task, "codex")
    assert review.standard_review_action(task)["waiting_for_user"] is False
    assert review.standard_review_action(task)["action"] == "rework"


def test_added_and_unregistered_files_change_only_registered_identity(tmp_path):
    task = setup_task(tmp_path)
    before = begin_review(task)
    put(task, "deliverables/extra.txt", "extra")
    assert review_progress.snapshot(task, [])["files"] == before["files"]
    put(task, "deliverables/manifest.json", {"deliverables": ["extra.txt", "result.png"]})
    assert review_progress.snapshot(task, [])["files"] != before["files"]
    put(task, "deliverables/manifest.json", {"deliverables": []})
    assert review_progress.snapshot(task, [])["files"] == {}


def test_code_candidate_uses_existing_git_identity_with_empty_manifest(tmp_path):
    import subprocess
    task = setup_task(tmp_path)
    repository = tmp_path / "repo"
    repository.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.STDOUT)
    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "source.py").write_text("version = 1\n")
    git("add", "source.py")
    git("commit", "-m", "Fixture")
    put(task, "deliverables/manifest.json", {"deliverables": []})
    before = review_progress.snapshot(task, [str(repository)])
    (repository / "source.py").write_text("version = 2\n")
    after = review_progress.snapshot(task, [str(repository)])
    assert "repositories" in review_progress.changes(task, before, after, {})


def test_wait_protocol_failure_never_persists_an_author_handoff(tmp_path):
    import task_completion
    task = setup_task(tmp_path, waiting=True)
    begin_review(task)
    publication(task, [])
    task_runner.record_standard_review_round(task, "codex")
    blocker = task_completion.independent_review_blocker(task)
    assert "phase_transition" not in blocker
    assert blocker["next_action"]["action"] == "stop_review_infrastructure"


def test_invalid_later_attempt_cannot_borrow_earlier_approval(tmp_path):
    import task_completion
    task = setup_task(tmp_path)
    begin_review(task)
    put(task, "findings.md", "Verdict: approved\n")
    assert task_runner.record_standard_review_round(task, "codex")["decision"] == "approved"
    task_phases.record_phase(task, "rework")
    put(task, "deliverables/result.png", "new candidate")
    begin_review(task, "attempt-2")
    publication(task, [])
    assert task_runner.record_standard_review_round(task, "codex") is None
    assert review.independent_review_status(task)["satisfied"] is False
    assert review.standard_review_action(task)["action"] == "stop_review_infrastructure"
    assert "phase_transition" not in task_completion.independent_review_blocker(task)


def test_author_can_name_changed_proof_for_prior_id_without_reviewer_preselection(tmp_path):
    task = setup_task(tmp_path)
    put(task, "live-evidence/proof.json", "old unchanged proof")
    begin_review(task)
    publication(task)  # reviewer did not preselect a proof path
    task_runner.record_standard_review_round(task, "codex")
    task_phases.record_phase(task, "rework")
    publication(task, [{"id": "F1", "summary": "Closed by proof", "evidence": ["live-evidence/proof.json"]}])
    assert review.standard_review_action(task)["action"] == "stop_no_progress"  # mapping alone
    put(task, "live-evidence/proof.json", "new proof actually measured")
    assert review.standard_review_action(task)["action"] == "review"


def test_new_proof_must_name_a_real_previous_finding(tmp_path):
    task = setup_task(tmp_path)
    begin_review(task)
    publication(task)
    task_runner.record_standard_review_round(task, "codex")
    task_phases.record_phase(task, "rework")
    put(task, "live-evidence/new.json", "newly measured proof")
    publication(task, [{"id": "invented", "summary": "Proof", "evidence": ["live-evidence/new.json"]}])
    assert review.standard_review_action(task)["action"] == "stop_no_progress"
    publication(task, [{"id": "F1", "summary": "Proof", "evidence": ["live-evidence/new.json"]}])
    assert review.standard_review_action(task)["action"] == "review"


def test_nonwaiting_approved_review_retains_normal_completion(tmp_path):
    import task_completion
    task = setup_task(tmp_path)
    begin_review(task)
    put(task, "findings.md", "Verdict: approved\n")
    assert task_runner.record_standard_review_round(task, "codex")["decision"] == "approved"
    assert review.standard_review_action(task)["action"] == "none"
    ready, reason = task_completion.completion_ready(task, defer_task_status=True)
    assert ready is True, reason


def test_candidate_comparison_does_not_inherit_checker_cli_delivery_caps(tmp_path):
    from deliverables import check_deliverables
    task = setup_task(tmp_path)
    names = [f"part-{i}.txt" for i in range(21)]
    for name in names:
        put(task, "deliverables/" + name, "useful result")
    put(task, "deliverables/manifest.json", {"deliverables": names})
    assert check_deliverables(task)["ok"] is False  # CLI policy stays unchanged
    assert len(review_progress.snapshot(task, [])["files"]) == 21
