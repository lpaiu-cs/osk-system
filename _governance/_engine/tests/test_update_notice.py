"""A new release is announced, never applied: tag-only checks, device-local notices.

Each case runs in its own process because `core.ROOT` is fixed at import. The
canonical repository is a local Git repository with tags, so no case touches the
network. The regression runner disables automatic checks for every other suite
(`OSK_UPDATE_CHECK=0`); these cases remove that switch unless they test it.
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
from osk import core, update, update_check, validate
root = core.ROOT
validate.make_mini_vault(root)
def git(*a, cwd):
    subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True)
git('init', '-q', '-b', 'main', str(root), cwd=root.parent)
up = root.parent / 'upstream'
git('init', '-q', '-b', 'main', str(up), cwd=root.parent)
git('-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'commit.gpgsign=false',
    'commit', '-q', '--allow-empty', '-m', 'release', cwd=up)
# Numeric order (v4.10.0 > v4.9.0); a pre-release, a non-version tag and a branch
# that looks like a version are not releases.
for tag in ('v4.0.0', 'v4.1.0', 'v4.9.0', 'v4.10.0', 'v4.11.0-rc.1', 'latest'):
    git('tag', tag, cwd=up)
git('branch', 'v9.9.9', cwd=up)
config = root / '.osk/config.json'
config.parent.mkdir()
def configure(**upstream):
    config.write_text(json.dumps({'upstream': {'source': 'git', 'url': str(up), 'pin': None,
                                               **upstream}}), encoding='utf-8')
configure()
def done(version, fill):
    core.ledger_append(update.UPDATE_JOURNAL, {'kind': 'done', 'version': version,
                                               'attest': 'sha256:' + fill * 64})
state_path = core.local_lock_path(update_check.STATE)
def cached(**over):
    state = {'at': time.time(), 'url': str(up), 'latest': 'v4.10.0', 'error': None, **over}
    state_path.write_text(json.dumps(state), encoding='utf-8')
    return state
'''


def _run(script: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        env = {k: v for k, v in os.environ.items() if k != 'OSK_UPDATE_CHECK'}
        env.update(OSK_VAULT_ROOT=str(Path(td) / 'vault'), PYTHONPATH=str(ENGINE),
                   PYTHONUTF8='1')
        return subprocess.run([sys.executable, '-c', script], env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=180)


class UpdateNoticeTests(unittest.TestCase):
    def check_case(self, body: str) -> None:
        result = _run(SETUP + body)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_check_notices_once_a_day_and_clears_when_the_journal_catches_up(self):
        self.check_case(r'''
done('v4.0.0', '0')
assert update.current_version() == 'v4.0.0'
# The first surface asks nothing of the network itself: it starts a detached check.
assert update_check.available() is None
proc = update_check.ensure_fresh()
assert proc is not None and proc.args[-1] == 'osk.update_check', proc
assert proc.wait(timeout=90) == 0
state = json.loads(state_path.read_text(encoding='utf-8'))
assert state['latest'] == 'v4.10.0' and state['error'] is None and state['url'] == str(up), state
assert update_check.ensure_fresh() is None, 'a fresh check was started again'
# Once a day: the hook (user screen) and the surface share one marker.
command = core.cli_command('update', '--to', 'v4.10.0', '--apply')
agent, user = update_check.session_notice()
assert 'v4.10.0' in user and 'v4.0.0' in user and 'osk 업데이트해 줘' in user, user
assert agent.startswith('[osk 새 릴리스 — v4.10.0 · 이 vault v4.0.0]'), agent
assert command in agent, agent
assert update_check.session_notice() == ('', '')
seen = update_check.surface()
assert seen['latest'] == 'v4.10.0' and seen['current'] == 'v4.0.0', seen
assert 'notify' not in seen and command in seen['how'], seen
later = time.time() + update_check.NOTICE_EVERY + 1
with mock.patch.object(update_check.time, 'time', return_value=later):
    assert 'notify' in update_check.surface()
    assert update_check.session_notice() == ('', ''), 'announced twice'
# The command the agent is given runs from any folder in the host's own shell,
# without PYTHONPATH or OSK_VAULT_ROOT in its environment.
bare = {k: v for k, v in os.environ.items() if k not in ('OSK_VAULT_ROOT', 'PYTHONPATH')}
probe = core.cli_command('update', '--check')
shell = (['powershell', '-NoProfile', '-NonInteractive', '-Command', probe] if os.name == 'nt'
         else ['sh', '-c', probe])
r = subprocess.run(shell, capture_output=True, text=True, encoding='utf-8', errors='replace',
                   cwd=str(root.parent), env=bare, timeout=90)
assert r.returncode == 0 and json.loads(r.stdout)['latest'] == 'v4.10.0', (r.returncode, r.stdout, r.stderr)
# `status` shows the comparison; `--check` asks again and is exclusive.
r = subprocess.run([sys.executable, '-m', 'osk.cli', 'status'], capture_output=True,
                   text=True, encoding='utf-8')
assert r.returncode == 0, r.stderr
st = json.loads(r.stdout)['update']
assert st['latest'] == 'v4.10.0' and st['available'] and 'auto_off' not in st, st
assert st['checked_at'] and st['checked_at'].endswith('(KST)'), st
r = subprocess.run([sys.executable, '-m', 'osk.update', '--check'], capture_output=True,
                   text=True, encoding='utf-8')
assert r.returncode == 0, r.stderr
rep = json.loads(r.stdout)
assert (rep['latest'], rep['current'], rep['available']) == ('v4.10.0', 'v4.0.0', True), rep
r = subprocess.run([sys.executable, '-m', 'osk.update', '--check', '--apply'],
                   capture_output=True, text=True, encoding='utf-8')
assert r.returncode != 0 and '--check' in r.stderr, (r.returncode, r.stderr)
# Another device updated and the synced journal arrived: nothing is left to announce.
done('v4.10.0', '1')
assert update.current_version() == 'v4.10.0'
assert update_check.available() is None and update_check.surface() is None
assert update_check.session_notice() == ('', '')
''')

    def test_notice_links_the_release_notes_of_a_github_source(self):
        self.check_case(r'''
done('v4.0.0', '0')
# The local test source has no release page, so no link is made up.
cached()
agent, user = update_check.session_notice()
assert user and 'releases/tag' not in user + agent, (user, agent)
assert 'notes' not in update_check.surface()
notice = core.local_lock_path(update_check.NOTICE)
link = 'https://github.com/o/r/releases/tag/v4.10.0'
for url in ('https://github.com/o/r.git', 'git@github.com:o/r.git', 'https://github.com/o/r'):
    configure(url=url)
    cached(url=url)
    notice.unlink()
    agent, user = update_check.session_notice()
    assert user.endswith(' 릴리스 노트: ' + link), (url, user)
    assert link in agent and '이행 안내' in agent, (url, agent)
    assert update_check.surface()['notes'] == link, url
''')

    def test_switches_pins_and_failures(self):
        self.check_case(r'''
# Without a known version there is nothing to compare, so nothing is checked.
assert update_check.off_reason() and update_check.ensure_fresh() is None
done('v4.0.0', '0')
fresh = cached()
assert update_check.available() == {'latest': 'v4.10.0', 'current': 'v4.0.0'}
cached(at=time.time() - update_check.CHECK_EVERY - 1)
def blocked():
    with mock.patch.object(core, 'spawn_worker', side_effect=AssertionError('spawned')):
        assert update_check.ensure_fresh() is None
    return (update_check.available() is None and update_check.surface() is None
            and update_check.session_notice() == ('', ''))
os.environ['OSK_UPDATE_CHECK'] = '0'
assert blocked(), 'environment switch'
del os.environ['OSK_UPDATE_CHECK']
config.write_text(json.dumps({'upstream': {'source': 'git', 'url': str(up)},
                              'update_check': False}), encoding='utf-8')
assert blocked(), 'configuration switch'
configure(source='bundle')
assert blocked(), 'bundle source'
configure(pin='v4.0.0')
assert blocked(), 'pinned version'
assert 'pin' in update_check.report()['auto_off'], update_check.report()
# A result from another source is not announced, and that source is due.
configure(url=str(up) + '-mirror')
assert update_check.available() is None
assert update_check.due(fresh, str(up) + '-mirror')
# A failed check is recorded, keeps the last result, and is retried after an hour.
missing = str(root.parent / 'missing.git')
configure(url=missing)
cached(url=missing)
rep = update_check.check()
st = json.loads(state_path.read_text(encoding='utf-8'))
assert st['error'] and rep['error'] and st['latest'] == 'v4.10.0', st
assert not update_check.due(st, missing)
assert update_check.due(st, missing, now=st['at'] + update_check.RETRY_AFTER)
assert not update_check.due(dict(st, error=None), missing, now=st['at'] + update_check.RETRY_AFTER)
assert update_check.due(dict(st, error=None), missing, now=st['at'] - 60), 'clock moved back'
r = subprocess.run([sys.executable, '-m', 'osk.update', '--check'], capture_output=True,
                   text=True, encoding='utf-8')
assert r.returncode == 1 and json.loads(r.stdout)['error'], (r.returncode, r.stdout)
''')

    def test_unattended_query_and_detached_worker_hold_no_terminal(self):
        self.check_case(r'''
seen = {}
TAGS = 'x\trefs/tags/v1.2.3\ny\trefs/tags/v1.2\n'
def fake_run(cmd, **kw):
    seen.clear()
    seen.update(kw, cmd=cmd)
    if hasattr(kw.get('stdout'), 'write'):      # unattended: output goes to a file
        kw['stdout'].write(TAGS.encode())
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.CompletedProcess(cmd, 0, TAGS, '')
os.environ['GIT_ASKPASS'] = os.environ['SSH_ASKPASS'] = 'ask'
with mock.patch.object(update.subprocess, 'run', side_effect=fake_run):
    assert update.latest_release_tag('U', timeout=5, unattended=True) == 'v1.2.3'
    assert seen['cmd'][:3] == ['git', '-c', 'core.askPass='], seen['cmd']
    assert not {'GIT_ASKPASS', 'SSH_ASKPASS'} & set(seen['env']), seen
    assert seen['stdin'] is subprocess.DEVNULL and seen['timeout'] == 5, seen
    # No pipe: a lingering grandchild cannot hold the timed-out wait open.
    assert hasattr(seen['stdout'], 'write') and hasattr(seen['stderr'], 'write'), seen
    assert 'capture_output' not in seen, seen
    assert seen['env']['GIT_TERMINAL_PROMPT'] == '0', seen
    assert seen['env']['GCM_INTERACTIVE'] == 'never', seen
    assert seen['creationflags'] == (0x08000000 if os.name == 'nt' else 0), seen
    assert update.latest_release_tag('U') == 'v1.2.3'
    assert seen['cmd'][:2] == ['git', 'ls-remote'], seen['cmd']
    assert 'env' not in seen and 'stdin' not in seen and seen['timeout'] == 60, seen
    assert seen['capture_output'] is True, seen
# A real timeout ends the check with a recorded error instead of hanging.
def slow_run(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, kw['timeout'])
done('v4.0.0', '0')
with mock.patch.object(update.subprocess, 'run', side_effect=slow_run):
    rep = update_check.check(unattended=True)
assert rep['error'] and 'TimeoutExpired' in rep['error'], rep
with mock.patch.object(core.subprocess, 'Popen') as popen:
    core.spawn_worker('osk.update_check')
kw = popen.call_args.kwargs
assert popen.call_args.args[0][1:] == ['-m', 'osk.update_check'], popen.call_args
assert all(kw[k] is subprocess.DEVNULL for k in ('stdin', 'stdout', 'stderr')), kw
assert kw['env']['OSK_VAULT_ROOT'] == str(core.ROOT) and kw['cwd'] == core.ROOT, kw
assert kw['start_new_session'] == (os.name != 'nt'), kw
assert kw['creationflags'] == (0x08000200 if os.name == 'nt' else 0), kw
''')

    def test_unattended_query_never_asks_for_credentials(self):
        self.check_case(r'''
import http.server, threading
class Deny(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="osk"')
        self.send_header('Content-Length', '0')
        self.end_headers()
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Deny)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:%d/osk.git' % server.server_address[1]
# An askpass that answers and notes each prompt. Git finds a script's
# interpreter by name on PATH on Windows and by its path elsewhere.
asked = root.parent / 'asked.txt'
askpass = root.parent / 'askpass.py'
askpass.write_text('#!' + Path(sys.executable).as_posix() + '\n'
                   'import os, sys\n'
                   'with open(os.environ["OSK_ASKED"], "a", encoding="utf-8") as f:\n'
                   '    f.write(sys.argv[-1] + "\\n")\n'
                   'print("secret")\n', encoding='utf-8')
askpass.chmod(0o755)
gitconfig = root.parent / 'gitconfig'
# No system or user credential helper, and no terminal prompt even when attended:
# a harness that cannot reach the askpass fails here instead of waiting for input.
os.environ.update(GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM='1',
                  GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', OSK_ASKED=str(asked),
                  NO_PROXY='127.0.0.1', no_proxy='127.0.0.1',
                  PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
def prompts(route, unattended):
    """The prompts one denied query put to the askpass reached through `route`."""
    gitconfig.write_text('[core]\n\taskPass = %s\n' % askpass.as_posix()
                         if route == 'core.askPass' else '', encoding='utf-8')
    for name in ('GIT_ASKPASS', 'SSH_ASKPASS'):
        if name == route:
            os.environ[name] = str(askpass)
        else:
            os.environ.pop(name, None)
    asked.unlink(missing_ok=True)
    try:
        update.latest_release_tag(url, timeout=60, unattended=unattended)
    except update.UpdateError as e:
        return (asked.read_text(encoding='utf-8') if asked.exists() else ''), str(e)
    raise AssertionError('a denied query returned tags')
for route in ('GIT_ASKPASS', 'core.askPass', 'SSH_ASKPASS'):
    shown, _ = prompts(route, unattended=False)
    assert 'Username' in shown, (route, 'the harness did not reach the askpass', shown)
    shown, error = prompts(route, unattended=True)
    assert shown == '', (route, shown)
    assert 'could not read Username' in error, (route, error)
server.shutdown()
''')

    def test_session_start_shows_the_user_and_briefs_the_agent_once(self):
        self.check_case(r'''
done('v4.0.0', '0')
cached()
project = root.parent / 'my-app'
project.mkdir()
hook = Path(update.__file__).resolve().parents[1] / 'scripts/hooks/claude_session_start.py'
def start(session='hook-release'):
    payload = {'session_id': session, 'cwd': str(project),
               'hook_event_name': 'SessionStart', 'source': 'startup'}
    r = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload).encode(),
                       capture_output=True, timeout=90)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.decode('utf-8'))
first = start()
text = first['hookSpecificOutput']['additionalContext']
assert text.startswith('[osk 세션 시작'), text[:200]
assert text.index('[osk 새 릴리스 — v4.10.0 · 이 vault v4.0.0]') > 0, text
assert 'v4.10.0' in first['systemMessage'], first
assert 'osk 업데이트해 줘' in first['systemMessage'], first
second = start('hook-release-later')      # the next session on this device the same day
assert 'systemMessage' not in second, second
assert '[osk 새 릴리스' not in second['hookSpecificOutput']['additionalContext'], second
''')

    def test_overview_carries_the_release_for_hosts_without_hooks(self):
        self.check_case(r'''
done('v4.0.0', '0')
cached()
sys.path.insert(0, str(Path(update.__file__).resolve().parents[1]))
import mcp_server as M
o = M.overview()
assert o['update']['latest'] == 'v4.10.0' and 'notify' in o['update'], o.get('update')
assert core.cli_command('update', '--to', 'v4.10.0', '--apply') in o['update']['how'], o['update']
assert 'notify' not in M.overview()['update'], 'notified twice'
done('v4.10.0', '1')
assert 'update' not in M.overview()
''')


if __name__ == '__main__':
    unittest.main()
