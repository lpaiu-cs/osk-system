"""Harness adapters keep the v4 judgment; hooks leave run records; doctor reads without writing.

Each case runs in its own process because `core.ROOT` is fixed at import. Every host
home (`HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`) is a temporary folder,
and a fake `claude`/`codex` comes first on `PATH`, so no case reads the developer's
own configuration or starts a real host CLI.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1]

SETUP = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from unittest import mock
from osk import core, harness, integration, validate
from osk.harness import runs
root = core.ROOT
validate.make_mini_vault(root)
subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True, capture_output=True)
home = Path.home()
claude_home = Path(os.environ['CLAUDE_CONFIG_DIR'])
codex_home = Path(os.environ['CODEX_HOME'])
for folder in (claude_home, codex_home):
    folder.mkdir(parents=True, exist_ok=True)
bin_dir = Path(os.environ['OSK_TEST_BIN'])
# Registrations point at this vault's own engine copy; the hooks under test are this engine's.
engine = root / '_governance' / '_engine'
hooks_dir = engine / 'scripts' / 'hooks'
server = engine / 'mcp_server.py'
tested_hooks = Path(harness.__file__).resolve().parents[2] / 'scripts' / 'hooks'
py = sys.executable
def fake_cli(name, text):
    if os.name == 'nt':
        (bin_dir / f'{name}.cmd').write_text(f'@echo {text}\r\n', encoding='utf-8')
    else:
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\necho "{text}"\n', encoding='utf-8')
        path.chmod(0o755)
def rows(path, *items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join((json.dumps(i) if not isinstance(i, str) else i) + '\n' for i in items),
                    encoding='utf-8')
    return path
'''


def _run(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home, bin_dir = td / 'home', td / 'bin'
        home.mkdir()
        bin_dir.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS', 'OSK_GROWTH_WORKER')}
        env.update(OSK_VAULT_ROOT=str(td / 'vault'), PYTHONPATH=str(ENGINE), PYTHONUTF8='1',
                   OSK_UPDATE_CHECK='0', HOME=str(home), USERPROFILE=str(home),
                   CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'),
                   OSK_TEST_BIN=str(bin_dir), PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
        return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=240)


class HarnessTests(unittest.TestCase):
    def check_case(self, body: str) -> None:
        result = _run(SETUP + body)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_adapters_keep_the_v4_judgment_and_names(self):
        self.check_case(r'''
# integration.hook_source's judgment before the adapters (v4.0.0), verbatim.
def before(env):
    sid = env.get("session_id") or env.get("conversation_id") or os.environ.get("CODEX_THREAD_ID")
    found = env.get("harness") or os.environ.get("OSK_HARNESS")
    path = env.get("transcript_path")
    if not found and os.environ.get("CODEX_THREAD_ID") == sid:
        found = "codex"
    if not found and path and sid and Path(path).stem == sid:
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
        if Path(path).resolve().is_relative_to(base.resolve()):
            found = "claude"
    if not found and path:
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("type") == "session_meta":
                    found = "codex"
                    break
                if row.get("sessionId"):
                    found = "claude"
                    break
    return sid, found
def after(env):
    sid = harness.session_id(env)
    return sid, harness.detect(env, sid, env.get("transcript_path"))
def outcome(judge, env, environ):
    with mock.patch.dict(os.environ, environ):
        for key in ('CODEX_THREAD_ID', 'OSK_HARNESS'):
            if key not in environ:
                os.environ.pop(key, None)
        try:
            return judge(env)
        except Exception as exc:
            return type(exc).__name__
fresh = claude_home / 'projects' / 'p' / 'own.jsonl'            # before the file exists
other_stem = claude_home / 'projects' / 'p' / 'other.jsonl'
rollout = rows(root.parent / 'rollout.jsonl', {'type': 'session_meta', 'payload': {'id': 'own'}})
claude_file = rows(root.parent / 'moved.jsonl', '', {'type': 'summary'}, {'type': 'user', 'sessionId': 'own'})
malformed = rows(root.parent / 'malformed.jsonl', 'not json')
neither = rows(root.parent / 'neither.jsonl', {'type': 'other'})
cases = [({'session_id': 'own', 'harness': 'codex'}, {}),
         ({'session_id': 'own', 'harness': 'gemini'}, {}),
         ({'session_id': 'own'}, {'OSK_HARNESS': 'claude'}),
         ({'session_id': 'own'}, {'CODEX_THREAD_ID': 'own'}),
         ({}, {'CODEX_THREAD_ID': 'own'}),
         ({'session_id': 'own', 'transcript_path': str(fresh)}, {}),
         ({'session_id': 'own', 'transcript_path': str(fresh)}, {'CODEX_THREAD_ID': 'other'}),
         ({'conversation_id': 'own', 'transcript_path': str(rollout)}, {}),
         ({'session_id': 'own', 'transcript_path': str(claude_file)}, {}),
         ({'session_id': 'own', 'transcript_path': str(malformed)}, {}),
         ({'session_id': 'own', 'transcript_path': str(neither)}, {}),
         ({'session_id': 'own', 'transcript_path': str(other_stem)}, {}),
         ({'session_id': 'own'}, {})]
for env, environ in cases:
    old, new = outcome(before, env, environ), outcome(after, env, environ)
    assert old == new, (env, environ, old, new)
assert outcome(after, cases[5][0], {}) == ('own', 'claude')
assert outcome(after, cases[7][0], {}) == ('own', 'codex')
# Without an ID nothing is judged, even with an unreadable transcript (same error as v4.0.0).
try:
    integration.hook_source({'transcript_path': str(malformed)})
    raise AssertionError('judged without a conversation ID')
except ValueError as exc:
    assert 'no actual conversation ID' in str(exc), exc
# A Codex subagent's hook that names its root conversation changes nothing.
child = rows(root.parent / 'child.jsonl', {'type': 'session_meta', 'payload': {'id': 'child', 'session_id': 'own'}})
try:
    integration.hook_source({'session_id': 'own', 'transcript_path': str(child)})
    raise AssertionError('subagent event was not recognized')
except integration.SubagentEvent as exc:
    assert 'Codex subagent' in str(exc), exc

assert harness.NAMES == ('claude', 'codex') and harness.fork_names() == ('claude', 'codex')
try:
    integration._identity('gemini', 'x')
    raise AssertionError('unknown harness accepted')
except ValueError as exc:
    assert str(exc) == 'explicit claude/codex harness and actual conversation_id required', exc
# Transcripts are found where each host keeps them; two candidates need an explicit path.
mine = rows(claude_home / 'projects' / 'p2' / 'c1.jsonl', {'sessionId': 'c1'})
assert integration._locate_transcript('claude', 'c1') == str(mine.resolve())
live = rows(codex_home / 'sessions' / '2026' / '09' / '26' / 'rollout-2026-09-26T00-00-00-x1.jsonl', {})
assert integration._locate_transcript('codex', 'x1') == str(live.resolve())
rows(codex_home / 'archived_sessions' / 'rollout-old-x1.jsonl', {})
try:
    integration._locate_transcript('codex', 'x1')
    raise AssertionError('ambiguous transcripts accepted')
except ValueError as exc:
    assert 'multiple transcripts' in str(exc), exc
from osk import response_growth as rg
rg.CONFIG.parent.mkdir(parents=True, exist_ok=True)
rg.CONFIG.write_text(json.dumps({'gemini': py}), encoding='utf-8')
try:
    rg.configured('claude')
    raise AssertionError('unknown fork harness accepted')
except ValueError as exc:
    assert 'must map harness names' in str(exc), exc
rg.CONFIG.unlink()
for argv in (['integration', 'status', '--harness', 'gemini', '--conversation', 'x'],
             ['fork', 'doctor', '--harness', 'gemini'], ['doctor', '--harness', 'gemini']):
    r = subprocess.run([py, '-m', 'osk.cli', *argv], capture_output=True, text=True, encoding='utf-8')
    assert r.returncode == 2 and 'invalid choice' in r.stderr, (argv, r.returncode, r.stderr)

# Both A hosts take one hook envelope; the unknown host gets the same.
for adapter in (*harness.ADAPTERS, harness.FALLBACK):
    assert adapter.hook_output('start', 't') == {
        'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 't'}}
    assert adapter.hook_output('input', 't', 'm') == {
        'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 't'},
        'systemMessage': 'm'}
    assert adapter.hook_notice('stop', 'm') == {'systemMessage': 'm'}
claude, codex = harness.get('claude'), harness.get('codex')
assert claude.parse_version('2.1.280 (Claude Code)') == '2.1.280'
assert codex.parse_version('codex-cli 0.155.0-alpha.16') == '0.155.0-alpha.16'
assert claude.parse_version('codex-cli 1.0.0') is None and codex.parse_version('2.1.280 (Claude Code)') is None
resumed = rows(root.parent / 'versions.jsonl', {'sessionId': 'v', 'version': '2.1.200'},
               {'sessionId': 'v', 'version': '2.1.281'}, 'torn')
assert claude.transcript_version(str(resumed)) == '2.1.281'
created = rows(root.parent / 'rollout-v.jsonl', {'type': 'session_meta', 'payload': {'id': 'v', 'cli_version': '0.154.0'}})
assert codex.transcript_version(str(created)) == '0.154.0'
assert harness.newer('0.158.0-alpha.2', codex.verified) and not harness.newer('0.154.0', codex.verified)
assert harness.newer('2.2.0', claude.verified) and not harness.newer(claude.verified, claude.verified)
assert not harness.newer('unknown', claude.verified) and not harness.newer(None, claude.verified)
''')

    def test_hooks_record_runs_and_keep_their_output(self):
        self.check_case(r'''
proj = root.parent / 'proj'           # not a Git work tree: the folder name is the session key
proj.mkdir()
def hook(name, payload=None, raw=None, **extra):
    data = raw if raw is not None else json.dumps(payload).encode('utf-8')
    r = subprocess.run([py, str(tested_hooks / name)], input=data, capture_output=True, timeout=120,
                       env={**os.environ, **extra}, cwd=str(proj))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.decode('utf-8')) if r.stdout.strip() else None
claude_t = claude_home / 'projects' / 'p' / 'c1.jsonl'          # a fresh session: no file yet
payload = {'session_id': 'c1', 'transcript_path': str(claude_t), 'cwd': str(proj),
           'hook_event_name': 'SessionStart', 'source': 'startup'}
out = hook('claude_session_start.py', payload)
assert out['hookSpecificOutput']['hookEventName'] == 'SessionStart', out
assert out['hookSpecificOutput']['additionalContext'].startswith('[osk 세션 시작'), out
start = runs.read()['runs']['claude/start']
assert start['session'] == 'proj' and start['python'] == py and time.time() - start['at'] < 120, start
out = hook('claude_prompt_submit.py', {**payload, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'q'})
assert out is None or out['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit', out
out = hook('capture_stop.py', {**payload, 'hook_event_name': 'Stop'})
assert out is None or set(out) == {'systemMessage'}, out
rollout = rows(root.parent / 'rollout-x1.jsonl', {'type': 'session_meta', 'payload': {'id': 'x1'}})
hook('claude_session_start.py', {'session_id': 'x1', 'transcript_path': str(rollout), 'cwd': str(proj),
                                 'hook_event_name': 'SessionStart', 'source': 'startup'})
recorded = runs.read()['runs']
assert {'claude/start', 'claude/input', 'claude/stop', 'codex/start'} <= set(recorded), recorded
# Unreadable input still starts the session; the run is kept under an unknown host.
out = hook('claude_session_start.py', raw=b'{broken')
assert '훅 입력 판독 진단' in out['hookSpecificOutput']['additionalContext'], out
assert runs.read()['runs']['unknown/start']['session'] is None
out = hook('claude_prompt_submit.py', raw=b'{broken')
assert 'diagnostic' in out['hookSpecificOutput']['additionalContext'], out
out = hook('capture_stop.py', raw=b'{broken')
assert set(out) == {'systemMessage'} and 'capture diagnostic' in out['systemMessage'], out
assert {'unknown/input', 'unknown/stop'} <= set(runs.read()['runs'])
# A fork worker's own hooks neither answer nor record.
state = core.local_lock_path(runs.STATE)
before = state.read_bytes()
for name in ('claude_session_start.py', 'claude_prompt_submit.py', 'capture_stop.py'):
    assert hook(name, payload, OSK_GROWTH_WORKER='1') is None
assert state.read_bytes() == before
# overview(session=…) after the start is the delivery evidence doctor reads.
sys.path.insert(0, str(tested_hooks.parents[1]))
import mcp_server as M
M.overview()
assert runs.read()['overview'] == {}, 'overview without a session was recorded'
out = M.overview(session='proj')
assert 'session_scope' in out and not set(out) - {'clusters', 'open_cases', 'broken', 'nodes', 'engine_rev',
                                                  'engine_stale', 'rechecks', 'update', 'session_scope'}, out
assert runs.read()['overview']['proj'] >= runs.read()['runs']['claude/start']['at']
# A fork's own overview is not evidence that the user's session received the text.
with mock.patch.dict(os.environ, {'OSK_GROWTH_WORKER': '1'}):
    M.overview(session='fork-own')
    assert runs.record_run('claude', 'start', 'fork-own') is False
assert 'fork-own' not in runs.read()['overview'], runs.read()['overview']
''')

    def test_doctor_reads_registration_runs_delivery_and_versions_without_writing(self):
        self.check_case(r'''
# Claude: exec form for the start (this vault), quoted shell form for input (this vault),
# and another vault's Stop script.
rows(claude_home / 'settings.json', {'hooks': {
    'SessionStart': [{'matcher': 'startup', 'hooks': [
        {'type': 'command', 'command': py, 'args': [str(hooks_dir / 'claude_session_start.py')]}]}],
    'UserPromptSubmit': [{'hooks': [
        {'type': 'command', 'command': f'"{py}" "{hooks_dir / "claude_prompt_submit.py"}"'}]}],
    'Stop': [{'hooks': [
        {'type': 'command', 'command': f'"{py}" /elsewhere/_governance/_engine/scripts/hooks/capture_stop.py'}]}]}})
rows(home / '.claude.json', {'mcpServers': {'osk-system': {'type': 'stdio', 'command': py, 'args': [str(server)]}}})
# Codex: two hooks in hooks.json, only the first trusted; the MCP entry names a missing Python.
hooks_json = rows(codex_home / 'hooks.json', {'hooks': {
    'SessionStart': [{'matcher': 'startup|resume', 'hooks': [
        {'type': 'command', 'command': f'"{py}" "{hooks_dir / "claude_session_start.py"}"'}]}],
    'UserPromptSubmit': [{'hooks': [
        {'type': 'command', 'command': f'"{py}" "{hooks_dir / "claude_prompt_submit.py"}"'}]}]}})
gone = root.parent / 'gone' / 'python'
def codex_config(python):
    s = lambda v: json.dumps(str(v))
    (codex_home / 'config.toml').write_text(
        f'[mcp_servers.osk-system]\ncommand = {s(python)}\nargs = [{s(server)}]\n\n'
        f'[hooks.state.{s(str(hooks_json) + ":session_start:0:0")}]\ntrusted_hash = "sha256:{"0" * 64}"\n',
        encoding='utf-8')
codex_config(gone)
# The newest captured Claude conversation gives its version; Codex falls back to PATH.
native = rows(claude_home / 'projects' / 'p' / 'c1.jsonl', {'sessionId': 'c1', 'version': '2.1.200'},
              {'sessionId': 'c1', 'version': '2.1.999'})
integration.state_path('claude', 'c1').write_text(
    json.dumps({'harness': 'claude', 'transcript_path': str(native)}), encoding='utf-8')
fake_cli('claude', '9.9.9 (Claude Code)')
fake_cli('codex', 'codex-cli 0.1.0')
runs.record_run('claude', 'start', 'proj')
time.sleep(0.05)
runs.record_overview('proj')
runs.record_run(None, 'input', None)

def snapshot():
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for base in (root, home) for p in base.rglob('*')
            if p.is_file()}
def doctor(*args, **extra):
    before = snapshot()
    r = subprocess.run([py, '-m', 'osk.cli', 'doctor', *args], capture_output=True, timeout=120,
                       env={**os.environ, **extra})
    assert snapshot() == before, 'doctor wrote state or configuration'
    return r.returncode, r.stdout.decode('utf-8'), r.stderr.decode('utf-8', 'replace')
def items(rep, name):
    host = next(h for h in rep['hosts'] if h['harness'] == name)
    return {i['check']: i for i in host['items']}

code, text, err = doctor('--json')
rep = json.loads(text)
assert code == 1 and rep['ok'] is False and rep['counts']['fail'] == 1, (code, rep['counts'], err)
engine_checks = {i['check']: i for i in rep['engine']}
assert engine_checks['Python']['level'] == 'ok' and engine_checks['mcp 패키지']['level'] == 'ok', rep['engine']
unknown = engine_checks['알 수 없는 호스트']
assert unknown['level'] == 'info' and 'input' in unknown['detail'], unknown
c = items(rep, 'claude')
assert c['MCP']['level'] == 'ok' and 'osk-system' in c['MCP']['detail'], c['MCP']
assert c['훅 SessionStart']['level'] == 'ok' and '마지막 실행' in c['훅 SessionStart']['detail'], c
# Claude Code has no trust gate: a registered hook that never ran only needs a new session.
assert c['훅 UserPromptSubmit']['level'] == 'warn', c['훅 UserPromptSubmit']
assert '신뢰' not in c['훅 UserPromptSubmit']['detail'], c['훅 UserPromptSubmit']
assert c['훅 UserPromptSubmit']['fix'] == harness.get('claude').reload, c['훅 UserPromptSubmit']
assert c['훅 Stop']['level'] == 'warn' and '/elsewhere/' in c['훅 Stop']['detail'], c['훅 Stop']
assert c['전달']['level'] == 'ok' and 'session=proj' in c['전달']['detail'], c['전달']
assert c['판본']['level'] == 'warn' and '최근 대화 2.1.999' in c['판본']['detail'], c['판본']
assert c['fork']['level'] == 'info', c['fork']
x = items(rep, 'codex')
assert x['MCP']['level'] == 'fail' and str(gone) in x['MCP']['detail'], x['MCP']
assert 'codex mcp add osk-system' in x['MCP']['fix'], x['MCP']
assert x['훅 SessionStart']['level'] == 'warn' and '신뢰' not in x['훅 SessionStart']['detail'], x
assert x['훅 UserPromptSubmit']['level'] == 'warn' and '신뢰 기록도' in x['훅 UserPromptSubmit']['detail'], x
assert x['훅 Stop']['level'] == 'warn' and '등록되지 않았다' in x['훅 Stop']['detail'], x['훅 Stop']
assert '전달' not in x, 'Codex never started a session here'
assert x['판본']['level'] == 'ok' and 'PATH CLI 0.1.0' in x['판본']['detail'], x['판본']
# With the registered Python back, nothing fails; warnings do not change the exit code.
codex_config(py)
code, text, err = doctor()
assert code == 0 and 'Claude Code' in text and 'Codex' in text and '판정: 실패 0' in text, (code, text, err)
code, text, err = doctor('--harness', 'codex', '--json')
assert code == 0 and [h['harness'] for h in json.loads(text)['hosts']] == ['codex'], text
# A host without a trace on this device is skipped, not warned about.
(bin_dir / ('codex.cmd' if os.name == 'nt' else 'codex')).unlink()
# PATH is only the fake bin: a real Codex install on the test machine must not count as a trace.
code, text, err = doctor('--harness', 'codex', '--json', CODEX_HOME=str(root.parent / 'no-codex'),
                         PATH=str(bin_dir))
host = json.loads(text)['hosts'][0]
assert code == 0 and host['in_use'] is False and [i['level'] for i in host['items']] == ['info'], host
''')

    def test_doctor_probes_the_registered_python_puts_trust_first_and_quotes_paths(self):
        self.check_case(r'''
from osk import doctor
from osk.harness import base
claude, codex = harness.get('claude'), harness.get('codex')
def mcp_item(entry):
    rows(home / '.claude.json', {'mcpServers': {'osk-system': {'type': 'stdio', **entry}}})
    return doctor._mcp(claude, claude.registrations()[0])[0]
# The registered interpreter, not the one running doctor, must start the server.
bare = root.parent / 'bare'
subprocess.run([py, '-m', 'venv', '--without-pip', str(bare)], check=True, capture_output=True)
bare_py = str(bare / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
item = mcp_item({'command': bare_py, 'args': [str(server)]})
assert item['level'] == 'fail' and 'mcp' in item['detail'], item
assert item['fix'] == doctor._pip([bare_py]) and 'pip' in item['fix'], item
old = root.parent / 'old_python.py'
old.write_text("import sys\nprint('Python 3.10.9')\nsys.exit(3)\n", encoding='utf-8')
item = mcp_item({'command': py, 'args': [str(old), str(server)]})
assert item['level'] == 'fail' and 'Python 3.10.9' in item['detail'], item
assert item['fix'] == claude.mcp_command(doctor._python(), server), item
assert mcp_item({'command': py, 'args': [str(server)]})['level'] == 'ok'
# A run left by an earlier registration does not stand in for trust in the current one.
rows(codex_home / 'hooks.json', {'hooks': {'UserPromptSubmit': [{'hooks': [
    {'type': 'command', 'command': base.hook_line([py, hooks_dir / 'claude_prompt_submit.py'])}]}]}})
(codex_home / 'config.toml').write_text('', encoding='utf-8')
runs.record_run('codex', 'input', 'proj')
def hook_item():
    return doctor._hook(codex, 'input', codex.registrations()[1], runs.read()['runs'].get('codex/input'))
item = hook_item()
assert item['level'] == 'warn' and '신뢰 기록이 없다' in item['detail'], item
key = json.dumps(str(codex_home / 'hooks.json') + ':user_prompt_submit:0:0')
(codex_home / 'config.toml').write_text(f'[hooks.state.{key}]\ntrusted_hash = "sha256:{"0" * 64}"\n',
                                         encoding='utf-8')
assert hook_item()['level'] == 'ok', hook_item()
# Paths with spaces survive the shell that will read the command.
spaced = ['C:/My Vault/.venv/python.exe' if os.name == 'nt' else '/My Vault/.venv/bin/python',
          'C:/My Vault/_governance/_engine/mcp_server.py' if os.name == 'nt' else '/My Vault/_engine/mcp_server.py']
assert base.command_tokens({'command': base.hook_line(spaced)}) == spaced
assert claude.mcp_command(*spaced) == core.shell_join(
    ['claude', 'mcp', 'add', '--scope', 'user', 'osk-system', '--', *spaced])
assert codex.mcp_command(*spaced) == core.shell_join(['codex', 'mcp', 'add', 'osk-system', '--', *spaced])
echo = [py, '-c', 'import json,sys;print(json.dumps(sys.argv[1:]))', 'a b', "it's", '--x=y', spaced[1]]
line = core.shell_join(echo)
shell = (['powershell', '-NoProfile', '-NonInteractive', '-Command', line] if os.name == 'nt'
         else ['sh', '-c', line])
r = subprocess.run(shell, capture_output=True, text=True, encoding='utf-8', timeout=60)
assert r.returncode == 0 and json.loads(r.stdout) == echo[3:], (line, r.stdout, r.stderr)
''')

    def test_a_second_registration_of_one_event_is_handled_once(self):
        self.check_case(r'''
import concurrent.futures
from osk import doctor
from osk._portalock import lock_exclusive, unlock
proj = root.parent / 'proj'
proj.mkdir()
transcript = rows(claude_home / 'projects' / 'p' / 'd1.jsonl', {'sessionId': 'd1', 'type': 'user'})
payload = {'session_id': 'd1', 'transcript_path': str(transcript), 'cwd': str(proj),
           'hook_event_name': 'SessionStart', 'source': 'startup'}
def start():
    r = subprocess.run([py, str(tested_hooks / 'claude_session_start.py')], cwd=str(proj),
                       input=json.dumps(payload).encode('utf-8'), capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
# Two registrations of one event fire together: one answers, the other stays silent.
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    outs = list(pool.map(lambda _: start(), range(2)))
assert sorted(bool(o) for o in outs) == [False, True], outs
assert 'claude/start' in runs.duplicates(), runs.duplicates()
# The next real event is not the same call: the transcript has grown.
with transcript.open('a', encoding='utf-8') as f:
    f.write(json.dumps({'sessionId': 'd1', 'type': 'assistant'}) + '\n')
assert start(), 'the next event was taken for a duplicate'
# Without a transcript, only calls that arrive together count as one.
env = {'session_id': 's', 'hook_event_name': 'UserPromptSubmit', 'prompt': 'q'}
assert runs.first_call('codex', 'input', env) is True
assert runs.first_call('codex', 'input', env) is False
assert runs.first_call('claude', 'input', env) is True, 'another host'
assert runs.first_call('codex', 'stop', env) is True, 'another event'
with mock.patch.object(runs.time, 'time', return_value=time.time() + runs.BLIND + 1):
    assert runs.first_call('codex', 'input', env) is True, 'a later turn with the same prompt'
# When the claim cannot be judged, the call is handled.
with open(core.local_lock_path(runs.LOCK), 'w') as held:
    lock_exclusive(held)
    assert runs.first_call('codex', 'input', env) is True
    unlock(held)
# doctor names the doubled registration and the observed duplicate call.
script = hooks_dir / 'claude_session_start.py'
rows(claude_home / 'settings.json', {'hooks': {'SessionStart': [
    {'hooks': [{'type': 'command', 'command': f'"{py}" "{script}"'}]},
    {'hooks': [{'type': 'command', 'command': py, 'args': [str(script)]}]}]}})
checks = {i['check']: i for i in doctor.report('claude')['hosts'][0]['items']}
assert checks['훅 SessionStart']['level'] == 'warn', checks['훅 SessionStart']
assert '2곳' in checks['훅 SessionStart']['detail'] and checks['훅 SessionStart']['fix'] == doctor._ONE
assert checks['중복 호출 SessionStart']['level'] == 'warn', checks
''')

    def test_recording_waits_briefly_and_keeps_recent_sessions(self):
        self.check_case(r'''
from osk._portalock import lock_exclusive, unlock
with open(core.local_lock_path(runs.LOCK), 'w') as held:
    lock_exclusive(held)
    t = time.monotonic()
    assert runs.record_run('claude', 'start', 'k') is False, 'recorded under a held lock'
    assert time.monotonic() - t < 5
    unlock(held)
assert runs.record_run('claude', 'start', 'k') is True and runs.read()['runs']['claude/start']['session'] == 'k'
for n in range(runs.KEEP + 5):
    runs.record_overview(f's{n}')
seen = runs.read()['overview']
assert len(seen) == runs.KEEP and 's0' not in seen and f's{runs.KEEP + 4}' in seen, sorted(seen)
core.local_lock_path(runs.STATE).write_text('{broken', encoding='utf-8')
assert runs.read() == {'runs': {}, 'overview': {}}
assert runs.record_overview('again') and runs.read()['overview'].keys() == {'again'}
''')


if __name__ == '__main__':
    unittest.main()
