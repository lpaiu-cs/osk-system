"""osk.raw — `_raw/` 세션 기록의 라운드 append.

구현 근거: 헌법 4조 3~4항(세션을 가진 scope의 `_raw/`·append-only),
시행령 §2(세션당 정본 하나·포착 범위·라운드 제목), Mechanism §8 3항(라운드
제목 문법과 숫자 H2 escape)·§9 4항(접두부 보존 append).

라운드 index는 **엔진 전속**이다. 호출자가 번호를 고르면 중복·역행이 계약
위반으로 들어오는데(Mechanism §8 3항), 그것은 `[[경로#index]]` 라운드 참조의
동일성을 깨뜨린다 — 이미 쓴 근거가 다른 라운드를 가리키게 된다. 번호를 부여할
수 있는 자리는 기존 기록 전체를 읽는 이곳뿐이므로 여기서 부여한다.

바이트는 `secrets.write_raw`를 지난다 — 비밀값 치환과 접두부 보존을 우회하는
`_raw/` 쓰기 경로를 두지 않기 위해서다(Mechanism §9).
"""
from __future__ import annotations
import re
from pathlib import Path

from .core import ROOT, mutation_lock, posix_rel
# `write`의 `_title_errors`·`_name_collision`을 그대로 쓴다 — 파일명 이식성
# 규칙(Windows 예약명·링크 파서 충돌·ext4 바이트 상한·대소문자 접기)의 정본은
# 한 벌이어야 한다. 여기서 다시 쓰면 두 규칙이 조용히 갈라진다.
from . import secrets, write

# 라운드 제목 — 행 머리의 `## ` 뒤에 양의 십진 index 하나(Mechanism §8 3항).
# escape된 제목(`\## 3`)은 `^## `에 걸리지 않으므로 이 하나로 판정이 끝난다.
_ROUND = re.compile(r"^## (\d+)[ \t]*$", re.M)

# escape 대상 — 대화 본문에 섞인 "숫자 H2와 같은 모양"의 행. 이미 escape된
# 행(`\## 3`)도 함께 잡아 backslash를 하나 더 얹는다. 그래야 escape/unescape가
# 정확히 역연산이 되고, 원문에 `\## 3`이 있어도 회상에서 복원된다.
_NUMERIC_H2 = re.compile(r"^(\\*)(## \d+[ \t]*)$", re.M)


def escape_numeric_h2(text: str) -> str:
    """대화 본문의 숫자 H2를 라운드 제목으로 오독하지 않게 첫 `#`을 escape한다
    (Mechanism §8 3항). 되돌리는 것은 `unescape_numeric_h2`다."""
    return _NUMERIC_H2.sub(lambda m: "\\" + m.group(1) + m.group(2), text)


def unescape_numeric_h2(text: str) -> str:
    """회상 시 escape를 되돌린다 — `escape_numeric_h2`의 역연산."""
    return re.sub(r"^\\(\\*)(## \d+[ \t]*)$",
                  lambda m: m.group(1) + m.group(2), text, flags=re.M)


def rounds(text: str) -> list[int]:
    """기록에 실재하는 라운드 index를 파일 순서대로 돌려준다."""
    return [int(m.group(1)) for m in _ROUND.finditer(text)]


def _next_index(text: str) -> int:
    """다음 라운드 index. 기존 index의 **중복·역행은 계약 위반**이므로(시행령
    §2 7항 · Mechanism §8 3항) 그 위에 덧쓰지 않고 거부한다 — 손상된 기록에
    이어 붙이면 `[[경로#index]]`가 어느 라운드를 가리키는지 정해지지 않는다."""
    seen = rounds(text)
    if seen != sorted(set(seen)) or (seen and seen[0] != 1):
        raise write.WriteError(
            "기록 손상 — 라운드 index가 1부터 단조 증가하지 않는다",
            [f"index 열: {seen} — 중복·역행·시작번호는 계약 위반이므로 "
             f"이어 쓰지 않는다 (시행령 §2 7항)"])
    return (seen[-1] + 1) if seen else 1


def record_path(scope: str, record: str) -> Path:
    """`= Scope/<scope>/_raw/<record>.md`. 이식성 기준으로 같은 이름이 이미
    있으면 **그 파일**을 쓴다 — 대소문자·정규화만 다른 두 정본이 생기면
    시행령 §2 1항의 '세션당 정본 하나'가 기기마다 갈라진다."""
    d = ROOT / "= Scope" / scope / "_raw"
    hit = write._name_collision(d, record) if d.is_dir() else None
    return d / (hit or f"{record}.md")


