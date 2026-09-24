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
    def setUp(self):
        # Preflight reads the native home/system config; never the developer's own proxy or trust list.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environ = patch.dict(rg.os.environ, {'HOME': home.name, 'USERPROFILE': home.name, 'ProgramData': home.name,
                                             'CODEX_HOME': str(Path(home.name) / '.codex')})
        environ.start()
        self.addCleanup(environ.stop)

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
                argv = rg.command(source, executable, runtime)
                self.assertEqual(argv[0], executable)
                self.assertEqual(argv[argv.index('--model')+1], 'parent-model')
                self.assertIn('--ephemeral', argv)
                self.assertEqual(source['cli_version'], '0.155.0-alpha.9.2')
                for env in ({}, {**runtime,'CODEX_THREAD_ID':'another'},
                            {'CODEX_VERSION':runtime['CODEX_VERSION']},
                            {**runtime,'CODEX_VERSION':'0.156.0'}):
                    with self.subTest(env=env), self.assertRaisesRegex(ValueError, 'version differs'):
                        rg.command(source, executable, env)
                with self.assertRaisesRegex(ValueError, 'runtime version'):
                    rg.command(source, executable, {**runtime,'CODEX_VERSION':'invalid'})
            self.assertTrue(all(c[1:] in (['--version'], ['login','status']) or c[0]=='git'
                                for c in calls), 'preflight started inference')

    def test_desktop_update_resolves_only_matching_sibling_and_keeps_auth_gate(self):
        from osk import native_cli, growth
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
                self.assertEqual(growth.check_command([str(old)])['executable'], str(newer))
                self.assertEqual(growth.check_command([str(same)], follow_desktop_update=False)['executable'], str(same))
                old.parent.mkdir(); old.touch()
                self.assertEqual(native_cli.resolve(str(old)), str(newer))
                with self.assertRaisesRegex(ValueError, 'matching Codex'):
                    native_cli.resolve(str(old), version='0.154.0')
                source = {'harness':'codex','conversation_id':'own','cwd':folder,
                          'cli_version':'0.155.0-alpha.16','model':'same-model','model_provider':'openai',
                          'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription'):
                    rg.command(source, str(old), {})
                source['cli_version'] = '0.154.0'  # Creation version survives an app update.
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription'):
                    rg.command(source, str(old), {'CODEX_THREAD_ID':'own',
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
                self.assertEqual(rg.command(source, str(old), env)[0], str(same))
                with self.assertRaisesRegex(ValueError, 'matching Claude'):
                    rg.command({**source, 'version': '2.1.266'}, str(old), env)
            self.assertTrue(calls and all(c[0] == str(same) and c[1:] in (['--version'], ['auth', 'status'])
                                          for c in calls), 'preflight started inference')
            # The relocated file still faces the exact --version guard.
            with patch.object(rg.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '2.1.275', '')):
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(source, str(old), env)
            # Desktop pruned the configured release: a new conversation's routing checks an
            # installed one, but a version-less source still never reaches a fork.
            gone, fresh = str(base / '2.1.270/claude.exe'), {**source, 'version': None}
            self.assertEqual(native_cli.resolve(gone), str(same))
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                self.assertEqual(rg.preflight(fresh, gone, env, require_version=False), str(same))
                with self.assertRaisesRegex(ValueError, 'version differs'):
                    rg.command(fresh, gone, env)

    def test_runtime_version_recovery_preserves_counters_and_reaches_supervisor(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch, Mock
            import os, subprocess
            native = core.ROOT / 'native.jsonl'
            rows = [
                {'type':'session_meta','payload':{'id':'own','cwd':str(core.ROOT),
                    'cli_version':'0.155.0-alpha.9.2','model_provider':'openai'}},
                {'type':'turn_context','payload':{'model':'parent-model','effort':'high',
                    'cwd':str(core.ROOT),'approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}}]
            native.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'codex':sys.executable}))
            env = {'harness':'codex','session_id':'own','transcript_path':str(native),'cwd':str(core.ROOT)}
            with integration._locked('codex','own') as p:
                saved = integration._load(p,'codex','own')
                clock = {'counter':'finals','seen':['t1'],'count':9,'attempted_count':0,'history_baselined':True}
                saved.update(prompt_count=15, response_growth=clock)
                integration._save(p,saved)
            def inspect(argv, **kwargs):
                assert argv[0]=='git' or argv[1:] in (['--version'],['login','status']), argv
                output = ('true' if argv[0]=='git' else 'codex-cli 0.155.0-alpha.16'
                          if '--version' in argv else 'Logged in using ChatGPT')
                return subprocess.CompletedProcess(argv,0,output,'')
            with patch.object(rg.subprocess,'run',side_effect=inspect), patch.dict(os.environ,
                    {'CODEX_THREAD_ID':'other','CODEX_VERSION':'0.155.0-alpha.16'}):
                assert rg.route(env)['mode']=='foreground'
                with patch.dict(os.environ,{'CODEX_THREAD_ID':'own'}):
                    assert rg.route(env)['mode']=='background'
                    with patch.object(rg.subprocess,'Popen',return_value=Mock()) as launch:
                        assert rg.launch(env,'own')
                        inherited = launch.call_args.kwargs['env']
                        assert inherited['CODEX_THREAD_ID']=='own'
                        assert inherited['CODEX_VERSION']=='0.155.0-alpha.16'
                        assert inherited['OSK_GROWTH_WORKER']=='1'
            state = integration._load(integration.state_path('codex','own'),'codex','own')
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
            from osk import response_growth as rg, native_cli
            from unittest.mock import patch, Mock
            import os, subprocess
            for key in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS'):
                os.environ.pop(key, None)  # Codex hook processes carry none of these.
            native = core.ROOT / 'native.jsonl'
            rows = [
                {'type':'session_meta','payload':{'id':'own','cwd':str(core.ROOT),
                    'cli_version':'0.155.0-alpha.9.2','model_provider':'openai'}},
                {'type':'turn_context','payload':{'model':'parent-model','effort':'high',
                    'cwd':str(core.ROOT),'approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}}]
            native.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'codex':sys.executable}))
            env = {'session_id':'own','transcript_path':str(native),'cwd':str(core.ROOT)}
            managed = core.ROOT / 'OpenAI/Codex/bin' / ('a'*16) / 'codex.exe'
            managed.parent.mkdir(parents=True)
            managed.touch()
            def inspect(argv, **kwargs):
                assert argv[0]=='git' or argv[1:] in (['--version'],['login','status']), argv
                output = ('true' if argv[0]=='git' else 'codex-cli 0.155.0-alpha.16'
                          if '--version' in argv else 'Logged in using ChatGPT')
                return subprocess.CompletedProcess(argv,0,output,'')
            with patch.object(rg.subprocess,'run',side_effect=inspect):
                with patch.object(native_cli,'_ancestors',return_value=[]):
                    route = rg.route(env)  # No managed ancestor: strict transcript version.
                    assert route['mode']=='foreground' and 'version differs' in route['reason'], route
                chain = [('cmd.exe', 'cmd.exe'), ('codex.exe', str(managed))]
                with patch.object(native_cli,'_ancestors',return_value=chain):
                    assert rg.route(env)['mode']=='background'
                    with patch.object(rg.subprocess,'Popen',return_value=Mock()) as launch:
                        assert rg.launch(env,'own')
                inherited = launch.call_args.kwargs['env']
                assert inherited['CODEX_THREAD_ID']=='own' and inherited['CODEX_VERSION']=='0.155.0-alpha.16'
                # The supervisor has no ancestor; its command() re-runs full preflight on the binding.
                with patch.dict(os.environ, inherited), patch.object(native_cli,'_ancestors',return_value=[]):
                    argv = rg.command(rg.profile(str(native),'codex','own'), sys.executable, rg.subscription_env())
                assert argv[0]==sys.executable and argv[argv.index('--model')+1]=='parent-model', argv
            # Before a rollout exists the runtime binding, not the newest install, picks the binary.
            newer = core.ROOT / 'OpenAI/Codex/bin' / ('b'*16) / 'codex.exe'
            newer.parent.mkdir(parents=True)
            newer.touch()
            rg.CONFIG.write_text(json.dumps({'codex':str(newer)}))
            logins = []
            def installed(argv, **kwargs):
                if argv[1:] == ['login','status']:
                    logins.append(argv[0])
                version = 'codex-cli 0.156.0' if 'b'*16 in argv[0] else 'codex-cli 0.155.0-alpha.16'
                return subprocess.CompletedProcess(argv,0,'true' if argv[0]=='git' else version
                                                   if '--version' in argv else 'Logged in using ChatGPT','')
            with patch.object(rg.subprocess,'run',side_effect=installed), \\
                    patch.object(native_cli,'_ancestors',return_value=chain):
                assert rg.route({'harness':'codex','session_id':'fresh','cwd':str(core.ROOT)})['mode']=='background'
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
                            argv = rg.command(source, str(exe), env)
                            self.assertNotIn('--skip-git-repo-check', argv)
                        else:
                            with self.assertRaisesRegex(ValueError, error):
                                rg.command(source, str(exe), env)

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
                            argv = rg.command(source, str(exe), env)
                            pins = [argv[i+1] for i, v in enumerate(argv) if v == '-c' and 'base_url' in argv[i+1]]
                            self.assertEqual(pins, ['openai_base_url="https://chatgpt.com/backend-api/codex"'])
                        else:
                            for check in (lambda: rg.command(source, str(exe), env),
                                          lambda: rg.preflight(source, str(exe), env, require_version=False)):
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
                return list(rg._codex_layers(env, str(cwd)))
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
            self.assertEqual(rg._codex_unpinned(rg._codex_layers(env, str(cwd))),
                             {layer(repo) + ': profiles.p.model_provider': 'bridge'})
            if rg.os.name == 'nt':  # %ProgramData%\OpenAI\Codex\config.toml sets system defaults.
                system = top / 'OpenAI/Codex/config.toml'
                system.parent.mkdir(parents=True)
                system.write_text('chatgpt_base_url = "http://127.0.0.1:17841/"', encoding='utf-8')
                with patch.dict(rg.os.environ, {'ProgramData': str(top)}):
                    self.assertEqual(layers('')[0], str(system))
                    self.assertIn(str(system) + ': chatgpt_base_url', rg._codex_unpinned(rg._codex_layers(env, str(cwd))))

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
                self.assertEqual(rg.preflight(fresh, str(exe), {}, require_version=False), str(exe))
                for source, require in ((dict(fresh, finals=['t1']), False), (fresh, True)):
                    with self.subTest(require=require), self.assertRaisesRegex(ValueError, 'sandbox policy'):
                        rg.preflight(source, str(exe), {}, require_version=require)
                with self.assertRaisesRegex(ValueError, 'sandbox policy'):
                    rg.command(dict(fresh, model='parent-model'), str(exe), {})

    def test_codex_subagent_hook_changes_no_conversation_state(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            import os, subprocess
            hooks = Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'
            sys.path.insert(0, str(hooks))
            import claude_session_start as hook
            os.environ.pop('OSK_GROWTH_WORKER', None)
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'codex':sys.executable}))
            def rollout(name, meta):
                path = core.ROOT / name
                path.write_text(json.dumps({'type':'session_meta','payload':{'cwd':str(core.ROOT), **meta}})+chr(10)+
                    json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'done'}})+chr(10),
                    encoding='utf-8')
                return str(path)
            child = rollout('rollout-child.jsonl', {'id':'child','session_id':'own','parent_thread_id':'own'})
            env = {'session_id':'own','transcript_path':child,'cwd':str(core.ROOT)}
            for harness in (None, 'codex'):
                event = dict(env, harness=harness) if harness else env
                assert hook.capture_block(event,'own') is None
                assert hook.capture_block(event,'own',startup=True) is None
                for script in ('capture_stop.py', 'claude_session_start.py', 'claude_prompt_submit.py'):
                    done = subprocess.run([sys.executable, str(hooks / script)], input=json.dumps(event).encode(),
                                          capture_output=True, timeout=30)
                    assert done.returncode == 0 and not done.stdout, (script, done.stdout, done.stderr)
            for sid in ('own', 'child'):
                assert not integration.state_path('codex', sid).exists(), sid
            # A foreign transcript is still an identity error, not a silent subagent.
            foreign = rollout('rollout-foreign.jsonl', {'id':'child','session_id':'another'})
            route = rg.route(dict(env, transcript_path=foreign))
            assert route['mode']=='foreground' and 'identity' in route['reason'], route
        ''')

    def test_empty_legacy_baseline_excludes_ancestor_finals_once(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch
            import os
            parent = core.ROOT / 'rollout-parent-own.jsonl'
            child = core.ROOT / 'rollout-child-own_next.jsonl'
            def finish(identity):
                return [{'type':'event_msg','payload':{'type':'task_started','turn_id':identity}},
                        {'type':'event_msg','payload':{'type':'task_complete','turn_id':identity,'last_agent_message':'done'}}]
            def save(path, rows):
                path.write_text(''.join(json.dumps(r)+chr(10) for r in rows), encoding='utf-8')
            header = {'type':'session_meta','payload':{'id':'own'}}
            save(parent, [header] + [r for n in range(9) for r in finish('old-'+str(n))])
            header['payload']['history_base'] = {'thread_id':'own','end_byte_offset':parent.stat().st_size}
            save(child, [header])
            with integration._locked('codex','own') as path:
                state = integration._load(path,'codex','own')
                state['response_growth'] = {'counter':'finals','seen':[],'count':0,'attempted_count':0}
                integration._save(path,state)
            with patch.dict(os.environ, {'CODEX_HOME':str(core.ROOT)}):
                for n in range(1,10):
                    with child.open('a',encoding='utf-8') as f:
                        f.write(''.join(json.dumps(r)+chr(10) for r in finish('new-'+str(n))))
                    source = rg.profile(str(child),'codex','own')
                    clock = rg.observe(source)
                    assert clock['count'] == n and clock['due'] == (n == 9), clock
                    assert rg.observe(source) == clock
                # A modern baseline actually observed before these completions
                # must count them even if they now live in an ancestor page.
                integration.state_path('codex','own').unlink()
                original_parent = parent.read_bytes()
                save(parent, [{'type':'session_meta','payload':{'id':'own'}}])
                env = {'harness':'codex','session_id':'own','transcript_path':str(parent)}
                with patch.object(rg,'configured',return_value='unused'):
                    rg.initialize(env)
                parent.write_bytes(original_parent)
                source = rg.profile(str(child),'codex','own')
                assert rg.observe(source)['count'] == 18
                # SessionStart before a new page exists also has no history baseline.
                integration.state_path('codex','own').unlink()
                original_child = child.read_bytes()
                child.unlink()
                with patch.object(rg,'configured',return_value='unused'):
                    rg.initialize(dict(env,transcript_path=str(child)))
                child.write_bytes(original_child)
                assert rg.observe(rg.profile(str(child),'codex','own'))['count'] == 9
        ''')

    def test_paginated_final_counter_keeps_baseline_and_active_page_model(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch
            import os
            folder = core.ROOT / 'native'
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
            with patch.dict(os.environ, {'CODEX_HOME':str(core.ROOT)}):
                assert rg.observe(rg.profile(str(parent),'codex','own'))['count'] == 0
                save(child, rows('same-parent-model','second',{'thread_id':'own','end_byte_offset':parent.stat().st_size}))
                source = rg.profile(str(child),'codex','own')
                assert source['finals'] == ['first','second'], source
                assert source['model'] == source['cli_version'] == 'same-parent-model', source
                assert source['transcript_path'] == str(child.resolve()), source
                assert rg.observe(source)['count'] == 1
                # Upgrade from a baseline that saw only the last page: no retroactive Stops.
                with integration._locked('codex','own') as path:
                    state = integration._load(path,'codex','own')
                    state['response_growth'] = {'counter':'finals','seen':['second'],'count':3,'attempted_count':0}
                    integration._save(path,state)
                assert rg.observe(source)['count'] == 3
                with child.open('a',encoding='utf-8') as f:
                    f.write(''.join(json.dumps(r) + chr(10) for r in rows('same-parent-model','third')[1:]))
                source = rg.profile(str(child),'codex','own')
                assert rg.observe(source)['count'] == 4
                assert rg.observe(source)['count'] == 4
                parent.write_bytes(parent.read_bytes().replace(b'old-model',b'bad-model'))
                assert not rg._source_unchanged(source)
                try:
                    rg.observe(dict(source,finals=['first','third']))
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
            result = rg.profile(str(path), 'codex', 'own')
            self.assertEqual(result['usage'], total)
            self.assertEqual(result['finals'], ['t1'])
            self.assertEqual(result['model'], 'same-model')
            self.assertEqual((result['source'], result['originator']), ('vscode', 'Codex Desktop'))
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
                      'cwd': folder, 'version': '2.1.266', 'permission_mode': 'default'}
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
            with patch.object(rg.subprocess, 'run', side_effect=[completed('codex-cli 0.155.0'), completed('Logged in using ChatGPT'), completed('true')]):
                command = rg.command(source, str(exe), env)
            self.assertIn('--ephemeral', command)
            self.assertEqual(command[-2:], ['own', '-'])
            self.assertIn('model_reasoning_effort="max"', command)
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
            with patch.object(rg.subprocess, 'run', side_effect=[completed('codex-cli 0.155.0'), completed('Logged in using an API key')]):
                with self.assertRaisesRegex(ValueError, 'ChatGPT subscription login'):
                    rg.preflight(source, str(exe), env)
            with patch.dict(rg.os.environ, {'OPENAI_API_KEY': 'fixture', 'ANTHROPIC_API_KEY': 'fixture',
                                            'OPENAI_BASE_URL': 'http://127.0.0.1:17841/v1'}):
                self.assertNotIn('OPENAI_API_KEY', rg.subscription_env())
                self.assertNotIn('OPENAI_BASE_URL', rg.subscription_env())
                self.assertNotIn('ANTHROPIC_API_KEY', rg.subscription_env())

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
                return rg.profile(str(path), 'claude', 'own')
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
                        argv = rg.command({**source, 'permission_mode': mode}, str(exe), env)
                        self.assertEqual(argv.count('--permission-mode'), 1)
                        self.assertEqual(argv[argv.index('--permission-mode') + 1], mode)
                        # --permission-mode bypassPermissions alone enables bypass; nothing wider is added.
                        self.assertFalse({'--dangerously-skip-permissions',
                                          '--allow-dangerously-skip-permissions'} & set(argv))
                for mode in (None, '', 'bogus', 'BypassPermissions', ['auto']):
                    with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'permission mode'):
                        rg.command({**source, 'permission_mode': mode}, str(exe), env)
                for gate in (lambda s: rg.command(s, str(exe), env),
                             lambda s: rg.preflight(s, str(exe), env, require_version=False)):
                    with self.assertRaisesRegex(ValueError, 'permission mode'):
                        gate(missing)
                # Before the first final there is no mode to copy yet; routing still checks the CLI.
                self.assertEqual(rg.preflight({**missing, 'finals': []}, str(exe), env, require_version=False), str(exe))
            self.assertTrue(all(c.args[0][1:] in (['--version'], ['auth', 'status']) for c in call.call_args_list))
            # A mode change between the checked source and the fork never forks. The file is the
            # checked source again, so the mode is the only difference (the control below forks).
            write(rows)
            job = {'harness': 'claude', 'conversation_id': 'own'}
            with patch.object(rg, 'preflight', return_value=str(exe)), \
                    patch.object(rg.growth, 'run', return_value={'ok': True, 'state': 'complete'}) as fork:
                result = rg.run({**source, 'permission_mode': 'default'}, job, str(exe))
                self.assertEqual(result['state'], 'unavailable')
                self.assertIn('source advanced', result['error'])
                self.assertFalse(fork.called)
                self.assertEqual(rg.run(source, job, str(exe))['state'], 'complete')
                self.assertEqual(fork.call_count, 1)

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
                    env = rg.subscription_env()
                    self.assertFalse({'ANTHROPIC_BASE_URL', *host} & set(env))
                    envs.clear()
                    with patch.object(rg.subprocess, 'run', side_effect=inspect):
                        rg.command(source, str(exe), env)
                    self.assertTrue(all(e is env for e in envs))  # auth status sees the child's env
            for url in ('http://127.0.0.1:8080', 'https://api.anthropic.com.proxy', 'https://api.anthropic.com/v2'):
                with self.subTest(url=url), patch.dict(rg.os.environ, {'ANTHROPIC_BASE_URL': url,
                                                                       'CLAUDE_CONFIG_DIR': folder}):
                    env = rg.subscription_env()
                    self.assertEqual(env['ANTHROPIC_BASE_URL'], url)
                    with patch.object(rg.subprocess, 'run', side_effect=inspect) as call:
                        with self.assertRaisesRegex(ValueError, 'unverified provider'):
                            rg.command(source, str(exe), env)
                        self.assertEqual(call.call_count, 1)

    def test_codex_permission_overrides_roundtrip_through_toml(self):
        import tomllib
        source = {'harness': 'codex', 'conversation_id': 'own', 'model': 'same-model',
                  'effort': 'high', 'approval_policy': {'granular': {
                      'sandbox_approval': False, 'rules': True, 'mcp_elicitations': False,
                      'request_permissions': True, 'skill_approval': False}},
                  'approvals_reviewer': 'auto_review'}
        roots = [str(Path(tempfile.gettempdir()) / '한글 workspace')]
        for slash in (False, True):
            for env_tmp in (False, True):
                source['sandbox_policy'] = {'type': 'workspace-write', 'writable_roots': roots,
                    'network_access': False, 'exclude_slash_tmp': slash, 'exclude_tmpdir_env_var': env_tmp}
                with self.subTest(slash=slash, env_tmp=env_tmp), patch.object(rg, 'preflight'):
                    argv = rg.command(source, 'unused', {})
                    config = tomllib.loads('\n'.join(argv[i+1] for i, v in enumerate(argv) if v == '-c'))
                    self.assertEqual(config['approval_policy'], source['approval_policy'])
                    self.assertEqual(config['approvals_reviewer'], source['approvals_reviewer'])
                    self.assertEqual(config['sandbox_workspace_write'],
                                     {k: v for k, v in source['sandbox_policy'].items() if k != 'type'})
                    self.assertNotIn('--skip-git-repo-check', argv)
        for policy in ({'type': 'workspace-write', 'future_restriction': True},
                       {'type': 'read-only', 'network_access': True},
                       {'type': 'external-sandbox'},
                       {'type': 'workspace-write', 'writable_roots': ['relative']},
                       {'type': 'workspace-write', 'exclude_slash_tmp': 'true'}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                rg._codex_overrides({**source, 'sandbox_policy': policy})

    def test_desktop_fork_preserves_app_tool_prefix_without_changing_cli_sources(self):
        source = {'harness': 'codex', 'conversation_id': 'own', 'model': 'parent-model',
                  'effort': 'high', 'approval_policy': 'never', 'sandbox_policy': {'type': 'read-only'}}
        flags = ('features.code_mode_host=true',
                 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true')
        for kind, originator in (('vscode', 'Codex Desktop'), ('exec', 'Codex Desktop'), ('cli', 'codex_cli_rs')):
            with self.subTest(source=kind), patch.object(rg, 'preflight'):
                argv = rg.command({**source, 'source': kind, 'originator': originator}, 'unused', {})
                self.assertEqual(argv[argv.index('--model')+1], source['model'])
                for flag in flags:
                    self.assertEqual(flag in argv, kind == 'vscode')

    def test_codex_ineligible_source_routes_to_foreground_without_consuming_review(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory
            from unittest.mock import patch
            import subprocess
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace('own', '', space='00_Scope/W1')
            native = core.ROOT / 'native.jsonl'
            rows = [{'type':'session_meta','payload':{'id':'own','cwd':str(core.ROOT),
                        'cli_version':'0.155.0','model_provider':'openai'}},
                    {'type':'turn_context','payload':{'turn_id':'t1','cwd':str(core.ROOT),'model':'same-model',
                        'effort':'high','approval_policy':'never','sandbox_policy':{'type':'read-only'}}},
                    {'type':'event_msg','payload':{'type':'user_message','message':'question'}},
                    {'type':'response_item','payload':{'type':'message','role':'user',
                        'content':[{'type':'input_text','text':'question'}]}},
                    {'type':'response_item','payload':{'type':'message','role':'assistant',
                        'content':[{'type':'output_text','text':'answer'}]}},
                    {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'answer'}}]
            native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'codex':sys.executable}))
            env = {'harness':'codex','session_id':'own','transcript_path':str(native),'cwd':str(core.ROOT),'session':'own'}
            real_run = subprocess.run
            def inspect(argv, **kwargs):
                if argv[0] == 'git':
                    return real_run(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0,
                    'codex-cli 0.155.0' if '--version' in argv else 'Logged in using ChatGPT', '')
            # Exercise the real Git boundary without spawning a model.
            with patch.object(rg.subprocess,'run',side_effect=inspect):
                start = hook.capture_block(env,'own',startup=True)
                assert 'Git worktree' in start and '검토 경고' in start, start
                pending = integration.status('codex','own')['pending_refs']
                for n in range(1,16):
                    text = hook.capture_block(env,'own')
                    assert ('[osk 케이던스' in text) == (n in {9,15}), (n,text)
                    assert not rg.launch(env,'own')
                state = integration.status('codex','own')
                assert state['pending_refs'] == pending and state['reviewed_rounds'] == 0
                assert state['response_growth']['attempted_count'] == 0
                real_run(['git','init','-q',str(core.ROOT)],check=True,capture_output=True)
                assert rg.route(env)['mode'] == 'background'
                # A future restriction must refuse even when Git/auth/version pass.
                rows[1]['payload']['sandbox_policy']['future_restriction'] = True
                native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
                route = rg.route(env)
                assert route['mode'] == 'foreground' and 'cannot be represented' in route['reason'], route
                # The ChatGPT Web bridge route is refused by name before any native query.
                del rows[1]['payload']['sandbox_policy']['future_restriction']
                rows[1]['payload']['model'] = 'chatgpt-web/pro'
                native.write_text(''.join(json.dumps(r)+'\\n' for r in rows))
                with patch.object(rg.subprocess,'run',side_effect=AssertionError('native query')):
                    route = rg.route(env)
                    assert route['mode'] == 'foreground' and 'ChatGPT Web bridge' in route['reason'], route
                    try:
                        rg.command(rg.profile(str(native),'codex','own'), sys.executable, {})
                        raise AssertionError('web route accepted')
                    except ValueError as exc:
                        assert 'ChatGPT Web bridge' in str(exc), exc
                source = rg.profile(str(native),'codex','own')
                with patch.object(growth,'run',side_effect=AssertionError('provider started')):
                    result = rg.run(source, {'harness':'codex','conversation_id':'own'}, sys.executable)
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

    def test_shadow_prompt_clock_does_not_trigger_a_fork_and_ninth_stop_does(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace('own', '', space='00_Scope/W1')
            native = core.ROOT / 'native.jsonl'
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'session':'own',
                   'permission_mode':'bypassPermissions'}
            rg.initialize(env)
            def append(n):
                rows = [{'type':'user','sessionId':'own','uuid':f'u{n}','permissionMode':'bypassPermissions',
                         'message':{'role':'user','content':f'question {n}'}},
                        {'type':'assistant','sessionId':'own','uuid':f'a{n}', 'message':{'role':'assistant','id':f'm{n}','model':'parent-model', 'stop_reason':'end_turn','content':[{'type':'text','text':f'answer {n}'}]}}]
                with native.open('a', encoding='utf-8') as f:
                    f.write(''.join(json.dumps(r)+'\\n' for r in rows))
            with patch.object(rg, 'run', return_value={'ok':False,'state':'incomplete'}) as worker, \
                    patch.object(scope_memory,'recovery_block',return_value='RECOVERY_SENTINEL'), \
                    patch.object(rg,'preflight'):
                for n in range(1, 10):
                    append(n)
                    hook.capture_block(env, 'own')
                    before = integration.status('claude','own')
                    assert before['prompt_count'] == n, before
                    assert before['response_growth']['count'] == n-1, before
                    if n == 9:  # A switch after the prompt writes no row; Stop's mode in force refuses.
                        switched = rg.process_stop(dict(env, permission_mode='default'))
                        assert switched['state'] == 'unavailable' and not worker.called, switched
                    result = rg.process_stop(env)
                    assert worker.call_count == (1 if n == 9 else 0), result
                    rg.process_stop(env)  # duplicate notification must not count or launch
                assert worker.call_count == 1
                with patch.object(rg.subprocess, 'Popen') as popen:
                    assert rg.launch(env, 'own')
                payload = json.loads(popen.return_value.stdin.write.call_args.args[0])
                assert payload['permission_mode'] == 'bypassPermissions', payload
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

    def test_unavailable_cli_routes_to_9_15_input_review_and_recovers_without_reset(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, scope_memory
            from unittest.mock import patch
            sys.path.insert(0, str(Path(rg.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            scope_memory.replace('own', '', space='00_Scope/W1')
            native = core.ROOT/'native.jsonl'
            native.write_text(json.dumps({'type':'user','sessionId':'own','uuid':'u1','message':{'role':'user','content':'question'}})+'\\n'+
                json.dumps({'type':'assistant','sessionId':'own','uuid':'a1','message':{'role':'assistant','id':'m1','model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}})+'\\n')
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'claude':sys.executable}))
            env = {'harness':'claude','session_id':'own','transcript_path':str(native),'cwd':str(core.ROOT),'session':'own'}
            with patch.object(rg,'preflight',side_effect=ValueError('subscription login required')), \
                    patch.object(rg.subprocess,'Popen',side_effect=AssertionError('unavailable fork launched')):
                start = hook.capture_block(env,'own',startup=True)
                assert '검토 경고' in start and 'subscription login required' in start, start
                assert integration.status('claude','own')['prompt_count'] == 0
                original = integration.status('claude','own')['pending_refs']
                for n in range(1,16):
                    text = hook.capture_block(env,'own')
                    assert ('[osk 케이던스' in text) == (n in {9,15}), (n,text)
                    if n in {9,15}:
                        assert '검토 경고' in text and original[0] in text, text
                    assert not rg.launch(env,'own')
                before = integration.status('claude','own')
                assert before['prompt_count'] == 15 and before['reviewed_rounds'] == 0, before
            with patch.object(rg,'preflight'):
                hook.capture_block(env,'own',startup=True)
                restored = integration.status('claude','own')
                assert restored['response_growth_route']['mode'] == 'background'
                assert restored['prompt_count'] == 15 and restored['pending_refs'] == original
                assert restored['response_growth']['count'] == before['response_growth']['count']
                hook.capture_block(env,'own')
            # Losing auth between cadence boundaries must surface the overdue review immediately.
            with patch.object(rg,'preflight',side_effect=ValueError('login expired')):
                text = hook.capture_block(env,'own')
                assert 'user 턴 17' in text and original[0] in text and '단독 턴' in text, text
                assert integration.status('claude','own')['reviewed_rounds'] == 0
            rg.CONFIG.write_text('{broken')
            text = hook.capture_block(env,'own',startup=True)
            assert '검토 경고' in text and 'UserPromptSubmit' in text
            assert not rg.launch(env,'own'), 'bad configuration prevented capture fallback'
        ''')

    def test_login_loss_after_input_does_not_consume_stop_attempt(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch
            source = {'harness':'claude','conversation_id':'own','model':'same-model','finals':[]}
            rg.observe(source)
            source['finals'] = [str(i) for i in range(9)]
            job = {'harness':'claude','conversation_id':'own','pending_refs':['fixture']}
            with patch.object(rg,'preflight',side_effect=ValueError('login expired')), \
                    patch.object(growth,'run',side_effect=AssertionError('provider started')):
                result = rg.attempt(source,job,'unused')
            assert result['state'] == 'unavailable', result
            status = integration.status('claude','own')
            assert status['response_growth']['attempted_count'] == 0
            assert status['reviewed_rounds'] == 0 and rg.observe(source)['due']
        ''')

    def test_source_advanced_before_fork_does_not_consume_stop_attempt(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration
            from unittest.mock import patch
            native = core.ROOT / 'native.jsonl'
            rows = [{'type':'assistant','sessionId':'own','uuid':f'a{n}','cwd':str(core.ROOT),'message':{'role':'assistant','id':f'm{n}',
                     'model':'same-model','stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}}
                    for n in range(9)]
            native.write_text(''.join(json.dumps(r)+'\\n' for r in rows), encoding='utf-8')
            source = rg.profile(str(native),'claude','own')
            rg.observe(dict(source, finals=[]))
            job = {'harness':'claude','conversation_id':'own','pending_refs':['fixture']}
            def append(row):
                with native.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(row)+'\\n')
            # Claude appends trailers such as stop_hook_summary seconds after Stop, during preflight.
            trailer = lambda *a, **k: append({'type':'system','subtype':'stop_hook_summary','sessionId':'own'})
            with patch.object(rg,'preflight',side_effect=trailer), \\
                    patch.object(growth,'run',return_value={'ok':False,'state':'incomplete'}) as fork:
                assert rg.attempt(source,job,'unused')['state'] == 'incomplete'
            assert fork.call_count == 1 and integration.status('claude','own')['response_growth']['attempted_count'] == 9
            # A new user turn before launch never forks and never spends the attempt.
            with native.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(dict(rows[0], uuid=f'a{n}', message=dict(rows[0]['message'], id=f'm{n}')))+'\\n'
                                for n in range(9, 18)))
            source = rg.profile(str(native),'claude','own')
            new_turn = lambda *a, **k: append({'type':'user','sessionId':'own','message':{'role':'user','content':'next'}})
            with patch.object(rg,'preflight',side_effect=new_turn), \\
                    patch.object(growth,'run',side_effect=AssertionError('fork started')):
                result = rg.attempt(source,job,'unused')
            assert result['state'] == 'unavailable' and 'source advanced' in result['error'], result
            status = integration.status('claude','own')
            assert status['response_growth']['attempted_count'] == 9, status
            assert status['reviewed_rounds'] == 0 and rg.observe(source)['due']
        ''')

    def test_stop_detaches_and_waits_for_native_final_flush(self):
        base_tests.GrowthTests().check_case('''
            import os, subprocess, time
            from osk import response_growth as rg, integration, scope_memory
            scope_memory.replace(core.ROOT.name, '', space='00_Scope/W1')
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
            node('Other Scope claim', scope='W2')
            scope_memory.replace('own', '', space='00_Scope/W1')
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
            argv = json.loads((core.ROOT / result['output'] / 'argv.json').read_text(encoding='utf-8'))
            assert argv[1:] == ['-c', script], argv
            assert result['domain_selected'] == 0 and result['scope_selected'] == 1, result
            manifest = [r for r in core.ledger_read(growth.LEDGER) if r['kind']=='plan'][-1]
            assert [j['scope'] for j in manifest['organization_jobs']] == ['W1'], manifest
            assert result['selected'] == 2 and manifest['timeout_seconds'] == 600
            assert integration.status('claude','own')['reviewed_rounds'] == 1
            assert growth.plan()['candidates'], 'unrelated Domain work was incorrectly acknowledged'
        ''')

    def test_fork_doctor_shows_route_verdict_without_writing_state_or_secrets(self):
        base_tests.GrowthTests().check_case('''
            from osk import response_growth as rg, integration, cli
            from types import SimpleNamespace
            from unittest.mock import patch
            import io, os, subprocess
            subprocess.run(['git','init','-q',str(core.ROOT)], check=True, capture_output=True)
            home = core.ROOT / 'claude-home'
            native = home / 'projects' / 'p' / 'own.jsonl'
            native.parent.mkdir(parents=True)
            native.write_text(json.dumps({'type':'user','sessionId':'own','cwd':str(core.ROOT),'version':'2.1.280',
                'permissionMode':'auto','message':{'role':'user','content':'q'}}) + chr(10) +
                json.dumps({'type':'assistant','sessionId':'own','message':{'id':'m1','model':'claude-same',
                'stop_reason':'end_turn','content':[{'type':'text','text':'answer'}]}}) + chr(10), encoding='utf-8')
            rg.CONFIG.parent.mkdir(exist_ok=True)
            rg.CONFIG.write_text(json.dumps({'claude': sys.executable}))
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
                roots = {core.ROOT, integration.state_path('claude', 'own').parent, Path(os.environ['CODEX_HOME'])}
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
                assert codex['endpoint']['pinned_for_forks'] == rg.OPENAI_ENDPOINT, codex
                assert codex['endpoint']['refused'] is None, codex
                for secret in ('s3cret', 'user:', 'person@example.com'):
                    assert secret not in text, secret
                os.environ['ANTHROPIC_BASE_URL'] = 'https://api.anthropic.com'
                ready = json.loads(doctor('--harness', 'claude', '--json'))[0]
                assert ready['verdict'] == {'mode': 'background', 'reason': None}, ready
                assert ready['endpoint']['fork'] == 'first-party default; dropped for forks', ready
            assert all(c[0] == 'git' or c[1:] in (['--version'], ['auth', 'status']) for c in calls), calls
            assert snapshot() == before, 'doctor wrote state'
            # The doctor's verdict is route()'s: route() itself writes the state the doctor skipped.
            with patch.object(rg.subprocess, 'run', side_effect=inspect):
                route = rg.route({'harness': 'claude', 'session_id': 'own', 'transcript_path': str(native)})
            assert {k: route[k] for k in ('mode', 'reason')} == ready['verdict'], route
            assert snapshot() != before
        ''')


if __name__ == '__main__':
    unittest.main()
