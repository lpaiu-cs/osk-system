"""회귀 수트의 한 갈래: 색인과 읽기 — 읽은 바이트, 열거의 실패, 같은 id, 지문, 엔진의 판.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import os, shutil, subprocess, tempfile, time
from pathlib import Path
from unittest import mock

from regression.support import _age_all, _append_round, check, node_text, ROOT, skip, VAULT, _w
from osk import cli, core, graph, validate, contract, write, node_moves, nodefile, placement  # noqa: E402


def _mini_engine(tmp: Path) -> Path:
    """엔진 트리의 사본 — 판 관련 시험이 **살아 있는 엔진에 쓰지 않게** 한다.

    처음 판의 시험은 탐침 파일을 실 엔진 디렉토리에 만들었다. 그 창 동안 같은
    기기의 다른 osk 서버 전부가 낡은 것으로 판정되어 쓰기를 거부했고, 수트가
    강제 종료되면 파일이 남아 재기동 전까지 그 상태가 이어졌다(독립 검토 3인
    지적)."""
    dst = tmp / "engine"
    (dst / "osk").mkdir(parents=True, exist_ok=True)
    (dst / "osk" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (dst / "osk" / "b.py").write_text("y = 2\n", encoding="utf-8")
    (dst / "tests").mkdir(exist_ok=True)
    (dst / "tests" / "t.py").write_text("z = 3\n", encoding="utf-8")
    (dst / "osk" / "__pycache__").mkdir(exist_ok=True)
    (dst / "osk" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    return dst


# ── v3.7.1 정확성 불변식 ────────────────────────────────────────────────
# 예제가 아니라 **불변식**으로 적는다. 앞 판의 수트는 신규 57건이 전부
# 통과했는데 아래 셋을 하나도 잡지 못했다 — 독립 검토가 재현해 보내 왔다.
def test_read_is_bound_to_bytes():
    """성공한 `read_node`의 해시는 **반환한 body를 만든 바로 그 바이트**의
    해시다. 아니면 호출자가 읽지 않은 상태의 해시를 CAS에 넣게 되고, 그
    해시가 통과해 외부 편집을 지운다(실측 재현)."""
    import mcp_server as M
    p = ROOT / "00_Scope/W1/regr-bytes.md"
    try:
        r0 = _w(write.create_node, VAULT, "regr-bytes", "결속", "ORIGINAL-A",
                "fable-5", space="00_Scope/W1")
        check("전제: 생성", r0.get("ok"), r0)
        _age_all()
        M._s()                                   # 캐시를 채운다
        raw, st = p.read_bytes(), p.stat()
        p.write_bytes(raw.replace(b"ORIGINAL-A", b"EXTERNAL-B"))
        os.utime(p, (st.st_atime, st.st_mtime))  # mtime 복원 → 지문 동일
        check("전제: 지문이 변경을 못 본다",
              not graph.index_signature(VAULT)[1]
              and M._vault_fingerprint()[0] == M._vault_fingerprint()[0])
        r = M.read_node("regr-bytes")
        check("해시는 돌려준 body의 바이트에서 나온다",
              r.get("hash") == core.sha256_bytes(p.read_bytes()), r.get("hash"))
        check("돌려준 body는 디스크의 실제 내용이다",
              "EXTERNAL-B" in r.get("body", ""), r.get("body"))
        # 그 해시로 덮는 것은 정당하다 — 호출자가 그 상태를 실제로 읽었다.
        # 반대로 **낡은** 해시는 거부돼야 한다.
        stale_hash = core.sha256_bytes(raw)
        r2 = _w(write.update_node, VAULT, "regr-bytes", body="덮기",
                expect_hash=stale_hash)
        check("읽지 않은 상태의 해시는 CAS가 거부한다", r2.get("ok") is False, r2)
        check("거부했으므로 외부 편집이 남아 있다",
              "EXTERNAL-B" in p.read_text(encoding="utf-8"))
    finally:
        p.unlink(missing_ok=True)
        _age_all()


def test_incomplete_scan_refuses_writes():
    """열거 실패가 하나라도 있으면 **쓰기 성공은 0건**이다.

    구판은 `os.scandir` 실패를 조용히 건너뛰었고, 그래서 디렉토리 하나가 안
    읽히면 이름 색인이 그 노드를 못 보고 다른 군집에 같은 이름이 거부 없이
    만들어졌다(실측 재현). 유일성은 전체를 봐야 말할 수 있다."""
    w1, w2 = ROOT / "00_Scope/W1", ROOT / "00_Scope/W2"
    w2.mkdir(parents=True, exist_ok=True)
    victim = w1 / "regr-scan.md"
    made = []
    try:
        if not (w2 / "W2.md").exists():
            _w(write.create_node, VAULT, "W2", "허브", "본문", "fable-5",
               space="00_Scope/W2")
            made.append(w2 / "W2.md")
        r0 = _w(write.create_node, VAULT, "regr-scan", "피해자", "본문", "fable-5",
                space="00_Scope/W1")
        check("전제: 생성", r0.get("ok"), r0)
        real = os.scandir

        def blind(path):
            if str(path) == str(w1):
                raise PermissionError(13, "권한 없음", str(path))
            return real(path)

        os.scandir = blind
        try:
            idx = graph.Index(VAULT)
            check("불완전 관측을 색인이 안다", not idx.complete, idx.scan_errors)
            check("못 본 자리의 이름은 색인에 없다", "regr-scan" not in idx.names)
            for label, call in (
                ("create_node", lambda: _w(
                    write.create_node, VAULT, "regr-scan", "복제", "본문", "fable-5",
                    space="00_Scope/W2")),
                ("update_node", lambda: _w(
                    write.update_node, VAULT, "W2", summary="고침")),
                ("record_candidate", lambda: _w(
                    write.record_candidate, VAULT, "duplication", ["W2", "W1"])),
            ):
                r = call()
                check(f"불완전 관측에서 {label} 거부", r.get("ok") is False, r)
                check(f"{label} 거부가 못 읽은 자리를 지목한다",
                      any("열거 불가" in v for v in r.get("violations", [])), r)
            # 읽기도 불완전 관측을 **접어 두지 않는다** — 키로 붙잡으면 그
            # 자리가 다시 읽히게 된 뒤에도 낡은 그림을 계속 쓴다.
            import mcp_server as M
            M._searcher, M._fingerprint = None, None
            s1 = M._s()
            check("불완전 관측은 캐시 키가 되지 않는다", M._fingerprint is None)
            check("그래서 다음 호출이 다시 짓는다", M._s() is not s1)
        finally:
            os.scandir = real
            import mcp_server as M
            M._searcher, M._fingerprint = None, None
        check("동명 중복이 생기지 않았다",
              len(list(ROOT.rglob("regr-scan.md"))) == 1,
              [str(x) for x in ROOT.rglob("regr-scan.md")])
    finally:
        victim.unlink(missing_ok=True)
        (w2 / "regr-scan.md").unlink(missing_ok=True)
        for f in made:
            f.unlink(missing_ok=True)


def test_broken_is_reported_not_gated():
    """파손 노드는 **생성을 막지 않고 알린다** (v3.7.3에서 뒤집힌 계약).

    v3.7.1은 여기에 관문을 세웠고 근거는 id였다 — *"그 파일의 id를 모르면 새
    id가 겹치지 않는다고 증명할 수 없다"*(구 §2 1항). v3.7.3이 생성 시 전수
    대조를 폐지하면서 그 근거가 사라졌다.

    관문 자체는 다른 일 — 파손이 오래 남지 못하게 하는 강제 함수 — 을 하고
    있었으므로 제자리에 다시 세우려 했으나 **값이 맞지 않는다.** "파손이
    있는가"는 `idx.broken`을 물어야 알고, 그것이 전수 판독을 강제한다. 관문
    하나에 매 생성 6.4초(25,000 노드)를 내는 셈이다.

    그래서 알림에 기댄다. 이 시험은 그 맞바꿈을 **명시적으로 고정**한다 —
    관문이 사라진 것이 사고가 아니라 판단이었음을, 그리고 알림 쪽은 살아
    있어야 함을 함께 적는다.

    무엇을 망가뜨리면 실패하는가:
      · `overview`가 `broken`을 싣지 않게 하면 → 알림이 죽는다
      · 생성에 전수 판독 관문을 되살리면 → 첫 단언이 실패한다(성능 계약)
    """
    import mcp_server as M
    bad = ROOT / "00_Scope/W1/regr-idbad.md"
    try:
        bad.write_text("---\nid: [닫히지 않은\n---\n본문\n", encoding="utf-8")
        check("전제: 파손으로 잡힌다", "regr-idbad" in graph.Index(VAULT).broken)
        r = _w(write.create_node, VAULT, "regr-idnew", "새 노드", "본문", "fable-5",
               space="00_Scope/W1")
        check("파손이 있어도 생성은 된다", r.get("ok"), r)
        _age_all()
        ov = M.overview()
        check("overview가 파손을 알린다", "regr-idbad" in (ov.get("broken") or []),
              ov.get("broken"))
    finally:
        bad.unlink(missing_ok=True)
        (ROOT / "00_Scope/W1/regr-idnew.md").unlink(missing_ok=True)
        _age_all()


# ── id 중복: 읽기와 쓰기가 갈리지 않는다 ────────────────────────────────
def test_duplicate_id_refused():
    """같은 id의 노드가 둘이면 **읽기도 쓰기도 고르지 않는다.**

    구판은 갈렸다 — `read_node`는 자체 순회로 **첫** 일치를, `live_locate`는
    `by_id`로 **마지막**을 골랐다. 사본은 바이트가 같아 `expect_hash`까지
    통과해, 에이전트가 **읽지 않은 파일이 갱신**됐다. 이름 중복에는 있던
    방어가 id에는 없었다. 3자 독립 검토가 각각 재현했고 여기서도 재현했다.

    중복 id는 표면이 만들 수 없다 — 언제나 외부 기원이다(파일 복제·복원·
    병합의 수동 해결). id 무작위부를 늘려도 그 경로는 `new_node_id`를 거치지
    않으므로 남는다.

    무엇을 망가뜨리면 이 시험이 실패하는가:
      · `parse_all`의 `dup_ids` 집계를 지우면 → 전제부터 실패
      · `live_locate`의 중복 거부를 지우면 → 쓰기가 통과해 바이트가 바뀐다
      · `read_node`를 자체 순회(첫 일치)로 되돌리면 → 읽기가 노드를 내준다
      · `resolve`의 ambiguous를 지우면 → 위상 판정이 한쪽을 조용히 고른다
    """
    import mcp_server as M
    p = ROOT / "00_Scope/W1/regr-dupid.md"
    twin = ROOT / "00_Scope/W1/regr-dupid 1.md"
    try:
        r0 = _w(write.create_node, VAULT, "regr-dupid", "요약", "ORIGINAL", "fable-5",
                space="00_Scope/W1")
        check("전제: 생성", r0.get("ok"), r0)
        nid = r0["id"]
        twin.write_bytes(p.read_bytes())      # 옵시디언 "사본 만들기"와 같은 상태
        _age_all()
        before_p, before_twin = p.read_bytes(), twin.read_bytes()
        check("전제: 바이트가 같다", before_p == before_twin)

        i = graph.Index(VAULT)
        check("색인이 id 중복을 싣는다", nid in i.dup_ids, i.dup_ids)
        check("중복은 두 경로를 모두 지목한다",
              len(i.dup_ids[nid]) == 2, i.dup_ids.get(nid))
        check("경로는 POSIX 표기다",
              all("\\" not in s for s in i.dup_ids[nid]), i.dup_ids[nid])
        check("resolve는 모호로 답한다", i.resolve(nid) == ("ambiguous",),
              i.resolve(nid))

        rr = M.read_node(nid)
        check("읽기는 노드를 내주지 않는다", "error" in rr, rr)
        check("읽기가 이유를 말한다", "id" in rr.get("error", ""), rr)

        r1 = _w(write.update_node, VAULT, nid, body="바뀐 본문")
        check("쓰기는 거부한다", r1.get("ok") is False, r1)
        check("거부가 중복을 지목한다", "같은 id" in str(r1), r1)

        # 세 번째 해석 경로도 고르지 않는다. 지금 생산 호출자는 없지만
        # 동점 규칙이 갈린 채로 두면 다음 호출자가 함정을 다시 연다.
        check("locate_by_id도 고르지 않는다",
              graph.locate_by_id(VAULT, nid) is None, graph.locate_by_id(VAULT, nid))

        # 계약의 본체 — 거부했으면 **어느 쪽도** 바뀌지 않았다.
        check("원본 바이트 불변", p.read_bytes() == before_p)
        check("사본 바이트 불변", twin.read_bytes() == before_twin)

        # 중복이 해소되면 종전대로 동작하고, **읽기와 쓰기가 같은 파일**을
        # 가리킨다. 이 마지막 단언이 동점 규칙의 통일을 지킨다.
        twin.unlink()
        _age_all()
        rr2 = M.read_node(nid)
        check("중복 해소 후 읽기 복구", rr2.get("path"), rr2)
        live = nodefile.live_locate(VAULT, nid, graph.Index(VAULT))
        check("읽기와 쓰기가 같은 파일을 가리킨다",
              rr2.get("path") == core.posix_rel(live, ROOT),
              (rr2.get("path"), live))
        r2 = _w(write.update_node, VAULT, nid, summary="새 요약")
        check("중복 해소 후 쓰기 복구", r2.get("ok"), r2)
    finally:
        p.unlink(missing_ok=True)
        twin.unlink(missing_ok=True)
        _age_all()


# ── 동 id는 이름 핸들로도 고르지 않는다 (v4.0.0) ───────────────────────
def test_duplicate_id_by_name_refused():
    """제목이 다른 사본(복원·동기화 충돌)은 **이름으로도** 읽히거나 고쳐지지 않는다.

    구판은 id 핸들만 거부했다. 이름 핸들은 그 이름의 후보만 열어 id가 겹친 줄
    몰랐고, 두 사본이 각자 이름으로 읽히고 고쳐져 조용히 갈라졌다. 이동도
    통과했고, 후보 상정은 "자기 자신과의 충돌"이라며 거부해 갈 길을 주지
    않았다(2026-09-24 재현). Mechanism §2 1항은 동 id면 읽기도 쓰기도 거부한다.

    무엇을 망가뜨리면 실패하는가:
      · `live_locate`의 이름 갈래 `id_twins` 검사를 지우면 → 쓰기·이동·상정 단언
      · `read_node`의 이름 갈래 검사를 지우면 → 읽기 단언
      · 후보표를 계약 파서 대신 `id: ` 줄 정규식으로 판정하면 → 따옴표 사본 단언
      · 검사가 전수 판독(`parse_all`)을 부르면 → 성능 계약 단언
    """
    import mcp_server as M
    p = ROOT / "00_Scope/W1/regr-dupn.md"
    other = ROOT / "00_Scope/regr-dupn-c"
    twin = other / "regr-dupn restored.md"
    other.mkdir(parents=True, exist_ok=True)
    try:
        r0 = _w(write.create_node, VAULT, "regr-dupn", "요약", "ORIGINAL", "fable-5",
                space="00_Scope/W1")
        check("전제: 생성", r0.get("ok"), r0)
        nid = r0["id"]
        twin.write_bytes(p.read_bytes())
        _age_all()
        before = (p.read_bytes(), twin.read_bytes())
        want = sorted(core.posix_rel(x, ROOT) for x in (p, twin))
        M._index, M._searcher, M._fingerprint = None, None, None
        with mock.patch.object(graph.Index, "parse_all",
                               side_effect=AssertionError("full parse")):
            for h in ("regr-dupn", "regr-dupn restored"):
                rr = M.read_node(h)
                check(f"이름 읽기는 사본을 내주지 않는다: {h}",
                      "error" in rr and str(want) in rr["error"], rr)
                check(f"읽기 거부가 갈 길을 준다: {h}",
                      "동일성 사고" in rr.get("error", "")
                      and "vault 밖" in rr.get("error", ""), rr)
            r1 = _w(write.update_node, VAULT, "regr-dupn", old_text="ORIGINAL", new_text="X")
            check("이름 쓰기는 거부한다(전수 판독 없이)",
                  r1.get("ok") is False and str(want) in str(r1), r1)
        r2 = _w(write.update_node, VAULT, "regr-dupn restored", summary="사본 고침")
        check("사본 이름 쓰기도 거부한다", r2.get("ok") is False, r2)
        mv = _w(node_moves.move_nodes, VAULT, ["regr-dupn restored"], "00_Scope/W1")
        check("사본 이동도 거부한다", mv.get("ok") is False and twin.is_file(), mv)
        rc = _w(write.record_candidate, VAULT, "duplication",
                ["regr-dupn", "regr-dupn restored"], "사본")
        check("후보 상정은 동일성 사고로 거부하고 해소 길을 준다",
              rc.get("ok") is False
              and any("동일성 사고" in v for v in rc.get("violations", [])), rc)
        check("어느 쪽도 바뀌지 않았다", (p.read_bytes(), twin.read_bytes()) == before)

        # 손으로 id에 따옴표를 친 사본도 같은 id다 — 판정은 계약 파서가 한다
        twin.write_bytes(before[1].replace(f"id: {nid}".encode(),
                                           f'id: "{nid}"'.encode()))
        _age_all()
        check("전제: 따옴표 사본도 같은 id로 판독된다", contract.parse(twin).id == nid)
        r3 = _w(write.update_node, VAULT, "regr-dupn", summary="고침")
        check("따옴표 사본도 이름 쓰기를 막는다", r3.get("ok") is False, r3)

        twin.unlink()
        _age_all()
        check("사본을 치우면 이름 읽기 복구", "error" not in M.read_node("regr-dupn"))
        r4 = _w(write.update_node, VAULT, "regr-dupn", summary="고침")
        check("사본을 치우면 이름 쓰기 복구", r4.get("ok"), r4)

        # 이동을 반영한 색인(`retarget`)은 옛 경로를 사본으로 세지 않는다 —
        # Windows의 디렉토리 판독 stat은 옛 경로에도 값을 내고, 접어 둔 토큰이
        # 그 값에 맞아 옛 자리가 살아 있는 사본처럼 보였다(검증 재현).
        _age_all()
        graph.Index(VAULT).id_twins(p)         # 토큰을 접어 둔다(racy 창 밖)
        idx = graph.Index(VAULT)
        idx.id_twins(p)
        moved = p.with_name("regr-dupn moved.md")
        os.replace(p, moved)
        idx.retarget(p, moved, placement.space_of(VAULT, moved))
        check("이동 뒤 같은 색인에서 자기 옛 경로는 사본이 아니다",
              idx.id_twins(moved) == [], idx.id_twins(moved))
        moved.unlink()
    finally:
        p.unlink(missing_ok=True)
        shutil.rmtree(other, ignore_errors=True)
        _age_all()


def test_same_file_link_is_duplicate():
    """같은 파일로 가는 파일 링크도 한 노드를 두 자리에 두는 **중복 소속**이다.

    사본과 같이 이름으로도 id로도 읽거나 고치지 않고, 거부와 검증기는 링크를
    지우라고 알린다(Mechanism §2 1항).

    무엇을 망가뜨리면 실패하는가:
      · `id_twins`가 같은 파일을 후보에서 빼면 → 읽기·쓰기 거부 단언
      · 검증기가 같은 파일을 가리지 않으면 → 링크 안내 단언"""
    import mcp_server as M
    p = ROOT / "00_Scope/W1/regr-link.md"
    other = ROOT / "00_Scope/regr-link-c"
    alias = other / "regr-link alias.md"
    other.mkdir(parents=True, exist_ok=True)
    try:
        r0 = _w(write.create_node, VAULT, "regr-link", "요약", "ORIGINAL", "fable-5",
                space="00_Scope/W1")
        check("전제: 생성", r0.get("ok"), r0)
        try:
            os.link(p, alias)
        except OSError as e:
            skip("같은 파일로 가는 링크는 중복 소속이다", f"이 파일시스템에 링크를 만들 수 없다: {e}")
            return
        _age_all()
        before = p.read_bytes()
        M._index, M._searcher, M._fingerprint = None, None, None
        for h in ("regr-link", "regr-link alias"):
            rr = M.read_node(h)
            check(f"링크 이름 읽기도 거부한다: {h}",
                  "error" in rr and "동일성 사고" in rr["error"], rr)
        r1 = _w(write.update_node, VAULT, "regr-link", summary="고침")
        check("링크가 걸린 노드는 이름 쓰기를 거부한다", r1.get("ok") is False, r1)
        check("거부가 링크를 지우라고 알린다", "링크를 지워" in str(r1), r1)
        check("바이트는 그대로다", p.read_bytes() == before)
        fails = str(validate.run(VAULT, cli.published_tools)["fail"])
        check("검증기가 링크를 id 중복으로 보고하고 지우라고 알린다",
              "id 중복" in fails and "같은 파일로 가는 링크" in fails, fails[:600])
        alias.unlink()
        _age_all()
        check("링크를 지우면 이름 읽기 복구", "error" not in M.read_node("regr-link"))
    finally:
        p.unlink(missing_ok=True)
        shutil.rmtree(other, ignore_errors=True)
        _age_all()


# ── 순회의 봉쇄와 대소문자 (v3.7.0) ─────────────────────────────────────
def test_scan_confinement_and_case():
    """손으로 짠 순회가 `rglob`이 하던 두 가지를 잃지 않았는가 — 대소문자
    무시(Windows)와 vault 밖 봉쇄."""
    # 대소문자: `.MD`·`.Md`도 노드다 — **어느 기기에서나** (v4.0.0).
    # 놓치면 그 노드가 색인에서 사라지고 표면이 **스스로 동명 중복을 만든다.**
    up = ROOT / "00_Scope/W1/regr-Case.MD"
    try:
        up.write_text(node_text("260806-cccc-1111", "대문자 확장자", "본문"),
                      encoding="utf-8")
        i = graph.Index(VAULT)
        check("대문자 확장자도 색인에 든다",
              "regr-Case" in i.names, sorted(i.names)[:5])
        r = _w(write.create_node, VAULT, "regr-Case", "충돌", "본문", "fable-5",
               space="00_Scope/W2")
        check("다른 군집에 같은 이름을 만들지 못한다 — 전역 유일이 선다",
              r.get("ok") is False, r)
        check("거부가 동명임을 밝힌다",
              any("이미 있다" in v for v in r.get("violations", [])), r)
        (ROOT / "00_Scope/W2/regr-Case.md").unlink(missing_ok=True)
    finally:
        up.unlink(missing_ok=True)

    # 봉쇄: 리파스 포인트(심볼릭 링크·정션)를 따라 내려가면 vault 밖 파일이
    # 안쪽 노드가 된다. Windows 정션은 `is_symlink()`가 **거짓**이므로
    # 그것만으로 거르면 뚫린다.
    tmp = Path(tempfile.mkdtemp(prefix="osk-outside-"))
    (tmp / "몰래").mkdir()
    (tmp / "몰래" / "regr-외부.md").write_text(
        node_text("260806-dddd-2222", "밖에 있다", "본문"), encoding="utf-8")
    link = ROOT / "00_Scope/W1/regr-link"
    made = False
    try:
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link),
                                   str(tmp / "몰래")],
                                  capture_output=True).returncode == 0
        else:
            try:
                os.symlink(tmp / "몰래", link, target_is_directory=True)
                made = True
            except OSError:
                made = False
        if made:
            i = graph.Index(VAULT)
            check("리파스 포인트 너머는 색인에 들지 않는다 — 봉쇄 유지",
                  "regr-외부" not in i.names)
            check("그 이름은 해석되지 않는다",
                  i.resolve("regr-외부")[0] == "dangling",
                  i.resolve("regr-외부"))
            r = _w(write.update_node, VAULT, "regr-외부", summary="밖에 쓰기")
            check("쓰기 통로도 vault 밖 파일을 잡지 못한다",
                  r.get("ok") is False, r)
        else:
            check("리파스 포인트를 만들 수 없어 건너뛴다(환경 제약)", True)

        # 리파스 **파일**의 배선 — 링크 파일은 조각이 아니라 `space_of`로
        # 보내야 vault 밖을 가리키는 것이 걸러진다. 실제 심볼릭 링크 파일은
        # Windows에서 관리자 권한을 요구하므로 판정기만 갈아 끼워 배선을
        # 고정한다(`is_symlink()`로 되돌리면 이 단언이 깨진다).
        probe = ROOT / "00_Scope/W1/regr-reparse.md"
        probe.write_text(node_text("260806-eeee-3333", "리파스 배선", "본문"),
                         encoding="utf-8")
        seen_paths = []
        real_space_of, real_isr = placement.space_of, placement.is_reparse
        placement.space_of = lambda vault, p: (seen_paths.append(Path(p)),
                                           real_space_of(vault, p))[1]
        placement.is_reparse = lambda e: Path(getattr(e, "path", e)).name == "regr-reparse.md"
        try:
            list(placement.iter_nodes(VAULT))
        finally:
            placement.space_of, placement.is_reparse = real_space_of, real_isr
            probe.unlink(missing_ok=True)
        check("리파스로 판정된 **파일**은 봉쇄 경로(`space_of`)로 간다",
              any(p.name == "regr-reparse.md" for p in seen_paths),
              [p.name for p in seen_paths][:4])
    finally:
        if made:
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "rmdir", str(link)],
                               capture_output=True)
            else:
                link.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 읽기 캐시의 서명: 범위와 racy 창 (v3.7.0) ───────────────────────────
def test_fingerprint_scope_and_racy():
    import mcp_server as M
    raw_dir = ROOT / "00_Scope/W1/_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    r = raw_dir / "regr-fp-raw.md"
    a = ROOT / "00_Scope/W1/regr-fp-scope.md"
    b = ROOT / "00_Scope/W1/regr-fp-moved.md"
    try:
        r.write_text("원자료 1\n", encoding="utf-8")
        a.write_text(node_text("260802-zzzz-rg07"), encoding="utf-8")
        _age_all()
        _entries, racy = graph.index_signature(VAULT)
        check("과거로 민 뒤에는 racy가 서지 않는다", not racy)
        fp0, _ = M._vault_fingerprint()
        check("파일을 건드리지 않으면 지문이 같다 — 헬퍼가 스스로 흔들지 않는다",
              fp0 == M._vault_fingerprint()[0])

        # 범위 ① 비노드(_raw) 변경이 지문을 바꾼다
        r.write_text("원자료 2 — 길이가 다르다\n", encoding="utf-8")
        _age_all()
        check("비노드(_raw) 변경도 지문을 바꾼다", fp0 != M._vault_fingerprint()[0])

        # 범위 ② 엔진 트리는 지문에 들지 않는다 — 들면 `.pyc` 재생성만으로도
        # 읽기 캐시가 통째로 날아간다
        entries, _racy = graph.index_signature(VAULT)
        eng = [rel for rel, _m, _s, _k, _t in entries if "_engine" in rel]
        check("엔진 트리는 지문 범위 밖 — 색인이 읽지 않는 것은 세지 않는다",
              not eng, eng[:4])
        kinds = {k[0] for _rel, _m, _sz, k, _t in entries}
        check("지문에 드는 소속은 색인이 읽는 것뿐",
              kinds <= {"domain", "person", "scope", "workbench-transit",
                        "governance", "raw", "sources", "ledger"}, kinds)
        check("노드 군집에서는 .md만 센다 — 색인이 그것만 읽는다",
              all(placement.is_md(rel.rsplit("/", 1)[-1]) for rel, _m, _sz, k, _t in entries
                  if placement.is_node_home(k)))

        # 경로·크기가 서명에 실제로 실리는가
        _age_all()
        fp1, _ = M._vault_fingerprint()
        os.rename(a, b)
        _age_all()
        check("순수 이동도 지문을 바꾼다 — 경로가 서명에 실린다",
              fp1 != M._vault_fingerprint()[0])
        _age_all()
        fp2, _ = M._vault_fingerprint()
        b.write_text(b.read_text(encoding="utf-8") + "덧붙임\n", encoding="utf-8")
        _age_all()
        check("같은 시각·다른 크기도 지문을 바꾼다 — 크기가 서명에 실린다",
              fp2 != M._vault_fingerprint()[0])

        # racy 창 — 아래로도 위로도 닫혀 있어야 한다. 여유는 아래 캐시 시험처럼
        # 고정한다: 실측 여유는 10 ms대라, 쓰기와 판정 사이가 그보다 길어지면(부하 걸린
        # CI) 방금 쓴 파일이 창 밖으로 나가 판정 논리가 아니라 시계를 시험하게 된다.
        with mock.patch.dict(graph._racy_margin_cache, {VAULT.root: 5_000_000_000}):
            _age_all()
            check("전제: 지금은 안정적", not graph.index_signature(VAULT)[1])
            b.write_text(node_text("260802-zzzz-rg07"), encoding="utf-8")
            check("방금 쓴 파일이 있으면 racy가 선다", graph.index_signature(VAULT)[1])
            # 미래 mtime은 racy가 **아니다** — 아래만 닫으면 그 파일 하나로 읽기
            # 캐시가 영구히 죽는다(실측: 20k에서 80 ms → 6.8~9.2 s)
            _age_all()
            future = time.time() + 86_400
            os.utime(b, (future, future))
            check("먼 미래의 mtime은 racy가 아니다 — 창은 위로도 닫힌다",
                  not graph.index_signature(VAULT)[1])
        _age_all()

        # racy 창에서는 캐시를 접지 않는다. 여유를 넉넉히 **고정해** 시험이
        # 시계에 의존하지 않게 한다 — 실측 여유는 10 ms대라 그대로 두면 파이썬
        # 몇 줄 사이에 창이 닫혀 무엇을 시험했는지 알 수 없어진다.
        with mock.patch.dict(graph._racy_margin_cache, {VAULT.root: 5_000_000_000}):
            M._searcher, M._fingerprint = None, None
            b.write_text(node_text("260802-zzzz-rg07") + "\n덧",
                         encoding="utf-8")
            s1 = M._s()
            s2 = M._s()
            check("racy 창 안에서는 매번 새 검색기 — 낡은 색인을 붙잡지 않는다",
                  s1 is not s2)
            check("창 안에서는 지문을 캐시 키로 접지 않는다",
                  M._fingerprint is None)
            _age_all()
            s3 = M._s()
            check("창이 닫히면 캐시가 선다", M._s() is s3)
            check("그때는 지문이 키로 선다", M._fingerprint is not None)

        # 여유는 상수가 아니라 **재서** 정한다
        graph._racy_margin_cache.pop(VAULT.root, None)
        m = graph.racy_margin_ns(VAULT)
        check("racy 여유는 바닥과 천장 사이에서 측정으로 정해진다",
              graph._RACY_FLOOR_NS <= m <= graph._RACY_CEIL_NS, m)
    finally:
        for p in (r, a, b):
            p.unlink(missing_ok=True)
        _age_all()


# ── 색인의 분할: 이름은 즉시, 계약은 묻는 것만 (v3.7.0) ─────────────────
def test_index_split():
    lazy = graph.Index(VAULT)
    check("생성만으로는 파일을 열지 않는다", not lazy.parsed, len(lazy.parsed))
    check("이름 색인은 그 자리에서 선다", len(lazy.names) > 0, len(lazy.names))

    bad = ROOT / "00_Scope/W1/regr-split-bad.md"
    bad2 = ROOT / "00_Scope/W2/regr-split-bad.md"
    good = ROOT / "00_Scope/W1/regr-split-ok.md"
    dupgood = ROOT / "00_Scope/W2/regr-split-ok.md"
    rawcollide = ROOT / "00_Scope/W1/_raw/regr-split-bad.md"
    try:
        bad.write_text("---\nid: [닫히지 않은\n---\n본문\n", encoding="utf-8")
        good.write_text(node_text("260806-bbbb-2222", "정상", "본문"),
                        encoding="utf-8")
        i = graph.Index(VAULT)
        # 등가성 — **파손이 있는 상태**에서 평가한다. 파손 0건에서 재면
        # `names == nodes`만 확인하는 셈이라 접기의 근거가 서지 않는다.
        check("전제: 파손이 있다", len(i.broken) > 0, sorted(i.broken))
        check("names == nodes ∪ broken (파손 있는 상태)",
              set(i.names) == set(i.nodes) | set(i.broken),
              (len(i.names), len(i.nodes), len(i.broken)))
        check("파손 파일도 이름 색인에는 있다", "regr-split-bad" in i.names)
        check("그러나 노드는 아니다", "regr-split-bad" not in i.nodes)
        check("파손 파일의 이름은 dangling으로 해석된다",
              i.resolve("regr-split-bad")[0] == "dangling",
              i.resolve("regr-split-bad"))
        check("정상 파일은 노드로 해석된다",
              i.resolve("regr-split-ok")[0] == "node")

        # 파손 파일은 동명 판정에 **참여하지 않는다**. 세면 그 이름을 가리키는
        # 모든 쓰기가 `모호 참조`로 막히고, 그 판정에는 소속이 없어서 뒤따르는
        # 헌법 8조 검사가 아예 제기되지 않는다.
        dupgood.write_text(node_text("260806-bbbb-3333", "정상 2", "본문"),
                           encoding="utf-8")
        i = graph.Index(VAULT)
        check("판독되는 동명 둘은 모호",
              i.resolve("regr-split-ok")[0] == "ambiguous",
              i.resolve("regr-split-ok"))
        check("dup_stems가 그것을 싣는다", "regr-split-ok" in i.dup_stems)
        dupgood.unlink()

        bad2.write_text("---\n또 깨짐\n", encoding="utf-8")
        i = graph.Index(VAULT)
        check("정상 + 파손 동명은 모호가 아니라 그 정상 노드다 — 구판 판정",
              i.resolve("regr-split-bad") == ("dangling",)
              or i.resolve("regr-split-bad")[0] == "dangling",
              i.resolve("regr-split-bad"))
        good2 = ROOT / "00_Scope/W2/regr-split-ok2.md"
        good2.write_text(node_text("260806-bbbb-4444", "정상 3", "본문"),
                         encoding="utf-8")
        badtwin = ROOT / "00_Scope/W1/regr-split-ok2.md"
        badtwin.write_text("---\n깨짐\n", encoding="utf-8")
        i = graph.Index(VAULT)
        check("정상 하나 + 파손 하나면 그 정상 노드로 해석된다 — 구판 판정",
              i.resolve("regr-split-ok2")[0] == "node",
              i.resolve("regr-split-ok2"))
        check("파손은 dup_stems에 실리지 않는다",
              "regr-split-ok2" not in i.dup_stems, i.dup_stems)
        good2.unlink(); badtwin.unlink()
        bad2.unlink()

        # 파손 노드와 이름이 겹치는 원자료는 그 이름을 되찾는다 — 이것이
        # 헌법 8조(Domain의 `_raw` 직접 참조)를 fail-open시키지 않는 자리다
        rawcollide.parent.mkdir(parents=True, exist_ok=True)
        rawcollide.write_text("원자료\n", encoding="utf-8")
        i = graph.Index(VAULT)
        check("파손 노드와 동명인 원자료는 비노드로 해석된다 — 구판 판정",
              i.resolve("regr-split-bad")[0] == "nonnode",
              i.resolve("regr-split-bad"))

        # 대상 하나를 묻는 데 전수를 열지 않는다
        j = graph.Index(VAULT)
        j.resolve("regr-split-ok")
        check("이름 하나 해석은 그 후보만 연다", len(j.parsed) == 1, len(j.parsed))
        check("전수 판독은 아직 아니다", not j._all_parsed)
        j.nodes
        check("`nodes`를 물으면 그때 전수 판독한다", j._all_parsed)
        # 전수 판독은 **한 번뿐**이다 — 접어 두지 않으면 `nodes`·`by_id`·
        # `broken`을 물을 때마다 전 노드를 다시 훑는다
        reads = {"n": 0}
        real_readable = graph.Index._readable

        def counted_readable(self, path):
            reads["n"] += 1
            return real_readable(self, path)

        graph.Index._readable = counted_readable
        try:
            j.parse_all(); j.nodes; j.by_id; j.broken; j.dup_stems
        finally:
            graph.Index._readable = real_readable
        check("접어 둔 뒤에는 다시 훑지 않는다", reads["n"] == 0, reads["n"])
    finally:
        for p in (bad, bad2, good, dupgood, rawcollide):
            p.unlink(missing_ok=True)

    # 쓰기 표면별 전수 판독 횟수 — id 유일성 대조를 요구하는 `create_node`만
    seen = {"n": 0}
    real = graph.Index.parse_all

    def counted(self):
        if not self._all_parsed:
            seen["n"] += 1
        return real(self)

    graph.Index.parse_all = counted
    try:
        def full_reads(label, fn):
            seen["n"] = 0
            r = _w(fn)
            check(f"전제: {label} 성공", r.get("ok"), r)
            return seen["n"]

        # v3.7.3 이전에는 1이었다 — id 유일성을 전수 대조로 담보했기 때문이다.
        # 담보가 폭으로 옮겨간 뒤에는 **0이어야 한다.** 이것이 그 판의 성능
        # 계약이며(25,000 노드에서 4.0초 → 0.55초), 되살아나면 여기서 잡힌다.
        check("create_node는 전수 판독을 부르지 않는다 (v3.7.3)",
              full_reads("create_node", lambda: write.create_node(VAULT,
                  "regr-split-w", "분할", "본문", "fable-5",
                  space="00_Scope/W1")) == 0)
        check("update_node(근거 없는 노드)는 전수 판독을 부르지 않는다",
              full_reads("update_node", lambda: write.update_node(VAULT,
                  "regr-split-w", summary="고침")) == 0)
        check("move_node는 전수 판독을 부르지 않는다",
              full_reads("move_node", lambda: node_moves.move_node(VAULT,
                  "regr-split-w", "00_Scope/Workbench/transit")) == 0)
    finally:
        graph.Index.parse_all = real
        for c in ("00_Scope/W1", "00_Scope/Workbench/transit"):
            (ROOT / c / "regr-split-w.md").unlink(missing_ok=True)


# ── 순회의 결정성 (v3.7.0) ──────────────────────────────────────────────
def test_traversal_deterministic():
    """동명이 둘이면 색인에서 **뒤에 오는 쪽이 이긴다.** 순서가 기기마다
    다르면 같은 트리가 기기마다 다른 노드를 가리킨다 — 정렬이 그것을 막는다."""
    real = placement.scan

    def reversed_scan(root, prefix, errors=None):
        return reversed(list(real(root, prefix, errors)))

    base = [(p, k) for p, k in placement.iter_nodes(VAULT)]
    non = dict(graph.Index(VAULT).nonnode)
    placement.scan = reversed_scan
    try:
        flipped = [(p, k) for p, k in placement.iter_nodes(VAULT)]
        check("열거 순서를 뒤집어도 `iter_nodes`의 순서는 같다 — 정렬이 선다",
              base == flipped,
              (len(base), len(flipped),
               [str(a[0]) for a, b in zip(base, flipped) if a != b][:3]))
        check("`nonnode` 등재도 순서에 흔들리지 않는다",
              non == dict(graph.Index(VAULT).nonnode))
    finally:
        placement.scan = real


# ── 쓰기 1회 = 색인 1회 (v3.7.0) ────────────────────────────────────────
def test_one_index_per_write():
    built = {"n": 0}
    real = graph.Index.__init__

    def counted(self, vault):
        built["n"] += 1
        return real(self, vault)

    def once(label, fn, expect_ok=True):
        built["n"] = 0
        r = _w(fn)
        check(f"{label}: 색인을 한 벌만 짓는다", built["n"] == 1,
              f"{built['n']}벌 / {r}")
        # 비용만 재고 성공을 안 재면, 통째로 실패하기 시작해도 조용히 통과한다
        if expect_ok:
            check(f"{label}: 성공", r.get("ok"), r)
        return r

    graph.Index.__init__ = counted
    try:
        r = once("create_node", lambda: write.create_node(VAULT,
            "regr-amp", "증폭", "본문 [[regr-amp]] [[regr-amp-오타]]", "fable-5",
            space="00_Scope/W1"))
        check("자기 자신을 가리키는 Link는 dangling이 아니다",
              r.get("dangling") is not None
              and "regr-amp" not in r["dangling"], r.get("dangling"))
        check("진짜 오타는 그대로 실린다",
              "regr-amp-오타" in (r.get("dangling") or []), r.get("dangling"))
        once("update_node", lambda: write.update_node(VAULT, "regr-amp", summary="고침"))
        once("update_node(no_change)",
             lambda: write.update_node(VAULT, "regr-amp", summary="고침"))
        once("create_node(엣지 동반)", lambda: write.create_node(VAULT,
            "regr-amp2", "증폭", "본문", "fable-5", space="00_Scope/W1",
            edges={"derived-from": "[[없는근거]]"}))
        once("record_candidate", lambda: write.record_candidate(VAULT,
            "duplication", ["regr-amp", "regr-amp2"]))
        m = once("move_node", lambda: node_moves.move_node(VAULT,
            "regr-amp", "00_Scope/Workbench/transit"))
        # 이동 후에도 손에 든 색인이 옛 경로를 쥐고 있으면 자기링크가 오타로
        # 실린다 — `create_node`가 막은 것과 같은 실패다
        check("이동해도 자기 Link는 dangling이 아니다",
              "regr-amp" not in (m.get("dangling") or []), m.get("dangling"))
        check("이동 뒤에도 진짜 오타는 실린다",
              "regr-amp-오타" in (m.get("dangling") or []), m.get("dangling"))
    finally:
        graph.Index.__init__ = real
    for c in ("00_Scope/W1", "00_Scope/Workbench/transit"):
        for stem in ("regr-amp", "regr-amp2"):
            (ROOT / c / f"{stem}.md").unlink(missing_ok=True)


# ── 엔진 판본 관문 (v3.7.0) ─────────────────────────────────────────────
# 두 사고("구판 코드를 쥔 서버가 구 경로에 계속 씀", "폐지된 술어를 단 노드가
# 14시간 뒤에 태어남")의 완화가 여기 선다. 구판의 완화는 `git rev-parse HEAD`를
# 읽어 **엔진과 무관한 수**를 냈다.
def test_engine_epoch_fence():
    from osk import epoch, raw as raw_mod, scope_memory as sm_mod

    check("적재판을 잡았다", epoch.loaded() != epoch.UNKNOWN, epoch.loaded())
    check("정상 상태에서는 낡지 않았다", not epoch.stale())
    check("판은 되풀이해도 같다", epoch.on_disk() == epoch.on_disk())

    # 판의 재료·범위는 **엔진 사본** 위에서 시험한다 — 살아 있는 엔진에 쓰면
    # 같은 기기의 다른 서버 전부가 그 창 동안 쓰기를 거부한다
    tmp = Path(tempfile.mkdtemp(prefix="osk-epoch-"))
    real_root = epoch.ENGINE_ROOT
    try:
        epoch.ENGINE_ROOT = _mini_engine(tmp)
        names = {p.relative_to(epoch.ENGINE_ROOT).as_posix()
                 for p in epoch.engine_files()}
        check("엔진의 .py만 센다", names == {"osk/a.py", "osk/b.py"}, names)
        check("수트는 판에 산입하지 않는다 — 시험을 고쳐도 서버는 낡지 않는다",
              not any(n.startswith("tests/") for n in names), names)
        d0 = epoch._digest()
        (epoch.ENGINE_ROOT / "osk" / "__pycache__" / "b.pyc").write_bytes(b"\x01")
        check("바이트코드는 판을 바꾸지 않는다", epoch._digest() == d0)
        (epoch.ENGINE_ROOT / "tests" / "t.py").write_text("z = 4\n",
                                                          encoding="utf-8")
        check("수트를 고쳐도 판은 그대로", epoch._digest() == d0)
        (epoch.ENGINE_ROOT / "osk" / "a.py").write_text("x = 2\n",
                                                        encoding="utf-8")
        check("엔진 내용이 바뀌면 판이 바뀐다", epoch._digest() != d0)
        (epoch.ENGINE_ROOT / "osk" / "a.py").write_text("x = 1\n",
                                                        encoding="utf-8")
        check("되돌리면 판도 돌아온다", epoch._digest() == d0)
        (epoch.ENGINE_ROOT / "osk" / "c.py").write_text("w = 0\n",
                                                        encoding="utf-8")
        check("파일이 늘면 판이 바뀐다 — 경로가 판의 재료다",
              epoch._digest() != d0)
        (epoch.ENGINE_ROOT / "osk" / "c.py").unlink()

        # **열거 실패는 판이 아니다.** `os.walk`의 기본값은 오류를 삼키고, 그러면
        # 빈 해시가 정상 판 행세를 해 관문이 통째로 no-op가 된다.
        empty = tmp / "empty"
        empty.mkdir()
        epoch.ENGINE_ROOT = empty
        check("엔진 `.py`가 없으면 판을 재지 못한다 — 빈 해시를 내지 않는다",
              epoch._measure() == epoch.UNKNOWN, epoch._measure())
        epoch.ENGINE_ROOT = tmp / "없는디렉토리"
        check("열거 불가도 판정 불가다", epoch._measure() == epoch.UNKNOWN)
        # **부분** 열거 실패가 진짜 위험이다. 전면 실패는 "파일 0개" 가드가
        # 잡지만, 일부 디렉토리만 못 읽으면 `os.walk`의 기본값이 그것을 조용히
        # 건너뛰어 **일부만 센 해시**가 정상 판 행세를 한다 — 그 프로세스는
        # 자기가 낡았는지 영영 모른다.
        epoch.ENGINE_ROOT = _mini_engine(tmp)
        real_walk = epoch.os.walk

        def partial(top, **kw):
            yield (str(Path(top) / "osk"), [], ["a.py"])   # 한 갈래는 읽히고
            onerror = kw.get("onerror")
            if onerror is not None:                        # 다른 갈래는 실패
                onerror(OSError(13, "권한 없음", str(top)))

        epoch.os.walk = partial
        try:
            check("부분 열거 실패는 판정 불가다 — 일부만 센 해시를 내지 않는다",
                  epoch._measure() == epoch.UNKNOWN, epoch._measure())
        finally:
            epoch.os.walk = real_walk

        # `__pycache__` 제외는 판정이 아니라 **순회 비용**의 문제다 — 아래가
        # `.py`만 모으므로 빼도 판은 같다. 그 사실을 여기 못박아 두면 다음
        # 사람이 이 항목을 안전 장치로 오해하지 않는다.
        epoch.ENGINE_ROOT = _mini_engine(tmp)
        d_with = epoch._digest()
        real_skip = epoch._SKIP_DIRS
        epoch._SKIP_DIRS = frozenset(x for x in real_skip if x != "__pycache__")
        try:
            check("`__pycache__` 제외는 판을 바꾸지 않는다 — 비용 문제일 뿐",
                  epoch._digest() == d_with)
        finally:
            epoch._SKIP_DIRS = real_skip
    finally:
        epoch.ENGINE_ROOT = real_root
        shutil.rmtree(tmp, ignore_errors=True)

    # 관문은 **잠금을 잡은 뒤**에 선다 — 밖에서 재면 재고 잡기까지 사이에
    # 갱신이 끼어드는 창이 열린다
    order = []
    real_lock = core.lock_exclusive
    real_fence = core._fence

    def spy_lock(f, *a, **kw):
        order.append("lock")
        return real_lock(f, *a, **kw)

    def spy_fence():
        order.append("fence")
        return real_fence()

    core.lock_exclusive, core._fence = spy_lock, spy_fence
    try:
        with core.mutation_lock(VAULT):
            pass
    finally:
        core.lock_exclusive, core._fence = real_lock, real_fence
    check("관문은 잠금 안에서 선다", order == ["lock", "fence"], order)

    # 낡음 위조 — 관문이 없으면 **성공했을** 인자로 부른다. 그래야 거부가
    # 관문 때문임이 증명된다(자기 이유로 거부되는 인자는 아무것도 못 밝힌다).
    real_loaded, target = epoch._LOADED, ROOT / "00_Scope/W1/regr-fence.md"
    r0 = _w(write.create_node, VAULT, "regr-fence", "관문", "본문", "fable-5",
            space="00_Scope/W1")
    check("전제: 관문 전에는 쓰기가 된다", r0.get("ok"), r0)
    r0b = _w(write.create_node, VAULT, "regr-fence-b", "관문", "본문", "fable-5",
             space="00_Scope/W1")
    check("전제: 두 번째 노드도 만들어졌다", r0b.get("ok"), r0b)
    h = r0["new_hash"]
    trace = ROOT / "00_Scope/W1/_cited/.records/2026-08-30-fence.txt"
    sm_before = _w(sm_mod.read, VAULT, "repo/fence")
    try:
        epoch._LOADED = "0000deadbeef"
        surfaces = {
            "create_node": lambda: _w(write.create_node, VAULT, "regr-fence-2", "관문",
                                      "본문", "fable-5", space="00_Scope/W1"),
            "update_node": lambda: _w(write.update_node, VAULT, "regr-fence",
                                      body="새 본문", expect_hash=h),
            "move_node": lambda: _w(node_moves.move_node, VAULT, "regr-fence",
                                    "00_Scope/Workbench/transit"),
            "record_candidate": lambda: _w(write.record_candidate, VAULT,
                                           "duplication",
                                           ["regr-fence", "regr-fence-b"]),
            "cite_round": lambda: _w(_append_round, "repo/fence",
                                     "2026-08-30-fence", "u", "a",
                                     "00_Scope/W1"),
            "scope_memory": lambda: _w(sm_mod.replace, VAULT, "repo/fence", "기억"),
        }
        for name, call in surfaces.items():
            r = call()
            check(f"낡은 프로세스의 {name} 거부", r.get("ok") is False, r)
            check(f"{name} 거부가 재기동을 지시한다",
                  any("재기동" in v for v in r.get("violations", [])), r)
        vio = surfaces["create_node"]()["violations"]
        check("거부는 적재판과 디스크판을 함께 보인다",
              any("0000deadbeef" in v and epoch.on_disk() in v for v in vio), vio)
        # 거부 뒤 흔적 없음 — 여섯 표면 전부
        check("create_node: 파일이 없다",
              not (ROOT / "00_Scope/W1/regr-fence-2.md").exists())
        check("update_node: 본문 그대로",
              "새 본문" not in target.read_text(encoding="utf-8"))
        check("move_node: 제자리 그대로", target.exists())
        check("cite_round: 기록이 생기지 않았다", not trace.exists())
        check("scope_memory: 기억이 바뀌지 않았다",
              _w(sm_mod.read, VAULT, "repo/fence").get("text")
              == sm_before.get("text"))
    finally:
        epoch._LOADED = real_loaded

    # 관문 거부 뒤에도 잠금이 실제로 풀렸는가 — 예외 객체를 살려 두어
    # 참조계수가 대신 닫아 주는 것을 막고 본다
    held = None
    try:
        epoch._LOADED = "0000deadbeef"
        try:
            with core.mutation_lock(VAULT):
                pass
        except core.StaleEngineError as e:
            held = e            # 프레임을 살려 둔다 — 참조계수에 기대지 않는다
    finally:
        epoch._LOADED = real_loaded
    check("관문이 거부해도 잠금은 풀린다(예외를 쥔 채로도)", held is not None)
    r1 = _w(write.update_node, VAULT, "regr-fence", summary="복구 확인")
    check("관문 해제 후 쓰기가 다시 된다", r1.get("ok"), r1)
    del held
    for p in (target, ROOT / "00_Scope/W1/regr-fence-b.md",
              ROOT / "00_Scope/Workbench/transit/regr-fence.md"):
        p.unlink(missing_ok=True)
