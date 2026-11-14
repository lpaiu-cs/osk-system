"""고정 전사의 허용 목록 — 이 기기의 실제 전사에서 대화의 내용을 빼는 규칙(`Redactor`)과, 고정 입력이 그
규칙을 지키는지 보는 지킴이(`problems`).

남는 것은 넷이다: 구조를 이루는 키(`KEYS`), 파서가 값으로 가르는 고정 낱말(`WORDS`), 하네스가 사용자
차례에 끼워 넣는 알림의 꼬리표(`TAGS` — `harness.base.injected_only`가 찾는 것), 참·거짓·빈 값. 그 밖은
모두 자리표시가 된다 — 글은 `t<n>`, 식별자는 `u<n>`, 수는 0과 차례 번호다. 모르는 키는 이름도 내용일 수
있어 값과 함께 버린다. 같던 것은 같게, 다르던 것은 다르게 바꾼다: 파서가 두 값을 견주는 자리(같은 턴, 같은
말을 실은 두 봉투)가 그대로 선다. 빈 글과 공백뿐인 글은 그대로 둔다 — 파서가 `strip()`으로 가른다.

시각도 차례 번호가 된다. 고정 입력 하나(그 기록 쪽까지) 안의 서로 다른 시각을 이른 순으로 세워, k째를
2001-01-02 0시의 k초 뒤로 적는다 — 같던 것은 같고 앞뒤는 그대로다. 파서가 시각에서 보는 것은 차례와 같음,
그리고 12시간(`IDLE_CLOSE`)을 넘는 간격이다. 그런 간격이 든 전사는 원문과 다른 길을 밟게 되어 고르지 않는다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

# 파서가 읽는 키와 행의 봉투. 이 밖의 키는 값과 함께 버린다.
KEYS = frozenset({
    "type", "uuid", "parentUuid", "logicalParentUuid", "sessionId", "timestamp", "isSidechain", "agentId",
    "message", "isCompactSummary", "isMeta", "promptId", "attachment", "operation", "reason", "commandUuid",
    "rewound", "leafUuid", "id", "role", "content", "stop_reason", "text", "name", "input", "tool_use_id",
    "commandMode", "origin", "kind", "source_uuid", "prompt",
    "payload", "history_base", "thread_id", "end_byte_offset", "forked_from_id", "originator", "turn_id",
    "next_turn_id", "last_agent_message", "error", "code", "images", "local_images", "item", "phase",
    "channel", "call_id", "arguments", "output", "namespace", "internal_chat_message_metadata_passthrough",
    "content_item_kinds", "client_id"})
# 값이 식별자인 키 — 같은 원문은 같은 식별자가 된다.
ID_KEYS = frozenset({
    "id", "uuid", "parentUuid", "logicalParentUuid", "sessionId", "leafUuid", "promptId", "source_uuid",
    "commandUuid", "agentId", "tool_use_id", "thread_id", "forked_from_id", "turn_id", "next_turn_id", "call_id",
    "client_id"})
# 값이 JSON을 담은 글일 수 있는 키 — Codex 파서가 `json.loads`로 풀어 본다. 푼 것을 빼고 다시 담는다.
JSON_KEYS = frozenset({"arguments", "input"})
# 파서가 결과를 참조로 바꾸는 도구(`_file_read`·`_mixed_output`). 이름은 마지막 마디만 남는다.
TOOLS = frozenset({"read", "read_file", "readfile", "view_image", "read_node", "read_raw", "read_cited",
                   "exec", "exec_command", "bash", "shell", "powershell", "run_command", "read_thread_terminal"})
WORDS = TOOLS | frozenset({
    # Claude: 행의 종류, 끝난 까닭, 블록의 종류, 대기열에 든 말
    "user", "assistant", "attachment", "queue-operation", "last-prompt", "end_turn", "stop_sequence",
    "tool_use", "tool_result", "text", "thinking", "redacted_thinking", "queued_command", "prompt", "human",
    "remove", "absorbed_mid_turn",
    # Codex: 행과 사건의 종류, 응답 항목, 역할과 단계, 턴을 일으킨 것
    "session_meta", "event_msg", "turn_context", "compacted", "response_item", "task_started",
    "user_message", "item_completed", "UserMessage", "task_complete", "turn_aborted", "message",
    "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output", "reasoning",
    "input_text", "output_text", "final_answer", "analysis", "codex_app", "automation_update",
    "send_message_to_thread", "goal.internal_context", "codex_exec"})
TAGS = ("system-reminder", "task-notification", "local-command-stdout", "local-command-stderr",
        "local-command-caveat", "in-app-browser-context", "ci-monitor-event", "recent-project-folders",
        "environment_context", "user_instructions", "heartbeat", "turn_aborted", "codex_internal_context")
_NAMES = "|".join(map(re.escape, TAGS))
MARKED = re.compile(rf"<({_NAMES})\b[^>]*>(.*?)</\1>|<!--(.*?)-->", re.S)
# 자리표시와 꼬리표만으로 된 글 — 빈 글과 공백도 든다.
TEXT = re.compile(rf"(?:\s|t\d+|</?(?:{_NAMES})>|<!--|-->)*")
ID = re.compile(r"u\d+")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
# 고정 전사의 시각 — 2001-01-02 0시에서 차례 번호만큼 뒤.
CLOCK = re.compile(r"2001-01-02T\d\d:\d\d:\d\d\.000Z")
BASE = datetime(2001, 1, 2)


def stamps(rows) -> list[str]:
    """행들의 서로 다른 시각, 이른 순으로 — k째가 2001-01-02 0시의 k초 뒤가 된다."""
    found = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "timestamp" and isinstance(x, str) and STAMP.fullmatch(x):
                    found.add(x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(rows)
    return sorted(found, key=lambda s: (_instant(s), s))


def _instant(s: str) -> float:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


class Redactor:
    """고정 입력 하나(전사와 그 기록 쪽)의 표. `clock`은 그 안의 시각을 이른 순으로(`stamps`) 든 것이다.
    `offsets(thread, offset)`는 앞서 뺀 기록 쪽의 바이트 경계를 옮긴다 — Codex `history_base`가 가리키는 자리다."""

    def __init__(self, clock: list[str], offsets=None):
        self.offsets = offsets
        self.clock = {s: k for k, s in enumerate(clock)}
        self.texts, self.ids, self.nums = {}, {}, {0: 0}

    @staticmethod
    def _table(table: dict, value, form):
        if value not in table:
            table[value] = form(len(table) + 1)
        return table[value]

    def value(self, v):
        if isinstance(v, dict):
            return {k: self.field(k, x) for k, x in v.items() if k in KEYS}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return type(v)(self._table(self.nums, v, int))
        return self.text(v)

    def field(self, k: str, x):
        if k == "history_base" and isinstance(x, dict) and self.offsets:
            return {**self.value(x), "end_byte_offset": self.offsets(x.get("thread_id"), x.get("end_byte_offset"))}
        if not isinstance(x, str) or k not in ID_KEYS | JSON_KEYS | {"timestamp", "name"}:
            return self.value(x)
        if k in ID_KEYS:
            return self.ident(x)
        if k == "timestamp":
            return self.time(x)
        if k == "name":
            last = x.lower().split("__")[-1].split(".")[-1]
            return x if x in WORDS else last if last in TOOLS else self.text(x)
        try:
            return json.dumps(self.value(json.loads(x)), separators=(",", ":"))
        except ValueError:
            return self.text(x)

    def ident(self, s: str) -> str:
        return s and self._table(self.ids, s, "u{}".format)

    def time(self, s: str) -> str:
        if s not in self.clock:
            return self.text(s)
        return (BASE + timedelta(seconds=self.clock[s])).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def text(self, s: str) -> str:
        """글 하나. 알림의 꼬리표는 이름만 남기고, 꼬리표 안과 사이의 글은 자리표시가 된다."""
        if s in WORDS or not MARKED.search(s):
            return s if s in WORDS or not s.strip() else self._table(self.texts, s, "t{}".format)
        parts, last = [], 0
        for m in MARKED.finditer(s):
            inner = self.text(m[2] if m[1] else m[3])
            parts += [self.text(s[last:m.start()]), f"<{m[1]}>{inner}</{m[1]}>" if m[1] else f"<!--{inner}-->"]
            last = m.end()
        return "".join(parts + [self.text(s[last:])])

    def line(self, raw: bytes) -> bytes:
        """전사의 한 줄. 빈 줄은 그대로, JSON 객체가 아닌 줄은 ValueError — 그런 전사는 고르지 않는다."""
        body = raw.rstrip(b"\r\n")
        if not body.strip():
            return raw
        row = json.loads(body)
        if not isinstance(row, dict):
            raise ValueError("not a JSON object")
        return json.dumps(self.value(row), separators=(",", ":")).encode() + raw[len(body):]


def problems(value, where: str = "") -> list[str]:
    """허용 목록 밖의 키·문자열·수가 놓인 자리. 값은 내지 않는다 — 날것의 전사가 들어와도 그 글을 옮기지 않는다.
    수는 Codex 기록 쪽의 바이트 경계(`end_byte_offset`)에만 있다."""
    if isinstance(value, list):
        return [p for x in value for p in problems(x, where)]
    if not isinstance(value, dict):
        return _leaf(value, where)
    bad = [f"{where}: key" for k in value if k not in KEYS]
    for k, x in value.items():
        if k == "end_byte_offset" and type(x) is int:
            continue
        if k in JSON_KEYS and isinstance(x, str) and not allowed(x):
            try:
                x = json.loads(x)
            except ValueError:
                pass
        bad += problems(x, f"{where}/{k if k in KEYS else '?'}")
    return bad


def _leaf(value, where: str) -> list[str]:
    if isinstance(value, str):
        return [] if allowed(value) else [f"{where}: string"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [f"{where}: number"]
    return []


def allowed(s: str) -> bool:
    return s in WORDS or bool(TEXT.fullmatch(s) or ID.fullmatch(s) or CLOCK.fullmatch(s))
