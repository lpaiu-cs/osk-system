"""After acknowledgement only structure is observed; only a registered correction reopens.

Run: python _governance/_engine/tests/test_integration_recovery.py
Every case uses a subprocess mini-vault; no native user transcript is read.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = Path(__file__).resolve().parents[1]
BOOT = """
import json, sys
from pathlib import Path
from osk import core, distillation as D, growth, integration as it, raw, transcripts, validate, write, conversation_capture, conversation_cite, conversation_review, conversation_state
from osk.vault import Vault
VAULT = Vault.open()
validate.make_mini_vault(VAULT, VAULT.root)
sid = 'receipt-recovery'
native = VAULT.root / 'native.jsonl'
def rounds(n):
    rows = []
    for i in range(1, n + 1):
        rows.extend([
            {'type':'user','sessionId':sid,'uuid':f'user-{i}',
             'message':{'role':'user','content':f'question {i}'}},
            {'type':'assistant','sessionId':sid,'uuid':f'answer-{i}',
             'message':{'id':f'message-{i}','role':'assistant','stop_reason':'end_turn',
                        'content':[{'type':'text','text':f'observation {i}'}]}}])
    return ''.join(json.dumps(row) + '\\n' for row in rows)
def capture(n):
    native.write_text(rounds(n), encoding='utf-8')
    result = conversation_capture.capture(VAULT, 'claude', sid, str(native), sid, '00_Scope/W1')
    assert result['ok'], result
    if write.resolve_session(VAULT, sid) != 'W1':
        write.set_primary(VAULT, sid, 'W1', 'fixture')   # 인용·추적은 결속하지 않는다 — 주된 scope는 지정으로(v5.2)
    return result
def preserved(through, key=None):
    return conversation_review.acknowledge(VAULT, 'claude', sid, through, 'preserved',
                          'The observation remains useful in this project.', [{'key':key or proof_key}])
def setup(ack=True):
    global proof_key
    first = capture(1)
    first['review_key'] = it.prompt(VAULT, 'claude', sid)['key']
    proof_key = first['review_key'] + ':retained-observation'
    # A node's source is a cited round of the original turn, not the turn's ref itself.
    cite = conversation_cite.cite(VAULT, 'claude/' + sid, turn='-1')
    first['cited'] = cite['round_ref']
    # The round is bound by its 근거 line, not by derived-from (Constitution art. 8 §6).
    first['line'] = chr(10).join(['', '', '## 근거', '', '- 2026-10-31 · `' + cite['coordinate'] + '` · 지지 — question 1'])
    created = D.create_node(VAULT, {'key':proof_key,'sources':[first['cited']],'hub':'W1'},
                            title='Retained observation', summary='Recovery evidence',
                            body='A completed observation worth retaining.' + first['line'],
                            drafter='fable-5', space='00_Scope/W1')
    assert created['distillation']['status'] == 'complete', created
    if ack:
        assert not preserved(first['through'])['pending']
    return first, created
def stored(n):
    # The full-capture engine (<= v4.1) kept each round in `_raw/`, tracked by its hash.
    capture(n)
    path = conversation_state.state_path(VAULT, 'claude', sid)
    s = conversation_state.load_state(VAULT, path, 'claude', sid)
    parsed = transcripts.read(str(native), 'claude', sid)
    shown = parsed['dialogue_v1']
    record = VAULT.root / '00_Scope/W1/_raw/.records' / (s['record'] + '.txt')
    blocks = [raw.round_block(i, raw.escape_numeric_h2(shown[r['id']]['user']),
                         raw.escape_numeric_h2(shown[r['id']]['agent']), dialogue_id=r['id'])
              for i, r in enumerate(parsed['rounds'], 1)]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes('\\n'.join(blocks).encode('utf-8'))
    rel = record.relative_to(VAULT.root).as_posix()
    s['rounds'] = [{'id': r['id'], 'ref': f'{rel}#{i}', 'completion': r['completion'],
                    'hash': core.sha256_bytes(block.rstrip('\\n').encode('utf-8'))}
                   for i, (r, block) in enumerate(zip(parsed['rounds'], blocks), 1)]
    through = conversation_state.snapshot(s)
    s['snapshots'] = {through: {'count': len(s['rounds']), 'prompt_count': s['prompt_count']}}
    conversation_state.save_state(path, s)
    return record, through, [r['ref'] for r in s['rounds']]
