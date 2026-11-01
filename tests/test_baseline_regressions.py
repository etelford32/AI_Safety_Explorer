"""File-backed and adversarial contracts for baseline storage (no hosted replay)."""
import hashlib
import json
import sqlite3

import pytest

from safety_explorer import baselines, db, intake, intake_store, sessions


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / 'regression.db')
    yield c
    c.close()


def seed(c):
    for i, role in enumerate(('system', 'user', 'assistant', 'tool')):
        sessions.append_turn(c, 'source', role, f'{role}: café 宇宙\nline {i}', turn_index=i,
                             source='import:messages', tier='C', language='fr',
                             meta={'model': 'declared-model', 'message_ids': ['upstream-1']})
    return 'source'


def rows(c, table):
    return [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY 1')]


def test_migration_preserves_legacy_sessions_and_summaries_on_repeated_reopen(tmp_path):
    path = tmp_path / 'legacy.db'
    c = db.connect(path)
    sessions.ensure(c)  # pre-feature tables, with no baseline tables
    sid = seed(c)
    c.execute("INSERT INTO session_summary VALUES (?,4,'legacy',0,'quiet','{}',0,'old')", (sid,))
    c.commit()
    before = {t: rows(c, t) for t in ('live_session', 'live_turn', 'session_summary')}
    c.close()
    for _ in range(3):
        c = db.init_db(path)
        assert {t: rows(c, t) for t in before} == before
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert not list(c.execute('PRAGMA foreign_key_check'))
        assert c.execute("SELECT name FROM sqlite_master WHERE name='idx_conversation_fork_baseline'").fetchone()
        baseline = baselines.snapshot(c, sid)
        f = baselines.fork(c, baseline['id'], -1, 'model', {'model': 'candidate'})
        assert len(rows(c, 'conversation_baseline')) == len(rows(c, 'conversation_fork')) == 1
        assert baselines.load(c, f['baseline_id'])['session_id'] == sid
        c.close()


def test_exact_payload_hash_attribution_and_source_preservation(conn):
    sid = seed(conn)
    before = {t: rows(conn, t) for t in ('live_session', 'live_turn')}
    a = baselines.snapshot(conn, sid)
    payload = baselines.load(conn, a['id'])
    assert payload['source'] == 'import:messages'
    assert payload['tier'] == 'C' and payload['language'] == 'fr'
    assert payload['meta'] == {'model': 'declared-model', 'message_ids': ['upstream-1']}
    expected = [dict(id=r[0], index=r[2], role=r[3], text=r[4], at=r[5])
                for r in conn.execute('SELECT * FROM live_turn ORDER BY turn_index')]
    assert payload['turns'] == expected
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    assert a == dict(id='base_' + digest, hash=digest, through_index=3, n_turns=4)
    assert {t: rows(conn, t) for t in before} == before
    payload['meta']['model'] = 'mutated return'
    assert baselines.load(conn, a['id'])['meta']['model'] == 'declared-model'


def test_imported_model_and_stored_message_identity_survive_snapshot(conn):
    batch = intake.Batch('messages', 'fixture.json')
    conv = intake.Conversation(key='upstream-conversation', model='model-from-export')
    conv.add('user', 'Question', '2025-01-01T00:00:00Z')
    conv.add('assistant', 'Answer', '2025-01-01T00:00:01Z')
    sid = intake_store.store_conversation(conn, batch, conv)['session_id']
    conn.commit()
    a = baselines.snapshot(conn, sid)
    p = baselines.load(conn, a['id'])
    assert p['meta']['model'] == 'model-from-export'
    assert p['meta']['key'] == 'upstream-conversation'
    assert [t['id'] for t in p['turns']] == [r[0] for r in conn.execute(
        'SELECT id FROM live_turn WHERE session_id=? ORDER BY turn_index', (sid,))]
    assert [t['at'] for t in p['turns']] == ['2025-01-01T00:00:00+00:00', '2025-01-01T00:00:01+00:00']


@pytest.mark.parametrize('index', [-1, 4, True, False, 1.0, '1'])
def test_invalid_snapshot_prefix_is_atomic(conn, index):
    sid = seed(conn)
    with pytest.raises(ValueError):
        baselines.snapshot(conn, sid, index)
    assert not rows(conn, 'conversation_baseline')
    assert not conn.in_transaction


@pytest.mark.parametrize('damage', ['gap', 'duplicate'])
def test_noncontiguous_turns_are_rejected_without_writes(conn, damage):
    sid = seed(conn)
    if damage == 'gap':
        conn.execute('DELETE FROM live_turn WHERE turn_index=1')
    else:
        # Full schema protects this; the older bare-session schema does not.
        conn.execute('DROP INDEX idx_live_turn_pos')
        conn.execute('UPDATE live_turn SET turn_index=0 WHERE turn_index=1')
    conn.commit()
    with pytest.raises(ValueError, match='noncontiguous'):
        baselines.snapshot(conn, sid)
    assert not rows(conn, 'conversation_baseline')


def test_prefix_stays_identical_after_growth_and_metadata_is_frozen(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid, 1)
    original = rows(conn, 'conversation_baseline')
    sessions.append_turn(conn, sid, 'user', 'More', turn_index=4)
    assert baselines.snapshot(conn, sid, 1) == a
    assert rows(conn, 'conversation_baseline') == original
    conn.execute("UPDATE live_session SET meta='{}', tier='B' WHERE id=?", (sid,))
    conn.commit()
    assert baselines.load(conn, a['id'])['tier'] == 'C'
    assert baselines.snapshot(conn, sid, 1)['id'] != a['id']


@pytest.mark.parametrize('operation', ['snapshot', 'fork'])
def test_caller_rollback_includes_baseline_operations(conn, operation):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    before = rows(conn, 'conversation_baseline')
    conn.execute("UPDATE live_session SET label='pending' WHERE id=?", (sid,))
    if operation == 'snapshot':
        baselines.snapshot(conn, sid, 0)
    else:
        baselines.fork(conn, a['id'], 0, 'sampling', {'temperature': 0})
    assert conn.in_transaction, 'baseline API must not commit caller work'
    conn.rollback()
    assert conn.execute('SELECT label FROM live_session').fetchone()[0] != 'pending'
    assert rows(conn, 'conversation_baseline') == before
    assert not rows(conn, 'conversation_fork')


def test_failed_operation_preserves_outer_transaction(conn):
    sid = seed(conn)
    conn.execute("UPDATE live_session SET label='pending' WHERE id=?", (sid,))
    with pytest.raises(ValueError):
        baselines.snapshot(conn, sid, 99)
    assert conn.in_transaction
    assert conn.execute('SELECT label FROM live_session').fetchone()[0] == 'pending'
    conn.rollback()


def test_insert_failure_rolls_back_only_operation(conn):
    sid = seed(conn)
    conn.execute("CREATE TRIGGER reject_baseline AFTER INSERT ON conversation_baseline BEGIN SELECT RAISE(ABORT, 'injected'); END")
    conn.execute("UPDATE live_session SET label='pending' WHERE id=?", (sid,))
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        baselines.snapshot(conn, sid)
    assert conn.in_transaction
    assert conn.execute('SELECT label FROM live_session').fetchone()[0] == 'pending'
    assert not rows(conn, 'conversation_baseline')
    conn.rollback()


@pytest.mark.parametrize('column,value', [('snapshot', '{}'), ('content_hash', '0'*64),
                                         ('session_id', 'other'), ('through_index', 0),
                                         ('source_tier', 'A')])
def test_load_and_fork_reject_corrupt_payload_or_metadata(conn, column, value):
    sid = seed(conn)
    seed_other = sessions.open_session(conn)
    a = baselines.snapshot(conn, sid)
    if column == 'session_id':
        value = seed_other
    conn.execute(f'UPDATE conversation_baseline SET {column}=? WHERE id=?', (value, a['id']))
    conn.commit()
    with pytest.raises(ValueError, match='integrity'):
        baselines.load(conn, a['id'])
    with pytest.raises(ValueError, match='integrity'):
        baselines.fork(conn, a['id'], 0, 'context', {'x': 1})
    assert not rows(conn, 'conversation_fork')


def test_rehashed_tampering_cannot_reuse_old_content_address(conn):
    a = baselines.snapshot(conn, seed(conn))
    payload = baselines.load(conn, a['id'])
    payload['turns'][0]['text'] = 'tampered'
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    conn.execute('UPDATE conversation_baseline SET snapshot=?, content_hash=? WHERE id=?',
                 (encoded, hashlib.sha256(encoded.encode()).hexdigest(), a['id']))
    conn.commit()
    with pytest.raises(ValueError, match='integrity'):
        baselines.load(conn, a['id'])


@pytest.mark.parametrize('position', [-2, 4, True, 1.0, '0'])
def test_invalid_fork_positions_write_nothing(conn, position):
    a = baselines.snapshot(conn, seed(conn))
    with pytest.raises(ValueError, match='point'):
        baselines.fork(conn, a['id'], position, 'context', {'x': 1})
    assert not rows(conn, 'conversation_fork')


@pytest.mark.parametrize('spec', [{}, [], None, 'text', {'x': float('nan')}, {'x': float('inf')}])
def test_invalid_specification_write_nothing(conn, spec):
    a = baselines.snapshot(conn, seed(conn))
    with pytest.raises(ValueError):
        baselines.fork(conn, a['id'], 0, 'context', spec)
    assert not rows(conn, 'conversation_fork')


@pytest.mark.parametrize('factor', sorted(baselines.FACTORS))
@pytest.mark.parametrize('position', [-1, 0, 3])
def test_fork_boundaries_canonical_dedup_and_source_preservation(conn, factor, position):
    a = baselines.snapshot(conn, seed(conn))
    before = {t: rows(conn, t) for t in ('live_session', 'live_turn', 'conversation_baseline')}
    one = baselines.fork(conn, a['id'], position, factor, {'b': 2, 'a': 1})
    assert one == baselines.fork(conn, a['id'], position, factor, {'a': 1, 'b': 2})
    persisted = conn.execute('SELECT * FROM conversation_fork').fetchone()
    assert persisted['baseline_id'] == a['id']
    assert persisted['fork_after_index'] == position and persisted['factor'] == factor
    assert json.loads(persisted['specification']) == {'a': 1, 'b': 2}
    assert {t: rows(conn, t) for t in before} == before
    assert baselines.fork(conn, a['id'], position, factor, {'a': 2})['id'] != one['id']
    assert len(rows(conn, 'conversation_fork')) == 2


def test_unknown_baseline_and_factor_are_rejected(conn):
    with pytest.raises(ValueError, match='unknown baseline'):
        baselines.fork(conn, 'missing', -1, 'context', {'x': 1})
    a = baselines.snapshot(conn, seed(conn))
    with pytest.raises(ValueError, match='factor'):
        baselines.fork(conn, a['id'], -1, 'multiple', {'x': 1})
    assert not rows(conn, 'conversation_fork')


def test_foreign_keys_restrict_deletion_and_orphans(conn):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    baselines.fork(conn, a['id'], -1, 'context', {'x': 1})
    for sql, args in [('DELETE FROM live_session WHERE id=?', (sid,)),
                      ('DELETE FROM conversation_baseline WHERE id=?', (a['id'],)),
                      ('UPDATE conversation_fork SET baseline_id=?', ('missing',)),
                      ('UPDATE conversation_baseline SET session_id=?', ('missing',))]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)
        conn.rollback()
    assert len(rows(conn, 'live_turn')) == 4
    assert not list(conn.execute('PRAGMA foreign_key_check'))


