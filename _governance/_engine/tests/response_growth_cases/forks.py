"""응답 뒤 검토(fork)의 수트의 한 갈래: fork와 검토의 차례 — 세기, 넘기기, 실패와 재시도.

`tests/test_response_growth.py`의 `ResponseGrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from response_growth_cases.support import VAULT
from osk import response_growth as rg, worker_preflight  # noqa: E402
import test_growth as base_tests  # noqa: E402


class ForkCases:
    def test_empty_legacy_baseline_excludes_ancestor_finals_once(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            import os
            parent = VAULT.root / 'rollout-parent-own.jsonl'
            child = VAULT.root / 'rollout-child-own_next.jsonl'
            def finish(identity):
                return [{'type':'event_msg','payload':{'type':'task_started','turn_id':identity}},
                        {'type':'event_msg','payload':{'type':'task_complete','turn_id':identity,'last_agent_message':'done'}}]
            def save(path, rows):
                path.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            header = {'type':'session_meta','payload':{'id':'own'}}
            save(parent, [header] + [r for n in range(9) for r in finish('old-'+str(n))])
            header['payload']['history_base'] = {'thread_id':'own','end_byte_offset':parent.stat().st_size}
            save(child, [header])
            with conversation_state.state_lock(VAULT, 'codex','own') as path:
                state = conversation_state.load_state(VAULT, path,'codex','own')
                state['response_growth'] = {'counter':'finals','seen':[],'count':0,'attempted_count':0}
                conversation_state.save_state(path,state)
            with patch.dict(os.environ, {'CODEX_HOME':str(VAULT.root)}):
                for n in range(1,10):
                    with child.open('a',encoding='utf-8') as f:
                        f.write(''.join(json.dumps(r)+chr(10) for r in finish('new-'+str(n))))
                    source = rg.profile(VAULT, str(child),'codex','own')
                    clock = rg.observe(VAULT, source)
                    assert clock['count'] == n and clock['due'] == (n == 9), clock
                    assert rg.observe(VAULT, source) == clock
                # A modern baseline actually observed before these completions
                # must count them even if they now live in an ancestor page.
                conversation_state.state_path(VAULT, 'codex','own').unlink()
                original_parent = parent.read_bytes()
                save(parent, [{'type':'session_meta','payload':{'id':'own'}}])
                env = {'harness':'codex','session_id':'own','transcript_path':str(parent)}
                with patch.object(rg,'configured',return_value='unused'):
                    rg.initialize(VAULT, env)
                parent.write_bytes(original_parent)
                source = rg.profile(VAULT, str(child),'codex','own')
                assert rg.observe(VAULT, source)['count'] == 18
                # SessionStart before a new page exists also has no history baseline.
                conversation_state.state_path(VAULT, 'codex','own').unlink()
                original_child = child.read_bytes()
                child.unlink()
                with patch.object(rg,'configured',return_value='unused'):
                    rg.initialize(VAULT, dict(env,transcript_path=str(child)))
                child.write_bytes(original_child)
                assert rg.observe(VAULT, rg.profile(VAULT, str(child),'codex','own'))['count'] == 9
        ''')

    def test_paginated_final_counter_keeps_baseline_and_active_page_model(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            import os
            folder = VAULT.root / 'native'
            folder.mkdir()
            parent = folder / 'rollout-first-own.jsonl'
            child = folder / 'rollout-next-own_page.jsonl'
            def rows(model, identity, history=None):
                meta = {'id':'own','cli_version':model,'source':'vscode','originator':'Codex Desktop'}
                if history is not None:
                    meta['history_base'] = history
                return [{'type':'session_meta','payload':meta},
                        {'type':'event_msg','payload':{'type':'task_started','turn_id':identity}},
                        {'type':'turn_context','payload':{'model':model}},
                        {'type':'event_msg','payload':{'type':'task_complete','turn_id':identity,'last_agent_message':'done'}}]
            def save(path, data):
                path.write_text(''.join(json.dumps(r) + chr(10) for r in data), encoding='utf-8')
            save(parent, rows('old-model', 'first'))
            with patch.dict(os.environ, {'CODEX_HOME':str(VAULT.root)}):
                assert rg.observe(VAULT, rg.profile(VAULT, str(parent),'codex','own'))['count'] == 0
                save(child, rows('same-parent-model','second',{'thread_id':'own','end_byte_offset':parent.stat().st_size}))
                source = rg.profile(VAULT, str(child),'codex','own')
                assert source['finals'] == ['first','second'], source
                assert source['model'] == source['cli_version'] == 'same-parent-model', source
                assert source['transcript_path'] == str(child.resolve()), source
                assert rg.observe(VAULT, source)['count'] == 1
                # Upgrade from a baseline that saw only the last page: no retroactive Stops.
                with conversation_state.state_lock(VAULT, 'codex','own') as path:
                    state = conversation_state.load_state(VAULT, path,'codex','own')
                    state['response_growth'] = {'counter':'finals','seen':['second'],'count':3,'attempted_count':0}
                    conversation_state.save_state(path,state)
                assert rg.observe(VAULT, source)['count'] == 3
                with child.open('a',encoding='utf-8') as f:
                    f.write(''.join(json.dumps(r) + chr(10) for r in rows('same-parent-model','third')[1:]))
                source = rg.profile(VAULT, str(child),'codex','own')
                assert rg.observe(VAULT, source)['count'] == 4
                assert rg.observe(VAULT, source)['count'] == 4
                parent.write_bytes(parent.read_bytes().replace(b'old-model',b'bad-model'))
                assert not rg._source_unchanged(source)
                try:
                    rg.observe(VAULT, dict(source,finals=['first','third']))
                    raise AssertionError('lost completion accepted')
                except ValueError:
                    pass
        ''')

    def test_only_successful_native_final_answers_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'native.jsonl'
            total = {'input_tokens': 500, 'cached_input_tokens': 400, 'output_tokens': 25}
            usage = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': total, 'last_token_usage': {'input_tokens': 500}}}}
            rows = [{'type': 'session_meta', 'payload': {'id': 'own', 'cwd': folder,
                     'source': 'vscode', 'originator': 'Codex Desktop'}},
                    {'type': 'turn_context', 'payload': {'model': 'same-model', 'effort': 'high'}},
                    usage, usage,
                    {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1', 'last_agent_message': 'done'}},
                    {'type': 'turn_context', 'payload': {'model': 'next-model', 'effort': 'low'}}]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"partial":', encoding='utf-8')
            result = rg.profile(VAULT, str(path), 'codex', 'own')
            self.assertEqual(result['usage'], total)
            self.assertEqual(result['finals'], ['t1'])
            self.assertEqual(result['model'], 'same-model')
            self.assertEqual((result['source'], result['originator']), ('vscode', 'Codex Desktop'))
            self.assertTrue(result['active'])
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                rg.profile(VAULT, str(path), 'codex', 'other')
            rows = rows[:-1] + [
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'failed'}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'failed','error':'failure','last_agent_message':'error reply'}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'aborted'}},
                {'type':'event_msg','payload':{'type':'turn_aborted','turn_id':'aborted'}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
            result = rg.profile(VAULT, str(path), 'codex', 'own')
            self.assertEqual(result['finals'], ['t1'])
            self.assertFalse(result['last_successful'])

            def assistant(mid, stop='tool_use', **kwargs):
                return {'type': 'assistant', 'sessionId': 'own', **kwargs,
                        'message': {'id': mid, 'model': 'claude-same', 'stop_reason': stop,
                                    'content':[{'type':'text','text':'answer'}]}}
            rows = [assistant('m1'), assistant('m1'), assistant('m2', 'end_turn'),
                    assistant('side', 'end_turn', isSidechain=True), assistant('agent', agentId='worker')]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
            result = rg.profile(VAULT, str(path), 'claude', 'own')
            self.assertEqual(result['finals'], ['m2'])
            self.assertFalse(result['active'])
            thinking = assistant('m3', 'end_turn')
            thinking['message']['content'] = [{'type':'thinking','thinking':'internal'}]
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(thinking) + '\n')
            self.assertTrue(rg.profile(VAULT, str(path), 'claude', 'own')['active'])
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(assistant('m3', 'end_turn')) + '\n')
            self.assertEqual(rg.profile(VAULT, str(path), 'claude', 'own')['finals'], ['m2','m3'])

    def test_claude_fork_runs_with_source_permission_mode_verbatim(self):
        with tempfile.TemporaryDirectory() as folder:
            path, exe = Path(folder) / 'native.jsonl', Path(folder) / 'cli.exe'
            exe.touch()
            def user(mode=None, **kwargs):
                return {'type': 'user', 'sessionId': 'own', 'cwd': folder, 'version': '2.1.280', **kwargs,
                        **({'permissionMode': mode} if mode else {}), 'message': {'role': 'user', 'content': 'q'}}
            def final(mid):
                return {'type': 'assistant', 'sessionId': 'own', 'message': {
                    'id': mid, 'model': 'claude-same', 'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': 'answer'}]}}
            def write(rows):
                path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
                return rg.profile(VAULT, str(path), 'claude', 'own')
            # The mode in force at the last final: later rows without the field and sidechains keep it.
            rows = [user('auto'), final('m1'), user('bypassPermissions'), user(),
                    user('plan', isSidechain=True), final('m2')]
            source = write(rows)
            self.assertEqual(source['permission_mode'], 'bypassPermissions')
            missing = write([user(), final('m1')])
            self.assertIsNone(missing['permission_mode'])
            env = {'CLAUDE_CONFIG_DIR': folder}
            def inspect(argv, **kwargs):
                text = ('2.1.280' if argv[1:] == ['--version'] else
                        json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'}))
                return subprocess.CompletedProcess(argv, 0, text, '')
            with patch.object(rg.subprocess, 'run', side_effect=inspect) as call:
                for mode in ('acceptEdits', 'auto', 'bypassPermissions', 'default', 'manual', 'dontAsk', 'plan'):
                    with self.subTest(mode=mode):
                        argv = rg.command(VAULT, {**source, 'permission_mode': mode}, str(exe), env)
                        self.assertEqual(argv.count('--permission-mode'), 1)
                        self.assertEqual(argv[argv.index('--permission-mode') + 1], mode)
                        # Only the deny hook is added (#85): tool definitions and MCP stay the source's.
                        self.assertEqual(argv[argv.index('--settings') + 1], worker_preflight.CLAUDE_FORK_SETTINGS)
                        self.assertFalse({'--tools', '--allowedTools', '--disallowedTools', '--strict-mcp-config',
                                          '--mcp-config'} & set(argv))
                        # --permission-mode bypassPermissions alone enables bypass; nothing wider is added.
                        self.assertFalse({'--dangerously-skip-permissions',
                                          '--allow-dangerously-skip-permissions'} & set(argv))
                for mode in (None, '', 'bogus', 'BypassPermissions', ['auto']):
                    with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'permission mode'):
                        rg.command(VAULT, {**source, 'permission_mode': mode}, str(exe), env)
                for gate in (lambda s: rg.command(VAULT, s, str(exe), env),
                             lambda s: worker_preflight.preflight(VAULT, s, str(exe), env, require_version=False)):
                    with self.assertRaisesRegex(ValueError, 'permission mode'):
                        gate(missing)
                # Before the first final there is no mode to copy yet; routing still checks the CLI.
                self.assertEqual(worker_preflight.preflight(VAULT, {**missing, 'finals': []}, str(exe), env, require_version=False), str(exe))
            self.assertTrue(all(c.args[0][1:] in (['--version'], ['auth', 'status']) for c in call.call_args_list))
            # A mode change between the checked source and the fork never forks. The file is the
            # checked source again, so the mode is the only difference (the control below forks).
            write(rows)
            job = {'harness': 'claude', 'conversation_id': 'own'}
            with patch.object(worker_preflight, 'preflight', return_value=str(exe)), \
                    patch.object(rg.growth, 'run', return_value={'ok': True, 'state': 'complete'}) as fork:
                result = rg.run(VAULT, {**source, 'permission_mode': 'default'}, job, str(exe))
                self.assertEqual(result['state'], 'unavailable')
                self.assertIn('source advanced', result['error'])
                self.assertFalse(fork.called)
                self.assertEqual(rg.run(VAULT, source, job, str(exe))['state'], 'complete')
                self.assertEqual(fork.call_count, 1)

    def test_turned_off_harness_neither_forks_nor_reviews_in_session(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, doctor, setup, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from osk import harness as adapters
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root / 'native.jsonl'
            rows = [{'type':'session_meta','payload':{'id':'own','cwd':str(VAULT.root),'cli_version':'0.155.0',
                        'model_provider':'openai','source':'cli','originator':'codex_cli_rs'}},
                    {'type':'turn_context','payload':{'turn_id':'t1','cwd':str(VAULT.root),'model':'same-model',
                        'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                    {'type':'response_item','payload':{'type':'message','role':'user',
                        'content':[{'type':'input_text','text':'question'}]}},
                    {'type':'response_item','payload':{'type':'message','role':'assistant',
                        'content':[{'type':'output_text','text':'answer'}]}},
                    {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'answer'}}]
            native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'codex': False}))
            env = {'harness':'codex','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            with patch.object(rg.subprocess, 'run', side_effect=AssertionError('CLI asked')):
                assert rg.probe(VAULT, env) == {'mode':'off','reason':rg.OFF}
                start = hook.capture_block(VAULT, env, 'own', startup=True)
                assert '대화 검토 꺼짐' in start and '검토 경고' not in start, start
                pending = conversation_capture.status(VAULT, 'codex','own')['pending_refs']
                for n in range(1, 16):
                    text = hook.capture_block(VAULT, env, 'own') or ''
                    assert '케이던스' not in text and '검토 경고' not in text, (n, text)
                    assert not rg.launch(VAULT, env, 'own')
                state = conversation_capture.status(VAULT, 'codex','own')
                assert state['pending_refs'] == pending and state['reviewed_rounds'] == 0, state
                assert state['response_growth_route']['mode'] == 'off', state
                assert rg.process_stop(VAULT, env)['state'] == 'disabled'
            item = doctor._fork(VAULT, adapters.get('codex'))
            assert item['level'] == 'info' and '껐다' in item['detail'], item
            assert setup._fork(VAULT, [adapters.get('codex')], False)['entries']['codex'] == {'action':'keep','path':False}
            for bad in (True, 0, '', ' '):
                rg.config_path(VAULT).write_text(json.dumps({'codex': bad}))
                try:
                    rg.configured(VAULT, 'codex')
                    raise AssertionError(bad)
                except ValueError:
                    pass
        ''')

    def test_codex_ineligible_source_routes_to_foreground_without_consuming_review(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            import subprocess
            patch.object(worker_preflight, '_codex_hook_ready', return_value=True).start()  # see test_codex_preflight_requires_*
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root / 'native.jsonl'
            rows = [{'type':'session_meta','payload':{'id':'own','cwd':str(VAULT.root),
                        'cli_version':'0.155.0','model_provider':'openai'}},
                    {'type':'turn_context','payload':{'turn_id':'t1','cwd':str(VAULT.root),'model':'same-model',
                        'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                    {'type':'event_msg','payload':{'type':'user_message','message':'question'}},
                    {'type':'response_item','payload':{'type':'message','role':'user',
                        'content':[{'type':'input_text','text':'question'}]}},
                    {'type':'response_item','payload':{'type':'message','role':'assistant',
                        'content':[{'type':'output_text','text':'answer'}]}},
                    {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'answer'}}]
            native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'codex':sys.executable}))
            env = {'harness':'codex','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            real_run = subprocess.run
            def inspect(argv, **kwargs):
                if argv[0] == 'git':
                    return real_run(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0,
                    'codex-cli 0.155.0' if '--version' in argv else 'Logged in using ChatGPT', '')
            # Exercise the real Git boundary without spawning a model. The hooks' worker asks (probe).
            with patch.object(rg.subprocess,'run',side_effect=inspect):
                rg.probe(VAULT, env)
                start = hook.capture_block(VAULT, env,'own',startup=True)
                assert 'Git worktree' in start and '검토 경고' in start, start
                pending = conversation_capture.status(VAULT, 'codex','own')['pending_refs']
                for n in range(1,16):
                    text = hook.capture_block(VAULT, env,'own')
                    assert ('[osk 케이던스' in text) == (n in {9,15}), (n,text)
                    assert not rg.launch(VAULT, env,'own')
                state = conversation_capture.status(VAULT, 'codex','own')
                assert state['pending_refs'] == pending and state['reviewed_rounds'] == 0
                assert state['response_growth']['attempted_count'] == 0
                real_run(['git','init','-q','-b','main',str(VAULT.root)],check=True,capture_output=True)
                assert rg.probe(VAULT, env)['mode'] == 'background'
                # A future restriction must refuse even when Git/auth/version pass.
                rows[1]['payload']['sandbox_policy']['future_restriction'] = True
                native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
                route = rg.probe(VAULT, env)
                assert route['mode'] == 'foreground' and 'cannot be represented' in route['reason'], route
                # The ChatGPT Web bridge route is refused by name before any native query.
                del rows[1]['payload']['sandbox_policy']['future_restriction']
                rows[1]['payload']['model'] = 'chatgpt-web/pro'
                native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
                with patch.object(rg.subprocess,'run',side_effect=AssertionError('native query')):
                    route = rg.probe(VAULT, env)
                    assert route['mode'] == 'foreground' and 'ChatGPT Web bridge' in route['reason'], route
                    try:
                        rg.command(VAULT, rg.profile(VAULT, str(native),'codex','own'), sys.executable, {})
                        raise AssertionError('web route accepted')
                    except ValueError as exc:
                        assert 'ChatGPT Web bridge' in str(exc), exc
                source = rg.profile(VAULT, str(native),'codex','own')
                with patch.object(growth,'run',side_effect=AssertionError('provider started')):
                    result = rg.run(VAULT, source, {'harness':'codex','conversation_id':'own'}, sys.executable)
                assert result['state'] == 'unavailable', result
        ''')

    def test_cumulative_fork_usage_is_not_child_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stdout.jsonl'
            path.write_text(json.dumps({'type': 'turn.completed', 'usage': {
                'input_tokens': 10000, 'cached_input_tokens': 7000, 'output_tokens': 100}}), encoding='utf-8')
            measured = rg.cache_usage(path, {'harness': 'codex', 'usage': {
                'input_tokens': 9000, 'cached_input_tokens': 7000, 'output_tokens': 90}})
            self.assertEqual(measured['input_tokens'], 1000)
            self.assertEqual(measured['cached_fraction'], 0)
            self.assertNotIn('first_request_floor', measured)
            self.assertFalse(rg.cache_usage(path, {'harness': 'codex'})['measured'])
            # The parent's last request bounds the first fork request: a whole-turn miss shows.
            self.assertEqual(rg.cache_usage(path, {'harness': 'codex', 'context': 900, 'usage': {
                'input_tokens': 9000, 'cached_input_tokens': 7000, 'output_tokens': 90}})['first_request_floor'], 0)
            # 2026-10-18 ephemeral CLI fork: parent and resume cumulative, then 23,602 in, 23,424 cached.
            path.write_text(json.dumps({'type': 'turn.completed', 'usage': {
                'input_tokens': 70704, 'cached_input_tokens': 46720, 'output_tokens': 25}}), encoding='utf-8')
            floor = rg.cache_usage(path, {'harness': 'codex', 'context': 23567, 'usage': {
                'input_tokens': 47102, 'cached_input_tokens': 23296, 'output_tokens': 17}})['first_request_floor']
            self.assertGreater(floor, 0.99)

    def test_counter_survives_resume_and_worker_failure_does_not_ack(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            source = {'harness':'claude','conversation_id':'own','finals':[]}
            assert not rg.observe(VAULT, source)['due']
            source['finals'] = [str(i) for i in range(9)]
            assert rg.observe(VAULT, source)['due']
            assert rg.observe(VAULT, source)['count'] == 9
            with patch.object(rg, 'run', return_value={'ok':False,'state':'incomplete'}):
                result = rg.attempt(VAULT, source, {'pending_refs':['fixture'], 'capture_error':None}, 'unused')
            assert result['state'] == 'incomplete'
            assert not rg.observe(VAULT, source)['due']
            assert conversation_capture.status(VAULT, 'claude','own')['reviewed_rounds'] == 0
            source['finals'] = source['finals'][:8]
            try:
                rg.observe(VAULT, source)
                assert False, 'truncated native history was accepted'
            except ValueError:
                pass
        ''')

    def test_shadow_prompt_clock_does_not_trigger_a_fork_and_ninth_stop_does(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root / 'native.jsonl'
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'session':'own',
                   'permission_mode':'bypassPermissions'}
            rg.initialize(VAULT, env)
            def append(n):
                rows = [{'type':'user','sessionId':'own','uuid':f'u{n}','permissionMode':'bypassPermissions',
                         'message':{'role':'user','content':f'question {n}'}},
                        {'type':'assistant','sessionId':'own','uuid':f'a{n}', 'message':{'role':'assistant','id':f'm{n}','model':'parent-model', 'stop_reason':'end_turn','content':[{'type':'text','text':f'answer {n}'}]}}]
                with native.open('a', encoding='utf-8') as f:
                    f.write(''.join(json.dumps(r)+'\\n' for r in rows))
            with patch.object(rg, 'run', return_value={'ok':False,'state':'incomplete'}) as worker, \
                    patch.object(scope_memory,'recovery_block',return_value='RECOVERY_SENTINEL'), \
                    patch.object(worker_preflight,'preflight'):
                for n in range(1, 10):
                    append(n)
                    hook.capture_block(VAULT, env, 'own')
                    before = conversation_capture.status(VAULT, 'claude','own')
                    assert before['prompt_count'] == n, before
                    assert before['response_growth']['count'] == n-1, before
                    if n == 9:  # A switch after the prompt writes no row; Stop's mode in force refuses.
                        switched = rg.process_stop(VAULT, dict(env, permission_mode='default'))
                        assert switched['state'] == 'unavailable' and not worker.called, switched
                    result = rg.process_stop(VAULT, env)
                    assert worker.call_count == (1 if n == 9 else 0), result
                    rg.process_stop(VAULT, env)  # duplicate notification must not count or launch
                assert worker.call_count == 1
                with patch.object(rg.subprocess, 'Popen') as popen:
                    assert rg.launch(VAULT, env, 'own')
                payload = json.loads(popen.return_value.stdin.write.call_args.args[0])
                assert payload['permission_mode'] == 'bypassPermissions', payload
                _vault, source, job, exe = worker.call_args.args
                assert len(job['pending_refs']) == 9 and job['conversation_id'] == 'own', job
                assert not job['organization_jobs']
                planned = {'scope_jobs':[job], 'candidates':[], 'organization_jobs':[]}
                assert 'RECOVERY_SENTINEL' in growth.prompt(VAULT, planned)
                assert source['model'] == 'parent-model'
                rg.initialize(VAULT, env)  # resume must not reset the durable clock
                status = conversation_capture.status(VAULT, 'claude','own')
                assert status['response_growth']['count'] == 9 and status['reviewed_rounds'] == 0, status
                assert 'incomplete' in hook.capture_block(VAULT, env, 'own', startup=True)
            source['finals'] += [str(n) for n in range(9,18)]
            with patch.object(rg, 'run', return_value={'ok':False,'state':'busy'}):
                assert rg.attempt(VAULT, source,job,exe)['state'] == 'busy'
            assert rg.observe(VAULT, source)['due'], 'busy worker consumed a due attempt'
        ''')

    def test_unavailable_cli_routes_to_9_15_input_review_and_recovers_without_reset(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            # The hooks' worker asks the CLI (probe); the hooks read its verdict.
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')), \
                    patch.object(rg.subprocess,'Popen',side_effect=AssertionError('unavailable fork launched')):
                rg.probe(VAULT, env)
                start = hook.capture_block(VAULT, env,'own',startup=True)
                assert '검토 경고' in start and 'subscription login required' in start, start
                rg.after_hook(VAULT, env)   # the hooks' worker captures (#192)
                assert conversation_capture.status(VAULT, 'claude','own')['prompt_count'] == 0
                original = conversation_capture.status(VAULT, 'claude','own')['pending_refs']
                cue = 'cite_round(conversation="claude/own"'
                for n in range(1,16):
                    text = hook.capture_block(VAULT, env,'own')
                    assert ('[osk 케이던스' in text) == (n in {9,15}), (n,text)
                    if n in {9,15}:
                        assert '검토 경고' in text and cue in text, text
                    assert not rg.launch(VAULT, env,'own')
                before = conversation_capture.status(VAULT, 'claude','own')
                assert before['prompt_count'] == 15 and before['reviewed_rounds'] == 0, before
            with patch.object(worker_preflight,'preflight'):
                rg.probe(VAULT, env)
                hook.capture_block(VAULT, env,'own',startup=True)
                restored = conversation_capture.status(VAULT, 'claude','own')
                assert restored['response_growth_route']['mode'] == 'background'
                assert restored['prompt_count'] == 15 and restored['pending_refs'] == original
                assert restored['response_growth']['count'] == before['response_growth']['count']
                hook.capture_block(VAULT, env,'own')
            # Losing auth between cadence boundaries must surface the overdue review immediately.
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('login expired')):
                rg.probe(VAULT, env)
                text = hook.capture_block(VAULT, env,'own')
                assert 'user 턴 17' in text and cue in text and '단독 턴' in text, text
                assert conversation_capture.status(VAULT, 'claude','own')['reviewed_rounds'] == 0
            rg.config_path(VAULT).write_text('{broken')
            text = hook.capture_block(VAULT, env,'own',startup=True)
            assert '검토 경고' in text and 'UserPromptSubmit' in text
            assert not rg.launch(VAULT, env,'own'), 'bad configuration prevented capture fallback'
        ''')

    def test_failing_forks_fall_back_to_9_15_review_and_retry_a_day_later(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            source = {'harness':'claude','conversation_id':'own','finals':[]}
            job = {'pending_refs':['fixture'], 'capture_error':None}
            def stop(n, result):
                source['finals'] = [str(i) for i in range(9 * n)]
                with patch.object(rg, 'run', return_value=result):
                    return rg.attempt(VAULT, source, job, 'unused')
            with patch.object(worker_preflight, 'preflight'):
                rg.observe(VAULT, source)
                assert rg.probe(VAULT, env)['mode'] == 'background'  # the CLI is ready (the hooks' worker asked)
                # One unfinished fork review keeps the fork; a busy worker says nothing about it.
                assert stop(1, {'ok': False, 'state': 'incomplete'})['state'] == 'incomplete'
                assert stop(2, {'ok': False, 'state': 'busy'})['state'] == 'busy'
                assert rg.route(VAULT, env)['mode'] == 'background'
                # The second unfinished review in a row hands the review to the session
                # (measured 2026-10-01: rejected forks left 82 conversations waiting).
                stop(3, {'ok': False, 'state': 'rejected', 'error': 'provider output has no successful final result'})
                route = rg.route(VAULT, env)
                assert route['mode'] == 'foreground' and 'did not finish' in route['reason'], route
                assert 'no successful final result' in route['reason'], route
                assert rg.route(VAULT, env)['changed'] is False, 'the reason must be stable between turns'
                assert not rg.launch(VAULT, env, 'own'), 'a failing fork was launched again'
                text = hook.capture_block(VAULT, env, 'own', startup=True)
                assert '검토 경고' in text and 'did not finish' in text, text
                # A day after the last failure the fork is tried again; a finished review clears the run.
                with patch.object(rg.time, 'time', return_value=rg.time.time() + rg.RETRY_AFTER + 1):
                    assert rg.route(VAULT, env)['mode'] == 'background'
                stop(4, {'ok': True, 'state': 'complete'})
                assert 'failed' not in conversation_capture.status(VAULT, 'claude', 'own')['response_growth']
                assert rg.route(VAULT, env)['mode'] == 'background'
        ''')

    def test_fork_that_finished_its_review_is_not_failed_by_remaining_organization(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            source = {'harness':'claude','conversation_id':'own','finals':[]}
            job = {'pending_refs':['fixture'], 'capture_error':None}
            def stop(n, result):
                source['finals'] = [str(i) for i in range(9 * n)]
                with patch.object(rg, 'run', return_value=result):
                    return rg.attempt(VAULT, source, job, 'unused')
            def run(review, **changed):
                return {'kind': 'run', 'ok': False, 'state': 'incomplete', 'returncode': 0, 'error': None,
                        'final_reviews': {'state': 'applied', 'errors': []},
                        'scope_outcomes': {'scope-review-x': {'status': review}},
                        'organization_outcomes': {'organization:x': {'status': 'pending', 'remaining_units': 5}},
                        **changed}
            with patch.object(worker_preflight, 'preflight'):
                rg.observe(VAULT, source)
                # The scope's organization takes several passes (Mechanism §9-4 4). After v5.0.0
                # two forks that preserved their review were counted as failed and handed the
                # conversation to in-session review for a day.
                for n in (1, 2, 3):
                    stop(n, run('complete'))
                    assert 'failed' not in conversation_capture.status(VAULT, 'claude', 'own')['response_growth']
                # A refused decision for another job does not undo the review this fork made.
                stop(4, run('complete', final_reviews={'state': 'rejected',
                                                       'errors': ['provider output has no successful final result']}))
                assert 'failed' not in conversation_capture.status(VAULT, 'claude', 'own')['response_growth']
                assert rg.route(VAULT, env)['mode'] == 'background'
                # Nor does its conversation hear that a review waits (#228: org deferral alone made
                # the hook say "백그라운드 검토 대기 — incomplete").
                assert '검토 대기' not in hook.capture_block(VAULT, env, 'own', startup=True)
                # A fork that left its own conversation's review unfinished still counts.
                stop(5, run('pending'))
                assert '백그라운드 검토 대기 — incomplete' in hook.capture_block(VAULT, env, 'own', startup=True)
                stop(6, run('pending', returncode=1,
                            final_reviews={'state': 'not_applied', 'errors': ['worker did not exit successfully']}))
                route = rg.route(VAULT, env)
                assert route['mode'] == 'foreground' and 'did not finish' in route['reason'], route
        ''')

    def test_login_loss_after_input_does_not_consume_stop_attempt(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            source = {'harness':'claude','conversation_id':'own','model':'same-model','finals':[]}
            rg.observe(VAULT, source)
            source['finals'] = [str(i) for i in range(9)]
            job = {'harness':'claude','conversation_id':'own','pending_refs':['fixture']}
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('login expired')), \
                    patch.object(growth,'run',side_effect=AssertionError('provider started')):
                result = rg.attempt(VAULT, source,job,'unused')
            assert result['state'] == 'unavailable', result
            status = conversation_capture.status(VAULT, 'claude','own')
            assert status['response_growth']['attempted_count'] == 0
            assert status['reviewed_rounds'] == 0 and rg.observe(VAULT, source)['due']
        ''')

    def test_source_advanced_before_fork_does_not_consume_stop_attempt(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            native = VAULT.root / 'native.jsonl'
            rows = [{'type':'assistant','sessionId':'own','uuid':f'a{n}','cwd':str(VAULT.root),'message':{'role':'assistant','id':f'm{n}',
                     'model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}}
                    for n in range(9)]
            native.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            source = rg.profile(VAULT, str(native),'claude','own')
            rg.observe(VAULT, dict(source, finals=[]))
            job = {'harness':'claude','conversation_id':'own','pending_refs':['fixture']}
            def append(row):
                with native.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(row)+'\\n')
            # Claude appends trailers such as stop_hook_summary seconds after Stop, during preflight.
            trailer = lambda *a, **k: append({'type':'system','subtype':'stop_hook_summary','sessionId':'own'})
            with patch.object(worker_preflight,'preflight',side_effect=trailer), \\
                    patch.object(growth,'run',return_value={'ok':False,'state':'incomplete'}) as fork:
                assert rg.attempt(VAULT, source,job,'unused')['state'] == 'incomplete'
            assert fork.call_count == 1 and conversation_capture.status(VAULT, 'claude','own')['response_growth']['attempted_count'] == 9
            # A new user turn before launch never forks and never spends the attempt.
            with native.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(dict(rows[0], uuid=f'a{n}', message=dict(rows[0]['message'], id=f'm{n}')))+'\\n'
                                for n in range(9, 18)))
            source = rg.profile(VAULT, str(native),'claude','own')
            new_turn = lambda *a, **k: append({'type':'user','sessionId':'own','message':{'role':'user','content':'next'}})
            with patch.object(worker_preflight,'preflight',side_effect=new_turn), \\
                    patch.object(growth,'run',side_effect=AssertionError('fork started')):
                result = rg.attempt(VAULT, source,job,'unused')
            assert result['state'] == 'unavailable' and 'source advanced' in result['error'], result
            status = conversation_capture.status(VAULT, 'claude','own')
            assert status['response_growth']['attempted_count'] == 9, status
            assert status['reviewed_rounds'] == 0 and rg.observe(VAULT, source)['due']
        ''')

    def test_claude_auth_refresh_race_retries_the_fork_once(self):
        # 14 of 86 Claude forks (2026-09-25..10-05) exited before inference with this message.
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, worker_preflight
            from unittest.mock import patch
            native = VAULT.root / 'native.jsonl'
            native.write_text(json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','cwd':str(VAULT.root),
                'message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn',
                           'content':[{'type':'text','text':'answer'}]}})+'\\n', encoding='utf-8')
            source = rg.profile(VAULT, str(native),'claude','own')
            job = {'harness':'claude','conversation_id':'own','pending_refs':['fixture']}
            (VAULT.root / 'raced').mkdir()
            (VAULT.root / 'raced/stdout.txt').write_text(json.dumps({'type':'result','is_error':True,
                'result':'Failed to refresh OAuth token: another Claude Code process is refreshing it'})+'\\n', encoding='utf-8')
            # A reviewed conversation may quote the message; only the CLI's own final result counts.
            (VAULT.root / 'quoted').mkdir()
            (VAULT.root / 'quoted/stdout.txt').write_text(json.dumps({'type':'assistant','message':{'content':[
                {'type':'text','text':'Failed to refresh OAuth token appeared in this conversation.'}]}})+'\\n'+json.dumps(
                {'type':'result','is_error':True,'result':'Reached the turn limit'})+'\\n', encoding='utf-8')
            raced = {'ok':False,'state':'incomplete','returncode':1,'output':'raced'}
            quoted = {'ok':False,'state':'incomplete','returncode':1,'output':'quoted'}
            other = {'ok':False,'state':'incomplete','returncode':1}
            done = {'ok':True,'state':'complete','returncode':0}
            for runs, calls, waits, final in (([raced, done], 2, 1, done), ([raced, raced], 2, 1, raced),
                                               ([other, done], 1, 0, other), ([quoted, done], 1, 0, quoted)):
                with patch.object(worker_preflight,'preflight',return_value='claude'), patch.object(rg.time,'sleep') as wait, \\
                        patch.object(rg,'cache_usage',return_value={}), \\
                        patch.object(growth,'run',side_effect=runs) as fork:
                    result = rg.run(VAULT, source,job,'unused')
                assert fork.call_count == calls and wait.call_count == waits, (runs, fork.call_count)
                assert result['state'] == final['state'] and result.get('returncode') == final['returncode'], result
        ''')

    def test_fork_supervisor_selects_only_own_snapshot_and_preserves_cwd(self):
        base_tests.GrowthTests().check_case('''
            from osk import integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state
            from unittest.mock import patch
            node('Unrelated Domain candidate')
            node('Other Scope claim', scope='W2')
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            native = VAULT.root / 'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'one-off question'}}) + '\\n' +
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','stop_reason':'end_turn','content':[{'type':'text','text':'one-off answer'}]}}) + '\\n')
            assert conversation_capture.capture(VAULT, 'claude','own',str(native),'own')['ok']
            job = integration.prompt(VAULT, 'claude','own',include_organization=False,max_rounds=3)
            source_cwd = VAULT.root / 'project'
            source_cwd.mkdir()
            script = packet_worker(change="assert Path.cwd().name == 'project'".replace('Path.cwd()', '__import__(\"pathlib\").Path.cwd()'))
            with patch.object(integration, 'catchup', side_effect=AssertionError('global catchup')):
                result = growth.run(VAULT, [sys.executable,'-c',script], scope_job=job, cwd=source_cwd)
            assert result['ok'], result
            argv = json.loads((VAULT.root / result['output'] / 'argv.json').read_text(encoding='utf-8'))
            assert argv[1:] == ['-c', script], argv
            assert result['domain_selected'] == 0 and result['scope_selected'] == 1, result
            manifest = [r for r in core.ledger_read(growth_ledger.ledger_path(VAULT)) if r['kind']=='plan'][-1]
            assert [j['scope'] for j in manifest['organization_jobs']] == ['W1'], manifest
            assert result['selected'] == 2 and manifest['timeout_seconds'] == 600
            assert conversation_capture.status(VAULT, 'claude','own')['reviewed_rounds'] == 1
            assert growth_plan.plan(VAULT)['candidates'], 'unrelated Domain work was incorrectly acknowledged'
        ''')

    def test_fork_doctor_shows_route_verdict_without_writing_state_or_secrets(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, cli, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from types import SimpleNamespace
            from unittest.mock import patch
            import io, os, subprocess
            subprocess.run(['git','init','-q','-b','main',str(VAULT.root)], check=True, capture_output=True)
            home = VAULT.root / 'claude-home'
            native = home / 'projects' / 'p' / 'own.jsonl'
            native.parent.mkdir(parents=True)
            native.write_text(json.dumps({'type':'user','sessionId':'own','cwd':str(VAULT.root),'version':'2.1.280',
                'permissionMode':'auto','message':{'role':'user','content':'q'}}) + chr(10) +
                json.dumps({'type':'assistant','sessionId':'own','message':{'id':'m1','model':'claude-same',
                'stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}}) + chr(10), encoding='utf-8')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude': sys.executable}))
            bridge = 'http://127.0.0.1:17841/v1'
            Path(os.environ['CODEX_HOME']).mkdir(parents=True, exist_ok=True)
            (Path(os.environ['CODEX_HOME']) / 'config.toml').write_text(f'openai_base_url = "{bridge}"', encoding='utf-8')
            os.environ.update(CLAUDE_CONFIG_DIR=str(home), OPENAI_BASE_URL='https://u:pa@s3cret@relay.example/v1?t=s3cret',
                              ANTHROPIC_BASE_URL='https://user:s3cret@proxy.example/v1?key=s3cret')
            calls = []
            def inspect(argv, **kwargs):
                calls.append(argv)
                text = ('true' if argv[0] == 'git' else '2.1.280 (Claude Code)' if argv[1:] == ['--version'] else
                        json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max',
                                    'email': 'person@example.com', 'orgId': 'org-s3cret'}))
                return subprocess.CompletedProcess(argv, 0, text, '')
            def snapshot():
                roots = {VAULT.root, conversation_state.state_path(VAULT, 'claude', 'own').parent, Path(os.environ['CODEX_HOME'])}
                return {str(p): (p.stat().st_mtime_ns, p.is_file() and p.read_bytes())
                        for root in roots for p in root.rglob('*')}
            def doctor(*args):
                out = SimpleNamespace(buffer=io.BytesIO())
                with patch.object(sys, 'stdout', out):
                    cli.main(['fork', 'doctor', '--session', 'own', *args])
                return out.buffer.getvalue().decode('utf-8')
            before = snapshot()
            with patch.object(rg.subprocess, 'run', side_effect=inspect), \\
                    patch.object(rg.subprocess, 'Popen', side_effect=AssertionError('fork launched')):
                text = doctor() + doctor('--json')
                claude, codex = json.loads(text[text.index('[\\n'):])
                assert 'claude: foreground' in text and 'unverified provider' in claude['verdict']['reason'], text
                assert claude['endpoint'] == {'ANTHROPIC_BASE_URL': 'https://proxy.example/v1',
                                              'fork': 'kept; forks refuse it'}, claude
                assert claude['source'] == {'version': '2.1.280', 'model': 'claude-same', 'permission_mode': 'auto',
                                            'finals': 1, 'active': False}, claude
                assert claude['auth'] == {'exit': 0, 'loggedIn': True, 'authMethod': 'claude.ai',
                                          'apiProvider': None, 'subscriptionType': 'max'}, claude
                assert claude['cli'] == sys.executable and claude['cwd']['git'] is True, claude
                assert codex['verdict'] == {'mode': 'foreground', 'reason': 'subscription fork CLI is not configured'}
                assert codex['endpoint']['top_level_overridden'] == bridge, codex
                assert codex['endpoint']['env_dropped'] == 'https://relay.example/v1', codex
                assert codex['endpoint']['pinned_for_forks'] == worker_preflight.OPENAI_ENDPOINT, codex
                assert codex['endpoint']['refused'] is None, codex
                for secret in ('s3cret', 'user:', 'person@example.com'):
                    assert secret not in text, secret
                os.environ['ANTHROPIC_BASE_URL'] = 'https://api.anthropic.com'
                ready = json.loads(doctor('--harness', 'claude', '--json'))[0]
                assert ready['verdict'] == {'mode': 'background', 'reason': None}, ready
                assert ready['endpoint']['fork'] == 'first-party default; dropped for forks', ready
            assert all(c[0] == 'git' or c[1:] in (['--version'], ['auth', 'status']) for c in calls), calls
            assert snapshot() == before, 'doctor wrote state'
            # The doctor's verdict is the hooks' worker's (probe), which route() reads; both write
            # the state the doctor skipped.
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                rg.probe(VAULT, {'harness': 'claude', 'session_id': 'own', 'transcript_path': str(native)})
                route = rg.route(VAULT, {'harness': 'claude', 'session_id': 'own', 'transcript_path': str(native)})
            assert {k: route[k] for k in ('mode', 'reason')} == ready['verdict'], route
            assert snapshot() != before
        ''')
