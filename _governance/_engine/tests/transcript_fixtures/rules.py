"""고정 전사의 허용 목록과, 고정 입력이 그것을 지키는지 보는 지킴이(`problems`).

고정 전사는 이 기기의 실제 전사에서 대화의 내용을 뺀 것이다. 남아도 되는 것은 넷이다: 구조를 이루는
키(`KEYS`), 파서가 값으로 가르는 고정 낱말(`WORDS`), 하네스가 사용자 차례에 끼워 넣는 알림의 꼬리표(`TAGS` —
`harness.base.injected_only`가 찾는 것), 참·거짓·빈 값. 그 밖은 모두 자리표시여야 한다 — 글은 `t<n>`,
식별자는 `u<n>`, 시각은 차례 번호(`CLOCK`)다. 수는 Codex 기록 쪽의 바이트 경계에만 있다. 빈 글과 공백뿐인
글은 그대로다 — 파서가 `strip()`으로 가른다.

내용을 빼는 쪽(실제 전사를 읽어 이 꼴로 바꾸는 코드)은 여기에 없다. 수트는 지킴이만 쓴다. 고정 전사를 만들 때
쓴 것은 이 파일의 이력에 있다(커밋 537765a5의 `Redactor`).
"""
from __future__ import annotations

import json
import re

# 파서가 읽는 키와 행의 봉투. 이 밖의 키는 고정 전사에 있으면 안 된다 — 이름도 내용일 수 있다.
KEYS = frozenset({
    "type", "uuid", "parentUuid", "logicalParentUuid", "sessionId", "timestamp", "isSidechain", "agentId",
    "message", "isCompactSummary", "isMeta", "promptId", "attachment", "operation", "reason", "commandUuid",
    "rewound", "leafUuid", "id", "role", "content", "stop_reason", "text", "name", "input", "tool_use_id",
    "commandMode", "origin", "kind", "source_uuid", "prompt",
    "payload", "history_base", "thread_id", "end_byte_offset", "forked_from_id", "originator", "turn_id",
    "next_turn_id", "last_agent_message", "error", "code", "images", "local_images", "item", "phase",
    "channel", "call_id", "arguments", "output", "namespace", "internal_chat_message_metadata_passthrough",
    "content_item_kinds", "client_id"})
# 값이 JSON을 담은 글일 수 있는 키 — Codex 파서가 `json.loads`로 풀어 본다. 지킴이도 풀어서 본다.
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
# 자리표시와 꼬리표만으로 된 글 — 빈 글과 공백도 든다.
TEXT = re.compile(rf"(?:\s|t\d+|</?(?:{_NAMES})>|<!--|-->)*")
ID = re.compile(r"u\d+")
# 고정 전사의 시각 — 2001-01-02 0시에서 차례 번호만큼 뒤.
CLOCK = re.compile(r"2001-01-02T\d\d:\d\d:\d\d\.000Z")


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
