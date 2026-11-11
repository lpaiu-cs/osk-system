"""Same-harness subscription forks; native completion and review receipts stay separate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from contextlib import closing

from . import core, growth, integration, transcripts, native_cli, conversation_capture, conversation_state
from . import harness as adapters
from ._portalock import lock_exclusive, unlock
from .vault import Vault
from . import worker_preflight


def config_path(vault: Vault) -> Path:
    return vault.root / '.osk/response-growth.json'


EVERY = 9
# A fork that starts but keeps failing must not leave the review nowhere: after this many
# consecutive unfinished fork reviews the conversation reviews in-session at turns 9 and 15,
# and the fork is tried again once RETRY_AFTER has passed since the last failure.
FALLBACK_AFTER = 2
RETRY_AFTER = 24 * 3600
# Results that say nothing about the fork itself (another worker, a missing CLI, raw not captured).
_NOT_FORK_FAILURES = {'busy', 'unavailable', 'capture_pending', 'running'}
# The 'off' route: no fork and no in-session review — the scheduled run integrates the
# conversation once it has ended (Mechanism §9-4 3항). Either the instance turned the host's
# review off (`"codex": false`) or the source is one a fork cannot continue from its cache.
OFF = 'per-conversation review is turned off in .osk/response-growth.json'
# Codex Desktop hands its app tools (`codex_app`) to its own app-server over a pipe, and its
# features are set at runtime. Both lead every request, so a CLI fork renders other tool
# definitions and recomputes the parent's whole context (12 of 12 forks, 2026-10).
DESKTOP_OFF = ('a CLI fork cannot reproduce the tool definitions Codex Desktop sends first, '
               'so it would recompute the whole parent context')
NOT_CONFIGURED = 'subscription fork CLI is not configured'
AUTH_RACE = 'Failed to refresh OAuth token'
AUTH_RACE_WAIT = 60
# The fork CLI's version and login answers take seconds under load: `claude auth status` alone
# outran its 5 s limit on every prompt in 2026-10 and flipped the route each time (#192). The
# hooks read the verdict their worker last asked (`probe`) and have it asked again after
# PROBE_EVERY; the worker waits PROBE_TIMEOUT per query. The fork itself still asks right
# before inference (`command`).
PROBE_EVERY = 600
PROBE_TIMEOUT = 30


def configured(vault: Vault, harness: str) -> str | bool | None:
    """The harness's fork CLI path; False when its review is turned off (`OFF`), None when unset."""
    if not config_path(vault).exists():
        return None
    settings = json.loads(config_path(vault).read_text(encoding='utf-8-sig'))
    if not isinstance(settings, dict) or set(settings) - set(adapters.fork_names()):
        raise ValueError('response-growth.json must map harness names to native CLI paths')
    executable = settings.get(harness)
    if executable is not None and executable is not False and (
            not isinstance(executable, str) or not executable.strip()):
        raise ValueError('response growth CLI path must be a nonempty string, or false to turn the review off')
    return executable


def _desktop(source: dict) -> bool:
    return source.get('source') == 'vscode' and source.get('originator') == 'Codex Desktop'


