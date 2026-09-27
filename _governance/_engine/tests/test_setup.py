"""setup connects this vault to the hosts on this device: only osk entries, backed up, idempotent.

Each case runs in its own process because `core.ROOT` is fixed at import. Every host
home (`HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`) is a temporary folder, and
fake `claude`/`codex` CLIs come first on `PATH`: they log their arguments and write the
MCP entry the real CLI would write, so no case touches the developer's configuration.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]

FAKE_CLI = r'''
import json, os, re, sys
from pathlib import Path
name, args = sys.argv[1], sys.argv[2:]
with open(os.environ['OSK_TEST_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps([name, *args]) + '\n')
if name == 'claude':
    path = Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    servers = data.setdefault('mcpServers', {})
    if args[:2] == ['mcp', 'add']:
        cut = args.index('--')
        servers[args[cut - 1]] = {'type': 'stdio', 'command': args[cut + 1], 'args': args[cut + 2:]}
    elif args[:2] == ['mcp', 'remove']:
        servers.pop(args[-1], None)
    path.write_text(json.dumps(data), encoding='utf-8')
elif name == 'codex':
    path = Path(os.environ['CODEX_HOME']) / 'config.toml'
    text = path.read_text(encoding='utf-8') if path.is_file() else ''
    if args[:2] in (['mcp', 'add'], ['mcp', 'remove']):
        key = args[args.index('--') - 1] if '--' in args else args[-1]
        text = re.sub(r'\[mcp_servers\.' + re.escape(key) + r'\]\n(?:[^\[\n].*\n)*', '', text)
        if args[1] == 'add':
            cut = args.index('--')
            text += (f'[mcp_servers.{key}]\ncommand = {json.dumps(args[cut + 1])}\n'
                     f'args = {json.dumps(args[cut + 2:])}\n')
    path.write_text(text, encoding='utf-8')
'''

SETUP = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from unittest import mock
from osk import core, harness, services, update, validate
from osk import setup as S
from osk.harness import base
root = core.ROOT
validate.make_mini_vault(root)
subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True, capture_output=True)
home = Path.home()
claude_home = Path(os.environ['CLAUDE_CONFIG_DIR'])
codex_home = Path(os.environ['CODEX_HOME'])
for folder in (claude_home, codex_home):
    folder.mkdir(parents=True, exist_ok=True)
bin_dir = Path(os.environ['OSK_TEST_BIN'])
log = Path(os.environ['OSK_TEST_LOG'])
py = sys.executable
engine = root / '_governance' / '_engine'
hooks_dir = engine / 'scripts' / 'hooks'
server = engine / 'mcp_server.py'
vpy = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
def fake_clis(names=('claude', 'codex')):
    for name in names:
        if os.name == 'nt':
            # What npm installs: a `.cmd`, and a `.ps1` that PowerShell prefers, handing `$args` on
            # the way npm's cmd-shim does.
            (bin_dir / f'{name}.cmd').write_text(f'@"{py}" "{bin_dir / "fake_cli.py"}" {name} %*\r\n',
                                                 encoding='utf-8')
            (bin_dir / f'{name}.ps1').write_text(f'& "{py}" "{bin_dir / "fake_cli.py"}" {name} $args\n',
                                                 encoding='utf-8')
        else:
            path = bin_dir / name
            path.write_text(f'#!/bin/sh\nexec "{py}" "{bin_dir / "fake_cli.py"}" {name} "$@"\n',
                            encoding='utf-8')
            path.chmod(0o755)
def calls():
    return [json.loads(l) for l in log.read_text(encoding='utf-8').splitlines()] if log.is_file() else []
def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
def osk_entries(data, event):
    script = hooks_dir / base.SCRIPTS[event]
    return [h for g in data.get('hooks', {}).get(harness.get('claude').events[event], [])
            for h in g.get('hooks', []) if base.mentions(base.command_tokens(h), script)]
def baseline(version='v4.0.0'):
    core.ledger_append(update.UPDATE_JOURNAL, {'kind': 'done', 'version': version,
                                               'attest': 'sha256:' + '0' * 64})
'''


# The OS service manager, in memory: the same answers `osk.services` gets from a real one.
FAKE_SERVICES = r'''
class Fake(services.Backend):
    name = 'fake'
    rows = {}
    def ident(self, kind):
        return f'osk-{kind}-{services.tag()}'
    def entries(self):
        return [dict(r) for r in self.rows.values()]
    def definition(self, kind, job):
        return {'argv': [job['python'], *job['args']], 'at': job['at']}
    def install(self, kind, job, stamp):
        i = self.ident(kind)
        self.rows[i] = {'id': i, 'name': i, 'tokens': [job['python'], *job['args']],
                        'definition': self.definition(kind, job)}
        return {'id': i, 'installed': True}
    def remove(self, entry, stamp):
        del self.rows[entry['id']]
        return {'id': entry['id'], 'removed': True, 'backup': str(home / 'backup')}
mock.patch.object(S.services, 'backend', lambda run=None: Fake()).start()
'''


def _case_path(bin_dir: Path) -> str:
    """The case's PATH: fake CLIs first, and without the developer's Kiro launcher — a `kiro`
    on PATH alone makes Kiro a host, so leaving it would make results depend on the device."""
    kept = [p for p in os.environ.get('PATH', '').split(os.pathsep) if p and not shutil.which('kiro', path=p)]
    return os.pathsep.join([str(bin_dir), *kept])


def _run(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home, bin_dir = td / 'home', td / 'bin'
        home.mkdir()
        bin_dir.mkdir()
        (bin_dir / 'fake_cli.py').write_text(FAKE_CLI, encoding='utf-8')
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS', 'OSK_GROWTH_WORKER',
                            'KIRO_SESSION_ID', 'ANTIGRAVITY_CONVERSATION_ID')}
        env.update(OSK_VAULT_ROOT=str(td / 'vault'), PYTHONPATH=str(ENGINE), PYTHONUTF8='1',
                   OSK_UPDATE_CHECK='0', HOME=str(home), USERPROFILE=str(home),
                   APPDATA=str(home / 'AppData' / 'Roaming'), LOCALAPPDATA=str(home / 'AppData' / 'Local'),
                   CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'),
                   OSK_TEST_BIN=str(bin_dir), OSK_TEST_LOG=str(td / 'cli.log'),
                   PATH=_case_path(bin_dir))
        return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=300)


class SetupTests(unittest.TestCase):
    def check_case(self, body: str) -> None:
        result = _run(SETUP + body)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_confirmed_apply_touches_only_this_vaults_entries_and_is_idempotent(self):
        self.check_case(r'''
fake_clis()
baseline()
settings = claude_home / 'settings.json'
others = {'theme': 'dark', 'hooks': {
    'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo mine'}]}],
    'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo other-tool'}]}],
    'Stop': [{'hooks': [{'type': 'command',
                         'command': f'"{py}" /elsewhere/_governance/_engine/scripts/hooks/capture_stop.py'}]}]}}
settings.write_text(json.dumps(others), encoding='utf-8')
original = settings.read_bytes()
# The plan changes nothing and names every step.
rep, code = S.run()
assert code == 0 and rep['changes'] and rep['baseline']['state'] == 'recorded', rep
c, x = rep['hosts']
assert (c['harness'], c['mcp']['action'], x['mcp']['action']) == ('claude', 'add', 'add'), rep
assert set(c['hooks']['events'].values()) == {'add'} and 'content' not in c['hooks'], c
assert any('/elsewhere/' in n for n in c['hooks']['notes']), c['hooks']
assert harness.get('codex').trust in rep['human'], rep['human']
assert settings.read_bytes() == original and not calls() and not (codex_home / 'hooks.json').exists()
# --apply asks first; the same command within the hour applies.
rep, code = S.run(apply=True)
assert code == 2 and rep['approval_required'] and rep['instruction'], rep
assert settings.read_bytes() == original and not calls()
rep, code = S.run(apply=True)
assert code == 0 and rep['ok'] and rep['applied'], rep
kept = read(settings)
assert kept['theme'] == 'dark' and kept['hooks']['PreToolUse'] == others['hooks']['PreToolUse'], kept
assert others['hooks']['SessionStart'][0] in kept['hooks']['SessionStart'], kept['hooks']['SessionStart']
assert others['hooks']['Stop'][0] in kept['hooks']['Stop'], 'another vault entry was touched'
for event in base.SCRIPTS:
    assert len(osk_entries(kept, event)) == 1, (event, kept)
backups = [Path(b) for b in rep['backups']]
assert [b.read_bytes() for b in backups if b.parent == claude_home] == [original], rep['backups']
codex_hooks = read(codex_home / 'hooks.json')['hooks']
assert codex_hooks['SessionStart'][0]['hooks'][0]['statusMessage'].startswith('osk:'), codex_hooks
assert codex_hooks['SessionStart'][0]['matcher'] == 'startup|resume|clear|compact', codex_hooks
added = [c for c in calls() if c[1:3] == ['mcp', 'add']]
assert [c[0] for c in added] == ['claude', 'codex'], calls()
assert added[0][-2:] == [vpy.as_posix(), server.as_posix()] and '--scope' in added[0], added
# What setup wrote is what doctor reads as this vault's registration.
for name in ('claude', 'codex'):
    servers, hooks, errors = harness.get(name).registrations()
    assert not errors and [s['name'] for s in servers] == ['osk-system'], (name, servers, errors)
    assert base.mentions(servers[0]['tokens'], server), servers
    for event, script in base.SCRIPTS.items():
        assert sum(base.mentions(h['tokens'], hooks_dir / script) for h in hooks) == 1, (name, event)
# Running again finds nothing to do: no confirmation, no backup, no CLI call.
before = (len(calls()), sorted(p.name for p in claude_home.iterdir()))
rep, code = S.run(apply=True)
assert code == 0 and not rep['changes'] and rep['applied'] is False, rep
assert all(v == 'keep' for h in rep['hosts'] for v in h['hooks']['events'].values()), rep
assert (len(calls()), sorted(p.name for p in claude_home.iterdir())) == before
# A stale entry of this vault is replaced, not duplicated.
data = read(settings)
for group in data['hooks']['SessionStart']:
    for h in group['hooks']:
        if base.mentions(base.command_tokens(h), hooks_dir / 'claude_session_start.py'):
            h['command'] = h['command'].replace(vpy.as_posix(), '/old/python')
settings.write_text(json.dumps(data), encoding='utf-8')
rep, _ = S.run()
assert rep['hosts'][0]['hooks']['events']['SessionStart'] == 'replace', rep['hosts'][0]
S.run(apply=True)
rep, code = S.run(apply=True)
assert code == 0 and rep['ok'], rep
assert len(osk_entries(read(settings), 'start')) == 1 and '/old/python' not in settings.read_text(encoding='utf-8')
# An osk entry that changed after the request needs a new confirmation.
S.run(apply=True, uninstall=True)
data = read(settings)
data['hooks']['SessionStart'] = [g for g in data['hooks']['SessionStart'] if not any(
    base.mentions(base.command_tokens(h), hooks_dir / 'claude_session_start.py') for h in g['hooks'])]
settings.write_text(json.dumps(data), encoding='utf-8')
rep, code = S.run(apply=True, uninstall=True)
assert code == 2 and rep['approval_required'], 'applied a plan the user had not seen'
# Other changes made after the request are kept: the write merges into the newest file
# (PR #106 review) — a permission rule and another tool's hook must not be lost.
data = read(settings)
data['permissions'] = {'deny': ['Bash(rm:*)']}
data['hooks']['PreToolUse'].append({'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': 'echo guard'}]})
settings.write_text(json.dumps(data), encoding='utf-8')
(codex_home / 'hooks.json').write_text(json.dumps({**read(codex_home / 'hooks.json'), 'note': 1}),
                                       encoding='utf-8')
# Uninstall takes out only this vault's entries.
rep, code = S.run(apply=True, uninstall=True)
assert code == 0 and rep['ok'], rep
left = read(settings)
assert left['permissions'] == {'deny': ['Bash(rm:*)']} and len(left['hooks']['PreToolUse']) == 2, left
assert read(codex_home / 'hooks.json')['note'] == 1, read(codex_home / 'hooks.json')
assert left['theme'] == 'dark' and others['hooks']['PreToolUse'][0] in left['hooks']['PreToolUse'], left
assert others['hooks']['Stop'][0] in left['hooks']['Stop'], left
assert not any(osk_entries(left, e) for e in base.SCRIPTS), left
assert 'SessionStart' not in read(codex_home / 'hooks.json').get('hooks', {}), read(codex_home / 'hooks.json')
removed = [c[0] for c in calls() if c[1:3] == ['mcp', 'remove']]
assert removed[-2:] == ['claude', 'codex'], calls()
for name in ('claude', 'codex'):
    assert not harness.get(name).registrations()[0], name
''')

    def test_without_a_cli_the_mcp_step_is_left_to_the_user(self):
        self.check_case(r'''
baseline()
os.environ['PATH'] = str(bin_dir)      # a real Codex CLI on the test machine must not be found
rep, code = S.run(only=['codex'])
mcp = rep['hosts'][0]['mcp']
assert code == 0 and mcp['action'] == 'add' and 'run' not in mcp, mcp
assert mcp['manual'].startswith('codex mcp add osk-system -- '), mcp
assert any(mcp['manual'] in step for step in rep['human']), rep['human']
S.run(apply=True, only=['codex'])
rep, code = S.run(apply=True, only=['codex'])
assert code == 0 and rep['ok'] and (codex_home / 'hooks.json').is_file() and not calls(), rep
# Unreadable settings are reported and left alone.
(codex_home / 'hooks.json').write_text('{broken', encoding='utf-8')
rep, code = S.run(apply=True, only=['codex'])
assert code == 1 and not rep['ok'] and 'hooks' in rep['errors'][0], rep
assert (codex_home / 'hooks.json').read_text(encoding='utf-8') == '{broken'
''')

    def test_a_batch_file_cli_gets_a_command_line_cmd_reads(self):
        self.check_case(r'''
fake_clis()
baseline()
# npm installs the CLIs as `claude.cmd`/`codex.cmd`, like the fake ones here, and Windows runs a batch
# file through `cmd /c`. An unquoted `&` splits the command there: the CLI registers `C:/R`, and cmd
# tries to run `D/osk/...`.
odd = Path('C:/R&D/osk/.venv/Scripts/python.exe' if os.name == 'nt' else '/R&D/osk/.venv/bin/python')
with mock.patch.object(S, '_python', lambda: odd):
    S.run(apply=True, only=['claude', 'codex'])
    rep, code = S.run(apply=True, only=['claude', 'codex'])
    assert code == 0 and rep['ok'], rep
    added = [c for c in calls() if c[1:3] == ['mcp', 'add']]
    assert [c[0] for c in added] == ['claude', 'codex'], calls()
    assert all(c[-2:] == [odd.as_posix(), server.as_posix()] for c in added), added
    rep, _ = S.run(only=['claude', 'codex'])
    assert [h['mcp']['action'] for h in rep['hosts']] == ['keep', 'keep'], rep['hosts']
if os.name == 'nt':
    # cmd expands `%` and `!` even inside double quotes: the plan stops before anything runs or is written.
    state = lambda: (len(calls()), read(claude_home / '.claude.json'), (claude_home / 'settings.json').read_bytes())
    before = state()
    for bad in ('C:/100%/osk/python.exe', 'C:/Hi!/osk/python.exe'):
        with mock.patch.object(S, '_python', lambda: Path(bad)):
            rep, code = S.run(apply=True, only=['claude'])
        assert code == 1 and rep['applied'] is False, rep
        assert any('배치 파일' in e and '--harness' in e for e in rep['errors']), rep['errors']
    assert state() == before
    # Without a CLI on PATH the step is left to the user. PowerShell hands a `.cmd` CLI `C:/R&D/...`
    # unquoted, and npm's `.ps1`, which it prefers when scripts may run, `--%` as an argument (PR #116
    # review): the line must still reach the CLI the user installs afterwards.
    full, moved = os.environ['PATH'], Path('C:/R&D/moved/.venv/Scripts/python.exe')
    os.environ['PATH'] = str(bin_dir.parent)
    with mock.patch.object(S, '_python', lambda: moved):
        rep, _ = S.run(only=['claude'])
    os.environ['PATH'] = full
    manual = rep['hosts'][0]['mcp']['manual']
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-Command', manual], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and calls()[-2:] == [
        ['claude', 'mcp', 'remove', '--scope', 'user', 'osk-system'],
        ['claude', 'mcp', 'add', '--scope', 'user', 'osk-system', '--', moved.as_posix(), server.as_posix()]], \
        (manual, r.stdout, r.stderr, calls()[-2:])
''')

    def test_the_baseline_is_bound_to_the_update_plan(self):
        self.check_case(r'''
fake_clis()
(root / 'release.json').write_text(json.dumps({'version': 'v4.0.0', 'files': {}}), encoding='utf-8')
ticket = core.local_lock_path(update.CONFIRMATION)
state, applied = {'id': 'r1'}, []
def fake_run(source=None, ref=None, bundle=None, apply=False, adopt=False):
    # The updater's own gate: under its lock it plans again and applies only the confirmed plan.
    # A pending drift lands on the next call, whether a plan or that locked re-plan.
    if 'drift' in state:
        state['id'] = state.pop('drift')
    plan = {'review_id': state['id'], 'files': 133, 'rebaseline': ['a'], 'add': [], 'update': [],
            'conflict': [], 'governance': {'protect': 'establish'}}
    if not apply:
        return plan
    pending = json.loads(ticket.read_text(encoding='utf-8')) if ticket.is_file() else {}
    if pending.get('review_id') != plan['review_id']:
        update.confirm(plan['review_id'])
        return {**plan, 'ok': False, 'approval_required': True}
    ticket.unlink()
    applied.append(plan['review_id'])
    baseline()
    return {'ok': True, 'governance_protected': 'established'}
record = S._record_baseline
def drifting(b):
    state['drift'] = 'r2'
    return record(b)
with mock.patch.object(S.update, 'run', fake_run):
    # The release plan changes after the user confirmed this setup plan, before the baseline
    # step (PR #106 review): nothing is applied and no confirmation of the unseen plan is left.
    S.run(apply=True, only=['claude'])
    with mock.patch.object(S, '_record_baseline', drifting):
        rep, code = S.run(apply=True, only=['claude'])
    step = rep['steps'][0]
    assert code == 1 and step['step'] == 'baseline' and not step['ok'] and '바뀌었다' in step['error'], rep
    assert not applied and update.current_version() is None and not ticket.exists(), (applied, ticket)
    # Planned and confirmed again, that plan is applied, through one call to the updater.
    rep, _ = S.run(only=['claude'])
    b = rep['baseline']
    assert (b['state'], b['version'], b['review_id'], b['governance']) == ('record', 'v4.0.0', 'r2', 'establish'), b
    assert any('update.jsonl' in step for step in rep['human']), rep['human']
    S.run(apply=True, only=['claude'])
    rep, code = S.run(apply=True, only=['claude'])
    step = rep['steps'][0]
    assert code == 0 and step == {'step': 'baseline', 'ok': True, 'current': 'v4.0.0',
                                  'governance_protected': 'established'}, rep
    assert applied == ['r2'], applied
# Once recorded, the baseline is not planned again.
rep, _ = S.run(only=['claude'])
assert rep['baseline'] == {'state': 'recorded', 'current': 'v4.0.0'}, rep['baseline']
''')

    def test_a_confirmed_plan_merges_into_the_newest_settings(self):
        self.check_case(r'''
fake_clis()
baseline()
settings = claude_home / 'settings.json'
settings.write_text(json.dumps({'hooks': {}}), encoding='utf-8')
p = S.plan(['claude'])
# While the user reads the plan, another tool tightens the settings (PR #106 review).
guard = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo guard'}]}
settings.write_text(json.dumps({'permissions': {'deny': ['Bash(rm:*)']}, 'hooks': {'PreToolUse': [guard]}}),
                    encoding='utf-8')
res = S._apply(p)
kept = read(settings)
assert res['ok'] and kept['permissions'] == {'deny': ['Bash(rm:*)']}, (res, kept)
assert kept['hooks']['PreToolUse'] == [guard], kept
assert all(len(osk_entries(kept, e)) == 1 for e in base.SCRIPTS), kept
# If what the plan would change changed after the plan, nothing is written or run: here
# the osk hook appeared by hand, and another vault took the MCP name.
S.run(apply=True, uninstall=True, only=['claude'])
S.run(apply=True, uninstall=True, only=['claude'])
p = S.plan(['claude'])
start = harness.get('claude').hook_group('start', S._command(harness.get('claude'), 'start'))
settings.write_text(json.dumps({'hooks': {'SessionStart': [start]}}), encoding='utf-8')
(claude_home / '.claude.json').write_text(json.dumps({'mcpServers': {'osk-system': {
    'command': py, 'args': ['/elsewhere/_governance/_engine/mcp_server.py']}}}), encoding='utf-8')
before, ran = settings.read_bytes(), len(calls())
res = S._apply(p)
steps = {s['step']: s for s in res['steps']}
assert not res['ok'] and '다시 계획' in steps['claude hooks']['error'], res
assert '다시 계획' in steps['claude mcp']['error'] and len(calls()) == ran, (res, calls()[ran:])
assert settings.read_bytes() == before
''')

    def test_mcp_ownership_is_judged_in_the_file_the_cli_changes(self):
        self.check_case(r'''
fake_clis()
baseline()
elsewhere = '/elsewhere/_governance/_engine/mcp_server.py'
# The CLI changes $CLAUDE_CONFIG_DIR/.claude.json; ~/.claude.json holds an older entry of this
# vault (PR #106 review). Uninstall must not remove the other vault's current entry.
(home / '.claude.json').write_text(json.dumps({'mcpServers': {'osk-system': {
    'command': vpy.as_posix(), 'args': [server.as_posix()]}}}), encoding='utf-8')
(claude_home / '.claude.json').write_text(json.dumps({'mcpServers': {'osk-system': {
    'command': py, 'args': [elsewhere]}}}), encoding='utf-8')
rep, _ = S.run(uninstall=True, only=['claude'])
mcp = rep['hosts'][0]['mcp']
assert mcp['action'] == 'absent' and 'run' not in mcp and 'manual' not in mcp, mcp
assert any(str(home / '.claude.json') in n for n in mcp['notes']), mcp
assert mcp['file'] == str(claude_home / '.claude.json'), mcp
# Installing replaces the other vault's entry only as a confirmed, named step.
rep, _ = S.run(only=['claude'])
mcp = rep['hosts'][0]['mcp']
assert mcp['action'] == 'replace' and elsewhere in mcp['replaces'][0], mcp
''')

    def test_the_bootstrap_prepares_nothing_for_a_plan_and_hands_over_to_the_engine(self):
        self.check_case(r'''
import venv
baseline()
boot = Path(S.__file__).resolve().parents[1] / 'scripts' / 'setup.py'
def run_boot(*args):
    r = subprocess.run([py, str(boot), *args], capture_output=True, timeout=240, cwd=str(root))
    return r.returncode, r.stdout.decode('utf-8'), r.stderr.decode('utf-8', 'replace')
code, out, err = run_boot()
rep = json.loads(out)
assert code == 0 and rep['state'] == 'missing' and not (root / '.venv').exists(), (out, err)
# A venv that can import the server's packages is used as it is.
venv.EnvBuilder(system_site_packages=True, with_pip=False).create(root / '.venv')
code, out, err = run_boot()
rep = json.loads(out)
assert code == 0 and rep['python'] == str(vpy) and rep['root'] == str(root), (out, err)
code, out, err = run_boot('doctor', '--json')
assert code == 0 and 'hosts' in json.loads(out), (out, err)
''')

    def test_chosen_features_go_through_the_same_confirmation(self):
        self.check_case(FAKE_SERVICES + r'''
fake_clis()
baseline()
S.run(apply=True)
S.run(apply=True)                       # hosts connected; only the chosen features remain
rep, _ = S.run()
assert not rep['changes'] and not any(k in rep for k in ('fork', 'schedule', 'sync')), rep
# fork: the CLI each host would use is written once confirmed.
rep, code = S.run(apply=True, fork=True)
assert code == 2 and {n: e['action'] for n, e in rep['fork']['entries'].items()} == \
    {'claude': 'add', 'codex': 'add'}, rep.get('fork')
assert any('auth login' in s for s in rep['human']) and any('fork doctor' in s for s in rep['human']), rep['human']
rep, code = S.run(apply=True, fork=True)
config_file = root / '.osk' / 'response-growth.json'
config = read(config_file)
assert code == 0 and rep['ok'] and set(config) == {'claude', 'codex'} and Path(config['claude']).is_file(), rep
# A CLI the user chose stays chosen.
mine = bin_dir / 'my-claude.exe'
mine.write_text('', encoding='utf-8')
config_file.write_text(json.dumps({**config, 'claude': str(mine)}), encoding='utf-8')
rep, code = S.run(apply=True, fork=True)
assert code == 0 and not rep['changes'] and rep['fork']['entries']['claude'] == {'action': 'keep', 'path': str(mine)}, rep
# Daily run: the command is this vault's MCP only, the subscription only, no other tool.
rep, code = S.run(apply=True, schedule='claude', at='07:30')
argv = rep['schedule']['command']['argv']
assert code == 2 and rep['schedule']['command']['action'] == 'add', rep
assert argv[1] == '-p' and argv[argv.index('--permission-mode') + 1] == 'dontAsk', argv
assert argv[argv.index('--allowedTools') + 1] == 'mcp__osk-system' and '--strict-mcp-config' in argv, argv
assert json.loads(argv[argv.index('--settings') + 1]) == {'forceLoginMethod': 'claudeai'}, argv
assert argv[argv.index('--tools') + 1] == '', argv          # no built-in tool, whatever settings allow
assert harness.get('claude').subscription_only(argv), argv  # the runner applies the fork's credentials
server_entry = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']['osk-system']
assert server_entry['args'] == [server.as_posix()] and server_entry['env'] == {'OSK_VAULT_ROOT': root.as_posix()}, argv
assert rep['schedule']['task']['action'] == 'add' and '07:30' in rep['schedule']['task']['command'], rep['schedule']
assert any('--limit 1' in s for s in rep['human']), rep['human']
rep, code = S.run(apply=True, schedule='claude', at='07:30')
command_file = root / '.osk' / 'growth-command.json'
assert code == 0 and rep['ok'] and read(command_file) == argv, rep
assert Fake.rows[f'osk-growth-{services.tag()}']['definition']['at'] == '07:30'
# A new time replaces the task and keeps the command file, which may carry the user's edits.
rep, code = S.run(apply=True, schedule='claude', at='08:00')
assert code == 2 and rep['schedule']['command']['action'] == 'keep' and rep['schedule']['task']['action'] == 'replace', rep
# The confirmation covers the chosen features: 08:00 was asked, 09:30 was not.
rep, code = S.run(apply=True, schedule='claude', at='09:30')
assert code == 2 and Fake.rows[f'osk-growth-{services.tag()}']['definition']['at'] == '07:30', rep
rep, _ = S.run(schedule='claude', at='25:00')
assert not rep['ok'] and 'HH:MM' in rep['errors'][0], rep
# setup writes no daily command for Codex; the user's own command file is used as it is.
command_file.unlink()
rep, code = S.run(apply=True, schedule='codex')
assert code == 1 and not rep['ok'] and '만들지 않는다' in rep['errors'][0] and not command_file.exists(), rep
import shutil
command_file.write_text(json.dumps([shutil.which('codex'), 'exec', '-']), encoding='utf-8')
rep, _ = S.run(schedule='codex')
assert rep['ok'] and rep['schedule']['command']['action'] == 'keep', rep
# Sync: a local main, a private origin, and a push that does not ask.
rep, _ = S.run(sync=True)
assert not rep['ok'] and 'main' in rep['errors'][0] and 'origin' in rep['errors'][0], rep
git = lambda *a: subprocess.run(['git', '-C', str(root), *a], check=True, capture_output=True)
git('add', '-A')
git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'vault')
git('remote', 'add', 'origin', 'git@github.com:LPAIU-CS/osk-system.git')
rep, _ = S.run(sync=True)
assert not rep['ok'] and '정본' in rep['errors'][0], rep
bare = root.parent / 'private.git'
git('remote', 'set-url', 'origin', str(bare))
rep, _ = S.run(sync=True)
assert not rep['ok'] and 'push' in rep['errors'][0], rep      # the remote is not there yet
subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True, capture_output=True)
rep, code = S.run(apply=True, sync=True)
assert code == 2 and rep['sync']['task']['action'] == 'add' and rep['sync']['origin'] == str(bare), rep
assert any('비공개' in s for s in rep['human']), rep['human']
rep, code = S.run(apply=True, sync=True)
assert code == 0 and rep['ok'] and f'osk-sync-{services.tag()}' in Fake.rows, rep
# Uninstalling one feature leaves the hosts and the other features.
rep, code = S.run(apply=True, uninstall=True, sync=True)
assert code == 2 and rep['hosts'] == [] and 'fork' not in rep and rep['sync']['task']['action'] == 'remove', rep
rep, code = S.run(apply=True, uninstall=True, sync=True)
assert code == 0 and f'osk-sync-{services.tag()}' not in Fake.rows and f'osk-growth-{services.tag()}' in Fake.rows, rep
assert osk_entries(read(claude_home / 'settings.json'), 'start'), 'uninstalling sync removed a hook'
# Uninstalling everything takes every osk registration of this vault, not the command file.
S.run(apply=True, uninstall=True)
rep, code = S.run(apply=True, uninstall=True)
assert code == 0 and rep['ok'] and not Fake.rows and not config_file.exists() and command_file.exists(), rep
assert not osk_entries(read(claude_home / 'settings.json'), 'start')
''')

    def test_a_feature_that_changed_after_confirmation_is_not_written(self):
        self.check_case(FAKE_SERVICES + r'''
fake_clis()
baseline()
p = S.plan(fork=True, schedule='claude')
config_file = root / '.osk' / 'response-growth.json'
config_file.parent.mkdir(parents=True, exist_ok=True)
config_file.write_text(json.dumps({'claude': str(py)}), encoding='utf-8')
(root / '.osk' / 'growth-command.json').write_text('["x"]', encoding='utf-8')
result = S._apply(p)
steps = {s['step']: s for s in result['steps']}
assert not result['ok'] and '바뀌었다' in steps['fork CLI']['error'], result
assert '생겼다' in steps['growth command']['error'] and not Fake.rows, result
assert read(config_file) == {'claude': str(py)}
''')

    def test_sync_checks_and_confirms_every_push_target(self):
        self.check_case(FAKE_SERVICES + r'''
fake_clis()
baseline()
S.run(apply=True)
S.run(apply=True)
git = lambda *a: subprocess.run(['git', '-C', str(root), *a], check=True, capture_output=True)
git('add', '-A')
git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'vault')
private, second = root.parent / 'private.git', root.parent / 'second.git'
for bare in (private, second):
    subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True, capture_output=True)
git('remote', 'add', 'origin', str(private))
# The fetch URL is private, but pushes go to the public canonical repository.
git('config', 'remote.origin.pushurl', 'https://github.com/lpaiu-cs/osk-system.git')
rep, code = S.run(apply=True, sync=True)
assert code == 1 and '정본' in rep['errors'][0] and not Fake.rows, rep
# Every push target is shown and confirmed; a target added afterwards needs a new confirmation.
git('config', 'remote.origin.pushurl', str(private))
rep, code = S.run(apply=True, sync=True)
assert code == 2 and rep['sync']['push'] == [str(private)], rep
git('config', '--add', 'remote.origin.pushurl', str(second))
rep, code = S.run(apply=True, sync=True)
assert code == 2 and rep['sync']['push'] == [str(private), str(second)] and not Fake.rows, rep
assert any(str(second) in s for s in rep['human']), rep['human']
rep, code = S.run(apply=True, sync=True)
assert code == 0 and rep['ok'] and f'osk-sync-{services.tag()}' in Fake.rows, rep
''')

    def test_uninstall_without_a_service_manager_still_removes_the_hosts(self):
        self.check_case(r'''
fake_clis()
baseline()
S.run(apply=True)
S.run(apply=True)
assert osk_entries(read(claude_home / 'settings.json'), 'start')
mock.patch.object(S.services, 'backend', lambda run=None: None).start()
rep, code = S.run(apply=True, uninstall=True)
assert code == 2 and rep['approval_required'] and 'errors' not in rep, rep
assert (rep['schedule']['task']['action'], rep['sync']['task']['action']) == ('absent', 'absent'), rep
rep, code = S.run(apply=True, uninstall=True)
assert code == 0 and rep['ok'] and not osk_entries(read(claude_home / 'settings.json'), 'start'), rep
# Registering a feature still needs one.
rep, _ = S.run(schedule='claude')
assert not rep['ok'] and '서비스 관리자' in rep['errors'][0], rep
''')

    def test_kiro_gets_its_own_hook_file_and_a_merged_mcp_entry(self):
        self.check_case(r'''
fake_clis(('claude', 'codex', 'kiro'))   # `kiro` is the IDE launcher: setup must never run it
baseline()
kiro_home = home / '.kiro'
hooks = kiro_home / 'hooks'
hooks.mkdir(parents=True)
mcp_json = kiro_home / 'settings' / 'mcp.json'
mcp_json.parent.mkdir(parents=True)
other = {'command': 'node', 'args': ['other.js'], 'disabled': True}
mcp_json.write_text(json.dumps({'mcpServers': {'other': other}, 'note': 'kept'}), encoding='utf-8')
mine = hooks / 'osk-system.json'
lint = hooks / 'lint.json'
lint.write_text(json.dumps({'version': 'v1', 'hooks': [{'name': 'lint', 'trigger': 'PostFileSave',
                'action': {'type': 'command', 'command': 'npm run lint'}}]}), encoding='utf-8')
assert 'kiro' in [a.name for a in S.hosts()]
rep, code = S.run(only=['kiro'])
k = rep['hosts'][0]
assert (k['mcp']['action'], k['mcp']['file']) == ('add', str(mcp_json)), k
assert k['hooks']['file'] == str(mine) and set(k['hooks']['events'].values()) == {'add'}, k
assert 'content' not in k['mcp'] and 'content' not in k['hooks'], 'the plan shows no file bodies'
assert rep['changes'] and any('신뢰' in s for s in rep['human']), rep['human']
S.run(apply=True, only=['kiro'])
rep, code = S.run(apply=True, only=['kiro'])
assert code == 0 and rep['ok'], rep
data = read(mcp_json)
assert data['note'] == 'kept' and data['mcpServers']['other'] == other, data
assert data['mcpServers']['osk-system'] == {'command': vpy.as_posix(), 'args': [server.as_posix()]}, data
assert any(b.startswith(str(mcp_json) + '.osk-backup-') for b in rep['backups']), rep['backups']
written = read(mine)
assert written['version'] == 'v1', written
assert [h['trigger'] for h in written['hooks']] == ['SessionStart', 'UserPromptSubmit', 'Stop'], written
for h, event in zip(written['hooks'], ('start', 'input', 'stop')):
    assert h['action']['type'] == 'command' and base.mentions(base.command_tokens(h), hooks_dir / base.SCRIPTS[event]), h
assert read(lint)['hooks'][0]['name'] == 'lint'
assert not [c for c in calls() if c[0] == 'kiro'], calls()
# Planned again, everything is kept.
rep, _ = S.run(only=['kiro'])
k = rep['hosts'][0]
assert k['mcp']['action'] == 'keep' and set(k['hooks']['events'].values()) == {'keep'}, k
assert not rep['changes'], rep
# An MCP change alone is still a change to apply.
data = read(mcp_json)
del data['mcpServers']['osk-system']
mcp_json.write_text(json.dumps(data), encoding='utf-8')
rep, _ = S.run(only=['kiro'])
assert rep['hosts'][0]['mcp']['action'] == 'add' and rep['changes'], rep
S.run(apply=True, only=['kiro'])
rep, code = S.run(apply=True, only=['kiro'])
assert code == 0 and 'osk-system' in read(mcp_json)['mcpServers'], (rep, read(mcp_json))
# Another vault's registration under the name is replaced only as confirmed: if it changed
# after the plan, nothing is written.
elsewhere = {'command': py, 'args': ['/elsewhere/_governance/_engine/mcp_server.py']}
data = read(mcp_json)
data['mcpServers']['osk-system'] = elsewhere
mcp_json.write_text(json.dumps(data), encoding='utf-8')
p = S.plan(['kiro'])
assert p['hosts'][0]['mcp']['action'] == 'replace' and p['hosts'][0]['mcp']['replaces'], p['hosts'][0]['mcp']
data['mcpServers']['osk-system'] = {'command': py, 'args': ['/third/_governance/_engine/mcp_server.py']}
mcp_json.write_text(json.dumps(data), encoding='utf-8')
before = mcp_json.read_bytes()
res = S._apply(p)
steps = {s['step']: s for s in res['steps']}
assert not res['ok'] and '다시 계획' in steps['kiro mcp']['error'] and mcp_json.read_bytes() == before, res
S.run(apply=True, only=['kiro'])
rep, code = S.run(apply=True, only=['kiro'])
assert code == 0 and read(mcp_json)['mcpServers']['osk-system']['args'] == [server.as_posix()], read(mcp_json)
# A path update does not lift the user's policy on the entry (PR #108 review). This vault's
# entry run by another Python keeps every field but its command, including a restriction
# added while the plan waited for confirmation.
policy = {'disabled': True, 'disabledTools': ['append_raw'], 'autoApprove': ['search'], 'timeout': 60000}
data = read(mcp_json)
data['mcpServers']['osk-system'] = {'command': '/old/python', 'args': [server.as_posix()],
                                    'env': {'A': '1'}, **policy}
mcp_json.write_text(json.dumps(data), encoding='utf-8')
p = S.plan(['kiro'])
assert p['hosts'][0]['mcp']['action'] == 'replace', p['hosts'][0]['mcp']
data['mcpServers']['osk-system']['disabledTools'].append('create_node')
mcp_json.write_text(json.dumps(data), encoding='utf-8')
res = S._apply(p)
assert res['ok'], res
assert read(mcp_json)['mcpServers']['osk-system'] == {
    'command': vpy.as_posix(), 'args': [server.as_posix()], 'env': {'A': '1'},
    **policy, 'disabledTools': ['append_raw', 'create_node']}, read(mcp_json)
# Another vault's entry keeps only the policy: its environment points at that vault.
data = read(mcp_json)
data['mcpServers']['osk-system'] = {'command': py, 'args': ['/elsewhere/_governance/_engine/mcp_server.py'],
                                    'env': {'OSK_VAULT_ROOT': '/elsewhere'}, 'cwd': '/elsewhere', **policy}
mcp_json.write_text(json.dumps(data), encoding='utf-8')
S.run(apply=True, only=['kiro'])
rep, code = S.run(apply=True, only=['kiro'])
assert code == 0 and read(mcp_json)['mcpServers']['osk-system'] == {
    'command': vpy.as_posix(), 'args': [server.as_posix()], **policy}, (rep, read(mcp_json))
# A hook the user added to osk's file stays, even one Kiro cannot read; osk's own entries are
# replaced, not doubled.
added = {'name': 'mine too', 'trigger': 'Stop', 'action': {'type': 'command', 'command': 'echo hi'}}
odd = {'name': 'no trigger'}
drifted = read(mine)
drifted['hooks'] += [added, odd]
drifted['hooks'][0]['action']['command'] = base.hook_line(['/old/python', hooks_dir / base.SCRIPTS['start']])
mine.write_text(json.dumps(drifted), encoding='utf-8')
rep, _ = S.run(only=['kiro'])
assert rep['hosts'][0]['hooks']['events'] == {'SessionStart': 'replace', 'UserPromptSubmit': 'keep',
                                              'Stop': 'keep'}, rep['hosts'][0]
S.run(apply=True, only=['kiro'])
rep, code = S.run(apply=True, only=['kiro'])
assert code == 0 and rep['ok'], rep
now = read(mine)['hooks']
assert added in now and odd in now and sum(
    base.mentions(base.command_tokens(h), hooks_dir / base.SCRIPTS['start']) for h in now) == 1, now
# Uninstall takes out only osk's entries: the user's hooks keep osk's file alive.
S.run(apply=True, uninstall=True, only=['kiro'])
rep, code = S.run(apply=True, uninstall=True, only=['kiro'])
assert code == 0 and read(mine)['hooks'] == [added, odd], read(mine)
assert read(mcp_json) == {'mcpServers': {'other': other}, 'note': 'kept'}, read(mcp_json)
# Without other entries the file goes — an empty Kiro hook file fails Kiro's schema.
mine.write_text(json.dumps({'version': 'v1', 'hooks': [kiro_hook for kiro_hook in drifted['hooks'][:1]]}),
                encoding='utf-8')
S.run(apply=True, uninstall=True, only=['kiro'])
rep, code = S.run(apply=True, uninstall=True, only=['kiro'])
assert code == 0 and not mine.exists() and lint.exists(), rep
assert any(p.name.startswith('osk-system.json.osk-backup-') for p in hooks.iterdir())
assert not [c for c in calls() if c[0] == 'kiro'], calls()
''')

    def test_antigravity_gets_its_own_hook_name_and_a_merged_mcp_entry(self):
        self.check_case(r'''
fake_clis()
baseline()
config = home / '.gemini' / 'config'
config.mkdir(parents=True)
mcp_json, hooks_json = config / 'mcp_config.json', config / 'hooks.json'
other = {'command': 'node', 'args': ['other.js']}
mcp_json.write_text(json.dumps({'mcpServers': {'other': other}}), encoding='utf-8')
lint = {'PostToolUse': [{'matcher': 'run_command', 'hooks': [{'type': 'command', 'command': 'npm run lint'}]}]}
quiet = {'enabled': False, 'Stop': [{'type': 'command', 'command': 'echo bye'}]}
hooks_json.write_text(json.dumps({'lint': lint, 'quiet': quiet}), encoding='utf-8')
assert 'antigravity' in [a.name for a in S.hosts()]
rep, code = S.run(only=['antigravity'])
a = rep['hosts'][0]
assert (a['mcp']['action'], a['mcp']['file']) == ('add', str(mcp_json)), a
assert a['hooks']['file'] == str(hooks_json) and a['hooks']['events'] == {
    'SessionStart': 'add', 'PreInvocation': 'add', 'Stop': 'add'}, a
S.run(apply=True, only=['antigravity'])
rep, code = S.run(apply=True, only=['antigravity'])
assert code == 0 and rep['ok'], rep
assert read(mcp_json) == {'mcpServers': {'other': other, 'osk-system': {
    'command': vpy.as_posix(), 'args': [server.as_posix()]}}}, read(mcp_json)
written = read(hooks_json)
assert (written['lint'], written['quiet']) == (lint, quiet), written
mine = written['osk-system']
assert set(mine) == {'SessionStart', 'PreInvocation', 'Stop'}, mine
for event in ('start', 'input', 'stop'):
    [h] = mine[harness.get('antigravity').events[event]]
    # The flat events hold handlers, not matcher groups, and Windows' `cmd /c` gets no quotes.
    assert h['type'] == 'command' and '"' not in h['command'], h
    assert base.mentions(base.command_tokens(h), hooks_dir / base.SCRIPTS[event]), h
# Planned again, everything is kept.
rep, _ = S.run(only=['antigravity'])
a = rep['hosts'][0]
assert a['mcp']['action'] == 'keep' and set(a['hooks']['events'].values()) == {'keep'}, a
assert not rep['changes'], rep
# On Windows a path cmd would split — a space, or `&` and the like without one — cannot be
# registered: setup refuses before writing anything, and says how to connect the other hosts.
# Removing osk's hooks never needs the path.
if os.name == 'nt':
    before = hooks_json.read_bytes()
    for odd in ('C:/Program Files/Python/python.exe', 'C:/R&D/Python/python.exe'):
        with mock.patch.object(S, '_python', lambda: Path(odd)):
            rep, code = S.run(apply=True, only=['antigravity'])
            assert code == 1 and any('--harness' in e for e in rep['errors']), (odd, rep)
            rep, _ = S.run(uninstall=True, only=['antigravity'])
            assert rep['ok'] and set(rep['hosts'][0]['hooks']['events'].values()) == {'remove'}, rep
    assert hooks_json.read_bytes() == before
# Uninstall takes out only osk's name; with nothing else left the file goes.
S.run(apply=True, uninstall=True, only=['antigravity'])
rep, code = S.run(apply=True, uninstall=True, only=['antigravity'])
assert code == 0 and read(hooks_json) == {'lint': lint, 'quiet': quiet}, read(hooks_json)
assert read(mcp_json) == {'mcpServers': {'other': other}}, read(mcp_json)
hooks_json.write_text(json.dumps({'osk-system': mine}), encoding='utf-8')
S.run(apply=True, uninstall=True, only=['antigravity'])
rep, code = S.run(apply=True, uninstall=True, only=['antigravity'])
assert code == 0 and not hooks_json.exists(), rep
''')


if __name__ == '__main__':
    unittest.main()
