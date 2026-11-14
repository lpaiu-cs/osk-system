"""응답 뒤 검토(fork)의 수트의 한 갈래: 훅 — 시작·입력·종료가 하는 일과 하지 않는 일.

`tests/test_response_growth.py`의 `ResponseGrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import subprocess

from osk import response_growth as rg, worker_preflight  # noqa: E402
import test_growth as base_tests  # noqa: E402


class HookCases:
    def test_codex_subagent_hook_changes_no_conversation_state(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            import os, subprocess
            hooks = Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'
            sys.path.insert(0, str(hooks))
            import claude_session_start as hook
            os.environ.pop('OSK_GROWTH_WORKER', None)
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'codex':sys.executable}))
            def rollout(name, meta):
                path = VAULT.root / name
                path.write_text(json.dumps({'type':'session_meta','payload':{'cwd':str(VAULT.root), **meta}})+chr(10)+
                    json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}})+chr(10),
                    encoding='utf-8')
                return str(path)
            child = rollout('rollout-child.jsonl', {'id':'child','session_id':'own','parent_thread_id':'own'})
            env = {'session_id':'own','transcript_path':child,'cwd':str(VAULT.root)}
            for harness in (None, 'codex'):
                event = dict(env, harness=harness) if harness else env
                assert hook.capture_block(VAULT, event,'own') is None
                assert hook.capture_block(VAULT, event,'own',startup=True) is None
                for script in ('capture_stop.py', 'claude_session_start.py', 'claude_prompt_submit.py'):
                    done = subprocess.run([sys.executable, str(hooks / script)], input=json.dumps(event).encode(),
                                          capture_output=True, timeout=30)
                    assert done.returncode == 0 and not done.stdout, (script, done.stdout, done.stderr)
            for sid in ('own', 'child'):
                assert not conversation_state.state_path(VAULT, 'codex', sid).exists(), sid
            # A foreign transcript is still an identity error, not a silent subagent.
            foreign = dict(env, transcript_path=rollout('rollout-foreign.jsonl', {'id':'child','session_id':'another'}))
            rg.probe(VAULT, foreign)
            route = rg.route(VAULT, foreign)
            assert route['mode']=='foreground' and 'identity' in route['reason'], route
        ''')

    def test_claude_fork_hook_denies_every_tool_but_osk_mcp(self):
        import re, shutil
        (entry,) = json.loads(worker_preflight.CLAUDE_FORK_SETTINGS)['hooks']['PreToolUse']
        for name, denied in (('Bash', True), ('Read', True), ('Edit', True), ('Agent', True), ('Skill', True),
                             ('WebFetch', True), ('mcp__notion__search', True), ('mcp__osk-system-x__search', True),
                             ('mcp__osk-systemx__search', True), ('ToolSearchX', True), ('xToolSearch', True),
                             ('ToolSearch', False), ('mcp__osk-system__search', False),
                             ('mcp__osk-system__create_node', False)):
            with self.subTest(tool=name):
                # Claude Code tests a regex matcher unanchored; the pattern anchors both ends itself.
                self.assertEqual(bool(re.search(entry['matcher'], name)), denied)
                self.assertEqual(bool(re.fullmatch(entry['matcher'], name)), denied)
        (hook,) = entry['hooks']
        expected = {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny'}
        # `sh`, not `bash`: Windows may resolve bash to the WSL launcher.
        shells = [['sh', '-c']] * bool(shutil.which('sh')) + [
            [s, '-NoProfile', '-Command'] for s in ('powershell', 'pwsh') if shutil.which(s)]
        for shell in shells:
            with self.subTest(shell=shell[0]):
                out = subprocess.run([*shell, hook['command']], capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 0, out.stderr)
                decision = json.loads(out.stdout)['hookSpecificOutput']
                self.assertEqual({k: decision[k] for k in expected}, expected)
                self.assertIn('mcp__osk-system__', decision['permissionDecisionReason'])

    def test_codex_fork_hook_denies_every_tool_but_osk_mcp(self):
        import re
        (group,) = worker_preflight.CODEX_FORK_HOOKS['PreToolUse']
        matcher = group['matcher']
        # Codex compiles the matcher with Rust `regex`; lookaround fails to compile and drops the hook.
        self.assertFalse(re.search(r'\(\?<?[=!]', matcher))
        for name, denied in (('exec_command', True), ('shell', True), ('apply_patch', True), ('Write', True),
                             ('Agent', True), ('update_plan', True), ('mcp__node_repl__js', True),
                             ('mcp__codex_apps__github_search_repositories', True), ('mcp__osk_system_x__search', True),
                             ('mcp__osk_systemx__search', True), ('mcp__osk-system__search', True), ('', True),
                             ('mcp__osk_system__', False), ('mcp__osk_system__search', False),
                             ('mcp__osk_system__create_node', False)):
            with self.subTest(tool=name):
                # Codex tests the regex unanchored; the pattern anchors both ends itself.
                self.assertEqual(bool(re.search(matcher, name)), denied)
        # The same deny line as Claude's, so the shell test above covers both.
        (claude,) = json.loads(worker_preflight.CLAUDE_FORK_SETTINGS)['hooks']['PreToolUse']
        self.assertEqual(group['hooks'], claude['hooks'])
        # The trust Codex itself computed for this hook (app-server hooks/list `currentHash`, 0.154.0 and
        # 0.163.0-alpha.10.2): a hook the engine changes must be measured again, or it never runs.
        (key, state), = worker_preflight.CODEX_FORK_HOOKS['state'].items()
        self.assertEqual(state, {'trusted_hash': 'sha256:1a48d3f94a018f06ae75f2f4af98283fd738c113841802c4186d2096501ed2dc'})
        self.assertEqual(key, ('C:\\<session-flags>\\config.toml' if rg.os.name == 'nt' else '/<session-flags>/config.toml')
                         + ':pre_tool_use:0:0')

    def test_stuck_capture_says_one_line_once_between_review_turns(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', 'shared memory sentinel', space='00_Scope/W1')
            from osk import write
            write.set_primary(VAULT, 'own', 'W1', 'test')
            native = VAULT.root/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),'session':'own'}
            stuck = ValueError('copied native round ID has different content; capture remains pending')
            with patch.object(worker_preflight,'preflight',side_effect=ValueError('subscription login required')), \\
                    patch.object(conversation_capture,'_inherited_rounds',side_effect=stuck):
                rg.probe(VAULT, env)
                assert '검토 경고' in hook.capture_block(VAULT, env,'own',startup=True)
                rg.after_hook(VAULT, env)   # the hooks' worker captures (#192)
                for n in range(1,16):
                    text = hook.capture_block(VAULT, env,'own')
                    if n in {9,15}:
                        assert '[osk 케이던스' in text and '포착 진단' in text, (n,text)
                        # The review turn still carries memory: the text once, then its hash line.
                        assert ('shared memory sentinel' in text) == (n == 9), (n,text)
                        assert n == 9 or 'scope_memory(session="own")' in text, text
                    elif n == 1:
                        assert text.startswith('[osk 포착 대기 — ValueError: copied native round ID'), (n,text)
                        assert '\\n' not in text and 'shared memory sentinel' not in text, (n,text)
                    else:
                        # The same wait is said once; `integration status` keeps it (#192).
                        assert text == '', (n,text)
                state = conversation_capture.status(VAULT, 'claude','own')
                assert state['prompt_count'] == 15 and state['capture_pending'], state
                assert state['reviewed_rounds'] == 0, 'a stuck capture is not a review'
                blocked = conversation_state.blocked(VAULT)
                assert blocked['count'] == 1 and blocked['conversations'][0]['conversation_id'] == 'own', blocked
                assert blocked['conversations'][0]['observed'].endswith('(KST)'), blocked
        ''')

    def test_input_hook_starts_no_process_and_reads_no_transcript(self):
        # #192: every prompt asked the fork CLI (`auth status` 8 s under load), parsed the whole
        # transcript under the conversation lock and ran git for the session key; 31 % of October
        # prompts hit the host's 10 s limit. The hook now counts the turn and hands the rest to
        # one detached worker.
        base_tests.GrowthTests().check_case('''
            import io, os, subprocess, types
            from osk import response_growth as rg, integration, transcripts, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_prompt_submit as hook
            native = VAULT.root / 'native.jsonl'
            def turn(n):
                with native.open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'type':'user','sessionId':'own','uuid':f'u{n}','message':{'role':'user','content':'q'}})+'\\n'+
                        json.dumps({'type':'assistant','sessionId':'own','uuid':f'a{n}','message':{'role':'assistant','id':f'm{n}',
                                    'model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'a'}]}})+'\\n')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root),
                   'hook_event_name':'UserPromptSubmit'}
            os.environ.pop('OSK_HOOK_WORKER')
            def submit(n):
                out = types.SimpleNamespace(buffer=io.BytesIO())
                with patch.object(sys,'stdin',io.StringIO(json.dumps(dict(env, prompt=f'prompt {n}')))), \\
                        patch.object(sys,'stdout',out):
                    hook.main()
                return out.buffer.getvalue().decode('utf-8')
            def work(n):
                with patch.object(rg.core,'spawn_worker') as worker:
                    submit(n)
                job = json.loads(worker.call_args.kwargs['stdin'])
                assert worker.call_count == 1 and job['defer'], worker.call_args
                with patch.object(worker_preflight,'preflight'):
                    rg.after_hook(VAULT, job)   # the worker: capture what is new, ask the fork CLI when due
                return conversation_capture.status(VAULT, 'claude','own')
            # A new conversation's transcript is written after its first prompt: the worker
            # asks the CLI, and nothing is missing yet. (The first hook learns the folder's
            # session key with git.)
            assert work(1)['capture_recovery']['state'] == 'awaiting_input'
            turn(1)
            assert work(2)['captured_rounds'] == 1
            started, read = [], []
            def process(*a, **k):
                started.append(a)
                raise AssertionError('the input hook started a process')
            def parse(*a, **k):
                read.append(a)
                raise AssertionError('the input hook read the transcript')
            with patch.object(subprocess,'run',side_effect=process), patch.object(subprocess,'Popen',side_effect=process), \\
                    patch.object(transcripts,'read',side_effect=parse), patch.object(rg,'profile',side_effect=parse), \\
                    patch.object(rg.core,'spawn_worker') as worker:
                quiet = submit(3)
                assert not worker.called, 'nothing new, yet a worker was started'
                turn(2)
                submit(4)
            assert not started and not read and '진단' not in quiet, (started, read, quiet)
            assert worker.call_count == 1 and json.loads(worker.call_args.kwargs['stdin'])['defer']
            assert conversation_capture.status(VAULT, 'claude','own')['prompt_count'] == 4
        ''')

    def test_worker_that_cannot_start_hands_review_to_the_session(self):
        # #192 review (#198): the route's verdict comes from the hooks' worker. A host that cannot
        # start one never learned a verdict and stayed on the fork's route, so the in-session
        # 9·15-turn review never took over.
        base_tests.GrowthTests().check_case('''
            import os
            from osk import response_growth as rg, worker_preflight
            from unittest.mock import patch
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(VAULT.root / 'native.jsonl'),
                   'cwd':str(VAULT.root),'hook_event_name':'UserPromptSubmit'}
            os.environ.pop('OSK_HOOK_WORKER')
            unchecked = rg.route(VAULT, env)
            assert unchecked['mode'] == 'background' and unchecked['probe_due'], unchecked
            with patch.object(rg.core,'spawn_worker',side_effect=OSError('no processes left')):
                assert rg.defer(VAULT, env,'key',probe_due=True) is False
            fallback = rg.route(VAULT, env)
            assert fallback['mode'] == 'foreground' and fallback['changed'], fallback
            assert 'no processes left' in fallback['reason'] and not fallback['probe_due'], fallback
        ''')

    def test_start_and_stop_hooks_start_no_process_and_read_no_transcript(self):
        # #192: SessionStart parsed the whole transcript (49.7 MB: 2.2 s, 6.2 s at full CPU; the
        # slowest tenth of October's compactions 28 s) and a foreground Stop parsed it every turn.
        # Both hand the capture to the input hook's worker now.
        base_tests.GrowthTests().check_case('''
            import io, os, subprocess, threading, types
            from osk import response_growth as rg, integration, transcripts, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from osk._portalock import lock_exclusive, unlock
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as start, capture_stop as stop
            native = VAULT.root / 'native.jsonl'
            def turn(n):
                with native.open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'type':'user','sessionId':'own','uuid':f'u{n}','message':{'role':'user','content':'q'}})+'\\n'+
                        json.dumps({'type':'assistant','sessionId':'own','uuid':f'a{n}','message':{'role':'assistant','id':f'm{n}',
                                    'model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'a'}]}})+'\\n')
            # No fork CLI is configured: Stop's route is the foreground one, where it captured in place.
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root)}
            os.environ.pop('OSK_HOOK_WORKER')
            os.environ.update(OSK_UPDATE_CHECK='0', CLAUDE_CONFIG_DIR=str(VAULT.root / 'claude-home'))
            def run(hook, event, n):
                out = types.SimpleNamespace(buffer=io.BytesIO())
                with patch.object(sys,'stdin',io.StringIO(json.dumps(dict(env, hook_event_name=event, source='resume', turn_id=n)))), \\
                        patch.object(sys,'stdout',out), patch.object(rg.core,'spawn_worker') as worker:
                    hook.main()
                return out.buffer.getvalue().decode('utf-8'), [json.loads(c.kwargs['stdin']) for c in worker.call_args_list]
            turn(1)
            _, jobs = run(start, 'SessionStart', 1)    # (the first hook learns the folder's session key with git)
            rg.after_hook(VAULT, jobs[0])
            assert conversation_capture.status(VAULT, 'claude','own')['captured_rounds'] == 1
            started, read = [], []
            def process(*a, **k):
                started.append(a)
                raise AssertionError('a hook started a process')
            def parse(*a, **k):
                read.append(a)
                raise AssertionError('a hook read the transcript')
            with patch.object(subprocess,'run',side_effect=process), patch.object(subprocess,'Popen',side_effect=process), \\
                    patch.object(transcripts,'read',side_effect=parse), patch.object(rg,'profile',side_effect=parse):
                turn(2)
                begun, jobs = run(start, 'SessionStart', 2)    # resumed with a turn not yet captured
                turn(3)
                ended, more = run(stop, 'Stop', 3)
            assert not started and not read and '진단' not in begun and not ended, (started, read, begun, ended)
            assert [j['defer'] for j in jobs + more] == [True, True], (jobs, more)
            # A worker that finds another one running waits its turn: Stop's turn is not dropped.
            with conversation_state.state_path(VAULT, 'claude','own').with_suffix('.defer.lock').open('a+b') as busy:
                lock_exclusive(busy)
                waiting = threading.Thread(target=rg.after_hook, args=(VAULT, more[0]))
                waiting.start()
                waiting.join(2)
                assert waiting.is_alive(), 'a busy worker dropped the turn Stop handed over'
                unlock(busy)
            waiting.join(60)
            assert conversation_capture.status(VAULT, 'claude','own')['captured_rounds'] == 3
            # A tracked conversation whose file left the hook's path is no new one: its worker
            # finds the file by ID, as the in-place capture did (#199 review).
            moved = VAULT.root / 'claude-home/projects/elsewhere/own.jsonl'
            moved.parent.mkdir(parents=True)
            turn(4)
            native.replace(moved)
            _, jobs = run(stop, 'Stop', 4)
            assert jobs, 'a moved transcript was skipped as a new conversation'
            rg.after_hook(VAULT, jobs[0])
            assert conversation_capture.status(VAULT, 'claude','own')['captured_rounds'] == 4
            native = moved
            # The worker's failure lands after its Stop: the next Stop says it once, as the input hook does.
            native.write_text(native.read_text(encoding='utf-8').replace('"u1"', '"u0"'), encoding='utf-8')
            rg.after_hook(VAULT, more[0])
            said = [run(stop, 'Stop', n)[0] for n in (5, 6)]
            assert 'capture diagnostic' in said[0] and 'prefix changed' in said[0] and not said[1], said
        ''')

    def test_input_hook_imports_no_yaml_or_tz_data(self):
        # #192 D: yaml and the tz data were a quarter of the input hook's imports, yet the hook
        # parses no node and writes no time. They load where they are used.
        base_tests.GrowthTests().check_case('''
            import re, subprocess
            hook = Path(core.__file__).resolve().parents[1] / 'scripts/hooks/claude_prompt_submit.py'
            prompt = {'session_id':'own','transcript_path':str(VAULT.root / 'native.jsonl'),'cwd':str(VAULT.root),
                      'hook_event_name':'UserPromptSubmit','prompt':'q'}
            r = subprocess.run([sys.executable, '-X', 'importtime', str(hook)], input=json.dumps(prompt),
                               capture_output=True, text=True, encoding='utf-8', timeout=60)
            loaded = set(re.findall(r'^import time:.*\\|\\s*(\\S+)$', r.stderr, re.M))
            assert r.returncode == 0 and 'osk.integration' in loaded, (r.returncode, r.stdout, r.stderr[-2000:])
            assert not {'yaml', 'zoneinfo'} & loaded, sorted(loaded)
        ''')

    def test_stop_detaches_and_waits_for_native_final_flush(self):
        base_tests.GrowthTests().check_case('''
            import os, subprocess, time
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            scope_memory.replace(VAULT, VAULT.root.name, '', space='00_Scope/W1')
            native = VAULT.root / 'native.jsonl'
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native), 'cwd':str(VAULT.root)}
            rg.initialize(VAULT, env)
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
                status = conversation_capture.status(VAULT, 'claude','own')
                if status.get('response_growth_stop'):
                    break
                time.sleep(0.05)
            assert status['response_growth']['count'] == 1, status
            assert status['captured_rounds'] == 1 and status['reviewed_rounds'] == 0, status
            assert status['response_growth_stop']['state'] == 'not_due', status
        ''')
