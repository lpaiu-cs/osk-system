"""성장 실행의 수트의 한 갈래: 작업자를 띄우기 — 명령의 점검, 시한, 정리.

`tests/test_growth.py`의 `GrowthTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import os
import unittest



class WorkerCases:
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

    def test_worker_hooks_do_not_feed_maintenance_into_new_conversations(self):
        self.check_case("""
            node('A')
            change = "import os,subprocess; assert os.environ.get('OSK_GROWTH_WORKER')=='1'; hooks=Path(growth.__file__).resolve().parents[1]/'scripts/hooks'; results=[subprocess.run([sys.executable,'-B',str(hooks/name)],input=b'{}',capture_output=True,timeout=20) for name in ('claude_session_start.py','claude_prompt_submit.py','capture_stop.py')]; assert all(r.returncode==0 and not r.stdout and not r.stderr for r in results),results"
            result = growth.run(VAULT, [sys.executable,'-c',packet_worker('from pathlib import Path; '+change)],limit=2)
            assert result['ok'], result
            assert not growth.run(VAULT, ['unused-command'])['selected']
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
