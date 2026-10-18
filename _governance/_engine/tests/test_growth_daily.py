"""Run with python tests/test_growth_daily.py; every check owns a subprocess mini-vault.

The scheduled run's day (#175, decision of 2026-10-18): Codex limit reads, waits instead of
failures, batches, the weekly limit, and per-batch tokens and cache hits."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_growth import BOOT, ENGINE  # the mini-vault fixture only; its cases run in their own process

DAILY = BOOT + """
import os, subprocess, time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from osk import growth_daily, integration, services
def shell(name, script):
    # A native-looking executable (`codex`, `claude`) that runs a Python fixture.
    fake = core.ROOT / (name + '-fake.py')
    fake.write_text(script, encoding='utf-8')
    if os.name == 'nt':
        cli = core.ROOT / (name + '.cmd')
        cli.write_text('@"' + sys.executable + '" "' + str(fake) + '" %*' + chr(13) + chr(10), encoding='utf-8')
    else:
        cli = core.ROOT / name
        cli.write_text('#!/bin/sh' + chr(10) + 'exec "' + sys.executable + '" "' + str(fake) + '" "$@"' + chr(10),
                       encoding='utf-8')
        cli.chmod(0o755)
    return str(cli)
def lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + chr(10) for r in rows), encoding='utf-8')
    return path
def adjustment(limit, at=None, **budgets):
    return {'at': (at or datetime.now(timezone.utc).astimezone()).isoformat(timespec='seconds'), 'limit': limit,
            'providers': {p: {'budget': b} for p, b in budgets.items()}}
