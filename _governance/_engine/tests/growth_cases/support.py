"""성장 실행의 수트의 바탕 — 임시 저장소를 세우고, 갈래들이 함께 쓰는 도우미를 둔다.

이 모듈을 읽는 것이 곧 격리다. 갈래 파일과 `tests/test_growth.py`은 엔진보다 이 모듈을 먼저 읽는다."""
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
BOOT = """
import json, sys
from pathlib import Path
from osk import cli, core, graph, growth, validate, write, contract, organization, cluster_gate, node_checks, node_moves, nodefile, growth_ledger, growth_plan, growth_reviews, worker_process, placement, topology
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
