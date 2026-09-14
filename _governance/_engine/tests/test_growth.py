"""Run with python tests/test_growth.py; every check owns a subprocess mini-vault."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = Path(__file__).resolve().parent.parent
BOOT = """
import json, sys
from pathlib import Path
from osk import core, graph, growth, validate, write
validate.make_mini_vault(core.ROOT)
def node(title, scope='W1', body=None):
    directory = core.ROOT / '= Scope' / scope
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / (scope + '.md')).exists():
        original = (core.ROOT / '= Scope/W1/W1.md').read_text(encoding='utf-8')
        (directory / (scope + '.md')).write_text(
            original.replace('260801-zzzz-w1ix', '260801-zzzz-' + scope.lower() + 'ix')
                    .replace('W1', scope), encoding='utf-8')
    result = write.create_node(title, title, body or title + ' reusable observation',
                               'gpt-6-astra', space='= Scope/' + scope)
    assert result['ok'], result
    return next(s for c in growth.plan(20)['candidates'] for s in c['sources']
                if s['name'] == title)
def register(planned=None):
    planned = growth.plan(20) if planned is None else planned
    with core.mutation_lock():
        return core.ledger_append(growth.LEDGER, {'kind':'plan', **planned})
def no_value_all():
    manifest = register()
    for candidate in manifest['candidates']:
        growth.review(candidate['key'], 'no_value', reason='No reusable synthesis in this exact set.', manifest=manifest['rid'])
def packet_worker(change='', wrapper='plain', code=0):
    source = "import json,sys; from osk import core,growth,distillation,integration; sys.stdin.read(); p=[r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]; q={'osk_reviews':{'manifest':p['rid'],'domain':[{'key':c['key'],'outcome':'no_value','reason':'No reusable synthesis in these compared sources.'} for c in p['candidates']],'scope':[dict((k,j[k]) for k in ('harness','conversation_id','through')) | {'outcome':'no_value','reason':'Only a completed one-off job.'} for j in p['scope_jobs']]}}; "
    if change:
        source += change + '; '
    wrappers = {
        'plain': "print(json.dumps(q))",
        'codex': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}},{'type':'turn.completed'}]]",
        'claude': "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(q)}))",
        'codex_tool': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'command_execution','aggregated_output':json.dumps(q)}},{'type':'turn.completed'}]]",
        'codex_no_complete': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}}]]",
        'codex_late_error': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}},{'type':'turn.completed'},{'type':'error','message':'later failure'}]]",
        'claude_tool': "[print(json.dumps(e)) for e in [{'type':'user','message':{'content':[{'type':'tool_result','content':json.dumps(q)}]}},{'type':'result','subtype':'success','is_error':False,'result':'No final packet.'}]]",
        'claude_error': "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'result':json.dumps(q)}))",
    }
    return source + wrappers[wrapper] + '; sys.exit(' + str(code) + ')'
