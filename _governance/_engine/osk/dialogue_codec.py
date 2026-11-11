"""osk.dialogue_codec — dialogue-v1: 사람의 대화는 글자 그대로, 운용 페이로드는 참조로 (시행령 §2).

하네스별 파서가 라운드를 적을 때 쓰는 부호화다. 대화의 허용 목록(`dialogue_content`·`dialogue_event`), 도구
호출과 결과의 참조(`reference`·`result_content`·`tool_evidence`), 비밀값을 거른 직렬화(`dump`). 이 부호화는
고정한다 — 옛 기록의 해시가 그 출력에 묶여 있다."""
from __future__ import annotations
import hashlib
import json
from . import secrets


def _structured(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _redact(text: str) -> str:
    """Filter decoded string literals without rebuilding JSON objects or keys."""
    decoder = json.JSONDecoder()
    parts, last, cursor = [], 0, 0
    while (cursor := text.find('"', cursor)) >= 0:
        try:
            value, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            cursor += 1
            continue
        filtered = _redact(value)
        if filtered != value:
            parts.extend((text[last:cursor], json.dumps(filtered)))
            last = end
        cursor = end
    # Keep every unchanged span, including duplicate keys, whitespace and escapes.
    return secrets.filter_text("".join(parts) + text[last:])[0]


def dump(value) -> str:
    # Decode string boundaries for filtering; raw.write_raw still filters last.
    return _redact(_structured(value))


def reads_file(name: str) -> bool:
    return name.lower().split("__")[-1].split(".")[-1] in {
        "read", "read_file", "readfile", "view_image", "read_node", "read_raw", "read_cited"}


def mixes_output(name: str) -> bool:
    return name.lower().split("__")[-1].split(".")[-1] in {
        "exec", "exec_command", "bash", "shell", "powershell", "run_command", "read_thread_terminal"}


def result_content(call: dict | None, content, locator: str):
    if not call or not (reads_file(call["name"]) or mixes_output(call["name"])):
        return content
    return {"omitted": "read file content (Bylaws 2.2)" if reads_file(call["name"]) else
            "mixed command output; may contain full file content",
            "coverage": "tool-output-reference", "source": call["input"],
            "native_result": locator,
            # A reference hashes the original native result, not its redacted display.
            "sha256": hashlib.sha256(_structured(content).encode("utf-8")).hexdigest()}


def reference(content, locator: str) -> dict:
    return {"coverage": "tool-output-reference", "native_result": locator,
            "sha256": hashlib.sha256(_structured(content).encode("utf-8")).hexdigest()}


def dialogue_content(content, locator: str):
    """dialogue-v1 allowlist. No truncation or paraphrase of visible dialogue.

    Keep this codec stable: old raw hashes bind its output. Transport extensions
    must not silently enlarge the archive; unrecognised content is a reference.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return [{"type": "attachment_ref", **reference(content, locator)}]
    result = []
    for item in content:
        if not isinstance(item, dict):
            result.append({"type": "attachment_ref", **reference(item, locator)})
        elif item.get("type") in {"thinking", "reasoning", "redacted_thinking"}:
            continue
        elif item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            result.append({"type": "text", "text": item["text"]})
        else:
            result.append({"type": "attachment_ref", **reference(item, locator)})
    return result


def dialogue_event(item: dict, locator: str) -> dict | None:
    typ = item.get("type")
    if (typ == "message" and item.get("role") == "assistant"
            and item.get("channel") != "analysis" and item.get("phase") != "analysis"):
        content = dialogue_content(item.get("content", []), locator)
        return {"type": "message", "role": "assistant", "phase": item.get("phase"), "content": content}
    if typ in {"function_call", "custom_tool_call", "tool_use"}:
        return {"type": "tool_call_ref", "name": item.get("name"),
                "call_id": item.get("call_id", item.get("id")),
                **reference(item.get("arguments", item.get("input")), locator)}
    if typ in {"function_call_output", "custom_tool_call_output", "tool_result"}:
        return {"type": "tool_result_ref", "tool_use_id": item.get("call_id", item.get("tool_use_id")),
                "content": reference(item.get("output", item.get("content")), locator)}
    # Explicit allowlist: internal reasoning, summaries, runner/subagent metadata
    # and future native events are not user/assistant dialogue.
    return None


def terminal(item: dict) -> dict:
    result = {k: item[k] for k in ("type", "turn_id", "next_turn_id") if k in item}
    if item.get("error"):
        error = item["error"]
        result["error"] = ({k: error[k] for k in ("message", "code") if k in error}
                           if isinstance(error, dict) else str(error))
    return result


def tool_evidence(items: list, locator: str) -> list[str]:
    """One round reference, not hundreds of repeated operational envelopes.

    Hash scheme is dialogue-v1: ordered call/result reference dictionaries with
    native locations removed. Payload hashes still bind every original value.
    """
    if not items:
        return []
    manifest = []
    for item in items:
        source = {k: v for k, v in item.items() if k != "native_result"}
        if isinstance(source.get("content"), dict):
            source["content"] = {k: v for k, v in source["content"].items() if k != "native_result"}
        manifest.append(source)
    return [dump({"type": "tool_evidence_ref", "native_result": locator,
                   "hash_scheme": "dialogue-tool-manifest-v1",
                   "sha256": hashlib.sha256(_structured(manifest).encode()).hexdigest(),
                   "calls": sum(x["type"] == "tool_call_ref" for x in items),
                   "results": sum(x["type"] == "tool_result_ref" for x in items),
                   "tools": sorted({str(x["name"]) for x in items if x.get("name")})})]
