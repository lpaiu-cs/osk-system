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
from osk import core, graph, growth, validate, write, contract, organization, cluster_gate, node_checks, node_moves, nodefile, growth_ledger, growth_plan, growth_reviews, worker_process, placement, topology
from osk.vault import Vault
VAULT = Vault.open()
validate.make_mini_vault(VAULT, VAULT.root)
def node(title, scope='W1', body=None):
    directory = VAULT.root / '00_Scope' / scope
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / (scope + '.md')).exists():
        original = (VAULT.root / '00_Scope/W1/W1.md').read_text(encoding='utf-8')
        original = original.split(chr(10)+'---'+chr(10),1)[0] + chr(10)+'---'+chr(10)+'# W1'+chr(10)
        (directory / (scope + '.md')).write_text(
            original.replace('260801-zzzz-w1ix', '260801-zzzz-' + scope.lower() + 'ix')
                    .replace('W1', scope), encoding='utf-8')
    result = write.create_node(VAULT, title, title, body or title + ' reusable observation',
                               'gpt-6-astra', space='00_Scope/' + scope)
    assert result['ok'], result
    hub = directory / (scope + '.md')
    old = contract.parse(hub).body
    write.update_node(VAULT, scope, body=old.rstrip() + chr(10) + '- [[' + title + ']]', expect_hash=core.sha256_file(hub))
    return next(s for c in growth_plan.plan(VAULT, 20)['candidates'] for s in c['sources']
                if s['name'] == title)
def legacy(sid, transcript, scope='W1'):
    # Rewrite a fresh cursor as the full-capture engine (<= v4.1) left it: rounds stored
    # in _raw/. The daily worker reviews only these; original turns stay with their conversation.
    from osk import integration, raw, transcripts, conversation_capture, conversation_cite, conversation_review, conversation_state
    path = conversation_state.state_path(VAULT, 'claude', sid)
    s = conversation_state.load_state(VAULT, path, 'claude', sid)
    shown = list(transcripts.read(str(transcript), 'claude', sid)['dialogue_v1'].values())
    record = VAULT.root / '00_Scope' / scope / '_raw' / '.records' / (s['record'] + '.txt')
    blocks = [raw.round_block(i, raw.escape_numeric_h2(r['user']), raw.escape_numeric_h2(r['agent']),
                         dialogue_id=r['id']) for i, r in enumerate(shown, 1)]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(chr(10).join(blocks).encode('utf-8'))
    rel = record.relative_to(VAULT.root).as_posix()
    s['rounds'] = [{'id': r['id'], 'ref': rel + '#' + str(i), 'completion': r['completion'],
                    'hash': core.sha256_bytes(b.rstrip(chr(10)).encode('utf-8'))}
                   for i, (r, b) in enumerate(zip(shown, blocks), 1)]
    s['snapshots'] = {conversation_state.snapshot(s): {'count': len(s['rounds']), 'prompt_count': s['prompt_count']}}
    conversation_state.save_state(path, s)
def register(planned=None):
    planned = growth_plan.plan(VAULT, 20) if planned is None else planned
    with core.mutation_lock(VAULT):
        return core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'plan', **planned})
def no_value_all():
    manifest = register()
    for candidate in manifest['candidates']:
        growth_reviews.review(VAULT, candidate['key'], 'no_value', reason='No reusable synthesis in this exact set.', manifest=manifest['rid'])
def packet_worker(change='', wrapper='plain', code=0, org='complete', read='True'):
    # org: the organization review outcome; read: which review units u the worker read
    source = "import json,sys; from osk import core,growth,distillation,integration, conversation_capture, conversation_cite, conversation_review, conversation_state, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); sys.stdin.read(); p=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]; q={'osk_reviews':{'manifest':p['rid'],'domain':[{'key':c['key'],'outcome':'no_value','reason':'No reusable synthesis in these compared sources.'} for c in p['candidates']],'scope':[dict((k,j[k]) for k in ('harness','conversation_id','through')) | {'outcome':'no_value','reason':'Only a completed one-off job.'} for j in p['scope_jobs']]}}; "
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

# C cites B; B's version was made by a recheck and C's correction went to the user — both recorded
# by an engine that measured the 근거 section (v4.1.8). `V50_CARRY` first carries as v5.0.x did.
UPGRADE_CARRY = """
    from osk import rechecks, raw
    new_state = rechecks.state
    rechecks.state = lambda data, node=True: core.sha256_bytes(data[contract.body_offset(data):] if node else data)
    node('A')
    node('D')
    record = VAULT.root / ('00_Scope/W1/_raw/.records/claude-c38d8ef2' + '0' * 24 + '.txt')
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(raw.round_block(1, 'independent observation', 'observed'), encoding='utf-8')
    evidence = chr(10).join(['', '', '## 근거', '', '- 2026-10-05 · `c38d8ef2#1` · 지지 — an observation'])
    # B also cites D, listed first: every carried row of B says its version was made by a recheck
    for title, body, basis in (('B', 'B relies on A.', ['D', 'A']), ('C', 'C relies on B.', ['B'])):
        assert write.create_node(VAULT, title, title, body + evidence, 'gpt-6-astra', space='00_Scope/W1',
                                 edges={'derived-from': basis})['ok']
    write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
    write.update_node(VAULT, 'B', old_text='B relies on A.', new_text='B relies on revised A.', rechecked=['A'])
    planned = growth_plan.plan(VAULT, 3)
    assert [(j['node'], j['cascade']) for j in planned['recheck_jobs']] == [('C', True)], planned['recheck_jobs']
    manifest = register({**planned, 'scope_jobs': []})
    assert growth_reviews.checkpoint(VAULT, {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
        'recheck': [{'key': planned['recheck_jobs'][0]['key'], 'outcome': 'escalated',
                     'reason': 'A second correction needs the user.', 'proposal': 'Change C later.'}]}})['ok']
    rechecks.state = new_state   # the upgrade
    def held():
        items = rechecks.candidates(VAULT, graph.Index(VAULT))[0]
        assert [(i['node'], i['cascade'], 'escalated' in i) for i in items] == [('C', True, True)], items
        assert not growth_plan.plan(VAULT, 3)['recheck_jobs'], 'the escalated check returned to the agent queue'
        assert [h['node'] for h in rechecks.report(VAULT, graph.Index(VAULT))['escalated']] == ['C']
    held()
    idx = graph.Index(VAULT)
    b = idx.locate('B')[0]
    if V50_CARRY:
        for basis in ('D', 'A'):
            t = idx.locate(basis)[0]
            core.ledger_append(rechecks.ledger_path(VAULT), rechecks._row(
                idx.node(b).id, rechecks.state(b.read_bytes()), idx.node(t).id, rechecks.state(t.read_bytes()),
                'unchanged', rechecks.CARRIED))
        held()
    assert rechecks.ensure_baseline(VAULT, carry=True) == (2 if V50_CARRY else 3), 'cascade and review not both carried'
    results = {r['result'] for r in core.ledger_read(rechecks.ledger_path(VAULT))[-2:] if r['node'] == idx.node(b).id
               and r['reason'] == rechecks.CARRIED and r['node_state'] == rechecks.state(b.read_bytes())}
    assert 'updated' in results and (V50_CARRY or results == {'updated'}), results
    review = core.ledger_read(growth_ledger.ledger_path(VAULT))[-1]
    assert review['kind'] == 'recheck_review' and (review['reason'], review['proposal']) == (
        'A second correction needs the user.', 'Change C later.'), review
    held()
    # Another device carried the same review before the two synced: a sibling row, equal but for its id.
    with open(growth_ledger.ledger_path(VAULT), 'a', encoding='utf-8') as f:
        f.write(json.dumps({**review, 'rid': core._next_rid(review['rid'])}, ensure_ascii=False) + chr(10))
    held()
    # a 근거 section is not a related state — editing the cited node's, then the citing node's
    write.update_node(VAULT, 'B', old_text='— an observation', new_text='— an observation, reworded')
    held()
    write.update_node(VAULT, 'C', old_text='— an observation', new_text='— an observation, reworded')
    held()
    assert rechecks.ensure_baseline(VAULT, carry=True) == 0, 'a carried record was carried again'
    write.update_node(VAULT, 'C', rechecked=['B'])   # the user's decision
    assert not rechecks.report(VAULT, graph.Index(VAULT)), rechecks.report(VAULT, graph.Index(VAULT))
"""

# B's version was made by a recheck, then A changed again: no pair of B is complete or carried, yet
# the cascade C relies on must still move to the current definition (#183 review).
UPGRADE_NO_PAIR = """
    from osk import rechecks, raw
    new_state = rechecks.state
    rechecks.state = lambda data, node=True: core.sha256_bytes(data[contract.body_offset(data):] if node else data)
    node('A')
    record = VAULT.root / ('00_Scope/W1/_raw/.records/claude-c38d8ef2' + '0' * 24 + '.txt')
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(raw.round_block(1, 'independent observation', 'observed'), encoding='utf-8')
    evidence = chr(10).join(['', '', '## 근거', '', '- 2026-10-05 · `c38d8ef2#1` · 지지 — an observation'])
    for title, body, basis in (('B', 'B relies on A.', 'A'), ('C', 'C relies on B.', 'B')):
        assert write.create_node(VAULT, title, title, body + evidence, 'gpt-6-astra', space='00_Scope/W1',
                                 edges={'derived-from': basis})['ok']
    write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
    write.update_node(VAULT, 'B', old_text='B relies on A.', new_text='B relies on revised A.', rechecked=['A'])
    write.update_node(VAULT, 'A', old_text='A revised observation', new_text='A twice revised observation')
    rechecks.state = new_state   # the upgrade
    cascade = lambda: {i['node']: i['cascade'] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]}
    assert cascade() == {'B': False, 'C': True}, cascade()
    assert rechecks.ensure_baseline(VAULT, carry=True) == 1
    write.update_node(VAULT, 'B', old_text='— an observation', new_text='— an observation, reworded')
    assert cascade() == {'B': False, 'C': True}, cascade()
    assert rechecks.ensure_baseline(VAULT, carry=True) == 0
"""


