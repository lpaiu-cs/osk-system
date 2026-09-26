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

from . import core, growth, integration, transcripts
from ._portalock import lock_exclusive, unlock

CONFIG = core.ROOT / '.osk/response-growth.json'
EVERY = 9
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
OPENAI_ENDPOINT = 'https://chatgpt.com/backend-api/codex'  # ChatGPT-login Codex backend; forks pin it.
# Keys the fork pins with -c. A native profile's own value wins over -c, so an active profile may not set them.
CODEX_PINNED = ('forced_login_method', 'model_provider', 'openai_base_url', 'model_reasoning_effort',
                'approval_policy', 'approvals_reviewer', 'sandbox_mode', 'sandbox_workspace_write')
# `claude --help` 2.1.280 choices; transcripts still write the accepted legacy name 'default'.
CLAUDE_PERMISSION_MODES = ('acceptEdits', 'auto', 'bypassPermissions', 'default', 'manual', 'dontAsk', 'plan')


def configured(harness: str) -> str | None:
    if not CONFIG.exists():
        return None
    settings = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    if not isinstance(settings, dict) or set(settings) - {'codex', 'claude'}:
        raise ValueError('response-growth.json must map harness names to native CLI paths')
    executable = settings.get(harness)
    if executable is not None and (not isinstance(executable, str) or not executable.strip()):
        raise ValueError('response growth CLI path must be a nonempty string')
    return executable


def initialize(env: dict) -> dict | None:
    """Baseline once at startup/input. Neither event increments the Stop counter."""
    harness, sid, path = integration.hook_source(env)
    if not configured(harness):
        return None
    with integration._locked(harness, sid) as p:
        state = integration._load(p, harness, sid)
        saved = state.get('response_growth')
        if saved is None:
            source = profile(path, harness, sid) if path and Path(path).exists() else None
            saved = {'counter': 'finals', 'seen': source['finals'] if source else [],
                     'count': 0, 'attempted_count': 0, 'history_baselined': source is not None}
            state['response_growth'] = saved
            integration._save(p, state)
        return {k: v for k, v in saved.items() if k != 'seen'}


def route(env: dict) -> dict:
    """Check local CLI readiness without inference; unavailable forks use in-session review."""
    harness, sid, path = integration.hook_source(env)
    try:
        if configured(harness):
            initialize(env)
        selected = check(harness, sid, path, env.get('cwd') or os.getcwd())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        selected = {'mode': 'foreground', 'reason': str(exc)}
    with integration._locked(harness, sid) as p:
        state = integration._load(p, harness, sid)
        changed = state.get('response_growth_route') != selected
        state['response_growth_route'] = selected
        integration._save(p, state)
    return {**selected, 'changed': changed}


def check(harness: str, sid: str | None, path: str | None, cwd: str) -> dict:
    """route()'s decision without its state writes; shared with `fork doctor`."""
    try:
        executable = configured(harness)
        if not executable:
            return {'mode': 'foreground', 'reason': 'subscription fork CLI is not configured'}
        source = (profile(path, harness, sid) if path and Path(path).exists() else
                  {'harness': harness, 'conversation_id': sid, 'cwd': cwd})
        preflight(source, executable, {**subscription_env(), **runtime_env(harness, sid)}, require_version=False)
        return {'mode': 'background', 'reason': None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'mode': 'foreground', 'reason': str(exc)}


def _shown(url) -> str | None:
    """Display only: userinfo, query and fragment can carry credentials."""
    return re.sub(r'//[^/?#]*@', '//', str(url)).split('?')[0].split('#')[0] if url else None