# The day resolves argv[0] as the runner does; these cases name executables that do not exist.
resolved = patch.object(growth, 'check_command', lambda command, **k: {'ok': True, 'executable': command[0]})
"""


class DailyTests(unittest.TestCase):
    def check_case(self, source):
        with tempfile.TemporaryDirectory(prefix="osk-growth-daily-") as directory:
            env = dict(os.environ, OSK_VAULT_ROOT=directory, PYTHONPATH=str(ENGINE), CODEX_HOME=directory + "/codex",
                       PYTHONDONTWRITEBYTECODE="1", TEMP=directory, TMP=directory, OSK_HOOK_WORKER="0")
            result = subprocess.run([sys.executable, "-B", "-c", DAILY + textwrap.dedent(source)],
                                    env=env, cwd=directory, capture_output=True,
                                    text=True, encoding="utf-8", timeout=150,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_codex_limit_read_counts_credits_and_treats_silence_as_exhausted(self):
        self.check_case("""
            # The app-server's JSON-RPC: initialize, then account/rateLimits/read; no model call.
            codex = shell('codex', chr(10).join([
                "import json, os, sys",
                "assert sys.argv[1:] == ['app-server'], sys.argv",
                "answer = os.environ['OSK_TEST_LIMITS']",
                "for line in sys.stdin:",
                "    m = json.loads(line)",
                "    if m.get('id') == 0: print(json.dumps({'id': 0, 'result': {}}), flush=True)",
                "    if m.get('id') == 1:",
                "        assert m['method'] == 'account/rateLimits/read', m",
                "        if answer == 'silent': sys.exit(0)",
                "        print(json.dumps({'id': 1, **json.loads(answer)}), flush=True)"]))
            def read(answer):
                os.environ['OSK_TEST_LIMITS'] = json.dumps(answer) if answer != 'silent' else answer
                return growth_daily.codex_limits(codex)
            week = {'usedPercent': 100, 'windowDurationMins': 10080, 'resetsAt': 1}
            hour = {'usedPercent': 3, 'windowDurationMins': 300, 'resetsAt': 1}
            snap = lambda **k: {'primary': hour, 'secondary': week, 'rateLimitReachedType': None,
                                'spendControlReached': False, **k}
            # A full weekly window still runs on credits: exhaustion means the credits are gone too.
            got = read({'result': {'ordinaryUsageAllowed': False, 'rateLimits': snap(
                credits={'hasCredits': True, 'unlimited': False, 'balance': '11594.3'})}})
            assert got['signal'] and not got['exhausted'] and got['weekly'] == 100 and got['five_hour'] == 3, got
            assert got['credits'] == '11594.3', got
            got = read({'result': {'ordinaryUsageAllowed': True, 'rateLimits': snap(credits=None)}})
            assert not got['exhausted'], got
            for depleted in ({'ordinaryUsageAllowed': False, 'rateLimits': snap(
                                 credits={'hasCredits': False, 'unlimited': False})},
                             {'ordinaryUsageAllowed': True, 'rateLimits': snap(
                                 rateLimitReachedType='workspace_member_credits_depleted')},
                             {'ordinaryUsageAllowed': True, 'rateLimits': snap(spendControlReached=True)},
                             {'ordinaryUsageAllowed': None, 'rateLimits': snap(credits=None)}):
                got = read({'result': depleted})
                assert got['signal'] and got['exhausted'], (depleted, got)
            # No answer is no signal, handled as exhausted.
            for answer in ('silent', {'error': {'code': -32600, 'message': 'not logged in'}}):
                started = time.monotonic()
                got = read(answer)
                assert not got['signal'] and got['exhausted'], got
                assert time.monotonic() - started < 25, 'an exited app-server must not wait out the timeout'
            got = growth_daily.codex_limits(str(core.ROOT / 'no-such-codex.exe'))
            assert not got['signal'] and got['exhausted'], got
            # The day asks the binary the runner will start. After a Desktop update the command file's
            # path is gone and the runner follows to the new one; the limit read must not stay behind.
            from osk import native_cli
            os.environ['OSK_TEST_LIMITS'] = json.dumps({'result': {'ordinaryUsageAllowed': True,
                                                                   'rateLimits': snap(credits=None)}})
            old = str(core.ROOT / 'Codex' / 'bin' / 'old' / 'codex.exe')
            with patch.object(native_cli, 'resolve', side_effect=lambda p: codex if p == old else p):
                who, limits, skipped = growth_daily._choose({'codex': [old, 'exec']}, {}, {})
            assert who == 'codex' and limits['signal'] and not limits['exhausted'] and not skipped, (who, limits, skipped)
        """)

    def test_usage_counts_tokens_without_cache_reads_and_flags_misses(self):
        self.check_case("""
            claude, codex = ['/x/claude.exe', '-p'], ['/x/codex.exe', 'exec']
            # Claude stream-json writes each part of a message with its usage; totals are result.usage.
            # result.modelUsage inherits a resumed conversation's totals and is never read.
            first = {'input_tokens': 10, 'cache_creation_input_tokens': 30000, 'cache_read_input_tokens': 0}
            out = lines(core.ROOT / 'claude.txt', [
                {'type': 'assistant', 'message': {'id': 'a1', 'usage': {**first, 'output_tokens': 5}}},
                {'type': 'assistant', 'message': {'id': 'a1', 'usage': {**first, 'output_tokens': 20}}},
                {'type': 'assistant', 'message': {'id': 'a2', 'usage': {'input_tokens': 5, 'cache_creation_input_tokens': 100,
                                                                        'cache_read_input_tokens': 30000, 'output_tokens': 50}}},
                {'type': 'assistant', 'message': {'id': 'a3', 'usage': {'input_tokens': 2000, 'cache_creation_input_tokens': 5000,
                                                                        'cache_read_input_tokens': 20000, 'output_tokens': 10}}},
                {'type': 'result', 'is_error': False, 'usage': {'input_tokens': 2015, 'cache_creation_input_tokens': 35100,
                                                                'cache_read_input_tokens': 50000, 'output_tokens': 80},
                 'modelUsage': {'claude-opus-5-5': {'inputTokens': 9999999, 'cacheReadInputTokens': 99999999}}}])
            got = growth_daily.usage(out, claude, cold_first=True)
            assert got == {'measured': True, 'tokens': 2015 + 35100 + 80, 'cache_read': 50000, 'requests': 3,
                           'misses': [[2, 0.741]], 'cold_start': {'tokens': 30030, 'hit': 0.0}}, got
            # A conversation review's first request must reuse the parent's cache: it is judged.
            got = growth_daily.usage(out, claude, cold_first=False)
            assert got['misses'] == [[0, 0.0], [2, 0.741]] and 'cold_start' not in got, got
            # Codex: the worker's own rollout, by the thread id its exec output names. A fork's rollout
            # carries the parent's rows; only rows from its own session_meta on count.
            tid = '01a14c65-fdfc-7ab3-b80d-f51373bf1720'
            out = lines(core.ROOT / 'codex.txt', [{'type': 'thread.started', 'thread_id': tid}, {'type': 'turn.started'},
                                                  {'type': 'turn.completed', 'usage': {'input_tokens': 10**7}}])
            count = lambda at, i, c, o: {'timestamp': at, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': i, 'cached_input_tokens': c, 'output_tokens': o}}}}
            lines(Path(os.environ['CODEX_HOME']) / 'sessions/2026/10/18' / ('rollout-2026-10-18T09-25-15-' + tid + '.jsonl'), [
                {'timestamp': '2026-10-18T00:25:21Z', 'type': 'session_meta', 'payload': {'id': tid}},
                count('2026-10-17T00:00:00Z', 900000, 0, 900),      # the parent's, copied
                count('2026-10-18T00:25:37Z', 31107, 6912, 532),    # cold start
                count('2026-10-18T00:26:00Z', 101409, 97536, 2679),
                count('2026-10-18T00:27:00Z', 50000, 40000, 10)])
            got = growth_daily.usage(out, codex, cold_first=True)
            tokens = (31107 - 6912 + 532) + (101409 - 97536 + 2679) + (50000 - 40000 + 10)
            assert got == {'measured': True, 'tokens': tokens, 'cache_read': 6912 + 97536 + 40000, 'requests': 3,
                           'misses': [[2, 0.8]], 'cold_start': {'tokens': 31107 - 6912 + 532, 'hit': 0.222}}, got
            assert not growth_daily.usage(out, ['/x/worker.exe'], cold_first=True)['measured']
            assert not growth_daily.usage(core.ROOT / 'missing.txt', claude, cold_first=True)['measured']
        """)

    def test_limit_rejections_and_busy_wait_instead_of_failing(self):
        self.check_case("""
            node('A')
            # Claude's organization turned overage off: at 100% the CLI is rejected and says so.
            event = lambda status: {'type': 'rate_limit_event', 'rate_limit_info': {'status': status}}
            rejected = lines(core.ROOT / 'rejected.txt', [event('rejected'), {'type': 'result', 'is_error': True,
                                                                                 'result': "You've hit your limit"}])
            allowed = lines(core.ROOT / 'allowed.txt', [event('allowed_warning')])
            assert growth_daily.limited(rejected, ['claude.exe']) and not growth_daily.limited(allowed, ['claude.exe'])
            with patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': True}):
                assert growth_daily.limited(allowed, ['codex.exe'])
            with patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': False}):
                assert not growth_daily.limited(allowed, ['codex.exe'])
            # A worker rejected by its limit leaves a waiting run, not a failed one, and the CLI exits 0.
            cli = shell('claude', chr(10).join([
                'import json, sys', 'sys.stdin.read()',
                "print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}}))",
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'limit'}))",
                'sys.exit(1)']))
            command = core.ROOT / 'claude-command.json'
            command.write_text(json.dumps([cli]), encoding='utf-8')
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--command-file',
                                   str(command), '--limit', '1'], capture_output=True, text=True, encoding='utf-8',
                                  timeout=120)
            result = json.loads(proc.stdout)
            assert result['state'] == 'limited' and result['provider'] == 'claude', result
            assert proc.returncode == 0 and not growth.run_failed(result), (proc.returncode, result)
            assert growth.run_failed({**result, 'state': 'incomplete'})
            assert not growth.run_failed({'ok': False, 'state': 'busy'})
            assert not growth.run_failed({'ok': False, 'state': 'limited'})
            assert growth.run_failed({'ok': False, 'state': 'unavailable'})
        """)

    def test_weekly_limit_follows_inflow_budget_and_change_rules(self):
        self.check_case("""
            now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
            def measured(new_jobs, old_jobs, codex=0, claude=0):
                return {'work': {'codex': codex, 'claude': claude},
                        'inflow': {'new_rounds': 0, 'new_jobs': new_jobs, 'old_rounds': 0, 'old_jobs': old_jobs, 'unread': 0}}
            def adjust(rows, m, providers=('codex', 'claude')):
                with patch.object(growth_daily, '_measure', return_value=m):
                    return growth_daily.adjust(rows, list(providers), now)
            # N = new jobs + ⌈old jobs ÷ 4⌉, S = ⌈N ÷ 7⌉, L = max(6, S + 3); before any adjustment L was 3.
            # An old backlog spends work ÷ 10 instead of ÷ 20; the budget pays for S at the Scope unit cost.
            got = adjust([], measured(70, 20, codex=66_310_000, claude=145_000_000))
            assert (got['N'], got['S'], got['ratio'], got['previous']) == (75, 11, 10, 3), got
            assert got['providers']['codex']['budget'] == 6_631_000 and got['unit_cost']['tokens'] == 150_000, got
            assert got['S_affordable'] == (6_631_000 + 14_500_000) // 150_000 // 7 and got['limit'] == 14, got
            got = adjust([], measured(70, 0, codex=66_310_000, claude=145_000_000))
            assert got['ratio'] == 20 and got['S_affordable'] == 10 and got['limit'] == 13, got
            # The budget cuts S: with no review budget the floor of 6 still holds.
            assert adjust([], measured(70, 0))['limit'] == 6
            # Larger is immediate; smaller halves at most; a difference under 3 keeps the current L.
            prior = lambda limit: [{'kind': 'plan', 'rid': 'p', 'daily': {'adjustment': {'limit': limit}}}]
            assert adjust(prior(20), measured(0, 0))['limit'] == 10
            assert adjust(prior(7), measured(0, 0))['limit'] == 7
            assert adjust(prior(6), measured(30, 0, codex=10**9))['limit'] == 6      # target 8
            assert adjust(prior(6), measured(70, 0, codex=10**9))['limit'] == 13
            # Each provider's last-week conversation reviews come off its budget; the unit cost is the
            # mean of last week's measured Scope batches.
            at = lambda days: (now - timedelta(days=days)).isoformat()
            rows = [{'kind': 'plan', 'rid': 'fork', 'work_context': 'stop:W1'},
                    {'kind': 'run', 'manifest': 'fork', 'at': at(2), 'provider': 'claude',
                     'usage': {'measured': True, 'tokens': 1_200_000}},
                    {'kind': 'run', 'manifest': 'fork', 'at': at(9), 'provider': 'claude',
                     'usage': {'measured': True, 'tokens': 9_999_999}},
                    {'kind': 'run', 'manifest': 'fork', 'at': at(1), 'provider': 'codex', 'usage': {'measured': False}},
                    {'kind': 'plan', 'rid': 's1', 'daily': {'kind': 'scope'}},
                    {'kind': 'run', 'manifest': 's1', 'at': at(3), 'provider': 'codex',
                     'usage': {'measured': True, 'tokens': 100_000}},
                    {'kind': 'plan', 'rid': 's2', 'daily': {'kind': 'scope'}},
                    {'kind': 'run', 'manifest': 's2', 'at': at(4), 'provider': 'claude',
                     'usage': {'measured': True, 'tokens': 140_000}},
                    {'kind': 'plan', 'rid': 'l1', 'daily': {'kind': 'light'}},
                    {'kind': 'run', 'manifest': 'l1', 'at': at(4), 'provider': 'codex',
                     'usage': {'measured': True, 'tokens': 50_000}}]
            got = adjust(rows, measured(1, 0, codex=2_000_000, claude=145_000_000))
            assert got['providers']['claude']['budget'] == 145_000_000 // 20 - 1_200_000, got
            assert got['providers']['codex'] == {'work': 2_000_000, 'reviews': 0, 'unmeasured_reviews': 1,
                                                 'budget': 100_000}, got
            assert got['unit_cost'] == {'tokens': 120_000, 'measured_batches': 2}, got
            # A fork run recorded before this change is measured from its output on this device.
            run = core.ROOT / '.osk/growth/runs/old'
            run.mkdir(parents=True)
            (run / 'argv.json').write_text(json.dumps(['C:/x/claude.exe', '-p']), encoding='utf-8')
            lines(run / 'stdout.txt', [{'type': 'result', 'usage': {'input_tokens': 7, 'cache_creation_input_tokens': 3,
                                                                    'cache_read_input_tokens': 500, 'output_tokens': 90}}])
            old = rows[:1] + [{'kind': 'run', 'manifest': 'fork', 'at': at(1), 'output': '.osk/growth/runs/old'}]
            assert adjust(old, measured(1, 0))['providers']['claude']['reviews'] == 100
            # Only configured providers have a budget.
            assert list(adjust([], measured(1, 0), providers=['claude'])['providers']) == ['claude']
        """)

    def test_measure_reads_work_and_splits_pending_rounds_by_week(self):
        self.check_case("""
            from osk import growth_status
            now = time.time()
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')
            since = now - 7 * 86400
            # Claude: the asked rounds' times come from their user rows; each message counts once.
            claude = lines(core.ROOT / 'claude.jsonl', [
                {'type': 'user', 'uuid': 'u-old', 'timestamp': iso(now - 9 * 86400)},
                {'type': 'user', 'uuid': 'u-new', 'timestamp': iso(now - 86400)},
                {'type': 'assistant', 'uuid': 'r1', 'timestamp': iso(now - 9 * 86400),
                 'message': {'id': 'old', 'usage': {'input_tokens': 10**6, 'output_tokens': 1}}},
                {'type': 'assistant', 'uuid': 'r2', 'timestamp': iso(now - 3600), 'message': {'id': 'm', 'usage': {
                    'input_tokens': 4, 'cache_creation_input_tokens': 10, 'cache_read_input_tokens': 900, 'output_tokens': 1}}},
                {'type': 'assistant', 'uuid': 'r3', 'timestamp': iso(now - 3600), 'message': {'id': 'm', 'usage': {
                    'input_tokens': 4, 'cache_creation_input_tokens': 10, 'cache_read_input_tokens': 900, 'output_tokens': 6}}}])
            # Codex: a turn id is a UUIDv7 and carries its own time.
            def turn(t):
                h = format(int(t * 1000), '012x')
                return h[:8] + '-' + h[8:] + '-7000-8000-000000000000'
            codex = lines(core.ROOT / 'codex.jsonl', [
                {'timestamp': iso(now - 9 * 86400), 'type': 'session_meta', 'payload': {'id': 'x'}},
                {'timestamp': iso(now - 60), 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'last_token_usage': {'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 5}}}},
                {'timestamp': iso(now - 8 * 86400), 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'last_token_usage': {'input_tokens': 10**6, 'cached_input_tokens': 0, 'output_tokens': 0}}}}])
            def state(harness, sid, path, ids, native=True):
                rounds = [{'id': i, 'ref': ('native:' if native else '00_Scope/W1/_raw/x.txt#') + str(n)}
                          for n, i in enumerate(ids)]
                return {'harness': harness, 'conversation_id': sid, 'transcript_path': str(path), 'rounds': rounds}
            states = [state('claude', 'c', claude, ['u-old:a', 'u-new:a']),
                      state('codex', 'x', codex, ['x:' + turn(now - 3600)] + ['x:' + turn(now - 9 * 86400)] * 10),
                      # no original: its native rounds cannot be reviewed; stored rounds still can
                      state('claude', 'gone', core.ROOT / 'gone.jsonl', ['g1:a', 'g2:a']),
                      state('claude', 'stored', core.ROOT / 'gone.jsonl', ['s1:a'], native=False)]
            view = lambda s: {'pending_refs': [r['ref'] for r in s['rounds']]}
            locate = lambda h, sid, saved=None: saved if Path(saved).exists() else None
            with patch.object(growth_status, '_states', return_value=states), \\
                    patch.object(integration, '_view', side_effect=view), \\
                    patch.object(integration, '_locate_transcript', side_effect=locate):
                got = growth_daily._measure(since)
            assert got['work'] == {'claude': 4 + 10 + 6, 'codex': 100 - 60 + 5}, got
            assert got['inflow'] == {'new_rounds': 2, 'new_jobs': 2, 'old_rounds': 12, 'old_jobs': 4, 'unread': 0}, got
        """)

    def test_day_runs_scope_first_then_light_and_stops_without_progress(self):
        self.check_case("""
            calls = []
            def fake(command, **args):
                calls.append(args)
                kind = 'scope' if args['queues'] == ('scope_jobs',) else 'light'
                n = len(calls)
                made, none = {1: (['s1'], []), 2: (['d1', 'd2'], ['o1']), 3: (['s2'], []), 4: ([], ['s3'])}[n]
                return {'kind': 'run', 'manifest': 'm' + str(n), 'state': 'incomplete', 'returncode': 0,
                        'selected': len(made + none), 'provider': 'codex', 'progress': {'made': made, 'none': none},
                        'usage': {'measured': True, 'tokens': 1000}}
            fixed = {**adjustment(8, codex=10**6), 'N': 1}
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=fixed), \\
                    patch.object(integration, 'capture_known', wraps=integration.capture_known) as capture, \\
                    patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': False, 'weekly': 2}):
                day = growth_daily.daily([['C:/x/codex.exe', 'exec']])
            assert day['state'] == 'no_progress' and day['ok'], day
            assert [c['queues'] for c in calls] == [('scope_jobs',), growth_daily.LIGHT, ('scope_jobs',), ('scope_jobs',)]
            # One Scope job a batch, 9 rounds, 900 s; the other queues three at a time, 600 s.
            assert {(c['limit'], c['timeout'], c['scope_rounds']) for c in calls if c['queues'] == ('scope_jobs',)} == {(1, 900, 9)}
            assert (calls[1]['limit'], calls[1]['timeout']) == (3, 600)
            # Capture runs once, before any batch; a job without progress is not picked again today.
            assert capture.call_count == 1 and [c['capture'] for c in calls] == [False] * 4, calls
            assert day['capture'] == {'ok': True, 'changed': 0, 'errors': 0}, day
            assert 'o1' not in calls[1]['exclude'] and 'o1' in calls[2]['exclude'], calls
            # Each plan records its batch, the provider and the limit read; the week's evidence goes once.
            assert [c['daily']['batch'] for c in calls] == [1, 2, 3, 4]
            assert calls[0]['daily']['adjustment'] == fixed and all('adjustment' not in c['daily'] for c in calls[1:])
            assert calls[0]['daily']['limits']['weekly'] == 2 and calls[0]['daily']['provider'] == 'codex'
            assert day['budget_left'] == {'codex': 10**6 - 4000} and day['done'] == {'scope': 3, 'light': 3}, day
            # The daily limit and the wall-clock budget stop the day; so does running out of work.
            calls.clear()
            done = lambda command, **args: (calls.append(args), {
                'kind': 'run', 'manifest': 'm', 'state': 'complete', 'returncode': 0, 'selected': args['limit'],
                'provider': None, 'progress': {'made': ['k'], 'none': []}, 'usage': {'measured': False}})[1]
            with patch.object(growth, 'run', side_effect=done), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily([['worker']])
            # L = 6: Scope takes L − 3 jobs, one at a time; the light batch keeps its three places.
            assert day['state'] == 'daily_limit' and day['done'] == {'scope': 3, 'light': 3}, day
            assert [(c['limit'], c['queues'] == growth_daily.LIGHT) for c in calls] == [
                (1, False), (3, True), (1, False), (1, False)], calls
            with patch.object(growth, 'run', side_effect=done), patch.object(growth_daily, 'DAY_SECONDS', 0), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                assert growth_daily.daily([['worker']])['state'] == 'time_budget'
            skipped = lambda command, **args: {'ok': True, 'state': 'skipped', 'selected': 0}
            with patch.object(growth, 'run', side_effect=skipped), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily([['worker']])
            assert day['state'] == 'empty' and day['ok'] and not day['batches'], day
            assert services.GROWTH_MINUTES == growth_daily.DAY_SECONDS // 60 + 30
        """)

    def test_day_turns_to_claude_when_codex_is_exhausted_or_rejected(self):
        self.check_case("""
            commands = [['C:/x/codex.exe', 'exec'], ['C:/x/claude.exe', '-p']]
            seen = []
            def fake(command, **args):
                who = growth_daily.provider(command)
                seen.append((who, args['daily']['kind']))
                if who == 'codex' and len(seen) == 2:
                    return {'kind': 'run', 'manifest': 'r', 'state': 'limited', 'returncode': 1, 'selected': 1,
                            'provider': who, 'progress': {'made': [], 'none': ['x']}, 'usage': {'measured': False}}
                return {'kind': 'run', 'manifest': 'r', 'state': 'incomplete', 'returncode': 0, 'selected': args['limit'],
                        'provider': who, 'progress': {'made': ['k'] if len(seen) < 4 else [], 'none': []},
                        'usage': {'measured': False}}
            reads = iter([{'signal': True, 'exhausted': True}, {'signal': True, 'exhausted': False},
                          {'signal': True, 'exhausted': False}])
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(8)), \\
                    patch.object(growth_daily, 'codex_limits', side_effect=lambda exe: next(reads)):
                day = growth_daily.daily(commands)
            # Exhausted Codex → Claude; next batch Codex again; Codex rejected mid-run → the same light
            # batch again on Claude, and Codex stays off for the day.
            assert seen == [('claude', 'scope'), ('codex', 'light'), ('claude', 'light'), ('claude', 'scope')], seen
            assert day['state'] == 'no_progress' and day['ok'], day
            # A failed read is no signal: the day runs on Claude; without a Claude worker it waits.
            # Capture costs no model call, so the new turns still reach the vault that day.
            path = lines(core.ROOT / 'native.jsonl', [])
            def turns(n):
                return [row for i in range(n) for row in (
                    {'type': 'user', 'sessionId': 'c1', 'uuid': 'u' + str(i), 'message': {'role': 'user', 'content': 'q' + str(i)}},
                    {'type': 'assistant', 'sessionId': 'c1', 'uuid': 'a' + str(i), 'message': {
                        'role': 'assistant', 'id': 'm' + str(i), 'content': [{'type': 'text', 'text': 'a' + str(i)}],
                        'stop_reason': 'end_turn'}})]
            lines(path, turns(2))
            integration.capture('claude', 'c1', str(path), 'c1-project', space='00_Scope/W1')
            rounds = lambda: len(integration._load(integration.state_path('claude', 'c1'), 'claude', 'c1')['rounds'])
            assert rounds() == 2
            lines(path, turns(5))
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)), \\
                    patch.object(growth_daily, 'codex_limits', return_value={'signal': False, 'exhausted': True}):
                seen.clear()
                day = growth_daily.daily(commands[:1])
            assert day['state'] == 'limited' and day['ok'] and not seen and day['skipped'] == {'codex': 'no_signal'}, day
            assert rounds() == 5 and day['capture'] == {'ok': True, 'changed': 1, 'errors': 0}, (rounds(), day)
            # Spent weekly budgets stop the day too: not a failure.
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6, codex=0, claude=-5)):
                day = growth_daily.daily(commands)
            assert day['state'] == 'budget' and day['ok'], day
        """)

    def test_day_waits_once_on_a_busy_lock(self):
        self.check_case("""
            answers = []
            def fake(command, **args):
                return answers.pop(0)
            ok = {'kind': 'run', 'manifest': 'm', 'state': 'complete', 'returncode': 0, 'selected': 1,
                  'provider': None, 'progress': {'made': [], 'none': ['k']}, 'usage': {'measured': False}}
            busy = {'ok': False, 'state': 'busy'}
            with patch.object(growth, 'run', side_effect=fake), patch.object(growth_daily, 'BUSY_WAIT', 0), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                answers[:] = [busy, ok]
                day = growth_daily.daily([['worker']])
                assert day['state'] == 'no_progress' and len(day['batches']) == 1 and not answers, day
                answers[:] = [busy, busy]
                day = growth_daily.daily([['worker']])
                assert day['state'] == 'busy' and day['ok'] and not day['batches'] and not answers, day
        """)

    def test_week_is_measured_once_and_todays_stalled_jobs_stay_out(self):
        self.check_case("""
            calls = []
            def fake(command, **args):
                calls.append(args)
                return {'kind': 'run', 'manifest': 'm', 'state': 'incomplete', 'returncode': 0, 'selected': 1,
                        'provider': None, 'progress': {'made': [], 'none': []}, 'usage': {'measured': False}}
            now = datetime.now(timezone.utc).astimezone()
            today, yesterday = now.date().isoformat(), (now - timedelta(days=1)).date().isoformat()
            def ledger(adjusted_days_ago):
                growth.LEDGER.unlink(missing_ok=True)
                for rid, day, stalled in (('p1', yesterday, 'old-job'), ('p2', today, 'stalled-job')):
                    plan = core.ledger_append(growth.LEDGER, {'kind': 'plan', 'work_context': 'daily', 'daily': {
                        'day': day, 'adjustment': adjustment(7, now - timedelta(days=adjusted_days_ago))}})
                    core.ledger_append(growth.LEDGER, {'kind': 'run', 'manifest': plan['rid'], 'ok': False,
                                                       'progress': {'made': [], 'none': [stalled]}})
            measure = patch.object(growth_daily, 'adjust', return_value=adjustment(9))
            with patch.object(growth, 'run', side_effect=fake), measure as adjust:
                ledger(2)
                day = growth_daily.daily([['worker']])
                assert not adjust.called and day['limit'] == 7 and 'adjustment' not in calls[-1]['daily'], day
                assert calls[-1]['exclude'] == {'stalled-job'}, calls[-1]
                ledger(7)
                day = growth_daily.daily([['worker']])
                assert adjust.called and day['limit'] == 9 and calls[-1]['daily']['adjustment']['limit'] == 9, day
            # A week that cannot be measured is not recorded: the last L holds and the next run measures again.
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, '_measure', side_effect=ValueError('state damaged')):
                day = growth_daily.daily([['worker']])
            assert not day['ok'] and day['limit'] == 7 and 'adjustment' not in calls[-1]['daily'], day
        """)

    def test_cli_day_records_batches_progress_and_usage(self):
        self.check_case("""
            node('A')
            node('B')
            command = core.ROOT / 'command.json'
            command.write_text(json.dumps([sys.executable, '-c', packet_worker(wrapper='claude')]), encoding='utf-8')
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--daily',
                                   '--command-file', str(command), '--invocation', 'scheduled'],
                                  capture_output=True, text=True, encoding='utf-8', timeout=120)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            day = json.loads(proc.stdout)
            assert day['ok'] and day['state'] in {'empty', 'no_progress'} and day['limit'] == 6, day
            rows = core.ledger_read(growth.LEDGER)
            plans = [r for r in rows if r['kind'] == 'plan']
            runs = [r for r in rows if r['kind'] == 'run']
            # No conversation is waiting, so the first batch is a light one: three jobs, 600 s.
            assert plans and all(p['daily']['kind'] == 'light' and p['timeout_seconds'] == 600 for p in plans), plans
            assert not any(p['scope_jobs'] for p in plans) and len(plans[0]['work_order']) <= 3, plans[0]
            assert plans[0]['daily']['adjustment']['limit'] == 6 and 'adjustment' not in plans[-1]['daily'] or len(plans) == 1
            assert plans[0]['invocation'] == 'scheduled' and plans[0]['daily']['batch'] == 1, plans[0]
            assert all({'provider', 'usage', 'progress'} <= set(r) for r in runs), runs
            assert runs[0]['progress']['made'] and runs[0]['usage'] == {'measured': False, 'reason': 'no per-request usage'}, runs[0]
            # The runner's evidence stays out of the worker's prompt; growth status shows it per batch.
            prompt = (core.ROOT / runs[0]['output'] / 'prompt.txt').read_text(encoding='utf-8')
            assert 'budget_left' not in prompt and 'adjustment' not in prompt, prompt[-500:]
            from osk import growth_status
            item = growth_status.report(until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())['runs']['items'][0]
            assert item['batch'] == {'day': day['day'], 'batch': 1, 'kind': 'light'} and item['usage'] == runs[0]['usage'], item
            # Several command files go only with --daily.
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--command-file',
                                   str(command), '--command-file', str(command)], capture_output=True, text=True,
                                  encoding='utf-8', timeout=60)
            assert proc.returncode == 1 and '--daily' in proc.stdout, proc.stdout
        """)

    def test_scope_batch_takes_nine_rounds_and_light_batch_skips_capture(self):
        self.check_case("""
            node('A')
            path = core.ROOT / 'native.jsonl'
            rows = []
            for i in range(12):
                rows += [{'type':'user','sessionId':'long','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'long','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),
                          'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(row)+chr(10) for row in rows), encoding='utf-8')
            integration.capture('claude','long',str(path),'long-project',space='00_Scope/W1')
            legacy('long', path)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']
            result = growth.run(worker, limit=1, timeout=900, queues=('scope_jobs',), daily={'kind': 'scope'})
            plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind'] == 'plan'][-1]
            assert [k for k in growth._QUEUES if plan[k]] == ['scope_jobs'], plan
            job = plan['scope_jobs'][0]
            assert len(job['pending_refs']) == 9 and job['remaining_rounds'] == 3, job
            assert plan['timeout_seconds'] == 900 and plan['daily'] == {'kind': 'scope'}, plan
            assert result['progress'] == {'made': [], 'none': [job['key']]}, result
            # The stalled job stays out; with nothing else waiting the Scope batch has nothing to run.
            assert growth.run(worker, limit=1, queues=('scope_jobs',), exclude={job['key']})['state'] == 'skipped'
            # A light batch neither captures nor selects conversations.
            with patch.object(integration, 'catchup', side_effect=AssertionError('captured')):
                result = growth.run(worker, limit=3, queues=growth_daily.LIGHT, capture=False)
            plan = [r for r in core.ledger_read(growth.LEDGER) if r['kind'] == 'plan'][-1]
            assert not plan['scope_jobs'] and result['selected'] >= 1, (plan, result)
            # Selection without capture does not read new turns.
            with patch.object(integration, 'capture', side_effect=AssertionError('captured')):
                assert integration.catchup(limit=1, max_rounds=9, capture_new=False)['jobs']
        """)


if __name__ == "__main__":
    unittest.main()
