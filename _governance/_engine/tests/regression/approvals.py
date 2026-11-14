"""회귀 수트의 한 갈래: 보호영역 — 지정·승인·반려·해제, 승인본 저장소, 갈래의 봉합.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, tempfile, time
from pathlib import Path
from unittest import mock

from regression.support import check, node_text, POSIX_MODE, _raises, rmtree_force, ROOT, skip, VAULT
from osk import cli, core, validate, authority, node_moves, nodefile  # noqa: E402


# ── 4. 보호영역 생애 (지정·pending·양측 CAS 승인·반려·해제 — 격리 subprocess) ──
def test_approval_lifecycle():
    with tempfile.TemporaryDirectory() as td:
        errs = validate.fixture_approval_lifecycle(VAULT, td)
        check("보호영역 생애(양측 CAS·반려 복원·미보호 거부)", not errs, errs)


# ── 13. 권한 검사는 봉투 평가기 전까지 절대 proceed를 내지 않는다 ────────
def test_authority_hold():
    for action in ("노드 이동", "서명", "아무 문자열"):
        v = authority.check(VAULT, action)
        check(f"authority.check({action!r}) = hold", v["verdict"] == "hold",
              v["verdict"])


# ── 14. 기준선: 정상 mini-vault + 보호영역 지정 → 검증기 PASS ───────────
# ── 승인 저장소 digest 봉쇄 (PR #14 리뷰 [high]) ────────────────────────
def test_store_digest_confined():
    """신뢰 밖 digest가 승인 저장소(STORE) 밖 경로를 읽지 못한다 — _obj_path는
    정확히 `sha256:<64 소문자 hex>`만 받고 계산 경로의 STORE 봉쇄를 재확인한다."""
    from osk import region_tree
    import os as _os
    good = "sha256:" + "a" * 64
    p = region_tree._obj_path(VAULT, good)
    store_s = _os.path.normpath(region_tree.store_dir(VAULT))
    check("정상 digest는 STORE 안으로만 해석",
          _os.path.commonpath([store_s, _os.path.normpath(p)]) == store_s)
    for bad in ("sha256:../" + "a" * 61, "sha256:/etc/passwd",
                "sha256:" + "a" * 63, "sha256:" + "a" * 65, "sha256:" + "A" * 64,
                "sha256:" + "g" * 64, "notsha:" + "a" * 64, "../../etc/passwd",
                "sha256:", "sha256:" + "a" * 62 + "/x"):
        check(f"부적격 digest 거부: {bad[:22]}",
              _raises(lambda b=bad: region_tree._obj_path(VAULT, b))())
        check(f"_store_get은 부적격 digest를 부재로: {bad[:22]}",
              region_tree.store_get(VAULT, bad) is None)


# ── 위임 성립의 보호 범위 — 상위는 상속, 하위는 우회 불가 ───────────────
def test_delegation_protection_scope():
    """헌법 10조 1항: 상위 구획의 보호는 그 하위 전체에 미친다 — 사용자가 Facet
    대신 `Person`을 지정했어도 위임은 성립한다. 반대로 Facet **하위**만
    지정한 것은 Facet의 미보호를 우회하지 못한다(헌법 7조 3항, fail-closed)."""
    from osk import authority, approval_acts
    dnode = ROOT / "00_Person/Delegation/regr-deleg.md"
    subdir = ROOT / "00_Person/Delegation/sub"
    clause = ("## 위임\n- 대상: 시험 행위\n- 범위: 시험\n"
              "- 조건: 없음\n- 종료: 없음\n")
    def eff():
        return {d["title"]: d["effective"] for d in authority.enumerate_delegations(VAULT)}
    try:
        dnode.write_text(node_text("260802-zzzz-rgd1", "위임 노드", clause),
                         encoding="utf-8")
        check("미보호에서는 미성립", eff().get("regr-deleg") is False, eff())
        # (a) 상위 구획만 보호 — 하향 상속으로 성립한다
        approval_acts.protect(VAULT, "00_Person", "상위 지정")
        check("상위 보호는 하위에 미친다(헌법 10조 1항)",
              eff().get("regr-deleg") is True, eff())
        approval_acts.unprotect(VAULT, "00_Person", "정리")
        # (b) Facet 하위만 보호 — Facet 자신은 미보호라 우회되지 않는다
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "filler.md").write_text("x", encoding="utf-8")
        approval_acts.protect(VAULT, "00_Person/Delegation/sub", "하위만")
        check("하위 구획만 보호되면 위임 미성립(우회 차단)",
              eff().get("regr-deleg") is False, eff())
        approval_acts.unprotect(VAULT, "00_Person/Delegation/sub", "정리")
        shutil.rmtree(subdir, ignore_errors=True)
        # (c) Facet 자신 보호 → 성립
        approval_acts.protect(VAULT, "00_Person/Delegation", "Facet 보호")
        check("위임 Facet 자체 보호 시 위임 성립",
              eff().get("regr-deleg") is True, eff())
        # (d) 열거는 승인본에서 — 작업본에서 지워도 승인본의 노드가 보고된다
        dnode.unlink()
        rows = {d["title"]: d for d in authority.enumerate_delegations(VAULT)}
        check("승인본에 있던 노드가 열거에서 사라지지 않는다",
              "regr-deleg" in rows, list(rows))
        check("다만 작업본 부재라 미성립",
              rows.get("regr-deleg", {}).get("effective") is False, rows)
        dnode.write_text(node_text("260802-zzzz-rgd1", "위임 노드", clause),
                         encoding="utf-8")
        approval_acts.unprotect(VAULT, "00_Person/Delegation", "정리")
    finally:
        for r in ("00_Person", "00_Person/Delegation", "00_Person/Delegation/sub"):
            try: approval_acts.unprotect(VAULT, r, "정리")
            except Exception: pass
        shutil.rmtree(subdir, ignore_errors=True)
        dnode.unlink(missing_ok=True)


# ── approve 양측 CAS는 관례가 아니라 계약으로 강제 (PR #14 리뷰 [high]) ──
def test_approve_requires_expect_work():
    """approve는 `expect_work`(검토한 작업본) 없이는 성립하지 않는다 — 기본값을
    두면 내부 호출이 작업본 측 CAS를 건너뛴다. base가 맞아도 생략하면 거부하고
    대장에 아무 기록도 남기지 않는다."""
    from osk import approvals as A, approval_acts, region_tree
    def raises_any(fn):
        try: fn(); return False
        except (ValueError, TypeError): return True
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "00_Scope/W2/regr-aw.md"
    f.write_text("v1", encoding="utf-8")
    try:
        approval_acts.protect(VAULT, "00_Scope/W2", "지정")
        f.write_text("v2", encoding="utf-8")            # pending
        base = A.approved_hash(VAULT, "00_Scope/W2")
        before = len(A.records(VAULT))
        check("expect_work 생략 approve 거부(필수 인자)",
              raises_any(lambda: approval_acts.approve(VAULT, "00_Scope/W2", base)))
        check("expect_work=None approve 거부",
              raises_any(lambda: approval_acts.approve(VAULT, "00_Scope/W2", base, None)))
        check("거부 시 승인 대장에 기록 없음", len(A.records(VAULT)) == before)
        work = region_tree.working_tree_hash(VAULT, "00_Scope/W2")
        approval_acts.approve(VAULT, "00_Scope/W2", base, expect_work=work, reason="검토")
        check("양측 CAS 충족 시 승인", A.state(VAULT, "00_Scope/W2") == "clean")
    finally:
        try: approval_acts.unprotect(VAULT, "00_Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 승인본 manifest↔blob 결속·복원 가능성 (PR #14 리뷰 [high]) ──────────
def test_approval_baseline_blobs_present():
    """_store_tree가 파일당 한 번 읽어 그 bytes를 박제하므로 manifest가 가리키는
    모든 blob이 실재한다(복원 가능). integrity는 manifest만이 아니라 그것이
    가리키는 blob의 실재까지 확인해 복원 불가능한 승인본을 적발한다."""
    from osk import approvals as A, approval_acts, region_tree
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "00_Scope/W2/regr-bb.md"
    f.write_text("내용A", encoding="utf-8")
    try:
        approval_acts.protect(VAULT, "00_Scope/W2", "지정")
        table = region_tree.tree_table(VAULT, A.approved_hash(VAULT, "00_Scope/W2"))
        check("승인본 manifest의 모든 blob 실재(단일 판독 박제)",
              all(region_tree.store_get(VAULT, h) is not None for h in table.values()), table)
        check("integrity 통과(정상 승인본)", A.integrity(VAULT) == [], A.integrity(VAULT))
        # 참조 blob 하나를 삭제 → 복원 불가 승인본을 integrity가 적발
        some_h = next(iter(table.values()))
        region_tree._obj_path(VAULT, some_h).unlink()
        check("integrity가 blob 부재(복원 불가)를 적발",
              any("복원 불가" in e for e in A.integrity(VAULT)), A.integrity(VAULT))
        f.write_text("변경", encoding="utf-8")            # pending
        check("blob 부재 승인본으로의 revert는 사전검증에서 거부",
              _raises(lambda: approval_acts.revert(VAULT, "00_Scope/W2", A.approved_hash(VAULT, "00_Scope/W2"),
                                       region_tree.working_tree_hash(VAULT, "00_Scope/W2")))())
        region_tree.store_put(VAULT, "내용A".encode("utf-8"))             # blob 복원
        check("blob 복원 후 integrity 통과", A.integrity(VAULT) == [], A.integrity(VAULT))
    finally:
        f.write_text("내용A", encoding="utf-8")            # 승인본으로 복귀 → clean
        try: approval_acts.unprotect(VAULT, "00_Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 내용 주소 저장소는 digest↔bytes 일치를 검증 (PR #14 리뷰 [high]) ────
def test_store_content_verified():
    """저장소는 읽기에서 `sha256(bytes)==digest`를 검증한다 — 같은 경로가 다른
    bytes로 변조되면(동기화 충돌·손상·변조) 부재/손상으로 취급되어 integrity
    FAIL·revert가 쓰기 전에 거부하고, _store_put은 손상 객체를 정상으로 치유한다.
    이 계약을 저장소 접근자 한 곳에서 강제하므로 integrity·revert는 손대지
    않아도 함께 fail-closed 된다."""
    from osk import approvals as A, approval_acts, region_tree
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "00_Scope/W2/regr-cv.md"
    f.write_text("정본내용", encoding="utf-8")
    try:
        approval_acts.protect(VAULT, "00_Scope/W2", "지정")
        table = region_tree.tree_table(VAULT, A.approved_hash(VAULT, "00_Scope/W2"))
        h = table["00_Scope/W2/regr-cv.md"]
        obj = region_tree._obj_path(VAULT, h)
        obj.write_bytes(b"corrupted-bytes-not-matching-the-digest")   # 같은 경로 변조
        check("변조된 blob은 _store_get에서 부재로(내용 검증)",
              region_tree.store_get(VAULT, h) is None)
        check("integrity가 손상 blob을 적발",
              any("복원 불가" in e for e in A.integrity(VAULT)), A.integrity(VAULT))
        f.write_text("변경", encoding="utf-8")            # pending
        check("손상 blob 승인본으로의 revert는 사전검증에서 거부",
              _raises(lambda: approval_acts.revert(VAULT, "00_Scope/W2", A.approved_hash(VAULT, "00_Scope/W2"),
                                       region_tree.working_tree_hash(VAULT, "00_Scope/W2")))())
        region_tree.store_put(VAULT, "정본내용".encode("utf-8"))          # 손상 객체 치유
        check("_store_put이 손상 객체를 정상으로 치유", region_tree.store_get(VAULT, h) is not None)
        check("치유 후 integrity 통과", A.integrity(VAULT) == [], A.integrity(VAULT))
    finally:
        f.write_text("정본내용", encoding="utf-8")          # 승인본으로 복귀 → clean
        try: approval_acts.unprotect(VAULT, "00_Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


def test_dir_entries_durable_before_ledger():
    """디렉터리 엔트리 내구화 (v4 3단계 · Mechanism §1-2 7항·§3 1항) — 파일만
    fsync하고 부모 디렉터리를 fsync하지 않으면, 전원 차단 뒤 대장 기록은
    남았는데 그것이 가리키는 승인본 객체·복원 파일이 사라질 수 있다(POSIX).
    보호·승인·반려와 노드 쓰기·대장 첫 생성은 **공유 헬퍼 한 곳**
    (`core.fsync_dir`)으로 부모를 내구화하고, 그 순서가 대장 기록보다 앞선다.
    Windows에서 그 헬퍼는 의도된 no-op이다(디렉터리를 열 수 없고 NTFS가
    메타데이터를 저널링한다) — 호출 순서는 두 OS에서 보고, 실제 디렉터리
    fsync는 POSIX에서만 본다."""
    from osk import approvals as A, approval_acts, region_tree
    real_fd = getattr(core, "fsync_dir", None)
    # 헬퍼가 없는 엔진(v3.22.x)에서도 아래 순서 검사가 **각각** 실패로 드러나도록
    # 여기서 멈추지 않는다 — 가로챌 자리가 없으면 만들어 두고(create) 호출을 센다.
    check("core가 디렉터리 엔트리 내구화 헬퍼를 둔다", callable(real_fd))
    real_fd = real_fd or (lambda d: None)
    norm = lambda p: os.path.normcase(os.path.realpath(p))
    ev = []                                   # ("dir", 경로) | ("ledger", 대장)
    real_open, real_fsync = os.open, os.fsync
    dir_fds = {}

    def spy_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if os.path.isdir(path):
            dir_fds[fd] = norm(path)
        return fd

    def spy_fsync(fd):
        if fd in dir_fds:                     # 실제 디렉터리 fsync(POSIX)
            ev.append(("os-dir", dir_fds[fd]))
        return real_fsync(fd)

    def spy_fd(d):
        ev.append(("dir", norm(d)))
        return real_fd(d)

    real_append = A.ledger_append

    def spy_append(path, record, expect=None):
        ev.append(("ledger", norm(path)))
        return real_append(path, record, expect=expect)

    def before_ledger(kind, d, ledger):
        """`d`의 `kind` 내구화가 `ledger`의 첫 기록보다 앞서 있는가."""
        li = next((i for i, e in enumerate(ev) if e == ("ledger", norm(ledger))), None)
        di = next((i for i, e in enumerate(ev) if e == (kind, norm(d))), None)
        return li is not None and di is not None and di < li

    def spying():
        return [mock.patch.object(core, "fsync_dir", spy_fd, create=True),
                mock.patch.object(A, "ledger_append", spy_append),
                mock.patch.object(approval_acts, "fsync_dir", spy_fd),
                mock.patch.object(approval_acts, "ledger_append", spy_append),
                mock.patch.object(os, "open", spy_open),
                mock.patch.object(os, "fsync", spy_fsync)]

    reg = "00_Scope/DUR"
    regdir = ROOT / "00_Scope" / "DUR"
    tag = f"{time.time_ns()}"
    regdir.mkdir(parents=True, exist_ok=True)
    f = regdir / "dur.md"
    f.write_text(f"내구화 {tag}", encoding="utf-8")
    try:
        def run(fn):
            before = set(region_tree.store_dir(VAULT).rglob("*")) if region_tree.store_dir(VAULT).exists() else set()
            ev.clear(); dir_fds.clear()
            ps = spying()
            for p in ps:
                p.start()
            try:
                fn()
            finally:
                for p in reversed(ps):
                    p.stop()
            return sorted(set(region_tree.store_dir(VAULT).rglob("*")) - before)

        for label, fn in (
                ("보호", lambda: approval_acts.protect(VAULT, reg, "내구화 시험")),
                ("승인", lambda: (f.write_text(f"승인본 {tag}", encoding="utf-8"),
                                 approval_acts.approve(VAULT, reg, A.approved_hash(VAULT, reg),
                                           region_tree.working_tree_hash(VAULT, reg), "내구화"))),
        ):
            new = run(fn)
            check(f"{label}: 새 저장소 엔트리가 생겼다", bool(new), new)
            late = [core.posix_rel(e, ROOT) for e in new
                    if not before_ledger("dir", e.parent, A.ledger_path(VAULT))]
            check(f"{label}: 새 객체·객체 디렉터리의 부모가 승인 기록 **전에** "
                  f"내구화된다(core.fsync_dir)", not late, (late, ev[:8]))
            if POSIX_MODE:
                late_os = [core.posix_rel(e, ROOT) for e in new
                           if not before_ledger("os-dir", e.parent, A.ledger_path(VAULT))]
                check(f"{label}: 실제 디렉터리 fsync가 승인 기록보다 앞선다(POSIX)",
                      not late_os, (late_os, ev[:8]))
            else:
                skip(f"{label}: 실제 디렉터리 fsync가 승인 기록보다 앞선다(POSIX)",
                     "Windows는 디렉터리를 열 수 없어 fsync_dir가 의도된 no-op이다 "
                     "(NTFS 메타데이터 저널)")

        # 반려 — 복원한 파일과 지운 파일의 부모가 revert 기록 전에 내구화된다
        f.write_text("에이전트 변경", encoding="utf-8")
        (regdir / "extra.md").write_text("추가", encoding="utf-8")
        run(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                             region_tree.working_tree_hash(VAULT, reg), "내구화"))
        check("반려: 복원이 승인본 그대로",
              f.read_text(encoding="utf-8") == f"승인본 {tag}"
              and not (regdir / "extra.md").exists())
        check("반려: 복원·삭제한 엔트리의 부모가 revert 기록 전에 내구화된다",
              before_ledger("dir", regdir, A.ledger_path(VAULT)), ev[:8])
        if POSIX_MODE:
            check("반려: 실제 디렉터리 fsync가 revert 기록보다 앞선다(POSIX)",
                  before_ledger("os-dir", regdir, A.ledger_path(VAULT)), ev[:8])
        else:
            skip("반려: 실제 디렉터리 fsync가 revert 기록보다 앞선다(POSIX)",
                 "Windows는 디렉터리를 열 수 없어 fsync_dir가 의도된 no-op이다 "
                 "(NTFS 메타데이터 저널)")

        # 승인본 재기록이 부모를 대신 내구화해 주지 않는 자리 — 승인 파일이 없는
        # 하위 디렉터리의 삭제, 영역 밖의 원위치(들어온 이동)·현위치(나간 이동)
        sub = regdir / "sub"
        sub.mkdir(exist_ok=True)
        (sub / "extra.md").write_text("추가", encoding="utf-8")
        run(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                             region_tree.working_tree_hash(VAULT, reg), "내구화"))
        check("반려: 승인 파일이 없는 하위 디렉터리의 삭제도 revert 기록 전에 내구화된다",
              not (sub / "extra.md").exists()
              and before_ledger("dir", sub, A.ledger_path(VAULT)), ev[:8])
        away = ROOT / "00_Scope" / "W1"
        for label, nid, start, dest, outside in (
                ("들어온", "260925-zzzz-dur1", away, reg, away),
                ("나간", "260925-zzzz-dur2", regdir, "00_Scope/W1", away)):
            stem = f"regr-dur-{nid[-4:]}-{tag}"
            (start / f"{stem}.md").write_text(node_text(nid), encoding="utf-8")
            if start == regdir:
                approval_acts.approve(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "내구화")
            check(f"{label} 이동 성립", node_moves.move_node(VAULT, stem, dest)["ok"])
            run(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                                 region_tree.working_tree_hash(VAULT, reg), "내구화"))
            check(f"반려: {label} 이동의 원상 복구가 영역 밖 엔트리까지 revert "
                  f"기록 전에 내구화한다",
                  (start / f"{stem}.md").is_file()
                  and before_ledger("dir", outside, A.ledger_path(VAULT)), ev[:8])
            (away / f"{stem}.md").unlink(missing_ok=True)

        # 노드 쓰기(nodefile.write_file)와 대장의 첫 생성
        deep = ROOT / "00_Scope" / "DUR" / "nest" / f"n{tag}"
        run(lambda: nodefile.write_file(deep / "x.json", b"{}"))
        got = {e[1] for e in ev if e[0] == "dir"}
        check("노드 쓰기: 파일의 부모와 새로 만든 조상의 부모를 내구화한다",
              {norm(deep), norm(deep.parent), norm(regdir)} <= got, sorted(got))
        led = VAULT.ledger / f"dur-{tag}" / "probe.jsonl"
        run(lambda: core.ledger_append(led, {"kind": "probe"}))
        got = [e[1] for e in ev if e[0] == "dir"]
        check("대장 첫 생성: 대장 디렉터리와 그 부모를 내구화한다",
              norm(led.parent) in got and norm(led.parent.parent) in got, got)
        run(lambda: core.ledger_append(led, {"kind": "probe"}))
        check("기존 대장의 append는 디렉터리를 다시 fsync하지 않는다",
              not [e for e in ev if e[0] == "dir"], ev)
        if os.name == "nt":
            ev.clear(); dir_fds.clear()
            with mock.patch.object(os, "open", spy_open):
                real_fd(regdir)
            check("Windows의 fsync_dir는 디렉터리를 열지 않는 명시적 no-op",
                  not dir_fds, dir_fds)
        shutil.rmtree(led.parent, ignore_errors=True)
    finally:
        f.write_text(f"승인본 {tag}", encoding="utf-8")
        (regdir / "extra.md").unlink(missing_ok=True)
        shutil.rmtree(regdir / "nest", ignore_errors=True)
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


def test_atomic_write_waits_out_open_reader():
    """Windows에서는 대상 파일을 연 읽기가 있으면 `os.replace`가 WinError 5로 실패한다
    (Python의 `open()`은 FILE_SHARE_DELETE를 주지 않는다). 대화 커서를 상태 잠금 없이
    읽는 곳(integration status·catchup·doctor·포착 중 다른 커서)이나 엔진 밖의 읽기가
    분리 작업자의 저장과 겹쳐 포착이 실패했다(#192 측정). 읽기는 곧 닫히므로 다시 건다."""
    if os.name != "nt":
        skip("열린 읽기가 원자 교체를 막는다", "Windows 공유 모드 전용")
        return
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "cursor.json"
        p.write_bytes(b"old")
        reader, real, failed = open(p, "rb"), os.replace, []

        def replace(src, dst):
            try:
                return real(src, dst)
            except PermissionError as e:
                failed.append(e.winerror)
                raise
            finally:
                reader.close()                # 열린 읽기는 첫 교체 시도 뒤 닫힌다

        try:
            with mock.patch.object(os, "replace", replace):
                core.atomic_write(p, b"new")
        finally:
            reader.close()
        check("전제: 첫 교체는 열린 읽기 때문에 WinError 5로 실패한다", failed == [5], failed)
        check("읽기가 닫히면 교체를 다시 걸어 새 내용을 쓴다", p.read_bytes() == b"new", p.read_bytes())


# ── 대장 잠금 안 전제 재확인 — 스냅샷 중 유입 기록을 덮지 않는다 ────────
def test_approve_precondition_under_lock():
    """승인본 측 CAS는 대장 **잠금 안에서** 다시 본다 — 작업본 스냅샷이 걸리는
    동안 다른 기기의 승인이 동기화로 들어오면, 그것을 못 본 행이 인과 자식으로
    붙어 사용자가 검토한 적 없는 승인본을 조용히 대체하는 일이 없다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Scope/W2"
    f = ROOT / "00_Scope/W2/regr-race.md"
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    real = region_tree.store_tree
    try:
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)
        f.write_text("v2", encoding="utf-8")                 # pending
        work = region_tree.working_tree_hash(VAULT, reg)

        def racing(vault, d):             # 스냅샷 도중 다른 기기 기록이 착지
            region_tree.store_tree = real
            core.ledger_append(A.ledger_path(VAULT), {
                "kind": "approve", "region": reg, "base": base,
                "accepted": work, "reason": "다른 기기(시험)"})
            return real(vault, d)
        region_tree.store_tree = racing
        before = len(A.records(VAULT))
        check("스냅샷 중 승인본이 바뀌면 승인 거부",
              _raises(lambda: approval_acts.approve(VAULT, reg, base, expect_work=work))())
        check("유입 기록 1행만 늘고 내 승인은 미기록",
              len(A.records(VAULT)) == before + 1, len(A.records(VAULT)) - before)
        check("현행 승인본은 유입 기록의 것", A.approved_hash(VAULT, reg) == work)
    finally:
        region_tree.store_tree = real
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 지정의 잠금 안 전제는 stale도 막는다 (PR #14 리뷰) ─────────────────
def test_protect_precondition_rejects_stale():
    """지정 스냅샷 도중 같은 영역의 비교 불능 기록이 유입돼 stale이 되면 지정은
    성립하지 않는다 — `is_protected`는 stale에서도 False라, 그것만 보면 분기가
    표면화되지 않고 새 초기 승인본으로 조용히 봉합된다(본문의 stale 거부와 어긋남)."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/pstale"
    regdir = ROOT / "00_Domain" / "pstale"
    real = region_tree.store_tree
    kept = A.ledger_path(VAULT).read_text(encoding="utf-8") if A.ledger_path(VAULT).exists() else ""
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        check("지정 전에는 미보호", A.state(VAULT, reg) == "unprotected")

        def racing(vault, d):             # 스냅샷 도중 다기기 분기가 착지
            region_tree.store_tree = real
            recs = A.records(VAULT)
            head = recs[-1]["rid"] if recs else None
            rid = head
            with open(A.ledger_path(VAULT), "a", encoding="utf-8") as fh:
                for i in (1, 2):
                    rid = core._next_rid(rid)
                    fh.write(json.dumps({
                        "rid": rid, "parents": [head] if head else [],
                        "at": core.now_iso(), "kind": "protect", "region": reg,
                        "base": None, "accepted": "sha256:" + f"{i}" * 64,
                        "reason": f"기기{i}(시험)"}, ensure_ascii=False) + "\n")
            return real(vault, d)
        region_tree.store_tree = racing
        check("스냅샷 중 stale이 되면 지정 거부", _raises(lambda: approval_acts.protect(VAULT, reg))())
        region_tree.store_tree = real
        check("분기는 그대로 남는다(봉합되지 않음)", A.state(VAULT, reg) == "stale")
        check("내 protect 행은 기록되지 않았다",
              sum(1 for r in A.records(VAULT)
                  if r.get("region") == reg and "시험" not in (r.get("reason") or "")) == 0)
    finally:
        region_tree.store_tree = real
        try: A.ledger_path(VAULT).write_text(kept, encoding="utf-8")   # 분기 원상 복구
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 사용자는 '차이'를 검토한다 — 해시 두 개는 검토가 아니다 (헌법 10조 2항) ──
def test_changeset_lists_difference():
    """헌법 10조 2항은 사용자가 **차이를 검토하여** 승인·반려하라고 한다.
    엔진은 그 차이를 파일 단위(추가·삭제·수정)로 낼 수 있어야 한다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/csview"
    regdir = ROOT / "00_Domain" / "csview"
    try:
        (regdir / "sub").mkdir(parents=True, exist_ok=True)
        (regdir / "keep.md").write_text("그대로", encoding="utf-8")
        (regdir / "gone.md").write_text("사라질 것", encoding="utf-8")
        (regdir / "sub" / "edit.md").write_text("v1", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        check("clean에서는 차이가 없다",
              A.changeset(VAULT, reg) == {"added": [], "removed": [],
                                   "modified": [], "moves": []},
              A.changeset(VAULT, reg))
        (regdir / "gone.md").unlink()
        (regdir / "sub" / "edit.md").write_text("v2", encoding="utf-8")
        (regdir / "new.md").write_text("새 파일", encoding="utf-8")
        cs = A.changeset(VAULT, reg)
        check("추가를 집는다", cs["added"] == ["00_Domain/csview/new.md"], cs)
        check("삭제를 집는다", cs["removed"] == ["00_Domain/csview/gone.md"], cs)
        check("수정을 집는다(하위 디렉터리 포함)",
              cs["modified"] == ["00_Domain/csview/sub/edit.md"], cs)
        check("차이가 있으면 pending", A.state(VAULT, reg) == "pending")
        approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "정리")
        check("반려 뒤 차이 없음", A.changeset(VAULT, reg)["modified"] == [])
    finally:
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── stale은 막다른 상태가 아니다 — 봉합 승인 (Mechanism §3 5항) ─────────
def test_stale_sealed_by_approve():
    """극대가 여럿이면 '모든 head를 잇는 사용자의 새 기록이 봉합한다'(§3 5항).
    네 조작이 전부 stale을 거부하면 그 길이 막혀 다기기 병합 한 번이 영역을
    영구히 고착시킨다 — 봉합 승인(base=None)이 그 길이다."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/sealed"
    regdir = ROOT / "00_Domain" / "sealed"
    kept = A.ledger_path(VAULT).read_text(encoding="utf-8") if A.ledger_path(VAULT).exists() else ""
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        approval_acts.protect(VAULT, reg, "지정")
        # 두 기기가 각각 승인 → git 병합이 두 줄을 남긴다(인과 극대 2)
        head = A.records(VAULT)[-1]["rid"]
        rid = head
        with open(A.ledger_path(VAULT), "a", encoding="utf-8") as fh:
            for i in (1, 2):
                rid = core._next_rid(rid)
                fh.write(json.dumps({
                    "rid": rid, "parents": [head], "at": core.now_iso(),
                    "kind": "approve", "region": reg, "base": A.approved_hash(VAULT, reg),
                    "accepted": region_tree.working_tree_hash(VAULT, reg), "reason": f"기기{i}"},
                    ensure_ascii=False) + "\n")
        check("병합 뒤 stale", A.state(VAULT, reg) == "stale")
        forks = [f["rid"] for f in A.divergence(VAULT, reg)]
        check("갈래가 둘로 보인다", len(forks) == 2)
        check("일반 승인은 거부(현행 승인본이 하나가 아니다)",
              _raises(lambda: approval_acts.approve(VAULT, reg, "sha256:" + "a" * 64,
                                        region_tree.working_tree_hash(VAULT, reg)))())
        check("갈래 집합 없는 봉합도 거부",
              _raises(lambda: approval_acts.approve(VAULT, reg, None,
                                        region_tree.working_tree_hash(VAULT, reg)))())
        # 검토 사이 새 갈래 유입 — 검토한 집합과 어긋나면 봉합 거부(본 적 없는
        # 갈래를 함께 봉합하지 않는다)
        rid3 = core._next_rid(max((r["rid"] for r in A.records(VAULT)),
                                  key=core.rid_key))
        with open(A.ledger_path(VAULT), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "rid": rid3, "parents": [head], "at": core.now_iso(),
                "kind": "approve", "region": reg, "base": A.approved_hash(VAULT, reg),
                "accepted": region_tree.working_tree_hash(VAULT, reg), "reason": "기기3(유입)"},
                ensure_ascii=False) + "\n")
        check("본 적 없는 갈래는 함께 봉합되지 않는다",
              _raises(lambda: approval_acts.approve(VAULT, reg, None, region_tree.working_tree_hash(VAULT, reg),
                                        seal_heads=forks))())
        forks = [f["rid"] for f in A.divergence(VAULT, reg)]   # 3갈래 재검토
        (regdir / "a.md").write_text("봉합 시점 상태", encoding="utf-8")
        rec = approval_acts.approve(VAULT, reg, None, expect_work=region_tree.working_tree_hash(VAULT, reg),
                        reason="분기 봉합", seal_heads=forks)
        check("봉합 승인이 검토한 갈래 셋을 모두 부모로 잇는다",
              len(rec["parents"]) == 3, rec["parents"])
        check("봉합 뒤 clean", A.state(VAULT, reg) == "clean")
        check("현행 승인본이 봉합 시점 상태", A.approved_hash(VAULT, reg) == rec["accepted"])
        check("이후 일반 승인이 다시 성립한다", A.divergence(VAULT, reg) and
              len(A.divergence(VAULT, reg)) == 1)
    finally:
        try: A.ledger_path(VAULT).write_text(kept, encoding="utf-8")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


