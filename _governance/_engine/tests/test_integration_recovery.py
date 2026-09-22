"""Receipt invalidation must remain reviewable without rewinding capture cursors.

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
from osk import core, distillation as D, growth, integration as it, validate, write
validate.make_mini_vault(core.ROOT)
sid = 'receipt-recovery'
native = core.ROOT / 'native.jsonl'
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
    result = it.capture('claude', sid, str(native), sid, '= Scope/W1')
    assert result['ok'], result
    return result
def preserved(through, key=None):
    return it.acknowledge('claude', sid, through, 'preserved',
                          'The observation remains useful in this project.', [{'key':key or proof_key}])
def setup(ack=True):
    global proof_key
    first = capture(1)
    first['review_key'] = it.prompt('claude', sid)['key']
    proof_key = first['review_key'] + ':retained-observation'
    created = D.create_node({'key':proof_key,'sources':first['pending_refs'],'hub':'W1'},
                            title='Retained observation', summary='Recovery evidence',
                            body='A completed observation worth retaining.',
                            drafter='fable-5', space='= Scope/W1')
    assert created['distillation']['status'] == 'complete', created
    if ack:
        assert not preserved(first['through'])['pending']
    return first, created
def remove_link(created):
    result = write.update_node('W1', old_text='- [[' + created['name'] + ']]', new_text='')
    assert result['ok'], result
def repair_link(created):
    result = D.resume(proof_key, name=created['id'])
    assert result['distillation']['status'] == 'complete', result
def own_job(result):
    return next(j for j in result['jobs'] if j['conversation_id'] == sid)
"""