"""


class GrowthTests(unittest.TestCase):
    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-growth-test-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory)
            result = subprocess.run([sys.executable, "-B", "-c", BOOT + textwrap.dedent(source)],
                                    env=env, cwd=directory, capture_output=True,
                                    text=True, encoding="utf-8", timeout=90,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_inventory_does_not_launch(self):
        self.check_case("""
            from unittest.mock import patch
            with patch('osk.growth.subprocess.Popen', side_effect=AssertionError('launched')):
                result = growth.run(['unused-command'])
            assert result == {'ok':True, 'state':'skipped', 'selected':0}, result
            assert not growth.LEDGER.exists()
        """)

    def test_worker_hooks_do_not_feed_maintenance_into_new_conversations(self):
        self.check_case("""
            node('A')
            change = "import os,subprocess; assert os.environ.get('OSK_GROWTH_WORKER')=='1'; hooks=Path(growth.__file__).resolve().parents[1]/'scripts/hooks'; results=[subprocess.run([sys.executable,'-B',str(hooks/name)],input=b'{}',capture_output=True,timeout=20) for name in ('claude_session_start.py','claude_prompt_submit.py','capture_stop.py')]; assert all(r.returncode==0 and not r.stdout and not r.stderr for r in results),results"
            result = growth.run([sys.executable,'-c',packet_worker('from pathlib import Path; '+change)],limit=1)
            assert result['ok'], result
            assert not growth.run(['unused-command'])['selected']
        """)

    def test_new_source_and_content_changes_reopen_comparisons(self):
        self.check_case("""
            first = node('A')
            no_value_all()
            assert not growth.plan()['candidates']
            node('B')
            candidates = growth.plan()['candidates']
            assert any({s['name'] for s in c['sources']} == {'A','B'} for c in candidates)
            no_value_all()
            assert not growth.plan()['candidates']
            result = write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            assert result['ok'], result
            assert growth.plan()['candidates']
        """)

    def test_cross_scope_old_new_and_completed_batch_progress(self):
        self.check_case("""
            for name in ('A','B','C','D','E'):
                node(name)
            first = growth.plan(1)
            assert len(first['candidates']) == 1
            manifest = register(first)
            growth.review(first['candidates'][0]['key'], 'no_value', reason='No value in first batch', manifest=manifest['rid'])
            second = growth.plan(1)
            assert second['candidates'][0]['key'] != first['candidates'][0]['key']
            no_value_all()
            assert not growth.plan()['candidates']
            node('Z', 'W2')
            candidates = growth.plan(20)['candidates']
            assert any({'A','Z'} <= {s['name'] for s in c['sources']} for c in candidates)
            assert all(len(c['sources']) <= 8 for c in candidates)
            no_value_all()
            assert not growth.plan()['candidates']
        """)

    def test_stale_manifest_and_unrecorded_key_are_rejected(self):
        self.check_case("""
            node('A')
            candidate = growth.plan()['candidates'][0]
            try:
                growth.review(candidate['key'], 'no_value', reason='Not registered', manifest='unknown')
                raise AssertionError('unrecorded review accepted')
            except ValueError:
                pass
            manifest = register()
            write.update_node('A', old_text='A reusable observation', new_text='Changed after selection')
            for outcome in ('no_value','deferred'):
                try:
                    growth.review(candidate['key'], outcome, reason='Stale', manifest=manifest['rid'])
                    raise AssertionError('stale review accepted')
                except ValueError:
                    pass
            assert not [r for r in core.ledger_read(growth.LEDGER) if r.get('kind') == 'review']
        """)

    def test_process_exit_alone_is_not_receipt(self):
        self.check_case("""
            node('A')
            for code in (0, 7):
                result = growth.run([sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(' + str(code) + ')'])
                assert not result['ok'], result
                assert result['returncode'] == code
                assert set(result['outcomes'].values()) == {'pending'}
                assert growth.plan()['candidates']
            assert not [r for r in core.ledger_read(growth.LEDGER) if r.get('kind') == 'review']
        """)

    def test_worker_receipt_and_deferred_next_run(self):
        self.check_case("""
            node('A')
            worker = "import sys; from osk import core,growth; assert sys.stdin.read(); plans=[r for r in core.ledger_read(growth.LEDGER) if r.get('kind')=='plan']; [growth.review(c['key'], 'deferred', reason='Need more evidence',manifest=plans[-1]['rid']) for c in plans[-1]['candidates']]"
            result = growth.run([sys.executable, '-c', worker])
            assert not result['ok'], result
            assert growth.plan()['candidates']
            worker = worker.replace("'deferred'", "'no_value'")
            result = growth.run([sys.executable, '-c', worker])
            assert result['ok'], result
            assert set(result['outcomes'].values()) == {'no_value'}
            assert growth.run(['unused-command'])['state'] == 'skipped'
        """)

    def test_preserved_requires_current_distillation_evidence(self):
        self.check_case("""
            node('A')
            manifest = register()
            candidate = manifest['candidates'][0]
            try:
                growth.review(candidate['key'], 'preserved', target='Invented', reason='A process said success',manifest=manifest['rid'])
                raise AssertionError('unverified preservation accepted')
            except ValueError:
                pass
            assert growth.plan()['candidates']
        """)

    def test_deferred_candidates_rotate(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            tried = []
            for _ in range(4):
                selected = register(growth.plan(1))
                candidate = selected['candidates'][0]
                assert candidate['key'] not in tried, tried
                tried.append(candidate['key'])
                growth.review(candidate['key'], 'deferred', reason='Need independent corroboration', manifest=selected['rid'])
            assert growth.plan()['candidates']
        """)

    def test_new_versions_get_priority_without_starving_old_comparisons(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            inventory = growth.plan(20)
            register(dict(inventory, candidates=[c for c in inventory['candidates']
                                                if c['grouping']=='comparison']))
            # Sources were attempted in pairs, but individual batches remain untried.
            node('Z', 'W2')
            first = growth.plan(2)
            assert any(s['name']=='Z' for s in first['candidates'][0]['sources']), first
            assert all(s['name']!='Z' for s in first['candidates'][1]['sources']), first
            assert growth.plan(2)==first, 'preview consumed the new version'
            register(first)
            assert write.update_node('I', old_text='I reusable observation', new_text='I changed')['ok']
            second = growth.plan(2)
            assert any(s['name']=='I' for s in second['candidates'][0]['sources']), second
            assert all(s['name']!='I' for s in second['candidates'][1]['sources']), second
            register(second)
            picked = []
            for i in range(4):
                node('Z'+str(i), 'W2')  # continuous new content, limit=1 still rotates
                selected = register(growth.plan(1))['candidates'][0]
                picked.append({s['name'] for s in selected['sources']})
            assert any(names <= set('ABCDEFGHI') for names in picked), picked
        """)

    def test_useful_subset_does_not_create_false_evidence(self):
        self.check_case("""
            from osk import distillation, contract
            for name in ('A','B','Noise','Other'):
                node(name)
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='= Domain/Principles')
            try:
                write.create_node(**request)  # the user-only confirmation gate remains intact
            except write.WriteError:
                pass
            assert write.create_node(**request)['ok']
            manifest = register(growth.plan(1))
            candidate = manifest['candidates'][0]
            selected = [s for s in candidate['sources'] if s['name'] in {'A','B'}]
            result = distillation.create_node(
                {'key':candidate['key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in selected],'hub':'Principles'},
                title='Shared rule',summary='Rule from A and B',body='A and B support this rule, with limited scope.',
                drafter='gpt-6-astra',space='= Domain/Principles')
            assert result['ok'], result
            receipt = growth.review(candidate['key'],'preserved',target='Shared rule',
                reason='A and B support the rule; Noise and Other are unrelated and omitted.',manifest=manifest['rid'])
            actual = contract.parse(core.ROOT / '= Domain/Principles/Shared rule.md').edges('derived-from')
            assert set(actual) == {s['id'] for s in selected}, actual
            assert len(receipt['omitted_sources']) == 2
            assert candidate['key'] not in {c['key'] for c in growth.plan(20)['candidates']}
            distillation._job_path(candidate['key']).unlink()
            assert candidate['key'] not in {c['key'] for c in growth.plan(20)['candidates']}, 'shared receipt depended on local journal'
            write.update_node('Shared rule',old_text='A and B support this rule, with limited scope.',new_text='Changed after completion.')
            assert candidate['key'] in {c['key'] for c in growth.plan(20)['candidates']}
        """)

    def test_timeout_stops_owned_descendants(self):
        self.check_case("""
            import time
            node('A')
            marker = core.ROOT / 'late-child.txt'
            child = "import pathlib,time; time.sleep(4); pathlib.Path('late-child.txt').write_text('survived')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); sys.stdin.read(); time.sleep(20)"
            result = growth.run([sys.executable,'-c',parent],timeout=1)
            assert not result['ok'] and result['error'], result
            assert result['cleanup_error'] is None, result
            time.sleep(4)
            assert not marker.exists(), 'descendant survived runner timeout'
            assert growth.plan()['candidates']
        """)

    def test_previous_saved_output_is_available_without_duplicate_node(self):
        self.check_case("""
            from osk import distillation
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='= Domain/Principles')
            try:
                write.create_node(**request)
            except write.WriteError:
                pass
            assert write.create_node(**request)['ok']
            growth.run([sys.executable,'-c','import sys; sys.stdin.read()'],limit=1)
            first = [r for r in core.ledger_read(growth.LEDGER) if r['kind'] == 'plan'][-1]
            candidate = first['candidates'][0]
            result = distillation.create_node(
                {'key':candidate['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in candidate['sources']],'hub':'Principles'},
                title='Retained rule',summary='Previously written rule',body='A and B support this bounded rule.',
                drafter='gpt-6-astra',space='= Domain/Principles')
            assert result['ok'], result
            next_candidate = growth.plan(1)['candidates'][0]
            assert next_candidate['previous_distillations'][0]['key'] == candidate['distill_key']
            worker = "import sys; from osk import core,growth; sys.stdin.read(); p=[r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]; [growth.review(c['key'],'preserved',target='Retained rule',reason='Existing saved result still covers A and B.',manifest=p['rid']) for c in p['candidates']]"
            outcome = growth.run([sys.executable,'-c',worker],limit=1)
            assert outcome['ok'], outcome
            assert len([1 for p,k in graph.Index().nodes.values() if k[0]=='domain' and not graph.is_hub(p)]) == 1
        """)

    def test_short_finished_transcript_runs_same_actor_without_domain_candidates(self):
        self.check_case("""
            from osk import integration
            path = core.ROOT / 'native.jsonl'
            user = {'type':'user','sessionId':'short','uuid':'u1','message':{'role':'user','content':'Report this temporary completed job.'}}
            final = {'type':'assistant','sessionId':'short','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'Temporary job finished.'}],'stop_reason':'end_turn'}}
            path.write_text(json.dumps(user)+'\\n',encoding='utf-8')
            state = integration.capture('claude','short',str(path),'short-project',space='= Scope/W1')
            assert state['capture_pending'], state
            with path.open('a',encoding='utf-8') as f:
                f.write(json.dumps(final)+'\\n')
            for change in (
                    "q['osk_reviews']['scope'][0]['through']='stale-snapshot'",
                    "q['osk_reviews']['scope'][0].update(outcome='preserved',targets=[{'key':'missing-proof'}])"):
                rejected = growth.run([sys.executable,'-c',packet_worker(change)])
                assert not rejected['ok'] and rejected['final_reviews']['errors'], rejected
            worker = packet_worker()
            result = growth.run([sys.executable,'-c',worker])
            assert result['ok'], result
            assert result['scope_selected'] == 1 and result['domain_selected'] == 0, result
            assert {r['status'] for r in result['scope_outcomes'].values()} == {'complete'}
            assert growth.run(['unused-command'])['state'] == 'skipped'
        """)

    def test_final_packet_applies_only_observed_domain_preservation(self):
        self.check_case("""
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='= Domain/Principles')
            try:
                write.create_node(**request)
            except write.WriteError:
                pass
            assert write.create_node(**request)['ok']
            change = "c=p['candidates'][0]; result=distillation.create_node({'key':c['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in c['sources']],'hub':'Principles'},title='Observed rule',summary='Rule from A and B',body='A and B support this bounded rule.',drafter='gpt-6-astra',space='= Domain/Principles'); assert result['ok'],result; q['osk_reviews']['domain'][0].update(outcome='preserved',target='Observed rule',reason='A and B support this bounded rule.')"
            result = growth.run([sys.executable,'-c',packet_worker(change)],limit=1)
            assert result['ok'], result
            assert set(result['domain_outcomes'].values()) == {'preserved'}, result
            assert result['final_reviews']['state'] == 'applied', result
            assert growth.run(['unused-command'])['state'] == 'skipped'
        """)

    def test_final_provider_messages_accept_codex_and_claude(self):
        self.check_case("""
            for wrapper in ('codex','claude'):
                node(wrapper)
                result = growth.run([sys.executable,'-c',packet_worker(wrapper=wrapper)],limit=1)
                assert result['ok'], result
                assert result['final_reviews']['state'] == 'applied', result
        """)

    def test_tool_output_unfinished_and_failed_provider_packets_are_ignored(self):
        self.check_case("""
            node('A')
            for wrapper in ('codex_tool','codex_no_complete','codex_late_error','claude_tool','claude_error'):
                result = growth.run([sys.executable,'-c',packet_worker(wrapper=wrapper)])
                assert not result['ok'], (wrapper,result)
                assert result['final_reviews']['state'] == 'rejected', (wrapper,result)
                assert set(result['domain_outcomes'].values()) == {'pending'}, result
            result = growth.run([sys.executable,'-c',packet_worker(code=7)])
            assert not result['ok'] and result['final_reviews']['state'] == 'not_applied', result
            assert not [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
        """)

    def test_final_packet_manifest_and_allowlist_reject_whole_packet(self):
        self.check_case("""
            node('A')
            for change in (
                    "q['osk_reviews']['manifest']='old-manifest'",
                    "q['osk_reviews']['domain'][0]['key']='unselected'",
                    "q['osk_reviews']['scope']=[{'harness':'claude','conversation_id':'unknown','through':'unknown','outcome':'no_value','reason':'Spoof'}]",
                    "q['osk_reviews']['domain'].append(q['osk_reviews']['domain'][0])",
                    "q['osk_reviews']['domain'][0]['command']='must never execute'"):
                result = growth.run([sys.executable,'-c',packet_worker(change)])
                assert not result['ok'] and result['final_reviews']['state'] == 'rejected', result
            assert not [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
        """)

    def test_final_packet_saved_declaration_requires_actual_receipt(self):
        self.check_case("""
            node('A')
            change = "q['osk_reviews']['domain'][0].update(outcome='preserved',target='Missing rule')"
            result = growth.run([sys.executable,'-c',packet_worker(change)])
            assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
            assert result['final_reviews']['errors'], result
            assert not [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
        """)

    def test_final_packet_does_not_duplicate_existing_valid_review(self):
        self.check_case("""
            node('A')
            node('B','W2')
            change = "growth.review(**q['osk_reviews']['domain'][0],manifest=p['rid'])"
            result = growth.run([sys.executable,'-c',packet_worker(change)],limit=3)
            assert result['ok'], result
            assert 'already_complete' in result['final_reviews']['domain'].values(), result
            assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']) == 3
        """)


if __name__ == '__main__':
    unittest.main()