def test_stale_changeset_before_confirmation():
    """#38: 각 갈래의 실제 차이가 확인보다 먼저 보이고, 불완전 비교는 멈춘다."""
    import contextlib, io
    from osk import approvals as A, cli, approval_acts, region_tree
    reg = "00_Domain/regr-seal-preview"
    directory = ROOT / reg
    saved = {p: p.read_bytes() if p.exists() else None
             for p in (A.ledger_path(VAULT), A.moves_path(VAULT))}

    def decline(_prompt):
        raise SystemExit("declined")

    def preview(confirm=decline):
        output, error = io.StringIO(), None
        with contextlib.redirect_stdout(output),\
                mock.patch.object(cli, "_confirm", side_effect=confirm) as prompt,\
                mock.patch.object(approval_acts, "approve", wraps=approval_acts.approve) as approve:
            try:
                cli.main(["approve", reg])
            except (SystemExit, ValueError, OSError) as exc:
                error = str(exc)
        return output.getvalue(), error, prompt.call_count, approve.call_count

    try:
        directory.mkdir(parents=True)
        (directory / "a.txt").write_text("branch A", encoding="utf-8")
        (directory / "gone.txt").write_text("old", encoding="utf-8")
        moved_id = "260802-zzzz-9038"
        src, dst = directory / "before.md", directory / "after.md"
        src.write_text(node_text(moved_id), encoding="utf-8")
        initial = approval_acts.protect(VAULT, reg)
        tree_a = initial["accepted"]
        A.record_move(VAULT, moved_id, src, dst)
        src.rename(dst)
        (directory / "a.txt").write_text("branch B", encoding="utf-8")
        (directory / "branch-only.txt").write_text("B", encoding="utf-8")
        tree_b = region_tree.store_tree(VAULT, directory)
        forks, rid = [], initial["rid"]
        for kind, tree, boundary in (("approve", tree_a, initial["moves_seen"]),
                                     ("revert", tree_b, A.moves_boundary(VAULT)),
                                     ("unprotect", tree_b, A.moves_boundary(VAULT))):
            rid = core._next_rid(rid)
            forks.append({"rid": rid, "parents": [initial["rid"]],
                          "region": reg, "kind": kind, "base": tree,
                          "accepted": tree if kind == "approve" else None,
                          "moves_seen": boundary})
        with A.ledger_path(VAULT).open("a", encoding="utf-8") as f:
            for row in forks:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        (directory / "a.txt").write_text("working", encoding="utf-8")
        (directory / "gone.txt").unlink()
        for i in range(25):
            (directory / f"new-{i:02}.txt").write_text("new", encoding="utf-8")
        work, ledger = region_tree.working_tree_hash(VAULT, reg), A.ledger_path(VAULT).read_bytes()
        a = A.changeset(VAULT, reg, record=forks[0], expect_work=work)
        b = A.changeset(VAULT, reg, record=forks[1], expect_work=work)
        check("stale은 갈래를 명시하지 않으면 변경집합을 고르지 않는다",
              A.changeset(VAULT, reg) is None)
        check("각 갈래의 추가·삭제·수정을 그 승인본과 비교한다",
              f"{reg}/branch-only.txt" in a["added"]
              and f"{reg}/branch-only.txt" not in b["added"]
              and f"{reg}/gone.txt" in a["removed"]
              and f"{reg}/a.txt" in b["modified"])
        check("이동의 생애 경계도 그 갈래를 따른다",
              a["moves"] == [{"node": moved_id, "from": f"{reg}/before.md",
                              "to": f"{reg}/after.md"}] and not b["moves"])
        check("해제 갈래는 해제 직전 승인본을 보여준다",
              A.changeset(VAULT, reg, record=forks[2], expect_work=work) == b)
        text, error, prompted, called = preview()
        check("세 갈래 변경집합을 확인 전에 모두 보여준다",
              prompted == 1 and called == 0 and error == "declined"
              and all(row["rid"] in text for row in forks)
              and text.count(f"{reg}/new-24.txt") == 3
              and "보호 해제 갈래" in text
              and f"{reg}/before.md → {reg}/after.md" in text)
        check("검토 거절은 승인 기록을 만들지 않는다", A.ledger_path(VAULT).read_bytes() == ledger)

        read_table = region_tree.tree_table_for_region
        with mock.patch.object(region_tree, "tree_table_for_region", side_effect=lambda vault, r, h:
                               None if h == tree_b else read_table(vault, r, h)):
            _, error, prompted, called = preview()
        check("어느 갈래든 승인본 미해석이면 확인 전에 중단한다",
              error and "승인본" in error and prompted == called == 0)
        with mock.patch.object(region_tree, "working_tree_hash", return_value=core.sha256_bytes(b"old")):
            _, error, prompted, called = preview()
        check("표시한 차이는 검토 대상으로 고정한 작업본에 결속된다",
              error and "작업본이 바뀌었다" in error and prompted == called == 0)
        with mock.patch.object(region_tree, "region_files", side_effect=PermissionError("unreadable")):
            _, error, prompted, called = preview()
        check("작업본 판독 실패를 빈 변경집합으로 승인하지 않는다",
              error and prompted == called == 0)

        def changed_after_preview(_prompt):
            (directory / "a.txt").write_text("changed after review", encoding="utf-8")

        _, error, prompted, called = preview(changed_after_preview)
        check("확인 중 편집도 기존 작업본 CAS가 봉합을 거부한다",
              prompted == called == 1 and error and "작업본" in error
              and A.ledger_path(VAULT).read_bytes() == ledger)
    finally:
        rmtree_force(directory)
        for path, data in saved.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)


