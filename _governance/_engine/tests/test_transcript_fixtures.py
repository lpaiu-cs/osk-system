"""고정 전사의 입출력 — Claude·Codex 파서(`osk.transcripts.read`)에 이 기기의 실제 전사에서 대화의 내용을
빼고 구조만 남긴 것(`transcript_fixtures/`)을 읽혀, 적어 둔 결과(`expected.json`)와 견준다.

적어 둔 결과는 파서를 상태 클래스로 다시 쓰기 전의 엔진(77bcb5e7)이 낸 것이고, 지금 엔진이 낸 것과 같았다.
고정 입력은 파서의 줄을 덮도록 골랐고, 원문과 같은 길(`osk/transcripts.py`에서 실행된 줄의 차례)을 밟는 것만
넣었다. Claude 전사는 턴 규칙의 조합(`rules`) 아홉 가지로 읽는다. Codex 전사의 기록 쪽(`history_base`)은 원래
놓였던 폴더의 꼴대로 임시 `CODEX_HOME`에 놓고, 압축본이던 것은 압축해 놓는다.

빼는 규칙과 지킴이는 `transcript_fixtures/rules.py`에 있다 — 고정 입력의 모든 키와 문자열이 허용 목록에
들거나 자리표시 꼴이어야 한다. 날것의 전사를 고정 입력으로 넣으면 지킴이가 실패한다.

결과는 기기마다 다른 것(파일의 수정 시각, 임시 폴더의 경로)을 빼고 해시로 견준다. 라운드의 차례·끝난 꼴·
남은 꼬리는 따로 적어 두어, 다르면 어디가 다른지 보인다.

같은 말을 두 봉투가 실어 오는 Codex 전사(`_mirror_text`)는 이 기기에 없다 — 옛 봉투(`user_message`)를 쓴
전사가 하나도 없다. 그 짝짓기는 손으로 지은 행으로 본다(`mirrors`).

    python tests/test_transcript_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

import zstandard

HERE = Path(__file__).resolve().parent / "transcript_fixtures"
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
from osk import transcripts  # noqa: E402
import rules  # noqa: E402

EXPECTED = HERE / "expected.json"
# 고정 입력의 파일 이름 — 식별자와 2001년의 날짜 폴더뿐이다.
NAMES = re.compile(rf"(sessions/2001/\d\d/\d\d/|archived_sessions/)?(rollout-\d+-)?{rules.ID.pattern}\.jsonl")


def normalized(result: dict) -> dict:
    """기기마다 다른 것 — 파일의 수정 시각과 임시 폴더의 경로 — 을 뺀 결과."""
    fingerprint = {k: v for k, v in result["native_fingerprint"].items() if k != "mtime_ns"}
    if "history" in fingerprint:
        fingerprint["history"] = [{**{k: v for k, v in page.items() if k != "mtime_ns"},
                                   "path": Path(page["path"]).name} for page in fingerprint["history"]]
    return {**result, "native_fingerprint": fingerprint}


def summary(result: dict) -> dict:
    """결과의 꼴과 해시. 꼴이 같은데 해시가 다르면 글이 다른 것이다."""
    text = json.dumps(normalized(result), ensure_ascii=False, sort_keys=True)
    return {"rounds": [[r["id"], r["completion"], r["end_line"]] for r in result["rounds"]],
            "pending_tail": result["pending_tail"], "tail": (result.get("tail") or {}).get("id"),
            "diagnostics": result["diagnostics"], "reverted": result.get("reverted"),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def outcome(path: Path, harness: str, sid: str, turn_rules: dict | None) -> dict:
    try:
        return summary(transcripts.read(str(path), harness, sid, rules=turn_rules))
    except ValueError as exc:
        return {"raised": f"{type(exc).__name__}: {exc}"}


def run(name: str, fixture: dict) -> list[dict]:
    """고정 입력 하나를 임시 폴더에 놓고 그 조합마다 읽는다."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        for f in (HERE / name).rglob("*.jsonl"):
            rel = f.relative_to(HERE / name).as_posix()
            target = home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel in fixture["compressed"]:
                target.with_name(target.name + ".zst").write_bytes(zstandard.ZstdCompressor().compress(f.read_bytes()))
            else:
                target.write_bytes(f.read_bytes())
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
            return [outcome(home / fixture["path"], fixture["harness"], fixture["conversation"], case["rules"])
                    for case in fixture["cases"]]


