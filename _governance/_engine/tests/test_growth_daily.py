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
    fake = VAULT.root / (name + '-fake.py')
    fake.write_text(script, encoding='utf-8')
    if os.name == 'nt':
        cli = VAULT.root / (name + '.cmd')
        cli.write_text('@"' + sys.executable + '" "' + str(fake) + '" %*' + chr(13) + chr(10), encoding='utf-8')
    else:
        cli = VAULT.root / name
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
resolved = patch.object(growth, 'check_command', lambda vault, command, **k: {'ok': True, 'executable': command[0]})
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
                return growth_daily.codex_limits(VAULT, codex)
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
            got = growth_daily.codex_limits(VAULT, str(VAULT.root / 'no-such-codex.exe'))
            assert not got['signal'] and got['exhausted'], got
            # The day asks the binary the runner will start. After a Desktop update the command file's
            # path is gone and the runner follows to the new one; the limit read must not stay behind.
            from osk import native_cli
            os.environ['OSK_TEST_LIMITS'] = json.dumps({'result': {'ordinaryUsageAllowed': True,
                                                                   'rateLimits': snap(credits=None, secondary={**week, 'usedPercent': 40})}})
            old = str(VAULT.root / 'Codex' / 'bin' / 'old' / 'codex.exe')
            with patch.object(native_cli, 'resolve', side_effect=lambda p: codex if p == old else p):
                who, limits, skipped = growth_daily._choose(VAULT, {'codex': [old, 'exec']}, {})
            assert who == 'codex' and limits['signal'] and not limits['exhausted'] and not skipped, (who, limits, skipped)
            # #306 D3: at 0.9 of the week Codex rests, credits or not — what is left is its user's.
            os.environ['OSK_TEST_LIMITS'] = json.dumps({'result': {'ordinaryUsageAllowed': False, 'rateLimits': snap(
                credits={'hasCredits': True, 'unlimited': False, 'balance': '11594.3'})}})
            with patch.object(native_cli, 'resolve', side_effect=lambda p: codex if p == old else p):
                who, limits, skipped = growth_daily._choose(VAULT, {'codex': [old, 'exec']}, {})
            assert who is None and skipped == {'codex': 'rest'} and limits['weekly'] == 100, (who, skipped)
        """)

    def test_usage_counts_tokens_without_cache_reads_and_flags_misses(self):
        self.check_case("""
            claude, codex = ['/x/claude.exe', '-p'], ['/x/codex.exe', 'exec']
            # Claude stream-json writes each part of a message with its usage; totals are result.usage.
            # result.modelUsage inherits a resumed conversation's totals and is never read.
            first = {'input_tokens': 10, 'cache_creation_input_tokens': 30000, 'cache_read_input_tokens': 0}
            out = lines(VAULT.root / 'claude.txt', [
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
            out = lines(VAULT.root / 'codex.txt', [{'type': 'thread.started', 'thread_id': tid}, {'type': 'turn.started'},
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
            assert not growth_daily.usage(VAULT.root / 'missing.txt', claude, cold_first=True)['measured']
        """)

    def test_limit_rejections_and_busy_wait_instead_of_failing(self):
        self.check_case("""
            node('A')
            # Claude's organization turned overage off: at 100% the CLI is rejected and says so.
            event = lambda status: {'type': 'rate_limit_event', 'rate_limit_info': {'status': status}}
            rejected = lines(VAULT.root / 'rejected.txt', [event('rejected'), {'type': 'result', 'is_error': True,
                                                                                 'result': "You've hit your limit"}])
            allowed = lines(VAULT.root / 'allowed.txt', [event('allowed_warning')])
            assert growth_daily.limited(VAULT, rejected, ['claude.exe']) and not growth_daily.limited(VAULT, allowed, ['claude.exe'])
            with patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': True}):
                assert growth_daily.limited(VAULT, allowed, ['codex.exe'])
            with patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': False}):
                assert not growth_daily.limited(VAULT, allowed, ['codex.exe'])
            # An unanswered query after a failed worker is no rejection: the failure stays a failure.
            with patch.object(growth_daily, 'codex_limits', return_value={'signal': False, 'exhausted': True}):
                assert not growth_daily.limited(VAULT, allowed, ['codex.exe'])
            # A worker rejected by its limit leaves a waiting run, not a failed one, and the CLI exits 0.
            cli = shell('claude', chr(10).join([
                'import json, sys', 'sys.stdin.read()',
                "print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}}))",
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'limit'}))",
                'sys.exit(1)']))
            command = VAULT.root / 'claude-command.json'
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

    def test_weekly_limit_follows_inflow_and_change_rules(self):
        self.check_case("""
            now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
            def measured(new_jobs, old_jobs, codex=0, claude=0):
                return {'work': {'codex': codex, 'claude': claude},
                        'inflow': {'new_rounds': 0, 'new_jobs': new_jobs, 'old_rounds': 0, 'old_jobs': old_jobs, 'unread': 0}}
            def adjust(rows, m, providers=('codex', 'claude')):
                with patch.object(growth_daily, '_measure', return_value=m):
                    return growth_daily.adjust(VAULT, rows, list(providers), now)
            # N = new jobs + ⌈old jobs ÷ 4⌉, S = ⌈N ÷ 7⌉, L = max(6, S + 3); before any adjustment L was 3.
            got = adjust([], measured(70, 20, codex=66_310_000, claude=145_000_000))
            assert (got['N'], got['S'], got['target'], got['previous'], got['limit']) == (75, 11, 14, 3, 14), got
            assert got['work'] == {'codex': 66_310_000, 'claude': 145_000_000} and 'providers' not in got, got
            # #306 D3: no provider budget cuts S — a week with no work still follows the inflow (it was 6).
            assert adjust([], measured(70, 0))['limit'] == 13
            # Larger is immediate; smaller halves at most; a difference under 3 keeps the current L.
            prior = lambda limit: [{'kind': 'plan', 'rid': 'p', 'daily': {'adjustment': {'limit': limit}}}]
            assert adjust(prior(20), measured(0, 0))['limit'] == 10
            assert adjust(prior(7), measured(0, 0))['limit'] == 7
            assert adjust(prior(6), measured(30, 0))['limit'] == 6      # target 8
            assert adjust(prior(6), measured(70, 0))['limit'] == 13
            assert list(adjust([], measured(1, 0), providers=['claude'])['work']) == ['claude']
            # The Scope batch unit (for the backlog estimate) is the mean of measured daily Scope batches.
            rows = [{'kind': 'plan', 'rid': 's1', 'daily': {'kind': 'scope'}},
                    {'kind': 'run', 'manifest': 's1', 'usage': {'measured': True, 'tokens': 100_000}},
                    {'kind': 'plan', 'rid': 's2', 'daily': {'kind': 'scope'}},
                    {'kind': 'run', 'manifest': 's2', 'usage': {'measured': True, 'tokens': 140_000}},
                    {'kind': 'plan', 'rid': 'l1', 'daily': {'kind': 'light'}},
                    {'kind': 'run', 'manifest': 'l1', 'usage': {'measured': True, 'tokens': 50_000}},
                    {'kind': 'plan', 'rid': 'fork', 'work_context': 'stop:W1'},
                    {'kind': 'run', 'manifest': 'fork', 'usage': {'measured': True, 'tokens': 9_999_999}}]
            assert growth_daily._scope_unit(rows) == (120_000, 2)
            assert growth_daily._scope_unit([]) == (growth_daily.SCOPE_UNIT_COST, 0)
        """)

    def test_a_provider_rests_at_nine_tenths_and_the_backlog_runs_scope_only(self):
        self.check_case("""
            # #306 D3: provider budgets are gone; a provider whose week reaches 0.9 rests and leaves the
            # rest to its user. Claude reports its week in each rate-limit event of a run.
            future, past = int(time.time()) + 3600, int(time.time()) - 3600
            def claude_run(rid, utilization, resets):
                lines(VAULT.root / '.osk/growth/runs' / rid / 'stdout.txt', [
                    {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
                        'five_hour': {'utilization': 0.1, 'resetsAt': future}}}},
                    {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed_warning', 'unifiedWindows': {
                        'seven_day': {'utilization': utilization, 'resetsAt': resets}}}}])
                core.ledger_append(growth.ledger_path(VAULT), {'kind': 'run', 'manifest': rid, 'provider': 'claude',
                                                   'output': '.osk/growth/runs/' + rid})
            assert growth_daily.claude_week(VAULT, growth.ledger_records(VAULT)) is None
            claude_run('r1', 0.95, future)
            assert growth_daily.claude_week(VAULT, growth.ledger_records(VAULT)) == 0.95
            claude_run('r2', 0.95, past)
            assert growth_daily.claude_week(VAULT, growth.ledger_records(VAULT)) is None, 'the window has reset since'
            claude_run('r3', 0.92, future)
            commands = {'codex': ['C:/x/codex.exe', 'exec'], 'claude': ['C:/x/claude.exe', '-p']}
            week = lambda used: patch.object(growth_daily, 'codex_limits',
                                             return_value={'signal': True, 'exhausted': False, 'weekly': used})
            with resolved, week(91):
                who, _, skipped = growth_daily._choose(VAULT, commands, {})
            assert who is None and skipped == {'codex': 'rest', 'claude': 'rest'}, skipped
            with resolved, week(40):
                assert growth_daily._choose(VAULT, commands, {})[0] == 'codex'
            claude_run('r4', 0.5, future)
            with resolved, week(91):
                assert growth_daily._choose(VAULT, commands, {})[:3:2] == ('claude', {'codex': 'rest'})
            # With every provider resting the day waits: not a failure.
            claude_run('r5', 0.9, future)
            with resolved, week(95), patch.object(growth, 'run', side_effect=AssertionError('ran')), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [commands['codex'], commands['claude']])
            assert day['state'] == 'rest' and day['ok'] and not day['batches'], day
            # The backlog runs Scope batches only, past the daily limit and the wall clock, until nothing is left.
            calls = []
            def fake(vault, command, **args):
                calls.append(args)
                if len(calls) > 8:
                    return {'ok': True, 'state': 'skipped', 'selected': 0}
                return {'kind': 'run', 'manifest': 'm' + str(len(calls)), 'state': 'complete', 'returncode': 0,
                        'selected': 2, 'provider': None, 'progress': {'made': ['k' + str(len(calls))], 'none': []},
                        'usage': {'measured': False}}
            with patch.object(growth, 'run', side_effect=fake), patch.object(growth_daily, 'DAY_SECONDS', 0), \\
                    patch.object(growth_daily, 'adjust', side_effect=AssertionError('the backlog measures no week')):
                day = growth_daily.daily(VAULT, [['worker']], backlog=True)
            assert day['state'] == 'empty' and day['backlog'] and len(day['batches']) == 8, day
            assert {c['queues'] for c in calls} == {('scope_jobs',)} and all(c['daily']['backlog'] for c in calls)
            assert all(c['scope_bundle'] == 9 for c in calls)
            # #312 review: the backlog's batches do not spend the scheduled day's L or wall clock — the
            # light queues still get their turn that day. Its no-progress jobs stay out, though.
            day_of = datetime.now(timezone.utc).astimezone().date().isoformat()
            begun = datetime.now(timezone.utc).astimezone() - timedelta(hours=3)
            for n in range(10):
                plan = core.ledger_append(growth.ledger_path(VAULT), {'kind': 'plan', 'at': begun.isoformat(timespec='seconds'),
                                                          'work_context': 'daily', 'daily': {
                                                              'day': day_of, 'kind': 'scope', 'backlog': True}})
                core.ledger_append(growth.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'state': 'incomplete',
                                                   'at': (begun + timedelta(minutes=15)).isoformat(timespec='seconds'),
                                                   'selected': 1, 'progress': {'made': [], 'none': ['stuck' + str(n)]}})
            calls.clear()
            def light_only(vault, command, **args):
                calls.append(args)
                if args['queues'] == ('scope_jobs',) or len(calls) > 3:
                    return {'ok': True, 'state': 'skipped', 'selected': 0}
                return {'kind': 'run', 'manifest': 'l' + str(len(calls)), 'state': 'complete', 'returncode': 0,
                        'selected': 3, 'provider': None, 'progress': {'made': ['x'], 'none': []}, 'usage': {'measured': False}}
            with patch.object(growth, 'run', side_effect=light_only), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [['worker']])
            assert any(c['queues'] == growth_daily.LIGHT for c in calls) and day['done'] == {'scope': 0, 'light': 6}, (calls, day)
            assert day['day_seconds'] < 60 * 60 and 'stuck3' in calls[0]['exclude'], (day, calls[0]['exclude'])
            # The CLI keeps --backlog to the daily run.
            command = VAULT.root / 'command.json'
            command.write_text(json.dumps(['worker']), encoding='utf-8')
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--backlog',
                                   '--command-file', str(command)], capture_output=True, text=True, encoding='utf-8',
                                  timeout=60)
            assert proc.returncode == 1 and '--daily' in proc.stdout, proc.stdout
        """)

    def test_measure_reads_work_and_splits_pending_rounds_by_week(self):
        self.check_case("""
            from osk import growth_status
            now = time.time()
            iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')
            since = now - 7 * 86400
            # Claude: the asked rounds' times come from their user rows; each message counts once.
            claude = lines(VAULT.root / 'claude.jsonl', [
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
            codex = lines(VAULT.root / 'codex.jsonl', [
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
                      state('claude', 'gone', VAULT.root / 'gone.jsonl', ['g1:a', 'g2:a']),
                      state('claude', 'stored', VAULT.root / 'gone.jsonl', ['s1:a'], native=False)]
            view = lambda vault, s: {'pending_refs': [r['ref'] for r in s['rounds']]}
            locate = lambda vault, h, sid, saved=None: saved if Path(saved).exists() else None
            with patch.object(growth_status, 'conversation_states', return_value=states), \\
                    patch.object(integration, 'state_view', side_effect=view), \\
                    patch.object(integration, 'locate_transcript', side_effect=locate):
                got = growth_daily._measure(VAULT, since)
            assert got['work'] == {'claude': 4 + 10 + 6, 'codex': 100 - 60 + 5}, got
            assert got['inflow'] == {'new_rounds': 2, 'new_jobs': 2, 'old_rounds': 12, 'old_jobs': 4, 'unread': 0}, got
        """)

    def test_day_runs_scope_first_then_light_and_goes_on_past_a_batch_without_progress(self):
        self.check_case("""
            calls = []
            def fake(vault, command, **args):
                calls.append(args)
                kind = 'scope' if args['queues'] == ('scope_jobs',) else 'light'
                n = len(calls)
                if n > 4:
                    return {'ok': True, 'state': 'skipped', 'selected': 0}
                made, none = {1: (['s1'], []), 2: (['d1', 'd2'], ['o1']), 3: (['s2', 's2-tail'], []), 4: ([], ['s3'])}[n]
                return {'kind': 'run', 'manifest': 'm' + str(n), 'state': 'incomplete', 'returncode': 0,
                        'selected': len(made + none), 'provider': 'codex', 'progress': {'made': made, 'none': none},
                        'usage': {'measured': True, 'tokens': 1000}}
            fixed = {**adjustment(8, codex=10**6), 'N': 1}
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=fixed), \\
                    patch.object(integration, 'capture_known', wraps=integration.capture_known) as capture, \\
                    patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': False, 'weekly': 2}):
                day = growth_daily.daily(VAULT, [['C:/x/codex.exe', 'exec']])
            # #306 D2: a batch without progress (a hold, too) does not end the day — the next batch runs
            # without the jobs that made none, until the queues have nothing left to pick.
            assert day['state'] == 'empty' and day['ok'], day
            assert [c['queues'] for c in calls] == [('scope_jobs',), growth_daily.LIGHT, ('scope_jobs',), ('scope_jobs',),
                                                    ('scope_jobs',), growth_daily.LIGHT], calls
            assert 's3' in calls[4]['exclude'], calls[4]
            # One Scope job a batch, 9 rounds, 900 s; the other queues three at a time, 600 s.
            assert {(c['limit'], c['timeout'], c['scope_rounds']) for c in calls if c['queues'] == ('scope_jobs',)} == {(1, 900, 9)}
            # #306 D5: a Scope batch bundles its scope's small tails up to nine turns and counts once toward L
            # (batch 3 took two conversations).
            assert {c['scope_bundle'] for c in calls if c['queues'] == ('scope_jobs',)} == {9}
            assert {c['scope_bundle'] for c in calls if c['queues'] != ('scope_jobs',)} == {0}
            assert (calls[1]['limit'], calls[1]['timeout']) == (3, 600)
            # Capture runs once, before any batch; a job without progress is not picked again today.
            assert capture.call_count == 1 and [c['capture'] for c in calls] == [False] * 6, calls
            assert day['capture'] == {'ok': True, 'appended': 0, 'failed': 0, 'known_errors': 0}, day
            assert 'o1' not in calls[1]['exclude'] and 'o1' in calls[2]['exclude'], calls
            # Each plan records its batch, the provider and the limit read; the week's evidence goes once.
            assert [c['daily']['batch'] for c in calls[:4]] == [1, 2, 3, 4]
            assert calls[0]['daily']['adjustment'] == fixed and all('adjustment' not in c['daily'] for c in calls[1:])
            assert calls[0]['daily']['limits']['weekly'] == 2 and calls[0]['daily']['provider'] == 'codex'
            assert 'budget_left' not in day and day['done'] == {'scope': 3, 'light': 3}, day
            # The daily limit and the wall-clock budget stop the day; so does running out of work.
            calls.clear()
            done = lambda vault, command, **args: (calls.append(args), {
                'kind': 'run', 'manifest': 'm', 'state': 'complete', 'returncode': 0, 'selected': args['limit'],
                'provider': None, 'progress': {'made': ['k'], 'none': []}, 'usage': {'measured': False}})[1]
            with patch.object(growth, 'run', side_effect=done), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [['worker']])
            # L = 6: Scope takes L − 3 jobs, one at a time; the light batch keeps its three places.
            assert day['state'] == 'daily_limit' and day['done'] == {'scope': 3, 'light': 3}, day
            assert [(c['limit'], c['queues'] == growth_daily.LIGHT) for c in calls] == [
                (1, False), (3, True), (1, False), (1, False)], calls
            with patch.object(growth, 'run', side_effect=done), patch.object(growth_daily, 'DAY_SECONDS', 0), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                assert growth_daily.daily(VAULT, [['worker']])['state'] == 'time_budget'
            skipped = lambda vault, command, **args: {'ok': True, 'state': 'skipped', 'selected': 0}
            with patch.object(growth, 'run', side_effect=skipped), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [['worker']])
            assert day['state'] == 'empty' and day['ok'] and not day['batches'], day
            assert services.GROWTH_MINUTES == growth_daily.DAY_SECONDS // 60 + 30
        """)

    def test_day_turns_to_claude_when_codex_is_exhausted_or_rejected(self):
        self.check_case("""
            commands = [['C:/x/codex.exe', 'exec'], ['C:/x/claude.exe', '-p']]
            seen = []
            def fake(vault, command, **args):
                who = growth_daily.provider(command)
                seen.append((who, args['daily']['kind']))
                if len(seen) > 4:
                    return {'ok': True, 'state': 'skipped', 'selected': 0}
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
                    patch.object(growth_daily, 'codex_limits', side_effect=lambda vault, exe: next(reads)):
                day = growth_daily.daily(VAULT, commands)
            # Exhausted Codex → Claude; next batch Codex again; Codex rejected mid-run → the same light
            # batch again on Claude, and Codex stays off for the day.
            assert seen[:4] == [('claude', 'scope'), ('codex', 'light'), ('claude', 'light'), ('claude', 'scope')], seen
            assert all(who == 'claude' for who, _ in seen[4:]) and day['state'] == 'empty' and day['ok'], (seen, day)
            # A failed read is no signal: the day runs on Claude; without a Claude worker it waits.
            # Capture costs no model call, so the new turns still reach the vault that day.
            path = lines(VAULT.root / 'native.jsonl', [])
            def turns(n):
                return [row for i in range(n) for row in (
                    {'type': 'user', 'sessionId': 'c1', 'uuid': 'u' + str(i), 'message': {'role': 'user', 'content': 'q' + str(i)}},
                    {'type': 'assistant', 'sessionId': 'c1', 'uuid': 'a' + str(i), 'message': {
                        'role': 'assistant', 'id': 'm' + str(i), 'content': [{'type': 'text', 'text': 'a' + str(i)}],
                        'stop_reason': 'end_turn'}})]
            lines(path, turns(2))
            integration.capture(VAULT, 'claude', 'c1', str(path), 'c1-project', space='00_Scope/W1')
            rounds = lambda: len(integration.load_state(VAULT, integration.state_path(VAULT, 'claude', 'c1'), 'claude', 'c1')['rounds'])
            assert rounds() == 2
            lines(path, turns(5))
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)), \\
                    patch.object(growth_daily, 'codex_limits', return_value={'signal': False, 'exhausted': True}):
                seen.clear()
                day = growth_daily.daily(VAULT, commands[:1])
            assert day['state'] == 'limited' and day['ok'] and not seen and day['skipped'] == {'codex': 'no_signal'}, day
            assert rounds() == 5 and day['capture'] == {'ok': True, 'appended': 1, 'failed': 0, 'known_errors': 0}, (rounds(), day)
            # A capture that fails is the day's failure even when the batches succeed; the same error
            # again the next time is the conversation's known state, not a new failure every day.
            path.unlink()
            ran = lambda vault, command, **args: {'kind': 'run', 'manifest': 'r', 'state': 'complete', 'returncode': 0,
                                           'selected': args['limit'], 'provider': None,
                                           'progress': {'made': ['k'], 'none': []}, 'usage': {'measured': False}}
            with patch.object(growth, 'run', side_effect=ran), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [['worker']])
                again = growth_daily.daily(VAULT, [['worker']])
            assert day['batches'] and not day['ok'] and day['capture']['failed'] == 1, day
            assert [c['conversation_id'] for e in day['errors'] for c in e.get('capture', [])] == ['c1'], day['errors']
            assert again['ok'] and again['capture'] == {'ok': True, 'appended': 0, 'failed': 0, 'known_errors': 1}, again
            # #306 D3: last week's work no longer sets a budget — a week of no Codex work still runs Codex.
            seen.clear()
            with patch.object(growth, 'run', side_effect=fake), resolved, \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6, codex=0, claude=-5)), \\
                    patch.object(growth_daily, 'codex_limits', return_value={'signal': True, 'exhausted': False, 'weekly': 21}):
                day = growth_daily.daily(VAULT, commands)
            assert seen and seen[0][0] == 'codex' and day['state'] != 'budget', (seen, day)
        """)

    def test_a_worker_fault_stops_its_queue_and_the_day_is_logged(self):
        self.check_case("""
            # #306 D2: 11-06 and 11-07 stopped at the first Scope batch, so no recheck batch ran. A worker
            # fault (non-zero exit, timeout, rejected packet) stops that queue only; the light queues still run.
            calls = []
            def fake(vault, command, **args):
                calls.append(args['queues'])
                failed = args['queues'] == ('scope_jobs',) or len(calls) > 2
                return {'kind': 'run', 'manifest': 'm' + str(len(calls)), 'state': 'incomplete', 'selected': 1,
                        'returncode': 1 if failed else 0, 'provider': None, 'usage': {'measured': False},
                        'progress': {'made': [] if failed else ['r1'], 'none': ['k' + str(len(calls))] if failed else []}}
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(9)):
                day = growth_daily.daily(VAULT, [['worker']])
            assert calls == [('scope_jobs',), growth_daily.LIGHT, growth_daily.LIGHT], calls
            assert day['state'] == 'worker_fault' and not day['ok'] and len(day['errors']) == 2, day
            # #306 D6: each day leaves its stop and batch count; a run of days without a batch is reported.
            log = VAULT.root / growth_daily.DAYS
            assert [json.loads(l)['state'] for l in log.read_text(encoding='utf-8').splitlines()] == ['worker_fault']
            assert growth_daily.idle(VAULT) is None, 'a day with batches is not idle'
            def days(*rows):
                lines(log, [{'day': d, 'state': s, 'batches': b} for d, s, b in rows])
            days(('2026-11-01', 'limited', 0), ('2026-11-02', 'budget', 0), ('2026-11-02', 'unavailable', 0),
                 ('2026-11-03', 'limited', 0))
            assert growth_daily.idle(VAULT, '2026-11-03') == {'days': 3, 'first': '2026-11-01', 'last': '2026-11-03',
                                                       'state': 'limited'}
            days(('2026-11-01', 'limited', 0), ('2026-11-02', 'daily_limit', 4), ('2026-11-03', 'unavailable', 0))
            assert growth_daily.idle(VAULT, '2026-11-03') == {'days': 1, 'first': '2026-11-03', 'last': '2026-11-03',
                                                       'state': 'unavailable'}
            days(('2026-11-01', 'limited', 0), ('2026-11-02', 'empty', 0))
            assert growth_daily.idle(VAULT, '2026-11-02') is None, 'nothing to pick is not idleness'
            # #313 review: days without a record (the scheduler stopped) count; today counts only once it ran.
            days(('2026-11-01', 'daily_limit', 4))
            assert growth_daily.idle(VAULT, '2026-11-05') == {'days': 3, 'first': '2026-11-02', 'last': '2026-11-04',
                                                       'state': 'missing'}, growth_daily.idle(VAULT, '2026-11-05')
            # A backlog run is not the daily run: its batches do not end the gap.
            lines(log, [{'day': '2026-11-01', 'state': 'daily_limit', 'batches': 4},
                        {'day': '2026-11-02', 'state': 'limited', 'batches': 0},
                        {'day': '2026-11-02', 'state': 'empty', 'batches': 5, 'backlog': True}])
            assert growth_daily.idle(VAULT, '2026-11-02') == {'days': 1, 'first': '2026-11-02', 'last': '2026-11-02',
                                                       'state': 'limited'}, growth_daily.idle(VAULT, '2026-11-02')
            sys.path.insert(0, str(Path(growth.__file__).resolve().parents[1] / 'scripts/hooks'))
            import claude_session_start as hook
            from datetime import date, timedelta
            today = date.today()
            days(((today - timedelta(days=1)).isoformat(), 'limited', 0), (today.isoformat(), 'unavailable', 0))
            text = hook._daily_note(VAULT)
            assert f'{today - timedelta(days=1):%m-%d}~{today:%m-%d}, 2일 동안' in text and '쓸 수 있는 워커가 없음' in text, text
            days((today.isoformat(), 'complete', 3))
            assert hook._daily_note(VAULT) == ''
        """)

    def test_day_waits_once_on_a_busy_lock(self):
        self.check_case("""
            answers = []
            def fake(vault, command, **args):
                return answers.pop(0)
            ok = {'kind': 'run', 'manifest': 'm', 'state': 'complete', 'returncode': 0, 'selected': 1,
                  'provider': None, 'progress': {'made': [], 'none': ['k']}, 'usage': {'measured': False}}
            busy = {'ok': False, 'state': 'busy'}
            skipped = {'ok': True, 'state': 'skipped', 'selected': 0}
            with patch.object(growth, 'run', side_effect=fake), patch.object(growth_daily, 'BUSY_WAIT', 0), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                answers[:] = [busy, ok, skipped, skipped]
                day = growth_daily.daily(VAULT, [['worker']])
                assert day['state'] == 'empty' and len(day['batches']) == 1 and not answers, day
                answers[:] = [busy, busy]
                day = growth_daily.daily(VAULT, [['worker']])
                assert day['state'] == 'busy' and day['ok'] and not day['batches'] and not answers, day
        """)

    def test_week_is_measured_once_and_todays_stalled_jobs_stay_out(self):
        self.check_case("""
            calls = []
            def fake(vault, command, **args):
                calls.append(args)
                return {'kind': 'run', 'manifest': 'm', 'state': 'incomplete', 'returncode': 0, 'selected': 1,
                        'provider': None, 'progress': {'made': [], 'none': []}, 'usage': {'measured': False}}
            now = datetime.now(timezone.utc).astimezone()
            today, yesterday = now.date().isoformat(), (now - timedelta(days=1)).date().isoformat()
            def ledger(adjusted_days_ago):
                growth.ledger_path(VAULT).unlink(missing_ok=True)
                for rid, day, stalled in (('p1', yesterday, 'old-job'), ('p2', today, 'stalled-job')):
                    plan = core.ledger_append(growth.ledger_path(VAULT), {'kind': 'plan', 'work_context': 'daily', 'daily': {
                        'day': day, 'adjustment': adjustment(7, now - timedelta(days=adjusted_days_ago))}})
                    core.ledger_append(growth.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'ok': False,
                                                       'progress': {'made': [], 'none': [stalled]}})
            measure = patch.object(growth_daily, 'adjust', return_value=adjustment(9))
            with patch.object(growth, 'run', side_effect=fake), measure as adjust:
                ledger(2)
                day = growth_daily.daily(VAULT, [['worker']])
                assert not adjust.called and day['limit'] == 7 and 'adjustment' not in calls[-1]['daily'], day
                assert calls[-1]['exclude'] == {'stalled-job'}, calls[-1]
                ledger(7)
                first = len(calls)
                day = growth_daily.daily(VAULT, [['worker']])
                assert adjust.called and day['limit'] == 9 and calls[first]['daily']['adjustment']['limit'] == 9, day
            # #258: the week counts dates. An adjustment written later in its day than today's run (capture
            # took longer that day) is still seven days old on the seventh day, not a few minutes short.
            late = (now - timedelta(days=7)).replace(hour=23, minute=59, second=0, microsecond=0)
            if late > now - timedelta(days=7):
                with patch.object(growth, 'run', side_effect=fake), \\
                        patch.object(growth_daily, 'adjust', return_value=adjustment(9)) as adjust:
                    growth.ledger_path(VAULT).unlink(missing_ok=True)
                    plan = core.ledger_append(growth.ledger_path(VAULT), {'kind': 'plan', 'work_context': 'daily', 'daily': {
                        'day': late.date().isoformat(), 'adjustment': adjustment(7, late)}})
                    core.ledger_append(growth.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'ok': False,
                                                       'progress': {'made': [], 'none': []}})
                    day = growth_daily.daily(VAULT, [['worker']])
                    assert adjust.called and day['limit'] == 9, day
            # A week that cannot be measured is not recorded: the last L holds and the next run measures again.
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, '_measure', side_effect=ValueError('state damaged')):
                day = growth_daily.daily(VAULT, [['worker']])
            assert not day['ok'] and day['limit'] == 7 and 'adjustment' not in calls[-1]['daily'], day
        """)

    def test_a_second_run_the_same_day_keeps_the_days_limit_and_time(self):
        self.check_case("""
            now = datetime.now(timezone.utc).astimezone()
            today = now.date().isoformat()
            def batch(kind, selected, minutes_ago, state='incomplete'):
                begun = now - timedelta(minutes=minutes_ago)
                plan = core.ledger_append(growth.ledger_path(VAULT), {'kind': 'plan', 'at': begun.isoformat(timespec='seconds'),
                                                          'work_context': 'daily', 'daily': {'day': today, 'kind': kind}})
                core.ledger_append(growth.ledger_path(VAULT), {'kind': 'run', 'manifest': plan['rid'], 'state': state,
                                                   'at': (begun + timedelta(minutes=10)).isoformat(timespec='seconds'),
                                                   'selected': selected, 'progress': {'made': ['k'], 'none': []}})
            # The 09:00 run did three Scope jobs and three light ones; a light batch Codex refused
            # was run again on Claude, so its jobs are not counted twice. Five batches, 50 minutes.
            for minutes_ago in (90, 80, 70):
                batch('scope', 1, minutes_ago)
            batch('light', 3, 60)
            batch('light', 3, 50, state='limited')
            calls = []
            def fake(vault, command, **args):
                calls.append(args)
                return {'kind': 'run', 'manifest': 'm', 'state': 'complete', 'returncode': 0, 'selected': args['limit'],
                        'provider': None, 'progress': {'made': ['k'], 'none': []}, 'usage': {'measured': False}}
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(6)):
                day = growth_daily.daily(VAULT, [['worker']])
            assert day['state'] == 'daily_limit' and not calls and day['done'] == {'scope': 3, 'light': 3}, day
            with patch.object(growth, 'run', side_effect=fake), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(9)):
                day = growth_daily.daily(VAULT, [['worker']])
            # L = 9 leaves three places: Scope takes them up to L − 3.
            assert day['state'] == 'daily_limit' and [c['queues'] for c in calls] == [('scope_jobs',)] * 3, (day, calls)
            calls.clear()
            with patch.object(growth, 'run', side_effect=fake), patch.object(growth_daily, 'DAY_SECONDS', 50 * 60), \\
                    patch.object(growth_daily, 'adjust', return_value=adjustment(9)):
                day = growth_daily.daily(VAULT, [['worker']])
            assert day['state'] == 'time_budget' and not calls and day['day_seconds'] >= 50 * 60, day
        """)

    def test_cli_day_records_batches_progress_and_usage(self):
        self.check_case("""
            node('A')
            node('B')
            command = VAULT.root / 'command.json'
            command.write_text(json.dumps([sys.executable, '-c', packet_worker(wrapper='claude')]), encoding='utf-8')
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--daily',
                                   '--command-file', str(command), '--invocation', 'scheduled'],
                                  capture_output=True, text=True, encoding='utf-8', timeout=120)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            day = json.loads(proc.stdout)
            assert day['ok'] and day['state'] in {'empty', 'daily_limit'} and day['limit'] == 6, day
            rows = core.ledger_read(growth.ledger_path(VAULT))
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
            prompt = (VAULT.root / runs[0]['output'] / 'prompt.txt').read_text(encoding='utf-8')
            assert 'budget_left' not in prompt and 'adjustment' not in prompt, prompt[-500:]
            from osk import growth_status
            item = growth_status.report(VAULT, until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())['runs']['items'][0]
            assert item['batch'] == {'day': day['day'], 'batch': 1, 'kind': 'light'} and item['usage'] == runs[0]['usage'], item
            # Several command files go only with --daily.
            proc = subprocess.run([sys.executable, '-B', '-m', 'osk.cli', 'growth', 'run', '--command-file',
                                   str(command), '--command-file', str(command)], capture_output=True, text=True,
                                  encoding='utf-8', timeout=60)
            assert proc.returncode == 1 and '--daily' in proc.stdout, proc.stdout
            # The day's exit code is its `ok`. A day that ended waiting (every provider limited) with a
            # failed capture still fails; a day that only waited does not.
            from osk import cli
            for result, code in (({'ok': False, 'state': 'limited', 'errors': [{'capture': [{'conversation_id': 'c'}]}]}, 1),
                                 ({'ok': True, 'state': 'limited', 'errors': []}, 0)):
                with patch.object(growth_daily, 'daily', return_value=result):
                    try:
                        cli.main(['growth', 'run', '--daily', '--command-file', str(command)])
                        got = 0
                    except SystemExit as exc:
                        got = exc.code or 0
                assert got == code, (result, got)
        """)

    def test_scope_batch_bundles_small_tails_of_its_scope(self):
        self.check_case("""
            import os, time
            # #306 D5: thirty of thirty-two daily Scope batches were one-turn tails, each paying the batch's
            # fixed cost. A Scope batch takes its head conversation and then that scope's small tails —
            # conversations whose whole wait fits the turns left — up to nine turns in all.
            node('A')
            node('B', 'W2')
            now = time.time()
            for n, (sid, turns, scope) in enumerate((('a', 2, 'W1'), ('b', 1, 'W2'), ('c', 3, 'W1'),
                                                     ('d', 6, 'W1'), ('e', 4, 'W1'))):
                path = VAULT.root / (sid + '.jsonl')
                rows = []
                for i in range(turns):
                    rows += [{'type': 'user', 'sessionId': sid, 'uuid': sid + '-u' + str(i),
                              'message': {'role': 'user', 'content': 'question ' + str(i)}},
                             {'type': 'assistant', 'sessionId': sid, 'uuid': sid + '-a' + str(i),
                              'message': {'role': 'assistant', 'id': sid + '-m' + str(i),
                                          'content': [{'type': 'text', 'text': 'answer ' + str(i)}], 'stop_reason': 'end_turn'}}]
                path.write_text(''.join(json.dumps(row) + chr(10) for row in rows), encoding='utf-8')
                integration.capture(VAULT, 'claude', sid, str(path), sid + '-project', space='00_Scope/' + scope)
                legacy(sid, path, scope)
            def queue():
                # These cursors have no turn times, so they queue by state mtime — and building a job saves
                # its cursor. Put the order back before each pick.
                for n, sid in enumerate('abcde'):
                    os.utime(integration.state_path(VAULT, 'claude', sid), (now - 1000 + n, now - 1000 + n))
            def pick(**options):
                queue()
                return [j['conversation_id'] for j in integration.catchup(VAULT, limit=1, max_rounds=9, capture_new=False,
                                                                          **options)['jobs']]
            assert pick() == ['a']
            # a (2) + c (3) + e (4): b is another scope, d (6) no longer fits after c
            got = pick(bundle=9)
            assert got == ['a', 'c', 'e'], got
            queue()
            head = integration.catchup(VAULT, limit=1, max_rounds=9, capture_new=False)['jobs'][0]
            got = pick(bundle=9, exclude={head['key']})
            assert got == ['b'], ('a stalled head gives way; its scope has no tails', got)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']
            queue()
            result = growth.run(VAULT, worker, limit=1, timeout=900, queues=('scope_jobs',), daily={'kind': 'scope'},
                                scope_bundle=9)
            plan = [r for r in core.ledger_read(growth.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]
            assert [j['conversation_id'] for j in plan['scope_jobs']] == ['a', 'c', 'e'], plan['scope_jobs']
            assert result['selected'] == 3 and growth_daily._counted('scope', result) == 1, result
            # The backlog: 5 conversations, 16 turns; W1's tails 6, 4, 3, 2 fill two batches (6+3, 4+2), W2's one.
            b = growth_daily.backlog(VAULT)
            assert (b['conversations'], b['turns'], b['batches']) == (5, 16, 3), b
            assert b['unit_tokens'] == growth_daily.SCOPE_UNIT_COST and b['tokens'] == 3 * b['unit_tokens'], b
            from osk import doctor
            item = next(i for i in doctor._vault_items(VAULT) if i['check'] == '대화 검토 적체')
            assert '묶음 약 3개' in item['detail'] and '450,000 토큰' in item['detail'], item
            assert '--backlog' in item['fix'] and '0.9' in item['fix'], item
        """)

    def test_scope_batch_takes_nine_rounds_and_light_batch_skips_capture(self):
        self.check_case("""
            node('A')
            path = VAULT.root / 'native.jsonl'
            rows = []
            for i in range(12):
                rows += [{'type':'user','sessionId':'long','uuid':'u'+str(i),'message':{'role':'user','content':'question '+str(i)}},
                         {'type':'assistant','sessionId':'long','uuid':'a'+str(i),'message':{'role':'assistant','id':'m'+str(i),
                          'content':[{'type':'text','text':'answer '+str(i)}],'stop_reason':'end_turn'}}]
            path.write_text(''.join(json.dumps(row)+chr(10) for row in rows), encoding='utf-8')
            integration.capture(VAULT, 'claude','long',str(path),'long-project',space='00_Scope/W1')
            legacy('long', path)
            worker = [sys.executable, '-c', 'import sys; sys.stdin.read()']
            result = growth.run(VAULT, worker, limit=1, timeout=900, queues=('scope_jobs',), daily={'kind': 'scope'})
            plan = [r for r in core.ledger_read(growth.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]
            assert [k for k in growth.QUEUES if plan[k]] == ['scope_jobs'], plan
            job = plan['scope_jobs'][0]
            assert len(job['pending_refs']) == 9 and job['remaining_rounds'] == 3, job
            assert plan['timeout_seconds'] == 900 and plan['daily'] == {'kind': 'scope'}, plan
            assert result['progress'] == {'made': [], 'none': [job['key']]}, result
            # The stalled job stays out; with nothing else waiting the Scope batch has nothing to run.
            assert growth.run(VAULT, worker, limit=1, queues=('scope_jobs',), exclude={job['key']})['state'] == 'skipped'
            # A light batch neither captures nor selects conversations.
            with patch.object(integration, 'catchup', side_effect=AssertionError('captured')):
                result = growth.run(VAULT, worker, limit=3, queues=growth_daily.LIGHT, capture=False)
            plan = [r for r in core.ledger_read(growth.ledger_path(VAULT)) if r['kind'] == 'plan'][-1]
            assert not plan['scope_jobs'] and result['selected'] >= 1, (plan, result)
            # Selection without capture does not read new turns.
            with patch.object(integration, 'capture', side_effect=AssertionError('captured')):
                assert integration.catchup(VAULT, limit=1, max_rounds=9, capture_new=False)['jobs']
        """)


if __name__ == "__main__":
    unittest.main()
