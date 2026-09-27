"""Antigravity 어댑터 — B 등급: 시작·입력·종료 훅과 전사는 있고, 구독 fork는 없다.

2026-09-27 이 기기의 Antigravity 2.0 앱(2.17.0)에서 훅을 실제로 돌려 확인했다.
  · 훅은 customization 루트의 `hooks.json`에서 읽는다 — 작업 폴더는 `.agents/hooks.json`,
    전역은 `~/.gemini/config/hooks.json`(같은 루트의 `mcp_config.json`이 전역 MCP다). 최상위
    키는 훅 이름이고, 그 아래 사건별 목록이다. 턴마다 다시 읽는다.
  · 사건: `SessionStart`(첫 입력이 기록된 뒤), `PreInvocation`(모델을 부를 때마다 —
    `invocationNum`이 0이면 사용자 입력 뒤 첫 호출), `Stop`(전사에 최종 답변이 이미 있다).
  · 명령은 `cmd /c`로 돈다(Windows). 명령 속 큰따옴표는 `\"`로 넘어가 cmd가 경로를 찾지
    못한다 — 공백 없는 경로를 따옴표 없이 쓴다.
  · stdin JSON은 camelCase다: `conversationId`·`workspacePaths`·`transcriptPath`
    (`brain/<ID>/.system_generated/logs/transcript_full.jsonl`). 환경에
    `ANTIGRAVITY_CONVERSATION_ID`를 둔다. cwd는 `hooks.json`이 있는 폴더라 작업 폴더가 아니다.
  · 출력은 JSON이다. 문맥은 `{"injectSteps": [{"ephemeralMessage": …}]}`로 넣고, 그 단계는
    전사에 남아 다음 턴에도 문맥에 있다(16,040자까지 그대로 닿았다). 할 말이 없으면 `{}`.
  · fork가 없다 — `agy` CLI는 앱의 대화를 불러오지 못하고(trajectory not found), print
    모드에 fork가 없다(`/fork`는 원 대화에 이어 붙는 프롬프트가 됐다). 판본을 줄 CLI도 없다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import MCP_NAME, Adapter, command_tokens, hook_line, mentions

HOOK_NAME = MCP_NAME        # hooks.json에서 osk가 소유하는 훅 이름
# 목록이 핸들러 그대로인 사건 — PreToolUse·PostToolUse만 `matcher`·`hooks` 묶음을 쓴다.
_FLAT = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
_POLICY = ("disabled", "disabledTools", "timeout")
_APPS = ("antigravity", "antigravity-cli", "antigravity-ide")     # 2.0 앱·CLI·IDE의 전사 자리


class Antigravity(Adapter):
    name = "antigravity"
    title = "Antigravity"
    cli = ""                # 앱에는 판본을 주는 CLI가 없다 — `agy`는 별도 제품이다
    verified = "2.17.0"
    guide = "Ab"            # 안내서 '선택: Antigravity 연결'
    reload = "Antigravity는 훅을 턴마다 다시 읽는다 — 새 대화에서 확인한다"
    events = {"start": "SessionStart", "input": "PreInvocation", "stop": "Stop"}
    sources: dict = {}      # 세 사건 모두 matcher가 없다
    matchers: dict = {}
    mcp_direct = True
    silence = "{}"          # 아무것도 싣지 않는 호출도 JSON 결과를 낸다

    def home(self) -> Path:
        return Path.home() / ".gemini" / "config"

    def normalize(self, env):
        if "conversationId" not in env or "session_id" in env:
            return env
        out = {**env, "session_id": env["conversationId"]}
        paths = env.get("workspacePaths")
        if isinstance(paths, list) and paths and "cwd" not in env:
            out["cwd"] = paths[0]
        if env.get("transcriptPath") and "transcript_path" not in env:
            out["transcript_path"] = env["transcriptPath"]
        return out

    def fires(self, event, env):
        # 입력 훅은 모델 호출마다 불린다 — 사용자 입력 뒤 첫 호출만 osk의 입력이다.
        return event != "input" or env.get("invocationNum") in (None, 0)

    def claims(self, sid, path):
        return bool(sid) and os.environ.get("ANTIGRAVITY_CONVERSATION_ID") == sid

    def claims_row(self, row):
        return {"step_index", "type", "source"} <= row.keys() and "payload" not in row

    def transcripts(self, sid):
        root = self.home().parent
        return [root / app / "brain" / sid / ".system_generated" / "logs" / "transcript_full.jsonl"
                for app in _APPS]

    def hook_output(self, event, text, system_message=""):
        if event == "stop" or not text:
            return self.silence
        return json.dumps({"injectSteps": [{"ephemeralMessage": text}]}, ensure_ascii=False)

    def hook_notice(self, event, message):
        return self.silence       # 사용자 화면만의 자리가 없다 — 종료를 막는 결정도 싣지 않는다

    def hook_command(self, argv):
        argv = [str(a) for a in argv]
        # Windows에서는 명령을 `cmd /c`에 넘기며 큰따옴표를 `\"`로 바꿔, 인용이 필요한(공백 든)
        # 경로를 cmd가 찾지 못한다. POSIX의 `sh -c`는 인용을 푼다.
        # ponytail: 공백 경로는 거부한다 — 필요해지면 8.3 짧은 경로로 바꿔 등록한다.
        if os.name == "nt" and any(re.search(r"\s", a) for a in argv):
            raise ValueError("Antigravity는 Windows에서 따옴표 든 훅 명령을 풀지 못한다 — 인터프리터와 "
                             "vault 경로에 공백이 없어야 한다. 다른 호스트만 이으려면 --harness로 고른다")
        return hook_line(argv)

    def mcp_files(self):
        return [self.home() / "mcp_config.json"]

    def mcp_servers(self, data):
        return data.get("mcpServers") or {}

    def mcp_write(self, data, python, server):
        servers = dict(data.get("mcpServers") or {})
        if not server:
            servers.pop(MCP_NAME, None)
            return {**data, "mcpServers": servers}
        old = servers.get(MCP_NAME)
        old = old if isinstance(old, dict) else {}
        # 이 vault의 서버를 가리키던 항목은 다 남기고 실행 경로만 바꾼다. 다른 vault의 항목은
        # 정책만 남긴다 — 그 `cwd`·`env`는 그 vault를 가리킨다.
        kept = old if mentions(command_tokens(old), Path(server)) else {k: old[k] for k in _POLICY if k in old}
        servers[MCP_NAME] = {**kept, "command": python, "args": [server]}
        return {**data, "mcpServers": servers}

    def hook_files(self):
        return [self.home() / "hooks.json"]

    def hook_table(self, data):
        table: dict = {}
        for name, spec in data.items():
            if not isinstance(spec, dict):
                continue
            for event, items in spec.items():
                if not isinstance(items, list):
                    continue    # `enabled` 같은 훅 단위 설정
                for item in items:
                    if isinstance(item, dict):
                        group = {"hooks": [item]} if event in _FLAT else dict(item)
                        table.setdefault(event, []).append({**group, "_name": name})
        return table

    def hook_content(self, data, table):
        # 훅 단위 설정과 osk가 모르는 최상위 값은 그대로 두고, 사건 목록만 표에서 다시 짓는다.
        out = {name: ({k: v for k, v in spec.items() if not isinstance(v, list)}
                      if isinstance(spec, dict) else spec) for name, spec in data.items()}
        for event, groups in table.items():
            for group in groups:
                name = group.get("_name", HOOK_NAME)
                body = {k: v for k, v in group.items() if k != "_name"}
                target = out.setdefault(name, {})
                target.setdefault(event, []).extend(body.get("hooks", []) if event in _FLAT else [body])
        # 사건이 하나도 남지 않은 osk 훅은 걷는다. 사용자의 이름은 원래 모양대로 남긴다.
        if isinstance(out.get(HOOK_NAME), dict) and not any(isinstance(v, list) for v in out[HOOK_NAME].values()):
            out.pop(HOOK_NAME)
        return out or None

    def hook_group(self, event, command):
        return {"hooks": [{"type": "command", "command": command, "timeout": 30}], "_name": HOOK_NAME}


ADAPTER = Antigravity()
