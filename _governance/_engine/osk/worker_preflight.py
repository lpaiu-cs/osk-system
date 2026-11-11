"""osk.worker_preflight — 작업자를 띄우기 전에 확인하는 것 (Mechanism §9-4).

구독 로그인만 쓰는 환경(`subscription_env`), fork가 쓸 수 있는 도구와 훅의 선언, Codex 설정의 층과 고정
여부, 그리고 모델을 부르기 전에 자격·설정·공급자를 확인하는 사전 점검(`preflight`). 확인하지 못하면 돌지
않는다. 무엇을 시킬지와 언제 띄울지는 위의 모듈이 정한다."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from . import conversation_state, native_cli
from . import harness as adapters
from .vault import Vault


NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


OPENAI_ENDPOINT = 'https://chatgpt.com/backend-api/codex'  # ChatGPT-login Codex backend; forks pin it.


# Keys the fork pins with -c. A native profile's own value wins over -c, so an active profile may not set them.
CODEX_PINNED = ('forced_login_method', 'model_provider', 'openai_base_url', 'model_reasoning_effort',
                'approval_policy', 'approvals_reviewer', 'sandbox_mode')


# `claude --help` 2.1.280 choices; transcripts still write the accepted legacy name 'default'.
CLAUDE_PERMISSION_MODES = ('acceptEdits', 'auto', 'bypassPermissions', 'default', 'manual', 'dontAsk', 'plan')


# A fork reads the source's untrusted dialogue with its permissions (#85). Restricting its tool
# definitions or mode would turn the cached parent prefix (~450k tokens) into a cache write, so a
# PreToolUse hook denies every tool but this vault's MCP and the ToolSearch that loads it, in any
# mode. The matcher reads the same as an unanchored RegExp test or a full match; the deny is a JSON
# literal that Git Bash and the PowerShell fallback both echo verbatim.
FORK_TOOLS = rf'^(?!(mcp__{adapters.MCP_NAME}__.*|ToolSearch)$).*$'


FORK_DENY = json.dumps({'hookSpecificOutput': {
    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
    'permissionDecisionReason': f'osk fork review: only mcp__{adapters.MCP_NAME}__* tools are available here. '
                                'Continue the review with them.'}})


FORK_HOOK = {'type': 'command', 'command': f"echo '{FORK_DENY}'"}


CLAUDE_FORK_SETTINGS = json.dumps({'forceLoginMethod': 'claudeai', 'hooks': {'PreToolUse': [
    {'matcher': FORK_TOOLS, 'hooks': [FORK_HOOK]}]}})


# Codex takes the same deny as a -c (session-flag) hook. Its PreToolUse also sees every MCP server
# and app connector (`mcp__<server>__<tool>`, every character outside [A-Za-z0-9_] made `_`), and
# its tool_search never reaches the hook, so only this vault's MCP is spared. The matcher is a Rust
# regex, which has no lookahead: it spells out every name that leaves the vault's prefix. An invalid
# or untrusted hook is skipped silently, so the hook carries its own trust — the sha256 of Codex's
# normalized form (defaults filled, keys sorted, compact JSON) under the synthetic session-flags key
# (hooks/list, 0.154.0 and 0.163.0-alpha.10.2).
_OSK_TOOL = re.sub(r'[^A-Za-z0-9_]', '_', f'mcp__{adapters.MCP_NAME}__')


CODEX_FORK_TOOLS = '^(?s:' + '|'.join(f'{_OSK_TOOL[:i]}(?:[^{c}].*)?' for i, c in enumerate(_OSK_TOOL)) + ')$'


_CODEX_TRUST = 'sha256:' + hashlib.sha256(json.dumps(
    {'event_name': 'pre_tool_use', 'matcher': CODEX_FORK_TOOLS,
     'hooks': [{**FORK_HOOK, 'timeout': 600, 'async': False}]},
    sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


CODEX_FORK_HOOKS = {'PreToolUse': [{'matcher': CODEX_FORK_TOOLS, 'hooks': [FORK_HOOK]}], 'state': {
    ('C:\\' if os.name == 'nt' else '/') + f'<session-flags>{os.sep}config.toml:pre_tool_use:0:0':
        {'trusted_hash': _CODEX_TRUST}}}


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


def _toml_value(value) -> str:
    """Encode only the native permission/config value types we can preserve."""
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return '{' + ', '.join(json.dumps(k) + '=' + _toml_value(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    if isinstance(value, (str, bool)):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError('source configuration contains an unsupported TOML value')


def codex_overrides(source: dict) -> list[str]:
    """Shared routing/launch gate: never silently discard a native restriction. The sandbox is
    read-only (#85), which bars writes and network but reads the whole disk: it covers the known
    policies below at any field value. An external sandbox or an unknown field (a read limit)
    refuses, since read-only does not reproduce it. The deny hook runs even where the user turned
    hooks off."""
    policy = source.get('sandbox_policy')
    if not isinstance(policy, dict):
        raise ValueError('source sandbox policy is unavailable')
    fields = {'workspace-write': {'writable_roots', 'network_access', 'exclude_slash_tmp', 'exclude_tmpdir_env_var'},
              'read-only': {'network_access'}, 'danger-full-access': set()}
    if policy.get('type') not in fields or set(policy) - fields[policy['type']] - {'type'}:
        raise ValueError('source sandbox restrictions cannot be represented by the fork CLI')
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
                'approvals_reviewer': reviewer, 'sandbox_mode': 'read-only'}
    return [part for key, value in settings.items() for part in ('-c', key + '=' + _toml_value(value))] + _codex_hook_args()


def _codex_hook_args() -> list[str]:
    return ['-c', 'features.hooks=true', '-c', 'hooks=' + _toml_value(CODEX_FORK_HOOKS)]


def _codex_hook_ready(argv: list, cwd: str, env: dict, timeout: float = 5) -> bool:
    """Whether native `app-server` hooks/list (`argv`) finds the fork's deny hook enabled and trusted.
    An active profile's features, a managed hooks-only policy or a changed Codex hash drops it
    silently, and read-only does not bound other MCP servers or app connectors (#85). No inference;
    app-server leaves at stdin EOF before answering, so the reply is read first."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', creationflags=NO_WINDOW)
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        proc.stdin.write(''.join(json.dumps(m) + '\n' for m in (
            {'method': 'initialize', 'id': 0, 'params': {'clientInfo': {'name': 'osk', 'version': '0'}}},
            {'method': 'initialized'}, {'method': 'hooks/list', 'id': 1, 'params': {'cwds': [cwd]}})))
        proc.stdin.flush()
        reply = next((m for m in map(json.loads, proc.stdout) if m.get('id') == 1), {})
    except (OSError, ValueError):
        reply = {}
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
    (key, state), = CODEX_FORK_HOOKS['state'].items()
    return any(h.get('key') == key and h.get('enabled') is True and h.get('trustStatus') == 'trusted'
               and h.get('currentHash') == state['trusted_hash']
               for e in (reply.get('result') or {}).get('data') or [] for h in e.get('hooks') or [])


def codex_config(env: dict) -> dict:
    """The fork's native user config.toml, the only layer read for project trust;
    an unreadable file refuses (ValueError/OSError)."""
    import tomllib
    path = Path(env.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    return tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def codex_layers(env: dict, cwd: str) -> dict:
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


def codex_unpinned(layers: dict) -> dict:
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


def cli_query(executable: str, args: list, cwd, env: dict, timeout: float = 5):
    """A local CLI status/version query; never a prompt."""
    return subprocess.run([executable, *args], cwd=cwd, env=env, shell=False, capture_output=True,
                          text=True, encoding='utf-8', timeout=timeout, creationflags=NO_WINDOW)


def git_worktree(cwd: str, env: dict, timeout: float = 5) -> bool:
    git = subprocess.run(['git', '-C', cwd, 'rev-parse', '--is-inside-work-tree'],
                         env={k: v for k, v in env.items() if not k.startswith('GIT_')}, shell=False,
                         capture_output=True, text=True, encoding='utf-8', timeout=timeout, creationflags=NO_WINDOW)
    return not git.returncode and git.stdout.strip() == 'true'


def codex_trusted(cwd: str, config: dict) -> bool:
    """Native exec also accepts a trusted project; mirror its per-path key, never widen it."""
    projects = config.get('projects')
    key = os.path.normcase(os.path.normpath(cwd))
    # Codex writes normalized keys (e:\vv\x for cwd E:\vv\x). A legacy or conflicting
    # case variant refuses rather than guess the native lookup.
    return isinstance(projects, dict) and key in projects and {
        v.get('trust_level') if isinstance(v, dict) else None
        for k, v in projects.items() if os.path.normcase(os.path.normpath(k)) == key} == {'trusted'}


def preflight(vault: Vault, source: dict, executable: str, env: dict, *, require_version: bool = True,
              timeout: float = 5) -> str:
    """Local authentication/version queries only; used by routing and rechecked before inference."""
    harness = source['harness']
    conversation_state.conversation_key(vault, harness, 'preflight')
    expected = source.get('cli_version') if harness == 'codex' else source.get('version')
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

    version = cli_query(executable, ['--version'], cwd, env, timeout)
    if (version.returncode or require_version and not expected or
            expected and not re.search(r'(?<![\w.])' + re.escape(expected) + r'(?![\w.])', version.stdout)):
        raise ValueError('configured CLI version differs from the source harness; fork was not started')
    if harness == 'codex':
        if require_version or source.get('finals'):
            codex_overrides(source)  # turn_context is copied only after a final answer.
        if source.get('model_provider') != 'openai' and (require_version or source.get('model_provider')):
            raise ValueError('only a verified ChatGPT subscription provider is supported')
        config = codex_config(env)
        # Top-level keys are pinned by -c; these would still select a bridge/proxy or a wider policy.
        unpinned = list(codex_unpinned(codex_layers(env, cwd)))
        if env.get('OPENAI_BASE_URL') and env['OPENAI_BASE_URL'].rstrip('/') != OPENAI_ENDPOINT:
            unpinned.append('OPENAI_BASE_URL')
        if unpinned:
            raise ValueError('Codex selects a non-first-party endpoint or policy the fork cannot pin ('
                             + ', '.join(unpinned) + '); no inference started')
        auth = cli_query(executable, ['login', 'status'], cwd, env, timeout)
        if auth.returncode or 'Logged in using ChatGPT' not in auth.stdout + auth.stderr:
            raise ValueError('ChatGPT subscription login is required; no API fallback')
        if not git_worktree(cwd, env, timeout) and not codex_trusted(cwd, config):
            raise ValueError('source directory is not a Git worktree or trusted Codex project; use in-session review')
        if not _codex_hook_ready([executable, *_codex_hook_args(), 'app-server'], cwd, env, timeout):
            raise ValueError('Codex would not run the fork deny hook (hooks off, managed-only or untrusted); '
                             'use in-session review')
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
    auth = cli_query(executable, ['auth', 'status'], cwd, env, timeout)
    status = json.loads(auth.stdout) if auth.returncode == 0 else {}
    if (status.get('authMethod') != 'claude.ai' or not status.get('loggedIn')
            or str(status.get('subscriptionType')).lower() not in {'pro', 'max', 'team', 'enterprise'}):
        raise ValueError('confirmed Claude subscription login is required; no API fallback')
    return executable