# ── 승인 조작은 노드 쓰기와 같은 잠금으로 직렬화된다 (PR #14 리뷰) ──────
def test_approval_serialized_with_writes():
    """반려의 마지막 전제 확인과 첫 파괴 사이에 정상 쓰기가 끼어들면 검토하지
    않은 변경이 사라진다. 네 조작 모두 노드 쓰기와 **같은** 전역 변경 잠금을
    잡는다 — 엔진이 통제하는 writer는 그 사이에 들어올 수 없다."""
    from osk import approvals as A, approval_acts, region_tree
    from osk import write
    import sync_daemon
    check("변경 잠금이 sync·update가 쓰는 그 잠금과 같은 파일이다",
          core.mutation_lock_path(VAULT)
          == sync_daemon._lock_path(ROOT, "osk-mutation.lock"))
    check("잠금 파일은 추적 트리 밖이다",
          not str(core.mutation_lock_path(VAULT)).startswith(str(ROOT) + os.sep)
          or ".git" in core.mutation_lock_path(VAULT).parts)
    # 같은 경로를 가리키는 것만으로는 부족하다 — 실제로 상호배제되는지 본다.
    # flock은 open file description마다이므로 같은 프로세스의 두 번째 열기도
    # 경합한다(데몬이 다른 프로세스에서 잡는 상황과 같은 판정).
    with core.mutation_lock(VAULT):
        with open(sync_daemon._lock_path(ROOT, "osk-mutation.lock"), "w") as fh:
            try:
                core.lock_exclusive(fh, blocking=False)
                core.unlock(fh)
                got = True
            except OSError:
                got = False
        check("변경 잠금 보유 중에는 데몬 쪽 획득이 실패한다(실 상호배제)",
              got is False)
    held = []
    real = core.mutation_lock

    class _Spy(real):
        def __enter__(self):
            held.append(1)
            return super().__enter__()
    reg = "00_Domain/lockchk"
    regdir = ROOT / "00_Domain" / "lockchk"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        approval_acts.mutation_lock = _Spy
        approval_acts.protect(VAULT, reg, "지정")
        (regdir / "a.md").write_text("v2", encoding="utf-8")
        approval_acts.approve(VAULT, reg, A.approved_hash(VAULT, reg),
                  expect_work=region_tree.working_tree_hash(VAULT, reg), reason="승인")
        (regdir / "a.md").write_text("v3", encoding="utf-8")
        approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "반려")
        approval_acts.unprotect(VAULT, reg, "해제")
        check("네 조작이 모두 잠금을 잡았다", len(held) == 4, len(held))
    finally:
        approval_acts.mutation_lock = real
        shutil.rmtree(regdir, ignore_errors=True)


