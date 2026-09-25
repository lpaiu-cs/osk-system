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
    source += "from osk import organization; q['osk_reviews']['organization']=[{'key':j['key'],'scope':j['scope'],'outcome':'complete','reason':'The fixture is one coherent, directly wired group.','after':organization.snapshot(j['scope'])['snapshot'],'intentional':[],'checked':[{'unit':u['unit'],'reason':'Known fixture claim and conditions'} for u in j['review_units']]} for j in p['organization_jobs']]; "
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
            try:
                growth.checkpoint(packet)
                raise AssertionError('unselected checkpoint accepted')
            except ValueError:
                pass
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
            register(planned)
            assert growth.daily_active()
            write.update_node('B', add_edges={'derived-from': 'A'})
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'complete'
            assert not growth.plan(3)['recheck_jobs']
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
                              add_edges={'derived-from': 'A'})
            planned = growth.plan(3)
            jobs = planned['recheck_jobs']
            assert [(j['node'], j['cascade']) for j in jobs] == [('C', True)], jobs
            manifest = register({**planned, 'scope_jobs': []})   # as run() records it
            packet = {'osk_reviews': {'manifest': manifest['rid'], 'domain': [], 'scope': [],
                      'recheck': [{'key': jobs[0]['key'], 'outcome': 'unchanged',
                                   'reason': 'x', 'proposal': 'y'}]}}
            try:
                growth.checkpoint(packet)
                raise AssertionError('a recheck review closed a check without update_node')
            except ValueError:
                pass
            packet['osk_reviews']['recheck'][0]['outcome'] = 'escalated'
            assert growth.checkpoint(packet)['ok']
            assert growth._recheck_status(jobs[0], graph.Index())['status'] == 'complete'
            assert not growth.plan(3)['recheck_jobs'], 'an escalated check returned to the agent queue'
            held = rechecks.report(graph.Index()).get('escalated')
            assert [h['node'] for h in held] == ['C'] and held[0]['proposal'] == 'y', held
            write.update_node('C', add_edges={'derived-from': 'B'})   # the user's decision
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
                              add_edges={'derived-from': 'A'})
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
                for bad in ([], 'codex', ['codex', 'bad' + chr(0)]):
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

    def test_short_finished_transcript_runs_same_actor_without_domain_candidates(self):
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
                try:
                    growth.checkpoint(packet)
                    raise AssertionError('unselected eviction accepted')
                except ValueError:
                    pass
        """)


if __name__ == '__main__':
    unittest.main()