def mirrors() -> list[str]:
    """같은 말을 옛 봉투(`user_message` 사건)와 새 봉투(`UserMessage` 항목)가 함께 실어 올 때. 글만 든 둘은
    하나씩 짝지어 하나로 남고(옛 봉투의 꼴로), 어느 쪽에든 글 밖의 것이 들면 짝짓지 않고 둘 다 남는다."""
    def legacy(text="q", **more):
        return {"type": "event_msg", "payload": {"type": "user_message", "message": text, **more}}

    def native(text="q", block=None, **more):
        item = {"type": "UserMessage", "id": "i", "content": [{"type": "text", "text": text, **(block or {})}], **more}
        return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "s", "turn_id": "t",
                                                 "item": item}}

    def users(*inputs) -> tuple:
        """그 입력으로 연 턴의 사용자 글 — 원래의 꼴과 dialogue-v1의 꼴."""
        answer = {"type": "message", "role": "assistant", "phase": "final_answer",
                  "content": [{"type": "output_text", "text": "a"}]}
        rows = [{"type": "session_meta", "payload": {"id": "s"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}, *inputs,
                {"type": "response_item", "payload": answer},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t", "last_agent_message": "a"}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-s.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            result = transcripts.read(str(path), "codex", "s")
        [found] = result["rounds"]
        return found["user"], result["dialogue_v1"]["t"]["user"]

    def apart(*each) -> tuple:
        """짝짓지 않은 입력들이 차례대로 남은 꼴."""
        return tuple("\n\n".join(parts) for parts in zip(*each))

    plain = users(legacy())
    cases = {
        "옛 봉투 뒤의 같은 글은 짝짓는다": (users(legacy(), native()), plain),
        "새 봉투 뒤의 같은 글도 짝짓고, 옛 봉투의 꼴로 남는다": (users(native(), legacy()), plain),
        "하나씩만 짝짓는다": (users(legacy(), legacy(), native()), apart(plain, plain)),
        "다른 글은 짝짓지 않는다": (users(legacy(), native("r")), apart(plain, users(native("r")))),
        "옛 봉투에 그림이 들면 짝짓지 않는다": (users(legacy(images=["x"]), native()),
                                      apart(users(legacy(images=["x"])), users(native()))),
        # 이 기기의 Codex가 쓰는 새 봉투에는 `client_id`가 든다 — 옛 봉투가 함께 오더라도 짝짓지 않는다.
        "새 봉투에 다른 필드가 들면 짝짓지 않는다": (users(legacy(), native(client_id="c")),
                                       apart(plain, users(native(client_id="c")))),
        "글 블록에 다른 것이 들면 짝짓지 않는다": (users(legacy(), native(block={"extra": 1})),
                                      apart(plain, users(native(block={"extra": 1})))),
    }
    return [f"mirror: {name}" for name, (got, want) in cases.items() if got != want]


def guard(expected: dict) -> list[str]:
    """고정 입력이 허용 목록을 지키는가. 자리와 까닭만 내고 글은 내지 않는다."""
    bad = [f"{d.name}: not in expected.json" for d in HERE.iterdir() if d.is_dir() and d.name not in expected
           and d.name != "__pycache__"]
    for name in expected:
        for f in sorted((HERE / name).rglob("*")):
            rel = f.relative_to(HERE / name).as_posix()
            if f.is_dir():
                continue
            if not NAMES.fullmatch(rel):
                bad.append(f"{name}: file name")
                continue
            for n, line in enumerate(f.read_bytes().split(b"\n"), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad.append(f"{name}/{rel}:{n}: not JSON")
                    continue
                bad += [f"{name}/{rel}:{n}{p}" for p in rules.problems(row if isinstance(row, dict) else [row])]
    return bad


def main() -> int:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    bad = guard(expected)
    # 지킴이 자신 — 지어낸 날것의 행(모르는 키, 글, 수)을 놓치면 지킴이가 풀린 것이다.
    if len(rules.problems({"type": "user", "cwd": "t1", "message": {"content": "hello", "id": 20261114}})) != 3:
        bad.append("guard: a raw row passes")
    failures, cases = [], 0
    for name, fixture in expected.items():
        for case, got in zip(fixture["cases"], run(name, fixture)):
            cases += 1
            want = case["expect"]
            if got != want:
                same = {k for k in want if got.get(k) == want[k]}
                failures.append(f"{name} [{case['label']}]: differs in {sorted((set(want) | set(got)) - same)}")
    failures += mirrors()
    for line in bad[:20] + failures[:40]:
        print("FAIL", line)
    print(f"고정 전사: {len(expected)}개, 조합 {cases}개, 그리고 두 봉투의 짝짓기 — 다른 결과 {len(failures)}, "
          f"허용 목록 밖 {len(bad)}")
    return 1 if bad or failures else 0


if __name__ == "__main__":
    sys.exit(main())
