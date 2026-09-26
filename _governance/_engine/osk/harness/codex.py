"""Codex 어댑터."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import MCP_NAME, Adapter, fold, read_config

_SEMVER = r"\d+\.\d+\.\d+(?:-[\w.]+)?"


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
                matches.extend(folder.glob(prefix + suffix))
        return matches

    def subagent(self, path, sid):
        with Path(path).open("rb") as f:
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
        with Path(path).open("rb") as f:
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
