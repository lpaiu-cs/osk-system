"""회귀 수트의 한 갈래: 보호영역의 반려와 이동 — 되돌리기, 이동 기록, 경로의 정체.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, node_text, POSIX_MODE, _raises, rmtree_force, ROOT, skip, VAULT
from osk import core  # noqa: E402


# ── 영역째 삭제된 사고도 반려로 복구된다 (PR #14 리뷰) ─────────────────
def test_revert_recreates_deleted_region():
    """보호영역 디렉터리가 통째로 사라져도(rm -r·동기화 삭제) 승인본이 유효하면
    반려가 디렉터리를 다시 만들어 복원한다 — 보호가 되돌려야 할 가장 기본적인
    사고가 영역 삭제인데 그것만 수동 복구를 요구하면 장치의 뜻이 무너진다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/gone"
    regdir = ROOT / "00_Domain" / "gone"
    try:
        (regdir / "sub").mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("본문A", encoding="utf-8")
        (regdir / "sub" / "b.md").write_text("본문B", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        shutil.rmtree(regdir)                         # 영역째 삭제 사고
        check("영역이 사라지면 pending", A.state(VAULT, reg) == "pending")
        check("작업본 tree는 빈 tree(판정 불능이 아님)",
              region_tree.working_tree_hash(VAULT, reg) is not None)
        approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "복구")
        check("디렉터리가 되살아났다", regdir.is_dir())
        check("파일 내용이 승인본 그대로", (regdir / "a.md").read_text() == "본문A")
        check("하위 디렉터리 파일도 복원", (regdir / "sub" / "b.md").read_text() == "본문B")
        check("복구 후 clean", A.state(VAULT, reg) == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 이동은 이동으로 기록되고, 반려가 이동 전체를 되돌린다 (시행령 §6 4항) ──
def test_move_recorded_and_reverted_in():
    """밖→보호영역 이동을 반려하면 노드는 삭제되지 않고 **원위치로 돌아간다**.
    기록이 없으면 반려가 이동을 '추가'로만 보아 노드를 지운다 — 출발지에는
    복원할 정보가 없으므로 그대로 소실이다(사용자 판정: 이동을 이동으로)."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    src = ROOT / "00_Scope/W1/regr-mvin.md"
    dst = ROOT / "00_Scope/W3/regr-mvin.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvi1", "이동 노드", "본문"),
                       encoding="utf-8")
        approval_acts.protect(VAULT, "00_Scope/W3", "도착지 보호")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        r = node_moves.move_node(VAULT, "regr-mvin", "00_Scope/W3")
        check("이동 성립", r["ok"], r)
        row = A.latest_move(core.ledger_read(A.moves_path(VAULT)), "to",
                             "00_Scope/W3/regr-mvin.md")
        check("이동이 이동으로 기록됐다",
              row is not None and row["from"] == "00_Scope/W1/regr-mvin.md"
              and row["node"] == "260802-zzzz-mvi1", row)
        check("도착 영역은 pending", A.state(VAULT, "00_Scope/W3") == "pending")
        cs = A.changeset(VAULT, "00_Scope/W3")
        check("변경집합이 이동을 이동으로 보인다",
              cs["moves"] == [{"node": "260802-zzzz-mvi1",
                               "from": "00_Scope/W1/regr-mvin.md",
                               "to": "00_Scope/W3/regr-mvin.md"}], cs)
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("노드가 원위치로 돌아왔다(삭제되지 않음)", src.is_file())
        check("도착지에는 없다", not dst.exists())
        check("도착 영역은 clean", A.state(VAULT, "00_Scope/W3") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W3", "정리")
        except Exception: pass
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_move_reverted_out_no_duplicate():
    """보호영역→밖 이동을 반려하면 밖의 사본을 **되가져와** 승인본을 복원한다 —
    승인본 재생성만 하면 같은 id가 두 곳에 남는다(복제 금지)."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    home = ROOT / "00_Scope/W3/regr-mvout.md"
    away = ROOT / "00_Scope/W1/regr-mvout.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-mvo1", "이동 노드", "본문"),
                        encoding="utf-8")
        approval_acts.protect(VAULT, "00_Scope/W3", "출발지 보호")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        check("이동 성립", node_moves.move_node(VAULT, "regr-mvout", "00_Scope/W1")["ok"])
        check("출발 영역은 pending", A.state(VAULT, "00_Scope/W3") == "pending")
        cs = A.changeset(VAULT, "00_Scope/W3")
        check("나간 이동도 이동으로 보인다",
              cs["moves"] and cs["moves"][0]["to"] == "00_Scope/W1/regr-mvout.md",
              cs)
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("노드가 제자리로 돌아왔다", home.is_file())
        check("밖의 사본이 남지 않았다(같은 id 하나뿐)", not away.exists())
        check("출발 영역은 clean", A.state(VAULT, "00_Scope/W3") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W3", "정리")
        except Exception: pass
        home.unlink(missing_ok=True)
        away.unlink(missing_ok=True)


def test_move_return_blocked_origin_occupied():
    """이동으로 온 노드는 지우지 않는다 — 되돌리거나 거부한다. 원위치가 다른
    파일로 차 있으면 반려를 거부하고 아무것도 건드리지 않는다."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    src = ROOT / "00_Scope/W1/regr-mvocc.md"
    dst = ROOT / "00_Scope/W3/regr-mvocc.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvc1", "이동 노드", "본문"),
                       encoding="utf-8")
        approval_acts.protect(VAULT, "00_Scope/W3", "도착지 보호")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        check("이동 성립", node_moves.move_node(VAULT, "regr-mvocc", "00_Scope/W3")["ok"])
        src.write_text("원위치에 새로 생긴 다른 파일", encoding="utf-8")
        check("원위치가 차 있으면 반려 거부",
              _raises(lambda: approval_acts.revert(VAULT, "00_Scope/W3", base,
                                       region_tree.working_tree_hash(VAULT, "00_Scope/W3")))())
        check("이동 노드는 그대로(삭제되지 않음)", dst.is_file())
        check("원위치의 새 파일도 그대로",
              src.read_text() == "원위치에 새로 생긴 다른 파일")
        check("영역은 여전히 pending", A.state(VAULT, "00_Scope/W3") == "pending")
        src.unlink()                       # 자리를 치우면 반려가 진행된다
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("치운 뒤에는 원위치로 복귀", src.is_file())
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W3", "정리")
        except Exception: pass
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_move_chain_reverted_no_duplicate():
    """보호영역을 나온 노드가 미처리인 채 한 번 더 이동하면(양끝 미보호) 그
    hop도 사슬로 기록되고, 반려가 **최종 위치**에서 되가져온다 — 옛 도착지만
    보면 '밖 사본 없음'으로 승인본을 재생성해 같은 id가 둘 남는다(복제 금지)."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    home = ROOT / "00_Scope/W3/regr-mvchain.md"
    hop1 = ROOT / "00_Scope/W1/regr-mvchain.md"
    hop2 = ROOT / "00_Scope/W4/regr-mvchain.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    (ROOT / "00_Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-chn1", "사슬 이동", "본문"),
                        encoding="utf-8")
        approval_acts.protect(VAULT, "00_Scope/W3", "출발지 보호")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        check("hop1 성립", node_moves.move_node(VAULT, "regr-mvchain", "00_Scope/W1")["ok"])
        check("hop2 성립(양끝 미보호)",
              node_moves.move_node(VAULT, "regr-mvchain", "00_Scope/W4")["ok"])
        rows = [r for r in core.ledger_read(A.moves_path(VAULT))
                if r.get("node") == "260802-zzzz-chn1"]
        check("두 hop 모두 사슬로 기록됐다", len(rows) == 2, rows)
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("노드가 제자리로 돌아왔다", home.is_file())
        check("hop1 자리에 사본 없음", not hop1.exists())
        check("hop2 자리에 사본 없음(중복 id 없음)", not hop2.exists())
        check("출발 영역은 clean", A.state(VAULT, "00_Scope/W3") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W3", "정리")
        except Exception: pass
        for f in (home, hop1, hop2):
            f.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "00_Scope/W4", ignore_errors=True)