class GrowthTests(unittest.TestCase):
    def test_first_turn_is_fair_when_budget_cannot_select_every_queue(self):
        self.check_case("""
            for limit in (3, 1, 2, 4, 5):
                rows, first = [], []
                for _ in range(8):
                    planned = {key:[{'key':key+'-0'}, {'key':key+'-1'}] for key in growth_ledger.QUEUES}
                    growth_plan.select_work(planned,limit,rows)
                    order = planned['work_order']
                    first.append(order[0]['queue'])
                    assert len(order) == limit
                    assert len({(i['queue'],i['index']) for i in order}) == limit
                    for key in growth_ledger.QUEUES:
                        assert [i['index'] for i in order if i['queue']==key] == list(range(len(planned[key])))
                        assert len(planned[key]) + planned['queued_not_selected'][key] == 2
                    rows.append({'kind':'plan',**planned})
                # Even if each worker stops after its first job, every queue
                # receives a turn within one attempt per queue.
                q = len(growth_ledger.QUEUES)
                assert all(set(first[n:n+q]) == set(growth_ledger.QUEUES) for n in range(9-q)), (limit,first)
        """)

    def test_execution_order_rotates_even_when_every_queue_is_selected(self):
        self.check_case("""
            rows, first, q = [], [], len(growth_ledger.QUEUES)
            for _ in range(2 * q):
                planned = {key:[{'key':key}] for key in growth_ledger.QUEUES}
                growth_plan.select_work(planned,q,rows)
                order = planned['work_order']
                first.append(order[0]['queue'])
                assert len(order) == q and {i['queue'] for i in order} == set(growth_ledger.QUEUES)
                rows.append({'kind':'plan',**planned})
            assert first[:q] == list(growth_ledger.QUEUES), first
            assert first[q:] == first[:q], first
            daily = {key:[{'key':key}] for key in growth_ledger.QUEUES}
            growth_plan.select_work(daily,q,[dict(row,work_context='stop:W1') for row in rows])
            assert daily['work_order'][0]['queue'] == growth_ledger.QUEUES[0], daily
        """)

    def test_eviction_settlement_needs_review_and_deferred_returns_to_queue(self):
        for deferred in (True, False):
            self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            item = evictions.record_evict(VAULT, 'W1', 'session', 'Reusable fact; second claim needs verification.')
            with patch.object(evictions, 'age_days', return_value=17):
                action = "from osk import evictions, write, cluster_gate, node_checks, node_moves, nodefile; j=p['eviction_jobs'][0]; saved=write.create_node(VAULT, 'Partial', 'Partial evidence', 'Only the first claim is verified.', 'fixture', space='00_Scope/W1', settle=j['of']); assert saved['ok']; "
                if DEFERRED_CASE:
                    action += "q['osk_reviews']['eviction']=[{'of':j['of'],'outcome':'deferred','reason':'First claim saved; resume second claim against the source.'}]; assert growth_reviews.checkpoint(VAULT, q)['ok']"
                else:
                    action += "sys.exit(7)"
                result = growth.run(VAULT, [sys.executable, '-B', '-c', packet_worker(action)], limit=1)
                assert not result['ok'], result
                assert result['eviction_outcomes']['eviction:'+item['rid']]['status'] == 'pending', result
                manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                job = manifest['eviction_jobs'][0]
                assert item['rid'] not in {r['rid'] for r in evictions.unsettled(VAULT)}
                again = growth_plan.plan(VAULT)['eviction_jobs']
                assert any(j['of']==item['rid'] for j in again), again
                assert again[0]['previous_settlement']['target'] == 'Partial'
                if DEFERRED_CASE:
                    assert again[0]['previous_deferral']['reason'].startswith('First claim saved')
                # M4: the review is checked against the settlement already recorded. A different
                # disposition or target is set aside and the eviction stays open.
                for outcome, target in (('merged', 'Partial'), ('node', 'Elsewhere')):
                    mismatch = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                                'eviction':[{'of':item['rid'],'outcome':outcome,'target':target,
                                             'reason':'Disagrees with the recorded settlement.'}]}}
                    refused = growth_reviews.checkpoint(VAULT, mismatch)
                    assert not refused['ok'] and any('different disposition' in e for e in refused['errors']), refused
                    assert growth_plan.eviction_status(VAULT, job)['status'] == 'pending'
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':item['rid'],'outcome':'node','target':'Partial',
                                       'reason':'Both claims now checked; second is a one-off value.'}]}}
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert growth_plan.eviction_status(VAULT, job)['status'] == 'complete'
                count = len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='eviction_review'])
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='eviction_review']) == count
                assert not growth_plan.plan(VAULT)['eviction_jobs']
                # A later explicit deferral must supersede even a completed review.
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'deferred','reason':'Reopen a newly disputed condition.'}]
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert growth_plan.eviction_status(VAULT, job)['status'] == 'pending'
                assert growth_plan.plan(VAULT)['eviction_jobs'][0]['of'] == item['rid']
                packet['osk_reviews']['eviction'] = [{'of':item['rid'],'outcome':'node','target':'Partial','reason':'Dispute resolved against the source.'}]
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
            """.replace('DEFERRED_CASE', repr(deferred)))

    def test_cli_checkpoint_survives_later_worker_failure_without_closing_other_jobs(self):
        self.check_case("""
            node('A', 'W1')
            node('B', 'W2')
            change = "from pathlib import Path; import subprocess; q['osk_reviews']['domain']=q['osk_reviews']['domain'][:1]; q['osk_reviews']['scope']=[]; checkpoint=VAULT.root/'checkpoint.json'; checkpoint.write_text(json.dumps(q),encoding='utf-8'); done=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','checkpoint','--file',str(checkpoint)],capture_output=True,text=True); assert done.returncode==0 and json.loads(done.stdout)['ok'],done; sys.exit(7)"
            result = growth.run(VAULT, [sys.executable, '-B', '-c', packet_worker(change)], limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            # M2: the runner applied nothing from this failed worker; the recorded review came
            # through the manual checkpoint, and the run record keeps the two apart.
            assert result['final_reviews']['state'] == 'not_applied', result['final_reviews']
            assert list(result['domain_outcomes'].values()).count('no_value') == 1, result
            assert 'pending' in result['domain_outcomes'].values(), result
            rows = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind'] == 'review']
            assert len(rows) == 1
            packet = json.loads((VAULT.root/'checkpoint.json').read_text(encoding='utf-8'))
            assert growth_reviews.checkpoint(VAULT, packet)['ok']  # Retry is idempotent.
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
            packet['osk_reviews']['domain'][0]['key'] = 'unselected'
            refused = growth_reviews.checkpoint(VAULT, packet)   # only that entry is set aside, with its reason
            assert not refused['ok'] and any('unselected domain review' in e for e in refused['errors']), refused
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
            # The manual path applies decisions only to a plan the runner recorded.
            packet['osk_reviews']['manifest'] = 'not-a-recorded-plan'
            try:
                growth_reviews.checkpoint(VAULT, packet)
                raise AssertionError('a checkpoint applied decisions without a recorded plan')
            except ValueError as e:
                assert 'recorded growth manifest' in str(e), e
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 1
        """)

    def test_scope_checkpoint_survives_worker_failure_and_resumes_only_pending_work(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            for sid in ('first', 'second'):
                path = VAULT.root / (sid + '.jsonl')
                rows = [
                    {'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'fixture question'}},
                    {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{
                        'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'fixture answer'}],
                        'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(row)+'\\n' for row in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude',sid,str(path),'checkpoint-fixture',space='00_Scope/W1')['ok']
                legacy(sid, path)
            change = "q['osk_reviews']['domain']=[]; q['osk_reviews']['scope']=q['osk_reviews']['scope'][:1]; (VAULT.root/'saved-checkpoint.json').write_text(json.dumps(q),encoding='utf-8'); assert growth_reviews.checkpoint(VAULT, q)['ok']; sys.exit(7)"
            result = growth.run(VAULT, [sys.executable,'-B','-c',packet_worker(change)],limit=3)
            assert not result['ok'] and result['returncode'] == 7, result
            first_plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in first_plan['scope_jobs']] == ['first']
            assert conversation_capture.status(VAULT, 'claude','first')['reviewed_rounds'] == 1
            packet = json.loads((VAULT.root/'saved-checkpoint.json').read_text(encoding='utf-8'))
            assert growth_reviews.checkpoint(VAULT, packet)['ok']
            before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'claude','first'),'claude','first')
            assert len(before['reviews']) == 1
            retry = growth.run(VAULT, [sys.executable,'-B','-c','import sys; sys.stdin.read()'],limit=3)
            assert not retry['ok']
            next_plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in next_plan['scope_jobs']] == ['second'], next_plan
            visible = growth._reading_plan(next_plan)['scope_jobs'][0]
            assert visible['raw_review'] == {'state':'verified','rounds':1,'error':None}
            assert visible['capture_recovery']['state'] == 'ready'
            after = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'claude','first'),'claude','first')
            assert after == before
            assert conversation_capture.status(VAULT, 'claude','second')['reviewed_rounds'] == 0
        """)

    def test_recheck_jobs_queue_in_daily_runs_and_fork_scope(self):
        self.check_case("""
            node('A')
            assert write.create_node(VAULT, 'B', 'b', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            planned = growth_plan.plan(VAULT, 3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['target']) for j in jobs] == [('B', '[[A]]')], jobs
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'pending'
            assert 'For recheck_jobs' in growth.prompt(VAULT, planned)
            assert [j['node'] for j in growth_plan._recheck_jobs(VAULT, graph.Index(VAULT), 'W1')] == ['B']
            assert not growth_plan._recheck_jobs(VAULT, graph.Index(VAULT), 'W2')   # a fork keeps to its scope
            assert not growth_ledger.daily_active(VAULT)
            manifest = register(planned)
            assert not growth_ledger.daily_active(VAULT), 'a plan is not completed work'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':manifest['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            write.update_node(VAULT, 'B', add_edges={'derived-from': 'A'})
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'pending', 'wiring is not a check'
            write.update_node(VAULT, 'B', rechecked=['A'])
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'complete'
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs']
        """)

    def test_latest_daily_result_controls_recheck_handoff(self):
        self.check_case("""
            from datetime import datetime, timedelta, timezone
            from unittest.mock import patch
            planned = register()
            assert not growth_ledger.daily_active(VAULT)
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':planned['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'plan','work_context':'stop:W1'})
            assert growth_ledger.daily_active(VAULT), 'Stop work must not replace daily health'
            latest = register()
            assert not growth_ledger.daily_active(VAULT), 'a newer unfinished plan supersedes old success'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':latest['rid'],'ok':False})
            assert not growth_ledger.daily_active(VAULT), 'failed daily work must yield rechecks to forks'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':latest['rid'],'ok':True})
            assert growth_ledger.daily_active(VAULT)
            rows = growth_ledger.ledger_records(VAULT)
            old = (datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
            for row in rows:
                row['at'] = old
            with patch.object(growth_ledger,'ledger_records',return_value=rows):
                assert not growth_ledger.daily_active(VAULT), 'expired success does not suppress fallback'
        """)

    def test_unfinished_organization_is_remaining_work_not_a_failed_run(self):
        self.check_case("""
            import subprocess
            node('A')
            node('B')
            # Organization reads a scope in several passes (Mechanism §9-4 4): a pass that leaves
            # units is progress. After v5.0.0 the scheduler logged every such daily run as failed
            # and the session start said the daily run had not run at all.
            command_file = VAULT.root/'command.json'
            command_file.write_text(json.dumps([sys.executable,'-c',packet_worker(org='deferred')]),
                                    encoding='utf-8')
            proc = subprocess.run([sys.executable,'-B','-m','osk.cli','growth','run',
                                   '--command-file',str(command_file),'--limit','3'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=120)
            result = json.loads(proc.stdout)
            assert result['state'] == 'incomplete' and not result['final_reviews']['errors'], result
            assert {s['status'] for s in result['organization_outcomes'].values()} == {'pending'}, result
            assert proc.returncode == 0 and not growth_ledger.run_failed(result), (proc.returncode, result)
            # #301: an organization left over is no reason to hand rechecks to the fork.
            status = growth_ledger.daily_status(VAULT)
            assert status['ran'] and status['active'], status
            assert status['last']['state'] == 'incomplete', status
            # A run that could not run, or whose worker or packet failed, still fails.
            assert growth_ledger.run_failed({**result, 'returncode': 1})
            assert growth_ledger.run_failed({**result, 'final_reviews': {'state': 'rejected', 'errors': ['no packet']}})
            assert growth_ledger.run_failed({'ok': False, 'state': 'unavailable'})
            assert not growth_ledger.run_failed({'ok': True, 'state': 'skipped', 'selected': 0})
            # Another run holding the lock is a wait, not a failure (#175: 10-05 and 10-06 ended busy).
            assert not growth_ledger.run_failed({'ok': False, 'state': 'busy'})
        """)

    def test_daily_handoff_follows_the_days_recheck_queue(self):
        self.check_case("""
            # #301 / #306 D7: the fork takes rechecks over only when that day's recheck queue was not done —
            # no light batch got its turn, or one left a selected recheck neither closed nor deferred.
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).astimezone().date().isoformat()
            def batch(kind, run=None, rechecks=(), waiting=0, **daily):
                plan = core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'plan', 'work_context': 'daily',
                                                          'daily': {'day': day, 'kind': kind, **daily},
                                                          'recheck_jobs': [{'key': k} for k in rechecks],
                                                          'queued_not_selected': {'recheck_jobs': waiting}})
                if run is not None:
                    core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'returncode': 0, **run})
                return plan
            def fresh():
                growth_ledger.ledger_path(VAULT).unlink(missing_ok=True)
            fresh()
            batch('scope', {'state': 'incomplete', 'ok': False, 'returncode': 1})
            assert not growth_ledger.daily_active(VAULT), 'the day stopped before any recheck batch'
            batch('light', {'state': 'incomplete', 'ok': False,
                            'recheck_outcomes': {'r1': {'status': 'complete'}}}, ['r1'])
            assert growth_ledger.daily_active(VAULT), 'a failed Scope batch and leftover organization do not hand rechecks over'
            batch('light', {'state': 'limited', 'ok': False}, ['r2'])
            assert growth_ledger.daily_active(VAULT), 'a limited batch is a wait — another provider ran it again'
            plan = batch('light', {'state': 'incomplete', 'ok': False,
                                   'recheck_outcomes': {'r3': {'status': 'pending'}}}, ['r3'])
            assert not growth_ledger.daily_active(VAULT), 'a selected recheck left open'
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind': 'recheck_review', 'manifest': plan['rid'], 'key': 'r3',
                                               'outcome': 'deferred', 'reason': 'cannot judge yet'})
            assert growth_ledger.daily_active(VAULT), 'a deferral is a judgement'
            batch('light', {'state': 'incomplete', 'ok': False, 'returncode': 1,
                            'recheck_outcomes': {'r4': {'status': 'complete'}}}, ['r4'])
            assert not growth_ledger.daily_active(VAULT), 'a failed worker judged nothing'
            fresh()
            batch('light')
            assert growth_ledger.daily_status(VAULT)['ran'] and not growth_ledger.daily_active(VAULT), 'a plan without a result'
            # #312 review: other light work took the places, so no recheck was picked though some waited.
            fresh()
            batch('light', {'state': 'complete', 'ok': True}, waiting=2)
            assert not growth_ledger.daily_active(VAULT), 'rechecks waited while other light work took the places'
            batch('light', {'state': 'incomplete', 'ok': False,
                            'recheck_outcomes': {'r5': {'status': 'complete'}}}, ['r5'], waiting=1)
            assert growth_ledger.daily_active(VAULT), 'a later batch of the day gave the rechecks their turn'
            # With no candidate waiting, a light batch without rechecks leaves nothing to hand over.
            fresh()
            batch('light', {'state': 'complete', 'ok': True})
            assert growth_ledger.daily_active(VAULT)
            # A backlog run is not the daily run: its Scope batches neither replace nor date the day.
            batch('scope', {'state': 'incomplete', 'ok': False}, backlog=True)
            assert growth_ledger.daily_active(VAULT) and growth_ledger.daily_status(VAULT)['last']['state'] == 'complete'
        """)

    def test_scope_tries_count_only_what_the_daily_run_judged(self):
        self.check_case("""
            # #306 D4: picks counted waits, failures and fork attempts and never shrank before progress,
            # so conversations tried once by a failing run or a fork stood at the back of the queue.
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            ago = lambda days: (now - timedelta(days=days)).isoformat(timespec='seconds')
            job = {'harness': 'claude', 'conversation_id': 'c', 'reviewed_rounds': 0}
            state = {'harness': 'claude', 'conversation_id': 'c', 'reviewed_count': 0}
            rows = []
            def attempt(run=None, context='daily'):
                rid = 'p' + str(len(rows))
                rows.append({'kind': 'plan', 'rid': rid, 'work_context': context, 'scope_jobs': [job]})
                if run is not None:
                    rows.append({'kind': 'run', 'manifest': rid, 'returncode': 0, 'at': ago(20), **run})
            attempt({'state': 'incomplete'})                       # judged, however old
            attempt({'state': 'limited', 'returncode': 1})         # waited on a limit
            attempt({'state': 'busy'})                             # waited on the lock
            attempt({'state': 'incomplete', 'returncode': 1})      # the worker failed, three weeks ago
            attempt()                                              # no result
            attempt({'state': 'incomplete'}, context='stop:W1')    # a fork's own attempt
            assert growth_plan.scope_tried(rows)(state) == 1
            # A recent failure still counts: a conversation that keeps failing gives others their turn (#136 M4).
            attempt({'state': 'incomplete', 'returncode': 1, 'at': ago(1)})
            assert growth_plan.scope_tried(rows)(state) == 2
        """)

    def test_final_packet_reads_a_trailing_block_not_prose_or_fences(self):
        self.check_case("""
            # #306 D1: four of seven rejected packets were valid JSON after one line of explanation.
            packet = {'osk_reviews': {'manifest': 'm', 'domain': [], 'scope': []}}
            body = json.dumps(packet, indent=2)
            out = VAULT.root / 'stdout.txt'
            def final(text):
                out.write_text(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                           'result': text}) + chr(10), encoding='utf-8')
                return growth_reviews._final_packet(out)
            nl = chr(10)
            assert final(body) == packet
            assert final('Reviewed both jobs.' + nl + body) == packet
            assert final('Reviewed both jobs.' + nl + nl + body + nl) == packet
            for text in ('Here: ' + body,                                   # not at a line start
                         body + nl + 'Done.',                               # prose after it
                         'See' + nl + body + nl + 'and more' + nl,          # in the middle of prose
                         'Result:' + nl + '```json' + nl + body + nl + '```',   # in a code fence
                         'Example:' + nl + '```json' + nl + body):                # in a fence left open
                try:
                    final(text)
                except ValueError:
                    continue
                raise AssertionError(text)
        """)

    def test_daily_status_keeps_a_plan_without_result_as_ran_not_completed(self):
        self.check_case("""
            assert growth_ledger.daily_status(VAULT) == {'ran': False, 'active': False, 'last': None}
            planned = register()
            status = growth_ledger.daily_status(VAULT)
            assert status['ran'] and not status['active'] and status['last']['state'] is None, status
            core.ledger_append(growth_ledger.ledger_path(VAULT), {'kind':'run','manifest':planned['rid'],'ok':True,
                                               'state':'complete'})
            assert growth_ledger.daily_status(VAULT)['active'] and growth_ledger.daily_active(VAULT)
        """)

    def test_recheck_note_tells_an_unfinished_daily_run_from_a_missing_one(self):
        self.check_case("""
            from unittest.mock import patch
            sys.path.insert(0, str(Path(growth.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            class Rechecks:
                @staticmethod
                def candidates(vault):
                    return [{'node': 'B', 'target': '[[A]]'}], False
            ran = {'ran': True, 'active': False,
                   'last': {'at': '2026-10-09T09:00:02+09:00', 'state': 'incomplete'}}
            with patch.object(growth_ledger, 'daily_status', return_value=ran):
                text = hook._recheck_note(VAULT, Rechecks)
            assert '최신 정기 실행(' in text and '다 끝내지 못해' in text, text
            assert '돌지 않아' not in text and 'SETUP' not in text, text
            with patch.object(growth_ledger, 'daily_status', return_value={'ran': False, 'active': False, 'last': None}):
                text = hook._recheck_note(VAULT, Rechecks)
            assert '돌지 않아' in text and 'SETUP' in text, text
        """)

    def test_scope_job_reading_instruction_follows_the_run_context(self):
        self.check_case("""
            # A daily worker took the Stop-fork clause for itself and judged turns it never read
            # (v5.0.0 operation, both daily scope reviews). Each run carries only its own case.
            job = {'harness':'claude','conversation_id':'c','pending_refs':['native:claude:c:t'],
                   'through':'sha256:x','key':'scope-review-x'}
            base = {'candidates':[],'scope_jobs':[job],'organization_jobs':[],'eviction_jobs':[],
                    'recheck_jobs':[]}
            daily = growth.prompt(VAULT, {**base,'work_context':'daily'})
            stop = growth.prompt(VAULT, {**base,'work_context':'stop:W1'})
            assert 'not in your context' in daily and 'read_cited(ref=<that ref>)' in daily, daily
            assert 'Stop fork' not in daily and 'still in your context' not in daily, daily
            assert 'still in your context' in stop and 'read_cited(ref=<that ref>)' not in stop, stop
            # Compaction drops a backlog from the fork's context too: it reads the range once (#226).
            assert 'read_cited(conversation=' in stop and 'through=<its through>' in stop, stop
            assert 'through=<its through>' not in daily, daily
            assert 'not in your context' in growth.prompt(VAULT, base), 'a preview is the daily inventory'
        """)

    def test_unrelated_capture_error_does_not_fail_completed_selected_work(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            from unittest.mock import patch
            node('A')
            catchup = {'ok':False,'jobs':[],'captures':[], 'remaining':1,
                       'errors':[{'conversation_id':'other','error':'native identity mismatch'}]}
            with patch.object(integration,'catchup',return_value=catchup):
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
                assert result['ok'] and result['selected'], result
                assert not result['capture']['ok'] and result['capture']['errors']==catchup['errors']
                assert growth.run(VAULT, ['unused-command'])['state']=='capture_pending'
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u',
                'message':{'role':'user','content':'question'}})+'\\n'+json.dumps(
                {'type':'assistant','sessionId':'own','uuid':'a','message':{'role':'assistant',
                 'id':'m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}})+'\\n',encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','own',str(native),'own',space='00_Scope/W1')['ok']
            job = integration.prompt(VAULT, 'claude','own')
            job.update(ok=False,capture_error='selected conversation capture failed')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],scope_job=job)
            assert not result['ok'], 'the selected conversation capture error remains a blocker'
        """)

    def test_recheck_escalation_holds_a_second_correction_for_the_user(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'B relies on A.', 'A'), ('C', 'C relies on B.', 'B')):
                assert write.create_node(VAULT, title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            jobs = growth_plan.plan(VAULT, 3)['recheck_jobs']
            assert [(j['node'], j['cascade'], j['next']) for j in jobs] == [('B', False, ['C'])], jobs
            # the agent's own correction of B is autonomous; C's recheck is then a cascade
            write.update_node(VAULT, 'B', old_text='B relies on A.', new_text='B relies on revised A.',
                              rechecked=['A'])
            planned = growth_plan.plan(VAULT, 3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['cascade']) for j in jobs] == [('C', True)], jobs
            manifest = register({**planned, 'scope_jobs': []})   # as run() records it
            packet = {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                      'recheck': [{'key': jobs[0]['key'], 'outcome': 'unchanged',
                                   'reason': 'x', 'proposal': 'y'}]}}
            refused = growth_reviews.checkpoint(VAULT, packet)
            assert not refused['ok'] and any('only escalates' in e for e in refused['errors']), refused
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] != 'complete', 'a recheck review closed a check'
            packet['osk_reviews']['recheck'][0]['outcome'] = 'escalated'
            assert growth_reviews.checkpoint(VAULT, packet)['ok']
            assert growth_plan.recheck_status(VAULT, jobs[0], graph.Index(VAULT))['status'] == 'complete'
            assert not growth_plan.plan(VAULT, 3)['recheck_jobs'], 'an escalated check returned to the agent queue'
            held = rechecks.report(VAULT, graph.Index(VAULT)).get('escalated')
            assert [h['node'] for h in held] == ['C'] and held[0]['proposal'] == 'y', held
            write.update_node(VAULT, 'C', rechecked=['B'])   # the user's decision
            assert not rechecks.report(VAULT, graph.Index(VAULT)), rechecks.report(VAULT, graph.Index(VAULT))
        """)

    def test_recheck_deferral_keeps_the_pair_open_and_hands_its_reason_on(self):
        # Bylaws §7 2: a recheck the agent could not judge is not closed; its reason goes to the
        # next recheck of the same node and target.
        self.check_case("""
            from osk import rechecks
            node('A')
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            planned = growth_plan.plan(VAULT, 3)
            job = planned['recheck_jobs'][0]
            assert 'previous_deferral' not in job, job
            manifest = register({**planned, 'scope_jobs': []})
            done = growth_reviews.checkpoint(VAULT, {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                'recheck': [{'key': job['key'], 'outcome': 'deferred',
                             'reason': 'read both; the revision may narrow B', 'proposal': 'drop me'}]}})
            assert done['ok'] and any('carries no proposal' in n for n in done['notes']), done
            row = [r for r in growth_ledger.ledger_records(VAULT) if r.get('kind') == 'recheck_review'][-1]
            assert row['outcome'] == 'deferred' and 'proposal' not in row, row
            assert growth_plan.recheck_status(VAULT, job, graph.Index(VAULT))['status'] == 'pending', 'a deferral closed the check'
            assert not rechecks.report(VAULT, graph.Index(VAULT)).get('escalated'), 'a deferral reached the user'
            again = growth_plan.plan(VAULT, 3)['recheck_jobs']
            assert [j['key'] for j in again] == [job['key']], again
            handed = again[0]['previous_deferral']
            assert handed['reason'].startswith('read both') and handed['state_changed'] is False, handed
            write.update_node(VAULT, 'A', old_text='A revised observation', new_text='A twice revised observation')
            planned = growth_plan.plan(VAULT, 3)
            assert planned['recheck_jobs'][0]['previous_deferral']['state_changed'] is True, planned['recheck_jobs']
            # an escalation still carries its proposal
            manifest = register({**planned, 'scope_jobs': []})
            refused = growth_reviews.checkpoint(VAULT, {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                'recheck': [{'key': planned['recheck_jobs'][0]['key'], 'outcome': 'escalated', 'reason': 'x'}]}})
            assert not refused['ok'] and any('invalid recheck review fields' in e for e in refused['errors']), refused
        """)

    def test_a_run_whose_only_open_jobs_are_rechecks_applies_its_final_packet(self):
        # The runner applies the worker's final packet while a selected job is open. A run
        # left with rechecks alone must apply its escalations and deferrals too (#162 review).
        self.check_case("""
            node('A')
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            job = growth_plan.plan(VAULT, 3)['recheck_jobs'][0]
            only = {'candidates': [], 'scope_jobs': [], 'organization_jobs': [], 'eviction_jobs': [],
                    'recheck_jobs': [job]}
            assert growth_reviews.needs_review(VAULT, only, growth_ledger.ledger_records(VAULT), graph.Index(VAULT)), 'an open recheck did not ask for the packet'
            write.update_node(VAULT, 'B', rechecked=['A'])
            assert not growth_reviews.needs_review(VAULT, only, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
        """)

    def test_evidence_section_changes_do_not_propagate_and_old_records_carry_over(self):
        # Bylaws §7 2: the 근거 section at a node's end lists its non-node evidence, so changing
        # it is not a related-state change. A record measured before that rule carries over.
        self.check_case("""
            from osk import rechecks
            first = '- 2026-10-05 · `c38d8ef2#1` · 지지 — first'
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', first]))
            assert write.create_node(VAULT, 'B', 'B', 'B relies on A.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'A'})['ok']
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0]
            write.update_node(VAULT, 'A', old_text=first, new_text=first + chr(10) + '- 2026-10-06 · `c38d8ef2#2` · 조건 — second')
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'a 근거 line made B a recheck candidate'
            # only the node's own heading starts it: a quoted or listed `## 근거` is body
            for inner in ('> ## 근거' + chr(10) + '> quoted line', '- item' + chr(10) + chr(10) + '  ## 근거' + chr(10) + '  listed'):
                assert rechecks.evidence_span('A claim.' + chr(10) + chr(10) + inner) is None, inner
            # a record that measured the whole body, the 근거 section included
            idx = graph.Index(VAULT)
            a, b = (idx.locate(n)[0] for n in ('A', 'B'))
            data = a.read_bytes()
            whole = core.sha256_bytes(data[contract.body_offset(data):])
            assert whole != rechecks.state(data)
            core.ledger_append(rechecks.ledger_path(VAULT), rechecks._row(idx.node(b).id, rechecks.state(b.read_bytes()),
                                                                  idx.node(a).id, whole, 'unchanged'))
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'an unchanged old-definition record stopped holding'
            assert rechecks.ensure_baseline(VAULT) == 0, 'a node write would scan every pair'
            assert rechecks.ensure_baseline(VAULT, carry=True) == 1
            row = core.ledger_read(rechecks.ledger_path(VAULT))[-1]
            assert row['reason'] == '이어받음' and row['target_state'] == rechecks.state(data), row
            write.update_node(VAULT, 'A', old_text='— second', new_text='— second, again')
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'the carried record did not hold'
            write.update_node(VAULT, 'A', old_text='A claim.', new_text='A revised claim.')
            assert [i['node'] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]] == ['B']
        """)

    def test_upgrade_keeps_escalations_and_cascades_recorded_before_the_evidence_rule(self):
        # Records written before the 근거 section left the state measured it. While the content
        # holds, a pending user review and a cascade must survive the upgrade (another session
        # reproduced both being lost on 2026-10-07), and an edit of a 근거 section after the carry
        # must not drop them (#177).
        self.check_case(UPGRADE_CARRY.replace('V50_CARRY', 'False'))

    def test_a_recheck_made_version_carries_when_no_pair_is_complete(self):
        self.check_case(UPGRADE_NO_PAIR)

    def test_a_v5_0_carry_still_keeps_the_cascade_after_an_evidence_edit(self):
        # v5.0.x carried the completion as `unchanged` and left the user review as it was. While
        # the content holds, the next carry still marks the version a recheck made (#177).
        self.check_case(UPGRADE_CARRY.replace('V50_CARRY', 'True'))

    def test_evidence_section_rule_resolves_coordinates_and_reports_until_activated(self):
        # Mechanism §6-1 4: each 근거 line has the §8 9 form and a coordinate that resolves — a
        # record and its rounds, a vault non-node file and its heading, a URL. A node is not a
        # 근거 line, and a non-node left in derived-from is reported until it moves to the section.
        self.check_case("""
            from osk import evidence, raw
            claude = 'claude-c38d8ef2' + '0' * 24
            first = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': 'q1', 'agent': 'a1'},
                                                          {'user': 'q2', 'agent': 'a2'}], '00_Scope/W1')
            (VAULT.root / '_sources' / 'r.md').write_text('# 결과' + chr(10) + 'x' + chr(10), encoding='utf-8')
            (VAULT.root / '_sources' / 'd').mkdir()
            node('N')
            lines = ['- 2026-10-05 · `c38d8ef2#1–2` · 지지 — rounds',
                     '- 2026-10-05 · `c38d8ef2#3` · 반례 — no round',
                     '- 2026-10-05 · `_sources/r.md#결과` · 조건 — file',
                     '- 2026-10-05 · `_sources/r.md#없다` · 조건 — heading',
                     '- 2026-10-05 · `_sources/d` · 조건 — directory',
                     '- 2026-10-05 · `https://example.com/a` · 지지 — url',
                     '- 2026-10-05 · `N` · 지지 — node',
                     '- 2026-10-05 · `brain:wiki/x.md` · 지지 — alias',
                     '- 10-05 · c38d8ef2#1 · 지지 — form']
            text = chr(10).join(['x', '', '## 근거', ''] + lines)
            idx = graph.Index(VAULT)
            errs = validate.evidence_errors(VAULT, text, [], idx)
            assert errs == ['없는 라운드: c38d8ef2#3', '없는 제목: _sources/r.md#없다', '없는 파일: _sources/d',
                            '노드를 가리키는 근거 줄: N — 노드 근거는 derived-from으로 옮긴다',
                            '선언되지 않은 별칭: brain:wiki/x.md',
                            '형식이 다른 관측 줄: - 10-05 · c38d8ef2#1 · 지지 — form'], errs
            # a shortened hash two records share is ambiguous; a longer prefix tells them apart
            raw.append_rounds(VAULT, 'repo/ev', 'codex-c38d8ef2' + '1' * 24, [{'user': 'q', 'agent': 'a'}], '00_Scope/W1')
            records = raw.Records(VAULT)
            assert validate.evidence_errors(VAULT, text, [], idx, records)[0].startswith('모호한 좌표: c38d8ef2#1–2')
            assert records.short(claude) == 'c38d8ef20', records.short(claude)
            longer = chr(10).join(['x', '', '## 근거', '', '- 2026-10-05 · `c38d8ef20#1–2` · 지지 — longer'])
            assert validate.evidence_errors(VAULT, longer, [], idx, records) == []
            # `# 관측` opens the section as the pre-v5.6 `## 근거` does (Mechanism §4-1 1), and renaming
            # the heading leaves the relevant state as it was — it is measured up to the heading line
            from osk import rechecks
            observed = longer.replace('## 근거', '# 관측')
            assert validate.evidence_errors(VAULT, observed, [], idx, records) == []
            assert validate.evidence_errors(VAULT, observed + chr(10) + '## 이력' + chr(10), []) == ['관측 절이 노드 끝에 있지 않다']
            assert rechecks.evidence_span('x' + chr(10) + '## 관측' + chr(10) + '- a') is None
            assert rechecks.evidence_span('x' + chr(10) + '# 근거' + chr(10) + '- a') is None
            fm = '---' + chr(10) + 'id: x' + chr(10) + '---' + chr(10)
            assert rechecks.state((fm + longer).encode()) == rechecks.state((fm + observed).encode())
            # a node named 관측 whose body opens with its own `# 관측` title: the claim is not a section,
            # so editing it changes the state and still reaches the nodes that derive from it (#310 review)
            titled = '# 관측' + chr(10) + chr(10) + '주장 A.' + chr(10)
            assert rechecks.evidence_span(titled) is None
            assert validate.evidence_errors(VAULT, titled, []) == []
            assert rechecks.state((fm + titled).encode()) != rechecks.state((fm + titled.replace('A', 'B')).encode())
            # a `# 관측` section added after a legacy `## 근거` one turns the older lines into body text —
            # the validator reports the stranded heading (review of #311)
            stranded = longer + chr(10) + chr(10) + '# 관측' + chr(10) + chr(10) + '- 2026-11-09 · `c38d8ef20#1` · 지지 — new'
            assert validate.evidence_errors(VAULT, stranded, [], idx, records) == ['관측 절이 노드 끝에 있지 않다']
            # a non-node left in derived-from is reported once; a node there is not
            assert validate.evidence_errors(VAULT, 'x', ['N', first['round_refs'][0]], idx) == [
                'derived-from에 비노드 대상 1개가 남아 있다 — 관측 절로 옮긴다']
            moved = chr(10).join(['x', '', '## 근거', '', '- a', '', '## 이력', '', 'y'])
            assert validate.evidence_errors(VAULT, moved, []) == ['관측 절이 노드 끝에 있지 않다']
            # any list marker is a list item
            starred = chr(10).join(['x', '', '## 근거', '', '* 2026-10-05 · `c38d8ef20#1` · 지지 — star'])
            assert validate.evidence_errors(VAULT, starred, [])[0].startswith('형식이 다른 관측 줄: * 2026'), 'a * item was skipped'
            nested = chr(10).join(['x', '', '## 근거', '', '- 2026-10-05 · `c38d8ef20#1` · 지지 — fine',
                                   '    * 2026-10-05 · `c38d8ef20#1` · 지지 — nested', 'a paragraph'])
            errs = validate.evidence_errors(VAULT, nested, [])
            assert [e.split(':')[0] for e in errs] == ['형식이 다른 관측 줄'] * 2, errs
            quoted = chr(10).join(['x', '', '> ## 근거', '>', '> - a'])
            assert validate.evidence_errors(VAULT, quoted, []) == [], 'a quoted heading was read as the section'
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', '- 2026-10-05 · `deadbeef#2` · 지지 — none',
                                         '- 2026-10-05 · `brain:wiki/x.md` · 지지 — held']))
            # B was written before v5.4 with a round in derived-from; writes no longer add one
            from unittest import mock
            assert 'derived-from은 노드만' in str(node_checks.check_edges(VAULT, {'derived-from': first['round_refs'][0]}, idx))
            with mock.patch.object(node_checks, 'check_edges', return_value=[]):
                assert write.create_node(VAULT, 'B', 'B', 'B claim.', 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': first['round_refs'][0]})['ok']
            from osk import roots
            roots.declare(VAULT, 'brain', 'wiki')
            rep = validate.run(VAULT)
            # a non-node left in derived-from is counted by node, not listed line by line
            assert list(rep['evidence_section']) == ['A'] and rep['evidence_section_leftovers'] == 1, rep['evidence_section']
            # a declared root that is not on this device is held, not a violation (Mechanism §6-1 4)
            assert rep['evidence_section']['A'] == ['없는 기록: deadbeef#2'], rep['evidence_section']
            assert rep['evidence_section_held'] == {'brain': '이 기기에 위치가 없다'}, rep['evidence_section_held']
            assert not rep['evidence_section_active']
            assert not any('근거 절' in f for d in rep['fail'] for f in d), rep['fail']
            core.ledger_append(VAULT.validators, {'kind': 'activate', 'rule': 'evidence-section'})
            assert any('근거 절' in f for d in validate.run(VAULT)['fail'] for f in d), 'an active rule did not count'
        """)

    def test_a_clean_rule_without_records_turns_itself_on_once_a_day(self):
        # Bylaws §11(3), Mechanism §6-1(2): a rule with violations stays report-only; once clean it is
        # activated at the next daily check, and a rule the user turned off is never turned back on.
        self.check_case("""
            from osk import response_growth
            day = 24 * 3600
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '', '- not a line']))
            assert 'evidence-section' not in validate.auto_activate(VAULT, now=day)
            assert not validate.validator_active(VAULT, 'evidence-section')
            assert write.update_node(VAULT, 'A', old_text='- not a line',
                                     new_text='- 2026-11-01 · `https://example.com/a` · 지지 — a page')['ok']
            assert validate.auto_activate(VAULT, now=day + 60) == [], 'checked twice in a day'
            assert 'evidence-section' in validate.auto_activate(VAULT, now=2 * day + 60)
            assert validate.validator_active(VAULT, 'evidence-section')
            assert core.ledger_read(VAULT.validators)[-1]['reason'] == validate.AUTO_REASON
            core.ledger_append(VAULT.validators, {'kind': 'deactivate', 'rule': 'evidence-section'})
            assert validate.auto_activate(VAULT, now=9 * day) == [] and not validate.validator_active(VAULT, 'evidence-section')
            # the hooks' background worker is where it runs on every install
            import contextlib
            from unittest import mock
            from osk import conversation_capture, conversation_state
            conversation_state.state_path(VAULT, 'claude', 's').parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(conversation_capture, 'hook_source', return_value=('claude', 's', None)), \\
                 mock.patch.object(conversation_capture, 'capture_due', return_value=False), \\
                 mock.patch.object(conversation_state, 'state_lock', lambda *a: contextlib.nullcontext(None)), \\
                 mock.patch.object(conversation_state, 'load_state', lambda *a: {}), \\
                 mock.patch.object(response_growth, '_probe_due', return_value=False), \\
                 mock.patch.object(validate, 'auto_activate') as auto:
                response_growth.after_hook(VAULT, {'session': 'k'})
            assert auto.called
        """)

    def test_a_user_deactivation_that_lands_during_the_check_is_kept(self):
        # Mechanism §6-1(2): the engine never overturns a user's record — the CLI does not take the
        # mutation lock, so the ledger lock (`expect`) is where an activation backs off.
        self.check_case("""
            from unittest import mock
            real = core.ledger_append
            def user_first(path, record, expect=None):
                # the user's `--deactivate` lands right before the activation takes the ledger lock
                if record.get('rule') == 'evidence-section':
                    real(VAULT.validators, {'kind': 'deactivate', 'rule': 'evidence-section'})
                return real(path, record, expect)
            with mock.patch.object(validate, 'ledger_append', user_first):
                assert 'evidence-section' not in validate.auto_activate(VAULT, now=24 * 3600)
            assert not validate.validator_active(VAULT, 'evidence-section')
            assert [r['kind'] for r in core.ledger_read(VAULT.validators) if r['rule'] == 'evidence-section'] == ['deactivate']
        """)

    def test_an_anchor_written_before_tag_spacing_still_matches(self):
        # #292: writes space `#123` off a following letter (Obsidian tag defence), so the text an agent
        # just wrote is not what is stored. An anchor copied from what the agent wrote still matches.
        self.check_case("""
            from osk import contract, scope_memory as sm
            node('A', body='이슈 #275와 #12-alpha를 고친다.')
            path = next((VAULT.root / '00_Scope/W1').glob('A.md'))
            assert '#275 와 #12 -alpha' in contract.parse(path).body, contract.parse(path).body
            r = write.update_node(VAULT, 'A', old_text='#275와 #12-alpha', new_text='#275와 #12-beta')
            assert r['ok'] and '#275 와 #12 -beta' in contract.parse(path).body, r
            h = sm.read(VAULT, 'p', space='00_Scope/W1')['hash']
            assert sm.replace(VAULT, 'p', text='# W1' + chr(10) + '- 이슈 #275와 연결', expect_hash=h, space='00_Scope/W1')['ok']
            r = sm.replace(VAULT, 'p', edits=[{'old_text': '#275와 연결', 'new_text': '#275와 이음'}], space='00_Scope/W1')
            assert r['ok'] and '#275 와 이음' in sm.read(VAULT, 'p', space='00_Scope/W1')['text'], r
        """)

    def test_a_scope_node_may_cite_a_ledger_record(self):
        # Constitution 8(3): a Scope node may reference any non-node. The ledger sits under Workbench but
        # is an institutional record (Workbench contract 2.3), not work state, so it is no cross-scope reference.
        self.check_case("""
            core.ledger_append(VAULT.validators, {'kind': 'activate', 'rule': 'evidence-section'})
            ledger = VAULT.validators.relative_to(VAULT.root).as_posix()
            node('A', body=chr(10).join(['A claim.', '', '## 근거', '',
                                         '- 2026-11-01 · `' + ledger + '` · 지지 — 활성화 기록']))
            rep = validate.run(VAULT)
            assert not any('scope 간' in f for d in rep['fail'] for v in d.values() for f in v), rep['fail']
            assert 'A' not in rep.get('evidence_section', {}), rep.get('evidence_section')
        """)

    def test_evidence_migration_moves_non_node_derived_from_into_support_lines(self):
        # Constitution 8(5·6)·14(8·9), Mechanism §5: each non-node derived-from entry becomes a `지지` line at
        # the node end, dated by the commit that brought the round (or put the entry in the frontmatter — not
        # the earlier body mention) and showing the cite note, else the user's words. An uncommitted round has
        # no date and stays. A covered entry only leaves the frontmatter and a node entry stays. The related
        # state does not change, each node is logged before it is written, and a rerun has nothing.
        self.check_case("""
            import os, subprocess
            from osk import evidence, evidence_migration as em, raw, rechecks
            def git(*a, when=None):
                env = dict(os.environ, **({'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when} if when else {}))
                return subprocess.run(['git', '-C', str(VAULT.root), *a], check=True, capture_output=True,
                                      text=True, env=env).stdout.strip()
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            claude = 'claude-c38d8ef2' + '0' * 24
            rounds = raw.append_rounds(VAULT, 'repo/ev', claude, [
                {'user': '<system-reminder>알림</system-reminder>' + '첫 질문입니다 ' * 9, 'agent': '긴 답'},
                {'user': '둘째 질문', 'agent': '답'}], '00_Scope/W1')['round_refs']
            noted = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '셋째 질문', 'agent': '인용 요지 #272의 결정'}],
                                      cited={'harness': 'claude', 'conversation': 'c1', 'turn': 't1'})['round_refs'][0]
            notice = raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '<task-notification><task-id>b1</task-id>'
                                       '</task-notification>', 'agent': '작업이 끝나 결과를 정리했다'}])['round_refs'][0]
            (VAULT.root / '_sources').mkdir(exist_ok=True)
            (VAULT.root / '_sources' / 'r.md').write_text('# 결과' + chr(10) + 'x' + chr(10), encoding='utf-8')
            section = chr(10).join(['', '', '## 근거', '', '- 2026-10-05 · `c38d8ef2#2` · 지지 — 이미 있다'])
            url = 'https://example.com/docs/page.md'
            node('N')
            node('A', body='주장 A. 출처는 ' + url)
            node('B', body='주장 B.' + section)
            node('C', body='주장 C.' + section + chr(10) + chr(10) + '## 이력' + chr(10) + chr(10) + 'y')
            git('add', '-A'); git('commit', '-qm', 'early', when='2026-01-01T12:00:00+09:00')
            def legacy(title, refs):
                p = VAULT.root / '00_Scope/W1' / (title + '.md')
                n = contract.parse(p)
                p.write_bytes(nodefile.render({**n.meta, 'derived-from': refs}, n.body))
                return p
            missing, late = rounds[0].replace('#1', '#9'), rounds[0].replace('#1', '#5')
            a = legacy('A', ['[[N]]', rounds[0], noted, '[[_sources/r.md]]', '[[' + url + ']]', missing, late])
            b = legacy('B', [rounds[1], rounds[0], notice])
            c = legacy('C', [rounds[0]])
            git('add', '-A'); git('commit', '-qm', 'seed', when='2026-02-01T12:00:00+09:00')
            assert raw.append_rounds(VAULT, 'repo/ev', claude, [{'user': '늦은 질문', 'agent': '답'}])['round_refs'] == [late]
            state = {p: rechecks.state(p.read_bytes()) for p in (a, b, c)}
            seen = em.preview(VAULT)
            assert (seen['nodes'], seen['line'], seen['covered'], seen['hold']) == (2, 6, 1, 3), seen
            assert sorted(h['reason'].split(':')[0] for h in seen['holds']) == [
                '근거 절을 본문을 바꾸지 않고 노드 끝에 달 수 없다', '날짜를 정할 수 없다 — git 기록에 없다', '없는 라운드'], seen
            assert (VAULT.root / seen['plan']).is_file() and not em.events_path(VAULT).exists()
            done = em.apply(VAULT)
            assert (done['nodes'], done['line'], done['covered'], done['hold']) == (2, 6, 1, 3), done
            assert all(rechecks.state(p.read_bytes()) == state[p] for p in (a, b, c)), 'the related state changed'
            na, nb = contract.parse(a), contract.parse(b)
            assert na.meta['derived-from'] == ['[[N]]', missing, late] and 'derived-from' not in nb.meta, (na.meta, nb.meta)
            assert evidence.coordinates(na.body) == ['c38d8ef2#1', 'c38d8ef2#3', '_sources/r.md', url], na.body
            # rounds date from the commit that brought them; the file and the URL from the commit that put them
            # in the frontmatter, not the earlier body mention
            first = '- 2026-01-01 · `c38d8ef2#1` · 지지 — ' + ('첫 질문입니다 ' * 9)[:50].rstrip() + '…'
            lines = na.body.rstrip(chr(10)).split(chr(10))
            assert lines[-7:-4] == ['주장 A. 출처는 ' + url, '# 관측', ''] and lines[-4] == first, lines
            assert lines[-3] == '- 2026-01-01 · `c38d8ef2#3` · 지지 — 인용 요지 #272 의 결정', lines
            assert lines[-2:] == ['- 2026-02-01 · `_sources/r.md` · 지지 — r.md',
                                  '- 2026-02-01 · `' + url + '` · 지지 — page.md'], lines
            assert nb.body.rstrip(chr(10)).split(chr(10))[-3:] == [
                '- 2026-10-05 · `c38d8ef2#2` · 지지 — 이미 있다', first,
                '- 2026-01-01 · `c38d8ef2#4` · 지지 — 작업이 끝나 결과를 정리했다'], nb.body
            assert validate.evidence_errors(VAULT, nb.body, nb.meta.get('derived-from')) == []
            events = [r for r in core.ledger_read(em.events_path(VAULT)) if r.get('rule') == em.RULE]
            assert [(e['source'], e['after']) for e in events] == [
                (p.relative_to(VAULT.root).as_posix(), core.sha256_file(p)) for p in (a, b)], events
            manifest = (VAULT.root / done['manifest']).read_text(encoding='utf-8')
            assert '## 결과' in manifest and '보류 — 없는 라운드' in manifest, manifest
            assert em.plan(VAULT)['counts'] == {'line': 0, 'covered': 0, 'hold': 3}, 'a rerun moved something again'
        """)

    def test_doctor_reports_and_applies_the_remaining_migrations(self):
        # 2026-11-09 user decision: compatibility and bulk migrations live in `osk doctor` — the report only
        # reads, `--migrate` plans, then applies them in order once confirmed. v5.6 renames the `## 근거` heading to `# 관측` without
        # touching the claim, the lines, the frontmatter or the related state. A body that opens with
        # `## 근거` would read `# 관측` as its own title, so it is held and keeps the legacy heading.
        self.check_case("""
            from osk import doctor, evidence_migration as em, rechecks
            line = '- 2026-11-09 · `https://example.com/a` · 지지 — page'
            node('L', body='주장 L.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            node('T', body='## 근거' + chr(10) + chr(10) + line)
            node('O', body='주장 O.' + chr(10) + chr(10) + '# 관측' + chr(10) + chr(10) + line)
            node('Tab', body='주장 Tab.' + chr(10) + chr(10) + '##' + chr(9) + '근거' + chr(10) + chr(10) + line)
            # X is cited by heading range: an H1 `# 관측` would close the range of `# X`, so it is held
            node('X', body='# X' + chr(10) + chr(10) + '주장 X.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            assert write.create_node(VAULT, 'C', 'C', 'C relies on X.', 'gpt-6-astra', space='00_Scope/W1',
                                     edges={'derived-from': 'X#X'})['ok']
            # Y's section is only linked from another body, not cited: the link would lose its anchor (#311 review)
            node('Y', body='주장 Y.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10) + line)
            node('D', body='D reads [[Y#근거]].')
            paths = {t: VAULT.root / '00_Scope/W1' / (t + '.md') for t in ('L', 'T', 'O', 'Tab', 'X', 'Y')}
            before = {t: p.read_bytes() for t, p in paths.items()}
            vault = {i['check']: i for i in doctor._vault(VAULT)}
            heading = next(i for k, i in vault.items() if '# 관측' in k)
            assert heading['level'] == 'warn' and '2개' in heading['detail'] and '--migrate' in heading['fix'], vault
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the report wrote'
            # The first run only plans and asks for the user's yes; nothing moves yet.
            plan, code = doctor.migrate(VAULT)
            assert code == 2 and plan['approval_required'] and [m[0] for m in plan['plan']['migrations']] == [
                heading['check']] and len(plan['plan']['migrations'][0][1]) == 2, plan
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the plan wrote'
            # The apply re-plans under the lock; a node the user did not confirm stops it before any write (#313 review).
            only_l = [t for t in plan['plan']['migrations'][0][1] if t[0].endswith('/L.md')]
            try:
                em.heading_apply(VAULT, expect=only_l)
            except RuntimeError as e:
                assert 'Tab.md' in str(e), e
            else:
                raise AssertionError('migrated a node that was not confirmed')
            # A confirmed node that changed after the user's yes stops it too (#313 review, round 2).
            stale = [[p, 'sha256:0' if p.endswith('/L.md') else h] for p, h in plan['plan']['migrations'][0][1]]
            try:
                em.heading_apply(VAULT, expect=stale)
            except RuntimeError as e:
                assert 'L.md' in str(e) and 'Tab.md' not in str(e), e
            else:
                raise AssertionError('migrated a node that changed after it was confirmed')
            assert all(p.read_bytes() == before[t] for t, p in paths.items()), 'the refused apply wrote'
            assert not list(em.events_path(VAULT).parent.glob('observation-heading-*')), 'the refused apply opened a manifest'
            done, code = doctor.migrate(VAULT)
            assert code == 0 and [d['migration'] for d in done['applied']] == [heading['check']], done
            assert (done['applied'][0]['nodes'], done['applied'][0]['holds']) == (2, 3), done
            new = paths['L'].read_bytes()
            assert new == before['L'].replace('## 근거'.encode(), '# 관측'.encode()), new
            assert rechecks.state(new) == rechecks.state(before['L'])
            assert paths['Tab'].read_bytes() == before['Tab'].replace(('##' + chr(9) + '근거').encode(), '# 관측'.encode())
            assert all(paths[t].read_bytes() == before[t] for t in ('T', 'O', 'X', 'Y'))
            assert not rechecks.candidates(VAULT, graph.Index(VAULT))[0], 'the migration made recheck candidates'
            manifest = (VAULT.root / done['applied'][0]['manifest']).read_text(encoding='utf-8')
            assert '`00_Scope/W1/X.md` → 보류 — 제목으로 참조된다(`#X`)' in manifest, manifest
            assert '`00_Scope/W1/Y.md` → 보류 — 제목으로 참조된다(`#근거`)' in manifest, manifest
            assert sorted(e['source'] for e in core.ledger_read(em.events_path(VAULT))
                          if e.get('rule') == em.HEADING_RULE) == ['00_Scope/W1/L.md', '00_Scope/W1/Tab.md']
            assert all(i['level'] == 'ok' for i in doctor._vault(VAULT)), doctor._vault(VAULT)
            # nothing left: a rerun applies nothing and opens no empty manifest
            assert doctor.migrate(VAULT) == ({'ok': True, 'applied': []}, 0)
            assert len(list(em.events_path(VAULT).parent.glob('observation-heading-*.md'))) == 1
        """)

    def test_doctor_migrates_evidence_before_moving_old_place_records(self):
        # The evidence migration dates a round by `git blame` on the record's current path, so a record
        # moved first is untracked there and its entry is held. `doctor --migrate` migrates first; the
        # record move (#308) is one of its migrations since the two doctor commands became one.
        self.check_case("""
            import subprocess
            from osk import doctor, evidence, raw
            def git(*a):
                subprocess.run(['git', '-C', str(VAULT.root), *a], check=True, capture_output=True)
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            name = 'claude-c38d8ef2' + '0' * 24
            old = VAULT.root / '00_Scope/W1/_raw/.records'
            old.mkdir(parents=True)
            (old / (name + '.txt')).write_bytes(raw.round_block(1, '옛 자리의 질문', '답').encode())
            # A also keeps the legacy `## 근거` heading: the v5.4 lines land in it, then the v5.6 rename takes
            # A at the hash v5.4 wrote — the confirmed hash carried forward, not a change after the yes.
            node('A', body='주장 A.' + chr(10) + chr(10) + '## 근거' + chr(10) + chr(10)
                 + '- 2026-11-09 · `https://example.com/a` · 지지 — page')
            a = VAULT.root / '00_Scope/W1/A.md'
            n = contract.parse(a)
            a.write_bytes(nodefile.render({**n.meta, 'derived-from': ['00_Scope/W1/_raw/.records/' + name + '.txt#1']}, n.body))
            git('add', '-A'); git('commit', '-qm', 'seed')
            v54, v52 = '비노드 derived-from → 관측 줄 (v5.4)', '옛 자리의 인용 기록 → 루트 `_cited/` (v5.2)'
            v56 = '관측 절 제목 `## 근거` → `# 관측` (v5.6)'
            plan, code = doctor.migrate(VAULT)
            assert code == 2 and [m[0] for m in plan['plan']['migrations']] == [v54, v52, v56], plan
            # A plan that changed after it was shown asks again: a second old-place record turned up.
            other = 'claude-d48d8ef2' + '0' * 24
            (old / (other + '.txt')).write_bytes(raw.round_block(1, '또 다른 질문', '답').encode())
            assert doctor.migrate(VAULT)[1] == 2
            rep, code = doctor.migrate(VAULT)
            applied = {a['migration']: a for a in rep['applied']}
            assert code == 0 and list(applied) == [v54, v52, v56], rep
            assert applied[v52]['count'] == 2 and applied[v54]['nodes'] == 1 and applied[v56]['nodes'] == 1, applied
            na = contract.parse(a)
            assert 'derived-from' not in na.meta and 'c38d8ef2#1' in evidence.coordinates(na.body), na.body
            assert '# 관측' in na.body and '## 근거' not in na.body, na.body
            assert raw.locate_record(VAULT, name) == VAULT.root / '_cited/.records' / (name + '.txt')
            assert doctor.migrate(VAULT) == ({'ok': True, 'applied': []}, 0)
        """)

    def test_recheck_cascade_keeps_node_identity_when_bodies_match(self):
        self.check_case("""
            from osk import rechecks
            node('A')
            for title, body, basis in (('B', 'Old B.', 'A'), ('C', 'C relies on B.', 'B'),
                                       ('X', 'Old X.', 'A'), ('Y', 'Y relies on X.', 'X')):
                assert write.create_node(VAULT, title, title, body, 'gpt-6-astra', space='00_Scope/W1',
                                         edges={'derived-from': basis})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            write.update_node(VAULT, 'B', old_text='Old B.', new_text='The same claim.',
                              rechecked=['A'])
            write.update_node(VAULT, 'X', old_text='Old X.', new_text='The same claim.')
            cascades = {i['node']: i['cascade'] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]}
            assert cascades['C'] is True, cascades
            assert cascades['Y'] is False, cascades
        """)

    def test_recheck_pick_rotates_by_attempts_in_a_fork_scope(self):
        self.check_case("""
            node('A')
            for title in ('B1', 'B2'):
                assert write.create_node(VAULT, title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            first = growth_plan.pick_rechecks(VAULT, graph.Index(VAULT), 1, 'W1')
            register({**growth_plan.plan(VAULT, 3), 'scope_jobs': [], 'recheck_jobs': first})
            again = growth_plan.pick_rechecks(VAULT, graph.Index(VAULT), 1, 'W1')
            assert {first[0]['node'], again[0]['node']} == {'B1', 'B2'}, (first, again)
        """)

    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-growth-test-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE),
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory, OSK_HOOK_WORKER="0")
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
            fake = VAULT.root / 'fake-claude.py'
            fake.write_text(chr(10).join([
                "import json, os, sys",
                "args = sys.argv[1:]",
                "if args == ['--version']: print('2.1.281 (Claude Code)'); sys.exit(0)",
                "if args[:2] == ['auth', 'status']: print(os.environ['OSK_TEST_AUTH']); sys.exit(0)",
                "open(os.path.join(os.environ['OSK_VAULT_ROOT'], 'seen-env.json'), 'w').write(json.dumps("
                "{k: os.environ.get(k) for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')}))",
                packet_worker(wrapper='claude')]), encoding='utf-8')
            if os.name == 'nt':
                cli = VAULT.root / 'claude.cmd'
                cli.write_text('@"' + sys.executable + '" "' + str(fake) + '" %*' + chr(13) + chr(10), encoding='utf-8')
            else:
                cli = VAULT.root / 'claude'
                cli.write_text('#!/bin/sh' + chr(10) + 'exec "' + sys.executable + '" "' + str(fake) + '" "$@"' + chr(10),
                               encoding='utf-8')
                cli.chmod(0o755)
            argv = harness.get('claude').growth_argv(str(cli), sys.executable, 'server.py', str(VAULT.root))
            config = VAULT.root / 'claude-config'
            config.mkdir()
            env = {'CLAUDE_CONFIG_DIR': str(config), 'ANTHROPIC_API_KEY': 'sk-ant-test', 'ANTHROPIC_AUTH_TOKEN': 'tok',
                   'OSK_TEST_AUTH': json.dumps({'loggedIn': True, 'authMethod': 'apiKey'})}
            # An API-key login is refused before any plan or model call.
            with patch.dict(os.environ, env):
                refused = growth.run(VAULT, argv)
            assert refused['state'] == 'unavailable' and 'subscription' in refused['error'], refused
            assert not growth_ledger.ledger_path(VAULT).exists() and not (VAULT.root / 'seen-env.json').exists()
            # On a confirmed subscription the command runs, and no API credential reaches it.
            env['OSK_TEST_AUTH'] = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'})
            with patch.dict(os.environ, env):
                result = growth.run(VAULT, argv)
            assert result['ok'], result
            seen = json.loads((VAULT.root / 'seen-env.json').read_text(encoding='utf-8'))
            assert seen == {'ANTHROPIC_API_KEY': None, 'ANTHROPIC_AUTH_TOKEN': None}, seen
            # The scheduler's real entry reads the command from the file and runs it the same way.
            import subprocess
            node('B')
            (VAULT.root / 'seen-env.json').unlink()
            command_file = VAULT.root / '.osk' / 'growth-command.json'
            command_file.parent.mkdir(parents=True, exist_ok=True)
            command_file.write_text(json.dumps(argv), encoding='utf-8')
            entry = Path(growth.__file__).resolve().parents[1] / 'scripts' / 'growth_run.py'
            for extra in (['--check'], ['--limit', '1']):
                done = subprocess.run([sys.executable, str(entry), '--command-file', str(command_file), *extra],
                                      env={**os.environ, **env}, capture_output=True, text=True,
                                      encoding='utf-8', timeout=120)
                assert done.returncode == 0 and json.loads(done.stdout)['ok'], (extra, done.stdout, done.stderr)
            seen = json.loads((VAULT.root / 'seen-env.json').read_text(encoding='utf-8'))
            assert seen == {'ANTHROPIC_API_KEY': None, 'ANTHROPIC_AUTH_TOKEN': None}, seen
            # Settings that choose an API key helper stop it before the model too.
            (config / 'settings.json').write_text(json.dumps({'apiKeyHelper': 'echo key'}), encoding='utf-8')
            with patch.dict(os.environ, env):
                refused = growth.run(VAULT, argv)
            assert refused['state'] == 'unavailable' and 'API' in refused['error'], refused
        """)

    def test_empty_inventory_does_not_launch(self):
        self.check_case("""
            from unittest.mock import patch
            with patch('osk.growth.subprocess.Popen', side_effect=AssertionError('launched')):
                result = growth.run(VAULT, ['unused-command'])
            assert result == {'ok':True, 'state':'skipped', 'selected':0}, result
            assert not growth_ledger.ledger_path(VAULT).exists()
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
                found = worker_process.check_command(VAULT, [program.name])
                # Contract: absolute, symlinks kept; POSIX searches from the vault,
                # Windows from the caller's cwd (spelled as the caller spells it).
                base = VAULT.root if os.name == 'posix' else Path.cwd()
                assert found['executable'] == str(base / program), found
            before = {str(p):p.read_bytes() for p in VAULT.root.rglob('*') if p.is_file()}
            with patch('osk.growth.subprocess.Popen', side_effect=AssertionError('launched')):
                assert worker_process.check_command(VAULT, [sys.executable,'--version'])['ok']
                missing = worker_process.check_command(VAULT, [str(VAULT.root/'retired-version/codex.exe')])
                assert not missing['ok'] and missing['state'] == 'invalid_command', missing
                assert not worker_process.check_command(VAULT, [str(VAULT.root)])['ok']
                for bad in ([], 'codex', ['codex', 'bad' + chr(0)], [''], ['', '--tools']):
                    try:
                        worker_process.check_command(VAULT, bad)
                        raise AssertionError('invalid argv accepted')
                    except ValueError:
                        pass
            after = {str(p):p.read_bytes() for p in VAULT.root.rglob('*') if p.is_file()}
            assert before == after
            node('A')
            rejected = growth.run(VAULT, [str(VAULT.root/'retired-version/codex.exe')])
            assert rejected['state'] == 'invalid_command', rejected
            assert not growth_ledger.ledger_path(VAULT).exists()
        """)

    @unittest.skipUnless(os.name == 'nt', 'only Windows runs a batch file through cmd.exe')
    def test_command_check_refuses_a_batch_cli_that_cmd_would_split(self):
        # An npm install puts `claude.cmd` on PATH. cmd.exe reads list2cmdline's `\"` as a quote
        # toggle, so the MCP config JSON leaves the vault path outside quotes and `&` splits it.
        self.check_case("""
            import subprocess
            from osk import harness
            probe = VAULT.root / 'probe.py'
            probe.write_text('import json, sys; print(json.dumps(sys.argv[1:]))', encoding='utf-8')
            cli = VAULT.root / 'claude.cmd'
            cli.write_text('@"' + sys.executable + '" "' + str(probe) + '" %*' + chr(13) + chr(10), encoding='utf-8')
            for root in ('C:/vault', 'C:/My Vault (x86)', 'C:/R&D/vault', 'C:/My R&D/vault'):
                argv = harness.get('claude').growth_argv(str(cli), root + '/.venv/Scripts/python.exe',
                                                         root + '/_governance/_engine/mcp_server.py', root)
                seen = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
                intact = seen.returncode == 0 and json.loads(seen.stdout) == argv[1:]
                assert intact == ('&' not in root), (root, seen.stdout, seen.stderr)  # cmd's own verdict
                checked = worker_process.check_command(VAULT, argv)
                assert checked['ok'] == intact, (root, checked)
                assert intact or 'cmd.exe' in checked['violations'][0], checked
            # The daily run refuses before it plans or starts anything.
            node('A')
            refused = growth.run(VAULT, argv)
            assert refused['state'] == 'invalid_command' and not growth_ledger.ledger_path(VAULT).exists(), refused
        """)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink venv execution')
    def test_command_check_preserves_posix_venv_entrypoint(self):
        self.check_case("""
            import subprocess, venv
            venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=True).create(VAULT.root / '.venv')
            executable = VAULT.root / '.venv/bin/python'
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
            expected = [str(VAULT.root / '.venv'), 'venv-only']
            original = subprocess.run([str(executable), '-c', probe], cwd=VAULT.root,
                                      capture_output=True, text=True, check=True)
            assert json.loads(original.stdout) == expected
            for entry in (str(executable), '.venv/bin/python'):
                checked = worker_process.check_command(VAULT, [entry])
                observed = subprocess.run([checked['executable'], '-c', probe], cwd=VAULT.root,
                                          capture_output=True, text=True)
                assert observed.returncode == 0, observed.stderr
                assert json.loads(observed.stdout) == expected
                assert checked['executable'] == str(executable)
            node('A')
            result = growth.run(VAULT, [str(executable), '-c', packet_worker(
                "import venv_probe; assert venv_probe.VALUE == 'venv-only'")], limit=1)
            assert result['ok'], (result, (VAULT.root / result['output'] / 'stderr.txt').read_text())
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
            worker(VAULT.root / 'worker', 'vault-root')
            worker(VAULT.root / 'bin/worker', 'vault-bin')
            worker(VAULT.root / 'bin/vault-only', 'vault-only')
            with tempfile.TemporaryDirectory(dir=VAULT.root.parent) as directory:
                caller = Path(directory)
                worker(caller / 'worker', 'caller-root')
                worker(caller / 'bin/worker', 'caller-bin')
                worker(caller / 'bin/caller-only', 'caller-only')
                os.chdir(caller)
                try:
                    for path, label in (('bin','vault-bin'), ('','vault-root'),
                                        (':bin','vault-root'), ('missing:bin','vault-bin'),
                                        (str(VAULT.root / 'bin'),'vault-bin')):
                        with patch.dict(os.environ, {'PATH':path}):
                            original = subprocess.run(['worker'], cwd=VAULT.root,
                                capture_output=True, text=True, check=True)
                            assert original.stdout.strip() == label
                            checked = worker_process.check_command(VAULT, ['worker'])
                            assert checked['ok'], checked
                            actual = subprocess.run([checked['executable']], cwd=VAULT.root,
                                capture_output=True, text=True, check=True)
                            assert actual.stdout == original.stdout, (path, checked, actual.stdout)
                            assert Path.cwd() == caller
                    with patch.dict(os.environ, {'PATH':'bin'}):
                        assert worker_process.check_command(VAULT, ['vault-only'])['ok']
                        assert not worker_process.check_command(VAULT, ['caller-only'])['ok']
                finally:
                    os.chdir(VAULT.root)
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
            result = growth.run(VAULT, [sys.executable, '-c', packet_worker(), '--model', 'gpt-5.6-sol'], limit=2)
            assert result['ok'], result
            text = (VAULT.root / result['output'] / 'prompt.txt').read_text(encoding='utf-8')
            assert 'Every create_node uses drafter "gpt-5.6-sol"' in text, text[:600]
            assert 'not notes about this review' in text and 'such as -2' in text
            assert [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]['drafter'] == 'gpt-5.6-sol'
        """)

    def test_reviews_keep_current_claims_and_bind_existing_ones_by_evidence_line(self):
        # 2026-10-05: an old round must not roll back a newer claim, and a round that only
        # supports a held claim gets a `# 관측` line instead of a rewritten body.
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            worker = growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            assert 'never replace, weaken or reorder a current claim' in worker
            assert '"# 관측" section' in worker and '42cf0fc0#1' in worker
            path = VAULT.root / 'held.jsonl'
            rows = [{'type':'user','sessionId':'held','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'held','uuid':'a1','message':{'role':'assistant','id':'m1',
                     'content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r) + '\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','held',str(path),'held-project',space='00_Scope/W1')['ok']
            own = integration.prompt(VAULT, 'claude','held')['text']
            assert '현행 주장을 옛 내용으로 바꾸거나 약화·재배열하지 않는다' in own
            assert '`# 관측` 절' in own and '`42cf0fc0#1`' in own
        """)

    def test_worker_hooks_do_not_feed_maintenance_into_new_conversations(self):
        self.check_case("""
            node('A')
            change = "import os,subprocess; assert os.environ.get('OSK_GROWTH_WORKER')=='1'; hooks=Path(growth.__file__).resolve().parents[1]/'scripts/hooks'; results=[subprocess.run([sys.executable,'-B',str(hooks/name)],input=b'{}',capture_output=True,timeout=20) for name in ('claude_session_start.py','claude_prompt_submit.py','capture_stop.py')]; assert all(r.returncode==0 and not r.stdout and not r.stderr for r in results),results"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker('from pathlib import Path; '+change)],limit=2)
            assert result['ok'], result
            assert not growth.run(VAULT, ['unused-command'])['selected']
        """)

    def test_new_source_and_content_changes_reopen_comparisons(self):
        self.check_case("""
            first = node('A')
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            node('B')
            candidates = growth_plan.plan(VAULT)['candidates']
            assert any({s['name'] for s in c['sources']} == {'A','B'} for c in candidates)
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            result = write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            assert result['ok'], result
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_cross_scope_old_new_and_completed_batch_progress(self):
        self.check_case("""
            for name in ('A','B','C','D','E'):
                node(name)
            first = growth_plan.plan(VAULT, 1)
            assert len(first['candidates']) == 1
            manifest = register(first)
            growth_reviews.review(VAULT, first['candidates'][0]['key'], 'no_value', reason='No value in first batch', manifest=manifest['rid'])
            second = growth_plan.plan(VAULT, 1)
            assert second['candidates'][0]['key'] != first['candidates'][0]['key']
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
            node('Z', 'W2')
            candidates = growth_plan.plan(VAULT, 20)['candidates']
            assert any({'A','Z'} <= {s['name'] for s in c['sources']} for c in candidates)
            assert all(len(c['sources']) <= 8 for c in candidates)
            no_value_all()
            assert not growth_plan.plan(VAULT)['candidates']
        """)

    def test_stale_manifest_and_unrecorded_key_are_rejected(self):
        self.check_case("""
            node('A')
            candidate = growth_plan.plan(VAULT)['candidates'][0]
            try:
                growth_reviews.review(VAULT, candidate['key'], 'no_value', reason='Not registered', manifest='unknown')
                raise AssertionError('unrecorded review accepted')
            except ValueError:
                pass
            manifest = register()
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='Changed after selection')
            # A decision on the selected sources is the reviewer's report and is kept, but it does
            # not complete a comparison whose sources changed; those form a new comparison.
            for outcome in ('no_value','deferred'):
                growth_reviews.review(VAULT, candidate['key'], outcome, reason='Stale', manifest=manifest['rid'])
            assert not growth_plan.completed(VAULT, candidate['key'], growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT)['candidates']}
        """)

    def test_process_exit_alone_is_not_receipt(self):
        self.check_case("""
            node('A')
            for code in (0, 7):
                result = growth.run(VAULT, [sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(' + str(code) + ')'])
                assert not result['ok'], result
                assert result['returncode'] == code
                assert set(result['outcomes'].values()) == {'pending'}
                assert growth_plan.plan(VAULT)['candidates']
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r.get('kind') == 'review']
        """)

    def test_worker_receipt_and_deferred_next_run(self):
        self.check_case("""
            node('A')
            worker = "import sys; from osk import core,growth, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); assert sys.stdin.read(); plans=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r.get('kind')=='plan']; [growth_reviews.review(VAULT, c['key'], 'deferred', reason='Need more evidence',manifest=plans[-1]['rid']) for c in plans[-1]['candidates']]"
            result = growth.run(VAULT, [sys.executable, '-c', worker])
            assert not result['ok'], result
            assert growth_plan.plan(VAULT)['candidates']
            worker = worker.replace("'deferred'", "'no_value'")
            worker += "; from osk import organization; [organization.review(VAULT, j['key'],j['scope'],'complete','The fixture group remains coherent.',after=organization.snapshot(VAULT, j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in plans[-1]['organization_jobs']]"
            result = growth.run(VAULT, [sys.executable, '-c', worker])
            assert result['ok'], result
            assert set(result['outcomes'].values()) == {'no_value'}
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_preserved_requires_current_distillation_evidence(self):
        self.check_case("""
            node('A')
            manifest = register()
            candidate = manifest['candidates'][0]
            try:
                growth_reviews.review(VAULT, candidate['key'], 'preserved', target='Invented', reason='A process said success',manifest=manifest['rid'])
                raise AssertionError('unverified preservation accepted')
            except ValueError:
                pass
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_deferred_candidates_rotate(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            tried = []
            for _ in range(4):
                selected = register(growth_plan.plan(VAULT, 1))
                candidate = selected['candidates'][0]
                assert candidate['key'] not in tried, tried
                tried.append(candidate['key'])
                growth_reviews.review(VAULT, candidate['key'], 'deferred', reason='Need independent corroboration', manifest=selected['rid'])
            assert growth_plan.plan(VAULT)['candidates']
        """)

    def test_new_versions_get_priority_without_starving_old_comparisons(self):
        self.check_case("""
            for name in ('A','B','C','D','E','F','G','H','I'):
                node(name)
            inventory = growth_plan.plan(VAULT, 20)
            register(dict(inventory, candidates=[c for c in inventory['candidates']
                                                if c['grouping']=='comparison']))
            # Sources were attempted in pairs, but individual batches remain untried.
            node('Z', 'W2')
            first = growth_plan.plan(VAULT, 2)
            assert any(s['name']=='Z' for s in first['candidates'][0]['sources']), first
            assert all(s['name']!='Z' for s in first['candidates'][1]['sources']), first
            assert growth_plan.plan(VAULT, 2)==first, 'preview consumed the new version'
            register(first)
            assert write.update_node(VAULT, 'I', old_text='I reusable observation', new_text='I changed')['ok']
            second = growth_plan.plan(VAULT, 2)
            assert any(s['name']=='I' for s in second['candidates'][0]['sources']), second
            assert all(s['name']!='I' for s in second['candidates'][1]['sources']), second
            register(second)
            picked = []
            for i in range(4):
                node('Z'+str(i), 'W2')  # continuous new content, limit=1 still rotates
                selected = register(growth_plan.plan(VAULT, 1))['candidates'][0]
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
                write.create_node(VAULT, **request)  # the user-only confirmation gate remains intact
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            manifest = register(growth_plan.plan(VAULT, 1))
            candidate = manifest['candidates'][0]
            selected = [s for s in candidate['sources'] if s['name'] in {'A','B'}]
            result = distillation.create_node(VAULT, 
                {'key':candidate['key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in selected],'hub':'Principles'},
                title='Shared rule',summary='Rule from A and B',body='A and B support this rule, with limited scope.',
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            receipt = growth_reviews.review(VAULT, candidate['key'],'preserved',target='Shared rule',
                reason='A and B support the rule; Noise and Other are unrelated and omitted.',manifest=manifest['rid'])
            actual = contract.parse(VAULT.root / '00_Domain/Principles/Shared rule.md').edges('derived-from')
            assert set(actual) == {s['name'] for s in selected}, actual
            assert len(receipt['omitted_sources']) == 2
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}
            distillation._job_path(VAULT, candidate['key']).unlink()
            assert candidate['key'] not in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}, 'shared receipt depended on local journal'
            write.update_node(VAULT, 'Shared rule',old_text='A and B support this rule, with limited scope.',new_text='Changed after completion.')
            assert candidate['key'] in {c['key'] for c in growth_plan.plan(VAULT, 20)['candidates']}
        """)

    def test_timeout_stops_owned_descendants(self):
        self.check_case("""
            import time
            node('A')
            marker = VAULT.root / 'late-child.txt'
            child = "import pathlib,time; time.sleep(4); pathlib.Path('late-child.txt').write_text('survived')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); sys.stdin.read(); time.sleep(20)"
            result = growth.run(VAULT, [sys.executable,'-c',parent],timeout=1)
            assert not result['ok'] and result['error'], result
            assert result['cleanup_error'] is None, result
            time.sleep(4)
            assert not marker.exists(), 'descendant survived runner timeout'
            assert growth_plan.plan(VAULT)['candidates']
        """)

    @unittest.skipUnless(os.name == 'nt', 'only Windows cleans the tree with taskkill /T')
    def test_tree_cleanup_fails_only_while_a_tree_process_runs(self):
        # taskkill /T kills leaves first. A venv launcher ends with its child before its own
        # turn, so the call fails although nothing of the tree is left running.
        self.check_case("""
            import subprocess
            from unittest.mock import patch
            from osk import native_cli
            # An older process naming the root as parent outlived an earlier owner of that pid.
            table = {10: (1, 'python.exe'), 11: (10, 'python.exe'), 12: (10, 'Taskmgr.exe'), 13: (11, 'conhost.exe')}
            with patch.object(native_cli, 'process_created', {10: 100, 11: 150, 12: 50, 13: 160}.get):
                assert set(worker_process._tree(table, {10: 100})) == {10, 11, 13}
            run = subprocess.run
            lost = subprocess.CompletedProcess([], 128, b'', b'ERROR: There is no running instance of the task.')
            def worker():
                return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=0x08000200)
            with patch.object(worker_process.subprocess, 'run', lambda argv, **kw: (run(argv, **kw), lost)[1]):
                assert worker_process.stop_tree(worker()) is None
            with patch.object(worker_process.subprocess, 'run', lambda argv, **kw: lost):  # kills nothing
                error = worker_process.stop_tree(worker())
            assert error and error.startswith('process-tree cleanup was not confirmed'), error
            # #231 review: after the snapshot the root starts a child and exits first, so taskkill
            # finds no root. The base interpreter keeps a venv launcher from between them, and the
            # child's own console keeps the root's conhost from outliving the root.
            late = ("import os,subprocess,sys; print(flush=True); sys.stdin.read(); print(subprocess.Popen("
                    "[sys.executable,'-c','import time; time.sleep(60)'],cwd=os.path.dirname(sys.executable),"
                    "creationflags=0x08000000).pid, flush=True)")
            root = subprocess.Popen([sys._base_executable, '-c', late], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=0x08000200)
            root.stdout.readline()
            child = []
            def vanish(argv, **kw):
                root.stdin.close()
                child.append(int(root.stdout.readline()))
                root.wait(10)
                return lost
            with patch.object(worker_process.subprocess, 'run', vanish):
                error = worker_process.stop_tree(root)
            run(['taskkill', '/PID', str(child[0]), '/F'], capture_output=True)
            assert error and error.startswith('process-tree cleanup was not confirmed'), error
        """)

    def test_previous_saved_output_is_available_without_duplicate_node(self):
        self.check_case("""
            from osk import distillation
            node('A')
            node('B')
            request = dict(title='Principles',summary='General knowledge',body='Reusable principles.',drafter='gpt-6-astra',space='00_Domain/Principles')
            try:
                write.create_node(VAULT, **request)
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            growth.run(VAULT, [sys.executable,'-c','import sys; sys.stdin.read()'],limit=1)
            first = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]
            candidate = first['candidates'][0]
            result = distillation.create_node(VAULT, 
                {'key':candidate['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in candidate['sources']],'hub':'Principles'},
                title='Retained rule',summary='Previously written rule',body='A and B support this bounded rule.',
                drafter='gpt-6-astra',space='00_Domain/Principles')
            assert result['ok'], result
            next_candidate = growth_plan.plan(VAULT, 1)['candidates'][0]
            assert next_candidate['previous_distillations'][0]['key'] == candidate['distill_key']
            worker = "import sys; from osk import core,growth,organization, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); sys.stdin.read(); p=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]; [growth_reviews.review(VAULT, c['key'],'preserved',target='Retained rule',reason='Existing saved result still covers A and B.',manifest=p['rid']) for c in p['candidates']]; [organization.review(VAULT, j['key'],j['scope'],'complete','Sources and local hub read; one coherent group.',after=organization.snapshot(VAULT, j['scope'])['snapshot'],checked=[{'unit':u['unit'],'reason':'Known fixture claim'} for u in j['review_units']]) for j in p['organization_jobs']]"
            outcome = growth.run(VAULT, [sys.executable,'-c',worker],limit=2)
            assert outcome['ok'], outcome
            assert len([1 for p,k in graph.Index(VAULT).nodes.values() if k[0]=='domain' and not placement.is_hub(p)]) == 1
        """)

    def test_short_finished_transcript_waits_for_its_own_fork(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            path = VAULT.root / 'native.jsonl'
            user = {'type':'user','sessionId':'short','uuid':'u1','message':{'role':'user','content':'Report this temporary completed job.'}}
            final = {'type':'assistant','sessionId':'short','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'Temporary job finished.'}],'stop_reason':'end_turn'}}
            path.write_text(json.dumps(user)+'\\n',encoding='utf-8')
            state = conversation_capture.capture(VAULT, 'claude','short',str(path),'short-project',space='00_Scope/W1')
            assert state['capture_pending'], state
            with path.open('a',encoding='utf-8') as f:
                f.write(json.dumps(final)+'\\n')
            # The daily run tracks the finished turn but has no copy of it to review.
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
            assert conversation_capture.status(VAULT, 'claude','short')['captured_rounds'] == 1
            # The conversation's own Stop fork holds the turn and reviews it.
            job = integration.prompt(VAULT, 'claude','short',include_organization=False)
            for change in (
                    "q['osk_reviews']['scope'][0]['through']='stale-snapshot'",
                    "q['osk_reviews']['scope'][0].update(outcome='preserved',targets=[{'key':'missing-proof'}])"):
                rejected = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],scope_job=job)
                assert not rejected['ok'] and rejected['final_reviews']['errors'], rejected
            worker = packet_worker()
            result = growth.run(VAULT, [sys.executable,'-c',worker],scope_job=job)
            assert result['ok'], result
            assert result['scope_selected'] == 1 and result['domain_selected'] == 0, result
            assert {r['status'] for r in result['scope_outcomes'].values()} == {'complete'}
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_daily_run_reads_an_ended_conversation_and_restarts_its_cadence(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            path = VAULT.root / 'ended.jsonl'
            rows = []
            for i in range(1, 4):
                rows += [{'type':'user','sessionId':'ended','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'ended','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','ended',str(path),'ended-project',space='00_Scope/W1')['ok']
            for _ in range(5):
                conversation_capture.tick(VAULT, 'claude','ended')
            # Still running: its turns stay with the conversation's own review cadence.
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
            old = time.time() - conversation_capture.ENDED_AFTER - 60
            os.utime(path, (old, old))
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['scope_selected'] == 1, result
            plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            job = growth._reading_plan(plan)['scope_jobs'][0]
            assert job['pending_refs'] == ['native:claude:ended:u' + str(i) for i in (1, 2, 3)], job
            assert 'read_command' not in job, job
            assert 'read_cited(ref=' in growth.prompt(VAULT, plan), 'a sandboxed worker reads through MCP'
            state = conversation_capture.status(VAULT, 'claude','ended')
            assert (state['reviewed_rounds'], state['pending']) == (3, False), state
            assert state['prompt_count'] == state['reviewed_prompt_count'] == 5, state
        """)

    def test_organization_packet_without_snapshot_refuses_a_unit_changed_after_reading(self):
        self.check_case("""
            node('A')
            assert 'Use osk MCP tools only' in growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            assert 'growth checkpoint --file' not in growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
            # The worker has no CLI snapshot. A unit it read that changes before the
            # runner applies the packet no longer matches its content key.
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/A.md'; "
                      "assert write.update_node(VAULT, 'A',body='Changed after the worker read it.',expect_hash=core.sha256_file(n))['ok']")
            stale = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed review unit' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert fresh['ok'], fresh
            assert {s['status'] for s in fresh['organization_outcomes'].values()} == {'complete'}, fresh
        """)

    def test_organization_packet_is_held_to_the_snapshot_in_its_plan(self):
        self.check_case("""
            node('A')
            node('B')
            # Another session swaps A's evidence after the plan. Unit keys cover bodies only,
            # so the planned snapshot is what tells the reviewed state from the current one.
            change = "from osk import write, cluster_gate, node_checks, node_moves, nodefile; assert write.update_node(VAULT, 'A',add_edges={'derived-from':'B'})['ok']"
            stale = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert any('changed after inspection' in e for e in stale['final_reviews']['errors']), stale
            assert {s['status'] for s in stale['organization_outcomes'].values()} != {'complete'}, stale
            fresh = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
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
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/B.md'; "
                      "assert write.update_node(VAULT, 'B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change, org='deferred', read="u['name']=='A'")],
                                limit=3)
            # B is also a Domain candidate's source, so that candidate is refused; only the
            # organization checkpoint is under test here.
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert {s['status'] for s in result['organization_outcomes'].values()} != {'complete'}, result
            from osk import organization
            nxt = organization.plan(VAULT, 'W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
            assert nxt.get('handoff') or nxt.get('coverage', {}).get('handoff'), nxt
        """)
        self.check_case("""
            node('A')
            node('B')
            # The worker also lists B, which it then corrected. B's judgment was of the old body,
            # so only A is kept; B is named back and goes to another reviewer.
            change = ("from osk import write, cluster_gate, node_checks, node_moves, nodefile; n=VAULT.root/'00_Scope/W1/B.md'; "
                      "assert write.update_node(VAULT, 'B',body='Corrected by the worker.',expect_hash=core.sha256_file(n))['ok']")
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change, org='deferred')], limit=3)
            errors = result['final_reviews']['errors']
            assert not [e for e in errors if e.startswith('Organization')], errors
            assert any('changed after reading' in n for n in result['final_reviews']['notes']), result
            from osk import organization
            nxt = organization.plan(VAULT, 'W1')
            assert not {u['name'] for u in nxt.get('review_units', [])} & {'A', 'B'}, nxt
        """)

    def test_prompt_units_carry_the_view_hash_read_node_returns(self):
        self.check_case("""
            # The worker sees only this prompt. On 2026-10-04 it compared view_hash with the sha256
            # inside each unit (a range content key) and deferred every range it had read.
            import mcp_server as M
            node('A')
            node('B')
            text = growth.prompt(VAULT, growth_plan.plan(VAULT, 3))
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
                write.create_node(VAULT, **request)
            except write.WriteError:
                pass
            assert write.create_node(VAULT, **request)['ok']
            change = "c=p['candidates'][0]; result=distillation.create_node(VAULT, {'key':c['distill_key'],'sources':[{'ref':s['id'],'hash':s['hash']} for s in c['sources']],'hub':'Principles'},title='Observed rule',summary='Rule from A and B',body='A and B support this bounded rule.',drafter='gpt-6-astra',space='00_Domain/Principles'); assert result['ok'],result; q['osk_reviews']['domain'][0].update(outcome='preserved',target='Observed rule',reason='A and B support this bounded rule.')"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=2)
            assert result['ok'], result
            assert set(result['domain_outcomes'].values()) == {'preserved'}, result
            assert result['final_reviews']['state'] == 'applied', result
            # Newly created Domain knowledge is pending its own organization
            # review; the Scope review cannot acknowledge it implicitly.
            pending = growth_plan.plan(VAULT)['organization_jobs']
            assert [j['scope'] for j in pending] == ['00_Domain/Principles'], pending
            reviewed = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=1)
            assert reviewed['ok'], reviewed
            assert growth.run(VAULT, ['unused-command'])['state'] == 'skipped'
        """)

    def test_final_provider_messages_accept_codex_and_claude(self):
        self.check_case("""
            for wrapper in ('codex','claude','claude_trailing','claude_two_turns'):
                node(wrapper)
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker(wrapper=wrapper)],limit=1)
                assert result['ok'], result
                assert result['final_reviews']['state'] == 'applied', result
        """)

    def test_tool_output_unfinished_and_failed_provider_packets_are_ignored(self):
        self.check_case("""
            node('A')
            for wrapper in ('codex_tool','codex_no_complete','codex_late_error','claude_tool','claude_error','claude_continued'):
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker(wrapper=wrapper)])
                assert not result['ok'], (wrapper,result)
                assert result['final_reviews']['state'] == 'rejected', (wrapper,result)
                assert set(result['domain_outcomes'].values()) == {'pending'}, result
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(code=7)])
            assert not result['ok'] and result['final_reviews']['state'] == 'not_applied', result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
        """)

    def test_a_wrong_packet_is_refused_whole_and_a_wrong_entry_only_itself(self):
        self.check_case("""
            node('A')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker("q['osk_reviews']['manifest']='old-manifest'")])
            assert not result['ok'] and result['final_reviews']['state'] == 'rejected', result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
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
                result = growth.run(VAULT, [sys.executable,'-c',packet_worker({change!r})])
                assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
                assert result['final_reviews']['errors'], result
                assert {{s['status'] for s in result['organization_outcomes'].values()}} == {{'complete'}}, result
                reviews = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
                assert bool(reviews) == {kept}, reviews
            """)
        # A decision with a needless part (a target on no_value) keeps its meaning; the part is
        # dropped and named in the notes.
        self.check_case("""
            node('A')
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker("q['osk_reviews']['domain'][0]['target']='Whatever'")])
            assert result['ok'], result
            assert any('target was dropped' in n for n in result['final_reviews']['notes']), result
            assert [r['outcome'] for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review'] == ['no_value']
        """)

    def test_a_report_from_an_older_selection_does_not_undo_a_newer_one(self):
        self.check_case("""
            node('A')
            old, new = register(), register()        # two selections of the same comparison
            key = old['candidates'][0]['key']
            assert key == new['candidates'][0]['key']
            growth_reviews.review(VAULT, key, 'no_value', reason='Decided on the later selection.', manifest=new['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            # The earlier selection's worker reports late. Its report is kept, but it does not
            # undo the later selection's decision (the growth ledger is causal, so a late append
            # would otherwise become the latest).
            growth_reviews.review(VAULT, key, 'deferred', reason='Late report from the earlier selection.', manifest=old['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT)), 'an older report reopened it'
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == 2
            # The same selection can still reopen its own decision on purpose.
            growth_reviews.review(VAULT, key, 'deferred', reason='Reopened within the later selection.', manifest=new['rid'])
            assert not growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
        """)

    def test_selections_made_apart_stay_undecided_until_a_later_one(self):
        self.check_case("""
            node('A')
            # Two devices select the same comparison before syncing. Neither selection follows
            # the other, so their different reports leave it undecided (Mechanism §3 1) instead
            # of letting the larger RID win. A selection made after the merge decides it.
            base = growth_ledger.ledger_path(VAULT).read_bytes() if growth_ledger.ledger_path(VAULT).exists() else b''
            x = register()
            key = x['candidates'][0]['key']
            growth_reviews.review(VAULT, key, 'deferred', reason='Device X could not decide.', manifest=x['rid'])
            branch = growth_ledger.ledger_path(VAULT).read_bytes()[len(base):]
            growth_ledger.ledger_path(VAULT).write_bytes(base)
            y = register()
            assert y['candidates'][0]['key'] == key and core.rid_key(x['rid']) < core.rid_key(y['rid'])
            growth_reviews.review(VAULT, key, 'no_value', reason='Device Y found nothing to keep.', manifest=y['rid'])
            growth_ledger.ledger_path(VAULT).write_bytes(growth_ledger.ledger_path(VAULT).read_bytes() + branch)    # the sync merge
            rows = growth_ledger.ledger_records(VAULT)
            assert growth_ledger.latest_report(rows, key, 'key') is None
            assert not growth_plan.completed(VAULT, key, rows, graph.Index(VAULT)), 'the larger RID decided a fork'
            z = register()
            growth_reviews.review(VAULT, key, 'no_value', reason='Decided after the merge.', manifest=z['rid'])
            assert growth_plan.completed(VAULT, key, growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
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
            real, calls = growth_reviews.review, []
            def review(*a, **kw):
                calls.append(kw.get('key', a[0] if a else None))
                if len(calls) == 2:
                    raise Crash()
                return real(*a, **kw)
            with patch.object(growth_reviews, 'review', side_effect=review):
                try:
                    growth.run(VAULT, [sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth_ledger.ledger_path(VAULT))
            reviews = [r for r in rows if r['kind']=='review']
            assert [r['key'] for r in reviews] == calls[:1], (reviews, calls)
            assert not [r for r in rows if r['kind']=='run']
            assert growth_plan.completed(VAULT, calls[0], growth_ledger.ledger_records(VAULT), graph.Index(VAULT))
            keys = [c['key'] for c in growth_plan.plan(VAULT, 3)['candidates']]
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
                    growth.run(VAULT, [sys.executable,'-c',packet_worker()], limit=3)
                    raise AssertionError('the run went on after the runner died')
                except Crash:
                    pass
            rows = core.ledger_read(growth_ledger.ledger_path(VAULT))
            assert [r for r in rows if r['kind']=='review'] and not [r for r in rows if r['kind']=='run'], rows
            again = growth_plan.plan(VAULT, 3)
            assert not again['candidates'], again['candidates']
            assert not again['organization_jobs'], again['organization_jobs']
        """)

    def test_final_packet_saved_declaration_requires_actual_receipt(self):
        self.check_case("""
            node('A')
            change = "q['osk_reviews']['domain'][0].update(outcome='preserved',target='Missing rule')"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)])
            assert not result['ok'] and result['final_reviews']['state'] == 'incomplete', result
            assert result['final_reviews']['errors'], result
            assert not [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']
        """)

    def test_final_packet_does_not_duplicate_existing_valid_review(self):
        self.check_case("""
            node('A')
            node('B','W2')
            change = "growth_reviews.review(VAULT, **q['osk_reviews']['domain'][0],manifest=p['rid'])"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker(change)],limit=3)
            assert result['ok'], result
            assert 'already_complete' in result['final_reviews']['domain'].values(), result
            assert len([r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='review']) == result['domain_selected'] == 2
        """)

    def test_total_budget_and_queue_rotation_do_not_ack_unselected_scope(self):
        self.check_case("""
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            path = VAULT.root / 'native.jsonl'
            rows = []
            for i in range(12):
                rows += [{'type':'user','sessionId':'bounded','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'bounded','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(row)+'\\n' for row in rows),encoding='utf-8')
            conversation_capture.capture(VAULT, 'claude','bounded',str(path),'bounded-project',space='00_Scope/W1')
            legacy('bounded', path)
            original = conversation_capture.status(VAULT, 'claude','bounded')
            seen = []
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']  # no ACKs
            for i in range(3):
                result = growth.run(VAULT, worker,limit=1)
                assert result['selected'] == 1 and not result['ok'], result
                plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                queue = next(k for k in growth_ledger.QUEUES if plan[k])
                seen.append(queue)
                if plan['scope_jobs']:
                    job = plan['scope_jobs'][0]
                    assert len(job['pending_refs']) == 9 and job['remaining_rounds'] == 3, job  # 9 rounds a job (#175)
                    assert job['through'] != original['through']
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert 'prompt' not in visible and 'last_review' not in visible
                    assert visible['through'] == job['through'] and visible['pending_refs'] == job['pending_refs']
                    job['last_review'] = {'through':job['through'], 'outcome':'deferred', 'reason':'Search CACHE_LIMIT retest first.'}
                    visible = growth._reading_plan(plan)['scope_jobs'][0]
                    assert visible['previous_deferral']['reason'] == job['last_review']['reason']
            assert seen == ['candidates', 'scope_jobs', 'organization_jobs'], seen
            assert conversation_capture.status(VAULT, 'claude','bounded')['pending_refs'] == original['pending_refs']
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker()],limit=3)
            assert result['ok'] and result['selected'] == 3, result
            state = conversation_capture.status(VAULT, 'claude','bounded')
            assert len(state['pending_refs']) == 3, state  # selected nine only, not the whole conversation
        """)

    def test_scope_queue_goes_by_turn_time_and_passes_a_stalled_conversation(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            # Two ended conversations compete for one Scope slot per run (limit=3 shares the
            # budget with the candidate and organization queues). 'early' holds the older turns
            # although its state was touched last, so time order picks it first. A run that ends
            # without an ACK leaves it in place; the next run gives 'late' its turn instead of
            # choosing 'early' again (#136 M4: six such runs all chose the first conversation).
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            for n, (sid, day) in enumerate((('late', '02'), ('early', '01'))):
                path = VAULT.root / (sid + '.jsonl')
                rows = []
                for i in range(1, 3):
                    rows += [{'type':'user','sessionId':sid,'uuid':sid+'-u'+str(i),'timestamp':'2026-10-'+day+'T0'+str(i)+':00:00Z','message':{'role':'user','content':'question '+str(i)}},
                             {'type':'assistant','sessionId':sid,'uuid':sid+'-a'+str(i),'message':{'role':'assistant','id':sid+'-m'+str(i),'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude',sid,str(path),sid+'-project',space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
                os.utime(conversation_state.state_path(VAULT, 'claude', sid), (ended + n, ended + n))
            worker = [sys.executable,'-c','import sys; sys.stdin.read()']   # no ACKs
            picks = []
            for _ in range(3):
                result = growth.run(VAULT, worker, limit=3)
                assert not result['ok'], result
                plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                picks.append(plan['scope_jobs'][0]['conversation_id'])
            assert picks == ['early', 'late', 'early'], picks
        """)

    def test_conversations_that_yield_no_job_do_not_hold_the_catch_up_slots(self):
        self.check_case("""
            import os, time
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            os.environ['CODEX_HOME'] = str(VAULT.root / 'codex-home')    # never the real sessions
            # Codex conversations whose original transcripts are gone still date their next turn
            # from the stored UUIDv7 turn ids, so they sort first, yet catch-up cannot build jobs
            # from them. Catch-up walks the whole sorted queue to the next conversation that yields
            # a job: neither the run limit nor a lookup cap lets them hold the slots (#152 review:
            # with nothing else queued, run after run reviewed nothing and B waited).
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            path = VAULT.root / 'b.jsonl'
            rows = [{'type':'user','sessionId':'b','uuid':'b-u1','timestamp':'2026-10-02T01:00:00Z','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'b','uuid':'b-a1','message':{'role':'assistant','id':'b-m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','b',str(path),'b-project',space='00_Scope/W1')['ok']
            os.utime(path, (ended, ended))
            for i in range(101):
                sid, turn = 'lost' + str(i), '01900000-0000-7000-8000-%012d' % i
                with conversation_state.state_lock(VAULT, 'codex', sid) as p:
                    s = conversation_state.load_state(VAULT, p, 'codex', sid)
                    s.update(session='lost-project', space='00_Scope/W1', prompt_count=1,
                             transcript_path=str(VAULT.root / 'gone.jsonl'),
                             rounds=[{'id':sid+'-u1:'+turn, 'ref':'native:codex:'+sid+':'+turn,
                                      'hash':'sha256:' + '0'*64, 'completion':'completed'}])
                    conversation_state.save_state(p, s)
            first = [x['conversation_id'] for x in integration._known_pending(VAULT, 100)[0]]
            assert len(first) == 100 and 'b' not in first, first[-3:]
            result = growth.run(VAULT, [sys.executable,'-c','import sys; sys.stdin.read()'], limit=1)
            assert result.get('state') != 'capture_pending', result
            plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['conversation_id'] for j in plan['scope_jobs']] == ['b'], plan['scope_jobs']
            assert conversation_capture.status(VAULT, 'codex','lost0')['capture_error'], 'the failure stays visible'
        """)

    def test_conversation_review_carries_its_scope_hub_tree(self):
        self.check_case("""
            from osk import integration, organization, conversation_capture, conversation_cite, conversation_review, conversation_state
            node('A')
            # Mechanism §9-4 3: the conversation review decides node splitting and placement and
            # carries its scope's hub tree; hub differentiation is left to the organization review.
            path = VAULT.root / 'native.jsonl'
            rows = [{'type':'user','sessionId':'tree','uuid':'u1','message':{'role':'user','content':'question'}},
                    {'type':'assistant','sessionId':'tree','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','tree',str(path),'tree-project',space='00_Scope/W1')['ok']
            job = integration.prompt(VAULT, 'claude','tree')
            tree = organization.hub_tree(VAULT, organization.scope_path(VAULT, 'W1'))
            assert job['hub_tree'] == tree and tree.startswith('- 00_Scope/W1 · 직속 노드 '), job['hub_tree']
            assert '이 scope의 허브 트리:' in job['text'] and tree in job['text']
            assert '하위 군집을 열지 않는다' in job['text']
            # Bylaws §3 8 (#273 question 3): other scopes' and Domain claims reach the review only by search.
            assert '노드를 새로 쓰기 전에 반드시 그 주제로 search' in job['text']
            reading = growth._reading_plan({'scope_jobs': [job], 'organization_jobs': []})
            assert reading['scope_jobs'][0]['hub_tree'] == tree
            # The packet drops the hook text, so the range reaches a Stop fork as a field (#226).
            assert job['range'] == '1번째 턴 1개(`u1`, 전체 1턴)', job.get('range')
            assert reading['scope_jobs'][0]['range'] == job['range']
            assert 'hub_tree' in growth.prompt(VAULT)
        """)

    def test_every_queue_and_its_later_candidate_get_turns_while_nothing_completes(self):
        self.check_case("""
            import os, time
            from unittest.mock import patch
            from osk import evictions, integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            # #136 M4 fixture: two candidates in each of the five queues and a worker that never
            # acknowledges or submits a packet. Within five runs of limit=3 every queue is chosen,
            # and so is the second candidate of every queue: a stalled first one holds no budget.
            node('A')
            node('B', 'W2')
            for title in ('R1', 'R2'):
                assert write.create_node(VAULT, title, title, title + ' relies on A.', 'gpt-6-astra',
                                         space='00_Scope/W1', edges={'derived-from': 'A'})['ok']
            write.update_node(VAULT, 'A', old_text='A reusable observation', new_text='A revised observation')
            for i in range(2):
                evictions.record_evict(VAULT, 'W1', 'session', 'Temporary observation ' + str(i))
            ended = time.time() - conversation_capture.ENDED_AFTER - 60
            for sid in ('c1', 'c2'):
                path = VAULT.root / (sid + '.jsonl')
                rows = [{'type':'user','sessionId':sid,'uuid':sid+'-u','message':{'role':'user','content':'question'}},
                        {'type':'assistant','sessionId':sid,'uuid':sid+'-a','message':{'role':'assistant','id':sid+'-m','content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}}]
                path.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
                assert conversation_capture.capture(VAULT, 'claude', sid, str(path), sid + '-project', space='00_Scope/W1')['ok']
                os.utime(path, (ended, ended))
            ident = {'candidates': 'key', 'scope_jobs': 'conversation_id', 'organization_jobs': 'scope',
                     'eviction_jobs': 'of', 'recheck_jobs': 'key'}
            assert set(ident) == set(growth_ledger.QUEUES)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']    # never acknowledges
            seen = {k: [] for k in ident}
            with patch.object(evictions, 'age_days', return_value=17):
                planned = growth_plan.plan(VAULT, 3)       # Scope jobs join at run time, from the conversations
                assert all(len(planned[k]) >= 2 for k in ident if k != 'scope_jobs'), planned
                assert len(integration._known_pending(VAULT, 3)[0]) == 2
                for _ in range(5):
                    result = growth.run(VAULT, worker, limit=3)
                    assert not result['ok'] and result['selected'] == 3, result
                    plan = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                    for k, field in ident.items():
                        seen[k] += [j[field] for j in plan[k]]
            assert all(len(set(v)) >= 2 for v in seen.values()), seen
        """)

    def test_only_dispatched_organization_jobs_advance_attempts(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import integration, conversation_capture, conversation_cite, conversation_review, conversation_state
            for name, scope in (('A','W1'),('B','W2'),('C','W3')):
                node(name, scope)
            path = VAULT.root / 'native.jsonl'
            rows = [
                {'type':'user','sessionId':'waiting','uuid':'u1','message':{'role':'user','content':'Keep this scoped fact.'}},
                {'type':'assistant','sessionId':'waiting','uuid':'a1','message':{'role':'assistant','id':'m1','content':[{'type':'text','text':'A bounded fact.'}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\\n' for r in rows),encoding='utf-8')
            assert conversation_capture.capture(VAULT, 'claude','waiting',str(path),'waiting',space='00_Scope/W1')['ok']
            legacy('waiting', path)
            worker = "import sys; from osk import core,growth,organization, growth_ledger, growth_plan, growth_reviews, worker_process; from osk.vault import Vault; VAULT = Vault.open(); sys.stdin.read(); p=[r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]; [organization.review(VAULT, j['key'],j['scope'],'deferred','Needs a later targeted review.') for j in p['organization_jobs']]"
            visited = []
            for i in range(8):
                before = organization.load_state(VAULT)['plans']
                # Inventories and previews must never create/refresh attempts.
                growth_plan.plan(VAULT, 3)
                assert organization.load_state(VAULT)['plans'] == before
                with patch.object(core, 'now_kst', return_value=f'2026-09-20T12:00:{i:02d}+09:00'):
                    result = growth.run(VAULT, [sys.executable,'-c',worker],limit=3)
                assert result['selected'] == 3 and not result['ok'], result
                manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                jobs = manifest['organization_jobs']
                assert len(jobs) == 1 and len(manifest['scope_jobs']) == len(manifest['candidates']) == 1, manifest
                selected = jobs[0]
                visited.append(selected['scope'])
                after = organization.load_state(VAULT)['plans']
                assert after[selected['key']]['last_attempt'] == f'2026-09-20T12:00:{i:02d}+09:00'
                assert {k:v for k,v in after.items() if v['scope'] != selected['scope']} == {
                    k:v for k,v in before.items() if v['scope'] != selected['scope']}, (before, after)
                assert organization.load_state(VAULT)['reviews'][selected['scope']]['outcome'] == 'deferred'
            assert visited == ['W1','W2','W3','W1','W2','W3','W1','W2'], visited
            assert conversation_capture.status(VAULT, 'claude','waiting')['reviewed_rounds'] == 0
        """)

    def test_stop_fork_skips_jobs_that_stalled_twice_this_week(self):
        # #259: a Stop fork attached the same organization job 8 times without progress.
        self.check_case("""
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            rows = []
            def run(days, job, made=False, **run_fields):
                queue = 'organization_jobs' if job['key'].startswith('org') else 'recheck_jobs'
                plan = {'kind':'plan','rid':'p%d' % len(rows),queue:[job]}
                rows.extend([plan, {'kind':'run','manifest':plan['rid'],'state':'incomplete','returncode':0,
                                    'at':(now - timedelta(days=days)).isoformat(timespec='seconds'),
                                    'progress':{'made':[job['key']] if made else [],'none':[] if made else [job['key']]},
                                    **run_fields}])
            a, b = {'key':'org-a','snapshot':'s1'}, {'key':'org-b','snapshot':'t1'}
            rc1 = {'key':'rc-1','node_state':'n1','target_state':'t1'}
            run(9, a); run(8, a)                                  # last week: not counted
            run(3, a); run(2, a)                                  # twice this week
            run(3, b); run(2, b, made=True); run(1, b)            # progress resets the count
            run(1, rc1); run(0, rc1)
            # The worker never reviewed these: refused by its limit, or the run failed (review of #268).
            c, d = {'key':'org-c'}, {'key':'org-d'}
            run(1, c, state='limited', returncode=1); run(0, c, state='limited', returncode=1)
            run(1, d, returncode=1); run(0, d, final_reviews={'state':'rejected','errors':['bad packet']})
            stalled = growth_plan.stalled(rows)
            assert stalled == {('org-a',None,None), ('rc-1','n1','t1')}, stalled
            assert growth_plan.skip_stalled([c, d], stalled, 2, {}) == [c, d]
            planned = {}
            # A stalled first candidate gives way to the next one (review of #268).
            assert growth_plan.skip_stalled([a, b], stalled, 1, planned) == [b], planned
            # Any write in the scope changes an organization snapshot, so the job stays stalled (#268).
            assert growth_plan.skip_stalled([dict(a, snapshot='s2'), b], stalled, 1, {}) == [b]
            assert growth_plan.skip_stalled([rc1, {'key':'rc-2'}, {'key':'rc-3'}], stalled, 1, planned) == [{'key':'rc-2'}]
            # A recheck keeps its key when the cited node or its basis changes: either state makes a new place.
            assert growth_plan.skip_stalled([dict(rc1, target_state='t2')], stalled, 1, planned) == [dict(rc1, target_state='t2')]
            assert growth_plan.skip_stalled([dict(rc1, node_state='n2')], stalled, 1, planned) == [dict(rc1, node_state='n2')]
            assert planned['stalled'] == ['org-a', 'rc-1'], planned
        """)

    def test_aged_evictions_get_bounded_slots_and_explicit_decisions(self):
        self.check_case("""
            from unittest.mock import patch
            from osk import evictions
            node('A')
            items = [evictions.record_evict(VAULT, 'W1', 'session', 'Temporary observation '+str(i)) for i in range(3)]
            assert not growth_plan.plan(VAULT)['eviction_jobs']  # Young entries retain the session-hook path.
            with patch.object(evictions, 'age_days', return_value=17):
                preview = growth_plan.plan(VAULT, 1)
                assert preview['eviction_jobs'][0]['of'] == items[0]['rid']
                worker = [sys.executable, '-B', '-c', 'import sys; sys.stdin.read()']
                selected = []
                for _ in range(3):
                    result = growth.run(VAULT, worker, limit=1)
                    assert result['selected'] == 1 and not result['ok'], result
                    manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
                    selected.extend(manifest['eviction_jobs'])
                assert len(selected) == 1 and selected[0]['of'] == items[0]['rid'], selected
                assert growth_plan.plan(VAULT, 1)['eviction_jobs'][0]['of'] == items[1]['rid']  # Unattempted work progresses.
                assert len(evictions.unsettled(VAULT)) == 3
                packet = {'osk_reviews': {'manifest':manifest['rid'], 'domain':[], 'scope':[],
                          'eviction':[{'of':items[0]['rid'],'outcome':'discarded',
                                       'reason':'Only the completed fixture job counter; no durable claim.'}]}}
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len(evictions.unsettled(VAULT)) == 2
                assert growth_plan.eviction_status(VAULT, selected[0])['status'] == 'complete'
                assert growth_reviews.checkpoint(VAULT, packet)['ok']
                assert len([r for r in evictions.records(VAULT) if r['kind']=='settle']) == 1
                packet['osk_reviews']['eviction'][0]['of'] = items[1]['rid']
                refused = growth_reviews.checkpoint(VAULT, packet)
                assert not refused['ok'] and any('unselected eviction review' in e for e in refused['errors']), refused
                assert len([r for r in evictions.records(VAULT) if r['kind']=='settle']) == 1
        """)


if __name__ == '__main__':
    unittest.main()
