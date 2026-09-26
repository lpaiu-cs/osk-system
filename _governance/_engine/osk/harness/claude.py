"""Claude Code 어댑터."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import MCP_NAME, Adapter

_SEMVER = r"\d+\.\d+\.\d+(?:-[\w.]+)?"


class Claude(Adapter):
    name = "claude"
    title = "Claude Code"
    cli = "claude"
    # 2026-09-26 이 기기에서 osk 훅이 포착한 가장 새 대화의 판(2.1.280 14건, 2.1.281 1건 오류 없이).
    verified = "2.1.281"
    fork = True
    guide = "3b"
    reload = "Claude Code는 훅을 세션을 시작할 때 읽는다 — 새 세션을 열고 `/hooks`에서 확인한다"

    def home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))

    def claims(self, sid, path):
        # 새 대화의 전사는 SessionStart 뒤에야 생길 수 있다 — 파일이 아니라 자리로 안다.
        if not (path and sid and Path(path).stem == sid):
            return False
        return Path(path).resolve().is_relative_to((self.home() / "projects").resolve())

    def claims_row(self, row):
        return bool(row.get("sessionId"))

    def transcripts(self, sid):
        return list((self.home() / "projects").glob(f"*/{sid}.jsonl"))

    def parse_version(self, text):
        m = re.search(r"(?<![\w.])(" + _SEMVER + r")\s*\(Claude Code\)", text or "")
        return m[1] if m else None

    def transcript_version(self, path):
        # 행마다 그 행을 쓴 판을 싣는다 — 이어 쓴 대화는 마지막 행이 지금의 판이다.
        # 전사는 수십 MB가 되므로 끝부분만 읽는다.
        with Path(path).open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - (1 << 16)))
            lines = f.read().split(b"\n")
        for line in reversed(lines[1:] if size > (1 << 16) else lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            version = row.get("version") if isinstance(row, dict) else None
            if isinstance(version, str) and re.fullmatch(_SEMVER, version):
                return version
        return None

    def mcp_argv(self, python, server):
        return ["claude", "mcp", "add", "--scope", "user", MCP_NAME, "--", str(python), str(server)]

    def mcp_remove_argv(self):
        return ["claude", "mcp", "remove", "--scope", "user", MCP_NAME]

    def mcp_files(self):
        # `claude mcp add --scope user`가 쓰는 자리. 설정 폴더를 옮기면 그 안에도 둔다.
        files = [Path.home() / ".claude.json"]
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            files.insert(0, self.home() / ".claude.json")
        return files

    def mcp_servers(self, data):
        return data.get("mcpServers") or {}

    def hook_files(self):
        return [self.home() / "settings.json"]


ADAPTER = Claude()
