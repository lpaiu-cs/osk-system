"""Anonymous fixed evidence and real-file, read-only growth status regressions."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = Path(__file__).resolve().parent.parent
BOOT = '''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from osk import core, growth, growth_status as S, integration, validate, write
validate.make_mini_vault(core.ROOT)
(core.ROOT / '.git').mkdir(exist_ok=True)
START = '2026-10-01T00:00:00+09:00'
AT = '2026-10-01T12:00:00+09:00'
END = '2026-10-02T00:00:00+09:00'
def cursor(sid, rounds=0, reviewed=0, **extra):
    p = integration.state_path('claude', sid)
    state = integration._load(p, 'claude', sid)
    state.update(rounds=[{'id':str(n),'ref':'anonymous#'+str(n),'completion':'complete'} for n in range(rounds)],
                 reviewed_count=reviewed, **extra)
    integration._save(p,state)
    return p,state
def report():
    return S.report(since=START,until=END)
def files():
    return {str(p.relative_to(core.ROOT)):(p.read_bytes(),p.stat().st_mtime_ns)
            for p in core.ROOT.rglob('*') if p.is_file()}
'''


class GrowthStatusTests(unittest.TestCase):
    def check_case(self, code):
        with tempfile.TemporaryDirectory(prefix='osk-status-test-') as folder:
            # The subprocess and its cursors belong only to this disposable vault.
            env = {**os.environ, 'OSK_VAULT_ROOT': folder, 'PYTHONPATH': str(ENGINE),
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
            result = subprocess.run([sys.executable, '-B', '-c', BOOT + textwrap.dedent(code)],
                                    cwd=folder, env=env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_denominators_do_not_count_uncaptured_tails_or_repairs_twice(self):
        self.check_case('''
            cursor('a',4,2,capture_pending=True,capture_error='native source missing',
                   repair_pending={'repair':{'refs':['anonymous#0','anonymous#2']}})
            cursor('b',capture_pending=True)
            result=report()
            assert result['ok'],result
            counts=result['integration']
            assert counts['conversations']==2 and counts['captured_rounds']==4,counts
            assert counts['unreviewed_rounds']==2 and counts['repair_rounds']==2,counts
            assert counts['review_pending_rounds']==3,counts
            assert counts['capture_pending_conversations']==2,counts
            assert counts['conversations_with_review_pending']==1,counts
            assert counts['uncaptured_rounds'] is None,counts
            assert result['meaning']=={'ok':'report sections were read, not growth success',
                 'semantic_growth':'not_measured','downstream_reuse':'not_measured','autonomy':'not_inferred'}
        ''')

    def test_recorded_exit_success_empty_selection_and_dispositions_are_distinct(self):
        self.check_case('''
            rows=[]
            triggers=[None,'manual','scheduled','user_request','stop_hook','unknown']
            for n,trigger in enumerate(triggers):
                plan={'kind':'plan','rid':'p'+str(n),'work_context':'daily','at':AT}
                if trigger is not None: plan['invocation']=trigger
                rows += [plan,{'kind':'run','rid':'r'+str(n),'manifest':plan['rid'],'at':AT,
                    'selected':0 if n==1 else 1,'returncode':0,'ok':n in (1,2),
                    'state':'complete' if n in (1,2) else 'incomplete',
                    'recheck_outcomes':{'removed':{'status':'complete','reason':'basis removed'},
                        'gone':{'status':'complete','reason':'node gone'},
                        'dangling':{'status':'complete','reason':'basis dangling'},
                        'held':{'status':'complete','reason':'escalated to the user'}}}]
            rows += [{'kind':'plan','rid':'legacy-stop','work_context':'stop:W1','at':AT},
                     {'kind':'run','rid':'last','manifest':'legacy-stop','at':AT,'selected':1,'returncode':1,'ok':False},
                     {'kind':'plan','rid':'unfinished','work_context':'daily','at':AT}]
            out=S._runs(rows,S._time(START),S._time(END))
            assert out['by_invocation']=={'unknown':2,'manual':1,'scheduled':1,'user_request':1,
                                          'stop_hook':1,'legacy_stop_context':1},out
            assert out['native_exit_zero']==6 and out['recorded_nonempty_complete']==1,out
            assert out['empty_selection']==1 and out['unfinished_plan_ids']==['unfinished'],out
            assert out['items'][0]['engine_rev'] is None,out
            assert out['items'][0]['queues']['recheck']=={
                'basis_removed':1,'node_gone':1,'basis_dangling':1,'escalated':1},out
        ''')

    def test_recheck_history_separates_baselines_carry_and_direct_judgments(self):
        self.check_case('''
            from osk import rechecks
            values=[('bound',rechecks.BASELINE),('bound',None),('unchanged',rechecks.CARRIED),
                    ('unchanged',None),('updated',None)]
            rows=[{'rid':str(n),'at':AT,'result':result,'reason':reason}
                  for n,(result,reason) in enumerate(values)]
            rows += [{'rid':'unclocked','result':'updated'},
                     {'rid':'boundary','result':'updated','at':END}]
            out=S._recheck_history(rows,S._time(START),S._time(END))
            assert out['by_category']=={'baseline':1,'binding':1,'carried':1,
                                       'explicit_unchanged':1,'explicit_updated':1},out
            assert out['records']==5 and out['outside_window']==1,out
            assert out['unknown_time']==['unclocked'],out
        ''')

    def test_eviction_reports_real_review_settlement_and_completion_separately(self):
        self.check_case('''
            from osk import evictions
            saved=write.create_node('Retained','retained','A reusable claim.','test',space='00_Scope/W1')
            assert saved['ok'],saved
            cases={'node':'node','merged':'merged','discarded':'discarded','deferred':'deferred',
                   'unreviewed':None,'settled_unreviewed':None,'settled_deferred':'deferred'}
            with patch.object(core,'now_iso',return_value=AT):
                jobs={}
                for name in cases:
                    row=evictions.record_evict('W1','test','Anonymous source '+name)
                    jobs[name]={'key':'eviction:'+row['rid'],'of':row['rid'],
                                'scope':row['scope'],'text':row['text']}
                plan=core.ledger_append(growth.LEDGER,{'kind':'plan','candidates':[],
                    'scope_jobs':[],'eviction_jobs':list(jobs.values())})
                evictions.settle(jobs['settled_unreviewed']['of'],'merged',target='Retained')
                evictions.settle(jobs['settled_deferred']['of'],'node',target='Retained')
                reviews=[]
                for name,outcome in cases.items():
                    if outcome is None: continue
                    entry={'of':jobs[name]['of'],'outcome':outcome,'reason':'Fixture review decision.'}
                    if outcome in ('node','merged'): entry['target']='Retained'
                    reviews.append(entry)
                done=growth.checkpoint({'osk_reviews':{'manifest':plan['rid'],'domain':[],
                                       'scope':[],'eviction':reviews}})
                assert done['ok'],done
                outcomes={job['key']:growth._eviction_status(job) for job in jobs.values()}
                core.ledger_append(growth.LEDGER,{'kind':'run','manifest':plan['rid'],
                                   'selected':len(jobs),'ok':False,'eviction_outcomes':outcomes})
            with patch.object(core,'now_iso',return_value=END):
                core.ledger_append(growth.LEDGER,{'kind':'run','manifest':plan['rid'],
                                   'selected':len(jobs),'ok':False,'eviction_outcomes':outcomes})
            before=files()
            result=report()
            assert result['ok'],result
            runs=result['runs']
            assert runs['runs']==1 and runs['outside_window']==1,runs
            assert runs['items'][0]['queues']['eviction']=={
                'by_status':{'complete':3,'pending':4},
                'by_review':{'node':1,'merged':1,'discarded':1,'deferred':2,'unreviewed':2},
                'by_settlement':{'node':2,'merged':2,'discarded':1,'unsettled':2}},runs
            assert before==files(),'eviction report changed evidence'
        ''')

    def test_cursor_enumeration_failure_is_unavailable_not_empty(self):
        self.check_case('''
            empty=report()
            assert empty['ok'] and empty['integration']['conversations']==0,empty
            path,state=cursor('a',1,reviews=[{'through':'fixed','at':AT,'outcome':'no_value','receipts':[]}])
            before=files()
            def deny_listing(original):
                def read(directory):
                    if Path(directory)==path.parent:
                        raise PermissionError(13,'fixture directory listing denied',str(directory))
                    return original(directory)
                return read
            # Exercise the OS boundary: glob suppresses scandir errors, while
            # an explicit directory read must propagate them on every platform.
            with patch.object(os,'scandir',side_effect=deny_listing(os.scandir)), \
                 patch.object(os,'listdir',side_effect=deny_listing(os.listdir)):
                assert json.loads(path.read_text(encoding='utf-8'))==state
                result=report()
            assert not result['ok'] and result['integration'] is None,result
            assert result['reviews'] is None and result['preservation'] is None,result
            assert any(e['section']=='integration' and 'PermissionError' in e['error']
                       for e in result['errors']),result
            assert before==files(),'failed report changed evidence'
        ''')

    @unittest.skipUnless(os.name=='posix' and os.geteuid()!=0,
                         'requires unprivileged POSIX directory permissions')
    def test_cursor_directory_search_without_list_permission_fails_cli(self):
        self.check_case('''
            import subprocess
            path,state=cursor('a',1)
            before=files()
            original_mode=path.parent.stat().st_mode
            try:
                path.parent.chmod(0o111)
                assert json.loads(path.read_text(encoding='utf-8'))==state
                try: os.listdir(path.parent)
                except PermissionError: pass
                else: raise AssertionError('filesystem did not deny directory listing')
                result=report()
                assert not result['ok'] and result['integration'] is None,result
                proc=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','status'],
                                    capture_output=True,text=True,encoding='utf-8')
                assert proc.returncode==1,(proc.stdout,proc.stderr)
                output=json.loads(proc.stdout)
                assert output['integration'] is None and output['reviews'] is None,output
                assert output['preservation'] is None,output
            finally:
                path.parent.chmod(original_mode)
            assert before==files(),'permission failure changed evidence'
        ''')

    def test_read_only_repeatable_report_and_real_cli(self):
        self.check_case('''
            import subprocess
            cursor('a',1,reviews=[{'through':'frozen','at':AT,'outcome':'no_value','receipts':[]}])
            before=files()
            with patch.object(integration,'status',side_effect=AssertionError('status writes repairs')), \
                 patch.object(integration,'_save',side_effect=AssertionError('unexpected write')), \
                 patch.object(growth,'plan',side_effect=AssertionError('report selected work')), \
                 patch.object(core,'ledger_append',side_effect=AssertionError('unexpected ledger write')):
                first,second=report(),report()
            assert first['ok'],first
            assert first['reviews']['outcomes']=={'no_value':1},first
            first.pop('observation'); second.pop('observation')
            assert first==second
            assert before==files(),'report changed files or mtimes'
            proc=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','status','--since',START,'--until',END],
                                capture_output=True,text=True,encoding='utf-8')
            assert proc.returncode==0,(proc.stdout,proc.stderr)
            assert json.loads(proc.stdout)['integration']['captured_rounds']==1,proc.stdout
            assert before==files(),'CLI changed files or mtimes'
        ''')

    def test_saved_body_and_current_placement_are_independent(self):
        self.check_case('''
            from osk import distillation
            source=write.create_node('Source','source','Reusable source claim.','test',space='00_Scope/W1')
            result=distillation.create_node({'key':'fixed','sources':[source['id']],'hub':'W1'},
                title='Retained',summary='retained',body='A durable claim.',drafter='test',space='00_Scope/W1')
            receipt=result['distillation']
            assert receipt['status']=='complete',result
            cursor('a',1,1,reviews=[{'through':'frozen','at':AT,'outcome':'preserved','receipts':[receipt,receipt]}])
            good=report()['preservation']
            assert good['receipt_versions']==1 and good['placement']=={'complete':1},good
            hub=core.ROOT/'00_Scope/W1/W1.md'
            before=hub.read_text(encoding='utf-8')
            after=before.replace('[[Retained]]','').replace('[['+result['id']+']]','')
            assert before!=after,before
            hub.write_text(after,encoding='utf-8')
            partial=report()
            assert partial['ok'],partial
            proof=partial['preservation']
            assert proof['body_and_sources']=={'complete':1} and proof['placement']=={'pending':1},proof
            assert partial['reviews']['outcomes']=={'preserved':1},partial
        ''')

    def test_corrupt_state_and_ledger_are_unavailable_not_zero(self):
        self.check_case('''
            path,state=cursor('a',1)
            state['root']='another vault'
            path.write_text(json.dumps(state),encoding='utf-8')
            damaged=report()
            assert not damaged['ok'] and damaged['integration'] is None,damaged
            assert damaged['reviews'] is None and damaged['preservation'] is None,damaged
            path.unlink()
            growth.LEDGER.parent.mkdir(parents=True,exist_ok=True)
            growth.LEDGER.write_text('not json'+chr(10),encoding='utf-8')
            damaged=report()
            assert not damaged['ok'] and damaged['runs'] is None,damaged
        ''')

    def test_timezone_bounds_and_undated_evidence(self):
        self.check_case('''
            for kwargs in ({'since':'2026-10-01'},{'since':END,'until':START}):
                try: S.report(**kwargs)
                except ValueError: pass
                else: raise AssertionError(kwargs)
            out=S._window([{'rid':'equal','at':'2026-09-30T15:00:00Z'},
                           {'rid':'end','at':END},{'rid':'unknown','at':'not a time'}],
                          S._time(START),S._time(END))
            assert [x['rid'] for x in out['rows']]==['equal'],out
            assert out['outside_window']==1 and out['unknown_time']==['unknown'],out
        ''')

    def test_runtime_observer_and_mcp_version_are_not_conflated(self):
        self.check_case('''
            from osk import epoch, response_growth
            with patch.object(response_growth,'doctor',return_value={'verdict':{'mode':'foreground','reason':'login required'}}) as doctor:
                out=S.report(since=START,until=END,preflight=True)
            assert out['ok'],out
            assert doctor.call_count==2,doctor.call_count
            assert len(out['runtime']['cli_preflight'])==2,out
            assert out['runtime']['mcp'].startswith('not_observed'),out
            import mcp_server as M
            with patch.object(epoch,'loaded',return_value='old'),patch.object(epoch,'on_disk',return_value='new'):
                current=M._engine_state()
            assert current['engine_rev']=='old' and current['engine_disk_rev']=='new' and current['engine_stale'] is True,current
            assert S._time(current['engine_observed_at']).tzinfo is not None
            with patch.object(epoch,'on_disk',side_effect=epoch.EpochError('cannot read engine')):
                unknown=M._engine_state()
                broken=report()
            assert unknown['engine_stale'] is None and unknown['engine_disk_rev'] is None,unknown
            assert not broken['ok'] and broken['runtime'] is None,broken
        ''')

    def test_actual_run_records_explicit_invocation_and_loaded_engine(self):
        self.check_case('''
            import subprocess
            from osk import epoch
            result=write.create_node('Evidence','evidence','Reusable observation.','test',space='00_Scope/W1')
            assert result['ok'],result
            command_file=core.ROOT/'command.json'
            command_file.write_text(json.dumps([sys.executable,'-B','-c','import sys;sys.stdin.read()']),encoding='utf-8')
            proc=subprocess.run([sys.executable,'-B','-m','osk.cli','growth','run',
                                 '--command-file',str(command_file),'--invocation','user_request','--limit','1'],
                                capture_output=True,text=True,encoding='utf-8',timeout=30)
            assert proc.returncode==1,(proc.stdout,proc.stderr)
            assert json.loads(proc.stdout)['state']=='incomplete',proc.stdout
            plan=[r for r in growth._records() if r['kind']=='plan'][-1]
            assert plan['invocation']=='user_request' and plan['work_context']=='daily',plan
            assert plan['engine_rev']==epoch.loaded(),plan
            with patch.object(growth,'check_command',side_effect=AssertionError('launched invalid invocation')):
                try: growth.run(['worker'],invocation='invented')
                except ValueError: pass
                else: raise AssertionError('invalid invocation accepted')
        ''')

    def test_response_worker_records_hook_entry_without_claiming_user_intent(self):
        self.check_case('''
            from osk import response_growth
            source={'harness':'claude','conversation_id':'a','cwd':str(core.ROOT),
                    'transcript_path':'fixture','active':False}
            job={'harness':'claude','conversation_id':'a'}
            with patch.object(response_growth,'command',return_value=['worker']), \
                 patch.object(response_growth,'profile',return_value=source), \
                 patch.object(growth,'run',return_value={'ok':False}) as run:
                response_growth.run(source,job,'worker')
            assert run.call_args.kwargs['invocation']=='stop_hook',run.call_args
            assert run.call_args.kwargs['scope_job']==job,run.call_args
        ''')


if __name__ == '__main__':
    unittest.main()