def correct(record, old, new):
    data = record.read_bytes()  # bytes: a text write would also change every line ending
    assert old.encode() in data, old
    record.write_bytes(data.replace(old.encode(), new.encode()))
def remove_link(created):
    result = write.update_node(VAULT, 'W1', old_text='- [[' + created['name'] + ']]', new_text='')
    assert result['ok'], result
def own_jobs(result):
    return [j for j in result['jobs'] if j['conversation_id'] == sid]
def refused(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('accepted')
"""


class IntegrationRecoveryTests(unittest.TestCase):
    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-integration-recovery-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory)
            result = subprocess.run(
                [sys.executable, "-B", "-c", BOOT + textwrap.dedent(source)],
                env=env, cwd=directory, capture_output=True, text=True,
                encoding="utf-8", timeout=90,
                creationflags=0x08000000 if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_edits_after_ack_keep_the_review_closed(self):
        # 2026-10-05: a third of an explicit batch re-reviewed closed batches after such edits.
        self.check_case("""
            from osk import scope_memory
            first, created = setup()
            remove_link(created)
            changed = write.update_node(VAULT, created['id'], body='A revised observation.' + first['line'],
                                        expect_hash=created['new_hash'])
            assert changed['ok'], changed
            second = capture(2)
            scope_memory.replace(VAULT, sid, 'retained summary', space='00_Scope/W1')
            conversation_review.acknowledge(VAULT, 'claude', sid, second['through'], 'summary', 'Kept as shared memory.',
                           [{'text': 'retained summary'}])
            scope_memory.replace(VAULT, sid, 'pruned summary', expect_hash=scope_memory.read(VAULT, sid)['hash'])
            for through in (first['through'], second['through']):
                done = conversation_review.review_status(VAULT, 'claude', sid, through)
                assert done['status'] == 'complete' and 'structure' not in done, done
            state = conversation_capture.status(VAULT, 'claude', sid)
            assert not state['pending'] and state['repair_pending'] == {}, state
            assert not own_jobs(it.list_pending(VAULT))
        """)

    def test_structural_break_is_reported_not_reopened(self):
        self.check_case("""
            first, created = setup()
            dropped = write.update_node(VAULT, created['id'], body='A completed observation worth retaining.',
                                        expect_hash=created['new_hash'])   # drops the 근거 line
            assert dropped['ok'], dropped
            done = conversation_review.review_status(VAULT, 'claude', sid, first['through'])
            assert done['status'] == 'complete', done
            assert done['structure'] == [{'key': proof_key, 'reason': 'target no longer cites its source'}], done
            (VAULT.root / created['path']).unlink()
            gone = conversation_review.review_status(VAULT, 'claude', sid, first['through'])
            assert gone['structure'][0]['reason'] == 'target missing', gone
            path = conversation_state.state_path(VAULT, 'claude', sid)
            before = path.read_bytes()
            with core.mutation_lock(VAULT):
                assert conversation_review.review_status_locked(VAULT, 'claude', sid, first['through'])['status'] == 'complete'
            assert path.read_bytes() == before  # A status read never writes the cursor.
            assert not conversation_capture.status(VAULT, 'claude', sid)['pending']
        """)

    def test_pre_42_recheck_repairs_close_and_correction_repairs_stay(self):
        self.check_case("""
            first, created = setup()
            remove_link(created)
            second = capture(2)
            conversation_review.acknowledge(VAULT, 'claude', sid, second['through'], 'no_value', 'A one-off check.')
            path = conversation_state.state_path(VAULT, 'claude', sid)
            state = json.loads(path.read_text(encoding='utf-8'))
            # The earlier recheck reopened the first review after its hub link moved; a
            # correction (registered with its own reason) reopened the second.
            state['repair_pending'] = {
                first['through']: {'reason': 'preserved target/source/hub receipt changed',
                                   'refs': first['pending_refs'], 'since': '2026-10-01T00:00:00+09:00',
                                   'review_count': 1},
                second['through']: {'reason': 'User-authorized correction changed a stored round',
                                    'refs': second['pending_refs'], 'since': '2026-09-20T00:00:00+09:00',
                                    'review_count': 2}}
            path.write_text(json.dumps(state), encoding='utf-8')
            view = conversation_capture.tick(VAULT, 'claude', sid)
            assert list(view['repair_pending']) == [second['through']] and view['pending'], view
            assert conversation_review.review_status(VAULT, 'claude', sid, first['through'])['status'] == 'complete'
            assert it.prompt(VAULT, 'claude', sid)['through'] == second['through']
        """)

    def test_correction_reopens_the_reviews_that_read_the_round(self):
        self.check_case("""
            record, through, refs = stored(2)
            done = conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'Both stored rounds were one-off checks.')
            assert not done['pending'] and done['reviewed_rounds'] == 2, done
            for bad in ([], ['native:claude:' + sid + ':user-1'], [refs[0][:-1] + '9']):
                refused(lambda: conversation_review.reopen(VAULT, 'claude', sid, bad, 'A stored round was corrected.'))
            refused(lambda: conversation_review.reopen(VAULT, 'claude', sid, [refs[0]], ' '))
            # Records are append-only. Removing a secret found later is the exception, and the
            # corrector registers it; nothing would notice the corrected bytes on its own.
            correct(record, 'question 1', 'question one')
            assert not conversation_capture.status(VAULT, 'claude', sid)['pending']
            view = conversation_review.reopen(VAULT, 'claude', sid, [refs[0]], 'A secret was removed from round 1.')
            assert view['pending'] and view['reviewed_rounds'] == 2, view  # the cursor is not rewound
            assert view['repair_pending'][through]['reason'] == 'A secret was removed from round 1.', view
            assert set(view['pending_refs']) == set(refs), view
            job = own_jobs(it.list_pending(VAULT))[0]
            assert job['through'] == through and '복구 대기' in job['prompt'], job
            again = conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'The corrected round is still a one-off check.')
            assert not again['pending'] and again['repair_pending'] == {}, again
            assert conversation_review.review_status(VAULT, 'claude', sid, through)['status'] == 'complete'
        """)

    def test_correction_reopens_a_review_under_old_cursor_coordinates(self):
        self.check_case("""
            record, through, refs = stored(2)
            path = conversation_state.state_path(VAULT, 'claude', sid)
            s = conversation_state.load_state(VAULT, path, 'claude', sid)
            # Cursors from before the hidden record layout keep their visible wiki coordinates.
            old = ['[[' + r['ref'].replace('/.records/', '/').replace('.txt#', '.md#') + ']]' for r in s['rounds']]
            for r, ref in zip(s['rounds'], old):
                r['ref'] = ref
            conversation_state.save_state(path, s)
            conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'Both stored rounds were one-off checks.')
            correct(record, 'question 1', 'question one')
            for ref in (refs[0], old[0]):  # either spelling names the same round
                view = conversation_review.reopen(VAULT, 'claude', sid, [ref], 'A secret was removed from round 1.')
                assert through in view['repair_pending'] and set(view['pending_refs']) == set(old), view
            done = conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'The corrected round is still a one-off check.')
            assert not done['pending'], done
        """)

    def test_correction_of_a_cited_round_reopens_the_review_its_receipt_closed(self):
        self.check_case("""
            first, created = setup()
            cited = VAULT.root / first['cited'].split('#')[0]
            correct(cited, 'question 1', 'question one')
            view = conversation_review.reopen(VAULT, 'claude', sid, [first['cited']], 'A secret was removed from the cited words.')
            assert first['through'] in view['repair_pending'], view
            # The cited round is not among the review's own refs: the repair names it, and the node
            # whose receipt cited it, for whoever rereads — the conversation or a worker.
            repair = view['repair_pending'][first['through']]
            assert repair['corrected'] == [raw.canonical_ref(VAULT, first['cited'])], repair
            assert [(t['key'], t['id']) for t in repair['targets']] == [(proof_key, created['id'])], repair
            text = it.prompt(VAULT, 'claude', sid)['text']
            assert raw.canonical_ref(VAULT, first['cited']) in text and created['name'] in text, text
            # A worker's scope job is the listed job, read through the worker's field filter.
            job = own_jobs(it.list_pending(VAULT))[0]
            seen = growth._reading_plan({'scope_jobs': [job]})['scope_jobs'][0]
            assert seen['repair']['corrected'] == repair['corrected'], seen
            assert seen['repair']['targets'] == repair['targets'], seen
            assert 'repair.corrected' in growth.prompt(VAULT, growth.plan(VAULT, 3))
            refused(lambda: preserved(first['through']))  # its receipt cited the earlier bytes
            again = D.update_node(VAULT, {'key': proof_key + '-2', 'sources': [first['cited']], 'hub': 'W1'},
                                  name=created['id'],
                                  body='A completed observation, rechecked against its corrected words.' + first['line'],
                                  expect_hash=created['new_hash'])
            assert again['distillation']['status'] == 'complete', again
            done = preserved(first['through'], proof_key + '-2')
            assert not done['pending'] and done['repair_pending'] == {}, done
        """)

    def test_a_newer_correction_is_not_closed_by_an_ack_of_the_earlier_one(self):
        self.check_case("""
            record, through, refs = stored(1)
            conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'Initial stored review.')
            correct(record, 'question 1', 'question one')
            conversation_review.reopen(VAULT, 'claude', sid, refs, 'First correction.')
            earlier = it.prompt(VAULT, 'claude', sid)
            assert earlier['through'] == through, earlier
            correct(record, 'question one', 'question uno')
            assert conversation_review.reopen(VAULT, 'claude', sid, refs, 'Second correction.')['pending']
            # The reviewer read the first correction; its ACK names a token that closes nothing now.
            refused(lambda: conversation_review.acknowledge(VAULT, 'claude', sid, earlier['through'], 'no_value', 'Read the first correction.'))
            latest = it.prompt(VAULT, 'claude', sid)
            assert latest['through'] != earlier['through'] and latest['key'] != earlier['key'], latest
            assert latest['repair']['reason'] == 'Second correction.', latest
            assert latest['repair']['corrected'] == [raw.canonical_ref(VAULT, refs[0])], latest
            done = conversation_review.acknowledge(VAULT, 'claude', sid, latest['through'], 'no_value', 'Read the second correction.')
            assert not done['pending'] and done['repair_pending'] == {}, done
            assert conversation_review.review_status(VAULT, 'claude', sid, through)['status'] == 'complete'
            # The same correction registered twice is one obligation, not two.
            correct(record, 'question uno', 'question one again')
            conversation_review.reopen(VAULT, 'claude', sid, refs, 'Third correction.')
            twice = conversation_review.reopen(VAULT, 'claude', sid, refs, 'Third correction.')
            assert len(twice['repair_pending']) == 1, twice
        """)

    def test_reopened_review_is_split_into_bounded_parts(self):
        self.check_case("""
            record, through, refs = stored(8)
            conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'Initial review.')
            correct(record, 'question 1', 'question one')
            correct(record, 'question 8', 'question eight')
            conversation_review.reopen(VAULT, 'claude', sid, [refs[0], refs[7]], 'Two stored rounds were corrected.')
            seen, tokens = [], []
            while conversation_capture.status(VAULT, 'claude', sid)['pending']:
                job = it.prompt(VAULT, 'claude', sid, max_rounds=3)
                assert 1 <= len(job['pending_refs']) <= 3, job
                assert not set(job['pending_refs']).intersection(seen)
                seen.extend(job['pending_refs'])
                tokens.append(job['through'])
                conversation_review.acknowledge(VAULT, 'claude', sid, job['through'], 'no_value', 'Reviewed this corrected part.')
                if len(seen) < 8:
                    assert conversation_review.review_status(VAULT, 'claude', sid, through)['status'] == 'pending'
            assert len(tokens) == 3 and set(seen) == set(refs)
            assert conversation_review.review_status(VAULT, 'claude', sid, through)['status'] == 'complete'
            assert conversation_capture.status(VAULT, 'claude', sid)['reviewed_rounds'] == 8
        """)

    def test_reopened_review_survives_new_tail_and_defer_without_rewinding(self):
        self.check_case("""
            record, through, refs = stored(1)
            conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'Initial stored review.')
            correct(record, 'question 1', 'question one')
            conversation_review.reopen(VAULT, 'claude', sid, refs, 'Round 1 was corrected.')
            repair_key = it.prompt(VAULT, 'claude', sid)['key']
            newer = capture(2)
            done = conversation_review.acknowledge(VAULT, 'claude', sid, newer['through'], 'no_value',
                                  'The later round is a one-off check.')
            assert done['reviewed_rounds'] == 2, done
            assert done['pending'] and through in done['repair_pending'], done
            assert it.prompt(VAULT, 'claude', sid)['through'] == through
            deferred = conversation_review.acknowledge(VAULT, 'claude', sid, through, 'deferred', 'Still checking the corrected round.')
            assert deferred['pending'] and deferred['reviewed_rounds'] == 2, deferred
            assert it.prompt(VAULT, 'claude', sid)['key'] == repair_key
            repaired = conversation_review.acknowledge(VAULT, 'claude', sid, through, 'no_value', 'The corrected round is still a one-off check.')
            assert not repaired['pending'] and repaired['reviewed_rounds'] == 2, repaired
            assert conversation_review.review_status(VAULT, 'claude', sid, newer['through'])['status'] == 'complete'
        """)

    def test_growth_run_does_not_reopen_after_a_later_edit(self):
        self.check_case("""
            first, created = setup(ack=False)
            # Worker completes selected A, then another completed round B. Its last action
            # unlinks A's node from the hub, after both ACKs and before the final checks.
            worker = '\\n'.join([
                'import json,sys',
                'from pathlib import Path',
                'from osk import core,growth,integration as it,write, conversation_capture, conversation_cite, conversation_review, conversation_state; from osk.vault import Vault; VAULT = Vault.open()',
                'sys.stdin.read()',
                "plan = [r for r in core.ledger_read(growth.ledger_path(VAULT)) if r['kind']=='plan'][-1]",
                "job = plan['scope_jobs'][0]",
                "conversation_review.acknowledge(VAULT, 'claude', SID, job['through'], 'preserved', 'Retained useful observation.', [{'key':PROOF}])",
                "Path(NATIVE).write_text(SECOND, encoding='utf-8')",
                "newer = conversation_capture.capture(VAULT, 'claude', SID, NATIVE, SID, '00_Scope/W1')",
                "assert newer['ok'], newer",
                "conversation_review.acknowledge(VAULT, 'claude', SID, newer['through'], 'no_value', 'The later check is transient.')",
                "for candidate in plan['candidates']:",
                "    growth.review(VAULT, candidate['key'], 'no_value', reason='No common rule in this comparison.', manifest=plan['rid'])",
                "write.update_node(VAULT, 'W1', old_text=LINK, new_text='')"])
            worker = ('SID=' + repr(sid) + '; NATIVE=' + repr(str(native)) + '; SECOND='
                      + repr(rounds(2)) + '; LINK=' + repr('- [[' + created['name'] + ']]')
                      + '; PROOF=' + repr(proof_key) + '\\n' + worker)
            fork_job = it.prompt(VAULT, 'claude', sid, include_organization=False, max_rounds=3)
            result = growth.run(VAULT, [sys.executable, '-B', '-c', worker], limit=3, scope_job=fork_job)
            assert result['scope_selected'] == 1, result
            assert all(o['status'] == 'complete' for o in result['scope_outcomes'].values()), result
            state = json.loads(conversation_state.state_path(VAULT, 'claude', sid).read_text(encoding='utf-8'))
            assert not state.get('repair_pending') and state['reviewed_count'] == 2, state
            again = growth.run(VAULT, [sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'], limit=3)
            assert not again.get('scope_selected'), again
        """)


if __name__ == "__main__":
    unittest.main()
