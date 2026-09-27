"""Harness adapters keep the v4 judgment; hooks leave run records; doctor reads without writing.

Each case runs in its own process because `core.ROOT` is fixed at import. Every host
home (`HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`) is a temporary folder,
and a fake `claude`/`codex` comes first on `PATH`, so no case reads the developer's
own configuration or starts a real host CLI.
"""
import os
from pathlib import Path
import shutil
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
        env = {k: v for k, v in os.environ.items()
               if k not in ('CODEX_THREAD_ID', 'CODEX_VERSION', 'OSK_HARNESS', 'OSK_GROWTH_WORKER',
                            'KIRO_SESSION_ID', 'ANTIGRAVITY_CONVERSATION_ID')}
        env.update(OSK_VAULT_ROOT=str(td / 'vault'), PYTHONPATH=str(ENGINE), PYTHONUTF8='1',
                   OSK_UPDATE_CHECK='0', HOME=str(home), USERPROFILE=str(home),
                   CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'),
                   OSK_TEST_BIN=str(bin_dir), PATH=_case_path(bin_dir))
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

assert harness.NAMES == ('claude', 'codex', 'kiro', 'antigravity') and harness.fork_names() == ('claude', 'codex')
try:
    integration._identity('gemini', 'x')
    raise AssertionError('unknown harness accepted')
