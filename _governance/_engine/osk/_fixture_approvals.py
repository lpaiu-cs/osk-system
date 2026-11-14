"""보호영역 생애 fixture — 격리 subprocess 전용.

호출 계약: OSK_VAULT_ROOT가 임시 mini-vault를 가리키는 별도 프로세스에서
실행된다. 실 vault·서버 프로세스의 전역 상태를 일절 건드리지 않는다.
출력: 오류 목록 JSON (빈 목록 = 통과).

소진하는 불변식 (헌법 10조 · 시행령 §6 · Mechanism §3):
  지정→clean, 작업본 수정→pending, 잘못된 base 승인 거부(양측 CAS 승인본 측),
  검토 뒤 작업본 변경 승인 거부(양측 CAS 작업본 측), 정상 승인→clean,
  반려→작업본 원상 복원(추가 파일 제거·승인본 파일 복원), pending에서 해제
  거부, clean에서 해제→unprotected, 미보호 영역 승인·반려 거부.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from osk.vault import Vault  # noqa: E402
from osk import approvals as A, approval_acts, region_tree  # noqa: E402

errs: list[str] = []


def expect(cond, msg):
    if not cond:
        errs.append(msg)


def designate(vault: Vault, reg: str, regdir: Path) -> None:
    """미보호의 거부, 지정 → clean, 이중 지정 거부, 작업본 수정 → pending."""
    # 미보호 영역의 승인·반려·해제는 거부된다 (fail-closed)
    expect(A.state(vault, reg) == "unprotected", "초기 상태가 unprotected 아님")
    for op in (lambda: approval_acts.approve(vault, reg, "sha256:x", expect_work="sha256:y"),
               lambda: approval_acts.revert(vault, reg, "sha256:x", "sha256:y"),
               lambda: approval_acts.unprotect(vault, reg)):
        try:
            op(); errs.append("미보호 영역 조작이 거부되지 않음")
        except ValueError:
            pass

    # 지정 → clean, 초기 승인본
    approval_acts.protect(vault, reg, "fixture 지정")
    expect(A.state(vault, reg) == "clean", "지정 후 clean 아님")
    expect(A.is_protected(vault, reg), "지정 후 미보호로 판정")
    expect(A.region_of(vault, regdir / "d.md") == reg, "region_of가 영역을 못 찾음")
    expect(A.file_matches_baseline(vault, regdir / "d.md"), "지정 직후 승인본 불일치")

    # 이중 지정 거부
    try:
        approval_acts.protect(vault, reg); errs.append("이중 지정이 거부되지 않음")
    except ValueError:
        pass

    # 작업본 수정 → pending
    (regdir / "d.md").write_text("v2", encoding="utf-8")
    (regdir / "added.md").write_text("추가", encoding="utf-8")
    expect(A.state(vault, reg) == "pending", "수정 후 pending 아님")
    expect(not A.file_matches_baseline(vault, regdir / "d.md"), "수정 후 승인본 일치로 오판")


def approve(vault: Vault, reg: str, regdir: Path) -> None:
    """양측 CAS의 거부 셋과 정상 승인 → clean."""
    base = A.approved_hash(vault, reg)
    # 잘못된 base 승인 거부 (양측 CAS — 승인본 측; 작업본 측은 맞게 주어 base만 시험)
    try:
        approval_acts.approve(vault, reg, "sha256:" + "d" * 64, expect_work=region_tree.working_tree_hash(vault, reg))
        errs.append("잘못된 base 승인이 거부되지 않음")
    except ValueError:
        pass
    # expect_work 필수 — None이면 즉시 거부 (양측 CAS를 관례로 우회 불가)
    try:
        approval_acts.approve(vault, reg, base, expect_work=None); errs.append("expect_work 없는 승인이 거부되지 않음")
    except ValueError:
        pass
    # 검토한 작업본이 그 사이 바뀌면 승인 거부 (양측 CAS — 작업본 측)
    stale_work = region_tree.working_tree_hash(vault, reg)
    (regdir / "d.md").write_text("v2-바뀜", encoding="utf-8")
    try:
        approval_acts.approve(vault, reg, base, expect_work=stale_work)
        errs.append("검토 뒤 변경된 작업본 승인이 거부되지 않음")
    except ValueError:
        pass

    # 정상 승인 → clean
    (regdir / "d.md").write_text("v2", encoding="utf-8")
    work = region_tree.working_tree_hash(vault, reg)
    approval_acts.approve(vault, reg, base, expect_work=work, reason="검토 완료")
    expect(A.state(vault, reg) == "clean", "승인 후 clean 아님")
    expect(A.file_matches_baseline(vault, regdir / "d.md"), "승인 후 승인본 불일치")


def release(vault: Vault, reg: str, regdir: Path) -> None:
    """pending에서 해제 거부, 반려 → 원상 복원, clean에서 해제 → unprotected, 최종 정합성."""
    # pending에서 해제 거부
    (regdir / "d.md").write_text("v3", encoding="utf-8")
    (regdir / "junk.md").write_text("junk", encoding="utf-8")
    try:
        approval_acts.unprotect(vault, reg); errs.append("pending에서 해제가 거부되지 않음")
    except ValueError:
        pass

    # 반려 → 작업본 원상 복원 (승인본=v2, added.md는 그 승인본에 있음, junk.md는 제거)
    approval_acts.revert(vault, reg, A.approved_hash(vault, reg), region_tree.working_tree_hash(vault, reg), "되돌림")
    expect(A.state(vault, reg) == "clean", "반려 후 clean 아님")
    expect((regdir / "d.md").read_text() == "v2", "반려가 d.md를 승인본으로 복원 안 함")
    expect((regdir / "added.md").exists(), "반려가 승인본의 added.md를 지움")
    expect(not (regdir / "junk.md").exists(), "반려가 승인 후 추가된 junk.md를 안 지움")

    # clean에서 해제 → unprotected, 이후 승인 거부
    approval_acts.unprotect(vault, reg, "해제")
    expect(A.state(vault, reg) == "unprotected", "해제 후 unprotected 아님")
    expect(not A.is_protected(vault, reg), "해제 후에도 보호로 판정")

    # 정합성 — 최종 상태에 결함 없음
    expect(A.integrity(vault) == [], f"integrity 비어있지 않음: {A.integrity(vault)}")


def main() -> None:
    vault = Vault.open()
    reg = vault.person + "/Delegation"      # mini-vault는 부모 vault의 레이아웃을 따른다
    regdir = vault.root / vault.person / "Delegation"
    regdir.mkdir(parents=True, exist_ok=True)
    (regdir / "d.md").write_text("v1", encoding="utf-8")
    designate(vault, reg, regdir)
    approve(vault, reg, regdir)
    release(vault, reg, regdir)
    print(json.dumps(errs, ensure_ascii=False))


if __name__ == "__main__":
    main()
