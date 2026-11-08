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


def test_legacy_inflight_completion_is_read_only_news_and_deduplicates_after_retention(tmp_path):
    path = task(tmp_path)
    meta = launch(path)
    baseline = task_engine.facts(path, baseline=True)
    meta.update(finished_at='later', outcome='completed')
    raw = json.dumps(meta).encode()
    (path / '.runner/runner.json').write_bytes(raw)  # old watcher, no archive hook
    page = task_engine.facts(path, baseline['cursors'])
    assert [row['id'] for row in page['events']] == ['first']
    assert page['events'][0]['source'] == str(path / '.runner/runner.json')
    assert (path / '.runner/runner.json').read_bytes() == raw
    assert not (path / run_facts.TERMINALS).exists()
    assert not task_engine.facts(path, page['cursors'])['events']
    run_facts.retain_terminal(path, meta)  # successor's normal preservation
    launch(path, 'next')
    caught_up = task_engine.facts(path, page['cursors'])
    assert not caught_up['events']
    assert caught_up['cursors']['direct_pending'] == []
    finish(path)
    next_page = task_engine.facts(path, caught_up['cursors'])
    assert [row['id'] for row in next_page['events']] == ['next']
    assert not task_engine.facts(path, next_page['cursors'])['events']


@pytest.mark.parametrize('observe_before_replacement', [True, False])
def test_lost_watcher_and_child_are_honest_news_without_fabricating_review_round(tmp_path, observe_before_replacement):
    path = task(tmp_path)
    processes = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) for _ in range(2)]
    try:
        meta = launch(path, pid=processes[0].pid, watcher_pid=processes[1].pid,
                      process_identity=task_runner.process_identity(processes[0].pid),
                      watcher_process_identity=task_runner.process_identity(processes[1].pid),
                      pid_namespace=task_runner.pid_namespace_identity(), review_kind='technical',
                      review_admission={'access_profile': {'role': 'reviewer', 'grants_write': False}})
        baseline = task_engine.facts(path, baseline=True)
        assert not task_engine.facts(path, baseline['cursors'])['events']
        (path / 'findings.md').write_text('Independent findings\nVerdict: rework\n')
        processes[0].kill(); processes[0].wait()
        assert not task_engine.facts(path, baseline['cursors'])['events']  # watcher still owns terminal
        processes[1].kill(); processes[1].wait()
        raw = (path / '.runner/runner.json').read_bytes()
        if observe_before_replacement:
            page = task_engine.facts(path, baseline['cursors'])
            assert [row['fact']['outcome'] for row in page['events']] == ['supervision_lost']
            assert (path / '.runner/runner.json').read_bytes() == raw
            assert not task_engine.facts(path, page['cursors'])['events']
        run_facts.retain_terminal(path, meta)
        launch(path, 'successor')
        retained = task_engine.facts(path, baseline['cursors'])['events'][0]
        assert retained['fact']['finished_at'] is None and retained['fact']['exit_code'] is None
        assert retained['fact']['published_verdict'] == 'rework'
        assert retained['fact']['verdict_source'] == str(path / 'findings.md')
        assert not (path / review_admission.ROUNDS_LEDGER).exists()
        if observe_before_replacement:
            assert not task_engine.facts(path, page['cursors'])['events']
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_unknown_namespace_or_missing_identity_is_not_proven_dead(tmp_path, monkeypatch):
    path = task(tmp_path)
    meta = launch(path, pid=99999999, watcher_pid=99999998,
                  process_identity='old', watcher_process_identity='old', pid_namespace='foreign')
    # Explicit foreign visibility refusal; never reconcile unseen host PIDs as dead.
    monkeypatch.setattr(task_runner, 'runner_pid_namespace_visible', lambda _: False)
    assert run_facts.current_fact(path, meta) is None
    monkeypatch.setattr(task_runner, 'runner_pid_namespace_visible', lambda _: True)
    meta.pop('watcher_process_identity')
    assert run_facts.current_fact(path, meta) is None


def test_incomplete_archive_does_not_hide_old_watchers_new_current_end(tmp_path):
    path = task(tmp_path)
    meta = launch(path)
    baseline = task_engine.facts(path, baseline=True)
    (path / run_facts.TERMINALS).write_bytes(b'{"unfinished":')
    meta.update(finished_at='later', outcome='completed')
    (path / '.runner/runner.json').write_text(json.dumps(meta))
    page = task_engine.facts(path, baseline['cursors'])
    assert [row['id'] for row in page['events']] == ['first']
    assert page['more']
    assert not task_engine.facts(path, page['cursors'])['events']
