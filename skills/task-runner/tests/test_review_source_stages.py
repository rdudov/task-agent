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


def same_second_continuation(task, monkeypatch, repositories=()):
    # The approval and this strictly later owner operation have identical UTC
    # strings. Ordering must come from the captured ledger positions.
    stamp = review.review_rounds(task)[0]['recorded_at']
    monkeypatch.setattr(review, 'utc_now', lambda: stamp)
    admit(task, repositories)
    task_phases.record_phase(task, 'rework', entered_at=stamp)
    put(task, 'deliverables/report.md', 'Changed same-second continuation\n')


def test_equal_second_accepted_stage_gets_canonical_review(source_stage, tmp_path, monkeypatch):
    task, repository, _ = source_stage
    history = (task / 'reviews/rounds.jsonl').read_bytes()
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    command, env = cli(tmp_path, task)
    before = {p: p.read_bytes() for p in task.rglob('*') if p.is_file()}
    result = subprocess.run(command + ['--dry-run'], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert review.author_target_repositories(task) == []
    assert review.standard_review_action(task)['action'] == 'review'
    assert before == {p: p.read_bytes() for p in before}
    assert (task / 'reviews/rounds.jsonl').read_bytes() == history


@pytest.mark.parametrize('later', ['grant', 'open_scope', 'closed_unchanged_scope', 'closed_changed_scope'])
def test_equal_second_later_source_is_never_retired(source_stage, monkeypatch, later):
    task, repository, _ = source_stage
    stamp = review.review_rounds(task)[0]['recorded_at']
    monkeypatch.setattr(review, 'utc_now', lambda: stamp)
    monkeypatch.setattr(write_admission, 'utc_now', lambda: stamp)
    if later == 'grant':
        admit(task, [repository])
    else:
        write_admission.open_write_scope(task, repository, 'later-run')
        if later == 'closed_changed_scope':
            put(repository, 'source.py', 'size = 9\n')
            git(repository, 'add', '.')
            git(repository, 'commit', '-m', 'Unreviewed later source')
        if later != 'open_scope':
            write_admission.close_write_scope(task, 'later-run', repository=repository)
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    assert review.author_target_repositories(task) == [str(repository)]
    assert review.standard_review_action(task)['action'] == 'stop_invalid_candidate'


@pytest.mark.parametrize('after_snapshot', ['none', 'closed_scope', 'grant'])
def test_same_second_source_before_snapshot_is_accepted(source_stage, monkeypatch, after_snapshot):
    task, repository, _ = source_stage
    # A later source scope is closed before the next snapshot at the exact same
    # clock value. The new bound snapshot must be the acceptance basis.
    stamp = review.review_rounds(task)[0]['recorded_at']
    monkeypatch.setattr(review, 'utc_now', lambda: stamp)
    monkeypatch.setattr(write_admission, 'utc_now', lambda: stamp)
    admit(task, [repository])
    write_admission.open_write_scope(task, repository, 'second-source')
    write_admission.close_write_scope(task, 'second-source', repository=repository)
    admitted = admit(task, [repository], reviewer=True)
    candidate = review_progress.snapshot(task, [str(repository)])
    candidate['attempt'] = admitted['admission_id']
    task_phases.record_phase(task, 'review', cause={'review_candidate': candidate}, entered_at=stamp)
    put(task, 'findings.md', 'Verdict: approved\n')
    put(task, 'review-findings.json', {'schema_version': 1, 'round': 2, 'decision': 'approved', 'findings': []})
    task_runner.record_standard_review_round(task, 'codex')
    if after_snapshot == 'closed_scope':
        write_admission.open_write_scope(task, repository, 'after-snapshot')
        write_admission.close_write_scope(task, 'after-snapshot', repository=repository)
    elif after_snapshot == 'grant':
        admit(task, [repository])
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    expected = [] if after_snapshot == 'none' else [str(repository)]
    assert review.author_target_repositories(task) == expected


def test_index_survives_process_restart_and_rebuild(source_stage, monkeypatch):
    import review_sources
    task, repository, _ = source_stage
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    command = [sys.executable, '-c',
               'import review_admission as r,sys,json; from pathlib import Path; '
               'print(json.dumps(r.author_target_repositories(Path(sys.argv[1]))))', str(task)]
    env = {**os.environ, 'PYTHONPATH': str(Path(review.__file__).parent)}
    assert subprocess.check_output(command, env=env, text=True).strip() == '[]'
    record = task / review.ADMISSION_RECORD
    value = json.loads(record.read_text())
    value.pop(review_sources.KEY)
    record.write_text(json.dumps(value))
    # A read-only reconstruction changes no bytes and uses the same decisions.
    old = record.read_bytes()
    assert review.author_target_repositories(task) == []
    assert record.read_bytes() == old
    review.update_source_index(task)
    assert review_sources.KEY in json.loads(record.read_text())
    assert subprocess.check_output(command, env=env, text=True).strip() == '[]'
    # Replacement invalidates the saved cursor, even if lengths stay equal.
    ledger = task / review.ROUNDS_LEDGER
    restored = ledger.with_suffix('.restored')
    restored.write_bytes(ledger.read_bytes())
    restored.replace(ledger)
    assert review.author_target_repositories(task) == []
    review.update_source_index(task)
    assert review.author_target_repositories(task) == []


def test_index_replays_interrupted_scope_write(source_stage, monkeypatch):
    task, repository, _ = source_stage
    same_second_continuation(task, monkeypatch)
    original = review.update_source_index
    monkeypatch.setattr(review, 'update_source_index', lambda *a, **k: None)
    write_admission.open_write_scope(task, repository, 'interrupted')
    monkeypatch.setattr(review, 'update_source_index', original)
    assert review.author_target_repositories(task) == [str(repository)]
    review.update_source_index(task)
    assert review.author_target_repositories(task) == [str(repository)]


@pytest.mark.parametrize('count', [0, 200, 20000])
def test_selection_work_does_not_grow_with_irrelevant_history(source_stage, monkeypatch, count):
    import review_sources
    task, repository, _ = source_stage
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    # Grow the owners' append-only histories. A single recovery at a real write
    # rebuilds the index; subsequent selection must not touch any ledger/history.
    for name, path in review_sources.LOGS.items():
        record = {'decision': 'exempt'} if name == 'admissions' else {'record': 'irrelevant'}
        with (task / path).open('a') as handle:
            handle.write((json.dumps(record) + '\n') * count)
    phases = task_phases.read_phases(task)
    phases['history'].extend([{'phase': 'blocked', 'entered_at': 'unused'}] * count)
    task_phases._write_json(task_phases.phases_path(task), phases)
    review.update_source_index(task)
    def forbidden(*a, **k):
        raise AssertionError('Warm selection read accumulated history')
    monkeypatch.setattr(review_sources, '_tail', forbidden)
    monkeypatch.setattr(task_phases, 'read_phases', forbidden)
    for _ in range(3):
        assert review.author_target_repositories(task) == []
        assert review.author_target_repositories(task, include_reviewed=True) == [str(repository)]
    # A real appended unresolved grant is immediately visible. Only its delta
    # needs folding; prior history is not an admissible shortcut to acceptance.
    monkeypatch.undo()
    admit(task, [repository])
    assert review.author_target_repositories(task) == [str(repository)]



def test_phase_restoration_before_next_writer_invalidates_old_index(source_stage, monkeypatch):
    task, repository, _ = source_stage
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    assert review.author_target_repositories(task) == []
    restored = task_phases.read_phases(task)
    for entry in restored['history']:
        entry.get('cause', {}).pop('review_candidate', None)
    restored['review_candidate_index'] = None
    task_phases._write_json(task_phases.phases_path(task), restored)
    task_phases.record_phase(task, 'blocked')
    assert review.author_target_repositories(task) == [str(repository)]


def test_truncated_scope_ledger_cannot_reuse_old_source_acceptance(source_stage, monkeypatch):
    task, repository, _ = source_stage
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    assert review.author_target_repositories(task) == []
    # A restore retaining only the open record has an unresolved obligation.
    ledger = write_admission.ledger_path(task)
    ledger.write_bytes(ledger.read_bytes().splitlines(keepends=True)[0])
    assert review.author_target_repositories(task) == [str(repository)]
    review.update_source_index(task)
    assert review.author_target_repositories(task) == [str(repository)]


def test_incomplete_scope_tail_is_retried(source_stage, monkeypatch):
    task, repository, _ = source_stage
    cleanup(task, repository)
    same_second_continuation(task, monkeypatch)
    ledger = write_admission.ledger_path(task)
    line = json.dumps({'record': write_admission.OPENED, 'run_id': 'pending-tail',
                       'before': {'repository': str(repository)}}).encode()
    with ledger.open('ab') as handle:
        handle.write(line[:len(line)//2])
    review.update_source_index(task)
    with ledger.open('ab') as handle:
        handle.write(line[len(line)//2:] + b'\n')
    assert review.author_target_repositories(task) == [str(repository)]


def test_pending_and_annulled_author_do_not_release_prior_source(source_stage, monkeypatch):
    task, repository, _ = source_stage
    first = review.bound_author_admission(task)
    record = review.admit_launch(task, workflow='standard', author_runner='codex',
        access_grant={'sandbox_mode': 'workspace-write', 'grants_write': False,
                      'target_repositories': [], 'granted_directories': []},
        contract={'gate_status': 'gated'}, which=lambda _: '/test/provider',
        assurance={'schema_version': '1.0', 'strategy': 'isolated_same_provider',
                   'owner_provider': 'codex', 'review_provider': 'codex',
                   'providers': {'codex': {'executable': 'codex'}}})
    receipt = review.commit_admission(task, record)
    assert review.author_target_repositories(task) == [str(repository)]
    review.annul_admission(task, receipt, reason='No child started')
    assert review.bound_author_admission(task)['admission_id'] == first['admission_id']
    assert review.author_target_repositories(task) == [str(repository)]



def test_missing_close_evidence_stays_unresolved_after_later_valid_close(source_stage, monkeypatch):
    task, repository, _ = source_stage
    state = write_admission.git_write_state(repository)
    write_admission._append(task, {'record': write_admission.CLOSED, 'run_id': 'incomplete-close',
                                  'before': state, 'after': state})
    write_admission.open_write_scope(task, repository, 'valid-close')
    write_admission.close_write_scope(task, 'valid-close', repository=repository)
    admission = admit(task, [repository], reviewer=True)
    candidate = review_progress.snapshot(task, [str(repository)])
    candidate['attempt'] = admission['admission_id']
    task_phases.record_phase(task, 'review', cause={'review_candidate': candidate})
    put(task, 'findings.md', 'Verdict: approved\n')
    put(task, 'review-findings.json', {'schema_version': 1, 'round': 2, 'decision': 'approved', 'findings': []})
    task_runner.record_standard_review_round(task, 'codex')
    cleanup(task, repository)
    continuation(task, monkeypatch)
    assert review.author_target_repositories(task) == [str(repository)]



def test_legacy_missing_grant_time_cannot_release_source(source_stage, monkeypatch):
    task, repository, _ = source_stage
    phases = task_phases.read_phases(task)
    phases['history'][1]['cause']['review_candidate'].pop('source_basis')
    task_phases._write_json(task_phases.phases_path(task), phases)
    ledger = task / review.ADMISSIONS_LEDGER
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    entries[0].pop('evaluated_at')
    ledger.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
    cleanup(task, repository)
    continuation(task, monkeypatch)
    assert review.author_target_repositories(task) == [str(repository)]
