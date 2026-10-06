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
from osk import core, graph, growth, validate, write, contract, organization
validate.make_mini_vault(core.ROOT)
def node(title, scope='W1', body=None):
    directory = core.ROOT / '00_Scope' / scope
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / (scope + '.md')).exists():
        original = (core.ROOT / '00_Scope/W1/W1.md').read_text(encoding='utf-8')
        original = original.split(chr(10)+'---'+chr(10),1)[0] + chr(10)+'---'+chr(10)+'# W1'+chr(10)
        (directory / (scope + '.md')).write_text(
            original.replace('260801-zzzz-w1ix', '260801-zzzz-' + scope.lower() + 'ix')
                    .replace('W1', scope), encoding='utf-8')
    result = write.create_node(title, title, body or title + ' reusable observation',
                               'gpt-6-astra', space='00_Scope/' + scope)
    assert result['ok'], result
    hub = directory / (scope + '.md')
    old = contract.parse(hub).body
    write.update_node(scope, body=old.rstrip() + chr(10) + '- [[' + title + ']]', expect_hash=core.sha256_file(hub))
    return next(s for c in growth.plan(20)['candidates'] for s in c['sources']
                if s['name'] == title)
def legacy(sid, transcript, scope='W1'):
    # Rewrite a fresh cursor as the full-capture engine (<= v4.1) left it: rounds stored
    # in _raw/. The daily worker reviews only these; original turns stay with their conversation.
    from osk import integration, raw, transcripts
    path = integration.state_path('claude', sid)
    s = integration._load(path, 'claude', sid)
    shown = list(transcripts.read(str(transcript), 'claude', sid)['dialogue_v1'].values())
    record = core.ROOT / '00_Scope' / scope / '_raw' / '.records' / (s['record'] + '.txt')
    blocks = [raw._block(i, raw.escape_numeric_h2(r['user']), raw.escape_numeric_h2(r['agent']),
                         dialogue_id=r['id']) for i, r in enumerate(shown, 1)]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(chr(10).join(blocks).encode('utf-8'))
    rel = record.relative_to(core.ROOT).as_posix()
    s['rounds'] = [{'id': r['id'], 'ref': rel + '#' + str(i), 'completion': r['completion'],
                    'hash': core.sha256_bytes(b.rstrip(chr(10)).encode('utf-8'))}
                   for i, (r, b) in enumerate(zip(shown, blocks), 1)]
    s['snapshots'] = {integration._snapshot(s): {'count': len(s['rounds']), 'prompt_count': s['prompt_count']}}
    integration._save(path, s)
def register(planned=None):
    planned = growth.plan(20) if planned is None else planned
    with core.mutation_lock():
        return core.ledger_append(growth.LEDGER, {'kind':'plan', **planned})
def no_value_all():
    manifest = register()
    for candidate in manifest['candidates']:
        growth.review(candidate['key'], 'no_value', reason='No reusable synthesis in this exact set.', manifest=manifest['rid'])
def packet_worker(change='', wrapper='plain', code=0, org='complete', read='True'):
    # org: the organization review outcome; read: which review units u the worker read
    source = "import json,sys; from osk import core,growth,distillation,integration; sys.stdin.read(); p=[r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]; q={'osk_reviews':{'manifest':p['rid'],'domain':[{'key':c['key'],'outcome':'no_value','reason':'No reusable synthesis in these compared sources.'} for c in p['candidates']],'scope':[dict((k,j[k]) for k in ('harness','conversation_id','through')) | {'outcome':'no_value','reason':'Only a completed one-off job.'} for j in p['scope_jobs']]}}; "
    if change:
        source += change + '; '
    source += "from osk import organization; q['osk_reviews']['organization']=[{'key':j['key'],'scope':j['scope'],'outcome':'" + org + "','reason':'The fixture is one coherent, directly wired group.','intentional':[],'checked':[{'unit':u['unit'],'reason':'Known fixture claim and conditions'} for u in j['review_units'] if " + read + "]} for j in p['organization_jobs']]; "
    wrappers = {
        'plain': "print(json.dumps(q))",
        'codex': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}},{'type':'turn.completed'}]]",
        'claude': "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':json.dumps(q)}))",
        'codex_tool': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'command_execution','aggregated_output':json.dumps(q)}},{'type':'turn.completed'}]]",
        'codex_no_complete': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}}]]",
        'codex_late_error': "[print(json.dumps(e)) for e in [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':json.dumps(q)}},{'type':'turn.completed'},{'type':'error','message':'later failure'}]]",
        'claude_tool': "[print(json.dumps(e)) for e in [{'type':'user','message':{'content':[{'type':'tool_result','content':json.dumps(q)}]}},{'type':'result','subtype':'success','is_error':False,'result':'No final packet.'}]]",
        'claude_error': "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'result':json.dumps(q)}))",
        # Claude Code 2.1.281 appends session metadata after the final result; a forked
        # conversation with background tasks is woken by their notifications and answers again.
        'claude_trailing': "[print(json.dumps(e)) for e in [{'type':'system','subtype':'init'},{'type':'assistant','message':{'content':[{'type':'text','text':'done'}]}},{'type':'result','subtype':'success','is_error':False,'result':json.dumps(q)},{'type':'system','subtype':'task_notification'},{'type':'rate_limit_event'},{'type':'system','subtype':'post_turn_summary'},{'type':'system','subtype':'task_summary'}]]",
        'claude_two_turns': "[print(json.dumps(e)) for e in [{'type':'result','subtype':'success','is_error':False,'result':'Monitoring the job.'},{'type':'system','subtype':'task_notification'},{'type':'system','subtype':'init'},{'type':'assistant','message':{'content':[{'type':'text','text':'done'}]}},{'type':'result','subtype':'success','is_error':False,'result':json.dumps(q)},{'type':'system','subtype':'task_summary'}]]",
        'claude_continued': "[print(json.dumps(e)) for e in [{'type':'result','subtype':'success','is_error':False,'result':json.dumps(q)},{'type':'assistant','message':{'content':[{'type':'text','text':'One more thing.'}]}},{'type':'system','subtype':'task_summary'}]]",
    }
    return source + wrappers[wrapper] + '; sys.exit(' + str(code) + ')'
