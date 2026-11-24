"""Accepted source and a later file continuation through real review owners.

Provider content is controlled; no model or installation acceptance is claimed.
Git scopes, admissions, snapshots, round validation, CLI and cleanup are real.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_admission as review
import review_progress
import task_phases
import task_runner
import task_workspace
import write_admission


def put(task, name, value):
    path = task / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def admit(task, repositories, *, reviewer=False):
    record = review.admit_launch(task, workflow='standard', author_runner='codex',
        access_grant={'sandbox_mode': 'read-only' if reviewer else 'workspace-write',
                      'grants_write': bool(repositories) and not reviewer,
                      'granted_directories': [str(p) for p in repositories],
                      'target_repositories': [str(p) for p in repositories]},
        contract={'gate_status': 'gated'}, review_launch=reviewer,
        assurance={'schema_version': '1.0', 'strategy': 'isolated_same_provider',
                   'owner_provider': 'codex', 'review_provider': 'codex',
                   'providers': {'codex': {'executable': 'codex'}}},
        which=lambda _: '/test/provider')
    review.commit_admission(task, record)
    review.confirm_admission(task)
    return record


@pytest.fixture
def source_stage(tmp_path, monkeypatch):
    task = tmp_path / 'tasks/001-result'
    task.mkdir(parents=True)
    put(task, 'task.md', '---\nid: 1\nstatus: blocked\n---\n# Result\n')
    put(task, 'plan.md', '# Plan\n')
    put(task, 'task_contract.json', {'version': 1})
    put(task, 'status.json', {'state': 'blocked'})
    put(task, 'deliverables/manifest.json', {'deliverables': ['report.md']})
    put(task, 'deliverables/report.md', 'Initial source-stage report\n')
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    git(canonical, 'init', '-b', 'main')
    git(canonical, 'config', 'user.name', 'Fixture')
    git(canonical, 'config', 'user.email', 'fixture@example.invalid')
    put(canonical, 'source.py', 'size = 2\n')
    git(canonical, 'add', '.')
    git(canonical, 'commit', '-m', 'Initial')
    repository = tmp_path / 'source-001'
    git(canonical, 'worktree', 'add', '-b', 'task/001', str(repository))
    monkeypatch.setattr(review, 'utc_now', lambda: '2026-10-23T08:00:00+00:00')
    admit(task, [repository])
    task_phases.record_phase(task, 'implementation', entered_at='2026-10-23T08:00:00+00:00')
    monkeypatch.setattr(write_admission, 'utc_now', lambda: '2026-10-23T08:49:00+00:00')
    write_admission.open_write_scope(task, repository, 'source-run')
    put(repository, 'source.py', 'size = 1\n')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'Reviewed source')
    write_admission.close_write_scope(task, 'source-run', repository=repository)
    monkeypatch.setattr(review, 'utc_now', lambda: '2026-10-23T08:50:00+00:00')
    admission = admit(task, [repository], reviewer=True)
    candidate = review_progress.snapshot(task, [str(repository)], include_evidence=True)
    candidate['attempt'] = admission['admission_id']
    task_phases.record_phase(task, 'review', cause={'review_candidate': candidate},
                             entered_at='2026-10-23T08:50:00+00:00')
    monkeypatch.setattr(review, 'utc_now', lambda: '2026-10-23T08:58:00+00:00')
    put(task, 'findings.md', 'Verdict: approved\n')
    put(task, 'review-findings.json', {'schema_version': 1, 'round': 1,
                                      'decision': 'approved', 'findings': []})
    assert task_runner.record_standard_review_round(task, 'codex')['decision'] == 'approved'
    return task, repository, canonical


def continuation(task, monkeypatch, repositories=()):
    monkeypatch.setattr(review, 'utc_now', lambda: '2026-11-23T09:28:03+00:00')
    admit(task, repositories)
    task_phases.record_phase(task, 'rework', entered_at='2026-11-23T09:28:03+00:00')
    put(task, 'status.json', {'state': 'blocked'})


def cleanup(task, repository):
    result = task_workspace.cleanup_workspace(task, {'access_grant': {
        'granted_directories': [str(repository)]}})
    assert result['outcome'] == 'removed', result
    assert not repository.exists()


def cli(tmp_path, task):
    binary = tmp_path / 'bin/codex'
    binary.parent.mkdir(exist_ok=True)
    binary.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
from pathlib import Path
task = Path(os.environ['STAGE_TEST_TASK'])
assert 'exec' in sys.argv and 'resume' not in sys.argv
assert '--model' in sys.argv and 'gpt-6-astra' in sys.argv
assert sys.argv[sys.argv.index('--sandbox') + 1] == 'workspace-write'
assert '--add-dir' not in sys.argv  # repo-less read-only role writes own task only
(task / 'fresh-reviewer').write_text(str(os.getpid()))
(task / 'findings.md').write_text('Verdict: approved\\n')
(task / 'status.json').write_text(json.dumps({'state': 'completed'}))
''')
    binary.chmod(0o755)
    env = {**os.environ, 'PATH': str(binary.parent) + os.pathsep + os.environ['PATH'],
           'TASK_AGENT_ROOT': str(tmp_path), 'TASKS_INDEX_ROOT': str(tmp_path),
           'STAGE_TEST_TASK': str(task)}
    command = [sys.executable, str(Path(task_runner.__file__)), 'review', str(task),
               '--foreground', '--memory-limit', 'none', '--model', 'gpt-6-astra']
    return command, env


def test_accepted_source_cleanup_then_changed_file_gets_fresh_review(source_stage, tmp_path, monkeypatch):
    task, repository, canonical = source_stage
    # Controlled installation: copy the exact accepted source. This is not live
    # product acceptance, and no record purporting to be such acceptance is made.
    installed = tmp_path / 'controlled-install/source.py'
    installed.parent.mkdir()
    installed.write_bytes((repository / 'source.py').read_bytes())
    assert installed.read_text() == 'size = 1\n'
    cleanup(task, repository)
    prior = {p: (task / p).read_bytes() for p in ['reviews/admissions.jsonl', 'reviews/rounds.jsonl', 'phases.json']}
    continuation(task, monkeypatch)
    put(task, 'deliverables/report.md', 'New monthly file-stage comparison\n')
    before = {p: (task / p).read_bytes() for p in prior}
    command, env = cli(tmp_path, task)
    prepared = subprocess.run(command + ['--dry-run'], env=env, text=True, capture_output=True)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert review.author_target_repositories(task) == []
    assert review.standard_review_action(task)['action'] == 'review'
    assert review.standard_review_action(task)['changed_basis'] == ['files']
    assert before == {p: (task / p).read_bytes() for p in before}
    assert not (task / 'fresh-reviewer').exists()
    launched = subprocess.run(command, env=env, text=True, capture_output=True)
    assert (task / 'fresh-reviewer').is_file(), launched.stdout + launched.stderr
    assert len(review.review_rounds(task)) == 2
    assert (task / 'reviews/rounds.jsonl').read_bytes().startswith(prior['reviews/rounds.jsonl'])
    assert (task / 'reviews/admissions.jsonl').read_bytes().startswith(prior['reviews/admissions.jsonl'])
    assert review_progress.latest_snapshot(task)['repositories'] == {}
    phases = task_phases.read_phases(task)
    assert phases['history'][1]['cause']['review_candidate']['repositories'][str(repository)]['head'] == git(canonical, 'rev-parse', 'task/001')
    meta = json.loads((task / '.runner/runner.json').read_text())
    assert meta['review_admission']['assurance_strategy'] == 'isolated_same_provider'
    assert meta['access_grant']['grants_write'] is False
    assert meta['access_grant']['granted_directories'] == []
    # Review acceptance releases only the source review. Publication and
    # cleanup keep their historical ownership and cannot borrow that approval.
    workspaces, failures = task_workspace.task_git_workspaces(task, meta)
    assert workspaces == [(repository, False)]
    assert failures and failures[0]['path'] == str(repository)


def test_retirement_is_not_progress(source_stage, monkeypatch):
    task, repository, _ = source_stage
    cleanup(task, repository)
    continuation(task, monkeypatch)
    assert review.standard_review_action(task)['action'] == 'stop_no_progress'
    assert review.standard_review_action(task)['changed_basis'] == []


@pytest.mark.parametrize('gap', ['unapproved', 'undecided_review', 'open_scope', 'changed_scope',
                               'unbound_review', 'unrecorded_target', 'current_target', 'new_unreviewed_grant'])
def test_missing_unfinished_source_still_refuses(source_stage, monkeypatch, gap):
    task, repository, _ = source_stage
    if gap == 'open_scope':
        write_admission.open_write_scope(task, repository, 'unfinished')
    elif gap == 'changed_scope':
        write_admission.open_write_scope(task, repository, 'unfinished')
        put(repository, 'source.py', 'size = 3\n')
        git(repository, 'add', '.')
        git(repository, 'commit', '-m', 'Unreviewed change')
        write_admission.close_write_scope(task, 'unfinished', repository=repository)
    elif gap in {'unapproved', 'undecided_review'}:
        # Separate valid rework publication on a new real attempt, never rewrite
        # an accepted round to manufacture this case.
        admission = admit(task, [repository], reviewer=True)
        candidate = review_progress.snapshot(task, [str(repository)])
        candidate['attempt'] = admission['admission_id']
        task_phases.record_phase(task, 'review', cause={'review_candidate': candidate})
        if gap == 'unapproved':
            put(task, 'findings.md', 'Verdict: rework\n')
            put(task, 'review-findings.json', {'schema_version': 1, 'round': 2, 'decision': 'rework',
                                              'findings': [{'id': 'F1', 'summary': 'Source incomplete'}]})
            task_runner.record_standard_review_round(task, 'codex')
    elif gap == 'new_unreviewed_grant':
        continuation(task, monkeypatch, [repository])
    elif gap in {'unbound_review', 'unrecorded_target'}:
        # A historical snapshot without an admitted reviewer must prove nothing.
        admissions = task / 'reviews/admissions.jsonl'
        lines = admissions.read_text().splitlines()
        removed = 'admitted_review' if gap == 'unbound_review' else 'admitted'
        admissions.write_text('\n'.join(line for line in lines if json.loads(line).get('decision') != removed) + '\n')
    cleanup(task, repository)
    continuation(task, monkeypatch, [repository] if gap == 'current_target' else [])
    put(task, 'deliverables/report.md', 'Changed file cannot hide unfinished source\n')
    assert str(repository) in review.author_target_repositories(task)
    assert review.standard_review_action(task)['action'] == 'stop_invalid_candidate'


def test_dirty_current_repository_remains_in_review(source_stage, monkeypatch):
    task, repository, _ = source_stage
    continuation(task, monkeypatch, [repository])
    put(repository, 'source.py', 'size = 3\n')
    assert review.author_target_repositories(task) == [str(repository)]
    action = review.standard_review_action(task)
    assert action['action'] == 'review'
    assert 'repositories' in action['changed_basis']


def test_narrower_author_keeps_unreviewed_earlier_target(source_stage, tmp_path, monkeypatch):
    task, repository, canonical = source_stage
    pending = tmp_path / 'pending-001'
    git(canonical, 'worktree', 'add', '-b', 'task/pending', str(pending))
    continuation(task, monkeypatch, [pending])
    write_admission.open_write_scope(task, pending, 'pending-run')
    write_admission.close_write_scope(task, 'pending-run', repository=pending)
    continuation(task, monkeypatch)
    cleanup(task, repository)
    assert review.author_target_repositories(task) == [str(pending)]
    command, env = cli(tmp_path, task)
    prepared = subprocess.run(command + ['--dry-run'], env=env, text=True, capture_output=True)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    metadata = json.loads(prepared.stdout)
    assert metadata['access_grant']['granted_directories'] == [str(pending)]
    cleanup(task, pending)
    refused = subprocess.run(command + ['--dry-run'], env=env, text=True, capture_output=True)
    assert refused.returncode != 0
    assert review.author_target_repositories(task) == [str(pending)]
