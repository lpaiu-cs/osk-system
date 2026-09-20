"""Metadata, billing boundary and receipt tests; no provider requests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from osk import response_growth as rg
import test_growth as base_tests


class ResponseGrowthTests(unittest.TestCase):
    def test_only_successful_native_final_answers_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'native.jsonl'
            total = {'input_tokens': 500, 'cached_input_tokens': 400, 'output_tokens': 25}
            usage = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': total, 'last_token_usage': {'input_tokens': 500}}}}
            rows = [{'type': 'session_meta', 'payload': {'id': 'own', 'cwd': folder}},
                    {'type': 'turn_context', 'payload': {'model': 'same-model', 'effort': 'high'}},
                    usage, usage,
                    {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1', 'last_agent_message': 'done'}},
                    {'type': 'turn_context', 'payload': {'model': 'next-model', 'effort': 'low'}}]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"partial":', encoding='utf-8')
            result = rg.profile(str(path), 'codex', 'own')
            self.assertEqual(result['usage'], total)
            self.assertEqual(result['finals'], ['t1'])
            self.assertEqual(result['model'], 'same-model')
            self.assertTrue(result['active'])
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                rg.profile(str(path), 'codex', 'other')
            rows = rows[:-1] + [
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'failed'}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'failed','error':'failure','last_agent_message':'error reply'}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'aborted'}},
                {'type':'event_msg','payload':{'type':'turn_aborted','turn_id':'aborted'}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
            result = rg.profile(str(path), 'codex', 'own')
            self.assertEqual(result['finals'], ['t1'])
            self.assertFalse(result['last_successful'])

            def assistant(mid, stop='tool_use', **kwargs):
                return {'type': 'assistant', 'sessionId': 'own', **kwargs,
                        'message': {'id': mid, 'model': 'claude-same', 'stop_reason': stop,
                                    'content':[{'type':'text','text':'answer'}]}}
            rows = [assistant('m1'), assistant('m1'), assistant('m2', 'end_turn'),
                    assistant('side', 'end_turn', isSidechain=True), assistant('agent', agentId='worker')]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
            result = rg.profile(str(path), 'claude', 'own')
            self.assertEqual(result['finals'], ['m2'])
            self.assertFalse(result['active'])
            thinking = assistant('m3', 'end_turn')
            thinking['message']['content'] = [{'type':'thinking','thinking':'internal'}]
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(thinking) + '\n')
            self.assertTrue(rg.profile(str(path), 'claude', 'own')['active'])
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(assistant('m3', 'end_turn')) + '\n')
            self.assertEqual(rg.profile(str(path), 'claude', 'own')['finals'], ['m2','m3'])

    def test_subscription_gate_and_exact_model_no_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'cli.exe'
            exe.touch()
            source = {'harness': 'claude', 'conversation_id': 'own', 'model': 'claude-same',
                      'cwd': folder, 'version': '2.1.266'}
            env = {'CLAUDE_CONFIG_DIR': folder}
            def completed(text):
                return subprocess.CompletedProcess([], 0, text, '')
            def auth(subscription):
                return completed(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
                                             'subscriptionType': subscription}))
            with patch.object(rg.subprocess, 'run', side_effect=[completed('2.1.266'), auth(None)]):
                with self.assertRaisesRegex(ValueError, 'subscription login'):
                    rg.command(source, str(exe), env)
            with patch.object(rg.subprocess, 'run', side_effect=[completed('2.1.266'), auth('max')]):
                command = rg.command(source, str(exe), env)
            self.assertIn('--no-session-persistence', command)
            self.assertEqual(command[command.index('--model') + 1], source['model'])
            self.assertNotIn('--bare', command)
            with patch.object(rg.subprocess, 'run', return_value=completed('2.1.251')) as call:
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(source, str(exe), env)
                self.assertEqual(call.call_count, 1)
            (Path(folder) / 'settings.json').write_text('{"apiKeyHelper":"external-command"}', encoding='utf-8')
            with patch.object(rg.subprocess, 'run', return_value=completed('2.1.266')) as call:
                with self.assertRaisesRegex(ValueError, 'unverified API'):
                    rg.command(source, str(exe), env)
                self.assertEqual(call.call_count, 1)

            source.update(harness='codex', cli_version='0.155.0', model='parent-model', model_provider='openai',
                          effort='max', approval_policy='never', sandbox_policy={'type': 'read-only'})
            with patch.object(rg.subprocess, 'run', side_effect=[completed('codex-cli 0.155.0'), completed('Logged in using ChatGPT')]):
                command = rg.command(source, str(exe), env)
            self.assertIn('--ephemeral', command)
            self.assertEqual(command[-2:], ['own', '-'])
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            with patch.dict(rg.os.environ, {'OPENAI_API_KEY': 'fixture', 'ANTHROPIC_API_KEY': 'fixture'}):
                self.assertNotIn('OPENAI_API_KEY', rg.subscription_env())
                self.assertNotIn('ANTHROPIC_API_KEY', rg.subscription_env())

    def test_cumulative_fork_usage_is_not_child_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stdout.jsonl'
            path.write_text(json.dumps({'type': 'turn.completed', 'usage': {
                'input_tokens': 10000, 'cached_input_tokens': 7000, 'output_tokens': 100}}), encoding='utf-8')
            measured = rg.cache_usage(path, {'harness': 'codex', 'usage': {
                'input_tokens': 9000, 'cached_input_tokens': 7000, 'output_tokens': 90}})
            self.assertEqual(measured['input_tokens'], 1000)
            self.assertEqual(measured['cached_fraction'], 0)
            self.assertFalse(rg.cache_usage(path, {'harness': 'codex'})['measured'])

    def test_counter_survives_resume_and_worker_failure_does_not_ack(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch
            source = {'harness':'claude','conversation_id':'own','finals':[]}
            assert not rg.observe(source)['due']
            source['finals'] = [str(i) for i in range(9)]
            assert rg.observe(source)['due']
            assert rg.observe(source)['count'] == 9
            with patch.object(rg, 'run', return_value={'ok':False,'state':'incomplete'}):
                result = rg.attempt(source, {'pending_refs':['fixture'], 'capture_error':None}, 'unused')
            assert result['state'] == 'incomplete'
            assert not rg.observe(source)['due']
            assert integration.status('claude','own')['reviewed_rounds'] == 0
            source['finals'] = source['finals'][:8]
            try:
                rg.observe(source)
                assert False, 'truncated native history was accepted'
            except ValueError:
                pass
        ''')

    def test_prompt_does_not_count_and_ninth_stop_selects_only_own_nine_rounds(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace('own', '', space='= Scope/W1')
            native = core.ROOT / 'native.jsonl'
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'session':'own'}
            rg.initialize(env)
            def append(n):
                rows = [{'type':'user','sessionId':'own','uuid':f'u{n}', 'message':{'role':'user','content':f'question {n}'}},
                        {'type':'assistant','sessionId':'own','uuid':f'a{n}', 'message':{'role':'assistant','id':f'm{n}','model':'parent-model', 'stop_reason':'end_turn','content':[{'type':'text','text':f'answer {n}'}]}}]
                with native.open('a', encoding='utf-8') as f:
                    f.write(''.join(json.dumps(r)+'\\n' for r in rows))
            with patch.object(rg, 'run', return_value={'ok':False,'state':'incomplete'}) as worker, \
                    patch.object(scope_memory,'recovery_block',return_value='RECOVERY_SENTINEL'):
                for n in range(1, 10):
                    append(n)
                    hook.capture_block(env, 'own')
                    before = integration.status('claude','own')
                    assert before['prompt_count'] == 0
                    assert before['response_growth']['count'] == n-1, before
                    result = rg.process_stop(env)
                    assert worker.call_count == (1 if n == 9 else 0), result
                    rg.process_stop(env)  # duplicate notification must not count or launch
                assert worker.call_count == 1
                source, job, exe = worker.call_args.args
                assert len(job['pending_refs']) == 9 and job['conversation_id'] == 'own', job
                assert not job['organization_jobs']
                planned = {'scope_jobs':[job], 'candidates':[], 'organization_jobs':[]}
                assert 'RECOVERY_SENTINEL' in growth.prompt(planned)
                assert source['model'] == 'parent-model'
                rg.initialize(env)  # resume must not reset the durable clock
                status = integration.status('claude','own')
                assert status['response_growth']['count'] == 9 and status['reviewed_rounds'] == 0, status
                assert 'incomplete' in hook.capture_block(env, 'own', startup=True)
            source['finals'] += [str(n) for n in range(9,18)]
            with patch.object(rg, 'run', return_value={'ok':False,'state':'busy'}):
                assert rg.attempt(source,job,exe)['state'] == 'busy'
            assert rg.observe(source)['due'], 'busy worker consumed a due attempt'
        ''')

    def test_stop_detaches_and_waits_for_native_final_flush(self):
        base_tests.GrowthTests().check_case('''
            import os, subprocess, time
            from osk import response_growth as rg, integration, scope_memory
            scope_memory.replace(core.ROOT.name, '', space='= Scope/W1')
            native = core.ROOT / 'native.jsonl'
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native), 'cwd':str(core.ROOT)}
            rg.initialize(env)
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n')
            hook = Path(rg.__file__).resolve().parents[1]/'scripts/hooks/capture_stop.py'
            child_env = dict(os.environ)
            child_env.pop('OSK_GROWTH_WORKER',None)
            stop = subprocess.run([sys.executable,str(hook)],input=json.dumps(env).encode(),env=child_env,
                                  capture_output=True,timeout=10)
            assert stop.returncode == 0 and not stop.stdout, (stop.stdout,stop.stderr)
            # Completion cannot flush until the parent hook has returned.
            with native.open('a',encoding='utf-8') as f:
                f.write(json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                status = integration.status('claude','own')
                if status.get('response_growth_stop'):
                    break
                time.sleep(0.05)
            assert status['response_growth']['count'] == 1, status
            assert status['captured_rounds'] == 1 and status['reviewed_rounds'] == 0, status
            assert status['response_growth_stop']['state'] == 'not_due', status
        ''')

    def test_fork_supervisor_selects_only_own_snapshot_and_preserves_cwd(self):
        base_tests.GrowthTests().check_case('''
            from osk import integration, scope_memory
            from unittest.mock import patch
            node('Unrelated Domain candidate')
            scope_memory.replace('own', '', space='= Scope/W1')
            native = core.ROOT / 'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'one-off question'}}) + '\\n' +
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','stop_reason':'end_turn','content':[{'type':'text','text':'one-off answer'}]}}) + '\\n')
            assert integration.capture('claude','own',str(native),'own')['ok']
            job = integration.prompt('claude','own',include_organization=False,max_rounds=3)
            source_cwd = core.ROOT / 'project'
            source_cwd.mkdir()
            script = packet_worker(change="assert Path.cwd().name == 'project'".replace('Path.cwd()', '__import__(\"pathlib\").Path.cwd()'))
            with patch.object(integration, 'catchup', side_effect=AssertionError('global catchup')):
                result = growth.run([sys.executable,'-c',script], scope_job=job, cwd=source_cwd)
            assert result['ok'], result
            assert result['domain_selected'] == 0 and result['scope_selected'] == 1, result
            assert integration.status('claude','own')['reviewed_rounds'] == 1
            assert growth.plan()['candidates'], 'unrelated Domain work was incorrectly acknowledged'
        ''')


if __name__ == '__main__':
    unittest.main()