"""


class GrowthTests(unittest.TestCase):
    def test_first_turn_is_fair_when_budget_cannot_select_every_queue(self):
        self.check_case("""
            for limit in (3, 1, 2, 4, 5):
                rows, first = [], []
                for _ in range(8):
                    planned = {key:[{'key':key+'-0'}, {'key':key+'-1'}] for key in growth._QUEUES}
                    growth._select_work(planned,limit,rows)
                    order = planned['work_order']
                    first.append(order[0]['queue'])
                    assert len(order) == limit
                    assert len({(i['queue'],i['index']) for i in order}) == limit
                    for key in growth._QUEUES:
                        assert [i['index'] for i in order if i['queue']==key] == list(range(len(planned[key])))
                        assert len(planned[key]) + planned['queued_not_selected'][key] == 2
                    rows.append({'kind':'plan',**planned})
                # Even if each worker stops after its first job, every queue
                # receives a turn within one attempt per queue.
                q = len(growth._QUEUES)
                assert all(set(first[n:n+q]) == set(growth._QUEUES) for n in range(9-q)), (limit,first)
        """)

    def test_execution_order_rotates_even_when_every_queue_is_selected(self):
        self.check_case("""
            rows, first, q = [], [], len(growth._QUEUES)
            for _ in range(2 * q):
                planned = {key:[{'key':key}] for key in growth._QUEUES}
                growth._select_work(planned,q,rows)
                order = planned['work_order']
                first.append(order[0]['queue'])
                assert len(order) == q and {i['queue'] for i in order} == set(growth._QUEUES)
                rows.append({'kind':'plan',**planned})
            assert first[:q] == list(growth._QUEUES), first
            assert first[q:] == first[:q], first
            daily = {key:[{'key':key}] for key in growth._QUEUES}
            growth._select_work(daily,q,[dict(row,work_context='stop:W1') for row in rows])
            assert daily['work_order'][0]['queue'] == growth._QUEUES[0], daily
        """)

    def test_eviction_settlement_needs_review_and_deferred_returns_to_queue(self):
        for deferred in (True, False):
            self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            item = evictions.record_evict('W1', 'session', 'Reusable fact; second claim needs verification.')
            with patch.object(evictions, 'age_days', return_value=17):
                action = "from osk import evictions, write; j=p['eviction_jobs'][0]; saved=write.create_node('Partial', 'Partial evidence', 'Only the first claim is verified.', 'fixture', space='00_Scope/W1', settle=j['of']); assert saved['ok']; "
                if DEFERRED_CASE:
                    action += "q['osk_reviews']['eviction']=[{'of':j['of'],'outcome':'deferred','reason':'First claim saved; resume second claim against the source.'}]; assert growth.checkpoint(q)['ok']"
                else:
                    action += "sys.exit(7)"
                result = growth.run([sys.executable, '-B', '-c', packet_worker(action)], limit=1)
                assert not result['ok'], result
                assert result['eviction_outcomes']['eviction:'+item['rid']]['status'] == 'pending', result
                manifest = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                job = manifest['eviction_jobs'][0]
                assert item['rid'] not in {r['rid'] for r in evictions.unsettled()}
                again = growth.plan()['eviction_jobs']
                assert any(j['of']==item['rid'] for j in again), again
                assert again[0]['previous_settlement']['target'] == 'Partial'
                if DEFERRED_CASE:
                    assert again[0]['previous_deferral']['reason'].startswith('First claim saved')
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':item['rid'],'outcome':'node','target':'Partial',
                                       'reason':'Both claims now checked; second is a one-off value.'}]}}
                assert growth.checkpoint(packet)['ok']
                assert growth._eviction_status(job)['status'] == 'complete'
                count = len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='eviction_review'])
                assert growth.checkpoint(packet)['ok']
                assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='eviction_review']) == count
                assert not growth.plan()['eviction_jobs']
                # A later explicit deferral must supersede even a completed review.
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'deferred','reason':'Reopen a newly disputed condition.'}]
                assert growth.checkpoint(packet)['ok']
                assert growth._eviction_status(job)['status'] == 'pending'
                assert growth.plan()['eviction_jobs'][0]['of'] == item['rid']
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'node','target':'Partial','reason':'Dispute resolved against the source.'}]
                assert growth.checkpoint(packet)['ok']
            """.replace('DEFERRED_CASE', repr(deferred)))

    def test_cli_checkpoint_survives_later_worker_failure_without_closing_other_jobs(self):
        self.check_case("""
            node('A', 'W1')
            node('B', 'W2')
            change = "from pathlib import Path; import subprocess; q['osk_reviews']['domain']=q['osk_reviews']['domain'][:1]; q['osk_reviews']['scope']=[]; checkpoint=core.ROOT/'checkpoint.json'; checkpoint.write_text(json.dumps(q),encoding='utf-8'); done=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','checkpoint','--file',str(checkpoint)],capture_output=True,text=True); assert done.returncode==0 and json.loads(done.stdout)['ok'],done; sys.exit(7)"
            result = growth.run([sys.executable, '-B', '-c', packet_worker(change)], limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            assert list(result['domain_outcomes'].values()).count('no_value') == 1, result
            assert 'pending' in result['domain_outcomes'].values(), result
            rows = [r for r in core.ledger_read(growth.LEDGER) if r['kind'] == 'review']
            assert len(rows) == 1
            packet = json.loads((core.ROOT/'checkpoint.json').read_text(encoding='utf-8'))
            assert growth.checkpoint(packet)['ok']  # Retry is idempotent.
            assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']) == 1
            packet['osk_reviews']['domain'][0]['key'] = 'unselected'
            refused = growth.checkpoint(packet)   # only that entry is set aside, with its reason
            assert not refused['ok'] and any('unselected domain review' in e for e in refused['errors']), refused
            assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']) == 1
        """)

    def test_scope_checkpoint_survives_worker_failure_and_resumes_only_pending_work(self):
        self.check_case("""
            from osk import integration
            node('A')
            for sid in ('first', 'second'):
                path = core.ROOT / (sid + '.jsonl')
                rows = [
                    {'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'fixture question'}},
                    {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{
                        'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'fixture answer'}],
                        'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(row)+'\\n' for row in rows), encoding='utf-8')
                assert integration.capture('claude',sid,str(path),'checkpoint-fixture',space='00_Scope/W1')['ok']
                legacy(sid, path)
            change = "q['osk_reviews']['domain']=[]; q['osk_reviews']['scope']=q['osk_reviews']['scope'][:1]; (core.ROOT/'saved-checkpoint.json').write_text(json.dumps(q),encoding='utf-8'); assert growth.checkpoint(q)['ok']; sys.exit(7)"
            result = growth.run([sys.executable,'-B','-c',packet_worker(change)],limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            first_plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in first_plan['scope_jobs']] == ['first']
            assert integration.status('claude','first')['reviewed_rounds'] == 1
            packet = json.loads((core.ROOT/'saved-checkpoint.json').read_text(encoding='utf-8'))
            assert growth.checkpoint(packet)['ok']
            before = integration._load(integration.state_path('claude','first'),'claude','first')
            assert len(before['reviews']) == 1
            retry = growth.run([sys.executable,'-B','-c','import sys; sys.stdin.read()'],limit=3)
            assert not retry['ok']
            next_plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in next_plan['scope_jobs']] == ['second'], next_plan
            visible = growth._reading_plan(next_plan)['scope_jobs'][0]
            assert visible['raw_review'] == {'state':'verified','rounds':1,'error':None}
            assert visible['capture_recovery']['state'] == 'ready'
            after = integration._load(integration.state_path('claude','first'),'claude','first')
            assert after == before
            assert integration.status('claude','second')['reviewed_rounds'] == 0
        """)

    def test_recheck_jobs_queue_in_daily_runs_and_fork_scope(self):
        self.check_case("""
            node('A')
            assert write.create_node('B', 'b', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            assert not growth.plan(3)['recheck_jobs']
            write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            planned = growth.plan(3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['target']) for j in jobs] == [('B', '[[A]]')], jobs
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'pending'
            assert 'For recheck_jobs' in growth.prompt(planned)
            assert [j['node'] for j in growth._recheck_jobs(graph.Index(), 'W1')] == ['B']
            assert not growth._recheck_jobs(graph.Index(), 'W2')   # a fork keeps to its scope
            assert not growth.daily_active()
            manifest = register(planned)
            assert not growth.daily_active(), 'a plan is not completed work'
            core.ledger_append(growth.LEDGER, {'kind':'run','manifest':manifest['rid'],'ok':True})
            assert growth.daily_active()
            write.update_node('B', add_edges={'derived-from': 'A'})
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'pending', 'wiring is not a check'
            write.update_node('B', rechecked=['A'])
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'complete'
            assert not growth.plan(3)['recheck_jobs']
        """)

    def test_latest_daily_result_controls_recheck_handoff(self):
        self.check_case("""
            from datetime import datetime, timedelta, timezone
            from unittest.mock import patch
            planned = register()
            assert not growth.daily_active()
            core.ledger_append(growth.LEDGER, {'kind':'run','manifest':planned['rid'],'ok':True})
            assert growth.daily_active()
            core.ledger_append(growth.LEDGER, {'kind':'plan','work_context':'stop:W1'})
            assert growth.daily_active(), 'Stop work must not replace daily health'
            latest = register()
            assert not growth.daily_active(), 'a newer unfinished plan supersedes old success'
            core.ledger_append(growth.LEDGER, {'kind':'run','manifest':latest['rid'],'ok':False})
            assert not growth.daily_active(), 'failed daily work must yield rechecks to forks'
            core.ledger_append(growth.LEDGER, {'kind':'run','manifest':latest['rid'],'ok':True})
            assert growth.daily_active()
            rows = growth._records()
            old = (datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
            for row in rows:
                row['at'] = old
            with patch.object(growth,'_records',return_value=rows):
                assert not growth.daily_active(), 'expired success does not suppress fallback'
        """)

    def test_unrelated_capture_error_does_not_fail_completed_selected_work(self):
        self.check_case("""
            from osk import integration
            from unittest.mock import patch
            node('A')
            catchup = {'ok':False,'jobs':[],'captures':[], 'remaining':1,
                       'errors':[{'conversation_id':'other','error':'native identity mismatch'}]}
            with patch.object(integration,'catchup',return_value=catchup):
                result = growth.run([sys.executable,'-c',packet_worker()],limit=3)
                assert result['ok'] and result['selected'], result
                assert not result['capture']['ok'] and result['capture']['errors']==catchup['errors']
                assert growth.run(['unused-command'])['state']=='capture_pending'
            native = core.ROOT/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u',
                'message':{'role':'user','content':'question'}})+'\\n'+json.dumps(
                {'type':'assistant','sessionId':'own','uuid':'a','message':{'role':'assistant',
                 'id':'m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}})+'\\n',encoding='utf-8')
            assert integration.capture('claude','own',str(native),'own',space='00_Scope/W1')['ok']
            job = integration.prompt('claude','own')
            job.update(ok=False,capture_error='selected conversation capture failed')
            result = growth.run([sys.executable,'-c',packet_worker()],scope_job=job)
            assert not result['ok'], 'the selected conversation capture error remains a blocker'
        """)

    def test_recheck_escalation_holds_a_second_correction_for_the_user(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'B relies on A.', 'A'), ('C', 'C relies on B.', 'B')):
                assert write.create_node(title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            jobs = growth.plan(3)['recheck_jobs']
            assert [(j['node'], j['cascade'], j['next']) for j in jobs] == [('B', False, ['C'])], jobs
            # the agent's own correction of B is autonomous; C's recheck is then a cascade
            write.update_node('B', old_text='B relies on A.', new_text='B relies on revised A.',
                              rechecked=['A'])
            planned = growth.plan(3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['cascade']) for j in jobs] == [('C', True)], jobs
            manifest = register({**planned, 'scope_jobs': []})   # as run() records it
            packet = {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                      'recheck': [{'key': jobs[0]['key'], 'outcome': 'unchanged',
                                   'reason': 'x', 'proposal': 'y'}]}}
            refused = growth.checkpoint(packet)
            assert not refused['ok'] and any('only escalates' in e for e in refused['errors']), refused
            assert growth._recheck_status(jobs[0], graph.Index())['status'] != 'complete', 'a recheck review closed a check'
            packet['osk_reviews']['recheck'][0]['outcome'] = 'escalated'
            assert growth.checkpoint(packet)['ok']
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'complete'
            assert not growth.plan(3)['recheck_jobs'], 'an escalated check returned to the agent queue'
            held = rechecks.report(graph.Index()).get('escalated')
            assert [h['node'] for h in held] == ['C'] and held[0]['proposal'] == 'y', held
            write.update_node('C', rechecked=['B'])   # the user's decision
            assert not rechecks.report(graph.Index()), rechecks.report(graph.Index())
        """)

    def test_recheck_cascade_keeps_node_identity_when_bodies_match(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'Old B.', 'A'), ('C', 'C relies on B.', 'B'),
                                       ('X', 'Old X.', 'A'), ('Y', 'Y relies on X.', 'X')):
                assert write.create_node(title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            write.update_node('B', old_text='Old B.', new_text='The same claim.',
                              rechecked=['A'])
            write.update_node('X', old_text='Old X.', new_text='The same claim.')
            cascades = {i['node']: i['cascade'] for i in rechecks.candidates(graph.Index())[0]}
            assert cascades['C'] is True, cascades
            assert cascades['Y'] is False, cascades
        """)

    def test_recheck_pick_rotates_by_attempts_in_a_fork_scope(self):
        self.check_case("""
            node('A')
            for title in ('B1', 'B2'):
                assert write.create_node(title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            first = growth._pick_rechecks(graph.Index(), 1, 'W1')
            register({**growth.plan(3), 'scope_jobs': [], 'recheck_jobs': first})
            again = growth._pick_rechecks(graph.Index(), 1, 'W1')
            assert {first[0]['node'], again[0]['node']} == {'B1', 'B2'}, (first, again)
        """)

    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-growth-test-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory)
            result = subprocess.run([sys.executable, "-B", "-c", BOOT + textwrap.dedent(source)],
                                    env=env, cwd=directory, capture_output=True,
                                    text=True, encoding="utf-8", timeout=90,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_subscription_command_runs_only_on_a_confirmed_subscription(self):
        self.check_case("""
            import os
            from unittest.mock import patch
            from osk import harness
            node('A')
            fake = core.ROOT / 'fake-claude.py'
            fake.write_text(chr(10).join([
                "import json, os, sys",
                "args = sys.argv[1:]",
                "if args == ['--version']: print('2.1.281 (Claude Code)'); sys.exit(0)",
                "if args[:2] == ['auth', 'status']: print(os.environ['OSK_TEST_AUTH']); sys.exit(0)",
                "open(os.path.join(os.environ['OSK_VAULT_ROOT'], 'seen-env.json'), 'w').write(json.dumps("
                "{k: os.environ.get(k) for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')}))",
                packet_worker(wrapper='claude')]), encoding='utf-8')
            if os.name == 'nt':
                cli = core.ROOT / 'claude.cmd'
                cli.write_text('@"' + sys.executable + '" "' + str(fake) + '" %*' + chr(13) + chr(10), encoding='utf-8')
            else:
                cli = core.ROOT / 'claude'
                cli.write_text('#!/bin/sh' + chr(10) + 'exec "' + sys.executable + '" "' + str(fake) + '" "$@"' + chr(10),
                               encoding='utf-8')
                cli.chmod(0o755)
            argv = harness.get('claude').growth_argv(str(cli), sys.executable, 'server.py', str(core.ROOT))
            config = core.ROOT / 'claude-config'
            config.mkdir()
            env = {'CLAUDE_CONFIG_DIR': str(config), 'ANTHROPIC_API_KEY': 'sk-ant-test', 'ANTHROPIC_AUTH_TOKEN': 'tok',
                   'OSK_TEST_AUTH': json.dumps({'loggedIn': True, 'authMethod': 'apiKey'})}
            # An API-key login is refused before any plan or model call.
            with patch.dict(os.environ, env):
                refused = growth.run(argv)
            assert refused['state'] == 'unavailable' and 'subscription' in refused['error'], refused
            assert not growth.LEDGER.exists() and not (core.ROOT / 'seen-env.json').exists()
            # On a confirmed subscription the command runs, and no API credential reaches it.
            env['OSK_TEST_AUTH'] = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'})
            with patch.dict(os.environ, env):
                result = growth.run(argv)
            assert result['ok'], result
            seen = json.loads((core.ROOT / 'seen-env.json').read_text(encoding='utf-8'))
            assert seen == {'ANTHROPIC_API_KEY': None, 'ANTHROPIC_AUTH_TOKEN': None}, seen
            # The scheduler's real entry reads the command from the file and runs it the same way.
            import subprocess
            node('B')
            (core.ROOT / 'seen-env.json').unlink()
            command_file = core.ROOT / '.osk' / 'growth-command.json'
            command_file.parent.mkdir(parents=True, exist_ok=True)
            command_file.write_text(json.dumps(argv), encoding='utf-8')
            entry = Path(growth.__file__).resolve().parents[1] / 'scripts' / 'growth_run.py'
            for extra in (['--check'], ['--limit', '1']):
                done = subprocess.run([sys.executable, str(entry), '--command-file', str(command_file), *extra],
                                      env={**os.environ, **env}, capture_output=True, text=True,
                                      encoding='utf-8', timeout=120)
                assert done.returncode == 0 and json.loads(done.stdout)['ok'], (extra, done.stdout, done.stderr)
            seen = json.loads((core.ROOT / 'seen-env.json').read_text(encoding='utf-8'))
            assert seen == {'ANTHROPIC_API_KEY': None, 'ANTHROPIC_AUTH_TOKEN': None}, seen
            # Settings that choose an API key helper stop it before the model too.
            (config / 'settings.json').write_text(json.dumps({'apiKeyHelper': 'echo key'}), encoding='utf-8')
            with patch.dict(os.environ, env):
                refused = growth.run(argv)
            assert refused['state'] == 'unavailable' and 'API' in refused['error'], refused
        """)

    def test_empty_inventory_does_not_launch(self):
        self.check_case("""
            from unittest.mock import patch
            with patch('osk.growth.subprocess.Popen', side_effect=AssertionError('launched')):
                result = growth.run(['unused-command'])
            assert result == {'ok':True, 'state':'skipped', 'selected':0}, result
            assert not growth.LEDGER.exists()
        """)

    def test_command_check_rejects_missing_binary_without_launch_or_writes(self):
        self.check_case("""
            from unittest.mock import patch
            import os
            program = Path('bin') / ('worker.exe' if os.name == 'nt' else 'worker')
            program.parent.mkdir()
            program.write_text('fixture executable')
            program.chmod(0o755)
            with patch.dict(os.environ, {'PATH':'bin'}):
                found = growth.check_command([program.name])
                # Contract: absolute, symlinks kept; POSIX searches from the vault,
                # Windows from the caller's cwd (spelled as the caller spells it).
                base = core.ROOT if os.name == 'posix' else Path.cwd()
                assert found['executable'] == str(base / program), found
            before = {str(p):p.read_bytes() for p in core.ROOT.rglob('*') if p.is_file()}
            with patch('osk.growth.subprocess.Popen', side_effect=AssertionError('launched')):
                assert growth.check_command([sys.executable,'--version'])['ok']
                missing = growth.check_command([str(core.ROOT/'retired-version/codex.exe')])
                assert not missing['ok'] and missing['state'] == 'invalid_command', missing
                assert not growth.check_command([str(core.ROOT)])['ok']
                for bad in ([], 'codex', ['codex', 'bad' + chr(0)], [''], ['', '--tools']):
                    try:
                        growth.check_command(bad)
                        raise AssertionError('invalid argv accepted')
                    except ValueError:
                        pass
            after = {str(p):p.read_bytes() for p in core.ROOT.rglob('*') if p.is_file()}
            assert before == after
            node('A')
            rejected = growth.run([str(core.ROOT/'retired-version/codex.exe')])
            assert rejected['state'] == 'invalid_command', rejected
            assert not growth.LEDGER.exists()
        """)

    @unittest.skipUnless(os.name == 'nt', 'only Windows runs a batch file through cmd.exe')
    def test_command_check_refuses_a_batch_cli_that_cmd_would_split(self):
        # An npm install puts `claude.cmd` on PATH. cmd.exe reads list2cmdline's `\"` as a quote
        # toggle, so the MCP config JSON leaves the vault path outside quotes and `&` splits it.
        self.check_case("""
            import subprocess
            from osk import harness
            probe = core.ROOT / 'probe.py'
            probe.write_text('import json, sys; print(json.dumps(sys.argv[1:]))', encoding='utf-8')
            cli = core.ROOT / 'claude.cmd'
            cli.write_text('@"' + sys.executable + '" "' + str(probe) + '" %*' + chr(13) + chr(10), encoding='utf-8')
            for root in ('C:/vault', 'C:/My Vault (x86)', 'C:/R&D/vault', 'C:/My R&D/vault'):
                argv = harness.get('claude').growth_argv(str(cli), root + '/.venv/Scripts/python.exe',
                                                         root + '/_governance/_engine/mcp_server.py', root)
                seen = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
                intact = seen.returncode == 0 and json.loads(seen.stdout) == argv[1:]
                assert intact == ('&' not in root), (root, seen.stdout, seen.stderr)  # cmd's own verdict
                checked = growth.check_command(argv)
                assert checked['ok'] == intact, (root, checked)
                assert intact or 'cmd.exe' in checked['violations'][0], checked
            # The daily run refuses before it plans or starts anything.
            node('A')
            refused = growth.run(argv)
            assert refused['state'] == 'invalid_command' and not growth.LEDGER.exists(), refused
        """)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink venv execution')
    def test_command_check_preserves_posix_venv_entrypoint(self):
        self.check_case("""
            import subprocess, venv
            venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=True).create(core.ROOT / '.venv')
            executable = core.ROOT / '.venv/bin/python'
            assert executable.is_symlink()
            site = subprocess.run([str(executable), '-c',
                "import sysconfig; print(sysconfig.get_path('purelib'))"],
                capture_output=True, text=True, check=True).stdout.strip()
            (Path(site) / 'venv_probe.py').write_text("VALUE = 'venv-only'")
            # system_site_packages는 기반 인터프리터만 본다 — 수트가 venv에서 돌면
            # 워커가 yaml을 못 찾는다. 수트 쪽 site 경로를 그대로 이어 준다.
            (Path(site) / 'suite_site.pth').write_text(chr(10).join(
                p for p in sys.path if p.endswith(('site-packages', 'dist-packages'))))
            probe = "import json,sys,venv_probe; print(json.dumps([sys.prefix,venv_probe.VALUE]))"
            expected = [str(core.ROOT / '.venv'), 'venv-only']
            original = subprocess.run([str(executable), '-c', probe], cwd=core.ROOT,
                                      capture_output=True, text=True, check=True)
            assert json.loads(original.stdout) == expected
            for entry in (str(executable), '.venv/bin/python'):
                checked = growth.check_command([entry])
                observed = subprocess.run([checked['executable'], '-c', probe], cwd=core.ROOT,
                                          capture_output=True, text=True)
                assert observed.returncode == 0, observed.stderr
                assert json.loads(observed.stdout) == expected
                assert checked['executable'] == str(executable)
            node('A')
            result = growth.run([str(executable), '-c', packet_worker(
                "import venv_probe; assert venv_probe.VALUE == 'venv-only'")], limit=1)
            assert result['ok'], (result, (core.ROOT / result['output'] / 'stderr.txt').read_text())
        """)

    @unittest.skipIf(os.name == 'nt', 'POSIX exec searches PATH after cwd')
    def test_command_check_uses_posix_execution_path(self):
        self.check_case("""
            import os, subprocess, tempfile
            from unittest.mock import patch
            def worker(path, label):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!' + sys.executable + chr(10) + 'print(' + repr(label) + ')' + chr(10))
                path.chmod(0o755)
            worker(core.ROOT / 'worker', 'vault-root')
            worker(core.ROOT / 'bin/worker', 'vault-bin')
            worker(core.ROOT / 'bin/vault-only', 'vault-only')
            with tempfile.TemporaryDirectory(dir=core.ROOT.parent) as directory:
                caller = Path(directory)
                worker(caller / 'worker', 'caller-root')
                worker(caller / 'bin/worker', 'caller-bin')
                worker(caller / 'bin/caller-only', 'caller-only')
                os.chdir(caller)
                try:
                    for path, label in (('bin','vault-bin'), ('','vault-root'),
                                        (':bin','vault-root'), ('missing:bin','vault-bin'),
                                        (str(core.ROOT / 'bin'),'vault-bin')):
                        with patch.dict(os.environ, {'PATH':path}):
                            original = subprocess.run(['worker'], cwd=core.ROOT,
                                capture_output=True, text=True, check=True)
                            assert original.stdout.strip() == label
                            checked = growth.check_command(['worker'])
                            assert checked['ok'], checked
                            actual = subprocess.run([checked['executable']], cwd=core.ROOT,
                                capture_output=True, text=True, check=True)
                            assert actual.stdout == original.stdout, (path, checked, actual.stdout)
                            assert Path.cwd() == caller
                    with patch.dict(os.environ, {'PATH':'bin'}):
                        assert growth.check_command(['vault-only'])['ok']
                        assert not growth.check_command(['caller-only'])['ok']
                finally:
                    os.chdir(core.ROOT)
        """)

    def test_worker_is_told_the_runs_model_as_drafter(self):
        # Codex workers guessed their model on all 88 nodes of the 2026-10-05 batch.
        self.check_case("""
            assert growth._drafter(['codex', 'exec', '--model', 'gpt-5.6-sol', '-']) == 'gpt-5.6-sol'
            assert growth._drafter(['codex', 'exec', '-m', 'gpt-5.6-sol', '-']) == 'gpt-5.6-sol'
            assert growth._drafter(['claude', '-p', '--model', 'claude-opus-5-5', '--verbose']) is None
            assert growth._drafter(['codex', 'exec', '--model', 'Not A Name', '-']) is None
            assert growth._drafter(['codex', 'exec', '-']) is None
            node('A')
            result = growth.run([sys.executable, '-c', packet_worker(), '--model', 'gpt-5.6-sol'], limit=2)
            assert result['ok'], result
            text = (core.ROOT / result['output'] / 'prompt.txt').read_text(encoding='utf-8')
            assert 'Every create_node uses drafter "gpt-5.6-sol"' in text, text[:600]
            assert 'not notes about this review' in text and 'such as -2' in text
            assert [r for r in core.ledger_read(growth.LEDGER) if r['kind'] == 'plan'][-1]['drafter'] == 'gpt-5.6-sol'
        """)

    def test_reviews_keep_current_claims_and_bind_existing_ones_by_evidence_line(self):
        # 2026-10-05: an old round must not roll back a newer claim, and a round that only
        # supports a held claim gets a `## 근거` line instead of a rewritten body.
        self.check_case("""
            from osk import integration
            node('A')
            worker = growth.prompt(growth.plan(3))
            assert 'never replace, weaken or reorder a current claim' in worker
            assert '"## 근거" section' in worker and '42cf0fc0#1' in worker
            path = core.ROOT / 'held.jsonl'
            rows = [{'type':'user','sessionId':'held','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'held','uuid':'a1','message':{'role':'assistant','id':'m1',
                     'content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r) + '\\n' for r in rows), encoding='utf-8')
            assert integration.capture('claude','held',str(path),'held-project',space='00_Scope/W1')['ok']
            own = integration.prompt('claude','held')['text']
            assert '현행 주장을 옛 내용으로 바꾸거나 약화·재배열하지 않는다' in own
            assert '`## 근거` 절' in own and '`42cf0fc0#1`' in own
        """)

    def test_worker_hooks_do_not_feed_maintenance_into_new_conversations(self):
        self.check_case("""
            node('A')
            change = "import os,subprocess; assert os.environ.get('OSK_GROWTH_WORKER')=='1'; hooks=Path(growth.__file__).resolve().parents[1]/'scripts/hooks'; results=[subprocess.run([sys.executable,'-B',str(hooks/name)],input=b'{}',capture_output=True,timeout=20) for name in ('claude_session_start.py','claude_prompt_submit.py','capture_stop.py')]; assert all(r.returncode==0 and not r.stdout and not r.stderr for r in results),results"
            result = growth.run([sys.executable,'-c',packet_worker('from pathlib import Path; '+change)],limit=2)
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
            # A decision on the selected sources is the reviewer's report and is kept, but it does
            # not complete a comparison whose sources changed; those form a new comparison.
            for outcome in ('no_value','deferred'):
                growth.review(candidate['key'], outcome, reason='Stale', manifest=manifest['rid'])
            assert not growth._completed(candidate['key'], growth._records(), graph.Index())
            assert candidate['key'] not in {c['key'] for c in growth.plan()['candidates']}
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
            worker += "; from osk import organization; [organization.review(j['key'],j['scope'],'complete','The fixture group remains coherent.',after=organization.snapshot(j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in plans[-1]['organization_jobs']]"
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
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
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
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            receipt = growth.review(candidate['key'],'preserved',target='Shared rule',
                reason='A and B support the rule; Noise and Other are unrelated and omitted.',manifest=manifest['rid'])
            actual = contract.parse(core.ROOT / '00_Domain/Principles/Shared rule.md').edges('derived-from')
            assert set(actual) == {s['name'] for s in selected}, actual
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
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
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
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            next_candidate = growth.plan(1)['candidates'][0]
            assert next_candidate['previous_distillations'][0]['key'] == candidate['distill_key']
            worker = "import sys; from osk import core,growth,organization; sys.stdin.read(); p=[r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]; [growth.review(c['key'],'preserved',target='Retained rule',reason='Existing saved result still covers A and B.',manifest=p['rid']) for c in p['candidates']]; [organization.review(j['key'],j['scope'],'complete','Sources and local hub read; one coherent group.',after=organization.snapshot(j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in p['organization_jobs']]"
            outcome = growth.run([sys.executable,'-c',worker],limit=2)
            assert outcome['ok'], outcome
            assert len([1 for p,k in graph.Index().nodes.values() if k[0]=='domain' and not graph.is_hub(p)]) == 1
        """)

    def test_short_finished_transcript_waits_for_its_own_fork(self):
        self.check_case("""
            from osk import integration
            path = core.ROOT / 'native.jsonl'
            user = {'type':'user','sessionId':'short','uuid':'u1','message':{'role':'user','content':'Report this temporary completed job.'}}
            final = {'type':'assistant','sessionId':'short','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'Temporary job finished.'}],'stop_reason':'end_turn'}}
            path.write_text(json.dumps(user)+'\\n',encoding='utf-8')
            state = integration.capture('claude','short',str(path),'short-project',space='00_Scope/W1')
            assert state['capture_pending'], state
            with path.open('a',encoding='utf-8') as f:
                f.write(json.dumps(final)+'\\n')
            # The daily run tracks the finished turn but has no copy of it to review.
            assert growth.run(['unused-command'])['state'] == 'skipped'
            assert integration.status('claude','short')['captured_rounds'] == 1
            # The conversation's own Stop fork holds the turn and reviews it.
            job = integration.prompt('claude','short',include_organization=False)
            for change in (
                    "q['osk_reviews']['scope'][0]['through']='stale-snapshot'",
                    "q['osk_reviews']['scope'][0].update(outcome='preserved',targets=[{'key':'missing-proof'}])"):
                rejected = growth.run([sys.executable,'-c',packet_worker(change)],scope_job=job)
                assert not rejected['ok'] and rejected['final_reviews']['errors'], rejected
            worker = packet_worker()
            result = growth.run([sys.executable,'-c',worker],scope_job=job)
            assert result['ok'], result
            assert result['scope_selected'] == 1 and result['domain_selected'] == 0, result
            assert {r['status'] for r in result['scope_outcomes'].values()} == {'complete'}
            assert growth.run(['unused-command'])['state'] == 'skipped'
        """)

    def test_daily_run_reads_an_ended_conversation_and_restarts_its_cadence(self):
        self.check_case("""
            import os, time
            from osk import integration
            path = core.ROOT / 'ended.jsonl'
            rows = []
            for i in range(1, 4):
                rows += [{'type':'user','sessionId':'ended','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'ended','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert integration.capture('claude','ended',str(path),'ended-project',space='00_Scope/W1')['ok']
            for _ in range(5):
                integration.tick('claude','ended')
            # Still running: its turns stay with the conversation's own review cadence.
            assert growth.run(['unused-command'])['state'] == 'skipped'
            old = time.time() - integration.ENDED_AFTER - 60
            os.utime(path, (old, old))
            result = growth.run([sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['scope_selected'] == 1, result
            plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
            job = growth._reading_plan(plan)['scope_jobs'][0]
            assert job['pending_refs'] == ['native:claude:ended:u' + str(i) for i in (1, 2, 3)], job
            assert 'read_command' not in job, job
            assert 'read_cited(ref=' in growth.prompt(plan), 'a sandboxed worker reads through MCP'
            state = integration.status('claude','ended')
            assert (state['reviewed_rounds'], state['pending']) == (3, False), state
            assert state['prompt_count'] == state['reviewed_prompt_count'] == 5, state
        """)

    def test_organization_packet_without_snapshot_refuses_a_unit_changed_after_reading(self):
        self.check_case("""
            node('A')
            assert 'Use osk MCP tools only' in growth.prompt(growth.plan(3))
            assert 'growth checkpoint --file' not in growth.prompt(growth.plan(3))
            # The worker has no CLI snapshot. A unit it read that changes before the
            # runner applies the packet no longer matches its content key.
            change = ("from osk import write; n=core.ROOT/'00_Scope/W1/A.md'; "
                      "assert write.update_node('A',body='Changed after the worker read it.',expect_hash=core.sha256_file(n))['ok']")
            stale = growth.run([sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed review unit' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run([sys.executable,'-c',packet_worker()],limit=3)
            assert fresh['ok'], fresh
            assert {s['status'] for s in fresh['organization_outcomes'].values()} == {'complete'}, fresh
        """)

    def test_organization_packet_is_held_to_the_snapshot_in_its_plan(self):
        self.check_case("""
            node('A')
            node('B')
            # Another session swaps A's evidence after the plan. Unit keys cover bodies only,
            # so the planned snapshot is what tells the reviewed state from the current one.
            change = "from osk import write; assert write.update_node('A',add_edges={'derived-from':'B'})['ok']"
            stale = growth.run([sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed after inspection' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run([sys.executable,'-c',packet_worker()],limit=3)
            assert fresh['ok'], fresh
            assert {s['status'] for s in fresh['organization_outcomes'].values()} == {'complete'}, fresh
        """)

    def test_organization_packet_keeps_units_read_before_its_own_edit(self):
        self.check_case("""
            node('A')
            node('B')
            # The worker reads A, corrects B and checkpoints A as deferred. Its own write moves
            # the scope past the planned snapshot; A's unchanged unit is still its progress and
            # B goes to another reviewer (#136 M4: the packet was refused and nothing was kept).
            change = ("from osk import write; n=core.ROOT/'00_Scope/W1/B.md'; "
                      "assert write.update_node('B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run([sys.executable,'-c',packet_worker(change, org='deferred', read="u['name']=='A'")],
                                limit=3)
            # B is also a Domain candidate's source, so that candidate is refused; only the
            # organization checkpoint is under test here.
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert {s['status'] for s in result['organization_outcomes'].values()} != {'complete'}, result
            from osk import organization
            nxt = organization.plan('W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
            assert nxt.get('handoff') or nxt.get('coverage', {}).get('handoff'), nxt
        """)
        self.check_case("""
            node('A')
            node('B')
            # The worker also lists B, which it then corrected. B's judgment was of the old body,
            # so only A is kept; B is named back and goes to another reviewer.
            change = ("from osk import write; n=core.ROOT/'00_Scope/W1/B.md'; "
                      "assert write.update_node('B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run([sys.executable,'-c',packet_worker(change, org='deferred')], limit=3)
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert any('changed after reading' in n for n in result['final_reviews']['notes']), result
            from osk import organization
            nxt = organization.plan('W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
        """)

    def test_prompt_units_carry_the_view_hash_read_node_returns(self):
        self.check_case("""
            # The worker sees only this prompt. On 2026-10-04 it compared view_hash with the sha256
            # inside each unit (a range content key) and deferred every range it had read.
            import mcp_server as M
            node('A')
            node('B')
            text = growth.prompt(growth.plan(3))
            assert organization.guidance() in text and 'expect_view_hash' in organization.guidance()
            units = [u for j in json.loads(text.rsplit(chr(10), 1)[1])['organization_jobs'] for u in j['review_units']]
            assert units
            for u in units:
                got = M.read_node(name=u['id'], view=u['view'])['view_hash']
                assert u['expect_view_hash'] == got, (u, got)
                assert u['unit'].split(':')[2] not in got, u
        """)

    def test_final_packet_applies_only_observed_domain_preservation(self):
        self.check_case("""
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
            try:
                write.create_node(**request)
            except write.WriteError:
                pass
            assert write.create_node(**request)['ok']
            change = "c=p['candidates'][0]; result=distillation.create_node({'key':c['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in c['sources']],'hub':'Principles'},title='Observed rule',summary='Rule from A and B',body='A and B support this bounded rule.',drafter='gpt-6-astra',space='00_Domain/Principles'); assert result['ok'],result; q['osk_reviews']['domain'][0].update(outcome='preserved',target='Observed rule',reason='A and B support this bounded rule.')"
            result = growth.run([sys.executable,'-c',packet_worker(change)],limit=2)
            assert result['ok'], result
            assert set(result['domain_outcomes'].values()) == {'preserved'}, result
            assert result['final_reviews']['state'] == 'applied', result
            # Newly created Domain knowledge is pending its own organization
            # review; the Scope review cannot acknowledge it implicitly.
            pending = growth.plan()['organization_jobs']
            assert [j['scope'] for j in pending] == ['00_Domain/Principles'], pending
            reviewed = growth.run([sys.executable,'-c',packet_worker()],limit=1)
            assert reviewed['ok'], reviewed
            assert growth.run(['unused-command'])['state'] == 'skipped'
        """)

    def test_final_provider_messages_accept_codex_and_claude(self):
        self.check_case("""
            for wrapper in ('codex','claude','claude_trailing','claude_two_turns'):
                node(wrapper)
                result = growth.run([sys.executable,'-c',packet_worker(wrapper=wrapper)],limit=1)
                assert result['ok'], result
                assert result['final_reviews']['state'] == 'applied', result
        """)

    def test_tool_output_unfinished_and_failed_provider_packets_are_ignored(self):
        self.check_case("""
            node('A')
            for wrapper in ('codex_tool','codex_no_complete','codex_late_error','claude_tool','claude_error','claude_continued'):
                result = growth.run([sys.executable,'-c',packet_worker(wrapper=wrapper)])
                assert not result['ok'], (wrapper,result)
                assert result['final_reviews']['state'] == 'rejected', (wrapper,result)
                assert set(result['domain_outcomes'].values()) == {'pending'}, result
            result = growth.run([sys.executable,'-c',packet_worker(code=7)])
            assert not result['ok'] and result['final_reviews']['state'] == 'not_applied', result
            assert not [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
        """)

    def test_a_wrong_packet_is_refused_whole_and_a_wrong_entry_only_itself(self):
        self.check_case("""
            node('A')
            result = growth.run([sys.executable,'-c',packet_worker("q['osk_reviews']['manifest']='old-manifest'")])
            assert not result['ok'] and result['final_reviews']['state'] == 'rejected', result
            assert not [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
        """)
        for change, kept in (
                ("q['osk_reviews']['domain'][0]['key']='unselected'", False),
                ("q['osk_reviews']['domain'].append(q['osk_reviews']['domain'][0])", False),
                ("q['osk_reviews']['domain'][0]['command']='must never execute'", False),
                ("q['osk_reviews']['scope']=[{'harness':'claude','conversation_id':'unknown',"
                 "'through':'unknown','outcome':'no_value','reason':'Spoof'}]", True)):
            # The packet is right and one entry is not: only that entry is set aside with its
            # reason, and the other decisions in the packet still apply (헌법 1조 3항).
            self.check_case(f"""
                node('A')
                result = growth.run([sys.executable,'-c',packet_worker({change!r})])
                assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
                assert result['final_reviews']['errors'], result
                assert {{s['status'] for s in result['organization_outcomes'].values()}} == {{'complete'}}, result
                reviews = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']
                assert bool(reviews) == {kept}, reviews
            """)
        # A decision with a needless part (a target on no_value) keeps its meaning; the part is
        # dropped and named in the notes.
        self.check_case("""
            node('A')
            result = growth.run([sys.executable,'-c',packet_worker("q['osk_reviews']['domain'][0]['target']='Whatever'")])
            assert result['ok'], result
            assert any('target was dropped' in n for n in result['final_reviews']['notes']), result
            assert [r['outcome'] for r in core.ledger_read(growth.LEDGER) if r['kind']=='review'] == ['no_value']
        """)

    def test_a_report_from_an_older_selection_does_not_undo_a_newer_one(self):
        self.check_case("""
            node('A')
            old, new = register(), register()        # two selections of the same comparison
            key = old['candidates'][0]['key']
            assert key == new['candidates'][0]['key']
            growth.review(key, 'no_value', reason='Decided on the later selection.', manifest=new['rid'])
            assert growth._completed(key, growth._records(), graph.Index())
            # The earlier selection's worker reports late. Its report is kept, but it does not
            # undo the later selection's decision (the growth ledger is causal, so a late append
            # would otherwise become the latest).
            growth.review(key, 'deferred', reason='Late report from the earlier selection.', manifest=old['rid'])
            assert growth._completed(key, growth._records(), graph.Index()), 'an older report reopened it'
            assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']) == 2
            # The same selection can still reopen its own decision on purpose.
            growth.review(key, 'deferred', reason='Reopened within the later selection.', manifest=new['rid'])
            assert not growth._completed(key, growth._records(), graph.Index())
        """)

    def test_selections_made_apart_stay_undecided_until_a_later_one(self):
        self.check_case("""
            node('A')
            # Two devices select the same comparison before syncing. Neither selection follows
            # the other, so their different reports leave it undecided (Mechanism §3 1) instead
            # of letting the larger RID win. A selection made after the merge decides it.
            base = growth.LEDGER.read_bytes() if growth.LEDGER.exists() else b''
            x = register()
            key = x['candidates'][0]['key']
            growth.review(key, 'deferred', reason='Device X could not decide.', manifest=x['rid'])
            branch = growth.LEDGER.read_bytes()[len(base):]
            growth.LEDGER.write_bytes(base)
            y = register()
            assert y['candidates'][0]['key'] == key and core._rid_key(x['rid']) < core._rid_key(y['rid'])
            growth.review(key, 'no_value', reason='Device Y found nothing to keep.', manifest=y['rid'])
            growth.LEDGER.write_bytes(growth.LEDGER.read_bytes() + branch)    # the sync merge
            rows = growth._records()
            assert growth._latest(rows, key, 'key') is None
            assert not growth._completed(key, rows, graph.Index()), 'the larger RID decided a fork'
            z = register()
            growth.review(key, 'no_value', reason='Decided after the merge.', manifest=z['rid'])
            assert growth._completed(key, growth._records(), graph.Index())
        """)

    def test_a_runner_stopped_while_applying_keeps_what_it_applied(self):
        self.check_case("""
            from unittest.mock import patch
            node('A')
            node('B', 'W2')
            # M2: the runner dies between two decisions of one final packet. The decision it
            # applied stays complete, nothing records the run as done, and the next plan presents
            # only the selection whose decision was not applied.
            class Crash(BaseException):
                pass
            real, calls = growth.review, []
            def review(*a, **kw):
                calls.append(kw.get('key', a[0] if a else None))
                if len(calls) == 2:
                    raise Crash()
                return real(*a, **kw)
            with patch.object(growth, 'review', side_effect=review):
                try:
                    growth.run([sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth.LEDGER)
            reviews = [r for r in rows if r['kind']=='review']
            assert [r['key'] for r in reviews] == calls[:1], (reviews, calls)
            assert not [r for r in rows if r['kind']=='run']
            assert growth._completed(calls[0], growth._records(), graph.Index())
            keys = [c['key'] for c in growth.plan(3)['candidates']]
            assert calls[0] not in keys and calls[1] in keys, (keys, calls)
        """)

    def test_a_runner_stopped_after_applying_does_not_present_its_decisions_again(self):
        self.check_case("""
            from unittest.mock import patch
            node('A')
            # M2: the decisions are applied, then the runner dies before it records the run and
            # replies. Nothing applied is undone or presented again.
            class Crash(BaseException):
                pass
            real = core.ledger_append
            def append(path, record, *a, **kw):
                if record.get('kind') == 'run':
                    raise Crash()
                return real(path, record, *a, **kw)
            with patch.object(core, 'ledger_append', side_effect=append):
                try:
                    growth.run([sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth.LEDGER)
            assert [r for r in rows if r['kind']=='review'] and not [r for r in rows if r['kind']=='run'], rows
            again = growth.plan(3)
            assert not again['candidates'], again['candidates']
            assert not again['organization_jobs'], again['organization_jobs']
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
            assert len([r for r in core.ledger_read(growth.LEDGER) if r['kind']=='review']) == result['domain_selected'] == 2
        """)

    def test_total_budget_and_queue_rotation_do_not_ack_unselected_scope(self):
        self.check_case("""
            from osk import integration
            node('A')
            path = core.ROOT / 'native.jsonl'
            rows = []
            for i in range(7):
                rows += [{'type':'user','sessionId':'bounded','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'bounded','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(row)+'\\n' for row in rows),encoding='utf-8')
            integration.capture('claude','bounded',str(path),'bounded-project',space='00_Scope/W1')
            legacy('bounded', path)
            original = integration.status('claude','bounded')
            seen = []
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']  # no ACKs
            for i in range(3):
                result = growth.run(worker,limit=1)
                assert result['selected'] == 1 and not result['ok'], result
                plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                queue = next(k for k in growth._QUEUES if plan[k])
                seen.append(queue)
                if plan['scope_jobs']:
                    job = plan['scope_jobs'][0]
                    assert len(job['pending_refs']) == 3 and job['remaining_rounds'] == 4, job
                    assert job['through'] != original['through']
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert 'prompt' not in visible and 'last_review' not in visible
                    assert visible['through'] == job['through'] and visible['pending_refs'] == job['pending_refs']
                    job['last_review'] = {'through':job['through'], 'outcome':'deferred', 'reason':'Search CACHE_LIMIT retest first.'}
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert visible['previous_deferral']['reason'] == job['last_review']['reason']
            assert seen == ['candidates', 'scope_jobs', 'organization_jobs'], seen
            assert integration.status('claude','bounded')['pending_refs'] == original['pending_refs']
            result = growth.run([sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['selected'] == 3, result
            state = integration.status('claude','bounded')
            assert len(state['pending_refs']) == 4, state  # selected three only, not the whole conversation
        """)

    def test_scope_queue_goes_by_turn_time_and_passes_a_stalled_conversation(self):
        self.check_case("""
            import os, time
            from osk import integration
            node('A')
            # Two ended conversations compete for one Scope slot per run (limit=3 shares the
            # budget with the candidate and organization queues). 'early' holds the older turns
            # although its state was touched last, so time order picks it first. A run that ends
            # without an ACK leaves it in place; the next run gives 'late' its turn instead of
            # choosing 'early' again (#136 M4: six such runs all chose the first conversation).
            ended = time.time() - integration.ENDED_AFTER - 60
            for n, (sid, day) in enumerate((('late', '02'), ('early', '01'))):
                path = core.ROOT / (sid + '.jsonl')
                rows = []
                for i in range(1, 3):
                    rows += [{'type':'user','sessionId':sid,'uuid':sid+'-u'+str(i),'timestamp':'2026-10-'+day+'T0'+str(i)+':00:00Z','message':{'role':'user','content':'question '+str(i)}},
                             {'type':'assistant','sessionId':sid,'uuid':sid+'-a'+str(i),'message':{'role':'assistant','id':sid+'-m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert integration.capture('claude',sid,str(path),sid+'-project',space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
                os.utime(integration.state_path('claude', sid), (ended + n, ended + n))
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']   # no ACKs
            picks = []
            for _ in range(3):
                result = growth.run(worker, limit=3)
                assert not result['ok'], result
                plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                picks.append(plan['scope_jobs'][0]['conversation_id'])
            assert picks == ['early', 'late', 'early'], picks
        """)

    def test_conversations_that_yield_no_job_do_not_hold_the_catch_up_slots(self):
        self.check_case("""
            import os, time
            from osk import integration
            os.environ['CODEX_HOME'] = str(core.ROOT / 'codex-home')    # never the real sessions
            # Codex conversations whose original transcripts are gone still date their next turn
            # from the stored UUIDv7 turn ids, so they sort first, yet catch-up cannot build jobs
            # from them. Catch-up walks the whole sorted queue to the next conversation that yields
            # a job: neither the run limit nor a lookup cap lets them hold the slots (#152 review:
            # with nothing else queued, run after run reviewed nothing and B waited).
            ended = time.time() - integration.ENDED_AFTER - 60
            path = core.ROOT / 'b.jsonl'
            rows = [{'type':'user','sessionId':'b','uuid':'b-u1','timestamp':'2026-10-02T01:00:00Z','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'b','uuid':'b-a1','message':{'role':'assistant','id':'b-m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert integration.capture('claude','b',str(path),'b-project',space='00_Scope/W1')['ok']
            os.utime(path, (ended, ended))
            for i in range(101):
                sid, turn = 'lost' + str(i), '01900000-0000-7000-8000-%012d' % i
                with integration._locked('codex', sid) as p:
                    s = integration._load(p, 'codex', sid)
                    s.update(session='lost-project', space='00_Scope/W1', prompt_count=1,
                             transcript_path=str(core.ROOT / 'gone.jsonl'),
                             rounds=[{'id':sid+'-u1:'+turn, 'ref':'native:codex:'+sid+':'+turn,
                                      'hash':'sha256:' + '0'*64, 'completion':'completed'}])
                    integration._save(p, s)
            first = [x['conversation_id'] for x in integration._known_pending(100)[0]]
            assert len(first) == 100 and 'b' not in first, first[-3:]
            result = growth.run([sys.executable,'-c','import sys; sys.stdin.read()'], limit=1)
            assert result.get('state') != 'capture_pending', result
            plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in plan['scope_jobs']] == ['b'], plan['scope_jobs']
            assert integration.status('codex','lost0')['capture_error'], 'the failure stays visible'
        """)

    def test_conversation_review_carries_its_scope_hub_tree(self):
        self.check_case("""
            from osk import integration, organization
            node('A')
            # Mechanism §9-4 3: the conversation review decides node splitting and placement and
            # carries its scope's hub tree; hub differentiation is left to the organization review.
            path = core.ROOT / 'native.jsonl'
            rows = [{'type':'user','sessionId':'tree','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'tree','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert integration.capture('claude','tree',str(path),'tree-project',space='00_Scope/W1')['ok']
            job = integration.prompt('claude','tree')
            tree = organization.hub_tree(organization._scope_path('W1'))
            assert job['hub_tree'] == tree and tree.startswith('- 00_Scope/W1 · 직속 노드 '), job['hub_tree']
            assert '이 scope의 허브 트리:' in job['text'] and tree in job['text']
            assert '하위 군집을 열지 않는다' in job['text']
            reading = growth._reading_plan({'scope_jobs': [job], 'organization_jobs': []})
            assert reading['scope_jobs'][0]['hub_tree'] == tree
            assert 'hub_tree' in growth.prompt()
        """)

    def test_every_queue_and_its_later_candidate_get_turns_while_nothing_completes(self):
        self.check_case("""
            import os, time
            from unittest.mock import patch
            from osk import evictions, integration
            # #136 M4 fixture: two candidates in each of the five queues and a worker that never
            # acknowledges or submits a packet. Within five runs of limit=3 every queue is chosen,
            # and so is the second candidate of every queue: a stalled first one holds no budget.
            node('A')
            node('B', 'W2')
            for title in ('R1', 'R2'):
                assert write.create_node(title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node('A', old_text='A reusable observation', new_text='A revised observation')
            for i in range(2):
                evictions.record_evict('W1', 'session', 'Temporary observation ' + str(i))
            ended = time.time() - integration.ENDED_AFTER - 60
            for sid in ('c1', 'c2'):
                path = core.ROOT / (sid + '.jsonl')
                rows = [{'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'question'}},
                        {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert integration.capture('claude', sid, str(path), sid + '-project', space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
            ident = {'candidates': 'key', 'scope_jobs': 'conversation_id', 'organization_jobs': 'scope',
                     'eviction_jobs': 'of', 'recheck_jobs': 'key'}
            assert set(ident) == set(growth._QUEUES)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']    # never acknowledges
            seen = {k: [] for k in ident}
            with patch.object(evictions, 'age_days', return_value=17):
                planned = growth.plan(3)       # Scope jobs join at run time, from the conversations
                assert all(len(planned[k]) >= 2 for k in ident if k != 'scope_jobs'), planned
                assert len(integration._known_pending(3)[0]) == 2
                for _ in range(5):
                    result = growth.run(worker, limit=3)
                    assert not result['ok'] and result['selected'] == 3, result
                    plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                    for k, field in ident.items():
                        seen[k] += [j[field] for j in plan[k]]
            assert all(len(set(v)) >= 2 for v in seen.values()), seen
        """)

    def test_only_dispatched_organization_jobs_advance_attempts(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import integration
            for name, scope in (('A','W1'),('B','W2'),('C','W3')):
                node(name, scope)
            path = core.ROOT / 'native.jsonl'
            rows = [
                {'type':'user','sessionId':'waiting','uuid':'u1','message':{'role':'user','content':'Keep this scoped fact.'}},
                {'type':'assistant','sessionId':'waiting','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'A bounded fact.'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows),encoding='utf-8')
            assert integration.capture('claude','waiting',str(path),'waiting',space='00_Scope/W1')['ok']
            legacy('waiting', path)
            worker = "import sys; from osk import core,growth,organization; sys.stdin.read(); p=[r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]; [organization.review(j['key'],j['scope'],'deferred','Needs a later targeted review.') for j in p['organization_jobs']]"
            visited = []
            for i in range(8):
                before = organization._load()['plans']
                # Inventories and previews must never create/refresh attempts.
                growth.plan(3)
                assert organization._load()['plans'] == before
                with patch.object(core, 'now_kst', return_value=f'2026-09-20T12:00:{i:02d}+09:00'):
                    result = growth.run([sys.executable,'-c',worker],limit=3)
                assert result['selected'] == 3 and not result['ok'], result
                manifest = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                jobs = manifest['organization_jobs']
                assert len(jobs) == 1 and len(manifest['scope_jobs']) == len(manifest['candidates']) == 1, manifest
                selected = jobs[0]
                visited.append(selected['scope'])
                after = organization._load()['plans']
                assert after[selected['key']]['last_attempt'] == f'2026-09-20T12:00:{i:02d}+09:00'
                assert {k:v for k,v in after.items() if v['scope'] != selected['scope']} == {
                    k:v for k,v in before.items() if v['scope'] != selected['scope']}, (before, after)
                assert organization._load()['reviews'][selected['scope']]['outcome'] == 'deferred'
            assert visited == ['W1','W2','W3','W1','W2','W3','W1','W2'], visited
            assert integration.status('claude','waiting')['reviewed_rounds'] == 0
        """)

    def test_aged_evictions_get_bounded_slots_and_explicit_decisions(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            node('A')
            items = [evictions.record_evict('W1', 'session', 'Temporary observation '+str(i)) for i in range(3)]
            assert not growth.plan()['eviction_jobs']  # Young entries retain the session-hook path.
            with patch.object(evictions, 'age_days', return_value=17):
                preview = growth.plan(1)
                assert preview['eviction_jobs'][0]['of'] == items[0]['rid']
                worker = [sys.executable, '-B', '-c', 'import sys; sys.stdin.read()']
                selected = []
                for _ in range(3):
                    result = growth.run(worker, limit=1)
                    assert result['selected'] == 1 and not result['ok'], result
                    manifest = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
                    selected.extend(manifest['eviction_jobs'])
                assert len(selected) == 1 and selected[0]['of'] == items[0]['rid'], selected
                assert growth.plan(1)['eviction_jobs'][0]['of'] == items[1]['rid']  # Unattempted work progresses.
                assert len(evictions.unsettled()) == 3
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':items[0]['rid'],'outcome':'discarded',
                                       'reason':'Only the completed fixture job counter; no durable claim.'}]}}
                assert growth.checkpoint(packet)['ok']
                assert len(evictions.unsettled()) == 2
                assert growth._eviction_status(selected[0])['status'] == 'complete'
                assert growth.checkpoint(packet)['ok']
                assert len([r for r in evictions.records() if r['kind']=='settle']) == 1
                packet['osk_reviews']['eviction'][0]['of'] = items[1]['rid']
                refused = growth.checkpoint(packet)
                assert not refused['ok'] and any('unselected eviction review' in e for e in refused['errors']), refused
                assert len([r for r in evictions.records() if r['kind']=='settle']) == 1
        """)


if __name__ == '__main__':
    unittest.main()