def test_concurrent_writer_cannot_split_metadata_and_turns(conn, tmp_path):
    sid = seed(conn)
    other = db.connect(tmp_path / 'regression.db')
    other.execute('PRAGMA busy_timeout=0')
    attempts = []
    def between_reads(sql):
        if sql.startswith('SELECT turn_index'):
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute("UPDATE live_session SET source='new-source' WHERE id=?", (sid,))
                other.execute("UPDATE live_turn SET text='new-text' WHERE turn_index=0")
                other.commit()
                attempts.append('committed')
            except sqlite3.OperationalError as e:
                attempts.append(str(e))
                other.rollback()
    conn.set_trace_callback(between_reads)
    try:
        a = baselines.snapshot(conn, sid)
    finally:
        conn.set_trace_callback(None)
        other.close()
    assert attempts and 'locked' in attempts[0], attempts
    p = baselines.load(conn, a['id'])
    assert p['source'] == 'import:messages'
    assert p['turns'][0]['text'].startswith('system:')


def test_stale_caller_read_transaction_fails_without_partial_snapshot(conn, tmp_path):
    sid = seed(conn)
    conn.execute('BEGIN')
    conn.execute('SELECT * FROM live_session').fetchall()
    other = db.connect(tmp_path / 'regression.db')
    other.execute("UPDATE live_session SET source='new-source'")
    other.commit()
    other.close()
    with pytest.raises(sqlite3.OperationalError):
        baselines.snapshot(conn, sid)
    assert conn.in_transaction
    assert not rows(conn, 'conversation_baseline')
    conn.rollback()
    assert baselines.load(conn, baselines.snapshot(conn, sid)['id'])['source'] == 'new-source'


