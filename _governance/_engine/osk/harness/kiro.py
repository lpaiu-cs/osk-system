"""Kiro 어댑터 — B 등급: 시작·입력·종료 훅과 전사는 있고, 구독 fork는 없다.

2026-09-27 이 기기의 Kiro 1.1.70(에이전트 확장 1.1.158) 번들과 로컬 전사로 확인했다.
  · 훅은 `~/.kiro/hooks/*.json`(사용자 전역)과 작업 폴더의 `.kiro/hooks/*.json`에서 읽고,
    신뢰한 작업 폴더에서만 돈다. 형식은 `{"version": "v1", "hooks": [{name, trigger,
    action: {type: "command", command}}]}`다. 명령은 셸(Windows는 cmd.exe)로 돈다.
  · stdin JSON은 `session_id`·`hook_event_name`·`cwd`(입력 훅은 `prompt`)이고, 환경에
    `KIRO_SESSION_ID`를 둔다. 전사 경로는 주지 않는다.
  · 시작·입력 훅이 0으로 끝나면 stdout 평문이 `<HOOK_INSTRUCTION>`에 싸여 문맥에 실린다.
    JSON을 풀지 않고, 사용자 화면만의 자리도 없다. 종료 훅의 출력은 어디에도 실리지 않는다.
  · 전사는 `~/.kiro/sessions/<작업 폴더 해시>/<대화 ID>/messages.jsonl`이다(`osk.transcripts`).
  · MCP 등록 CLI가 없다 — setup이 `~/.kiro/settings/mcp.json`에 직접 병합한다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .base import MCP_NAME, Adapter, command_tokens, mentions

_SEMVER = r"\d+\.\d+\.\d+(?:-[\w.]+)?"
# setup이 소유하는 훅 파일 — Kiro는 hooks 폴더의 JSON을 모두 읽는다.
HOOK_FILE = "osk-system.json"
# MCP 항목에서 사용자가 건 정책(Kiro 번들의 선언 형식) — 실행 경로를 바꿔도 남긴다.
# 실행 자리(`command`·`args`·`cwd`·`env`)와 원격 연결(`url`·`headers`·`oauth`)은 그 서버의 것이다.
_POLICY = ("disabled", "disabledTools", "autoApprove", "timeout", "waitForReady", "versionNegotiation")


class Kiro(Adapter):
    name = "kiro"
    title = "Kiro"
    cli = "kiro"            # IDE 실행기 — 판본(`--version`)만 묻는다
    verified = "1.1.70"
    guide = "Kb"            # 안내서 '선택: Kiro 연결'
    reload = "Kiro는 신뢰한 작업 폴더에서만 훅을 돌린다 — 폴더를 신뢰하고 새 채팅을 연다"
    trust = "Kiro가 작업 폴더를 신뢰할지 물으면 허락한다 — 신뢰하지 않은 폴더에서는 osk 훅이 돌지 않는다"
    sources: dict = {}      # 세 사건 모두 matcher가 없다 — 늘 모든 등록이 돈다
    matchers: dict = {}
    mcp_direct = True

    def home(self) -> Path:
        return Path.home() / ".kiro"

    def claims(self, sid, path):
        # 훅 프로세스의 환경에 자기 대화 ID가 있다. 다른 호스트의 훅이 Kiro 터미널에서
        # 떴어도 대화 ID가 같을 수는 없다.
        return bool(sid) and os.environ.get("KIRO_SESSION_ID") == sid

    def claims_row(self, row):
        # 행은 `{id, timestamp, payload: {type, …}}`다 — Codex 행과 달리 최상위에 `type`이 없다.
        return "type" not in row and isinstance(row.get("payload"), dict) and "type" in row["payload"]

    def transcripts(self, sid):
        return list((self.home() / "sessions").glob(f"*/{sid}/messages.jsonl"))

    def parse_version(self, text):
        # `kiro --version`의 첫 줄이 판본이다(다음 줄은 커밋과 아키텍처).
        m = re.match(_SEMVER + r"$", (text or "").strip().split("\n")[0].strip())
        return m[0] if m else None

    def hook_output(self, event, text, system_message=""):
        return text

    def hook_notice(self, event, message):
        return ""

    def mcp_files(self):
        return [self.home() / "settings" / "mcp.json"]

    def mcp_servers(self, data):
        return data.get("mcpServers") or {}

    def mcp_write(self, data, python, server):
        servers = dict(data.get("mcpServers") or {})
        if not server:
            servers.pop(MCP_NAME, None)
            return {**data, "mcpServers": servers}
        old = servers.get(MCP_NAME)
        old = old if isinstance(old, dict) else {}
        # 경로 갱신이 차단 해제가 되지 않게 한다. 이 vault의 서버를 가리키던 항목은 다 남기고
        # 실행 경로만 바꾼다. 다른 vault의 항목은 정책만 남긴다 — 그 환경은 그 vault를 가리킨다.
        kept = old if mentions(command_tokens(old), Path(server)) else {k: old[k] for k in _POLICY if k in old}
        servers[MCP_NAME] = {**kept, "command": python, "args": [server]}
        return {**data, "mcpServers": servers}

    def hook_files(self):
        folder = self.home() / "hooks"
        others = sorted(p for p in folder.glob("*.json") if p.name != HOOK_FILE) if folder.is_dir() else []
        return [folder / HOOK_FILE, *others]

    def hook_table(self, data):
        hooks = data.get("hooks", [])
        if not isinstance(hooks, list):
            return hooks
        table: dict = {}
        for entry in hooks:
            trigger = entry.get("trigger") if isinstance(entry, dict) else None
            # 사건 이름이 없는 항목도 제자리에 남긴다 — 알아보지 못한 것을 지우지 않는다.
            table.setdefault(trigger if isinstance(trigger, str) else "", []).append({"hooks": [entry]})
        return table

    def hook_content(self, data, table):
        hooks = [e for groups in table.values() for g in groups for e in g["hooks"]]
        # 항목이 없는 훅 파일은 Kiro의 형식 검사를 통과하지 못한다 — 파일을 지운다.
        return {**data, "version": "v1", "hooks": hooks} if hooks else None

    def hook_group(self, event, command):
        return {"hooks": [{"name": f"osk {event}", "trigger": self.events[event],
                           "action": {"type": "command", "command": command}}]}


ADAPTER = Kiro()
