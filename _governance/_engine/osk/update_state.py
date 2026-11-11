"""osk.update_state — 이 인스턴스가 무엇을 받았는가 (Mechanism §1-2).

갱신 설정(어느 정본에서 받는가)과 갱신 저널을 읽는다. 저널이 직전 적용 상태의 정본이다: 마친
트랜잭션의 인과 극대가 지금 판과 엔진이 관리하는 파일을 말한다. 받고 적용하는 일은 위의 모듈이 한다."""
from __future__ import annotations
import json, re
from pathlib import Path
from .ledger import causal_maxima, ledger_read
from .vault import Vault


def journal_path(vault: Vault) -> Path:
    """운영 저널 — 권위 대장이 아니다"""
    return vault.ledger / "update.jsonl"


def config_path(vault: Vault) -> Path:
    """인스턴스 소유 로컬 설정"""
    return vault.root / ".osk" / "config.json"


DEFAULT_UPSTREAM = "https://github.com/lpaiu-cs/osk-system.git"


def is_canonical(url: str) -> bool:
    """`url`이 공개 정본 저장소인가 — HTTPS·SSH 표기, `.git`, 대소문자를 가리지 않는다. setup의
    `--sync` 사전 확인과 데몬의 전송 경계가 같은 판정을 쓴다: vault는 정본으로 가지 않는다."""
    def norm(u: str) -> str:
        u = re.sub(r"\.git$", "", u.strip().rstrip("/").lower())
        return re.sub(r"^[a-z+]+://", "", u).split("@")[-1].replace(":", "/")
    return norm(url) == norm(DEFAULT_UPSTREAM)


ATTESTATION = "release.json"


VERSION_RE = r"^v\d+\.\d+\.\d+$"             # 릴리스·태그·자동 탐색이 공유하는 계약


class UpdateError(RuntimeError):
    """갱신 중단 — 인스턴스에 아무것도 쓰지 않는다."""


def load_config(vault: Vault) -> dict:
    if config_path(vault).exists():
        try:
            return json.loads(config_path(vault).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"로컬 설정 판독 실패 {config_path(vault)}: {e}")
    return {"upstream": {"source": "git", "url": DEFAULT_UPSTREAM, "pin": None}}


# ── 저널 — 직전 적용 상태의 정본 ─────────────────────────────────────────
def _done_txns(recs: list[dict]) -> set:
    return {r.get("txn") for r in recs
            if r.get("kind") == "done" and r.get("txn")}


def _is_committed(r: dict, done: set) -> bool:
    """`done`이 그 트랜잭션의 커밋 지점이다(Mechanism §1-2 7항). 커밋 전에 죽어
    남은 `apply`/`remove`는 파일이 pre-image로 복구되므로 상태의 후보가 아니다.
    `txn`이 없는 유산 기록은 그대로 후보다(호환 경계)."""
    return not r.get("txn") or r["txn"] in done


def committed(recs: list[dict]) -> list[dict]:
    """커밋된 기록만 남긴 목록 — **존재 검사 전용**(has_history). 인과 판정에는
    쓰지 않는다: 목록에서 빼면 그 기록이 잇던 parents 간선도 끊겨 남은 기록들이
    거짓 분기가 된다. 판정은 `causal_maxima(..., candidate=...)`로 후보만
    제한한다."""
    done = _done_txns(recs)
    return [r for r in recs if _is_committed(r, done)]


def _committed_maximum(recs: list[dict], rel_path: str) -> dict | None:
    """그 경로의 **커밋된 기록 중 인과 극대**. 조상 관계는 전체 저널의 DAG로
    계산한다(미커밋 기록이 사슬을 끊지 않게).

    극대가 여럿이어도 그것들이 **같은 최종 상태**(kind·hash 동일)를 말하면
    동치로 수렴시킨다 — 다기기가 같은 릴리스를 각각 정상 적용한 뒤 저널이
    union 병합되면 값이 같은 병렬 극대가 생기는데, 그걸 미확정으로 보면
    baseline이 사라져 멀쩡한 파일이 전부 drift로 오판된다. 값이 실제로 갈리는
    경우만 미확정(None)이다."""
    done = _done_txns(recs)
    m = causal_maxima(recs, rel_path, None, "path",
                      candidate=lambda r: _is_committed(r, done))
    if len(m) == 1:
        return m[0]
    if m and all((r.get("kind"), r.get("hash"))
                 == (m[0].get("kind"), m[0].get("hash")) for r in m):
        return m[0]
    return None


def last_applied_hash(recs: list[dict], rel_path: str) -> str | None:
    r = _committed_maximum(recs, rel_path)
    return r.get("hash") if r and r.get("kind") == "apply" else None


def managed_paths(recs: list[dict]) -> dict[str, str]:
    """지금 이 인스턴스가 정본으로부터 관리 중인 경로 → 기준선 해시.
    경로별(커밋된) 인과 극대가 `apply`면 관리 중, `remove`면 삭제됨
    (last_applied_hash와 같은 규율). 삭제 전파의 '직전 관리 집합'이 여기서 나온다."""
    out = {}
    for p in {r.get("path") for r in recs if r.get("path")}:
        r = _committed_maximum(recs, p)
        if r and r.get("kind") == "apply":
            out[p] = r.get("hash")
    return out


def current_version(vault: Vault, recs: list[dict] | None = None) -> str | None:
    """현재 판본 — `done` 기록의 **인과 극대**(물리 마지막 행이 아니다).
    update.jsonl은 union 병합되는 대장이므로 파일 순서는 정본이 아니다
    (core 판정 계약 · sibling `last_applied_hash`와 같은 규율). 병렬 극대도
    version과 릴리스 증빙 해시(attest)가 모두 같으면 같은 판본이다. 증빙 없는
    구판 기록은 유일 극대일 때만 읽고, 동치를 증명할 수 없는 분기는 None."""
    recs = ledger_read(journal_path(vault)) if recs is None else recs
    maxima = causal_maxima(recs, "done", field="kind")
    if len(maxima) <= 1:
        return maxima[0].get("version") if maxima else None
    version, attest = maxima[0].get("version"), maxima[0].get("attest")
    if (isinstance(version, str) and version and isinstance(attest, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", attest)
            and all((r.get("version"), r.get("attest")) == (version, attest)
                    for r in maxima)):
        return version
    return None


def has_history(vault: Vault, recs: list[dict] | None = None) -> bool:
    """이 인스턴스가 이미 갱신 관리 이력을 가졌는가 — **커밋된** apply·remove·
    done 중 하나라도 있으면 참. adopt(최초 편입)의 허용 판정에 쓴다. 커밋 전에
    죽어 남은 기록으로 adopt가 막히면 복구가 불가능해지므로 committed로 본다."""
    recs = committed(ledger_read(journal_path(vault)) if recs is None else recs)
    return any(r.get("kind") in ("apply", "remove", "done") for r in recs)
