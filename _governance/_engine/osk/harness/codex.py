"""Codex 어댑터."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..transcripts import open_native, rollouts
from .base import MCP_NAME, Adapter, fold, read_config

_SEMVER = r"\d+\.\d+\.\d+(?:-[\w.]+)?"
# `codex exec`의 인자 가운데 `codex exec resume`(0.130 도움말)에 그대로 옮기는 것 — 값을 받는 것과 받지 않는 것.
_RESUME_VALUED = {"-c", "--config", "-m", "--model", "--enable", "--disable"}
_RESUME_FLAGS = {"--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"}
_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


class Codex(Adapter):
    name = "codex"
    title = "Codex"
    cli = "codex"
    # 2026-09-26 이 기기에서 osk 훅이 포착한 가장 새 대화의 판.
    verified = "0.155.0-alpha.16.3"
    fork = True
    guide = "4b"
    reload = ("Codex는 새로 추가하거나 바뀐 훅을 `/hooks`에서 신뢰하기 전까지 건너뛴다 — "
              "신뢰한 뒤 새 세션을 연다")
    status = {"start": "osk: loading scope memory", "input": "osk: checking review cadence",
              "stop": "osk: capturing the finished round"}
    trust = "Codex에서 `/hooks`를 열어 osk 훅 세 개를 검토하고 신뢰한다"
    login = "login"
    # 훅은 세션의 셸로 돈다 — Windows에서는 PowerShell(`-NoProfile -Command`)이다
    # (2026-09-27 0.154.0·0.158.0-alpha.2에서 실측).
    hook_shell = "powershell"

    def home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))

    def ambient_session(self):
        # Codex 안의 도구 셸은 자기 대화 ID를 환경에 둔다. 훅 프로세스에는 없다 —
        # 그때는 전사의 첫 행(`session_meta`)으로 안다.
        return os.environ.get("CODEX_THREAD_ID")

    def claims(self, sid, path):
        return bool(sid) and os.environ.get("CODEX_THREAD_ID") == sid

    def claims_row(self, row):
        return row.get("type") == "session_meta"

    def transcripts(self, sid):
        base, matches = self.home(), []
        for folder, prefix in ((base / "sessions", "*/*/*/"), (base / "archived_sessions", "")):
            for suffix in (f"*-{sid}.jsonl", f"*-{sid}_*.jsonl"):
                # A rollout untouched for 7 days is compressed in place (`.jsonl.zst`, #203).
                matches.extend(rollouts(folder, prefix + suffix))
        return matches

    def subagent(self, path, sid):
        with open_native(path) as f:
            first = next((line for line in f if line.strip()), b"")
        row = json.loads(first) if first.endswith(b"\n") else None
        meta = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
        if isinstance(meta, dict) and meta.get("id") != sid and meta.get("session_id") == sid:
            return "Codex subagent hook names its root conversation; no state changed"
        return None

    def parse_version(self, text):
        m = re.fullmatch(r"codex-cli (" + _SEMVER + r")\s*", text or "")
        return m[1] if m else None

    def transcript_version(self, path):
        # `session_meta`는 대화를 만든 판이다 — 앱을 갱신한 뒤 이어 쓴 옛 대화는 옛 판으로 보인다.
        with open_native(path) as f:
            first = next((line for line in f if line.strip()), b"")
        try:
            row = json.loads(first)
        except ValueError:
            return None
        meta = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
        version = meta.get("cli_version") if isinstance(meta, dict) else None
        return version if isinstance(version, str) and re.fullmatch(_SEMVER, version) else None

    def mcp_argv(self, python, server):
        return ["codex", "mcp", "add", MCP_NAME, "--", str(python), str(server)]

    def mcp_remove_argv(self):
        return ["codex", "mcp", "remove", MCP_NAME]

    def mcp_files(self):
        return [self.home() / "config.toml"]

    def mcp_servers(self, data):
        return data.get("mcp_servers") or {}

    def hook_files(self):
        # 훅은 `hooks.json`에도, `config.toml`의 `[hooks]` 표에도 둘 수 있다.
        return [self.home() / "hooks.json", self.home() / "config.toml"]

    def resume_argv(self, argv, session):
        # `codex exec`만 잇는다 — fork(`exec fork`)와 세션을 남기지 않는 `--ephemeral`은 이을 세션이 없다.
        # resume에는 `--sandbox`가 없어 같은 뜻의 설정(`sandbox_mode`)으로 옮긴다. 정기 실행의 Codex 명령은
        # 사용자가 적으므로, 옮기는 법을 모르는 인자가 하나라도 있으면 잇지 않는다.
        if argv[1:2] != ["exec"]:
            return None
        rest = argv[2:-1] if argv[-1:] == ["-"] else argv[2:]
        out, sandbox, i = [argv[0], "exec", "resume"], [], 0
        while i < len(rest):
            arg, value = rest[i], rest[i + 1] if i + 1 < len(rest) else None
            if arg in _RESUME_FLAGS:
                out.append(arg)
            elif arg in _RESUME_VALUED and value is not None:
                out += [arg, value]
            elif arg in ("-s", "--sandbox") and value in _SANDBOXES:
                sandbox = ["-c", f'sandbox_mode="{value}"']
            else:
                return None
            i += 1 if arg in _RESUME_FLAGS else 2
        # `--sandbox`는 `-c sandbox_mode`보다 앞서지만 옮기면 둘 다 `-c`라 뒤의 것이 이긴다 — 둘이 함께면 잇지
        # 않는다(#420 리뷰: 첫 실행은 읽기 전용, 다시 묻는 턴은 전체 권한이 될 수 있었다).
        if sandbox and any(a in ("-c", "--config") and v.lstrip().startswith("sandbox_mode") for a, v in zip(out, out[1:])):
            return None
        return [*out, *sandbox, session, "-"]

    def cli_candidates(self):
        # Windows 데스크톱 앱이 둔 CLI — 갱신마다 `bin/<해시>/`가 새로 생긴다.
        if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
            return []
        return list((Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))

    def trusted(self, file, event, group, index):
        """신뢰 기록은 `config.toml`의 `[hooks.state."<파일>:<사건>:<묶음>:<순번>"]`에
        `trusted_hash`로 남는다(2026-09-26 이 기기의 Codex에서 확인한 형식). 해시가 지금
        정의와 맞는지는 알 수 없다 — 그것은 실행 기록이 말한다."""
        try:
            config = read_config(self.home() / "config.toml") or {}
        except (OSError, ValueError):
            return None
        hooks = config.get("hooks")
        state = hooks.get("state") if isinstance(hooks, dict) else None
        if not isinstance(state, dict):
            return False
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        want = fold(f"{file}:{snake}:{group}:{index}")
        return any(fold(key) == want and isinstance(value, dict) and value.get("trusted_hash")
                   for key, value in state.items())


ADAPTER = Codex()
