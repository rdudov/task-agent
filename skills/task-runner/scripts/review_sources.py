"""Rebuildable source-selection index in the existing current admission record.

Ledgers and phase snapshots remain the evidence. Their writers advance this
compact projection; observers read it without writing. Only missing/replaced
inputs require replay. No history, round limit or acceptance lives here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

KEY = 'source_review_index'
LOGS = {'admissions': 'reviews/admissions.jsonl', 'rounds': 'reviews/rounds.jsonl',
        'scopes': '.runner/write-admission.jsonl'}


def fingerprint(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def position(path: Path) -> int:
    value = fingerprint(path)
    return value[2] if value else 0


def record_identity(entry: dict) -> str:
    """Identity of an original owner record, independent of its file position."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot_basis(task_dir: Path, repositories: list[str]) -> dict:
    index = read(task_dir)
    return {'version': 2, 'sources': {
        repository: {key: index['sources'].get(repository, {}).get(key)
                     for key in ('grant_identity', 'scope_identity')}
        for repository in repositories}}


def decision_basis(task_dir: Path) -> list[str]:
    index = read(task_dir)
    identities = {entry['record_identity'] for entry in index['rounds'].values()}
    if index.get('last_round_identity'):
        identities.add(index['last_round_identity'])
    return sorted(identities)


def _tail(path: Path, offset: int, size: int) -> tuple[list[tuple[int, dict]], int]:
    entries = []
    if not size:
        return entries, 0
    with path.open('rb') as handle:
        handle.seek(offset)
        while handle.tell() < size:
            line = handle.readline(size - handle.tell())
            if not line.endswith(b'\n'):
                break  # Retry the incomplete tail after its owner completes it.
            offset = handle.tell()
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(item, dict):
                entries.append((offset, item))
    return entries, offset


def _author(index: dict, end: int, entry: dict) -> None:
    if (entry.get('decision') != 'admitted'
            or entry.get('classification', {}).get('work_class') != 'material'):
        return
    index['author'] = entry
    for repository in entry.get('access_profile', {}).get('target_repositories', []):
        if isinstance(repository, str):
            source = index['sources'].setdefault(repository, {})
            source.setdefault('first_grant', end)
            source['grant'] = end
            source['grant_identity'] = record_identity(entry)
            source['grant_time'] = entry.get('evaluated_at')


