"""osk.secrets — 비밀값 정규식 필터.

구현 근거: 시행령 §2 3항(고위험 정형 패턴만, 그 밖의 마스킹 없음),
Mechanism §9(패턴 7종·Python re·양성/음성 fixture와 함께 활성화).
"""
from __future__ import annotations
import os, re, tempfile
from pathlib import Path

PATTERNS = {
    "pem-private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    "aws-access-key": r"\bAKIA[0-9A-Z]{16}\b",
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    "openai-style-key": r"\bsk-[A-Za-z0-9_\-]{20,}\b",
    "slack-token": r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b",
    "google-api-key": r"\bAIza[0-9A-Za-z_\-]{35}\b",
    "bearer-header": r"Authorization:\s*Bearer\s+[A-Za-z0-9_\-.~+/]+=*",
}
_COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}


def filter_text(text: str) -> tuple[str, list[str]]:
    hits = []
    for name, rx in _COMPILED.items():
        text, n = rx.subn(f"[FILTERED:{name}]", text)
        if n:
            hits += [name] * n
    return text, hits


def write_raw(path: Path | str, text: str) -> tuple[Path, list[str]]:
    """`_raw/` 원본 기록의 **단일 통로** (Mechanism §9 · 시행령 §2 3항).

    필터를 우회하는 _raw 쓰기 경로를 두지 않기 위해, 원본을 파일로 남기는
    코드는 장래에도 이 함수를 통과해야 한다 — 치환은 호출자 재량이 아니다.
    vault 밖 경로·`_raw/` 밖 경로는 기록하지 않고 거부한다(fail-closed).
    `text`는 **파일 전문**이며, 기존 파일이 있으면 그 바이트를 접두부로
    보존해야 한다(아래 append 판정). 반환은 (기록한 경로, 적중 패턴 목록)."""
    from .core import resolve_in_root
    from . import graph
    p = resolve_in_root(path)
    if p is None:
        raise ValueError(f"vault 밖 경로 — _raw 기록 거부: {path}")
    if graph.space_of(p)[0] != "raw":
        raise ValueError(f"`_raw/` 밖 경로 — 이 통로로 기록할 수 없다: {p}")
    filtered, hits = filter_text(text)
    # 바이트로 쓴다 — 라운드 범위·상태 해시가 "정규화하지 않은 UTF-8 바이트"로
    # 정의돼 있으므로(Mechanism §8 3~4항), 텍스트 모드의 개행 변환이 끼면
    # Windows에서 쓴 기록만 해시가 달라진다.
    data = filtered.encode("utf-8")
    # Mechanism §9 4항 — `_raw/` 쓰기는 기존 파일의 정확한 바이트를 새 파일의
    # 접두부로 보존하는 append만 허용한다. 판정은 **치환 뒤** 바이트로 한다:
    # 기존 파일도 이 통로를 지나며 치환됐으므로 평소에는 그대로 일치하고,
    # 패턴이 늘어 과거 기록까지 새로 치환돼야 하는 경우에만 접두부가 어긋나
    # 거부된다 — 증거를 소급해 고쳐 쓰는 대신 멈추는 쪽이 감사 추적을 지킨다.
    if p.exists():
        prior = p.read_bytes()
        if not data.startswith(prior):
            raise ValueError(
                f"append 아님 — 기존 {len(prior)}바이트가 접두부로 보존되지 "
                f"않았다. `_raw/`는 불변이며 append만 허용한다: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)      # 원자 교체 — 미치환 중간 상태를 남기지 않는다
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return p, hits


# ── fixture (Mechanism §9 2항 — 활성화 요건) ──────────────────────────
POSITIVE = {
    "pem-private-key": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
    "aws-access-key": "key=AKIAIOSFODNN7EXAMPLE",
    "github-token": "ghp_" + "a" * 36,
    "openai-style-key": "sk-" + "a" * 24,
    "slack-token": "xoxb-1234567890-abc",
    "google-api-key": "AIza" + "a" * 35,
    "bearer-header": "Authorization: Bearer abc.def-ghi",
}
NEGATIVE = [
    "일반 문장에는 아무 일도 없다",
    "ask-me-anything 스레드",             # sk- 오탐 방지
    "AKIA 뒤가 짧으면 AKIA1234 매치 없음",
    "ghp_short",
    "Authorization: Basic abc",
]


def self_test() -> list[str]:
    errs = []
    for name, sample in POSITIVE.items():
        out, hits = filter_text(sample)
        if name not in hits:
            errs.append(f"양성 미탐: {name}")
        if sample == out:
            errs.append(f"양성 무치환: {name}")
    for sample in NEGATIVE:
        out, hits = filter_text(sample)
        if hits:
            errs.append(f"음성 오탐: {sample!r} → {hits}")
    return errs