def doctor(harness: str, sid: str | None = None, transcript: str | None = None) -> dict:
    """Read-only `fork doctor`: check()'s verdict plus the local facts behind it; no state writes."""
    from . import native_cli
    if transcript and not sid:
        raise ValueError('--transcript needs the conversation ID (--session)')
    if sid:
        integration._identity(harness, sid)
        transcript = transcript or integration._locate_transcript(harness, sid)
    cwd = os.getcwd()
    report = {'harness': harness, 'verdict': check(harness, sid, transcript, cwd),
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
        source = fact('source', lambda: profile(transcript, harness, sid))
    if source:
        keys = (('cli_version', 'model', 'model_provider', 'effort', 'approval_policy', 'approvals_reviewer',
                 'sandbox_policy') if harness == 'codex' else ('version', 'model', 'permission_mode'))
        report['source'] = {**{k: source.get(k) for k in keys}, 'finals': len(source['finals']),
                            'active': source['active']}
        cwd = source.get('cwd') or cwd
    env = {**subscription_env(), **runtime_env(harness, sid)}
    expected = (source or {}).get('cli_version' if harness == 'codex' else 'version')
    if harness == 'codex':
        report['runtime_version'] = env.get('CODEX_VERSION')
        expected = env.get('CODEX_VERSION') or expected
    folder = cwd if Path(cwd).is_dir() else None
    executable = fact('configured', lambda: configured(harness))
    cli = executable and fact('cli', lambda: native_cli.resolve(executable, version=expected, env=env))
    if cli and Path(cli).is_absolute() and Path(cli).is_file():
        fact('cli_version', lambda: _query(cli, ['--version'], folder, env).stdout.strip())

        def auth():  # A summary only: status output can name an account, organization or API key.
            if harness == 'codex':
                r = _query(cli, ['login', 'status'], folder, env)
                return ('ChatGPT' if 'Logged in using ChatGPT' in r.stdout + r.stderr
                        else f'not a ChatGPT login (exit {r.returncode})')
            r = _query(cli, ['auth', 'status'], folder, env)
            status = json.loads(r.stdout or '{}')  # Shown even on a nonzero exit; preflight still refuses it.
            return {'exit': r.returncode, **{k: status.get(k) for k in (
                'loggedIn', 'authMethod', 'apiProvider', 'subscriptionType')}}
        fact('auth', auth)
    config, layers = {}, {}
    if harness == 'codex':
        try:
            config, layers = _codex_config(env), _codex_layers(env, cwd)
        except (OSError, ValueError) as exc:
            report['config'] = f'error: {exc}'
        report['endpoint'] = {'pinned_for_forks': OPENAI_ENDPOINT,
                              'top_level_overridden': _shown(config.get('openai_base_url')),
                              'env_dropped': _shown(os.environ.get('OPENAI_BASE_URL')),
                              'config_layers': ', '.join(layers) or None,
                              'refused': ', '.join(f'{k}={_shown(v)}' for k, v in _codex_unpinned(layers).items())
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
        fact('git', lambda: _git_worktree(cwd, env), report['cwd'])
        if harness == 'codex':
            report['cwd']['trusted'] = _codex_trusted(cwd, config)
    return report


def launch(env: dict, session: str) -> bool:
    """Detach only a small supervisor; Stop returns before native final markers flush."""
    harness, sid, path = integration.hook_source(env)
    with integration._locked(harness, sid) as p:
        state = integration._load(p, harness, sid)
        if state.get('response_growth_route', {}).get('mode') == 'foreground':
            return False
    try:
        if not configured(harness):
            return False
    except (OSError, ValueError):
        return False  # Input/startup routing reports the error; Stop still captures raw.
    payload = {'harness': harness, 'session_id': sid, 'transcript_path': path,
               'session': session, 'space': env.get('space'), 'permission_mode': env.get('permission_mode')}
    log = integration.state_path(harness, sid).with_suffix('.growth.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as stderr:
        core.spawn_worker('osk.response_growth', stderr=stderr,
                          env={'OSK_GROWTH_WORKER': '1', **runtime_env(harness, sid)},
                          stdin=json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    return True


def runtime_env(harness: str, sid: str) -> dict:
    """Codex hooks lack CODEX_THREAD_ID/CODEX_VERSION; bind the emitting Desktop process's version."""
    if harness != 'codex' or not sid or os.environ.get('CODEX_THREAD_ID', sid) != sid:
        return {}  # Another thread's environment proves this process is not that hook.
    from . import native_cli
    version = native_cli.runtime_codex()
    return {'CODEX_THREAD_ID': sid, 'CODEX_VERSION': version} if version else {}


def process_stop(env: dict, *, flush_timeout: float = 5) -> dict:
    """One native final completion, bounded flush wait, then the existing supervisor."""
    harness, sid, _ = integration.hook_source(env)
    executable = configured(harness)
    if not executable:
        return {'ok': True, 'state': 'disabled'}
    # ponytail: one worker per conversation; missed Stops catch up by native IDs.
    lock_path = integration.state_path(harness, sid).with_suffix('.growth.lock')
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
                    _, _, path = integration.hook_source(env)
                    if not path:
                        raise ValueError('native transcript is not available yet')
                    source = profile(path, harness, sid)
                    if not source['active']:
                        break
                    error = 'native final answer is not flushed; review remains pending'
                except (OSError, ValueError) as exc:
                    error = str(exc)
                if time.monotonic() >= deadline:
                    raise ValueError(error)
                time.sleep(0.1)
            captured = integration.hook_capture(env, env['session'])
            if not source['last_successful']:
                result = {'ok': True, 'state': 'not_final'}
            else:
                with integration._locked(harness, sid) as p:
                    state = integration._load(p, harness, sid)
                    if 'response_growth' not in state:
                        # First notification after installation counts once, never replays history.
                        state['response_growth'] = {'counter': 'finals', 'seen': source['finals'][:-1],
                                                    'count': 0, 'attempted_count': 0, 'history_baselined': True}
                        integration._save(p, state)
                clock = observe(source)
                if not clock['due']:
                    result = {'ok': not bool(captured['capture_error']), 'state': 'not_due', **clock}
                elif harness == 'claude' and env.get('permission_mode') != source.get('permission_mode'):
                    # A mode switch without a new prompt writes no row; Stop names the mode in force.
                    result = {'ok': False, 'state': 'unavailable', **clock,
                              'error': 'permission mode changed since the prompt; review remains pending'}
                else:
                    job = integration.prompt(harness, sid, include_organization=False, max_rounds=EVERY)
                    if job.get('session'):
                        from . import scope_memory
                        job['scope_recovery'] = scope_memory.recovery_block(job['session'])
                    result = attempt(source, job, executable)
        except Exception as exc:
            result = {'ok': False, 'state': 'pending', 'error': f'{type(exc).__name__}: {exc}'}
        finally:
            unlock(lock)
    with integration._locked(harness, sid) as p:
        state = integration._load(p, harness, sid)
        state['response_growth_stop'] = {k: result[k] for k in ('ok', 'state', 'error') if k in result}
        integration._save(p, state)
    return result


def profile(path: str, harness: str, sid: str) -> dict:
    """Read native metadata locally, without copying history into a prompt or raw."""
    integration._identity(harness, sid)
    native = Path(path).resolve()
    before = transcripts.native_fingerprint(str(native), harness, sid)
    result = {'harness': harness, 'conversation_id': sid, 'finals': [], 'active': False,
              'last_successful': False,
              'transcript_path': str(native), 'fingerprint': before}
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
                    total = info.get('total_token_usage')
                    if isinstance(total, dict):
                        result['usage'] = total
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


def subscription_env() -> dict:
    env = dict(os.environ, OSK_GROWTH_WORKER='1')
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY',
                'ANTHROPIC_AUTH_TOKEN', 'CLAUDECODE', 'CLAUDE_CODE_SIMPLE',
                # Desktop host auth channel; the fork uses only its own claude.ai login.
                'CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH', 'CLAUDE_CODE_MESSAGING_SOCKET',
                'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_HOST_SESSION_ID'):
        env.pop(key, None)
    # Desktop sets the first-party default; any other endpoint is kept and refused.
    if env.get('ANTHROPIC_BASE_URL', '').rstrip('/') == 'https://api.anthropic.com':
        del env['ANTHROPIC_BASE_URL']
    return env


def observe(source: dict) -> dict:
    """Deduplicate native completions; deployment/resume never replays old history."""
    harness, sid = source['harness'], source['conversation_id']
    ids = source['finals']
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ValueError('completion identities must be unique')
    with integration._locked(harness, sid) as path:
        state = integration._load(path, harness, sid)
        saved = state.get('response_growth')
        if saved is None:
            saved = {'counter': 'finals', 'seen': ids, 'count': 0, 'attempted_count': 0,
                     'history_baselined': True}
            state['response_growth'] = saved
        else:
            # Older versions saw only the current page. Restoring an explicit
            # history prefix must not count pre-installation finals as new Stops.
            start = (ids.index(saved['seen'][0]) if harness == 'codex' and saved['seen']
                     and source.get('fingerprint', {}).get('history')
                     and saved['seen'][0] in ids else 0)
            if harness == 'codex' and not saved['seen'] and not saved.get('history_baselined'):
                start = source.get('history_final_count', 0)
            if saved['counter'] != 'finals' or ids[start:start + len(saved['seen'])] != saved['seen']:
                raise ValueError('native completion prefix/counter changed; existing cursor was not reset')
            # Startup/resume is not a reset: unobserved completions still count.
            saved['count'] += len(ids) - start - len(saved['seen'])
            saved['seen'] = ids
            saved['history_baselined'] = True
        integration._save(path, state)
        return {'count': saved['count'], 'attempted_count': saved['attempted_count'],
                'due': saved['count'] - saved['attempted_count'] >= EVERY,
                'counter': 'finals'}


def attempt(source: dict, job: dict, executable: str) -> dict:
    """The caller serializes one conversation; the shared growth lock limits cost."""
    harness, sid = source['harness'], source['conversation_id']
    clock = observe(source)
    if not clock['due']:
        return {'ok': True, 'state': 'not_due', **clock}
    if job.get('capture_error') or job.get('capture_pending'):
        return {'ok': False, 'state': 'capture_pending', **clock}
    with integration._locked(harness, sid) as path:
        state = integration._load(path, harness, sid)
        saved = state['response_growth']
        if saved['count'] - saved['attempted_count'] < EVERY:
            return {'ok': True, 'state': 'already_claimed'}
        saved['attempted_count'] = clock['count']
        saved['last_result'] = {'ok': False, 'state': 'running'}
        integration._save(path, state)  # A crashed worker is not relaunched in a tight loop.
    try:
        result = (run(source, job, executable) if job.get('pending_refs') else
                  {'ok': True, 'state': 'already_reviewed'})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {'ok': False, 'state': 'blocked', 'error': str(exc)}
    with integration._locked(harness, sid) as path:
        state = integration._load(path, harness, sid)
        saved = state['response_growth']
        saved['last_result'] = {k: result[k] for k in ('ok', 'state', 'error', 'output', 'cache') if k in result}
        if result.get('state') in {'busy', 'unavailable'}:
            saved['attempted_count'] = clock['attempted_count']
        integration._save(path, state)
    return result


def _toml_value(value) -> str:
    """Encode only the native permission/config value types we can preserve."""
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return '{' + ', '.join(json.dumps(k) + '=' + _toml_value(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    if isinstance(value, (str, bool)):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError('source configuration contains an unsupported TOML value')


def _codex_overrides(source: dict) -> list[str]:
    """Shared routing/launch gate: never silently discard a native restriction."""
    policy = source.get('sandbox_policy')
    if not isinstance(policy, dict):
        raise ValueError('source sandbox policy is unavailable')
    mode = policy.get('type')
    fields = {'workspace-write': {'writable_roots', 'network_access', 'exclude_slash_tmp', 'exclude_tmpdir_env_var'},
              'read-only': {'network_access'}, 'danger-full-access': set()}
    if mode not in fields or set(policy) - fields[mode] - {'type'}:
        raise ValueError('source sandbox restrictions cannot be represented by the fork CLI')
    for key in fields[mode] - {'writable_roots'}:
        if key in policy and not isinstance(policy[key], bool):
            raise ValueError('source sandbox flags must be boolean')
    if mode == 'read-only' and policy.get('network_access', False):
        raise ValueError('read-only network policy cannot be represented by the fork CLI')
    approval = source.get('approval_policy')
    if isinstance(approval, dict):
        granular = approval.get('granular')
        required = {'sandbox_approval', 'rules', 'mcp_elicitations'}
        optional = {'request_permissions', 'skill_approval'}
        if (set(approval) != {'granular'} or not isinstance(granular, dict) or
                not required <= set(granular) or set(granular) - required - optional or
                not all(isinstance(v, bool) for v in granular.values())):
            raise ValueError('source granular approval policy cannot be preserved')
        approval = {'granular': {**dict.fromkeys(sorted(optional), False), **granular}}
    elif approval not in ('never', 'on-request', 'untrusted'):
        raise ValueError('source approval policy is unavailable or unsupported')
    if not isinstance(source.get('effort'), str) or not source['effort']:
        raise ValueError('source reasoning effort is unavailable')
    reviewer = source.get('approvals_reviewer') or 'user'
    if reviewer not in ('user', 'auto_review'):
        raise ValueError('source approval reviewer is unsupported')
    # -c replaces only the top-level key (e.g. a local bridge); preflight refuses a
    # profile/provider base_url, which native config resolution would prefer.
    settings = {'forced_login_method': 'chatgpt', 'model_provider': 'openai',
                'openai_base_url': OPENAI_ENDPOINT, 'model_reasoning_effort': source['effort'], 'approval_policy': approval,
                'approvals_reviewer': reviewer, 'sandbox_mode': mode}
    if source.get('source') == 'vscode' and source.get('originator') == 'Codex Desktop':
        # The app includes these tool definitions at the start of each request.
        # A CLI fork without them loses the parent's cached prefix even after Stop.
        settings['features.code_mode_host'] = True
        settings['plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled'] = True
    if mode == 'workspace-write':
        roots = policy.get('writable_roots', [])
        if not isinstance(roots, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in roots):
            raise ValueError('source writable roots must be absolute paths')
        settings['sandbox_workspace_write.writable_roots'] = roots
        for key in sorted(fields[mode] - {'writable_roots'}):
            settings['sandbox_workspace_write.' + key] = policy.get(key, False)
    return [part for key, value in settings.items() for part in ('-c', key + '=' + _toml_value(value))]


def _codex_config(env: dict) -> dict:
    """The fork's native user config.toml, the only layer read for project trust;
    an unreadable file refuses (ValueError/OSError)."""
    import tomllib
    path = Path(env.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    return tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def _codex_layers(env: dict, cwd: str) -> dict:
    """Every config file native exec reads, by path: system defaults, CODEX_HOME, and each
    `.codex/config.toml` from the project root (nearest marker, else cwd) down to cwd."""
    import tomllib
    system = (Path(os.environ.get('ProgramData') or 'C:/ProgramData') / 'OpenAI/Codex' if os.name == 'nt'
              else Path('/etc/codex'))
    home = Path(env.get('CODEX_HOME') or Path.home() / '.codex')
    layers = {str(p): tomllib.loads(p.read_text(encoding='utf-8'))
              for p in (system / 'config.toml', home / 'config.toml') if p.is_file()}
    markers = next((c['project_root_markers'] for c in reversed(layers.values()) if 'project_root_markers' in c),
                   ['.git'])
    if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
        raise ValueError('Codex project_root_markers must be a list of names')
    folders = [Path(cwd), *Path(cwd).parents]
    root = next((i for i, d in enumerate(folders) if any((d / m).exists() for m in markers)), 0)
    for folder in reversed(folders[:root + 1]):
        path = folder / '.codex/config.toml'
        if path.is_file():
            layers[str(path)] = tomllib.loads(path.read_text(encoding='utf-8'))
    return layers


def _codex_unpinned(layers: dict) -> dict:
    """Settings that win over the fork's -c pins, checked per layer so no layer masks another:
    any pinned key an active profile sets, and each endpoint other than the first-party one."""
    fixed = {'forced_login_method': 'chatgpt', 'model_provider': 'openai', 'openai_base_url': OPENAI_ENDPOINT,
             'base_url': OPENAI_ENDPOINT, 'chatgpt_base_url': 'https://chatgpt.com/backend-api'}
    names = {c['profile'] for c in layers.values() if isinstance(c.get('profile'), str)}
    found = {}
    for path, config in layers.items():
        profiles, providers = config.get('profiles'), config.get('model_providers')
        values = {f'profiles.{n}.{k}': v for n in names if isinstance(profiles, dict)
                  and isinstance(profiles.get(n), dict) for k, v in profiles[n].items()
                  if k in (*CODEX_PINNED, 'chatgpt_base_url')}
        openai = providers.get('openai') if isinstance(providers, dict) else None
        values['model_providers.openai.base_url'] = openai.get('base_url') if isinstance(openai, dict) else None
        values['chatgpt_base_url'] = config.get('chatgpt_base_url')
        found.update({f'{path}: {k}': v for k, v in values.items()
                      if v is not None and str(v).rstrip('/') != fixed.get(k.rsplit('.', 1)[-1])})
    return found


def _query(executable: str, args: list, cwd, env: dict):
    """A local CLI status/version query; never a prompt."""
    return subprocess.run([executable, *args], cwd=cwd, env=env, shell=False, capture_output=True,
                          text=True, encoding='utf-8', timeout=5, creationflags=NO_WINDOW)


def _git_worktree(cwd: str, env: dict) -> bool:
    git = subprocess.run(['git', '-C', cwd, 'rev-parse', '--is-inside-work-tree'],
                         env={k: v for k, v in env.items() if not k.startswith('GIT_')}, shell=False,
                         capture_output=True, text=True, encoding='utf-8', timeout=5, creationflags=NO_WINDOW)
    return not git.returncode and git.stdout.strip() == 'true'


def _codex_trusted(cwd: str, config: dict) -> bool:
    """Native exec also accepts a trusted project; mirror its per-path key, never widen it."""
    projects = config.get('projects')
    key = os.path.normcase(os.path.normpath(cwd))
    # Codex writes normalized keys (e:\vv\x for cwd E:\vv\x). A legacy or conflicting
    # case variant refuses rather than guess the native lookup.
    return isinstance(projects, dict) and key in projects and {
        v.get('trust_level') if isinstance(v, dict) else None
        for k, v in projects.items() if os.path.normcase(os.path.normpath(k)) == key} == {'trusted'}


def preflight(source: dict, executable: str, env: dict, *, require_version: bool = True) -> str:
    """Local authentication/version queries only; used by routing and rechecked before inference."""
    harness = source['harness']
    integration._identity(harness, 'preflight')
    expected = source.get('cli_version') if harness == 'codex' else source.get('version')
    from . import native_cli
    if harness == 'codex':
        if str(source.get('model') or '').startswith('chatgpt-web/'):
            raise ValueError('source model uses the ChatGPT Web bridge route; fork would not use the Codex backend')
        # session_meta keeps the creation version when Desktop resumes an old
        # conversation after an update. Hooks and their detached supervisor inherit
        # the running harness version; bind it to this conversation before using it.
        if source.get('conversation_id') and env.get('CODEX_THREAD_ID') == source['conversation_id']:
            runtime_version = env.get('CODEX_VERSION')
            if runtime_version is not None:
                if (not isinstance(runtime_version, str) or
                        not re.fullmatch(r'\d+\.\d+\.\d+(?:-[\w.]+)?', runtime_version)):
                    raise ValueError('invalid Codex runtime version; review remains pending')
                expected = runtime_version
    # Claude writes its running version on every row, so no runtime env is needed.
    executable = native_cli.resolve(executable, version=expected, env=env)
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise ValueError('configure an existing absolute native CLI path')
    cwd = source.get('cwd')
    if not cwd or not Path(cwd).is_dir():
        raise ValueError('source working directory is unavailable')

    version = _query(executable, ['--version'], cwd, env)
    if (version.returncode or require_version and not expected or
            expected and not re.search(r'(?<![\w.])' + re.escape(expected) + r'(?![\w.])', version.stdout)):
        raise ValueError('configured CLI version differs from the source harness; fork was not started')
    if harness == 'codex':
        if require_version or source.get('finals'):
            _codex_overrides(source)  # turn_context is copied only after a final answer.
        if source.get('model_provider') != 'openai' and (require_version or source.get('model_provider')):
            raise ValueError('only a verified ChatGPT subscription provider is supported')
        config = _codex_config(env)
        # Top-level keys are pinned by -c; these would still select a bridge/proxy or a wider policy.
        unpinned = list(_codex_unpinned(_codex_layers(env, cwd)))
        if env.get('OPENAI_BASE_URL') and env['OPENAI_BASE_URL'].rstrip('/') != OPENAI_ENDPOINT:
            unpinned.append('OPENAI_BASE_URL')
        if unpinned:
            raise ValueError('Codex selects a non-first-party endpoint or policy the fork cannot pin ('
                             + ', '.join(unpinned) + '); no inference started')
        auth = _query(executable, ['login', 'status'], cwd, env)
        if auth.returncode or 'Logged in using ChatGPT' not in auth.stdout + auth.stderr:
            raise ValueError('ChatGPT subscription login is required; no API fallback')
        if not _git_worktree(cwd, env) and not _codex_trusted(cwd, config):
            raise ValueError('source directory is not a Git worktree or trusted Codex project; use in-session review')
        return executable
    # The fork runs with the source's own mode, verbatim; never a guessed or wider one.
    if (require_version or source.get('finals')) and source.get('permission_mode') not in CLAUDE_PERMISSION_MODES:
        raise ValueError('source permission mode is unavailable or unsupported; use in-session review')
    # Settings can select API auth even when a separate subscription is logged in.
    base = Path(env.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    for path in (base / 'settings.json', Path(cwd) / '.claude/settings.json', Path(cwd) / '.claude/settings.local.json'):
        if path.exists():
            settings = json.loads(path.read_text(encoding='utf-8'))
            if settings.get('apiKeyHelper') or any(k in settings.get('env', {}) for k in (
                    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')):
                raise ValueError('Claude settings select an unverified API/provider path; no inference started')
    if any(env.get(k) for k in ('ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK',
                               'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')):
        raise ValueError('Claude environment selects an unverified provider')
    auth = _query(executable, ['auth', 'status'], cwd, env)
    status = json.loads(auth.stdout) if auth.returncode == 0 else {}
    if (status.get('authMethod') != 'claude.ai' or not status.get('loggedIn')
            or str(status.get('subscriptionType')).lower() not in {'pro', 'max', 'team', 'enterprise'}):
        raise ValueError('confirmed Claude subscription login is required; no API fallback')
    return executable


def command(source: dict, executable: str, env: dict) -> list[str]:
    """Fail closed before inference. Never fall back to API credentials or a cheaper model."""
    harness, sid = source['harness'], source['conversation_id']
    integration._identity(harness, sid)
    # Preflight first: it names the ChatGPT Web route the model check would only reject generically.
    executable = preflight(source, executable, env)
    model = source.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', model):
        raise ValueError('actual source model is unavailable')
    if harness == 'codex':
        return [executable, 'exec', 'fork', '--ephemeral', '--json', '--model', model,
                *_codex_overrides(source), sid, '-']
    return [executable, '-p', '--resume', sid, '--fork-session', '--no-session-persistence',
            '--model', model, '--permission-mode', source['permission_mode'],
            '--output-format', 'stream-json', '--verbose',
            '--settings', '{"forceLoginMethod":"claudeai"}']


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
    return {'measured': True, **values, 'cached_fraction': values['cached_input_tokens'] / values['input_tokens']
            if values['input_tokens'] else None, 'scope': 'whole worker turn, not proof of first-request reuse'}


def run(source: dict, job: dict, executable: str) -> dict:
    """Use the existing supervisor, manifest, deadline and receipt validation."""
    if any(job.get(k) != source.get(k) for k in ('harness', 'conversation_id')):
        raise ValueError('a fork may review only its own source conversation')
    env = subscription_env()
    try:
        argv = command(source, executable, env)
        # Native trailers (stop summary, title) land seconds after Stop and change only the file.
        fresh = profile(source['transcript_path'], source['harness'], source['conversation_id'])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'state': 'unavailable', 'error': str(exc)}
    if fresh['active'] or any(fresh.get(k) != source.get(k)
                              for k in (fresh.keys() | source.keys()) - {'fingerprint', 'usage'}):
        # A new turn or changed model/policy never forks; retry, never spend the attempt.
        return {'ok': False, 'state': 'unavailable', 'error': 'source advanced before fork; review remains pending'}
    source = fresh  # Fork, and measure cache, from the snapshot just checked.
    result = growth.run(argv, limit=1, timeout=600, scope_job=job, cwd=Path(source['cwd']), worker_env=env)
    if result.get('output'):
        try:
            result['cache'] = (cache_usage(core.ROOT / result['output'] / 'stdout.txt', source)
                               if _source_unchanged(source) else
                               {'measured': False, 'reason': 'source advanced; inherited usage baseline is unconfirmed'})
        except (ValueError, OSError) as exc:
            result['cache'] = {'measured': False, 'reason': str(exc)}
    return result


if __name__ == '__main__':
    # No conversation text is passed to this process; failures remain in local status.
    process_stop(json.load(sys.stdin))