def _block(index: int, user: str, agent: str) -> str:
    """한 라운드 = user 발화 + 그에 속한 에이전트 응답(시행령 §2 7항)."""
    return (f"## {index}\n\n"
            f"### user\n\n{user.rstrip()}\n\n"
            f"### agent\n\n{agent.rstrip()}\n")


def append_round(session: str, record: str, user: str, agent: str,
                 space: str | None = None) -> dict:
    """세션 기록에 라운드 하나를 append한다.

    착지는 세션 라우팅이 정한다(Mechanism §6-2 6항) — 결속이 없으면 `space`를
    요구하고, 성공하면 그 scope로 세션을 확정한다. `space`의 표기는
    `create_node`와 같은 군집 전체 경로(`"= Scope/W1"`)다 — 같은 표면에서 같은
    값이 같은 뜻이어야 호출자가 `overview`의 `clusters`를 그대로 옮겨 쓴다."""
    if not user.strip() or not agent.strip():
        raise write.WriteError(
            "라운드는 user 발화와 그 응답을 함께 담는다", [
                "user·agent 중 빈 쪽이 있다 — 한 라운드는 user 발화와 그에 "
                "속한 에이전트 응답의 쌍이다 (시행령 §2 7항)"])
    errs = write._title_errors(record)      # 기록 이름이 곧 파일명이다
    if errs:
        raise write.WriteError("기록 이름 부적격 — 쓰지 않았다", errs)

    with mutation_lock():
        bound = write.resolve_session(session)
        dest = _scope_of_space(space) if space else bound
        if not dest:
            raise write.WriteError(
                "착지 미정 — 쓰지 않았다",
                [f"세션 `{session}`의 scope 결속이 없다. `space`를 주면 그 자리에 "
                 f"기록하고 결속을 확정한다. 가능한 space: {_space_list()}"])
        if dest not in _scope_names():
            raise write.WriteError(
                "없는 scope — 쓰지 않았다",
                [f"`{space or dest}`는 `_raw/`를 둘 수 있는 scope가 아니다. "
                 f"가능한 space: {_space_list()}"])

        p = record_path(dest, record)
        prior = p.read_text(encoding="utf-8") if p.exists() else ""
        index = _next_index(prior)
        if prior and not prior.endswith("\n"):
            prior += "\n"
        body = _block(index, escape_numeric_h2(user), escape_numeric_h2(agent))
        # 되돌아온 경로를 쓴다 — 통로가 봉쇄·해소한 그 경로가 실제로 기록된
        # 자리이고, `ROOT`도 해소된 값이라 둘의 상대 계산이 어긋나지 않는다.
        written, hits = secrets.write_raw(p, prior + ("\n" if prior else "") + body)

        if not bound:
            write.bind_session(session, dest, "첫 세션 기록에서 확정")
        rel = posix_rel(written, ROOT)
        # `round_ref`를 그대로 돌려준다 — 이 값이 곧 `derived-from`의 비노드
        # 대상 표기다(Mechanism §8 2항). 호출자가 경로와 index를 조립하다
        # 틀리면 근거 배선이 dangling으로 앉는다.
        return {"ok": True, "path": rel, "index": index,
                "round_ref": f"[[{rel}#{index}]]",
                "filtered": sorted(set(hits))}


def _scope_names() -> list[str]:
    """`_raw/`를 둘 수 있는 scope — 헌법 4조 3항은 세션을 가진 scope에 이
    구획을 두게 한다. Workbench도 자기 운영 세션의 기록을 담는다(Workbench
    계약 2.4)."""
    d = ROOT / "= Scope"
    return sorted(x.name for x in d.iterdir()
                  if x.is_dir() and not x.name.startswith(".")) if d.is_dir() else []


def _space_list() -> str:
    return ", ".join(f"= Scope/{s}" for s in _scope_names())


def _scope_of_space(space: str) -> str | None:
    """`"= Scope/<이름>"` → `<이름>`. 맨 이름은 접지 않는다 — `create_node`가
    맨 이름을 거부하는 것과 같은 규율이며, 같은 표면에서 같은 인자가 다른
    관대함을 가지면 호출자가 규칙을 하나로 배우지 못한다."""
    parts = [x for x in space.strip().strip("/").split("/") if x]
    return parts[1] if len(parts) == 2 and parts[0] == "= Scope" else None