def initialize(vault: Vault, env: dict) -> dict | None:
    """Baseline once at startup/input. Neither event increments the Stop counter."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    if not configured(vault, harness):
        return None
    with conversation_state.state_lock(vault, harness, sid) as p:
        state = conversation_state.load_state(vault, p, harness, sid)
        saved = state.get('response_growth')
        if saved is None:
            source = profile(vault, path, harness, sid) if path and Path(path).exists() else None
            saved = {'counter': 'finals', 'seen': source['finals'] if source else [],
                     'count': 0, 'attempted_count': 0, 'history_baselined': source is not None}
            state['response_growth'] = saved
            conversation_state.save_state(p, state)
        return {k: v for k, v in saved.items() if k != 'seen'}


def route(vault: Vault, env: dict) -> dict:
    """The review route without waiting on the fork CLI: a host without a fork or a configured
    CLI reviews in-session, a host turned off or a Desktop source (whatever the CLI setting)
    does neither (`OFF`, `DESKTOP_OFF`), otherwise the verdict is the
    one the hooks' worker last asked (`probe`) — the fork until it has answered. `probe_due`
    asks the hook to start the worker (`defer`)."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    verdict = check(vault, harness, sid, path, env.get('cwd') or os.getcwd(), ask=False)
    with conversation_state.state_lock(vault, harness, sid) as p:
        state = conversation_state.load_state(vault, p, harness, sid)
        asked = state.get('response_growth_probe')
        provisional = bool(verdict and verdict.pop('probe', False))
        due = (verdict is None or provisional) and _probe_due(state, path)
        if provisional and asked and asked.get('reason') == DESKTOP_OFF:
            verdict = None  # The worker read a Desktop transcript; no CLI setting brings the review back.
        selected = verdict or ({'mode': asked['mode'], 'reason': asked.get('reason')} if asked else
                               state.get('response_growth_route') or {'mode': 'background', 'reason': None})
        failed = (state.get('response_growth') or {}).get('failed') or {}
        if (selected['mode'] == 'background' and failed.get('count', 0) >= FALLBACK_AFTER
                and 0 <= time.time() - failed.get('at', 0) < RETRY_AFTER):
            selected = {'mode': 'foreground', 'reason': f"the last {failed['count']} subscription fork "
                        f"reviews did not finish ({failed.get('reason')}); the fork is tried again a day "
                        "after the last failure"}
        # A default nobody has checked yet is no news ("구독 CLI를 확인했다" would be false).
        changed = state.get('response_growth_route') != selected and bool(verdict or asked)
        state['response_growth_route'] = selected
        conversation_state.save_state(p, state)
    return {**selected, 'changed': changed, 'probe_due': due}


def _probe_due(state: dict, path: str | None = None) -> bool:
    asked = state.get('response_growth_probe') or {}
    if asked.get('unread') and path and Path(path).exists():
        return True  # The transcript appeared after the first turn: only it names a Desktop source.
    return not 0 <= time.time() - asked.get('at', 0) < PROBE_EVERY