# ── 지정 뒤의 동시 편집은 오류가 아니라 다음 변경집합이다 ──────────────
def test_protect_concurrent_write_becomes_pending():
    """스냅샷 직후의 정상 동시 편집은 지정을 무효로 만들지 않는다 — 영역이
    곧바로 pending으로 드러나고 사용자가 승인하거나 반려하면 된다. 이것을 하드
    오류로 바꾸면 자가 치유되는 상태를 재시도로 바꿀 뿐이다(파괴적인 반려에만
    작업본 결속을 건다)."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Domain/pcas"
    regdir = ROOT / "00_Domain" / "pcas"
    f = regdir / "a.md"
    real_append = A.ledger_append
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("A", encoding="utf-8")

        def racing(path, record, expect=None):
            if record.get("kind") == "protect":       # 박제 뒤·append 전 편집
                approval_acts.ledger_append = real_append
                f.write_text("B", encoding="utf-8")
            return real_append(path, record, expect)
        approval_acts.ledger_append = racing
        rec = approval_acts.protect(VAULT, reg, "지정")
        approval_acts.ledger_append = real_append
        check("지정은 성립한다", bool(rec.get("rid")))
        check("그 편집은 곧바로 변경집합으로 드러난다", A.state(VAULT, reg) == "pending")
        check("승인본은 스냅샷 시점 상태", A.approved_hash(VAULT, reg) == rec["accepted"])
        approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "반려")
        check("반려로 처분하면 clean", A.state(VAULT, reg) == "clean")
        check("작업본이 스냅샷 상태로 복원", f.read_text() == "A")
    finally:
        approval_acts.ledger_append = real_append
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 해제도 잠금 안 전제 재확인을 거친다 (PR #14 리뷰) ───────────────────
def test_unprotect_precondition_under_lock():
    """해제 판정과 append 사이에 다른 기기의 승인이 동기화로 들어오면 해제는
    성립하지 않는다 — 그러지 않으면 stale이 아니라 unprotected가 되어 사용자가
    보지 못한 최신 승인이 조용히 해제로 덮인다(행의 base도 이미 거짓이다)."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Scope/W2"
    f = ROOT / "00_Scope/W2/regr-unp.md"
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    real_append = A.ledger_append
    try:
        approval_acts.protect(VAULT, reg, "지정")
        base = A.approved_hash(VAULT, reg)
        d = core.resolve_in_root(VAULT, reg)
        f.write_text("v2", encoding="utf-8")
        other = region_tree.store_tree(VAULT, d)          # 다른 기기가 승인할 tree(B)
        f.write_text("v1", encoding="utf-8")          # 작업본은 되돌려 clean
        check("해제 직전 상태는 clean", A.state(VAULT, reg) == "clean")

        def racing(path, record, expect=None):
            if record.get("kind") == "unprotect":     # 판정 뒤·append 전에 유입
                approval_acts.ledger_append = real_append
                real_append(A.ledger_path(VAULT), {
                    "kind": "approve", "region": reg, "base": base,
                    "accepted": other, "reason": "다른 기기(시험)"})
            return real_append(path, record, expect)
        approval_acts.ledger_append = racing
        check("유입 승인이 있으면 해제 거부", _raises(lambda: approval_acts.unprotect(VAULT, reg))())
        approval_acts.ledger_append = real_append
        check("영역은 여전히 보호 중", A.is_protected(VAULT, reg))
        check("현행 승인본은 유입된 승인의 것", A.approved_hash(VAULT, reg) == other)
    finally:
        approval_acts.ledger_append = real_append
        try: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg), "정리")
        except Exception: pass
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── stale은 해제가 아니다 — 파일 판정이 fail-open 하지 않는다 ───────────
def test_stale_region_not_unprotected():
    """인과 극대가 둘이면(다기기 병합) 영역은 stale이다 — 판정 불능이지 해제가
    아니므로, 현황에서 사라지거나 파일 판정이 '보호영역 밖 = 제약 없음'으로
    새면 안 된다(fail-open 금지)."""
    from osk import approvals as A, approval_acts, region_tree
    reg = "00_Scope/W2"
    f = ROOT / "00_Scope/W2/regr-stale.md"
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    try:
        approval_acts.protect(VAULT, reg, "지정")
        check("보호 직후 승인본 일치", A.file_matches_baseline(VAULT, f))
        # 같은 head를 부모로 갖는 기록 두 줄 = 다기기 병합 결과(인과 극대 2).
        # ledger_append는 parents를 스스로 정하므로, 병합 결과 파일을 그대로
        # 흉내내려면 대장에 직접 적는다(git merge가 남기는 상태와 동일).
        head = A.records(VAULT)[-1]["rid"]
        base_h, work_h = A.approved_hash(VAULT, reg), region_tree.working_tree_hash(VAULT, reg)
        kept = A.ledger_path(VAULT).read_text(encoding="utf-8")     # 시험 뒤 되돌릴 원본
        rid = head
        with open(A.ledger_path(VAULT), "a", encoding="utf-8") as fh:
            for i in (1, 2):
                rid = core._next_rid(rid)
                fh.write(json.dumps({
                    "rid": rid, "parents": [head], "at": core.now_iso(),
                    "kind": "approve", "region": reg, "base": base_h,
                    "accepted": work_h, "reason": f"기기{i}(시험)"},
                    ensure_ascii=False) + "\n")
        check("영역이 stale", A.state(VAULT, reg) == "stale")
        check("stale 영역도 현황에 남는다", reg in A.protected_regions(VAULT))
        check("stale 영역 안의 파일은 보호영역 밖으로 새지 않는다",
              A.region_of(VAULT, f) == reg)
        check("integrity도 stale을 적발",
              any("stale" in e and reg in e for e in A.integrity(VAULT)))
        f.write_text("아무도 승인한 적 없는 내용", encoding="utf-8")
        check("stale에서 파일 판정은 불일치(fail-closed)",
              A.file_matches_baseline(VAULT, f) is False)
        new = ROOT / "00_Scope/W2/regr-stale-new.md"
        new.write_text("아무도 본 적 없는 새 파일", encoding="utf-8")
        check("stale 영역에 새로 생긴 파일도 불일치",
              A.file_matches_baseline(VAULT, new) is False)
        new.unlink(missing_ok=True)
    finally:
        try: A.ledger_path(VAULT).write_text(kept, encoding="utf-8")   # 분기 원상 복구
        except Exception: pass
        f.unlink(missing_ok=True)
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass


