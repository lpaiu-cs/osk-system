"""setup connects this vault to the hosts on this device: only osk entries, backed up, idempotent.

Each case runs in its own process because `core.ROOT` is fixed at import. Every host
home (`HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`) is a temporary folder, and
fake `claude`/`codex` CLIs come first on `PATH`: they log their arguments and write the
MCP entry the real CLI would write, so no case touches the developer's configuration.
"""
import os
from pathlib import Path
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
else:
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
from osk import core, harness, update, validate
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
def fake_clis():
    for name in ('claude', 'codex'):
        if os.name == 'nt':
            (bin_dir / f'{name}.cmd').write_text(f'@"{py}" "{bin_dir / "fake_cli.py"}" {name} %*\r\n',
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


def _run(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home, bin_dir = td / 'home', td / 'bin'
        home.mkdir()
        bin_dir.mkdir()
        (bin_dir / 'fake_cli.py').write_text(FAKE_CLI, encoding='utf-8')
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS', 'OSK_GROWTH_WORKER')}
        env.update(OSK_VAULT_ROOT=str(td / 'vault'), PYTHONPATH=str(ENGINE), PYTHONUTF8='1',
                   OSK_UPDATE_CHECK='0', HOME=str(home), USERPROFILE=str(home),
                   CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'),
                   OSK_TEST_BIN=str(bin_dir), OSK_TEST_LOG=str(td / 'cli.log'),
                   PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
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
start = harness.get('claude').hook_group('start', S._command('start'))
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


if __name__ == '__main__':
    unittest.main()