def probe(vault: Vault, env: dict) -> dict:
    """Ask the fork CLI for route()'s verdict, off the hooks' time limit (their worker)."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    try:
        if configured(vault, harness):
            initialize(vault, env)
        verdict = check(vault, harness, sid, path, env.get('cwd') or os.getcwd(), timeout=PROBE_TIMEOUT)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        verdict = {'mode': 'foreground', 'reason': str(exc)}
    return _remember(vault, harness, sid, verdict, unread=not (path and Path(path).exists()))


def _remember(vault: Vault, harness: str, sid: str, verdict: dict, *, unread: bool = False) -> dict:
    with conversation_state.state_lock(vault, harness, sid) as p:
        state = conversation_state.load_state(vault, p, harness, sid)
        state['response_growth_probe'] = {**verdict, 'at': time.time(), **({'unread': True} if unread else {})}
        conversation_state.save_state(p, state)
    return verdict


def defer(vault: Vault, env: dict, session: str, *, probe_due: bool) -> bool:
    """Hand the hook's slow part to a detached worker (`after_hook`) when there is any: new
    transcript rows to capture, or a fork CLI verdict to ask again. Never waits on it.
    `OSK_HOOK_WORKER=0` keeps it from starting (the test suites drive its steps themselves)."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    if os.environ.get('OSK_HOOK_WORKER') == '0' or not probe_due and not conversation_capture.capture_due(vault, harness, sid, path):
        return False
    payload = {'defer': True, 'harness': harness, 'session_id': sid, 'transcript_path': path,
               'cwd': env.get('cwd'), 'session': session, 'space': env.get('space')}
    log = conversation_state.state_path(vault, harness, sid).with_suffix('.growth.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open('ab') as stderr:
            core.spawn_worker(vault, 'osk.response_growth', stderr=stderr,
                              env={'OSK_GROWTH_WORKER': '1', **runtime_env(harness, sid, ask=False)},
                              stdin=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    except OSError as exc:
        # No worker means no verdict and no background review either (Stop's `launch` starts the
        # same way): review in-session until the verdict is stale, then try again. Counts stay.
        _remember(vault, harness, sid, {'mode': 'foreground', 'reason': f'the hooks could not start their worker ({exc})'})
        return False
    return True


def after_hook(vault: Vault, env: dict) -> None:
    """The hooks' detached part: capture this conversation's new turns, then ask the fork CLI
    again when its verdict is due. Workers of one conversation take turns, and one that waited
    redoes only what is still due: a turn that Stop handed over while another worker was
    parsing is not dropped. Last, once a day, it turns on a validator rule that has no record
    and no violations (Bylaws §11(3)) — every install runs this worker, not every install the
    daily growth run."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    lock_path = conversation_state.state_path(vault, harness, sid).with_suffix('.defer.lock')
    with lock_path.open('a+b') as lock:
        lock_exclusive(lock)
        try:
            if conversation_capture.capture_due(vault, harness, sid, path):
                conversation_capture.hook_capture(vault, env, env['session'])
            with conversation_state.state_lock(vault, harness, sid) as p:
                due = _probe_due(conversation_state.load_state(vault, p, harness, sid), path)
            if due:
                probe(vault, env)
        finally:
            unlock(lock)
    try:
        from . import validate
        validate.auto_activate(vault)
    except Exception:
        pass        # a rule that is not turned on stays report-only


def check(vault: Vault, harness: str, sid: str | None, path: str | None, cwd: str, *, ask: bool = True,
          timeout: float = 5) -> dict | None:
    """route()'s decision without its state writes; shared with `fork doctor`. With `ask=False`
    it is None where only the fork CLI or the transcript can decide (`preflight`, `_desktop`), and
    a Codex host without its CLI gets a `probe`-marked verdict a Desktop transcript overrides."""
    try:
        if not adapters.get(harness).fork:
            # B-grade host: in-session review is its design, not a missing setting.
            return {'mode': 'foreground', 'reason': f'{adapters.get(harness).title} has no subscription fork'}
        executable = configured(vault, harness)
        if executable is False:
            return {'mode': 'off', 'reason': OFF}
        if not executable and (not ask or harness != 'codex'):
            # Only a Codex transcript, which only the hooks' worker reads, names a Desktop source
            # (`probe`): until then a Codex host without its CLI reviews in-session.
            return {'mode': 'foreground', 'reason': NOT_CONFIGURED, **({'probe': True} if harness == 'codex' else {})}
        if not ask:
            return None
        source = (profile(vault, path, harness, sid) if path and Path(path).exists() else
                  {'harness': harness, 'conversation_id': sid, 'cwd': cwd})
        if _desktop(source):
            return {'mode': 'off', 'reason': DESKTOP_OFF}
        if not executable:
            return {'mode': 'foreground', 'reason': NOT_CONFIGURED}
        worker_preflight.preflight(vault, source, executable, {**worker_preflight.subscription_env(), **runtime_env(harness, sid)}, require_version=False,
                  timeout=timeout)
        return {'mode': 'background', 'reason': None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'mode': 'foreground', 'reason': str(exc)}


def _shown(url) -> str | None:
    """Display only: userinfo, query and fragment can carry credentials."""
    return re.sub(r'//[^/?#]*@', '//', str(url)).split('?')[0].split('#')[0] if url else None


def doctor(vault: Vault, harness: str, sid: str | None = None, transcript: str | None = None) -> dict:
    """Read-only `fork doctor`: check()'s verdict plus the local facts behind it; no state writes."""
    if transcript and not sid:
        raise ValueError('--transcript needs the conversation ID (--session)')
    if sid:
        conversation_state.conversation_key(vault, harness, sid)
        transcript = transcript or conversation_capture.locate_transcript(vault, harness, sid)
    cwd = os.getcwd()
    report = {'harness': harness, 'verdict': check(vault, harness, sid, transcript, cwd),
              'conversation_id': sid, 'transcript': transcript}

    def fact(key, fn, into=report):
        """A failed local query is shown, never raised: the verdict above still prints."""
        try:
            into[key] = fn()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            into[key] = f'error: {exc}'
            return None
        return into[key]

    source = None
    if transcript and Path(transcript).exists():
        source = fact('source', lambda: profile(vault, transcript, harness, sid))
    if source:
        keys = (('cli_version', 'model', 'model_provider', 'effort', 'approval_policy', 'approvals_reviewer',
                 'sandbox_policy') if harness == 'codex' else ('version', 'model', 'permission_mode'))
        report['source'] = {**{k: source.get(k) for k in keys}, 'finals': len(source['finals']),
                            'active': source['active']}
        cwd = source.get('cwd') or cwd
    env = {**worker_preflight.subscription_env(), **runtime_env(harness, sid)}
    expected = (source or {}).get('cli_version' if harness == 'codex' else 'version')
    if harness == 'codex':
        report['runtime_version'] = env.get('CODEX_VERSION')
        expected = env.get('CODEX_VERSION') or expected
    folder = cwd if Path(cwd).is_dir() else None
    executable = fact('configured', lambda: configured(vault, harness))
    cli = executable and fact('cli', lambda: native_cli.resolve(executable, version=expected, env=env))
    if cli and Path(cli).is_absolute() and Path(cli).is_file():
        fact('cli_version', lambda: worker_preflight.cli_query(cli, ['--version'], folder, env).stdout.strip())

        def auth():  # A summary only: status output can name an account, organization or API key.
            if harness == 'codex':
                r = worker_preflight.cli_query(cli, ['login', 'status'], folder, env)
                return ('ChatGPT' if 'Logged in using ChatGPT' in r.stdout + r.stderr
                        else f'not a ChatGPT login (exit {r.returncode})')
            r = worker_preflight.cli_query(cli, ['auth', 'status'], folder, env)
            status = json.loads(r.stdout or '{}')  # Shown even on a nonzero exit; preflight still refuses it.
            return {'exit': r.returncode, **{k: status.get(k) for k in (
                'loggedIn', 'authMethod', 'apiProvider', 'subscriptionType')}}
        fact('auth', auth)
    config, layers = {}, {}
    if harness == 'codex':
        try:
            config, layers = worker_preflight.codex_config(env), worker_preflight.codex_layers(env, cwd)
        except (OSError, ValueError) as exc:
            report['config'] = f'error: {exc}'
        report['endpoint'] = {'pinned_for_forks': worker_preflight.OPENAI_ENDPOINT,
                              'top_level_overridden': _shown(config.get('openai_base_url')),
                              'env_dropped': _shown(os.environ.get('OPENAI_BASE_URL')),
                              'config_layers': ', '.join(layers) or None,
                              'refused': ', '.join(f'{k}={_shown(v)}' for k, v in worker_preflight.codex_unpinned(layers).items())
                              or None}
        report['provider'] = {'profile': config.get('profile'), 'config': config.get('model_provider'),
                              'fork': 'openai'}
    else:
        base = os.environ.get('ANTHROPIC_BASE_URL')
        report['endpoint'] = {'ANTHROPIC_BASE_URL': _shown(base), 'fork': (
            'unset' if not base else 'kept; forks refuse it' if env.get('ANTHROPIC_BASE_URL')
            else 'first-party default; dropped for forks')}
    report['cwd'] = {'path': cwd, 'exists': bool(folder)}
    if folder:
        fact('git', lambda: worker_preflight.git_worktree(cwd, env), report['cwd'])
        if harness == 'codex':
            report['cwd']['trusted'] = worker_preflight.codex_trusted(cwd, config)
    return report


def launch(vault: Vault, env: dict, session: str) -> bool:
    """Detach only a small supervisor; Stop returns before native final markers flush."""
    harness, sid, path = conversation_capture.hook_source(vault, env)
    with conversation_state.state_lock(vault, harness, sid) as p:
        state = conversation_state.load_state(vault, p, harness, sid)
        if state.get('response_growth_route', {}).get('mode') in ('foreground', 'off'):
            return False
    try:
        if not configured(vault, harness):
            return False
    except (OSError, ValueError):
        return False  # Input/startup routing reports the error; Stop still captures raw.
    payload = {'harness': harness, 'session_id': sid, 'transcript_path': path,
               'session': session, 'space': env.get('space'), 'permission_mode': env.get('permission_mode')}
    log = conversation_state.state_path(vault, harness, sid).with_suffix('.growth.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stderr:
        core.spawn_worker(vault, 'osk.response_growth', stderr=stderr,
                          env={'OSK_GROWTH_WORKER': '1', **runtime_env(harness, sid, ask=False)},
                          stdin=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    return True


def runtime_env(harness: str, sid: str, *, ask: bool = True) -> dict:
    """Codex hooks lack CODEX_THREAD_ID/CODEX_VERSION; bind the emitting Desktop process's version.
    With `ask=False` a hook only names that Desktop binary for its detached worker, whose own
    ancestry no longer reaches Codex; the worker asks its version (`native_cli.runtime_codex`)."""
    if harness != 'codex' or not sid or os.environ.get('CODEX_THREAD_ID', sid) != sid:
        return {}  # Another thread's environment proves this process is not that hook.
    if not ask:
        return {'CODEX_THREAD_ID': sid, native_cli.DESKTOP_ENV: native_cli.codex_ancestor() or ''}
    version = native_cli.runtime_codex()
    return {'CODEX_THREAD_ID': sid, 'CODEX_VERSION': version} if version else {}


def process_stop(vault: Vault, env: dict, *, flush_timeout: float = 5) -> dict:
    """One native final completion, bounded flush wait, then the existing supervisor."""
    harness, sid, _ = conversation_capture.hook_source(vault, env)
    executable = configured(vault, harness)
    if not executable:
        return {'ok': True, 'state': 'disabled'}
    # ponytail: one worker per conversation; missed Stops catch up by native IDs.
    lock_path = conversation_state.state_path(vault, harness, sid).with_suffix('.growth.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except OSError:
            return {'ok': False, 'state': 'busy'}
        try:
            deadline = time.monotonic() + flush_timeout
            while True:
                try:
                    _, _, path = conversation_capture.hook_source(vault, env)
                    if not path:
                        raise ValueError('native transcript is not available yet')
                    source = profile(vault, path, harness, sid)
                    if not source['active']:
                        break
                    error = 'native final answer is not flushed; review remains pending'
                except (OSError, ValueError) as exc:
                    error = str(exc)
                if time.monotonic() >= deadline:
                    raise ValueError(error)
                time.sleep(0.1)
            captured = conversation_capture.hook_capture(vault, env, env['session'])
            if not source['last_successful']:
                result = {'ok': True, 'state': 'not_final'}
            else:
                with conversation_state.state_lock(vault, harness, sid) as p:
                    state = conversation_state.load_state(vault, p, harness, sid)
                    if 'response_growth' not in state:
                        # First notification after installation counts once, never replays history.
                        state['response_growth'] = {'counter': 'finals', 'seen': source['finals'][:-1],
                                                    'count': 0, 'attempted_count': 0, 'history_baselined': True}
                        conversation_state.save_state(p, state)
                clock = observe(vault, source)
                if not clock['due']:
                    result = {'ok': not bool(captured['capture_error']), 'state': 'not_due', **clock}
                elif harness == 'claude' and env.get('permission_mode') != source.get('permission_mode'):
                    # A mode switch without a new prompt writes no row; Stop names the mode in force.
                    result = {'ok': False, 'state': 'unavailable', **clock,
                              'error': 'permission mode changed since the prompt; review remains pending'}
                else:
                    job = integration.prompt(vault, harness, sid, include_organization=False, max_rounds=EVERY)
                    if job.get('session'):
                        from . import scope_memory
                        job['scope_recovery'] = scope_memory.recovery_block(vault, job['session'])
                    result = attempt(vault, source, job, executable)
        except Exception as exc:
            result = {'ok': False, 'state': 'pending', 'error': f'{type(exc).__name__}: {exc}'}
        finally:
            unlock(lock)
    with conversation_state.state_lock(vault, harness, sid) as p:
        state = conversation_state.load_state(vault, p, harness, sid)
        state['response_growth_stop'] = {**{k: result[k] for k in ('ok', 'state', 'error') if k in result},
                                         'reviewed': _review_done(result)}
        conversation_state.save_state(p, state)
    return result


def profile(vault: Vault, path: str, harness: str, sid: str) -> dict:
    """Read native metadata locally, without copying history into a prompt or raw."""
    conversation_state.conversation_key(vault, harness, sid)
    native = Path(path).resolve()
    before = transcripts.native_fingerprint(str(native), harness, sid)
    result = {'harness': harness, 'conversation_id': sid, 'finals': [], 'active': False,
              'last_successful': False,
              'transcript_path': str(native), 'fingerprint': before,
              'reverted': transcripts.reverted(str(native), harness, sid)}
    finals, identified, context, turn, permission = set(), False, {}, None, None
    with closing(transcripts.native_lines(str(native), harness, sid)) as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                result['active'] = True
                break  # An unflushed JSONL tail is not a completed response.
            if not line.strip():
                continue
            row = json.loads(line)
            p = row.get('payload', {})
            if harness == 'codex':
                if row.get('type') == 'session_meta':
                    if p.get('id') != sid:
                        raise ValueError('native conversation identity mismatch')
                    identified = True
                    result['history_final_count'] = len(result['finals'])
                    result.update({k: p.get(k) for k in ('cwd', 'cli_version', 'source', 'originator', 'model_provider')})
                elif row.get('type') == 'turn_context':
                    result['active'] = True
                    context = {k: p.get(k) for k in (
                        'model', 'effort', 'cwd', 'approval_policy', 'approvals_reviewer', 'sandbox_policy')}
                elif row.get('type') == 'event_msg' and p.get('type') == 'task_started':
                    turn = p.get('turn_id')
                    result['active'] = True
                elif row.get('type') == 'event_msg' and p.get('type') == 'token_count':
                    info = p.get('info') or {}
                    total, last = info.get('total_token_usage'), info.get('last_token_usage')
                    if isinstance(total, dict):
                        result['usage'] = total
                    if isinstance(last, dict):
                        result['context'] = last.get('input_tokens', 0)  # The fork's first request holds at least this.
                elif row.get('type') == 'event_msg' and p.get('type') == 'task_complete':
                    identity = p.get('turn_id')
                    if turn and identity != turn:
                        raise ValueError('native completion turn identity mismatch')
                    result['active'] = False
                    result['last_successful'] = bool(identity and not p.get('error') and str(p.get('last_agent_message') or '').strip())
                    if result['last_successful'] and identity not in finals:
                        finals.add(identity)
                        result['finals'].append(identity)
                        result.update(context)
                elif row.get('type') == 'event_msg' and p.get('type') == 'turn_aborted':
                    result.update(active=False, last_successful=False)
            elif row.get('sessionId') == sid and not row.get('isSidechain') and not row.get('agentId'):
                identified = True
                result.update({k: row[k] for k in ('cwd', 'version') if row.get(k)})
                permission = row.get('permissionMode', permission)  # Prompt rows carry the mode in force.
                message = row.get('message') or {}
                model, identity = message.get('model'), message.get('id')
                if row.get('type') == 'user':
                    result['active'] = True
                if (row.get('type') == 'assistant' and identity and model and model != '<synthetic>'
                        and message.get('stop_reason')):
                    content = message.get('content', [])
                    has_text = bool(content.strip()) if isinstance(content, str) else any(
                        b.get('type') == 'text' and str(b.get('text') or '').strip()
                        for b in content if isinstance(b, dict))
                    successful = message['stop_reason'] == 'end_turn' and has_text
                    # Claude writes thinking/text parts of one message separately.
                    result['active'] = not (successful or identity in finals)
                    result['last_successful'] = not result['active']
                    if successful and identity not in finals:
                        finals.add(identity)
                        result['finals'].append(identity)
                        result.update(model=model, permission_mode=permission)
    if not identified:
        raise ValueError('native conversation identity was not found')
    if not _source_unchanged(result):
        raise ValueError('native file changed during metadata read; retry after completion')
    return result


def _source_unchanged(source: dict) -> bool:
    return transcripts.native_fingerprint(source['transcript_path'], source['harness'],
                                         source['conversation_id']) == source['fingerprint']


def observe(vault: Vault, source: dict) -> dict:
    """Deduplicate native completions; deployment/resume never replays old history."""
    harness, sid = source['harness'], source['conversation_id']
    ids = source['finals']
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ValueError('completion identities must be unique')
    with conversation_state.state_lock(vault, harness, sid) as path:
        state = conversation_state.load_state(vault, path, harness, sid)
        saved = state.get('response_growth')
        if saved is None:
            saved = {'counter': 'finals', 'seen': ids, 'count': 0, 'attempted_count': 0,
                     'history_baselined': True}
            state['response_growth'] = saved
        else:
            # A turn Codex reverted is out of the conversation now (#208); its Stop was
            # real, so the count keeps it.
            seen = [i for i in saved['seen'] if i not in source.get('reverted', ())]
            # Older versions saw only the current page. Restoring an explicit
            # history prefix must not count pre-installation finals as new Stops.
            start = (ids.index(seen[0]) if harness == 'codex' and seen
                     and source.get('fingerprint', {}).get('history')
                     and seen[0] in ids else 0)
            # A cursor with nothing left to anchor on — never baselined, or every turn it saw
            # reverted — takes the earlier pages' finals as seen.
            if harness == 'codex' and not seen and (saved['seen'] or not saved.get('history_baselined')):
                start = source.get('history_final_count', 0)
            if saved['counter'] != 'finals' or ids[start:start + len(seen)] != seen:
                raise ValueError('native completion prefix/counter changed; existing cursor was not reset')
            # Startup/resume is not a reset: unobserved completions still count.
            saved['count'] += len(ids) - start - len(seen)
            saved['seen'] = ids
            saved['history_baselined'] = True
        conversation_state.save_state(path, state)
        return {'count': saved['count'], 'attempted_count': saved['attempted_count'],
                'due': saved['count'] - saved['attempted_count'] >= EVERY,
                'counter': 'finals'}


def _review_done(result: dict) -> bool:
    """fork가 원대화의 검토를 마쳤는가 — 그 검토가 이 fork의 일이다. 함께 고른 정돈·재검토·
    퇴출이 남거나(여러 번에 나눠 끝난다, Mechanism §9-4 4항) 그 결정이 거부돼도 이 시도는
    진척했다. 남은 것을 실패로 세었더니 검토를 마친 fork가 두 번 만에 세션 안 검토로
    넘어갔다(v5.0.0 운영 관측). 원대화의 검토가 끝나지 않은 시도만 실패다."""
    if result.get('ok'):
        return True
    own = result.get('scope_outcomes') or {}
    return bool(own) and all(s.get('status') == 'complete' for s in own.values())


def attempt(vault: Vault, source: dict, job: dict, executable: str) -> dict:
    """The caller serializes one conversation; the shared growth lock limits cost."""
    harness, sid = source['harness'], source['conversation_id']
    clock = observe(vault, source)
    if not clock['due']:
        return {'ok': True, 'state': 'not_due', **clock}
    if job.get('capture_error') or job.get('capture_pending'):
        return {'ok': False, 'state': 'capture_pending', **clock}
    with conversation_state.state_lock(vault, harness, sid) as path:
        state = conversation_state.load_state(vault, path, harness, sid)
        saved = state['response_growth']
        if saved['count'] - saved['attempted_count'] < EVERY:
            return {'ok': True, 'state': 'already_claimed'}
        saved['attempted_count'] = clock['count']
        saved['last_result'] = {'ok': False, 'state': 'running'}
        conversation_state.save_state(path, state)  # A crashed worker is not relaunched in a tight loop.
    try:
        result = (run(vault, source, job, executable) if job.get('pending_refs') else
                  {'ok': True, 'state': 'already_reviewed'})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {'ok': False, 'state': 'blocked', 'error': str(exc)}
    with conversation_state.state_lock(vault, harness, sid) as path:
        state = conversation_state.load_state(vault, path, harness, sid)
        saved = state['response_growth']
        # `reviewed` keeps the fork's own verdict for the hook: an unfinished side job is not a
        # wait for this conversation (#228).
        done = _review_done(result)
        saved['last_result'] = {**{k: result[k] for k in ('ok', 'state', 'error', 'output', 'cache') if k in result},
                                'reviewed': done}
        if done:
            saved.pop('failed', None)
        elif result.get('state') not in _NOT_FORK_FAILURES:
            count = (saved.get('failed') or {}).get('count', 0) + 1
            saved['failed'] = {'count': count, 'at': time.time(),
                               'reason': result.get('error') or result.get('state')}
        if result.get('state') in {'busy', 'unavailable'}:
            saved['attempted_count'] = clock['attempted_count']
        conversation_state.save_state(path, state)
    return result


def command(vault: Vault, source: dict, executable: str, env: dict) -> list[str]:
    """Fail closed before inference. Never fall back to API credentials or a cheaper model."""
    harness, sid = source['harness'], source['conversation_id']
    conversation_state.conversation_key(vault, harness, sid)
    # Preflight first: it names the ChatGPT Web route the model check would only reject generically.
    executable = worker_preflight.preflight(vault, source, executable, env)
    model = source.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', model):
        raise ValueError('actual source model is unavailable')
    if harness == 'codex':
        # Only an ephemeral root fork inherits the parent's cache key (`session-id`); a persistent
        # fork routes to its own and recomputes the parent context (2026-10: 0.99 vs 0.00).
        return [executable, 'exec', 'fork', '--ephemeral', '--json', '--model', model,
                *worker_preflight.codex_overrides(source), sid, '-']
    return [executable, '-p', '--resume', sid, '--fork-session', '--no-session-persistence',
            '--model', model, '--permission-mode', source['permission_mode'],
            '--output-format', 'stream-json', '--verbose', '--settings', worker_preflight.CLAUDE_FORK_SETTINGS]


def cache_usage(output: Path, source: dict) -> dict:
    """Child usage only. Codex fork JSONL includes the parent's cumulative counters."""
    events = [json.loads(line) for line in output.read_text(encoding='utf-8').split('\n') if line.strip()]
    if source['harness'] == 'codex':
        last = next((e.get('usage') for e in reversed(events) if e.get('type') == 'turn.completed'), None)
        baseline = source.get('usage')
        if not isinstance(last, dict) or not isinstance(baseline, dict):
            return {'measured': False, 'reason': 'missing cumulative baseline or successful completion'}
        values = {k: last.get(k, 0) - baseline.get(k, 0) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')}
    else:
        last = next((e for e in reversed(events) if e.get('type') == 'result'), {})
        u = last.get('usage') or {}
        if last.get('is_error') is not False or not u:
            return {'measured': False, 'reason': 'no successful Claude usage'}
        cached = u.get('cache_read_input_tokens', 0)
        values = {'input_tokens': u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0) + cached,
                  'cached_input_tokens': cached, 'output_tokens': u.get('output_tokens', 0)}
    if any(v < 0 for v in values.values()) or values['cached_input_tokens'] > values['input_tokens']:
        return {'measured': False, 'reason': 'fork baseline did not match; cumulative usage is not a cache result'}
    result = {'measured': True, **values, 'cached_fraction': values['cached_input_tokens'] / values['input_tokens']
              if values['input_tokens'] else None, 'scope': 'whole worker turn, not proof of first-request reuse'}
    if source['harness'] == 'codex' and source.get('context'):
        # An ephemeral fork leaves no per-request usage. Its first request misses at most the whole
        # turn's uncached input and holds at least the parent's last request, so its hit rate is no lower.
        uncached = values['input_tokens'] - values['cached_input_tokens']
        result['first_request_floor'] = max(0.0, 1 - uncached / source['context'])
    return result


def _auth_race(vault: Vault, result: dict) -> bool:
    """The Claude CLI exits before inference while another Claude process refreshes the shared
    OAuth token ("usually transient; retry in a minute") — 14 of 86 forks, 2026-09-25..10-05.
    Only the CLI's own final result counts: a reviewed conversation may quote the message."""
    if result.get('returncode') in (0, None) or not result.get('output'):
        return False
    try:
        lines = (vault.root / result['output'] / 'stdout.txt').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'result':
            return event.get('is_error') is True and str(event.get('result', '')).startswith(AUTH_RACE)
    return False


def run(vault: Vault, source: dict, job: dict, executable: str) -> dict:
    """Use the existing supervisor, manifest, deadline and receipt validation."""
    if any(job.get(k) != source.get(k) for k in ('harness', 'conversation_id')):
        raise ValueError('a fork may review only its own source conversation')
    if _desktop(source):
        # Before the hooks' worker has read the transcript the route still says fork.
        _remember(vault, source['harness'], source['conversation_id'], {'mode': 'off', 'reason': DESKTOP_OFF})
        return {'ok': False, 'state': 'unavailable', 'error': DESKTOP_OFF}
    # Stop named its Codex Desktop binary (`launch`); its version is asked here, off the hook's clock.
    env = {**worker_preflight.subscription_env(), **runtime_env(source['harness'], source['conversation_id'])}
    for retry in (False, True):
        try:
            argv = command(vault, source, executable, env)
            # Native trailers (stop summary, title) land seconds after Stop and change only the file.
            fresh = profile(vault, source['transcript_path'], source['harness'], source['conversation_id'])
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {'ok': False, 'state': 'unavailable', 'error': str(exc)}
        if fresh['active'] or any(fresh.get(k) != source.get(k)
                                  for k in (fresh.keys() | source.keys()) - {'fingerprint', 'usage', 'context'}):
            # A new turn or changed model/policy never forks; retry, never spend the attempt.
            return {'ok': False, 'state': 'unavailable', 'error': 'source advanced before fork; review remains pending'}
        source = fresh  # Fork, and measure cache, from the snapshot just checked.
        result = growth.run(vault, argv, limit=1, timeout=600, scope_job=job, cwd=Path(source['cwd']),
                            worker_env=env, invocation='stop_hook')
        if retry or not _auth_race(vault, result):
            break
        time.sleep(AUTH_RACE_WAIT)  # once: a second race stays a failed attempt, as before
    if result.get('output'):
        try:
            result['cache'] = (cache_usage(vault.root / result['output'] / 'stdout.txt', source)
                               if _source_unchanged(source) else
                               {'measured': False, 'reason': 'source advanced; inherited usage baseline is unconfirmed'})
        except (ValueError, OSError) as exc:
            result['cache'] = {'measured': False, 'reason': str(exc)}
    return result


if __name__ == '__main__':
    # No conversation text is passed to this process; failures remain in local status.
    payload = json.load(sys.stdin)
    after_hook(Vault.open(), payload) if payload.get('defer') else process_stop(Vault.open(), payload)