# ── 중첩 영역: 안쪽 승인이 바깥 미승인을 가리지 않는다 ──────────────────
def test_nested_regions_all_checked():
    """파일이 여러 보호영역에 속하면 **전부**의 승인본과 일치해야 한다 — 하위
    구획만 승인하고 바깥 영역은 그 변경을 승인한 적 없는데 일치로 새면 안 된다."""
    from osk import approvals as A, approval_acts, region_tree
    outer, inner = "00_Scope/W3", "00_Scope/W3/sub"
    od, idir = ROOT / "00_Scope/W3", ROOT / "00_Scope/W3/sub"
    x = idir / "x.md"
    try:
        idir.mkdir(parents=True, exist_ok=True)
        x.write_text("v1", encoding="utf-8")
        approval_acts.protect(VAULT, outer, "바깥 지정")
        approval_acts.protect(VAULT, inner, "안쪽 지정")
        check("초기에는 양쪽 다 일치", A.file_matches_baseline(VAULT, x))
        x.write_text("v2", encoding="utf-8")
        approval_acts.approve(VAULT, inner, A.approved_hash(VAULT, inner),
                  expect_work=region_tree.working_tree_hash(VAULT, inner), reason="안쪽만 승인")
        check("안쪽은 clean", A.state(VAULT, inner) == "clean")
        check("바깥은 pending", A.state(VAULT, outer) == "pending")
        check("안쪽 승인만으로는 일치로 판정되지 않는다",
              A.file_matches_baseline(VAULT, x) is False)
        check("가장 안쪽 영역만 물으면 일치", A.file_in_region_baseline(VAULT, inner, x))
    finally:
        for r in (inner, outer):
            try: approval_acts.unprotect(VAULT, r, "정리")
            except Exception: pass
        shutil.rmtree(od, ignore_errors=True)


