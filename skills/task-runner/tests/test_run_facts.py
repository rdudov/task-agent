"""Primary lifecycle functions; synthetic records are not native acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import review_admission
import run_facts
import task_engine
import task_runner


def task(tmp_path):
    path = tmp_path / "101-work"
    (path / ".runner").mkdir(parents=True)
    return path


def launch(path, name="first", **extra):
    meta = {"run_id": name, "started_at": "2030-01-01T00:00:00Z", "runner": "codex",
            "workflow": "standard", "task_dir": str(path), "sandbox_mode": "read-only",
            "child_started_at": "2030-01-01T00:00:01Z", **extra}
    task_runner.write_json(path / ".runner/runner.json", meta)
    return meta


def finish(path, **extra):
    return task_runner.finish_runner_meta(path, {"finished_at": "2030-01-01T00:00:05Z",
                                               "outcome": "completed", **extra})


@pytest.mark.parametrize("outcome", ["completed", "failed_to_launch", "stopped",
                                    "rejected_completion_contract", "recovered_failed"])
def test_any_terminal_survives_successor_before_observation(tmp_path, outcome):
    path = task(tmp_path)
    baseline = task_engine.facts(path, baseline=True)
    launch(path, child_started_at=None if outcome == "failed_to_launch" else "started")
    finish(path, outcome=outcome)
    finish(path, outcome=outcome)  # repeated terminal publication is not news
    launch(path, "successor")
    page = task_engine.facts(path, baseline["cursors"])
    assert len(page["events"]) == 1
    event = page["events"][0]
    assert event["fact"]["outcome"] == outcome
    assert event["id"] == "first"
    assert "terminal-runs.jsonl#bytes=" in event["source"]
    assert not task_engine.facts(path, page["cursors"])["events"]


def test_initial_boundary_suppresses_legacy_terminal_on_first_replacement(tmp_path):
    path = task(tmp_path)
    meta = launch(path)
    meta.update(finished_at="yesterday", outcome="completed")
    # Exact pre-upgrade shape: no retention hook was installed when it finished.
    (path / ".runner/runner.json").write_text(json.dumps(meta))
    baseline = task_engine.facts(path, baseline=True)
    run_facts.retain_terminal(path, meta)  # real start's pre-replacement owner
    launch(path, "next")
    assert not task_engine.facts(path, baseline["cursors"])["events"]
    finish(path)
    assert task_engine.facts(path, baseline["cursors"])["events"][0]["id"] == "next"


def review(path, event, decision):
    return review_admission.record_review_round(
        path, event_id=event, decision={"decision": decision,
                                      "findings": ["repair"] if decision == "rework" else []},
        review_provider="codex")


def test_reviews_and_refusal_use_actual_owners_under_unchanged_task_label(tmp_path):
    path = task(tmp_path)
    baseline = task_engine.facts(path, baseline=True)
    review(path, "review-1", "rework")
    review(path, "review-2", "approved")
    review_admission.record_infrastructure_obligation(
        path, event_id="review-3", source="dev-pipeline:review_refused",
        reason="Bound reviewer is unavailable", allocate=False)
    page = task_engine.facts(path, baseline["cursors"])
    assert {row["id"] for row in page["events"]} == {"review-1", "review-2", "review-3"}
    assert page["events"][-1]["stream"] == "refusals"
    review(path, "review-2", "approved")
    assert not task_engine.facts(path, page["cursors"])["events"]


def test_same_size_replacement_and_copied_prefix_recover_by_content(tmp_path):
    path = task(tmp_path)
    review(path, "round-A", "approved")
    cursor = task_engine.facts(path, baseline=True)["cursors"]
    ledger = path / review_admission.ROUNDS_LEDGER
    raw = ledger.read_bytes()
    stamp = ledger.stat()
    ledger.rename(ledger.with_suffix(".old"))
    ledger.write_bytes(raw.replace(b"round-A", b"round-B"))
    os.utime(ledger, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    page = task_engine.facts(path, cursor)
    assert [item["id"] for item in page["events"]] == ["round-B"]
    # Inode replacement with copied history must not replay the old prefix.
    ledger.rename(ledger.with_suffix(".copy"))
    ledger.write_bytes(raw.replace(b"round-A", b"round-B"))
    review(path, "round-C", "approved")
    assert [item["id"] for item in task_engine.facts(path, page["cursors"])["events"]] == ["round-C"]


def test_bounded_pages_partial_append_and_lost_ledger_refusal(tmp_path):
    path = task(tmp_path)
    for n in range(80):
        launch(path, str(n))
        finish(path)
    page = task_engine.facts(path)
    assert len(page["events"]) == run_facts.PAGE_SIZE and page["more"]
    second = task_engine.facts(path, page["cursors"])
    assert second["events"][0]["id"] == "32"
    baseline = task_engine.facts(path, baseline=True)
    ledger = path / run_facts.TERMINALS
    with ledger.open("ab") as handle:
        handle.write(b'{"unfinished":')
    pending = task_engine.facts(path, baseline["cursors"])
    assert pending["more"] and not pending["events"]
    launch(path, "recovered")
    finish(path)
    recovered = task_engine.facts(path, baseline["cursors"])
    assert recovered["events"][0]["id"] == "recovered"
    ledger.unlink()
    with pytest.raises(ValueError, match="disappeared"):
        task_engine.facts(path, recovered["cursors"])


def test_real_public_cli_prints_retained_primary_fact(tmp_path):
    path = task(tmp_path)
    launch(path)
    finish(path)
    command = [sys.executable, str(Path(task_engine.__file__)), "facts", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["events"][0]["id"] == "first"