except ValueError as exc:
    assert str(exc) == 'explicit claude/codex/kiro/antigravity harness and actual conversation_id required', exc
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
for adapter in (harness.get('claude'), harness.get('codex'), harness.FALLBACK):
    assert adapter.hook_output('start', 't') == {
        'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 't'}}
    assert adapter.hook_output('input', 't', 'm') == {
        'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 't'},
        'systemMessage': 'm'}
    assert adapter.hook_notice('stop', 'm') == {'systemMessage': 'm'}
# Kiro takes the context as plain text and has no user-only channel.
kiro = harness.get('kiro')
assert kiro.hook_output('input', 't', 'm') == 't' and kiro.hook_notice('stop', 'm') == ''
# Antigravity takes JSON only: context is an injected step, and a silent call is `{}`.
ag = harness.get('antigravity')
assert json.loads(ag.hook_output('start', 't', 'm')) == {'injectSteps': [{'ephemeralMessage': 't'}]}
assert ag.hook_output('input', '') == ag.hook_output('stop', 't') == ag.hook_notice('stop', 'm') == ag.silence == '{}'
assert ag.fires('input', {'invocationNum': 0}) and not ag.fires('input', {'invocationNum': 2})
assert ag.fires('start', {}) and ag.fires('stop', {'invocationNum': 3})
seen = harness.normalize({'conversationId': 'a1', 'workspacePaths': ['c:/w'], 'transcriptPath': '/t.jsonl'})
assert (seen['session_id'], seen['cwd'], seen['transcript_path']) == ('a1', 'c:/w', '/t.jsonl'), seen
assert harness.normalize({'session_id': 's', 'cwd': 'x'}) == {'session_id': 's', 'cwd': 'x'}
if os.name == 'nt':   # cmd /c cannot take a quoted path, and unquoted `&` splits the command
    for odd in ('C:/Program Files/py.exe', 'C:/R&D/py.exe'):
        try:
            ag.hook_command([odd, 'C:/v/hook.py'])
            raise AssertionError(f'{odd} was accepted for cmd /c')
        except ValueError as exc:
            assert 'Antigravity' in str(exc), exc
assert ag.hook_command(['C:/py/python.exe', 'C:/v/hook.py']) == 'C:/py/python.exe C:/v/hook.py'
if os.name == 'nt':   # what cmd reads unquoted stays unquoted — a quote would not survive `cmd /c`
    assert ag.hook_command(["C:/O'Brien+1/py.exe", 'C:/v/hook.py']) == "C:/O'Brien+1/py.exe C:/v/hook.py"
claude, codex = harness.get('claude'), harness.get('codex')
assert claude.parse_version('2.1.280 (Claude Code)') == '2.1.280'
assert codex.parse_version('codex-cli 0.155.0-alpha.16') == '0.155.0-alpha.16'
assert claude.parse_version('codex-cli 1.0.0') is None and codex.parse_version('2.1.280 (Claude Code)') is None
assert kiro.parse_version('1.1.70\n8ce1870416c7dc7e51fffb01765d93ef7ad55102\nx64') == '1.1.70'
assert kiro.parse_version('2.1.280 (Claude Code)') is None
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
# Blocks a hook folded for its host's limit come back by name (osk.hook_text).
out = M.overview(session='proj', include=['tidy', 'organization', 'recovery'])
assert set(out['included']) == {'tidy', 'organization', 'recovery'}, out
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

    def test_a_hook_line_runs_in_the_shell_its_host_uses(self):
        self.check_case(r'''
import shutil
from osk import doctor
from osk.harness import base
# A vault under C:/R&D/: an unquoted `&` ends the command in cmd, bash and PowerShell alike.
# Each host gets a line its shell reads, or refuses the path before setup writes anything —
# only Antigravity, which takes no quotes on Windows.
def line_of(a, argv):
    try:
        return a.hook_command(argv)
    except ValueError:
        assert os.name == 'nt' and a.name == 'antigravity', a.name
lab = ['C:/R&D/osk/.venv/Scripts/python.exe', 'C:/R&D/osk/_governance/_engine/scripts/hooks/claude_session_start.py']
for a in harness.ADAPTERS:
    line = line_of(a, lab)
    assert line is None or base.command_tokens({'command': line}) == lab, (a.name, line)  # tokens[0]: the Python
# Run each host's line the way that host runs it (Windows): Claude Code in Git Bash, Codex in
# its session's PowerShell, Kiro through Node's `spawn(command, {shell: true})`.
amp = root.parent / 'R&D'
subprocess.run([py, '-m', 'venv', '--without-pip', str(amp / 'venv')], check=True, capture_output=True)
probe = amp / 'probe.py'
probe.write_text('import json, sys; print(json.dumps(sys.argv))', encoding='utf-8')
argv = [(amp / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')).as_posix(), probe.as_posix()]
if os.name == 'nt':    # Git Bash is `<Git>/bin/bash.exe`; `git` may be `<Git>/cmd` or `<Git>/mingw64/bin`
    bash = next(p / 'bin' / 'bash.exe' for p in Path(shutil.which('git')).resolve().parents
                if (p / 'bin' / 'bash.exe').is_file())
for a in harness.ADAPTERS:
    line = line_of(a, argv)
    if line is None:
        continue
    run = (['sh', '-c', line] if os.name != 'nt' else
           f'cmd.exe /d /s /c "{line}"' if a.hook_shell == 'cmd' else
           ['powershell', '-NoProfile', '-Command', line] if a.hook_shell == 'powershell' else
           [str(bash), '-c', line])
    r = subprocess.run(run, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout) == argv[1:], (a.name, line, r.stdout, r.stderr)
# Without Git Bash, Claude Code runs the line in PowerShell, where a quoted command is only an
# expression: what both shells read bare stays bare, as before (#115 review, `C:/work@home/...`).
if os.name == 'nt':    # relative to the case folder: the temp root itself may hold a space
    plain = 'w@h+1#2=3^4%5!6~7]8'
    shutil.copytree(amp / 'venv', root.parent / plain / 'venv')
    shutil.copy(probe, root.parent / plain / 'probe.py')
    argv = [f'./{plain}/venv/Scripts/python.exe', f'./{plain}/probe.py']
    assert harness.get('claude').hook_command(argv) == harness.get('codex').hook_command(argv) == ' '.join(argv)
    for run in (['powershell', '-NoProfile', '-Command'], [str(bash), '-c']):
        r = subprocess.run(run + [' '.join(argv)], cwd=root.parent, capture_output=True, text=True,
                           encoding='utf-8', timeout=60)
        assert r.returncode == 0 and json.loads(r.stdout) == argv[1:], (run[0], r.stdout, r.stderr)
# What a shell still expands inside double quotes cannot be quoted: setup refuses it, doctor says why.
if os.name == 'nt':
    for a, path in ((harness.get('kiro'), 'C:/R&D/100%/python.exe'), (harness.get('claude'), 'C:/R&D/$x/python.exe'),
                    (harness.get('codex'), 'C:/R&D/$x/python.exe')):
        try:
            a.hook_command([path, lab[1]])
            raise AssertionError((a.name, path))
        except ValueError as e:
            assert path in str(e) and '--harness' in str(e), e
        with mock.patch.object(doctor, '_python', lambda: Path(path)):
            item = doctor._hook(a, 'start', [], None)
        assert path in item['fix'], item
''')

    def test_a_second_registration_of_one_event_is_handled_once(self):
        self.check_case(r'''
import concurrent.futures, itertools
from osk import doctor
from osk._portalock import lock_exclusive, unlock
proj = root.parent / 'proj'
proj.mkdir()
transcript = rows(claude_home / 'projects' / 'p' / 'd1.jsonl', {'sessionId': 'd1', 'type': 'user'})
payload = {'session_id': 'd1', 'transcript_path': str(transcript), 'cwd': str(proj),
           'hook_event_name': 'SessionStart', 'source': 'startup'}
# These hooks wait up to 30 s for the lock, so both calls are judged and the claim decides, not the
# runner's speed. With the product's short wait a slow runner leaves the second call unjudged, and
# an unjudged call answers by design (the held lock below).
launch = 'import runpy, sys; from osk.harness import runs; runs.WAIT = 30; runpy.run_path(sys.argv[1], run_name="__main__")'
def start():
    r = subprocess.run([py, '-c', launch, str(tested_hooks / 'claude_session_start.py')], cwd=str(proj),
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
# A claim made while this call waited for the lock is the earlier call's, not a future one.
real, again = runs._update, {**env, 'prompt': 'r'}
def claimed_while_waiting(change, *args):
    with mock.patch.object(runs, '_update', real):
        assert runs.first_call('codex', 'input', again) is True
    return real(change, *args)
with mock.patch.object(runs.time, 'time', side_effect=itertools.count(time.time(), 0.01).__next__), \
        mock.patch.object(runs, '_update', claimed_while_waiting):
    assert runs.first_call('codex', 'input', again) is False, 'both calls were handled'
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
# Matchers with no start cause in common run one group per start: not a duplicate (PR #105 review).
def start_groups(*matchers):
    rows(claude_home / 'settings.json', {'hooks': {'SessionStart': [
        {'matcher': m, 'hooks': [{'type': 'command', 'command': f'"{py}" "{script}"'}]} for m in matchers]}})
    return {i['check']: i for i in doctor.report('claude')['hosts'][0]['items']}['훅 SessionStart']
item = start_groups('startup', 'resume|clear|compact')
assert item['level'] == 'ok' and '겹치지 않는다' in item['detail'], item
item = start_groups('startup|resume', 'resume')
assert item['level'] == 'warn' and '— resume에 함께 불린다' in item['detail'], item
codex = harness.get('codex')
assert codex.fires_on('input', 'anything') == frozenset({'*'}), 'events without causes always run'
assert codex.fires_on('start', '*') == codex.fires_on('start', None) == frozenset(codex.sources['start'])
assert codex.fires_on('start', '(') == frozenset(), 'a matcher that is not a regex is compared by name'
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

    def test_kiro_hooks_answer_in_plain_text_and_capture_its_own_transcript(self):
        self.check_case(r'''
from osk import doctor, write
from osk.harness import base
proj = root.parent / 'proj'
proj.mkdir()
write.bind_session('proj', 'W1')
sid = 'sess_0f8a2c4e-1111-4222-8333-944455556666'
transcript = home / '.kiro' / 'sessions' / 'ws1' / sid / 'messages.jsonl'
def row(n, payload):
    return {'id': f'r{n}', 'timestamp': f'2026-09-27T00:00:{n:02d}Z', 'payload': payload}
rows(transcript,
     row(1, {'type': 'user', 'content': '첫 질문', 'images': [], 'documents': []}),
     row(2, {'type': 'turn_start', 'executionId': 'e1'}),
     row(3, {'type': 'assistant', 'operationType': 'Reasoning', 'content': 'internal thought', 'executionId': 'e1'}),
     row(4, {'type': 'assistant', 'operationType': 'Say', 'content': '읽어 보겠습니다.', 'executionId': 'e1'}),
     row(5, {'type': 'tool_call', 'toolCallId': 't1', 'toolName': 'read_file', 'args': {'path': 'a.txt'},
             'executionId': 'e1'}),
     row(6, {'type': 'tool_result', 'toolCallId': 't1', 'content': 'FILE BODY', 'executionId': 'e1'}),
     row(7, {'type': 'assistant', 'operationType': 'Say', 'content': '답은 42입니다.', 'executionId': 'e1'}),
     row(8, {'type': 'turn_end', 'stopReason': 'end_turn', 'executionId': 'e1'}),
     row(9, {'type': 'user', 'content': '두 번째', 'images': [], 'documents': []}),
     row(10, {'type': 'turn_start', 'executionId': 'e2'}),
     row(11, {'type': 'turn_end', 'stopReason': 'cancelled', 'executionId': 'e2'}),
     # A turn that never ended is superseded by the next one; a turn with no user row was
     # started by something else (an agent hook) and is marked, not given a made-up input.
     row(12, {'type': 'user', 'content': '세 번째', 'images': [], 'documents': []}),
     row(13, {'type': 'turn_start', 'executionId': 'e3'}),
     row(14, {'type': 'assistant', 'operationType': 'Say', 'content': '하다 만 답', 'executionId': 'e3'}),
     row(15, {'type': 'user', 'content': '네 번째', 'images': [], 'documents': []}),
     row(16, {'type': 'turn_start', 'executionId': 'e4'}),
     row(17, {'type': 'assistant', 'operationType': 'Say', 'content': '넷째 답', 'executionId': 'e4'}),
     row(18, {'type': 'turn_end', 'stopReason': 'end_turn', 'executionId': 'e4'}),
     row(19, {'type': 'turn_start', 'executionId': 'e5'}),
     row(20, {'type': 'assistant', 'operationType': 'Say', 'content': '훅이 시킨 일', 'executionId': 'e5'}),
     row(21, {'type': 'turn_end', 'stopReason': 'end_turn', 'executionId': 'e5'}),
     # The next prompt, before its turn starts, waits as the tail.
     row(22, {'type': 'user', 'content': '다섯 번째', 'images': [], 'documents': []}))
def hook(name, payload, **extra):
    r = subprocess.run([py, str(tested_hooks / name)], input=json.dumps(payload).encode('utf-8'),
                       capture_output=True, timeout=120, cwd=str(proj),
                       env={**os.environ, 'KIRO_SESSION_ID': sid, **extra})
    assert r.returncode == 0, r.stderr
    return r.stdout.decode('utf-8')
given = {'session_id': sid, 'cwd': str(proj)}
# Kiro puts the start and input hooks' stdout into the context as it is — plain text, not JSON.
out = hook('claude_session_start.py', {**given, 'hook_event_name': 'SessionStart'})
assert out.startswith('[osk 세션 시작 — session="proj"]'), out
assert 'Kiro has no subscription fork' in out, out
out = hook('claude_prompt_submit.py', {**given, 'hook_event_name': 'UserPromptSubmit', 'prompt': '두 번째'})
assert not out.lstrip().startswith('{'), out
# Kiro shows the stop hook's output nowhere: it prints nothing, and it captures the transcript
# found by the conversation ID, since Kiro gives no transcript path.
assert hook('capture_stop.py', {**given, 'hook_event_name': 'Stop'}) == ''
st = integration.status('kiro', sid)
assert (st['captured_rounds'], st['aborted_rounds'], st['interrupted_rounds']) == (5, 1, 1), st
assert st['capture_pending'] and not st['capture_error'], st
assert Path(st['transcript_path']) == transcript.resolve(), st
text = (root / st['pending_refs'][0].split('#')[0]).read_text(encoding='utf-8')
assert text.count('<!-- osk-capture: dialogue-v1 ') == 5, text
assert '첫 질문' in text and '읽어 보겠습니다.' in text and '답은 42입니다.' in text, text
assert '"type": "superseded"' in text and '하다 만 답' in text and '넷째 답' in text, text
assert '"native_trigger": "inputless"' in text and '다섯 번째' not in text, text
# A transcript in another conversation's folder is not read.
other = integration.capture('kiro', 'sess_other', str(transcript), 'proj')
assert 'does not match' in (other['capture_error'] or ''), other
# When capture fails, Kiro's stop hook still prints nothing: it has no place to show a notice.
assert hook('capture_stop.py', {'session_id': 'sess_missing', 'cwd': str(proj), 'hook_event_name': 'Stop'},
            KIRO_SESSION_ID='sess_missing') == ''
assert integration.status('kiro', 'sess_missing')['capture_error'], 'the failure is kept in the state'
# Reasoning is outside the capture scope; tool payloads are references (Bylaws §2 2).
assert 'internal thought' not in text and 'FILE BODY' not in text, text
assert '"tool_evidence_ref"' in text and '"read_file"' in text, text
assert '"stopReason": "cancelled"' in text, text
assert {'kiro/start', 'kiro/input', 'kiro/stop'} <= set(runs.read()['runs']), runs.read()['runs']
# Another host's hook started from a Kiro terminal keeps its own identity and envelope.
claude_t = rows(claude_home / 'projects' / 'p' / 'c9.jsonl', {'type': 'user', 'sessionId': 'c9'})
out = hook('claude_session_start.py', {'session_id': 'c9', 'transcript_path': str(claude_t),
                                       'cwd': str(proj), 'hook_event_name': 'SessionStart', 'source': 'startup'})
assert json.loads(out)['hookSpecificOutput']['hookEventName'] == 'SessionStart', out
assert 'claude/start' in runs.read()['runs'], runs.read()['runs']
# doctor reads Kiro's own hook file format, its MCP settings file and `kiro --version`.
kiro = harness.get('kiro')
table = {}
for event in harness.SCRIPTS:
    command = base.hook_line([py, hooks_dir / harness.SCRIPTS[event]])
    table.setdefault(kiro.events[event], []).append(kiro.hook_group(event, command))
rows(home / '.kiro' / 'hooks' / 'osk-system.json', kiro.hook_content({}, table))
rows(home / '.kiro' / 'settings' / 'mcp.json', kiro.mcp_write({}, py, str(server)))
fake_cli('kiro', '1.1.70')
rep = doctor.report('kiro')
got = {i['check']: i for i in rep['hosts'][0]['items']}
assert got['MCP']['level'] == 'ok', got
assert all(got[f'훅 {n}']['level'] == 'ok' for n in ('SessionStart', 'UserPromptSubmit', 'Stop')), got
assert got['판본']['level'] == 'ok' and '1.1.70' in got['판본']['detail'], got
assert 'fork' not in got, got
''')

    def test_antigravity_hooks_answer_in_json_and_capture_its_own_transcript(self):
        self.check_case(r'''
from osk import doctor, write
from osk.harness import base
proj = root.parent / 'proj'
proj.mkdir()
write.bind_session('proj', 'W1')
sid = 'a7c1d2e3-1111-4222-8333-944455556666'
transcript = home / '.gemini' / 'antigravity' / 'brain' / sid / '.system_generated' / 'logs' / 'transcript_full.jsonl'
def step(n, typ, source='MODEL', **extra):
    return {'step_index': n, 'source': source, 'type': typ, 'status': 'DONE',
            'created_at': f'2026-09-27T00:00:{n:02d}Z', **extra}
def ask(text):
    return ('<USER_REQUEST>\n' + text + '\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n'
            'The current local time is: 2026-09-27T16:14:05+09:00.\n</ADDITIONAL_METADATA>')
tricky = ('    들여쓴 첫 줄\n이 코드의 "</USER_REQUEST>" 다음 문장도 보존한다.\n</USER_REQUEST>\n'
          '줄 머리의 닫는 태그 뒤도 사용자의 말이다.')
rows(transcript,
     step(0, 'USER_INPUT', 'USER_EXPLICIT', content=ask('첫 질문')),
     step(1, 'EPHEMERAL_MESSAGE', 'SYSTEM_SDK', content='[osk 세션 시작 — 주입된 문맥]'),
     step(2, 'PLANNER_RESPONSE', content='읽어 보겠습니다.', thinking='internal thought',
          tool_calls=[{'name': 'view_file', 'args': {'AbsolutePath': 'a.txt'}}]),
     step(3, 'GENERIC', content='FILE BODY'),
     step(4, 'PLANNER_RESPONSE', content='답은 42입니다.'),
     # A turn that ended in an error before any reply is failed, not completed.
     step(5, 'USER_INPUT', 'USER_EXPLICIT', content=ask('두 번째')),
     step(6, 'PLANNER_RESPONSE', tool_calls=[{'name': 'run_command', 'args': {'CommandLine': 'ls'}}]),
     step(7, 'ERROR_MESSAGE', 'SYSTEM', content='model error'),
     # The user's words stay byte for byte: indentation, and a closing tag they typed.
     step(8, 'USER_INPUT', 'USER_EXPLICIT', content=ask(tricky) + '\n<USER_SETTINGS_CHANGE>\n'
          'The user changed setting `Model Selection`.\n</USER_SETTINGS_CHANGE>'),
     step(9, 'CHECKPOINT', 'SYSTEM', content='checkpoint'),
     step(10, 'PLANNER_RESPONSE', content='셋째 답'),
     # The next prompt, before its reply, waits as the tail.
     step(11, 'USER_INPUT', 'USER_EXPLICIT', content=ask('네 번째')))
hooks_home = home / '.gemini' / 'config'
hooks_home.mkdir(parents=True, exist_ok=True)
def hook(name, payload, **extra):
    # Antigravity runs a hook in the folder of its hooks.json, not in the workspace.
    r = subprocess.run([py, str(tested_hooks / name)], input=json.dumps(payload).encode('utf-8'),
                       capture_output=True, timeout=120, cwd=str(hooks_home),
                       env={**os.environ, 'ANTIGRAVITY_CONVERSATION_ID': sid, **extra})
    assert r.returncode == 0, r.stderr
    return r.stdout.decode('utf-8')
given = {'conversationId': sid, 'workspacePaths': [str(proj)], 'transcriptPath': str(transcript),
         'artifactDirectoryPath': str(transcript.parents[2]), 'modelName': 'gemini-3.8-flash-high'}
# The start hook injects the context as a step; the session key comes from the workspace.
out = json.loads(hook('claude_session_start.py', given))
text = out['injectSteps'][0]['ephemeralMessage']
assert text.startswith('[osk 세션 시작 — session="proj"]'), text
assert 'Antigravity has no subscription fork' in text, text
# PreInvocation runs before every model call: only the first after an input is osk's input.
before = integration.status('antigravity', sid)['prompt_count']
assert hook('claude_prompt_submit.py', {**given, 'invocationNum': 3, 'initialNumSteps': 11}) == '{}'
assert integration.status('antigravity', sid)['prompt_count'] == before, 'a later model call ticked the clock'
out = hook('claude_prompt_submit.py', {**given, 'invocationNum': 0, 'initialNumSteps': 11})
assert out == '{}' or 'injectSteps' in json.loads(out), out
assert integration.status('antigravity', sid)['prompt_count'] == before + 1
# Stop answers `{}` (no decision) and captures the transcript it was given.
assert hook('capture_stop.py', {**given, 'executionNum': 0,
            'terminationReason': 'NO_TOOL_CALL', 'fullyIdle': True, 'error': ''}) == '{}'
st = integration.status('antigravity', sid)
assert (st['captured_rounds'], st['failed_rounds']) == (3, 1), st
assert st['capture_pending'] and not st['capture_error'], st
assert Path(st['transcript_path']) == transcript.resolve(), st
raw_text = (root / st['pending_refs'][0].split('#')[0]).read_text(encoding='utf-8')
assert raw_text.count('<!-- osk-capture: dialogue-v1 ') == 3, raw_text
assert '첫 질문' in raw_text and '읽어 보겠습니다.' in raw_text and '답은 42입니다.' in raw_text, raw_text
assert '셋째 답' in raw_text and '"type": "superseded"' in raw_text and '네 번째' not in raw_text, raw_text
assert '### user\n\n' + tricky + '\n' in raw_text, raw_text
# Hook context, thinking, tool payloads and the system metadata are not the dialogue.
for outside in ('주입된 문맥', 'internal thought', 'FILE BODY', 'ADDITIONAL_METADATA', 'checkpoint',
                'USER_SETTINGS_CHANGE', 'Model Selection'):
    assert outside not in raw_text, (outside, raw_text)
# An input of a shape the parser does not know is kept whole rather than cut.
from osk import transcripts
for odd in ('no wrapper', '<USER_REQUEST>\nopen only', '<USER_REQUEST>\nx\n</USER_REQUEST>\ntrailing'):
    assert transcripts._ag_user(odd) == odd, odd
assert '"tool_evidence_ref"' in raw_text and '"view_file"' in raw_text, raw_text
# A transcript in another conversation's folder is not read.
other = integration.capture('antigravity', 'b8d2e3f4-0000-4000-8000-000000000000', str(transcript), 'proj')
assert 'does not match' in (other['capture_error'] or ''), other
# When capture fails, Stop still answers JSON and lets the agent stop.
assert hook('capture_stop.py', {**given, 'conversationId': 'c9e3f4a5-0000-4000-8000-000000000000',
                                'transcriptPath': str(root.parent / 'missing.jsonl')},
            ANTIGRAVITY_CONVERSATION_ID='c9e3f4a5-0000-4000-8000-000000000000') == '{}'
assert {'antigravity/start', 'antigravity/input', 'antigravity/stop'} <= set(runs.read()['runs']), runs.read()['runs']
# doctor reads the named hook in ~/.gemini/config/hooks.json and the global MCP file.
ag = harness.get('antigravity')
table = {}
for event in harness.SCRIPTS:
    command = ag.hook_command([py, hooks_dir / harness.SCRIPTS[event]])
    table.setdefault(ag.events[event], []).append(ag.hook_group(event, command))
rows(hooks_home / 'hooks.json', ag.hook_content({}, table))
rows(hooks_home / 'mcp_config.json', ag.mcp_write({}, py, str(server)))
rep = doctor.report('antigravity')
got = {i['check']: i for i in rep['hosts'][0]['items']}
assert got['MCP']['level'] == 'ok', got
assert all(got[f'훅 {n}']['level'] == 'ok' for n in ('SessionStart', 'PreInvocation', 'Stop')), got
assert got['판본']['level'] == 'info', got
assert 'fork' not in got, got
''')


if __name__ == '__main__':
    unittest.main()
