"""서명 생애·기각 회복 결속 fixture — 격리 subprocess 전용.

호출 계약: OSK_VAULT_ROOT가 임시 mini-vault를 가리키는 별도 프로세스에서
실행된다. 실 vault·서버 프로세스의 전역 상태를 일절 건드리지 않는다.
출력: 오류 목록 JSON (빈 목록 = 통과).
"""
from __future__ import annotations
import json, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from osk import core  # noqa: E402
import osk.signatures as S  # noqa: E402

errs: list[str] = []
ROOT = core.ROOT
(core.LEDGER / "case").mkdir(parents=True, exist_ok=True)

N1 = b"---\nid: t-1\n---\nv1\n"
N2 = b"---\nid: t-1\n---\nv2\n"
node = ROOT / "n.md"
node.write_bytes(N1)

rec1 = core.ledger_append(core.SIGNATURES, {
    "kind": "sign", "node": "t-1", "path": str(node),
    "hash": core.sha256_bytes(N1), "reason": "fixture"})
if S.status("t-1", node) != "signed":
    errs.append("서명 직후 signed 아님")
node.write_bytes(N2)
if S.status("t-1", node) != "unsigned":
    errs.append("변경 후 unsigned 아님")

D_AT = (datetime.now().astimezone() + timedelta(seconds=2)).replace(microsecond=0)
V_AT = D_AT + timedelta(seconds=1)


def case(no: str, **kw):
    d = {"case_no": no, "status": "adjudicated", "verdict": "기각",
         "parties": ["t-1"], "pre_sign": {"t-1": rec1["rid"]},
         "docketed_at": D_AT.isoformat(), "verdict_at": V_AT.isoformat(),
         "applied": "회복", "schema_version": 1}
    d.update({k: v for k, v in kw.items() if v is not None})
    for k in [k for k, v in kw.items() if v is None]:
        d.pop(k, None)
    body = "\n".join(
        f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}"
        for k, v in d.items())
    (core.LEDGER / "case" / f"{no}.md").write_text(body + "\n\n본문\n", encoding="utf-8")


# 허위 사건 매트릭스 — 전부 차단돼야 한다
case("CASE-2026-9001", status="candidate")
case("CASE-2026-9002", parties=["not-t-1"])
case("CASE-2026-9003", case_no="CASE-2026-9998")
case("CASE-2026-9004", pre_sign={"t-1": "0000-없는rid"})
case("CASE-2026-9005", docketed_at=None)                      # 헤더 필드 누락
for no, tag in [("CASE-2026-9001", "ⓑ 미종결"), ("CASE-2026-9002", "ⓓ 비당사자"),
                ("CASE-2026-9003", "ⓐ case_no 불일치"), ("CASE-2026-9004", "ⓔ pre_sign 불일치"),
                ("CASE-2026-9005", "ⓕ 헤더 누락"), ("CASE-2026-9099", "ⓐ 사건 부재")]:
    try:
        S.restore_for_dismissal("t-1", N1, no)
        errs.append(f"{tag} 차단 실패")
    except ValueError:
        pass

case("CASE-2026-9010")
try:
    S.restore_for_dismissal("t-1", b"---\nid: t-1\n---\nv-wrong\n", "CASE-2026-9010")
    errs.append("② 해시 차단 실패")
except ValueError:
    pass
S.restore_for_dismissal("t-1", N1, "CASE-2026-9010")
if S.status("t-1", node) != "signed":
    errs.append("정상 회복 후 signed 아님")

core.ledger_append(core.SIGNATURES, {
    "kind": "unsign", "node": "t-1", "path": str(node),
    "hash": rec1["hash"], "reason": "fixture 해제"})
case("CASE-2026-9011", pre_sign={"t-1": rec1["rid"]})
try:
    S.restore_for_dismissal("t-1", N1, "CASE-2026-9011")
    errs.append("③ 해제 후 회복 차단 실패")
except ValueError:
    pass

# 경로 주입·형식 위반 사건 번호는 사건부 실재 결속을 우회할 수 없다
for bad in ("../../../../outside/fake", "CASE-9999", "CASE-2026-9001/../x"):
    try:
        S.restore_for_dismissal("t-1", N1, bad)
        errs.append(f"사건 번호 형식 우회 차단 실패: {bad}")
    except ValueError:
        pass

# 인과 해소: 비교 불능 분기 주입 → unsigned → 사용자 재서명 → 해소
fork = {"rid": core._make_rid(core._rid_parts(rec1["rid"])[0], 0xF00),
        "parents": [], "kind": "sign", "node": "t-1",
        "path": str(node), "hash": rec1["hash"], "at": core.now_iso()}
with open(core.SIGNATURES, "a", encoding="utf-8") as f:
    f.write(json.dumps(fork, ensure_ascii=False) + "\n")
if S.status("t-1", node) != "unsigned":
    errs.append("분기 주입 후 fail-closed 안 됨")
node.write_bytes(N1)
core.ledger_append(core.SIGNATURES, {
    "kind": "sign", "node": "t-1", "path": str(node),
    "hash": core.sha256_bytes(N1), "reason": "사용자 재서명 — 분기 해소"})
if S.status("t-1", node) != "signed":
    errs.append("재서명 후 분기 미해소")

print(json.dumps(errs, ensure_ascii=False))