def test_move_lifecycle_cutoff():
    """승인으로 처분된 과거 이동은 새 반려의 해석 대상이 아니다 — 해석의 경계는
    현재 승인본이 성립한 기록이다. 경계가 없으면 처분된 첫 이탈의 출발지가
    '영역 안 생성분'으로 오독돼 재진입 노드가 **삭제**된다."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    p1 = ROOT / "00_Person/P1"; p2 = ROOT / "00_Person/P2"
    p1.mkdir(parents=True, exist_ok=True); p2.mkdir(parents=True, exist_ok=True)
    node_p1, node_p2 = p1 / "regr-lc.md", p2 / "regr-lc.md"
    w1, w4 = ROOT / "00_Scope/W1/regr-lc.md", ROOT / "00_Scope/W4/regr-lc.md"
    (ROOT / "00_Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        node_p1.write_text(node_text("260802-zzzz-lcx1", "생애 경계", "본문"),
                           encoding="utf-8")
        approval_acts.protect(VAULT, "00_Person", "지정")
        base = A.approved_hash(VAULT, "00_Person")
        check("이탈 이동 성립", node_moves.move_node(VAULT, "regr-lc", "00_Scope/W1")["ok"])
        approval_acts.approve(VAULT, "00_Person", base, region_tree.working_tree_hash(VAULT, "00_Person"), "이탈 수용")
        base2 = A.approved_hash(VAULT, "00_Person")
        check("처분 뒤 방랑 hop 성립(사슬 기록)",
              node_moves.move_node(VAULT, "regr-lc", "00_Scope/W4")["ok"])
        check("재진입 성립", node_moves.move_node(VAULT, "regr-lc", "00_Person/P2")["ok"])
        approval_acts.revert(VAULT, "00_Person", base2, region_tree.working_tree_hash(VAULT, "00_Person"), "재진입 반려")
        check("노드는 직전 위치로 돌아간다(삭제되지 않음)", w4.is_file())
        check("재진입 자리는 비었다", not node_p2.exists())
        check("처분된 과거 자리로 되돌리지 않는다",
              not node_p1.exists() and not w1.exists())
        check("영역은 clean", A.state(VAULT, "00_Person") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Person", "정리")
        except Exception: pass
        for f in (node_p1, node_p2, w1, w4):
            f.unlink(missing_ok=True)
        shutil.rmtree(p1, ignore_errors=True); shutil.rmtree(p2, ignore_errors=True)
        shutil.rmtree(ROOT / "00_Scope/W4", ignore_errors=True)


def test_move_reentry_single_plan():
    """같은 생애에서 나갔다가 **다른 자리로 재진입**한 노드의 반려 — 해석 단위가
    rel이면 한 파일에 계획이 둘 잡혀 복원이 중도에 깨진다(실측: os.replace
    FileNotFoundError로 부분 변경 방치). 노드 단위 해석은 계획을 하나만 세워
    승인본 원적으로 되돌린다."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    p1 = ROOT / "00_Person/P1"; p2 = ROOT / "00_Person/P2"
    p1.mkdir(parents=True, exist_ok=True); p2.mkdir(parents=True, exist_ok=True)
    node_p1, node_p2 = p1 / "regr-re.md", p2 / "regr-re.md"
    w1 = ROOT / "00_Scope/W1/regr-re.md"
    try:
        node_p1.write_text(node_text("260802-zzzz-lcx2", "재진입", "본문"),
                           encoding="utf-8")
        approval_acts.protect(VAULT, "00_Person", "지정")
        base = A.approved_hash(VAULT, "00_Person")
        check("이탈 성립", node_moves.move_node(VAULT, "regr-re", "00_Scope/W1")["ok"])
        check("다른 facet 재진입 성립",
              node_moves.move_node(VAULT, "regr-re", "00_Person/P2")["ok"])
        approval_acts.revert(VAULT, "00_Person", base, region_tree.working_tree_hash(VAULT, "00_Person"), "반려")
        check("노드가 승인본 원적으로 돌아왔다",
              node_p1.is_file() and "본문" in node_p1.read_text())
        check("재진입 자리·경유지에 사본 없음",
              not node_p2.exists() and not w1.exists())
        check("영역은 clean", A.state(VAULT, "00_Person") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Person", "정리")
        except Exception: pass
        for f in (node_p1, node_p2, w1):
            f.unlink(missing_ok=True)
        shutil.rmtree(p1, ignore_errors=True); shutil.rmtree(p2, ignore_errors=True)


def test_move_cutoff_causal_not_clock():
    """생애 경계는 인과다 — rid 크기 비교가 아니다. 시계가 뒤처진 기기의 승인
    이후 이동은 rid가 승인 rid보다 **작지만**, 승인 기록의 `moves_seen`이 보지
    못한 행이므로 해석에 들어간다. 크기 비교였다면 잘려서 반려가 밖의 실물을
    못 본 채 승인본을 재생성해 같은 id가 둘 남는다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/skew"
    regdir = ROOT / "00_Domain" / "skew"
    away = ROOT / "00_Scope/W1/regr-skew.md"
    kept = A.moves_path(VAULT).read_text(encoding="utf-8") if A.moves_path(VAULT).exists() else None
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "regr-skew.md").write_text(
            node_text("260802-zzzz-skw1", "시계 편차", "본문"), encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        prot = A.records(VAULT)[-1]
        check("생애 기록이 이동 경계를 박제한다", "moves_seen" in prot, prot)
        base = A.approved_hash(VAULT, reg)
        # 뒤처진 시계의 기기에서 승인 **이후** 실행된 이동 — rid는 승인보다 작다
        ms = core.rid_parts(prot["rid"])[0] - 60_000
        rid = core._make_rid(ms, 0)
        check("편차 전제: 이동 rid < 승인 rid",
              core.rid_key(rid) < core.rid_key(prot["rid"]))
        with open(A.moves_path(VAULT), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "rid": rid, "parents": core.heads(core.ledger_read(A.moves_path(VAULT))),
                "at": core.now_iso(), "kind": "move", "node": "260802-zzzz-skw1",
                "from": "00_Domain/skew/regr-skew.md",
                "to": "00_Scope/W1/regr-skew.md"}, ensure_ascii=False) + "\n")
        os.replace(regdir / "regr-skew.md", away)
        check("이동 뒤 pending", A.state(VAULT, reg) == "pending")
        cs = A.changeset(VAULT, reg)
        check("표시도 그 이동을 이동으로 본다",
              cs["moves"] and cs["moves"][0]["to"] == "00_Scope/W1/regr-skew.md",
              cs)
        approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg), "반려")
        check("밖의 실물이 회수됐다(같은 id 하나뿐)", not away.exists())
        check("승인본 원적으로 복원", (regdir / "regr-skew.md").is_file())
        check("영역은 clean", A.state(VAULT, reg) == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        away.unlink(missing_ok=True)
        shutil.rmtree(regdir, ignore_errors=True)
        if kept is None:
            A.moves_path(VAULT).unlink(missing_ok=True)
        else:
            A.moves_path(VAULT).write_text(kept, encoding="utf-8")


def test_move_phantom_tail_row_harmless():
    """실패한 마지막 이동의 잔행(기록만 남고 rename 실패)은 무해해야 한다 —
    해석이 마지막 행의 to(의도)가 아니라 **실물이 있는 가장 최근 hop**(사실)을
    현재 위치로 삼는다. 의도를 믿으면 앞선 성공 이동의 실물을 못 보고 승인본을
    재생성해 같은 id가 둘 남는다."""
    from osk import approvals as A, write, node_moves, approval_acts, region_tree
    home = ROOT / "00_Scope/W3/regr-phantom.md"
    w1 = ROOT / "00_Scope/W1/regr-phantom.md"
    w4 = ROOT / "00_Scope/W4/regr-phantom.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    (ROOT / "00_Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-phm2", "잔행", "본문"),
                        encoding="utf-8")
        approval_acts.protect(VAULT, "00_Scope/W3", "지정")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        check("이탈 성립", node_moves.move_node(VAULT, "regr-phantom", "00_Scope/W1")["ok"])
        # 두 번째 이동 — 기록만 남고 rename이 실패한 상황(권한·IO 오류)
        A.record_move(VAULT, "260802-zzzz-phm2", w1, w4)
        check("실물은 여전히 첫 도착지에", w1.is_file() and not w4.exists())
        cs = A.changeset(VAULT, "00_Scope/W3")
        check("표시도 실물 위치를 낸다",
              cs["moves"] and cs["moves"][0]["to"] == "00_Scope/W1/regr-phantom.md",
              cs)
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("실물이 원적으로 회수됐다", home.is_file())
        check("경유지·거짓 도착지에 사본 없음(같은 id 하나뿐)",
              not w1.exists() and not w4.exists())
        check("영역은 clean", A.state(VAULT, "00_Scope/W3") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W3", "정리")
        except Exception: pass
        for f in (home, w1, w4):
            f.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "00_Scope/W4", ignore_errors=True)


def test_move_unrecorded_outside_protection():
    """양끝 다 보호 밖인 이동은 변경집합과 무관하므로 기록하지 않는다
    (시행령 §6 4항의 범위 그대로 — 기록부를 소음으로 채우지 않는다)."""
    from osk import approvals as A, write, node_moves
    src = ROOT / "00_Scope/W1/regr-mvfree.md"
    dst = ROOT / "00_Scope/W3/regr-mvfree.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvf1", "자유 이동", "본문"),
                       encoding="utf-8")
        before = len(core.ledger_read(A.moves_path(VAULT)))
        check("이동 성립", node_moves.move_node(VAULT, "regr-mvfree", "00_Scope/W3")["ok"])
        check("기록부는 그대로", len(core.ledger_read(A.moves_path(VAULT))) == before)
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


# ── 구조 충돌은 첫 쓰기 전에 잡는다 — 부분 복원 금지 (PR #14 리뷰) ─────
def test_revert_structure_conflict_no_partial():
    """작업본에서 파일↔디렉터리가 뒤바뀐 평범한 재구성이면, 반영 도중 실패해
    앞선 파일만 덮인 **부분 복원**이 된다. 준비 단계에서 전수로 잡아 거부하므로
    다른 작업본 변경이 그대로 남는다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/struct"
    regdir = ROOT / "00_Domain" / "struct"
    a, sub = regdir / "a.md", regdir / "sub"
    try:
        sub.mkdir(parents=True, exist_ok=True)
        a.write_text("승인본 A", encoding="utf-8")
        (sub / "b.md").write_text("승인본 B", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)
        a.write_text("작업본 A2", encoding="utf-8")      # 사용자의 변경
        shutil.rmtree(sub)
        sub.write_text("이제 파일이다", encoding="utf-8")  # 디렉터리→파일
        check("반려가 구조 충돌로 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg)))())
        check("앞선 파일이 덮이지 않았다(부분 복원 없음)",
              a.read_text() == "작업본 A2")
        check("충돌 객체도 그대로", sub.read_text() == "이제 파일이다")
        # 반대 방향 — 승인본은 디렉터리 자리, 작업본은 그 자리에 파일
        sub.unlink()
        sub.mkdir()
        (sub / "b.md").write_text("승인본 B", encoding="utf-8")
        approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg), "구조 정리 후 반려")
        check("구조를 바로잡으면 반려 성립", A.state(VAULT, reg) == "clean")
        check("승인본으로 복원", a.read_text() == "승인본 A")
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 부재와 '디렉터리가 아닌 것'은 다르다 (PR #14 리뷰) ─────────────────
def test_region_replaced_by_file_is_pending():
    """부재만 빈 작업본으로 읽는다 — 같은 경로에 일반 파일이 생기면 구조가
    깨진 것이므로 판정 불능(pending)이고 해제도 거부된다. 둘을 접으면 빈
    승인본을 가진 영역이 clean으로 오판되고 해제까지 허용된다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/asfile"
    regdir = ROOT / "00_Domain" / "asfile"
    try:
        regdir.mkdir(parents=True, exist_ok=True)     # 빈 영역 → 승인본도 빈 tree
        approval_acts.protect(VAULT, reg, "지정")
        check("빈 영역은 지정 직후 clean", A.state(VAULT, reg) == "clean")
        regdir.rmdir()
        check("부재는 빈 작업본 — 여전히 clean", A.state(VAULT, reg) == "clean")
        regdir.write_text("디렉터리가 파일로 바뀌었다", encoding="utf-8")
        check("같은 경로가 파일이면 판정 불능",
              region_tree.working_tree_hash(VAULT, reg) is None)
        check("따라서 pending", A.state(VAULT, reg) == "pending")
        check("해제도 거부된다", _raises(lambda: approval_acts.unprotect(VAULT, reg))())
        # 이 상태에서는 작업본을 판정할 수 없어 호출부가 None을 넘기게 된다.
        # 그때 인자 탓으로 보고하면 사용자가 실제 원인(치울 객체)을 못 본다 —
        # 경로 진단이 인자 검사보다 먼저다.
        try:
            approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg))
            msg = ""
        except ValueError as e:
            msg = str(e)
        check("거부 사유가 치울 객체를 가리킨다(인자 탓이 아니라)",
              "치우면" in msg, msg)
    finally:
        if regdir.is_file():
            regdir.unlink()
        regdir.mkdir(parents=True, exist_ok=True)
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 거부된 반려는 부재조차 바꾸지 않는다 (PR #14 리뷰) ─────────────────
def test_refused_revert_leaves_region_absent():
    """CAS가 어긋나 거부된 반려는 무변이어야 한다 — 영역 재생성도 확인을 통과한
    뒤의 첫 mutation이다. 앞에 두면 '작업본을 건드리지 않았다'면서 부재→빈
    디렉터리라는 변경이 이미 일어난다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/absent"
    regdir = ROOT / "00_Domain" / "absent"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("본문", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)
        shutil.rmtree(regdir)                          # 영역째 삭제
        check("영역 경로가 부재", not regdir.exists())
        check("검토한 작업본이 어긋나면 반려 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, base, base))())   # base != 빈 tree
        check("거부 후에도 경로는 여전히 부재(무변)", not regdir.exists())
        approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg), "정상 복구")
        check("맞는 expect_work로는 복구된다",
              regdir.is_dir() and (regdir / "a.md").read_text() == "본문")
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 반려도 검토한 변경집합에만 성립한다 (PR #14 리뷰) ──────────────────
def test_revert_binds_reviewed_changeset():
    """반려는 파괴적이므로 승인과 **같은 결속**을 쓴다 — 확인 프롬프트 사이에
    에이전트가 더 쓴 변경까지 '사용자가 승인한 반려'로 묶여 사라지면 안 된다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/rbind"
    regdir = ROOT / "00_Domain" / "rbind"
    f = regdir / "a.md"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("승인본", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)
        f.write_text("검토한 변경 B", encoding="utf-8")
        reviewed = region_tree.working_tree_hash(VAULT, reg)            # 사용자가 확인한 변경집합
        f.write_text("프롬프트 사이 새 변경 C", encoding="utf-8")   # 에이전트 쓰기
        check("검토하지 않은 C가 섞이면 반려 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, base, reviewed))())
        check("C가 살아 있다", f.read_text() == "프롬프트 사이 새 변경 C")
        check("영역은 여전히 pending", A.state(VAULT, reg) == "pending")
        approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg), "다시 검토 후 반려")
        check("다시 검토한 뒤에는 반려 성립", A.state(VAULT, reg) == "clean")
        check("승인본으로 복원됨", f.read_text() == "승인본")
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 반려는 파괴 직전에 전제를 다시 본다 (PR #14 리뷰) ──────────────────
def test_revert_confirms_before_destroying():
    """반려는 파괴적이다 — 준비 도중 다른 기기의 승인이 들어오면, 기록만 막는
    사후 검사로는 부족하고 **작업본을 건드리기 전에** 거부해야 한다. 그러지
    않으면 사용자가 방금 승인한 내용이 옛 승인본으로 덮여 사라진다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/rcas"
    regdir = ROOT / "00_Domain" / "rcas"
    f = regdir / "a.md"
    real_get = region_tree.store_get
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("v1", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)                   # 승인본 A = v1
        f.write_text("v2", encoding="utf-8")
        other = region_tree.store_tree(VAULT, core.resolve_in_root(VAULT, reg))  # 다른 기기가 승인할 tree B = v2
        check("반려 전 상태는 pending", A.state(VAULT, reg) == "pending")

        def racing(vault, h):             # 준비(blob 적재) 도중 승인 B가 유입
            region_tree.store_get = real_get
            core.ledger_append(A.ledger_path(VAULT), {
                "kind": "approve", "region": reg, "base": base,
                "accepted": other, "reason": "다른 기기(시험)"})
            return real_get(vault, h)
        region_tree.store_get = racing
        check("준비 중 승인본이 바뀌면 반려 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg)))())
        region_tree.store_get = real_get
        check("작업본이 옛 승인본으로 덮이지 않았다", f.read_text() == "v2")
        check("현행 승인본은 유입된 승인의 것", A.approved_hash(VAULT, reg) == other)
        check("유입 승인 기준으로는 clean", A.state(VAULT, reg) == "clean")
    finally:
        region_tree.store_get = real_get
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── revert 미완료(삭제 실패)는 기록하지 않는다 (PR #14 리뷰 [high]) ──────
def test_revert_incomplete_no_record():
    """manifest에 없는 추가 파일의 삭제가 실패하면(권한 등) 작업본이 pending으로
    남고, revert는 복원 완료 확인에 실패해 대장에 기록하지 않는다 — '복원을 마친
    뒤에만 기록'(Mechanism §3 6항) 계약이 위장되지 않는다(fail-closed)."""
    from osk import approvals as A, approval_acts, region_tree
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        skip("삭제 실패 시 revert가 예외로 거부", "root는 삭제 실패를 만들 수 없다")
        return
    reg = "00_Domain/revinc"
    regdir = ROOT / "00_Domain" / "revinc"
    locked = regdir / "locked"
    holder = None
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "keep.md").write_text("keep-v1", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        locked.mkdir()
        junk = locked / "junk.md"
        junk.write_text("junk", encoding="utf-8")                   # pending(추가)
        before = len(A.records(VAULT))
        if POSIX_MODE:
            os.chmod(locked, 0o500)                                 # 삭제 불가
        else:
            # Windows는 디렉터리 권한으로 자식의 unlink를 막지 못한다(실측:
            # `chmod(0o500)` 뒤 mode는 0o555가 되고 unlink는 성공한다). 그래서
            # 이 시나리오가 그 기기에서 **네 검사 모두 FAIL**로 남아 있었고,
            # 반려의 fail-closed 보호가 한 번도 시험되지 않았다. 열린 핸들이
            # 그 자리를 대신한다 — 삭제가 실제로 실패하는 정상 수단이다.
            holder = open(junk, "rb")
        check("삭제 실패 시 revert가 예외로 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                                       region_tree.working_tree_hash(VAULT, reg)))())
        check("삭제 못한 파일이 남아 있다", (locked / "junk.md").exists())
        check("revert 대장 행이 추가되지 않았다", len(A.records(VAULT)) == before)
        check("영역은 여전히 pending(복원 미완료)", A.state(VAULT, reg) == "pending")
    finally:
        if holder is not None:
            holder.close()
        try:
            os.chmod(locked, 0o700)
        except OSError:
            pass
        shutil.rmtree(locked, ignore_errors=True)     # 추가분 정리 → clean
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 반려는 그 이름 그대로의 자리에만 쓰고 지운다 (v3.22.2) ──────────────
def _dir_link(link: Path, target: Path) -> bool:
    """디렉터리 링크 — Windows는 정션(관리자 불필요), POSIX는 심볼릭 링크."""
    if os.name == "nt":
        return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True).returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def _files_of(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


def _txt(p: Path) -> str | None:
    return p.read_text(encoding="utf-8") if p.is_file() else None


def test_revert_path_identity():
    """영역 안 디렉터리가 정션·링크로 바뀌었거나 이름의 대소문자만 바뀌면,
    realpath가 승인본의 그 자리가 아닌 다른 파일을 가리킨다. 구판 반려는 그
    경로로 쓰고 지워 다른 구획·vault 밖 파일을 지우고, 대소문자 변경에서는
    방금 복원한 파일을 삭제 순회가 지웠다(실측). 반려는 쓰기 전에 전부 멈추고,
    열거는 링크 너머로 내려가지 않는다."""
    import contextlib
    from osk import approvals as A, approval_acts, region_tree
    base = ROOT / "00_Domain" / "regr-ident"
    rev = lambda reg: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg))
    ext = Path(tempfile.mkdtemp(prefix="osk-ident-ext-"))
    links = []
    try:
        # 1) 영역 안 디렉터리 → vault 안 다른 구획으로의 링크
        reg, regdir, other = ("00_Domain/regr-ident/j", base / "j",
                              base / "other")
        (regdir / "sub").mkdir(parents=True)
        (regdir / "sub" / "a.md").write_text("승인-a", encoding="utf-8")
        (regdir / "keep.md").write_text("keep", encoding="utf-8")
        other.mkdir()
        for n, t in (("a.md", "남의-a"), ("o1.md", "남의-o1")):
            (other / n).write_text(t, encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        shutil.rmtree(regdir / "sub")
        if _dir_link(regdir / "sub", other):
            links.append(regdir / "sub")
            before, n = _files_of(other), len(A.records(VAULT))
            cs = A.changeset(VAULT, reg) or {}
            check("링크 너머 파일은 영역으로 열거되지 않는다",
                  not any("/sub/" in r for r in cs.get("added", []) + cs.get("modified", []))
                  and A.state(VAULT, reg) == "pending", cs)
            check("링크(vault 안)를 지나는 반려는 쓰기 전에 거부", _raises(lambda: rev(reg))())
            check("다른 구획 파일은 한 바이트도 바뀌지 않았다", _files_of(other) == before)
            check("거부된 반려는 기록되지 않는다", len(A.records(VAULT)) == n)
            os.rmdir(regdir / "sub") if os.name == "nt" else os.unlink(regdir / "sub")
            links.remove(regdir / "sub")
            rev(reg)
            check("링크를 치우면 반려가 승인본을 복원한다",
                  _txt(regdir / "sub" / "a.md") == "승인-a"
                  and A.state(VAULT, reg) == "clean" and _files_of(other) == before)
        else:
            skip("반려 정체성: vault 안 디렉터리 링크", "링크를 만들 수 없는 환경")

        # 2) 영역 안 링크 → vault 밖 폴더
        (ext / "x.md").write_text("밖의 사용자 파일", encoding="utf-8")
        if _dir_link(regdir / "ext", ext):
            links.append(regdir / "ext")
            (regdir / "keep.md").write_text("에이전트 변경", encoding="utf-8")
            cs = A.changeset(VAULT, reg) or {}
            check("vault 밖 링크 너머는 영역 변경집합에 들지 않는다",
                  not any("/ext/" in r for r in cs.get("added", [])), cs)
            rev(reg)
            check("반려 뒤에도 vault 밖 파일이 남는다",
                  _txt(ext / "x.md") == "밖의 사용자 파일"
                  and _txt(regdir / "keep.md") == "keep")
        else:
            skip("반려 정체성: vault 밖 링크", "링크를 만들 수 없는 환경")

        # 3) 대소문자만 바뀐 파일·디렉터리 이름
        creg, cdir = "00_Domain/regr-ident/c", base / "c"
        cdir.mkdir()
        (cdir / "Probe.md").write_text("p", encoding="utf-8")
        insensitive = (cdir / "probe.md").exists()
        folds = os.path.realpath(cdir / "probe.md").endswith("Probe.md")
        (cdir / "Probe.md").unlink()
        if not insensitive:
            skip("반려 정체성: 대소문자 변경", "대소문자를 구분하는 파일시스템")
        else:
            (cdir / "Note.md").write_text("승인 노트", encoding="utf-8")
            (cdir / "Sub").mkdir()
            (cdir / "Sub" / "a.md").write_text("승인 a", encoding="utf-8")
            approval_acts.protect(VAULT, creg, "지정")
            # realpath가 대소문자를 접는 OS(Windows)에서는 접지 않는 realpath(macOS의
            # 동작)로도 한 번 더 돈다 — 거기서는 디렉터리 목록 대조만이 막는다.
            modes = [("", contextlib.nullcontext)] + ([(
                " — realpath 미정규화",
                lambda: mock.patch("os.path.realpath", os.path.abspath))] if folds else [])
            for tag, ctx in modes:
                with ctx():
                    (cdir / "junk.md").write_text("버릴 것", encoding="utf-8")
                    for old, new in (("Note.md", "note.md"), ("Sub", "sub")):
                        n = len(A.records(VAULT))
                        os.rename(cdir / old, cdir / "tmp"); os.rename(cdir / "tmp", cdir / new)
                        check(f"대소문자 변경({old}→{new}) 반려는 쓰기 전에 거부{tag}",
                              _raises(lambda: rev(creg))())
                        check(f"대소문자 변경({old}) 뒤 파일이 하나도 사라지지 않았다{tag}",
                              _txt(cdir / "note.md") == "승인 노트"
                              and _txt(cdir / "sub" / "a.md") == "승인 a"
                              and (cdir / "junk.md").exists() and len(A.records(VAULT)) == n)
                        os.rename(cdir / new, cdir / "tmp"); os.rename(cdir / "tmp", cdir / old)
                    rev(creg)
                    check(f"이름을 되돌리면 반려가 동작한다{tag}",
                          sorted(p.name for p in cdir.iterdir()) == ["Note.md", "Sub"]
                          and A.state(VAULT, creg) == "clean")
            approval_acts.unprotect(VAULT, creg, "정리")
    finally:
        for l in links:
            try: os.rmdir(l) if os.name == "nt" else os.unlink(l)
            except OSError: pass
        for r in ("00_Domain/regr-ident/j", "00_Domain/regr-ident/c"):
            try: approval_acts.unprotect(VAULT, r, "정리")
            except Exception: pass
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(ext, ignore_errors=True)


def test_region_root_is_not_an_excluded_compartment():
    """영역 **루트**가 제외 이름을 담아도 반려가 그 영역을 지우지 않는다 (4차 리뷰).

    `region_files(region_dir)`는 루트 자신에는 걸러내기를 걸지 않고 그 **아래**
    자식 디렉터리와 파일 이름만 거른다. 제외 판정이 vault 상대 **전체** 경로를
    보면 두 해석이 갈린다 — 루트에 제외 이름(구판이 지정한 `…/_scope_memory`)이나
    점 접두(`protect`가 지금도 허용하는 `.foo`)가 든 영역에서 승인본 table의 모든
    행이 빠져 `table={}`가 되는데, 삭제 순회는 그 파일들을 정상 열거하므로 반려가
    영역을 통째로 지우고, 이행 논리가 빈 manifest를 새 `base`로 적어 그 삭제를
    clean한 반려로 확정한다(실측 재현).

    무엇을 망가뜨리면 실패하는가:
      · `excluded_rel`이 다시 전체 경로를 보면 → 사전 대조가 반려를 거부해
        "반려가 성립한다"가 실패한다(그 대조까지 함께 지우면 파일이 사라진다)
      · `revert`의 사전 대조를 지우면 → 세 번째 묶음(해석이 갈린 상황)에서
        영역이 지워지고 삭제가 기록으로 확정된다
    """
    from osk import approvals as A, approval_acts, region_tree
    made, protected = [], []
    try:
        for leaf, legacy in ((".hid", False), ("_scope_memory", True)):
            reg = "00_Domain/" + leaf
            d = ROOT / reg
            d.mkdir(parents=True, exist_ok=True)
            made.append(d)
            body_a, body_b = "원본\n".encode("utf-8"), "둘\n".encode("utf-8")
            (d / "a.md").write_bytes(body_a)
            (d / "b.md").write_bytes(body_b)
            if legacy:
                # 개정으로 이제 **지정할 수 없는** 형태다. 구판이 남긴 영역을
                # 모사하려면 대장에 직접 적는다 — 그것이 이 경로의 현실이다.
                check("제외 이름을 루트로 하는 새 지정은 거부된다 (4차)",
                      _raises(lambda r=reg: approval_acts.protect(VAULT, r, "시험"))())
                core.ledger_append(A.ledger_path(VAULT), {
                    "kind": "protect", "region": reg, "base": None,
                    "accepted": region_tree.store_tree(VAULT, d),
                    "moves_seen": A.moves_boundary(VAULT),
                    "reason": "구판 지정 모사"})
            else:
                approval_acts.protect(VAULT, reg, "4차 리뷰 회귀")
            protected.append(reg)
            base = A.approved_hash(VAULT, reg)
            check(f"[{leaf}] 전제: 지정 직후 clean (4차)",
                  A.state(VAULT, reg) == "clean", A.state(VAULT, reg))
            check(f"[{leaf}] 루트 이름은 제외 판정에 들지 않는다 (4차)",
                  region_tree.legacy_excluded(VAULT, base, reg) == [],
                  region_tree.legacy_excluded(VAULT, base, reg))
            tbl = region_tree.tree_table_for_region(VAULT, reg, base)
            check(f"[{leaf}] 복원 table이 영역 파일을 그대로 담는다 (4차)",
                  tbl is not None and len(tbl) == 2, tbl)

            (d / "a.md").write_bytes("변경\n".encode("utf-8"))
            (d / "c.md").write_bytes("더한 것\n".encode("utf-8"))
            check(f"[{leaf}] 전제: 변경이 pending으로 보인다 (4차)",
                  A.state(VAULT, reg) == "pending", A.state(VAULT, reg))
            rec = approval_acts.revert(VAULT, reg, base, region_tree.working_tree_hash(VAULT, reg), reason="4차")
            check(f"[{leaf}] 반려가 성립한다 (4차)",
                  rec.get("kind") == "revert", rec)
            check(f"[{leaf}] 반려가 영역을 지우지 않고 되돌린다 (4차)",
                  (d / "a.md").read_bytes() == body_a
                  and (d / "b.md").read_bytes() == body_b
                  and not (d / "c.md").exists(),
                  sorted(x.name for x in d.iterdir()))
            check(f"[{leaf}] 되돌린 뒤 clean이다 (4차)",
                  A.state(VAULT, reg) == "clean", A.state(VAULT, reg))

        # 해석이 실제로 갈린 상황에서 **파괴가 시작되지 않는다.** 위 두 묶음은
        # 걸러내기가 옳아진 뒤의 동작이고, 이것은 그 걸러내기가 언젠가 다시
        # 어긋나도 반려가 승인본에 든 살아 있는 파일을 지우지 못한다는 것이다 —
        # 같은 규칙을 두 곳에서 읽는 한 그 어긋남은 되돌아올 수 있다.
        reg = protected[0]
        d = ROOT / reg
        before = sorted(x.name for x in d.iterdir())
        with mock.patch.object(region_tree, "excluded_rel", lambda rel, region: True):
            check("해석이 갈리면 반려가 파괴 전에 멈춘다 (4차)",
                  _raises(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                                           region_tree.working_tree_hash(VAULT, reg), "시험"))())
        check("멈춘 반려는 파일을 하나도 건드리지 않았다 (4차)",
              sorted(x.name for x in d.iterdir()) == before,
              sorted(x.name for x in d.iterdir()))

        # 구판 형태의 영역에서 **빠져나오는 길**은 해제다 — 파괴적이지 않고,
        # 지정이 막힌 뒤에도 남아 있어야 하는 유일한 출구다.
        check("구판 루트 영역은 해제로 빠져나온다 (4차)",
              approval_acts.unprotect(VAULT, protected[1], "이행").get("kind") == "unprotect"
              and A.state(VAULT, protected[1]) == "unprotected", A.state(VAULT, protected[1]))
        protected.pop()
    finally:
        for reg in protected:
            try:
                approval_acts.unprotect(VAULT, reg, "시험 종료")
            except Exception:
                pass
        for d in made:
            if d.exists():
                rmtree_force(d)
