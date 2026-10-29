"""일회성 작업 폴더 — 하네스가 대화나 작업마다 새로 만드는 작업 폴더(시행령 §3 9항).

판별은 작업 폴더의 경로 모양으로 한다(Mechanism §6-2 6항): 하네스가 관리하는 뿌리 폴더와
그 아래의 모양이며, 확인한 하네스 판본을 함께 적는다. 엔진의 규칙(`RULES`)과 동기화되는 대장
`_ledger/workspaces.jsonl`의 규칙(`kind: "rule"`)이 같은 형식이다. Git 저장소는 판별하지
않는다 — 그 키는 저장소 동일성으로 구별된다.

일회성 작업 폴더의 프로젝트 키는 결속하지 않고 그 키에 선 결속도 따르지 않는다. 판별은 대화에 묶는다 —
포착이 그 대화의 작업 폴더로 한 번 판별해 커서에 고정하고, 포착·인용·검토가 그것을 쓴다. 프로젝트 키만
받는 쓰기 도구는 그 프로세스가 선 작업 폴더로 판정한다(`enter`·`is_one_off`). 같은 이름의 정상
저장소 키는 다른 프로세스의 일이라 영향을 받지 않는다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import core

LEDGER = core.LEDGER / "workspaces.jsonl"

# 운영 기기(Windows)에서 확인한 규칙이다(2026-10-11, #179; Claude-Data는 2026-10-29). 다른 OS의 경로와
# 규칙이 없는 하네스는 설치하는 에이전트가 같은 형식으로 대장에 더한다(SETUP의 일회성 작업 폴더).
RULES = (
    {"kind": "rule", "harness": "codex", "os": "nt", "version": "0.159.2",
     "root": "~/Documents/Codex", "shape": r"\d{4}-\d{2}-\d{2}/(?:task(?:-\d+)?/)?[^/]+",
     "note": "Codex 데스크톱의 대화 폴더 — 이 폴더에서 돈 Claude Code 세션도 같다"},
    {"kind": "rule", "harness": "claude", "os": "nt", "version": "2.1.281",
     "root": "%APPDATA%/Claude/scratch-workspaces",
     "shape": r"[0-9a-f-]{36}/[0-9a-f-]{36}/scratch-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}",
     "note": "Claude 데스크톱의 scratch 작업 공간"},
    {"kind": "rule", "harness": "claude", "os": "nt", "version": "2.1.308",
     "root": "%LOCALAPPDATA%/Claude-Data/claude_pzs8sxrjxfjjc/scratch-workspaces",
     "shape": r"[0-9a-f-]{36}/[0-9a-f-]{36}/scratch-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}",
     "note": "Claude 데스크톱 3.16부터의 scratch 작업 공간 — Store 설치의 데이터가 Claude-Data로 옮겨졌다"},
    {"kind": "rule", "harness": "antigravity", "os": "nt", "version": "2.17.0",
     "root": "~/.gemini/antigravity/playground", "shape": r"[a-z]+(?:-[a-z]+)+",
     "note": "Antigravity의 playground"},
)


def rules() -> list[dict]:
    """이 기기의 OS에 맞는 규칙. 대장의 규칙(나중 것부터)이 엔진 규칙보다 먼저다 — 같은 뿌리를 다시
    확인해 더한 규칙이 앞선다. 대장을 읽지 못하면 엔진 규칙만 쓴다."""
    try:
        extra = [r for r in core.ledger_read(LEDGER) if r.get("kind") == "rule"]
    except (OSError, ValueError):
        extra = []
    return [r for r in (*reversed(extra), *RULES) if r.get("os") in (None, os.name)]


def _root(rule: dict) -> Path | None:
    text = os.path.expandvars(os.path.expanduser(str(rule.get("root") or "")))
    if not text or "%" in text or "$" in text:     # 정의되지 않은 환경 변수
        return None
    try:
        root = Path(text).resolve()
    except OSError:
        return None
    # 드라이브·파일시스템 뿌리나 홈 폴더 자체는 일회성 작업 폴더의 뿌리가 아니다 — 모든 폴더가 걸린다
    return None if root == root.parent or root == Path.home().resolve() else root


def _line(version) -> tuple:
    """판본의 주·부 번호. 고침 번호만 다른 판은 같은 판본으로 본다 — 작업 공간의 모양은 기능 판에서 바뀐다."""
    return tuple(int(n) for n in re.findall(r"\d+", str(version or ""))[:2])


def judge(harness: str, cwd: str | None, version: str | None = None) -> dict | None:
    """작업 폴더가 어느 규칙의 뿌리 아래면 그 판정, 아니면 None. 뿌리 아래에서 규칙에 없는 모양이나
    규칙을 확인한 판본과 주·부 번호가 다른 하네스 판본은 일회성으로 둔 채 `warning`으로 알린다.
    동기화된 규칙이 깨졌으면 그 규칙만 건너뛴다."""
    if not cwd:
        return None
    try:
        path = Path(cwd).resolve()
    except OSError:
        return None
    for rule in rules():
        try:
            root = _root(rule)
            rel = path.relative_to(root) if root else None
            fits = bool(rel and rel.parts) and re.match(rf"(?:{rule['shape']})(?:/|$)", rel.as_posix())
            name, confirmed = str(rule["harness"]), rule.get("version")
        except (ValueError, KeyError, TypeError, re.error):
            continue
        if rel is None or not rel.parts:
            continue
        if any((p / ".git").exists() for p in [path, *path.parents][:len(rel.parts)]):
            return None             # Git 저장소의 키는 저장소 동일성으로 구별된다
        out = {"one_off": True, "harness": name, "root": rule["root"], "cwd": str(path), "version": version}
        if not fits:
            out["warning"] = (f"`{rule['root']}` 아래의 규칙에 없는 모양({rel.as_posix()})이다 — 결속하지 "
                              "않았다. 일회성 작업 폴더의 규칙을 다시 확인한다(SETUP의 일회성 작업 폴더)")
        elif version and harness == name and _line(version) != _line(confirmed):
            out["warning"] = (f"{harness} {version}은 규칙을 확인한 판본({confirmed})과 다르다 — "
                              "일회성 작업 폴더의 규칙을 다시 확인한다(SETUP의 일회성 작업 폴더)")
        return out
    return None


def add(harness: str, root: str, shape: str, version: str, note: str = "") -> dict:
    """규칙 하나를 대장에 더한다 — 규칙이 없는 하네스의 일회성 작업 폴더을 설치하는 에이전트가
    적는다. 이 기기의 OS에 묶는다. 대장 행도 작업 트리의 변경이라 변경 잠금 안에서 쓴다."""
    re.compile(shape)
    if not all(isinstance(v, str) and v.strip() for v in (harness, root, shape, version)):
        raise ValueError("harness, root, shape and version are required")
    with core.mutation_lock():
        return core.ledger_append(LEDGER, {"kind": "rule", "harness": harness, "os": os.name,
                                           "version": version, "root": root, "shape": shape,
                                           "note": note})


_here: tuple[str, dict | None] | None = None     # (이 프로세스가 선 작업 폴더의 키, 판별)


def enter(cwd: str | None) -> dict | None:
    """이 프로세스가 선 작업 폴더를 판별해 둔다 — 훅은 그 대화의 작업 폴더로 부른다. 부르지 않은
    프로세스(MCP 서버 등)는 처음 물을 때 자기 작업 폴더로 정한다."""
    global _here
    found = judge("", cwd) if cwd else None
    _here = (Path(cwd).name if cwd else "", found)
    return found


def is_one_off(key: str | None) -> bool:
    """`key`가 이 프로세스가 선 일회성 작업 폴더의 키인가. 키 문자열만으로 정하지 않는다 — 같은
    이름의 저장소 키가 다른 세션에 따로 있을 수 있다. 대화를 아는 자리(포착·인용·검토)는 그 대화에
    고정한 판별을 쓴다(`integration`)."""
    if _here is None:
        enter(os.getcwd())
    key = (key or "").strip()
    return bool(key) and key == _here[0] and bool((_here[1] or {}).get("one_off"))