class IntegrationRecoveryTests(unittest.TestCase):
    def test_repair_budget_requires_each_chunk_and_rechecks_completed_chunks(self):
        self.check_case("""
            from osk import scope_memory
            capture(8)
            parent = it.prompt('claude', sid)
            scope_memory.replace(sid, 'retained summary')
            it.acknowledge('claude', sid, parent['through'], 'summary', 'Initial review.',
                           [{'text': 'retained summary'}])
            scope_memory.replace(sid, 'changed summary', expect_hash=scope_memory.read(sid)['hash'])
            assert it.review_status('claude', sid, parent['through'])['status'] == 'pending'
            refs, tokens = [], []
            while it.status('claude', sid)['pending']:
                job = it.prompt('claude', sid, max_rounds=3)
                assert 1 <= len(job['pending_refs']) <= 3, job
                assert not set(job['pending_refs']).intersection(refs)
                refs.extend(job['pending_refs'])
                tokens.append(job['through'])
                it.acknowledge('claude', sid, job['through'], 'summary', 'Reviewed this chunk.',
                               [{'text': 'changed summary'}])
                if len(refs) < 8:
                    assert it.review_status('claude', sid, parent['through'])['status'] == 'pending'
            assert len(tokens) == 3 and set(refs) == set(parent['pending_refs'])
            assert it.review_status('claude', sid, parent['through'])['status'] == 'complete'
            scope_memory.replace(sid, 'another summary', expect_hash=scope_memory.read(sid)['hash'])
            state = it.status('claude', sid)
            assert set(state['repair_pending']) == set(tokens), state
            assert it.review_status('claude', sid, parent['through'])['status'] == 'pending'
            assert state['reviewed_rounds'] == 8
        """)

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

    def test_unchanged_transcript_queues_repair_and_same_through_reack(self):
        self.check_case("""
            first, created = setup()
            original = native.read_bytes()
            remove_link(created)
            failed = it.review_status('claude', sid, first['through'])
            assert failed['status'] == 'pending', failed
            state = it.status('claude', sid)
            assert state['pending'], state
            assert state['reviewed_rounds'] == 1, state
            assert first['through'] in state['repair_pending'], state
            assert state['pending_refs'] == first['pending_refs'], state
            job = own_job(it.list_pending())
            assert job['through'] == first['through'], job
            assert '복구 대기' in job['prompt'], job
            caught = it.catchup()
            assert own_job(caught)['through'] == first['through'], caught
            assert caught['captures'][0]['appended'] == 0, caught
            assert native.read_bytes() == original
            repair_link(created)
            assert it.status('claude', sid)['pending']  # Repair needs an explicit replacement ACK.
            done = preserved(first['through'])
            assert not done['pending'] and done['reviewed_rounds'] == 1, done
            assert it.review_status('claude', sid, first['through'])['status'] == 'complete'
        """)

    def test_repair_survives_new_tail_and_defer_without_rewinding(self):
        self.check_case("""
            first, created = setup()
            remove_link(created)
            assert it.review_status('claude', sid, first['through'])['status'] == 'pending'
            repair_key = it.prompt('claude', sid)['key']
            newer = capture(2)
            done = it.acknowledge('claude', sid, newer['through'], 'no_value',
                                  'The later round is a one-off check.')
            assert done['reviewed_rounds'] == 2, done
            assert done['pending'] and first['through'] in done['repair_pending'], done
            assert done['last_review']['refs'] == newer['pending_refs'][1:], done
            assert it.prompt('claude', sid)['through'] == first['through']
            deferred = it.acknowledge('claude', sid, first['through'], 'deferred',
                                      'Still checking the retained source.')
            assert deferred['pending'] and deferred['reviewed_rounds'] == 2, deferred
            assert it.prompt('claude', sid)['key'] == repair_key
            repair_link(created)
            repaired = preserved(first['through'])
            assert not repaired['pending'] and repaired['reviewed_rounds'] == 2, repaired
            assert it.review_status('claude', sid, newer['through'])['status'] == 'complete'
            assert it.review_status('claude', sid, first['through'])['status'] == 'complete'
        """)

    def test_native_resume_discovers_latest_invalid_receipt_and_prompts_now(self):
        self.check_case("""
            first, created = setup()
            remove_link(created)
            resumed = it.hook_capture({'harness':'claude','session_id':sid,
                                       'transcript_path':str(native)}, sid)
            assert resumed['pending'] and resumed['reviewed_rounds'] == 1, resumed
            assert first['through'] in resumed['repair_pending'], resumed
            assert it.tick('claude', sid)['due']
            prompt = it.prompt('claude', sid)
            assert prompt['through'] == first['through'], prompt
            assert prompt['key'] == it.prompt('claude', sid)['key']
            assert any(p['key'] == proof_key for p in prompt['previous_distillations']), prompt
            repair_link(created)
            assert not preserved(prompt['through'])['pending']
        """)

    def test_explicit_old_final_failure_persists_after_newer_ack(self):
        self.check_case("""
            first, created = setup()
            newer = capture(2)
            it.acknowledge('claude', sid, newer['through'], 'no_value', 'Transient later check.')
            assert it.review_status('claude', sid, first['through'])['status'] == 'complete'
            remove_link(created)
            assert not it.status('claude', sid)['pending']  # No all-history integrity scan.
            path = it.state_path('claude', sid)
            before = path.read_bytes()
            with core.mutation_lock():
                failed = it._review_status_locked('claude', sid, first['through'])
            assert failed['status'] == 'pending', failed
            assert path.read_bytes() == before  # Mutation-lock caller never takes local lock/writes.
            assert it.review_status('claude', sid, first['through'])['status'] == 'pending'
            state = it.status('claude', sid)
            assert state['pending'] and state['reviewed_rounds'] == 2, state
            assert own_job(it.catchup())['through'] == first['through']
        """)

    def test_changed_target_requires_replacement_proof_for_same_snapshot(self):
        self.check_case("""
            first, created = setup()
            old_key = first['review_key']
            changed = write.update_node(created['id'], body='Concurrent revised observation.',
                                        expect_hash=created['new_hash'])
            assert changed['ok'], changed
            assert it.review_status('claude', sid, first['through'])['status'] == 'pending'
            try:
                preserved(first['through'])
                raise AssertionError('stale target proof acknowledged')
            except ValueError:
                pass
            prompt = it.prompt('claude', sid)
            assert prompt['key'] != old_key, prompt
            assert prompt['key'] == it.prompt('claude', sid)['key']
            key = prompt['key'] + ':retained-observation'
            repaired = D.update_node({'key':key,'sources':first['pending_refs'],'hub':'W1'},
                                     name=created['id'], body='Rechecked observation with corrected evidence.',
                                     expect_hash=changed['new_hash'])
            assert repaired['distillation']['status'] == 'complete', repaired
            done = preserved(first['through'], key)
            assert not done['pending'] and done['reviewed_rounds'] == 1, done
            state = json.loads(it.state_path('claude', sid).read_text(encoding='utf-8'))
            assert len(state['reviews']) == 2 and state['reviews'][0]['receipts'][0]['key'] == proof_key
            assert it.review_status('claude', sid, first['through'])['status'] == 'complete'
            changed_again = write.update_node(created['id'], body='A later independent correction.',
                                              expect_hash=repaired['new_hash'])
            assert changed_again['ok'], changed_again
            assert it.review_status('claude', sid, first['through'])['status'] == 'pending'
            assert it.prompt('claude', sid)['key'] != prompt['key']
        """)

    def test_growth_final_invalidation_is_selected_again_after_newer_ack(self):
        self.check_case("""
            first, created = setup(ack=False)
            # Worker completes selected A, then another completed round B. Its
            # last action invalidates A, after both ACKs and before final checks.
            worker = '\\n'.join([
                'import json,sys',
                'from pathlib import Path',
                'from osk import core,growth,integration as it,write',
                'sys.stdin.read()',
                "plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]",
                "job = plan['scope_jobs'][0]",
                "it.acknowledge('claude', SID, job['through'], 'preserved', 'Retained useful observation.', [{'key':PROOF}])",
                "Path(NATIVE).write_text(SECOND, encoding='utf-8')",
                "newer = it.capture('claude', SID, NATIVE, SID, '= Scope/W1')",
                "assert newer['ok'], newer",
                "it.acknowledge('claude', SID, newer['through'], 'no_value', 'The later check is transient.')",
                "for candidate in plan['candidates']:",
                "    growth.review(candidate['key'], 'no_value', reason='No common rule in this comparison.', manifest=plan['rid'])",
                "write.update_node('W1', old_text=LINK, new_text='')"])
            worker = ('SID=' + repr(sid) + '; NATIVE=' + repr(str(native)) + '; SECOND='
                      + repr(rounds(2)) + '; LINK=' + repr('- [[' + created['name'] + ']]')
                      + '; PROOF=' + repr(proof_key) + '\\n' + worker)
            result = growth.run([sys.executable, '-B', '-c', worker], limit=3)
            assert result['state'] == 'incomplete' and result['scope_selected'] == 1, result
            state = json.loads(it.state_path('claude', sid).read_text(encoding='utf-8'))
            assert first['through'] in state.get('repair_pending', {}), state
            assert state['reviewed_count'] == 2, state
            again = growth.run([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'], limit=3)
            assert again['scope_selected'] == 1, again
            selected = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]['scope_jobs']
            assert selected[0]['through'] == first['through'], selected
        """)


if __name__ == "__main__":
    unittest.main()
