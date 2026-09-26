"""services registers this vault's daily run and sync daemon with the OS service manager.

Each case runs in its own process because `core.ROOT` is fixed at import. The home folder is
temporary and the service manager's commands (`launchctl`, `systemctl`, PowerShell's task
cmdlets) are recorded fakes, so no case touches this device's scheduler. The one exception is
`test_real_task_scheduler`, which registers and removes a real Windows task and runs only when
`OSK_TEST_REAL_TASKS=1` (CI's Windows jobs).
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]

PRELUDE = r'''
import json, os, plistlib, subprocess, sys
from pathlib import Path
from osk import core, services
root = core.ROOT
engine = root / '_governance' / '_engine'
growth_py, daemon_py = engine / 'scripts' / 'growth_run.py', engine / 'sync_daemon.py'
calls = []
def run(argv):
    calls.append(argv)
    return subprocess.CompletedProcess(argv, 0, b'', b'')
command_file = root / '.osk' / 'growth-command.json'
growth = services.job('growth', command_file=command_file, at='07:30')
sync = services.job('sync')
home = Path.home()
'''


def _run(script: str, *, vault: str = 'vault') -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home = td / 'home'
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != 'XDG_CONFIG_HOME'}
        env.update(OSK_VAULT_ROOT=str(td / vault), PYTHONPATH=str(ENGINE), PYTHONUTF8='1',
                   OSK_UPDATE_CHECK='0', HOME=str(home), USERPROFILE=str(home))
        (td / vault).mkdir()
        return subprocess.run([sys.executable, '-c', PRELUDE + script], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=300)


class ServicesTests(unittest.TestCase):
    def check_case(self, body: str, **kw) -> None:
        result = _run(body, **kw)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_launchd_agents_are_this_vaults_and_replace_the_example(self):
        self.check_case(r'''
m = services.Launchd(run)
agents = home / 'Library' / 'LaunchAgents'
# The example from `scripts/launchd/`, filled in for this vault: this vault's daemon, another label.
agents.mkdir(parents=True)
(agents / 'com.example.ltm-vault-daemon.plist').write_bytes(plistlib.dumps(
    {'Label': 'com.example.ltm-vault-daemon', 'ProgramArguments': ['/usr/bin/python3', str(daemon_py)]}))
# Another vault's agent and an unrelated agent stay untouched.
(agents / 'com.other.plist').write_bytes(plistlib.dumps(
    {'Label': 'com.other', 'ProgramArguments': ['/x/other-vault/_governance/_engine/sync_daemon.py']}))
p = services.plan(m, 'sync', sync, False)
assert (p['action'], p['remove']) == ('replace', ['com.example.ltm-vault-daemon']), p
result = services.apply(m, 'sync', sync, p, False, '20260927-090000')
assert result['ok'] and result['done'] == ['com.example.ltm-vault-daemon', m.ident('sync')], result
assert not (agents / 'com.example.ltm-vault-daemon.plist').exists()
assert Path(result['backups'][0]).parent == home / '.osk-system' / 'backups', result
assert plistlib.loads(Path(result['backups'][0]).read_bytes())['Label'] == 'com.example.ltm-vault-daemon'
data = plistlib.loads((agents / f"{m.ident('sync')}.plist").read_bytes())
assert data['ProgramArguments'] == [sync['python'], str(daemon_py)], data
assert data['KeepAlive'] is True and data['EnvironmentVariables']['SYNC_ENABLED'] == '1', data
assert data['EnvironmentVariables']['OSK_VAULT_ROOT'] == str(root), data
assert ['launchctl', 'bootstrap'] == calls[-1][:2] and calls[-1][-1] == str(agents / f"{m.ident('sync')}.plist"), calls
assert services.plan(m, 'sync', sync, False)['action'] == 'keep'
assert (agents / 'com.other.plist').is_file()
# The daily run fires at the chosen minute.
p = services.plan(m, 'growth', growth, False)
assert p['action'] == 'add' and '07:30' in p['command'], p
services.apply(m, 'growth', growth, p, False, '20260927-090001')
data = plistlib.loads((agents / f"{m.ident('growth')}.plist").read_bytes())
assert data['StartCalendarInterval'] == {'Hour': 7, 'Minute': 30} and 'KeepAlive' not in data, data
assert data['ProgramArguments'][1:4] == [str(growth_py), '--command-file', str(command_file)], data
# A changed minute is a replacement; uninstalling removes both and backs them up.
later = services.job('growth', command_file=command_file, at='08:00')
assert services.plan(m, 'growth', later, False)['action'] == 'replace'
for kind in ('growth', 'sync'):
    p = services.plan(m, kind, None, True)
    assert p['action'] == 'remove', p
    assert services.apply(m, kind, None, p, True, '20260927-090002')['ok']
assert sorted(f.name for f in agents.iterdir()) == ['com.other.plist']
assert services.plan(m, 'growth', None, True)['action'] == 'absent'
''')

    def test_systemd_units_quote_paths_and_pair_the_timer(self):
        self.check_case(r'''
m = services.Systemd(run)
units = home / '.config' / 'systemd' / 'user'
units.mkdir(parents=True)
# The example service from `scripts/systemd/`, filled in for this vault.
(units / 'ltm-vault-daemon.service').write_text(
    f'[Service]\nExecStart=/usr/bin/python3 "{daemon_py}"\n', encoding='utf-8')
p = services.plan(m, 'sync', sync, False)
assert (p['action'], p['remove']) == ('replace', ['ltm-vault-daemon']), p
assert services.apply(m, 'sync', sync, p, False, '20260927-090000')['ok']
assert ['systemctl', '--user', 'disable', '--now', 'ltm-vault-daemon.service'] in calls, calls
assert calls[-1] == ['systemctl', '--user', 'enable', '--now', f"{m.ident('sync')}.service"], calls
text = (units / f"{m.ident('sync')}.service").read_text(encoding='utf-8')
assert 'Environment=SYNC_ENABLED=1' in text and 'Restart=always' in text, text
# The vault path has a space and a percent sign: ExecStart round-trips to the same files.
entry = next(e for e in m.entries() if e['id'] == m.ident('sync'))
assert services.owns('sync', entry['name'], entry['tokens']), entry
assert services.plan(m, 'sync', sync, False)['action'] == 'keep'
calls.clear()
p = services.plan(m, 'growth', growth, False)
services.apply(m, 'growth', growth, p, False, '20260927-090001')
timer = (units / f"{m.ident('growth')}.timer").read_text(encoding='utf-8')
assert 'OnCalendar=*-*-* 07:30:00' in timer and 'Persistent=true' in timer, timer
assert f"Unit={m.ident('growth')}.service" in timer, timer
assert calls[-1] == ['systemctl', '--user', 'enable', '--now', f"{m.ident('growth')}.timer"], calls
p = services.plan(m, 'growth', None, True)
result = services.apply(m, 'growth', None, p, True, '20260927-090002')
assert len(result['backups']) == 2 and not (units / f"{m.ident('growth')}.timer").exists(), result
assert any('OnCalendar' in Path(b).read_text(encoding='utf-8') for b in result['backups']), result
assert calls[-2][:4] == ['systemctl', '--user', 'disable', '--now'] and calls[-2][4].endswith('.timer'), calls
''', vault='va ult %1')

    def test_task_scheduler_keeps_its_own_task_and_retires_the_manual_ones(self):
        self.check_case(r'''
class Tasks(services.TaskScheduler):
    """The four PowerShell calls, answered as Get-ScheduledTask would after registration."""
    store = {}
    def _list(self):
        return list(self.store.values())
    def _register(self, spec):
        start = f"2026-09-27T{spec['at']}:00" if spec['at'] else ''
        self.store['\\' + spec['name']] = {
            'path': '\\', 'name': spec['name'],
            'actions': [{'execute': spec['execute'], 'arguments': spec['arguments'], 'workdir': spec['workdir']}],
            'triggers': [{'kind': 'MSFT_TaskDailyTrigger' if spec['at'] else 'MSFT_TaskLogonTrigger', 'start': start}],
            'battery': False, 'limit': f"PT{spec['minutes']}M" if spec['minutes'] else 'PT0S'}
    def _export(self, path, name):
        return '<Task/>'
    def _unregister(self, path, name):
        del self.store[path + name]
m = Tasks(run)
vault = str(root)
# What the manual guides produced on this user's device: a wrapper under .osk and a cmd.exe daemon.
m.store['\\osk-domain-growth'] = {'path': '\\', 'name': 'osk-domain-growth', 'battery': True, 'limit': 'PT15M',
    'actions': [{'execute': 'powershell.exe', 'workdir': '',
                 'arguments': f'-NoProfile -File "{vault}\\.osk\\run-growth-scheduled.ps1"'}],
    'triggers': [{'kind': 'MSFT_TaskDailyTrigger', 'start': '2026-09-14T09:00:00+09:00'}]}
m.store['\\osk-sync-daemon'] = {'path': '\\', 'name': 'osk-sync-daemon', 'battery': True, 'limit': 'PT72H',
    'actions': [{'execute': 'cmd.exe', 'workdir': '',
                 'arguments': f'/c set "SYNC_ENABLED=1" && "{vault}\\.venv\\Scripts\\pythonw.exe" "{daemon_py}" --interval 900'}],
    'triggers': [{'kind': 'MSFT_TaskLogonTrigger', 'start': ''}]}
# A task that only opens the vault in an editor is reported, not touched.
m.store['\\open-notes'] = {'path': '\\', 'name': 'open-notes', 'battery': True, 'limit': 'PT1H',
    'actions': [{'execute': 'notepad.exe', 'arguments': f'"{vault}\\README.md"', 'workdir': ''}], 'triggers': []}
p = services.plan(m, 'growth', growth, False)
assert (p['action'], p['remove']) == ('replace', ['\\osk-domain-growth']), p
assert any('open-notes' in n for n in p['notes']), p
result = services.apply(m, 'growth', growth, p, False, '20260927-090000')
assert result['ok'] and Path(result['backups'][0]).name == 'osk-domain-growth.osk-backup-20260927-090000.xml', result
assert Path(result['backups'][0]).read_text(encoding='utf-8') == '<Task/>'
task = m.store['\\' + m.ident('growth')]
assert task['actions'][0]['execute'] == growth['python'], task
assert subprocess.list2cmdline(growth['args']) == task['actions'][0]['arguments'], task
assert services.plan(m, 'growth', growth, False)['action'] == 'keep'
p = services.plan(m, 'sync', sync, False)
assert (p['action'], p['remove']) == ('replace', ['\\osk-sync-daemon']), p
services.apply(m, 'sync', sync, p, False, '20260927-090001')
action = m.store['\\' + m.ident('sync')]['actions'][0]
assert action['execute'] == 'cmd.exe', action
assert action['arguments'] == f'/c set SYNC_ENABLED=1&& start "" "{sync["python"]}" "{daemon_py}"', action
# `osk.update` finds the task that starts this daemon by the daemon path in its action.
assert str(daemon_py).lower().replace('/', '\\') in (action['execute'] + ' ' + action['arguments']).lower().replace('/', '\\')
assert services.plan(m, 'sync', sync, False)['action'] == 'keep'
# A registration that appears after the confirmation is not overwritten.
p = services.plan(m, 'growth', None, True)
m.store['\\osk-domain-growth'] = dict(m.store['\\' + m.ident('growth')], name='osk-domain-growth')
result = services.apply(m, 'growth', None, p, True, '20260927-090002')
assert not result['ok'] and '바뀌었다' in result['error'], result
assert '\\' + m.ident('growth') in m.store
''')

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('OSK_TEST_REAL_TASKS') == '1',
                         'registers a real Windows task; CI Windows jobs set OSK_TEST_REAL_TASKS=1')
    def test_real_task_scheduler(self):
        self.check_case(r'''
m = services.TaskScheduler()
p = services.plan(m, 'growth', growth, False)
assert p['action'] == 'add', p
try:
    assert services.apply(m, 'growth', growth, p, False, '20260927-090000')['ok']
    assert services.plan(m, 'growth', growth, False)['action'] == 'keep', m.entries()
finally:
    p = services.plan(m, 'growth', None, True)
    result = services.apply(m, 'growth', None, p, True, '20260927-090001')
assert result['ok'] and Path(result['backups'][0]).read_text(encoding='utf-8').lstrip().startswith('<'), result
assert services.plan(m, 'growth', None, True)['action'] == 'absent'
''')


if __name__ == '__main__':
    unittest.main()
