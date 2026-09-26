"""하네스 어댑터의 공통 계약.

기본값은 두 A 등급 호스트(Claude Code·Codex)가 함께 받는 것이다 — 같은 세 훅
스크립트를 같은 사건 이름으로 부르고, 같은 JSON 봉투를 받는다. 어댑터는 다른
것만 덮어쓴다. 지원하지 않는 기능은 기본값(없음)으로 둔다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

MCP_NAME = "osk-system"
# osk의 세 훅 사건과 그 스크립트. 두 호스트가 같은 스크립트를 부른다 — 파일 이름의
# `claude_`는 이미 등록된 명령 경로를 지키려는 이름이다.
SCRIPTS = {"start": "claude_session_start.py", "input": "claude_prompt_submit.py",
           "stop": "capture_stop.py"}


def fold(text: str) -> str:
    """경로 비교용 표기 — `..`를 접고 구분자를 `/`로, Windows에서는 대소문자도 접는다."""
    text = os.path.normpath(text).replace("\\", "/")
    return text.casefold() if os.name == "nt" else text


def command_tokens(entry: dict) -> list[str]:
    """등록된 훅·MCP 명령의 토큰. 실행 형식(`command` + `args`)은 그대로 쓰고, 셸
    형식 한 줄은 그 기기 셸의 규칙으로 가른다."""
    command, args = entry.get("command"), entry.get("args")
    if not isinstance(command, str) or not command.strip():
        return []
    if isinstance(args, list):
        return [command, *(a for a in args if isinstance(a, str))]
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    return [t.strip("\"'") for t in tokens]


def hook_line(argv) -> str:
    """셸 형식 등록 한 줄 — `command_tokens`가 되읽어 같은 토큰을 얻는 표기다. Windows는
    명령줄 규칙(쌍따옴표), 그 밖은 POSIX 셸 인용이다. 경로를 이어 붙이면 공백 든 경로가
    여러 토큰으로 갈린다."""
    argv = [str(a) for a in argv]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def mentions(tokens: list[str], target: Path) -> bool:
    """명령이 그 파일을 부르는가 — 경로 표기(구분자·대소문자)와 무관하게 잰다."""
    want = fold(str(target))
    return any(fold(t) == want for t in tokens)


def before(tokens: list[str], target: Path) -> list[str]:
    """명령에서 그 파일 앞의 토큰 — 해석기와 그 선택지(`py -3.11` 등)다. 없으면 빈 목록."""
    want = fold(str(target))
    return next((tokens[:i] for i, t in enumerate(tokens) if fold(t) == want), [])


def refers(tokens: list[str], name: str) -> str | None:
    """이름이 `name`인 파일을 부르는 토큰 — 다른 vault의 등록을 가리킬 때 쓴다."""
    return next((t for t in tokens if fold(t).endswith("/" + fold(name))), None)


def _groups(hooks):
    """`{"hooks": {사건: [{"matcher", "hooks": [항목…]}]}}`의 (사건, 묶음, 순번, 항목, matcher).
    목록이 아닌 값(Codex의 `[hooks.state]` 같은 표)은 사건이 아니므로 건너뛴다."""
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g, group in enumerate(groups):
            entries = group.get("hooks") if isinstance(group, dict) else None
            matcher = group.get("matcher") if isinstance(group, dict) else None
            for i, entry in enumerate(entries if isinstance(entries, list) else []):
                if isinstance(entry, dict):
                    yield event, g, i, entry, matcher


def read_config(file: Path) -> dict | None:
    """설정 파일(JSON 또는 TOML) — 없으면 None. 읽지 못하면 올린다(호출자가 보인다)."""
    if not file.is_file():
        return None
    text = file.read_text(encoding="utf-8-sig")
    if file.suffix == ".toml":
        import tomllib
        data = tomllib.loads(text)
    else:
        data = json.loads(text)
    return data if isinstance(data, dict) else {}


class Adapter:
    name = ""        # 대화 상태·`--harness`·`.osk/response-growth.json`의 열쇠
    title = ""       # 사람에게 보이는 이름
    cli = ""         # PATH에서 찾는 명령 이름
    verified = ""    # 훅 입출력·전사 형식을 확인한 가장 새 판
    fork = False     # 구독 fork(`osk.response_growth`)를 지원하는가
    guide = ""       # 안내서(GETTING-STARTED)에서 훅을 등록하는 단계
    reload = ""      # 등록했는데 이 기기에서 돌지 않았을 때 사용자가 할 일
    events = {"start": "SessionStart", "input": "UserPromptSubmit", "stop": "Stop"}
    # matcher가 고르는 원인 — SessionStart는 시작 원인마다 맞는 묶음만 돈다. 나머지 사건은
    # matcher 없이 늘 모든 등록이 돈다.
    sources = {"start": ("startup", "resume", "clear", "compact")}

    def fires_on(self, event: str, matcher) -> frozenset[str]:
        """그 matcher의 등록이 불리는 원인 — 원인이 없는 사건은 `{"*"}`(늘 불린다).
        matcher는 원인 이름 전체에 맞는 정규식으로 읽는다. 정규식이 아니면 이름 그대로
        비교한다 — 겹침을 넓게 짐작해 필요한 등록을 지우라고 하지 않는다."""
        domain = self.sources.get(event)
        if not domain:
            return frozenset({"*"})
        if not isinstance(matcher, str) or matcher in ("", "*"):
            return frozenset(domain)
        try:
            pattern = re.compile(matcher)
        except re.error:
            return frozenset(s for s in domain if s == matcher)
        return frozenset(s for s in domain if pattern.fullmatch(s))

    # ── 판별 ──────────────────────────────────────────────────────────────
    def home(self) -> Path:
        raise NotImplementedError

    def ambient_session(self) -> str | None:
        """훅 입력 밖(환경)에서 아는 자기 대화 ID."""
        return None

    def claims(self, sid: str, path: str | None) -> bool:
        """전사를 읽지 않고 자기 대화임을 아는가."""
        return False

    def claims_row(self, row: dict) -> bool:
        """전사의 이 행이 자기 형식임을 보이는가."""
        return False

    def transcripts(self, sid: str) -> list[Path]:
        """그 대화 ID의 전사 후보 — 존재·유일성은 호출자가 본다."""
        return []

    def subagent(self, path: str, sid: str) -> str | None:
        """하위 에이전트의 훅이 뿌리 대화를 이름으로 댔으면 그 사유."""
        return None

    # ── 훅 출력 ───────────────────────────────────────────────────────────
    def hook_output(self, event: str, text: str, system_message: str = "") -> dict:
        """문맥 주입. `system_message`는 모델 문맥이 아니라 사용자 화면에 뜬다."""
        output = {"hookSpecificOutput": {"hookEventName": self.events[event],
                                         "additionalContext": text}}
        if system_message:
            output["systemMessage"] = system_message
        return output

    def hook_notice(self, event: str, message: str) -> dict:
        """사용자 화면에만 뜨는 알림 — 결정·계속 요구를 싣지 않는다."""
        return {"systemMessage": message}

    # ── 판본 ──────────────────────────────────────────────────────────────
    def parse_version(self, text: str) -> str | None:
        """`<cli> --version` 출력의 판본."""
        return None

    def transcript_version(self, path: str) -> str | None:
        """전사에 적힌 호스트 판본."""
        return None

    # ── 등록 ──────────────────────────────────────────────────────────────
    def mcp_command(self, python: Path, server: Path) -> str:
        """이 vault의 MCP 서버를 등록하는 한 줄 — 없으면 빈 문자열."""
        return ""

    def mcp_files(self) -> list[Path]:
        return []

    def mcp_servers(self, data: dict) -> dict:
        """설정 파일 하나의 MCP 서버 표 — 이름 → 항목."""
        return {}

    def hook_files(self) -> list[Path]:
        return []

    def trusted(self, file: Path, event: str, group: int, index: int) -> bool | None:
        """호스트가 그 훅을 신뢰했다고 기록했는가 — 신뢰 관문이 없거나 알 수 없으면 None."""
        return None

    def registrations(self) -> tuple[list[dict], list[dict], list[str]]:
        """(MCP 등록, 훅 등록, 읽지 못한 파일) — 설정 파일을 읽기만 한다."""
        servers, hooks, errors = [], [], []
        for file in self.mcp_files():
            try:
                data = read_config(file)
            except (OSError, ValueError) as exc:
                errors.append(f"{file}: {type(exc).__name__}: {exc}")
                continue
            table = self.mcp_servers(data) if data else {}
            for name, entry in table.items() if isinstance(table, dict) else ():
                if isinstance(entry, dict):
                    servers.append({"file": file, "name": name, "tokens": command_tokens(entry)})
        for file in self.hook_files():
            try:
                data = read_config(file)
            except (OSError, ValueError) as exc:
                errors.append(f"{file}: {type(exc).__name__}: {exc}")
                continue
            for event, g, i, entry, matcher in _groups((data or {}).get("hooks")):
                hooks.append({"file": file, "event": event, "group": g, "index": i,
                              "matcher": matcher, "tokens": command_tokens(entry)})
        return servers, hooks, errors