def read(task_dir: Path, *, persist: bool = False, phases: dict | None = None,
         phase_origin: list[int] | None = None,
         log_origin: tuple[str, list[int] | None] | None = None,
         _current: dict | None = None) -> dict:
    # Local imports keep the append owners independent at import time.
    try:
        from . import review_admission as admission, task_phases, write_admission
    except ImportError:
        import review_admission as admission
        import task_phases
        import write_admission
    logs = LOGS
    current = admission.recorded_admission(task_dir) if _current is None else _current
    saved = current.get(KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    signatures = {name: fingerprint(task_dir / path) for name, path in logs.items()}
    phase_signature = fingerprint(task_dir / task_phases.PHASES_FILE)
    valid = (saved.get('version') == 2
             and all(isinstance(saved.get(k), dict) for k in ('sources', 'reviews', 'rounds', 'cursors'))
             and type(saved.get('phase_count')) is int)
    cursors = saved.get('cursors', {}) if valid else {}
    for name, signature in signatures.items():
        prior = cursors.get(name, {}).get('fingerprint')
        if signature != prior:
            # Size growth alone cannot distinguish append from a restored and
            # regrown history. Only the writer that observed this exact cursor
            # before its durable append may advance it without reconstruction.
            if (log_origin != (name, prior) or not signature
                    or prior and (signature[:2] != prior[:2] or signature[2] <= prior[2])):
                valid = False
    # A phase writer supplies its already-read append-only record. A changed
    # phase file without that writer is recovery, not permission to trust an
    # old projection (including an externally restored same-length history).
    if (phases is None and phase_signature != saved.get('phase_fingerprint')
            or phases is not None and phase_origin != saved.get('phase_fingerprint')):
        valid = False
    if valid and phases is not None and len(phases.get('history', [])) < saved.get('phase_count', 0):
        valid = False
    index = saved if valid else {'version': 2, 'sources': {}, 'reviews': {}, 'rounds': {},
                                 'cursors': {}, 'phase_count': 0}
    deltas = {}
    for name, path in logs.items():
        signature = signatures[name]
        prior = index['cursors'].get(name, {})
        if signature == prior.get('fingerprint'):
            deltas[name] = []
            continue
        entries, offset = _tail(task_dir / path, prior.get('offset', 0), signature[2] if signature else 0)
        deltas[name] = entries
        index['cursors'][name] = {'fingerprint': signature, 'offset': offset}
    withdrawn = {e.get('annuls') for _, e in deltas['admissions']
                 if e.get('kind') == admission.ANNULLED_ADMISSION}
    if withdrawn and valid:
        # Revocation is rare and may restore an arbitrarily old binding. Replay
        # it once through the same reducer instead of retaining an author stack.
        current.pop(KEY, None)
        return read(task_dir, persist=persist, phases=phases, phase_origin=phase_origin,
                    log_origin=log_origin, _current=current)
    commitment = admission.admission_commitment(task_dir)
    pending_id = commitment.get('admission_id') if commitment else None
    pending = index.pop('pending', None)
    if pending:
        deltas['admissions'].insert(0, pending)
    for end, entry in deltas['admissions']:
        identity = entry.get('admission_id')
        if identity in withdrawn or entry.get('kind') == admission.ANNULLED_ADMISSION:
            continue
        if identity and identity == pending_id:
            index['pending'] = [end, entry]
            continue
        _author(index, end, entry)
        if entry.get('decision') == 'admitted_review':
            index['reviews'][identity] = {**entry, 'ledger_end': end}
            index['review_head'] = identity
    for end, entry in deltas['scopes']:
        repository = entry.get('before', {}).get('repository')
        run = entry.get('run_id')
        if not isinstance(repository, str) or not isinstance(run, str):
            continue
        kind = entry.get('record')
        if kind not in {write_admission.OPENED, write_admission.CLOSED}:
            continue
        source = index['sources'].setdefault(repository, {})
        scopes = source.setdefault('unclosed', {})
        source['scope_end'] = end
        source['scope_identity'] = record_identity(entry)
        if (kind == write_admission.OPENED or not entry.get('closed_at')
                or entry.get('resolution') in write_admission.SYNTHETIC_RESOLUTIONS):
            scopes[run] = True
        else:
            scopes.pop(run, None)
        if kind == write_admission.CLOSED:
            source['after'] = entry.get('after')
            source['closed_at'] = entry.get('closed_at')
            source['latest_close_time'] = max(source.get('latest_close_time', ''),
                                              str(entry.get('closed_at') or ''))
    for end, entry in deltas['rounds']:
        identity = record_identity(entry)
        index['last_round_identity'] = identity
        attempt = entry.get('review_attempt')
        if attempt:
            index['rounds'][attempt] = {**entry, 'ledger_end': end, 'record_identity': identity}
    if phase_signature != index.get('phase_fingerprint'):
        record = phases if phases is not None else task_phases.read_phases(task_dir)
        history = record.get('history', [])
        for phase_number, entry in enumerate(history[index['phase_count']:], index['phase_count']):
            candidate = entry.get('cause', {}).get('review_candidate', {})
            for repository_number, (repository, state) in enumerate(candidate.get('repositories', {}).items()):
                source = index['sources'].setdefault(repository, {})
                source.setdefault('first_snapshot', [phase_number, repository_number])
                source['candidate'] = {'attempt': candidate.get('attempt'), 'state': state,
                                       'basis': candidate.get('source_basis'),
                                       'entered_at': entry.get('entered_at')}
        index['phase_count'] = len(history)
        index['phase_fingerprint'] = phase_signature
    attempts = {s['candidate'].get('attempt') for s in index['sources'].values() if 'candidate' in s}
    attempts.add(index.get('review_head'))
    index['reviews'] = {k: v for k, v in index['reviews'].items() if k in attempts}
    index['rounds'] = {k: v for k, v in index['rounds'].items() if k in attempts}
    # A legacy single-record binding is still handled by the admission owner.
    if not index.get('author') and not signatures['admissions']:
        _author(index, 0, {k: v for k, v in current.items() if k != KEY})
    if persist:
        admission._write_json(task_dir / admission.ADMISSION_RECORD, {**current, KEY: index})
    return index
