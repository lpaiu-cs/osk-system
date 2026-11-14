"""응답 뒤 검토(fork)의 수트의 한 갈래: 어느 런타임으로 도는가 — 데스크톱의 판, Codex의 설정, 구독의 관문.

`tests/test_response_growth.py`의 `ResponseGrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from response_growth_cases.support import REAL_HOOK_READY, VAULT
from osk import response_growth as rg, worker_preflight  # noqa: E402
import test_growth as base_tests  # noqa: E402


class RuntimeCases:
    def test_resumed_codex_uses_only_its_own_runtime_version(self):
        with tempfile.TemporaryDirectory() as folder:
            source = {'harness':'codex','conversation_id':'own','cwd':folder,
                      'cli_version':'0.155.0-alpha.9.2','model':'parent-model',
                      'model_provider':'openai','effort':'high','approval_policy':'never',
                      'sandbox_policy':{'type':'read-only'}}
            executable = str(Path(sys.executable).resolve())
            runtime = {'CODEX_THREAD_ID':'own','CODEX_VERSION':'0.155.0-alpha.16'}
            calls = []
            def inspect(argv, **kwargs):
                calls.append(argv)
                output = ('true' if argv[0] == 'git' else
                          'codex-cli 0.155.0-alpha.16' if '--version' in argv else
                          'Logged in using ChatGPT')
                return subprocess.CompletedProcess(argv, 0, output, '')
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                argv = rg.command(VAULT, source, executable, runtime)
                self.assertEqual(argv[0], executable)
                self.assertEqual(argv[argv.index('--model')+1], 'parent-model')
                self.assertIn('--ephemeral', argv)
                self.assertEqual(source['cli_version'], '0.155.0-alpha.9.2')
                for env in ({}, {**runtime,'CODEX_THREAD_ID':'another'},
                            {'CODEX_VERSION':runtime['CODEX_VERSION']},
                            {**runtime,'CODEX_VERSION':'0.156.0'}):
                    with self.subTest(env=env), self.assertRaisesRegex(ValueError, 'version differs'):
                        rg.command(VAULT, source, executable, env)
                with self.assertRaisesRegex(ValueError, 'runtime version'):
                    rg.command(VAULT, source, executable, {**runtime,'CODEX_VERSION':'invalid'})
            self.assertTrue(all(c[1:] in (['--version'], ['login','status']) or c[0]=='git'
                                for c in calls), 'preflight started inference')

    def test_desktop_update_resolves_only_matching_sibling_and_keeps_auth_gate(self):
        from osk import native_cli, worker_process
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'OpenAI/Codex/bin'
            old = base / ('1' * 16) / 'codex.exe'
            same = base / ('2' * 16) / 'codex.exe'
            newer = base / ('3' * 16) / 'codex.exe'
            for p in (same, newer, base / 'codex.exe'):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
                p.chmod(0o700)
            calls = []
            def inspect(argv, **kwargs):
                calls.append(argv)
                if Path(argv[0]) == old:
                    return subprocess.CompletedProcess(argv, 1, '', 'retired binary cannot start')
                text = ('codex-cli ' + ('0.155.0-alpha.16' if Path(argv[0]) == same else '0.156.0')
                        if argv[1:] == ['--version'] else 'Logged in using an API key')
                return subprocess.CompletedProcess(argv, 0, text, '')
            with patch.object(native_cli.subprocess, 'run', side_effect=inspect):
                self.assertEqual(native_cli.resolve(str(old), version='0.155.0-alpha.16'), str(same))
                self.assertEqual(native_cli.resolve(str(old)), str(newer))
                self.assertEqual(worker_process.check_command(VAULT, [str(old)])['executable'], str(newer))
                self.assertEqual(worker_process.check_command(VAULT, [str(same)], follow_desktop_update=False)['executable'], str(same))
                old.parent.mkdir(); old.touch()
                self.assertEqual(native_cli.resolve(str(old)), str(newer))
                with self.assertRaisesRegex(ValueError, 'matching Codex'):
                    native_cli.resolve(str(old), version='0.154.0')
                source = {'harness':'codex','conversation_id':'own','cwd':folder,
                          'cli_version':'0.155.0-alpha.16','model':'same-model','model_provider':'openai',
                          'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription'):
                    rg.command(VAULT, source, str(old), {})
                source['cli_version'] = '0.154.0'  # Creation version survives an app update.
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription'):
                    rg.command(VAULT, source, str(old), {'CODEX_THREAD_ID':'own',
                                                'CODEX_VERSION':'0.155.0-alpha.16'})
            self.assertTrue(all(Path(c[0]) != base / 'codex.exe' for c in calls))
            foreign = Path(folder) / 'other-cli.exe'
            with patch.object(native_cli.subprocess, 'run', side_effect=AssertionError('foreign scan')):
                self.assertEqual(native_cli.resolve(str(foreign), version='0.155.0-alpha.16'), str(foreign))

    def test_claude_desktop_update_follows_only_exact_source_version(self):
        from osk import native_cli
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'Claude/claude-code'
            old, same = base / '2.1.251/claude.exe', base / '2.1.280/claude.exe'
            for p in (old, same):
                p.parent.mkdir(parents=True)
                p.touch()
            self.assertEqual(native_cli.resolve(str(old), version='2.1.280'), str(same))
            self.assertEqual(native_cli.resolve(str(old)), str(old))  # No newest guess.
            for version in ('2.1.266', '../2.1.280'):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(old), version=version)
            pinned = Path(folder) / '.local/bin/claude.exe'
            self.assertEqual(native_cli.resolve(str(pinned), version='2.1.280'), str(pinned))
            source = {'harness': 'claude', 'conversation_id': 'own', 'model': 'claude-same',
                      'cwd': folder, 'version': '2.1.280', 'permission_mode': 'default'}
            env = {'CLAUDE_CONFIG_DIR': folder}
            calls = []
            def inspect(argv, **kwargs):
                calls.append(argv)
                text = ('2.1.280 (Claude Code)' if argv[1:] == ['--version'] else
                        json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'}))
                return subprocess.CompletedProcess(argv, 0, text, '')
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                self.assertEqual(rg.command(VAULT, source, str(old), env)[0], str(same))
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    rg.command(VAULT, {**source, 'version': '2.1.266'}, str(old), env)
            self.assertTrue(calls and all(c[0] == str(same) and c[1:] in (['--version'], ['auth', 'status'])
                                          for c in calls), 'preflight started inference')
            # The relocated file still faces the exact --version guard.
            with patch.object(rg.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '2.1.275', '')):
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(VAULT, source, str(old), env)
            # Desktop pruned the configured release: a new conversation's routing checks an
            # installed one, but a version-less source still never reaches a fork.
            gone, fresh = str(base / '2.1.270/claude.exe'), {**source, 'version': None}
            self.assertEqual(native_cli.resolve(gone), str(same))
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                self.assertEqual(worker_preflight.preflight(VAULT, fresh, gone, env, require_version=False), str(same))
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(VAULT, fresh, gone, env)

    def test_claude_desktop_hash_directory_keeps_version_and_installation_boundary(self):
        from osk import native_cli, worker_process
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'Claude/claude-code'
            old = base / '2.1.280/claude.exe'
            same = base / '2.1.286/635c1867224a/claude.exe'
            newer = base / '2.1.287/abcdef012345/claude.exe'
            for p in (same, newer):
                p.parent.mkdir(parents=True)
                p.touch()
                p.chmod(0o700)
            self.assertEqual(native_cli.resolve(str(old), version='2.1.286'), str(same))
            self.assertEqual(native_cli.resolve(str(same), version='2.1.287'), str(newer))
            self.assertEqual(native_cli.resolve(str(old)), str(newer))
            self.assertEqual(worker_process.check_command(VAULT, [str(old)])['executable'], str(newer))
            self.assertEqual(native_cli.resolve(str(same)), str(same))
            for version in ('2.1.285', '../2.1.286'):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(old), version=version)
            source = {'harness':'claude', 'conversation_id':'own', 'model':'same-model',
                      'version':'2.1.286', 'permission_mode':'default', 'cwd':folder}
            def inspect(argv, **kwargs):
                self.assertEqual(argv[0], str(same))
                self.assertIn(argv[1:], (['--version'], ['auth', 'status']))
                text = ('2.1.286 (Claude Code)' if argv[1:] == ['--version'] else
                        json.dumps({'loggedIn':True, 'authMethod':'claude.ai', 'subscriptionType':'max'}))
                return subprocess.CompletedProcess(argv, 0, text, '')
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                self.assertEqual(rg.command(VAULT, source, str(old), {'CLAUDE_CONFIG_DIR':folder})[0], str(same))
            # Two unpinned builds of one version are ambiguous; an existing explicit pin is not.
            other = same.parent.parent / 'abcdef012345/claude.exe'
            other.parent.mkdir(); other.touch()
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                native_cli.resolve(str(old), version='2.1.286')
            self.assertEqual(native_cli.resolve(str(same), version='2.1.286'), str(same))
            # A hash-shaped symlink must not select a binary outside this installation.
            other.unlink(); other.parent.rmdir()
            foreign = Path(folder) / 'foreign'
            foreign.mkdir(); (foreign / 'claude.exe').touch()
            try:
                other.parent.symlink_to(foreign, target_is_directory=True)
            except OSError:
                pass
            else:
                self.assertEqual(native_cli.resolve(str(old), version='2.1.286'), str(same))
            junk = base / '2.1.288/not-a-build/claude.exe'
            junk.parent.mkdir(parents=True); junk.touch()
            with self.assertRaisesRegex(ValueError, 'matching Claude'):
                native_cli.resolve(str(old), version='2.1.288')

    def test_claude_desktop_pin_follows_the_store_install_to_claude_data(self):
        # 10-21: app 3.16 moved a store install's data from Packages/Claude_<package>/LocalCache/
        # Roaming/Claude to %LOCALAPPDATA%/Claude-Data/claude_<package> and left the old folder empty.
        # The pinned CLI follows the same package's new folder, and that folder is a Desktop layout.
        from osk import native_cli, worker_process
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / 'Local'
            store = local / 'Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude'
            pinned = store / 'claude-code/2.1.286/635c1867224a/claude.exe'
            moved = local / 'Claude-Data/claude_pzs8sxrjxfjjc/claude-code'
            older, same = moved / '2.1.299/fbd8e0c0a9e1/claude.exe', moved / '2.1.304/aed62990b91e/claude.exe'
            store.mkdir(parents=True)
            (store / '.user-data-left.claude_pzs8sxrjxfjjc').touch()
            for p in (older, same):
                p.parent.mkdir(parents=True)
                p.touch()
                p.chmod(0o700)
            with patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
                self.assertEqual(native_cli.resolve(str(pinned), version='2.1.304'), str(same))
                self.assertEqual(native_cli.resolve(str(pinned)), str(same))
                self.assertEqual(worker_process.check_command(VAULT, [str(pinned)])['executable'], str(same))
                # A pin into the new folder follows the source's version like any Desktop install.
                self.assertEqual(native_cli.resolve(str(older), version='2.1.304'), str(same))
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(older), version='2.1.305')
                # Another package's folder is another installation: it is not followed here.
                other = local / 'Packages/Claude_otherpackage1/LocalCache/Roaming/Claude/claude-code/2.1.286/claude.exe'
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(other))
            # With nowhere to follow, a dead pin is "unavailable", not a path error (was WinError 3).
            with patch.dict(os.environ, {'LOCALAPPDATA': str(Path(folder) / 'none')}):
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(pinned))
            # `claude_<x>` outside Claude-Data is not a Desktop layout: the pin stays as written.
            elsewhere = Path(folder) / 'tools/claude_x/claude-code/2.1.304/claude.exe'
            self.assertEqual(native_cli.resolve(str(elsewhere), version='2.1.305'), str(elsewhere))

    def test_claude_desktop_pin_seen_inside_the_package_follows_the_one_claude_data_install(self):
        # #257: setup ran inside the store package and wrote the pin as %APPDATA%/Claude/claude-code,
        # which the scheduled task does not see. The pin names no package, so it follows the one
        # Claude-Data install; with two installs it stays a dead pin.
        from osk import native_cli, worker_process
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / 'Local'
            pinned = Path(folder) / 'Roaming/Claude/claude-code/2.1.299/f0584be0fbaf/claude.exe'
            data = local / 'Claude-Data/claude_pzs8sxrjxfjjc/claude-code'
            older, newest = data / '2.1.304/aed62990b91e/claude.exe', data / '2.1.308/c17a819afb40/claude.exe'
            for p in (older, newest):
                p.parent.mkdir(parents=True)
                p.touch()
                p.chmod(0o700)
            with patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
                self.assertEqual(native_cli.resolve(str(pinned)), str(newest))
                self.assertEqual(worker_process.check_command(VAULT, [str(pinned)])['executable'], str(newest))
                self.assertEqual(native_cli.resolve(str(pinned), version='2.1.304'), str(older))
                (local / 'Claude-Data/claude_otherpackage1/claude-code').mkdir(parents=True)
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    native_cli.resolve(str(pinned))

    def test_runtime_version_recovery_preserves_counters_and_reaches_supervisor(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch, Mock
            import os, subprocess
            patch.object(worker_preflight, '_codex_hook_ready', return_value=True).start()  # see test_codex_preflight_requires_*
            native = VAULT.root / 'native.jsonl'
            rows = [
                {'type':'session_meta','payload':{'id':'own','cwd':str(VAULT.root),
                    'cli_version':'0.155.0-alpha.9.2','model_provider':'openai'}},
                {'type':'turn_context','payload':{'model':'parent-model','effort':'high',
                    'cwd':str(VAULT.root),'approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}}]
            native.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'codex':sys.executable}))
            env = {'harness':'codex','session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root)}
            with conversation_state.state_lock(VAULT, 'codex','own') as p:
                saved = conversation_state.load_state(VAULT, p,'codex','own')
                clock = {'counter':'finals','seen':['t1'],'count':9,'attempted_count':0,'history_baselined':True}
                saved.update(prompt_count=15, response_growth=clock)
                conversation_state.save_state(p,saved)
            def inspect(argv, **kwargs):
                assert argv[0]=='git' or argv[1:] in (['--version'],['login','status']), argv
                output = ('true' if argv[0]=='git' else 'codex-cli 0.155.0-alpha.16'
                          if '--version' in argv else 'Logged in using ChatGPT')
                return subprocess.CompletedProcess(argv,0,output,'')
            # The hooks' worker asks the CLI (probe); route() reads its verdict.
            with patch.object(rg.subprocess,'run',side_effect=inspect), patch.dict(os.environ,
                    {'CODEX_THREAD_ID':'other','CODEX_VERSION':'0.155.0-alpha.16'}):
                assert rg.probe(VAULT, env)['mode']=='foreground' and rg.route(VAULT, env)['mode']=='foreground'
                with patch.dict(os.environ,{'CODEX_THREAD_ID':'own'}):
                    assert rg.probe(VAULT, env)['mode']=='background' and rg.route(VAULT, env)['mode']=='background'
                    with patch.object(rg.subprocess,'Popen',return_value=Mock()) as launch:
                        assert rg.launch(VAULT, env,'own')
                        inherited = launch.call_args.kwargs['env']
                        assert inherited['CODEX_THREAD_ID']=='own'
                        assert inherited['CODEX_VERSION']=='0.155.0-alpha.16'
                        assert inherited['OSK_GROWTH_WORKER']=='1'
            state = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex','own'),'codex','own')
            assert state['response_growth']==clock and state['prompt_count']==15, state
            assert state['reviewed_count']==saved['reviewed_count']
            assert state['rounds']==saved['rounds']
        ''')

    def test_codex_runtime_comes_only_from_nearest_managed_ancestor(self):
        from osk import native_cli
        with tempfile.TemporaryDirectory() as folder:
            managed = Path(folder) / 'OpenAI/Codex/bin' / ('a' * 16) / 'codex.exe'
            standalone = Path(folder) / 'Programs/OpenAI/Codex/bin/codex.exe'
            shell = ('cmd.exe', str(Path(folder) / 'cmd.exe'))
            def version(argv, **kwargs):
                assert argv[1:] == ['--version'], argv
                return subprocess.CompletedProcess(argv, 0, 'codex-cli 0.155.0-alpha.16\n', '')
            with patch.object(native_cli.subprocess, 'run', side_effect=version):
                for chain, expected in (([shell, ('codex.exe', str(managed))], '0.155.0-alpha.16'),
                                        ([('CODEX.EXE', str(managed))], '0.155.0-alpha.16'),
                                        ([('codex.exe', str(standalone)), ('codex.exe', str(managed))], None),
                                        ([('codex.exe', ''), ('codex.exe', str(managed))], None),
                                        ([shell], None)):
                    with self.subTest(chain=chain), patch.object(native_cli, '_ancestors', return_value=chain):
                        self.assertEqual(native_cli.runtime_codex(), expected)
                with patch.object(native_cli, '_ancestors', return_value=[('codex.exe', str(managed))]):
                    with patch.dict(rg.os.environ, {'CODEX_THREAD_ID': 'own'}):
                        self.assertEqual(rg.runtime_env('codex', 'own'),
                                         {'CODEX_THREAD_ID': 'own', 'CODEX_VERSION': '0.155.0-alpha.16'})
                        self.assertEqual(rg.runtime_env('codex', 'another'), {})  # Other thread's process.
                    self.assertEqual(rg.runtime_env('claude', 'own'), {})
        if rg.os.name == 'nt':  # The real Toolhelp walk reaches this process's parent.
            chain = list(native_cli._ancestors())
            self.assertTrue(chain and all(isinstance(n, str) and n for n, _ in chain), chain)

    def test_codex_hook_binds_desktop_runtime_for_route_and_supervisor(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, native_cli, worker_preflight
            from unittest.mock import patch, Mock
            import os, subprocess
            patch.object(worker_preflight, '_codex_hook_ready', return_value=True).start()  # see test_codex_preflight_requires_*
            for key in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS'):
                os.environ.pop(key, None)  # Codex hook processes carry none of these.
            native = VAULT.root / 'native.jsonl'
            rows = [
                {'type':'session_meta','payload':{'id':'own','cwd':str(VAULT.root),
                    'cli_version':'0.155.0-alpha.9.2','model_provider':'openai'}},
                {'type':'turn_context','payload':{'model':'parent-model','effort':'high',
                    'cwd':str(VAULT.root),'approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}}]
            native.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            rg.config_path(VAULT).parent.mkdir(exist_ok=True)
            rg.config_path(VAULT).write_text(json.dumps({'codex':sys.executable}))
            env = {'session_id':'own','transcript_path':str(native),'cwd':str(VAULT.root)}
            managed = VAULT.root / 'OpenAI/Codex/bin' / ('a'*16) / 'codex.exe'
            managed.parent.mkdir(parents=True)
            managed.touch()
            def inspect(argv, **kwargs):
                assert argv[0]=='git' or argv[1:] in (['--version'],['login','status']), argv
                output = ('true' if argv[0]=='git' else 'codex-cli 0.155.0-alpha.16'
                          if '--version' in argv else 'Logged in using ChatGPT')
                return subprocess.CompletedProcess(argv,0,output,'')
            with patch.object(rg.subprocess,'run',side_effect=inspect):
                with patch.object(native_cli,'_ancestors',return_value=[]):
                    route = rg.probe(VAULT, env)  # No managed ancestor: strict transcript version.
                    assert route['mode']=='foreground' and 'version differs' in route['reason'], route
                chain = [('cmd.exe', 'cmd.exe'), ('codex.exe', str(managed))]
                # The hook names its Desktop binary to its detached worker, whose ancestry
                # no longer reaches Codex; the worker asks that binary's version.
                with patch.object(native_cli,'_ancestors',return_value=chain):
                    named = rg.runtime_env('codex','own',ask=False)
                assert named == {'CODEX_THREAD_ID':'own', native_cli.DESKTOP_ENV:str(managed)}, named
                with patch.dict(os.environ, named), patch.object(native_cli,'_ancestors',return_value=[]):
                    assert rg.probe(VAULT, env)['mode']=='background' and rg.route(VAULT, env)['mode']=='background'
                with patch.object(native_cli,'_ancestors',return_value=chain):
                    with patch.object(rg.subprocess,'Popen',return_value=Mock()) as launch:
                        assert rg.launch(VAULT, env,'own')
                inherited = launch.call_args.kwargs['env']
                # Stop names its Desktop binary too (#192); the supervisor asks its version (run()).
                assert inherited['CODEX_THREAD_ID']=='own' and inherited[native_cli.DESKTOP_ENV]==str(managed)
                assert 'CODEX_VERSION' not in inherited, inherited
                # The supervisor has no ancestor: run() asks the named binary's version, and its
                # command() re-runs full preflight on that binding.
                with patch.dict(os.environ, inherited), patch.object(native_cli,'_ancestors',return_value=[]), \\
                        patch.object(rg.growth,'run',return_value={'ok':True}) as fork:
                    rg.run(VAULT, rg.profile(VAULT, str(native),'codex','own'), {'harness':'codex','conversation_id':'own'}, sys.executable)
                argv, forked = fork.call_args.args[1], fork.call_args.kwargs['worker_env']
                assert argv[0]==sys.executable and argv[argv.index('--model')+1]=='parent-model', argv
                assert forked['CODEX_VERSION']=='0.155.0-alpha.16', forked.get('CODEX_VERSION')
            # Before a rollout exists the runtime binding, not the newest install, picks the binary.
            newer = VAULT.root / 'OpenAI/Codex/bin' / ('b'*16) / 'codex.exe'
            newer.parent.mkdir(parents=True)
            newer.touch()
            rg.config_path(VAULT).write_text(json.dumps({'codex':str(newer)}))
            logins = []
            def installed(argv, **kwargs):
                if argv[1:] == ['login','status']:
                    logins.append(argv[0])
                version = 'codex-cli 0.156.0' if 'b'*16 in argv[0] else 'codex-cli 0.155.0-alpha.16'
                return subprocess.CompletedProcess(argv,0,'true' if argv[0]=='git' else version
                                                   if '--version' in argv else 'Logged in using ChatGPT','')
            with patch.object(rg.subprocess,'run',side_effect=installed), \\
                    patch.object(native_cli,'_ancestors',return_value=chain):
                assert rg.probe(VAULT, {'harness':'codex','session_id':'fresh','cwd':str(VAULT.root)})['mode']=='background'
            assert logins == [str(managed)], logins
        ''')

    def test_codex_trusted_non_git_project_passes_only_its_own_key(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as folder:
            exe = Path(home) / 'cli.exe'
            exe.touch()
            source = {'harness':'codex','conversation_id':'own','cwd':folder,'cli_version':'0.155.0',
                      'model':'parent-model','model_provider':'openai','effort':'high',
                      'approval_policy':'never','sandbox_policy':{'type':'read-only'}}
            def inspect(argv, **kwargs):
                if argv[0] == 'git':
                    return subprocess.CompletedProcess(argv, 128, '', 'not a git repository')
                return subprocess.CompletedProcess(argv, 0, 'codex-cli 0.155.0' if '--version' in argv
                                                   else 'Logged in using ChatGPT', '')
            def project(path, level='trusted'):
                return f'[projects.{json.dumps(path)}]\ntrust_level = "{level}"\n'
            config, env = Path(home) / 'config.toml', {'CODEX_HOME': home}
            # Codex writes e:\vv\x for a session cwd E:\vv\x; other spellings are not guessed.
            own, refused = rg.os.path.normcase(folder), 'Git worktree or trusted'
            cases = [('', refused), (project(own), None), (project(own, 'untrusted'), refused),
                     (project(rg.os.path.normcase(str(Path(folder).parent))), refused), ('[projects', '')]
            if rg.os.name == 'nt':
                cases += [(project(folder.upper()), refused),
                          (project(own) + project(folder.upper(), 'untrusted'), refused)]
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                for body, error in cases:
                    config.write_text(body, encoding='utf-8')
                    with self.subTest(body=body):
                        if error is None:
                            argv = rg.command(VAULT, source, str(exe), env)
                            self.assertNotIn('--skip-git-repo-check', argv)
                        else:
                            with self.assertRaisesRegex(ValueError, error):
                                rg.command(VAULT, source, str(exe), env)

    def test_codex_pins_endpoint_and_refuses_unpinnable_ones_before_login_check(self):
        with tempfile.TemporaryDirectory() as home:
            exe = Path(home) / 'cli.exe'
            exe.touch()
            source = {'harness':'codex','conversation_id':'own','cwd':home,'cli_version':'0.155.0',
                      'model':'parent-model','model_provider':'openai','effort':'high',
                      'approval_policy':'never','sandbox_policy':{'type':'read-only'}}
            calls = []
            def inspect(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, 'true' if argv[0] == 'git' else 'codex-cli 0.155.0'
                                                   if '--version' in argv else 'Logged in using ChatGPT', '')
            bridge = 'http://127.0.0.1:17841/v1'
            # A top-level bridge/proxy is overridden by the pinned -c; profile/provider/env ones are not.
            cases = [('', {}, True), ('openai_base_url = "https://chatgpt.com/backend-api/codex/"', {}, True),
                     (f'openai_base_url = "{bridge}"', {}, True),
                     ('openai_base_url = "https://chatgpt.com.proxy/backend-api/codex"', {}, True),
                     ('', {'OPENAI_BASE_URL': 'https://api.openai.com/v1'}, False), ('', {'OPENAI_BASE_URL': bridge}, False),
                     (f'profile = "p"\n[profiles.p]\nopenai_base_url = "{bridge}"', {}, False),
                     ('profile = "p"\n[profiles.p]\nopenai_base_url = "https://api.openai.com/v1"', {}, False),
                     (f'[model_providers.openai]\nbase_url = "{bridge}"', {}, False),
                     # A profile's own value wins over every -c pin: its provider and its policy.
                     (f'profile = "p"\n[profiles.p]\nmodel_provider = "bridge"\n'
                      f'[model_providers.bridge]\nbase_url = "{bridge}"', {}, False),
                     ('profile = "p"\n[profiles.p]\nsandbox_mode = "danger-full-access"', {}, False),
                     ('profile = "p"\n[profiles.p]\nmodel_provider = "openai"\nmodel_verbosity = "low"', {}, True),
                     ('[profiles.q]\nsandbox_mode = "danger-full-access"', {}, True),
                     (f'chatgpt_base_url = "{bridge}"', {}, False),
                     ('chatgpt_base_url = "https://chatgpt.com/backend-api/"', {}, True)]
            (Path(home) / '.codex').mkdir()
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                # cwd is home: its .codex/config.toml is the project layer native exec also reads.
                for (body, extra, allowed), layer in ((c, l) for c in cases for l in ('config.toml', '.codex/config.toml')):
                    for name in ('config.toml', '.codex/config.toml'):
                        (Path(home) / name).write_text(body if name == layer else '', encoding='utf-8')
                    env, calls[:] = {'CODEX_HOME': home, **extra}, []
                    with self.subTest(body=body, env=extra, layer=layer):
                        if allowed:
                            argv = rg.command(VAULT, source, str(exe), env)
                            pins = [argv[i+1] for i, v in enumerate(argv) if v == '-c' and 'base_url' in argv[i+1]]
                            self.assertEqual(pins, ['openai_base_url="https://chatgpt.com/backend-api/codex"'])
                        else:
                            for check in (lambda: rg.command(VAULT, source, str(exe), env),
                                          lambda: worker_preflight.preflight(VAULT, source, str(exe), env, require_version=False)):
                                with self.assertRaisesRegex(ValueError, 'non-first-party'):
                                    check()
                            self.assertFalse([c for c in calls if c[1:] == ['login', 'status']])

    def test_codex_config_layers_follow_native_project_root_and_never_mask(self):
        with tempfile.TemporaryDirectory() as top:
            top = Path(top)
            outer, repo, home = top / 'outer', top / 'outer/repo', top / 'home'
            cwd = repo / 'a/b'
            (repo / '.git').mkdir(parents=True)
            for folder in (outer, repo, cwd):
                (folder / '.codex').mkdir(parents=True)
                (folder / '.codex/config.toml').write_text('', encoding='utf-8')
            home.mkdir()
            env = {'CODEX_HOME': str(home)}
            layer = lambda folder: str(folder / '.codex/config.toml')
            def layers(user):
                (home / 'config.toml').write_text(user, encoding='utf-8')
                return list(worker_preflight.codex_layers(env, str(cwd)))
            user = str(home / 'config.toml')
            # From the Git root down to cwd; a folder above the root is not a native layer.
            self.assertEqual(layers(''), [user, layer(repo), layer(cwd)])
            self.assertEqual(layers('project_root_markers = []'), [user, layer(cwd)])
            (outer / 'root.flag').touch()
            self.assertEqual(layers('project_root_markers = ["root.flag"]'), [user, layer(outer), layer(repo), layer(cwd)])
            with self.assertRaisesRegex(ValueError, 'project_root_markers'):
                layers('project_root_markers = ".git"')
            # The user layer selects a profile a project layer defines; a nearer layer cannot mask it.
            (Path(layer(repo))).write_text('[profiles.p]\nmodel_provider = "bridge"', encoding='utf-8')
            (Path(layer(cwd))).write_text('[profiles.p]\nmodel_provider = "openai"', encoding='utf-8')
            layers('profile = "p"')
            self.assertEqual(worker_preflight.codex_unpinned(worker_preflight.codex_layers(env, str(cwd))),
                             {layer(repo) + ': profiles.p.model_provider': 'bridge'})
            if rg.os.name == 'nt':  # %ProgramData%\OpenAI\Codex\config.toml sets system defaults.
                system = top / 'OpenAI/Codex/config.toml'
                system.parent.mkdir(parents=True)
                system.write_text('chatgpt_base_url = "http://127.0.0.1:17841/"', encoding='utf-8')
                with patch.dict(rg.os.environ, {'ProgramData': str(top)}):
                    self.assertEqual(layers('')[0], str(system))
                    self.assertIn(str(system) + ': chatgpt_base_url', worker_preflight.codex_unpinned(worker_preflight.codex_layers(env, str(cwd))))

    def test_codex_route_before_first_final_needs_no_sandbox_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'cli.exe'
            exe.touch()
            fresh = {'harness':'codex','conversation_id':'own','cwd':folder,'cli_version':'0.155.0',
                     'model_provider':'openai','finals':[]}
            def inspect(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, 'true' if argv[0] == 'git' else 'codex-cli 0.155.0'
                                                   if '--version' in argv else 'Logged in using ChatGPT', '')
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                self.assertEqual(worker_preflight.preflight(VAULT, fresh, str(exe), {}, require_version=False), str(exe))
                for source, require in ((dict(fresh, finals=['t1']), False), (fresh, True)):
                    with self.subTest(require=require), self.assertRaisesRegex(ValueError, 'sandbox policy'):
                        worker_preflight.preflight(VAULT, source, str(exe), {}, require_version=require)
                with self.assertRaisesRegex(ValueError, 'sandbox policy'):
                    rg.command(VAULT, dict(fresh, model='parent-model'), str(exe), {})

    def test_subscription_gate_and_exact_model_no_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'cli.exe'
            exe.touch()
            source = {'harness': 'claude', 'conversation_id': 'own', 'model': 'claude-same',
                      'cwd': folder, 'version': '2.1.266', 'permission_mode': 'default'}
            env = {'CLAUDE_CONFIG_DIR': folder}
            def completed(text):
                return subprocess.CompletedProcess([], 0, text, '')
            def auth(subscription):
                return completed(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
                                             'subscriptionType': subscription}))
            with patch.object(rg.subprocess, 'run', side_effect=[completed('2.1.266'), auth(None)]):
                with self.assertRaisesRegex(ValueError, 'subscription login'):
                    rg.command(VAULT, source, str(exe), env)
            with patch.object(rg.subprocess, 'run', side_effect=[completed('2.1.266'), auth('max')]):
                command = rg.command(VAULT, source, str(exe), env)
            self.assertIn('--no-session-persistence', command)
            self.assertEqual(command[command.index('--model') + 1], source['model'])
            self.assertNotIn('--bare', command)
            with patch.object(rg.subprocess, 'run', return_value=completed('2.1.251')) as call:
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(VAULT, source, str(exe), env)
                self.assertEqual(call.call_count, 1)
            (Path(folder) / 'settings.json').write_text('{"apiKeyHelper":"external-command"}', encoding='utf-8')
            with patch.object(rg.subprocess, 'run', return_value=completed('2.1.266')) as call:
                with self.assertRaisesRegex(ValueError, 'unverified API'):
                    rg.command(VAULT, source, str(exe), env)
                self.assertEqual(call.call_count, 1)

            source.update(harness='codex', cli_version='0.155.0', model='parent-model', model_provider='openai',
                          effort='max', approval_policy='never', sandbox_policy={'type': 'read-only'})
            with patch.object(rg.subprocess, 'run', side_effect=[completed('codex-cli 0.155.0'), completed('Logged in using ChatGPT'), completed('true')]):
                command = rg.command(VAULT, source, str(exe), env)
            self.assertIn('--ephemeral', command)
            self.assertEqual(command[-2:], ['own', '-'])
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            with patch.object(rg.subprocess, 'run', side_effect=[completed('codex-cli 0.155.0'), completed('Logged in using an API key')]):
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription login'):
                    worker_preflight.preflight(VAULT, source, str(exe), env)
            with patch.dict(rg.os.environ, {'OPENAI_API_KEY': 'fixture', 'ANTHROPIC_API_KEY': 'fixture',
                                            'OPENAI_BASE_URL': 'http://127.0.0.1:17841/v1'}):
                self.assertNotIn('OPENAI_API_KEY', worker_preflight.subscription_env())
                self.assertNotIn('OPENAI_BASE_URL', worker_preflight.subscription_env())
                self.assertNotIn('ANTHROPIC_API_KEY', worker_preflight.subscription_env())

    def test_subscription_env_strips_only_first_party_default_and_host_auth(self):
        host = {'CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH': '1', 'CLAUDE_CODE_MESSAGING_SOCKET': 'pipe',
                'CLAUDE_CODE_MESSAGING_TOKEN': 'fixture', 'CLAUDE_CODE_HOST_SESSION_ID': 'host'}
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'cli.exe'
            exe.touch()
            source = {'harness': 'claude', 'conversation_id': 'own', 'model': 'claude-same',
                      'cwd': folder, 'version': '2.1.280', 'permission_mode': 'default'}
            envs = []
            def inspect(argv, **kwargs):
                envs.append(kwargs['env'])
                text = ('2.1.280' if argv[1:] == ['--version'] else
                        json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'max'}))
                return subprocess.CompletedProcess(argv, 0, text, '')
            for url in ('https://api.anthropic.com', 'https://api.anthropic.com/'):
                with self.subTest(url=url), patch.dict(rg.os.environ, {**host, 'ANTHROPIC_BASE_URL': url,
                                                                       'CLAUDE_CONFIG_DIR': folder}):
                    env = worker_preflight.subscription_env()
                    self.assertFalse({'ANTHROPIC_BASE_URL', *host} & set(env))
                    envs.clear()
                    with patch.object(rg.subprocess, 'run', side_effect=inspect):
                        rg.command(VAULT, source, str(exe), env)
                    self.assertTrue(all(e is env for e in envs))  # auth status sees the child's env
            for url in ('http://127.0.0.1:8080', 'https://api.anthropic.com.proxy', 'https://api.anthropic.com/v2'):
                with self.subTest(url=url), patch.dict(rg.os.environ, {'ANTHROPIC_BASE_URL': url,
                                                                       'CLAUDE_CONFIG_DIR': folder}):
                    env = worker_preflight.subscription_env()
                    self.assertEqual(env['ANTHROPIC_BASE_URL'], url)
                    with patch.object(rg.subprocess, 'run', side_effect=inspect) as call:
                        with self.assertRaisesRegex(ValueError, 'unverified provider'):
                            rg.command(VAULT, source, str(exe), env)
                        self.assertEqual(call.call_count, 1)

    def test_codex_permission_overrides_roundtrip_through_toml(self):
        import tomllib
        source = {'harness': 'codex', 'conversation_id': 'own', 'model': 'same-model',
                  'effort': 'high', 'approval_policy': {'granular': {
                      'sandbox_approval': False, 'rules': True, 'mcp_elicitations': False,
                      'request_permissions': True, 'skill_approval': False}},
                  'approvals_reviewer': 'auto_review'}
        # Every known source policy narrows to read-only (#85), at any field value: read-only bars
        # the writes and network those fields grant. The approval policy stays the source's.
        for policy in ({'type': 'danger-full-access'}, {'type': 'read-only'}, {'type': 'read-only', 'network_access': True},
                       {'type': 'workspace-write', 'writable_roots': ['relative'], 'network_access': True,
                        'exclude_slash_tmp': 'true', 'exclude_tmpdir_env_var': False}):
            with self.subTest(policy=policy), patch.object(worker_preflight, 'preflight'):
                argv = rg.command(VAULT, {**source, 'sandbox_policy': policy}, 'unused', {})
                config = tomllib.loads('\n'.join(argv[i+1] for i, v in enumerate(argv) if v == '-c'))
                self.assertEqual(config['approval_policy'], source['approval_policy'])
                self.assertEqual(config['approvals_reviewer'], source['approvals_reviewer'])
                self.assertEqual(config['sandbox_mode'], 'read-only')
                self.assertNotIn('sandbox_workspace_write', config)
                self.assertNotIn('--skip-git-repo-check', argv)
                # The deny hook rides along as a trusted session-flag hook, on even if the user turned hooks off.
                self.assertIs(config['features']['hooks'], True)
                self.assertEqual(config['hooks'], worker_preflight.CODEX_FORK_HOOKS)
                self.assertNotIn('--dangerously-bypass-hook-trust', argv)
        # Read-only reads the whole disk, so it cannot stand in for an external sandbox or an
        # unknown field such as a read limit: those still refuse.
        for policy in (None, {}, {'type': 'external-sandbox'}, {'type': 'workspace-write', 'future_restriction': True},
                       {'type': 'read-only', 'access': {'type': 'restricted', 'readable_roots': []}}):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, 'sandbox'):
                worker_preflight.codex_overrides({**source, 'sandbox_policy': policy})
        for approval in ('bogus', {'granular': {'rules': True}}, {'granular': {
                'sandbox_approval': False, 'rules': True, 'mcp_elicitations': 'no'}}):
            with self.subTest(approval=approval), self.assertRaisesRegex(ValueError, 'approval policy'):
                worker_preflight.codex_overrides({**source, 'sandbox_policy': {'type': 'read-only'}, 'approval_policy': approval})

    def test_codex_preflight_requires_native_hooks_list_to_run_the_deny_hook(self):
        import time
        (key, state), = worker_preflight.CODEX_FORK_HOOKS['state'].items()
        ok = {'key': key, 'source': 'sessionFlags', 'enabled': True, 'trustStatus': 'trusted',
              'currentHash': state['trusted_hash']}
        # Stands in for `codex … app-server`: answers hooks/list, or leaves at EOF/hangs as told.
        server = ('import json, sys, time\nhooks = json.loads(sys.argv[1])\nfor line in sys.stdin:\n'
                  '    if json.loads(line).get("id") == 1:\n        if hooks == "eof": sys.exit()\n'
                  '        if hooks == "hang": time.sleep(60)\n'
                  '        print(json.dumps({"id": 1, "result": {"data": [{"hooks": hooks}]}}), flush=True)\n')
        with tempfile.TemporaryDirectory() as folder:
            for hooks, expected in (([ok], True), ([{**ok, 'trustStatus': 'untrusted'}], False),
                                    ([{**ok, 'enabled': False}], False), ([{**ok, 'currentHash': 'sha256:x'}], False),
                                    ([{**ok, 'key': 'other'}], False),
                                    # hooks off, a managed hooks-only policy or a config error: no entry at all
                                    ([], False), ('eof', False), ('hang', False)):
                with self.subTest(hooks=hooks):
                    started = time.monotonic()
                    argv = [sys.executable, '-c', server, json.dumps(hooks)]
                    self.assertIs(REAL_HOOK_READY(argv, folder, dict(rg.os.environ), timeout=3), expected)
                    self.assertLess(time.monotonic() - started, 30)
            # Preflight asks with the fork's own hook overrides in the source folder, and refuses otherwise.
            source = {'harness': 'codex', 'conversation_id': 'own', 'cwd': folder, 'cli_version': '0.155.0',
                      'model': 'parent-model', 'model_provider': 'openai', 'effort': 'high',
                      'approval_policy': 'never', 'sandbox_policy': {'type': 'read-only'}}
            def inspect(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, 'true' if argv[0] == 'git' else 'codex-cli 0.155.0'
                                                   if '--version' in argv else 'Logged in using ChatGPT', '')
            executable = str(Path(sys.executable).resolve())
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                argv = rg.command(VAULT, source, executable, {})
                (asked, cwd, *_), _ = self.hook_ready.call_args
                self.assertEqual((asked, cwd), ([executable, *worker_preflight._codex_hook_args(), 'app-server'], folder))
                self.assertEqual(argv[-6:-2], worker_preflight._codex_hook_args())
                self.hook_ready.return_value = False
                with self.assertRaisesRegex(ValueError, 'deny hook'):
                    rg.command(VAULT, source, executable, {})

    def test_desktop_source_routes_off_and_never_forks(self):
        # Desktop's first request leads with tool definitions only the running app serves
        # (codex_app over its pipe) — a CLI fork would recompute the whole parent context.
        source = {'harness': 'codex', 'conversation_id': 'own', 'model': 'parent-model',
                  'effort': 'high', 'approval_policy': 'never', 'sandbox_policy': {'type': 'read-only'}}
        desktop = {**source, 'source': 'vscode', 'originator': 'Codex Desktop'}
        with tempfile.TemporaryDirectory() as folder:
            native = Path(folder) / 'native.jsonl'
            native.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': 'own', 'cwd': folder, 'source': 'vscode', 'originator': 'Codex Desktop'}}) + '\n',
                encoding='utf-8')
            with patch.object(rg, 'configured', return_value='cli'), \
                    patch.object(worker_preflight, 'preflight', side_effect=AssertionError('CLI asked')):
                self.assertEqual(rg.check(VAULT, 'codex', 'own', str(native), folder),
                                 {'mode': 'off', 'reason': rg.DESKTOP_OFF})
        with patch.object(rg, '_remember') as remember, \
                patch.object(worker_preflight, 'preflight', side_effect=AssertionError('CLI asked')), \
                patch.object(rg.growth, 'run', side_effect=AssertionError('fork started')):
            result = rg.run(VAULT, desktop, {'harness': 'codex', 'conversation_id': 'own'}, 'unused')
        self.assertEqual(result['state'], 'unavailable')   # not a fork failure: never falls back in-session
        remember.assert_called_once_with(VAULT, 'codex', 'own', {'mode': 'off', 'reason': rg.DESKTOP_OFF})
        for kind, originator in (('exec', 'Codex Desktop'), ('cli', 'codex_cli_rs')):
            with self.subTest(source=kind), patch.object(worker_preflight, 'preflight'):
                argv = rg.command(VAULT, {**source, 'source': kind, 'originator': originator}, 'unused', {})
                self.assertEqual(argv[argv.index('--model')+1], source['model'])
                self.assertFalse([a for a in argv if 'code_mode_host' in a or 'codex_app' in a])

    def test_desktop_source_routes_off_without_a_configured_cli(self):
        # No fork CLI is no reason to review a Desktop conversation in-session: the hooks never
        # read the transcript, so the worker's verdict must outrank the missing-CLI one (PR #204).
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory, conversation_capture, conversation_cite, conversation_review, conversation_state, worker_preflight
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace(VAULT, 'own', '', space='00_Scope/W1')
            assert not rg.config_path(VAULT).exists()
            def transcript(sid, kind, originator):
                native = VAULT.root / f'{sid}.jsonl'
                rows = [{'type':'session_meta','payload':{'id':sid,'cwd':str(VAULT.root),'cli_version':'0.155.0',
                            'model_provider':'openai','source':kind,'originator':originator}},
                        {'type':'turn_context','payload':{'turn_id':'t1','cwd':str(VAULT.root),'model':'same-model',
                            'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                        {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'answer'}}]
                native.write_text(''.join(json.dumps(r)+chr(10) for r in rows))
            env = {'harness':'codex','session_id':'own','transcript_path':str(VAULT.root / 'own.jsonl'),
                   'cwd':str(VAULT.root),'session':'own'}
            with patch.object(rg.subprocess, 'run', side_effect=AssertionError('CLI asked')):
                # Before the first turn there is no transcript: in-session, asked again once it exists.
                assert rg.probe(VAULT, env) == {'mode':'foreground','reason':rg.NOT_CONFIGURED}
                route = rg.route(VAULT, env)
                assert route['mode'] == 'foreground' and not route['probe_due'], route
                transcript('own', 'vscode', 'Codex Desktop')
                assert rg.route(VAULT, env)['probe_due']
                assert rg.probe(VAULT, env) == {'mode':'off','reason':rg.DESKTOP_OFF}
                route = rg.route(VAULT, env)
                assert route['mode'] == 'off' and route['changed'] and not route['probe_due'], route
                start = hook.capture_block(VAULT, env, 'own', startup=True)
                assert '대화 검토 꺼짐' in start and '검토 경고' not in start, start
                for n in range(1, 16):
                    text = hook.capture_block(VAULT, env, 'own') or ''
                    assert '케이던스' not in text and '검토 경고' not in text, (n, text)
                    assert not rg.launch(VAULT, env, 'own')
                assert conversation_capture.status(VAULT, 'codex','own')['response_growth_route']['mode'] == 'off'
                # A CLI conversation of the same host keeps its in-session review.
                transcript('cli', 'cli', 'codex_cli_rs')
                cli = {**env, 'session_id':'cli', 'transcript_path':str(VAULT.root / 'cli.jsonl')}
                assert rg.probe(VAULT, cli) == {'mode':'foreground','reason':rg.NOT_CONFIGURED}
                assert rg.route(VAULT, cli)['mode'] == 'foreground'
        ''')