def test_existing_pre_feature_snapshot_remains_loadable(conn):
    sid = seed(conn)
    # Exact payload shape from PR #7 before stored turn IDs were added.
    payload = {'session_id': sid, 'source': 'import:messages', 'tier': 'C',
               'language': 'fr', 'meta': {},
               'turns': [{'index': 0, 'role': 'user', 'text': 'Historical', 'at': 'old'}]}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    ident = 'base_' + digest
    conn.execute('INSERT INTO conversation_baseline VALUES (?,?,?,?,?,?,?)',
                 (ident, sid, 0, digest, encoded, 'C', 'old'))
    conn.commit()
    assert baselines.load(conn, ident) == payload
    assert baselines.fork(conn, ident, 0, 'model', {'model': 'candidate'})['baseline_id'] == ident


@pytest.mark.parametrize('operation', ['snapshot', 'fork'])
def test_caller_commit_persists_operations_visible_to_reopened_connection(conn, tmp_path, operation):
    sid = seed(conn)
    a = baselines.snapshot(conn, sid)
    conn.execute('BEGIN')
    if operation == 'snapshot':
        result = baselines.snapshot(conn, sid, 0)
        table = 'conversation_baseline'
    else:
        result = baselines.fork(conn, a['id'], 0, 'context', {'prefix': 'neutral'})
        table = 'conversation_fork'
    other = db.connect(tmp_path / 'regression.db')
    try:
        assert not other.execute(f'SELECT id FROM {table} WHERE id=?', (result['id'],)).fetchone()
        conn.commit()
        assert other.execute(f'SELECT id FROM {table} WHERE id=?', (result['id'],)).fetchone()
    finally:
        other.close()


def test_fork_insert_failure_does_not_commit_or_rollback_caller_work(conn):
    a = baselines.snapshot(conn, seed(conn))
    conn.execute("CREATE TRIGGER reject_fork AFTER INSERT ON conversation_fork BEGIN SELECT RAISE(ABORT, 'injected'); END")
    conn.execute("UPDATE live_session SET label='pending'")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        baselines.fork(conn, a['id'], -1, 'context', {'x': 1})
    assert conn.in_transaction
    assert conn.execute('SELECT label FROM live_session').fetchone()[0] == 'pending'
    assert not rows(conn, 'conversation_fork')
    conn.rollback()


def test_fork_identity_includes_baseline_factor_and_position(conn):
    sid = seed(conn)
    full = baselines.snapshot(conn, sid)
    prefix = baselines.snapshot(conn, sid, 1)
    variants = [(full['id'], 0, 'context'), (prefix['id'], 0, 'context'),
                (full['id'], 1, 'context'), (full['id'], 0, 'model')]
    ids = {baselines.fork(conn, b, i, f, {'x': 1})['id'] for b, i, f in variants}
    assert len(ids) == len(variants)