# ── 승인본은 그 영역의 tree여야 한다 (PR #14 리뷰 [high]) ──────────────
def test_baseline_bound_to_region():
    """승인본 manifest는 **그 영역의** tree일 때만 해석된다 — 영역 밖 항목을 섞은
    tree는 정상 protect/approve가 만들 수 없으므로, 자기 파일 항목이 맞아도 권한
    판정이 성립하지 않는다(권위는 성립인데 복원은 거부되는 승인본 금지)."""
    from osk import approvals as A, authority, approval_acts, region_tree
    reg = authority.delegation_region(VAULT)
    dnode = ROOT / "00_Person/Delegation/regr-bind.md"
    outsider = ROOT / "00_Domain/regr-outside.md"
    clause = ("## 위임\n- 대상: 시험 행위\n- 범위: 시험\n"
              "- 조건: 없음\n- 종료: 없음\n")
    dnode.write_text(node_text("260802-zzzz-rgd3", "영역 결속 시험", clause),
                     encoding="utf-8")
    outsider.write_text("영역 밖 파일", encoding="utf-8")
    good = None
    try:
        approval_acts.protect(VAULT, reg, "지정")
        good = A.approved_hash(VAULT, reg)
        check("정상 승인본에서는 위임 성립", A.file_in_region_baseline(VAULT, reg, dnode))
        # 위임 파일 항목은 현재 파일과 정확히 맞추고, 영역 밖 항목을 하나 섞는다
        entries = sorted(([r, h] for r, h in region_tree.tree_table(VAULT, good).items()),
                         key=lambda e: e[0])
        entries.append(["00_Domain/regr-outside.md",
                        region_tree.store_put(VAULT, outsider.read_bytes())])
        entries.sort(key=lambda e: e[0])
        evil = region_tree.store_put(VAULT, region_tree.manifest_blob(entries))
        check("혼합 tree 자체는 형상 검증을 통과", region_tree.tree_table(VAULT, evil) is not None)
        check("그러나 그 영역의 tree로는 해석되지 않는다",
              region_tree.tree_table_for_region(VAULT, reg, evil) is None)
        core.ledger_append(A.ledger_path(VAULT), {
            "kind": "approve", "region": reg, "base": good,
            "accepted": evil, "reason": "영역 밖 항목 혼입(시험)"})
        check("integrity가 영역 불일치를 적발",
              any("영역 불일치" in e and reg in e for e in A.integrity(VAULT)), A.integrity(VAULT))
        check("위임 성립이 부정된다(fail-closed)",
              A.file_in_region_baseline(VAULT, reg, dnode) is False)
        eff = {d["title"]: d["effective"] for d in authority.enumerate_delegations(VAULT)}
        check("권위 판정도 미성립", eff.get("regr-bind") is False, eff)
        check("revert도 거부",
              _raises(lambda: approval_acts.revert(VAULT, reg, A.approved_hash(VAULT, reg),
                                       region_tree.working_tree_hash(VAULT, reg)))())
        check("영역 밖 파일이 변하지 않음", outsider.read_text() == "영역 밖 파일")
    finally:
        if good:
            core.ledger_append(A.ledger_path(VAULT), {
                "kind": "approve", "region": reg, "base": None,
                "accepted": good, "reason": "시험 정리"})
        try: approval_acts.unprotect(VAULT, reg, "정리")
        except Exception: pass
        dnode.unlink(missing_ok=True)
        outsider.unlink(missing_ok=True)


# ── 14f. 파싱 불가 위임 파일이 권한 검사를 죽이지 않는다 ────────────────
def test_broken_delegation_isolated():
    memo = ROOT / "00_Person/Delegation/temp-memo.md"
    try:
        memo.write_text("frontmatter 없는 임시 메모\n", encoding="utf-8")
        v = authority.check(VAULT, "아무 행위")
        check("파싱 불가 위임 파일에도 authority.check가 산다",
              v["verdict"] == "hold")
        dels = authority.enumerate_delegations(VAULT)
        check("파싱 불가 파일은 위임으로 세지 않는다(fail-closed)",
              all(not d["effective"] for d in dels if d.get("broken")))
        check("broken으로 표면화된다", any(d.get("broken") for d in dels))
    finally:
        memo.unlink(missing_ok=True)



# ── 24. 군집 허브 노드는 위임이 아니다 (시행령 §5 1항) ─────────────────────
def test_index_node_not_delegation():
    """Delegation Facet도 노드 군집이라 동명 허브을 두는데(헌법 3조 8항),
    v3.3.0에서 허브가 승인본에 들어가는 순간 위임으로 열거되어 절 형식
    검사가 영구 실패했다(실측). 열거가 허브를 걸러야 두 제도가 공존한다."""
    from osk import authority, validate
    idx = ROOT / "00_Person/Delegation/Delegation.md"
    made = not idx.exists()
    if made:
        idx.write_text(node_text("260802-zzzz-dgix", "위임 Facet 허브"),
                       encoding="utf-8")
    try:
        dels = authority.enumerate_delegations(VAULT)
        check("허브 노드는 위임으로 열거되지 않는다",
              all(d["title"] != "Delegation" for d in dels), dels)
        rep = validate.run(VAULT, cli.published_tools)
        check("허브 노드가 위임 3요건을 깨뜨리지 않는다",
              not any("위임 3요건" in k for d in rep["fail"] for k in d),
              rep["fail"])
    finally:
        if made:
            idx.unlink(missing_ok=True)
