"""회귀 수트의 한 갈래: 감사와 개정이 고정한 것 — 2026-09-02 전면 감사, 통치문서 개정.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import hashlib, json, os, shlex, shutil, subprocess, tempfile
from pathlib import Path

from regression.support import (
    _append_round, check, ENGINE, node_text, _raises, rmtree_force, ROOT, _rounds_of, VAULT, _vault_at, _w
)
from osk import core, graph, validate, contract, write, publish, node_moves, topology  # noqa: E402


# ── 감사 2026-09-02: 재현으로 확정한 결함의 고정 (이슈 #24~#43) ───────────
def test_audit_fixes_2026_09_02():
    """전면 감사(2026-09-02)가 재현으로 확정한 결함들을 고정한다.

    한 시나리오에 묶은 것은 이 검사들이 서로를 전제하지 않고 **되돌리면
    실패한다**는 성질만 공유하기 때문이다. 항목마다 무엇을 망가뜨리면 어느
    단언이 실패하는지 적는다 — 통과 건수가 아니라 그것이 이 검사들의 근거다.
    """
    from osk import approvals as A, raw as R, scope_memory as SM, update_source, update_txn, approval_acts
    mine, restore = [], []
    try:
        # ① 이식성 충돌은 표면이 만들지도, 이동이 덮지도 않는다 (#25)
        #    되돌리면 — `create_node`의 전역 이식성 키 검사를 지우면 첫 단언이,
        #    `move_nodes`의 `seen_keys`를 지우면 셋째·넷째가 실패한다.
        for sub in ("AudX", "AudY", "AudZ"):
            _w(write.create_node, VAULT, sub, f"{sub} 허브", "본문", "fable-5",
               space=f"00_Scope/W1/{sub}")
            mine.append(ROOT / f"00_Scope/W1/{sub}/{sub}.md")
        r = _w(write.create_node, VAULT, "AudFoo", "이식성 시험", "본문", "fable-5",
               space="00_Scope/W1/AudX")
        mine.append(ROOT / "00_Scope/W1/AudX/AudFoo.md")
        check("전제: 첫 이름은 만들어진다", r.get("ok"), r)
        r = _w(write.create_node, VAULT, "audfoo", "대소문자만 다른 이름", "본문",
               "fable-5", space="00_Scope/W1/AudY")
        check("대소문자만 다른 이름은 전역에서도 거부된다 (#25)",
              not r.get("ok")
              and any("이식성" in v for v in r.get("violations", [])), r)

        #    표면이 못 만드는 상태는 외부 기원으로만 온다(파일 복제·병합) —
        #    그 상태에서 이동이 덮지 않는지가 이 결함의 핵심이다.
        ext = ROOT / "00_Scope/W1/AudY/audfoo.md"
        ext.write_text(node_text("260802-audt-0f00", "외부 기원 동명"),
                       encoding="utf-8")
        mine.append(ext)
        r = _w(node_moves.move_nodes, VAULT, ["AudFoo", "audfoo"], "00_Scope/W1/AudZ")
        check("이식성 충돌 묶음 이동은 거부된다 (#25)",
              not r.get("ok"), r)
        check("아무것도 옮기지 않았다 (#25)",
              (ROOT / "00_Scope/W1/AudX/AudFoo.md").is_file() and ext.is_file(),
              sorted(p.name for p in (ROOT / "00_Scope/W1/AudZ").glob("*.md")))
        ext.unlink()

        # ② 엣지 델타가 라운드 좌표를 지우지 않는다 (#26)
        #    되돌리면 — 델타 루프를 `contract.edge_targets`로 되돌리면 실패한다.
        p = ROOT / "00_Scope/W1/audit-edge.md"
        p.write_text(node_text(
            "260802-audt-0e01", "라운드 좌표 보존",
            extra='derived-from: "[[00_Scope/W1/_raw/audrec#3]]"\n'),
            encoding="utf-8")
        mine.append(p)
        r = _w(write.update_node, VAULT, "audit-edge", add_edges={"derived-from": "W1"})
        got = str(contract.parse(p).meta.get("derived-from"))
        check("엣지를 더해도 `#라운드`가 남는다 (#26)",
              "audrec.txt#3" in got, got)
        r = _w(write.update_node, VAULT, "audit-edge",
               remove_edges={"derived-from": "W1"})
        got = str(contract.parse(p).meta.get("derived-from"))
        check("엣지를 빼도 `#라운드`가 남는다 (#26)", "audrec.txt#3" in got, got)
        #    같은 호출 안의 중복은 한 번만 앉는다
        r = _w(write.update_node, VAULT, "audit-edge",
               add_edges={"derived-from": ["W1", "W1", "[[W1]]"]})
        got = contract.parse(p).meta.get("derived-from")
        check("한 호출 안의 중복 엣지는 한 번만 앉는다 (#26)",
              str(got).count("[[W1]]") == 1, got)

        # ③ 앵커 편집은 파서가 읽을 형태로 쓴다 (#27)
        #    되돌리면 — `norm_body`의 개행 정규화를 지우면 둘째가 실패한다.
        r = _w(write.update_node, VAULT, "audit-edge",
               old_text="본문", new_text="첫 줄\r\n둘째 줄")
        check("앵커 편집이 성공한다 (#27)", r.get("ok"), r)
        check("쓴 바이트에 `\\r`이 남지 않는다 (#27)",
              b"\r" not in p.read_bytes(), p.read_bytes()[-40:])
        r = _w(write.update_node, VAULT, "audit-edge",
               old_text="첫 줄\r\n둘째 줄", new_text="되찾음")
        check("CRLF 앵커로도 그 자리를 다시 찾는다 (#27)", r.get("ok"), r)
        #    전문 치환 경로도 같다 — 앵커 정규화와 **별개로** `norm_body`가
        #    접는다. 앵커만 접으면 이 자리가 열린 채 남는다.
        r = _w(write.update_node, VAULT, "audit-edge", body="가\r\n나\r\n",
               expect_hash=core.sha256_file(p))
        check("전문 치환도 `\\r`을 쓰지 않는다 (#27)",
              r.get("ok") and b"\r" not in p.read_bytes(), r)

        # ④ 전문 치환 거부문은 해시를 내주지 않는다 (#33)
        #    되돌리면 — 거부문에 `sha256_file(path)`를 도로 넣으면 실패한다.
        r = _w(write.update_node, VAULT, "audit-edge", body="통째로 새 본문")
        check("해시 없는 전문 치환은 거부된다 (#33)", not r.get("ok"), r)
        check("거부문이 현재 해시를 내주지 않는다 (#33)",
              "sha256:" not in json.dumps(r, ensure_ascii=False), r)

        # ⑤ 하위 군집의 첫 결속은 **scope**에 선다 (#28)
        #    되돌리면 — `bind_session(session, dest_dir.name)`으로 되돌리면 실패.
        r = _w(write.create_node, VAULT, "audit-bind", "하위 군집 결속", "본문",
               "fable-5", session="audit-sub-sess", space="00_Scope/W1/AudX")
        mine.append(ROOT / "00_Scope/W1/AudX/audit-bind.md")
        check("하위 군집에서 결속해도 scope 이름에 묶인다 (#28)",
              r.get("bound_scope") == "W1"
              and write.bindings(VAULT, "audit-sub-sess")["scopes"] == ["W1"], r)
        write.set_primary(VAULT, "audit-sub-sess", "W1", "시험 지정")

        # ⑥ 결속 밖 scope 착지는 받아 결속에 더한다 (v5.2 — 시행령 §3 10항 · Mechanism §6-2 6항)
        #    되돌리면 — create_node가 결속과 다른 scope를 거부하면 실패한다.
        other = ROOT / "00_Scope/AudOther"
        other.mkdir(exist_ok=True)
        (other / "AudOther.md").write_text(node_text("260802-audt-0oth", "AudOther 허브"), encoding="utf-8")
        r = _w(write.create_node, VAULT, "audit-cross", "교차 착지", "본문", "fable-5",
               session="audit-sub-sess", space="00_Scope/AudOther")
        check("결속 밖 scope 착지는 통과하고 결속에 더한다 (v5.2)",
              r.get("ok") and r.get("bound_scope") == "AudOther"
              and write.bindings(VAULT, "audit-sub-sess")["scopes"] == ["W1", "AudOther"]
              and write.resolve_session(VAULT, "audit-sub-sess") == "W1", r)
        for p in (other / "audit-cross.md", other / "AudOther.md"):
            p.unlink(missing_ok=True)
        try:
            other.rmdir()
        except OSError:
            pass

        # ⑦ 작업 상태 참조 금지는 소속을 가리지 않는다 (#36)
        #    되돌리면 — `node_checks.topology_of`의 workbench 갈래를 scope 안으로 되돌리면
        #    Domain 노드가 통과해 실패한다.
        note = ROOT / "00_Scope/Workbench/audit-note.md"
        note.write_text("작업 상태(비노드)\n", encoding="utf-8")
        mine.append(note)
        dom = ROOT / "00_Domain/AudD"
        dom.mkdir(parents=True, exist_ok=True)
        (dom / "AudD.md").write_text(
            node_text("260802-audt-0d01", "AudD 허브"), encoding="utf-8")
        mine.append(dom / "AudD.md")
        dnode = dom / "audit-dom.md"
        dnode.write_text(node_text(
            "260802-audt-0d02", "작업 상태 참조",
            extra='derived-from: "[[00_Scope/Workbench/audit-note]]"\n'),
            encoding="utf-8")
        mine.append(dnode)
        errs = topology.topology_check(VAULT, graph.Index(VAULT))
        check("Domain의 작업 상태 참조를 검증기가 잡는다 (#36)",
              any("작업 상태는 근거로 쓰지 않는다" in e and "audit-dom" in e
                  for e in errs), [e for e in errs if "audit-dom" in e])
        dnode.unlink()

        # ⑧ 동명 노드는 읽기·후보 상정도 고르지 않는다 (#35)
        #    되돌리면 — read_node의 `dup_stems` 갈래나 record_candidate의
        #    `live_locate`를 되돌리면 실패한다.
        for sub in ("AudX", "AudY"):
            (ROOT / f"00_Scope/W1/{sub}/audit-dup.md").write_text(
                node_text(f"260802-audt-0{'a' if sub == 'AudX' else 'b'}11",
                          "동명 시험"), encoding="utf-8")
            mine.append(ROOT / f"00_Scope/W1/{sub}/audit-dup.md")
        r = _w(write.record_candidate, VAULT, "duplication",
               ["audit-dup", "audit-edge"])
        check("동명 노드는 충돌 후보 당사자로도 고르지 않는다 (#35)",
              not r.get("ok"), r)
        try:
            import mcp_server as _srv
            rr = _srv.read_node("audit-dup")
            check("읽기 표면도 동명 노드를 고르지 않는다 (#35)",
                  "error" in rr and "정해지지 않는다" in rr["error"], rr)
        except Exception as e:                       # 표면 판독 불가는 별도 결함
            check("읽기 표면도 동명 노드를 고르지 않는다 (#35)", False, repr(e))
        for sub in ("AudX", "AudY"):
            (ROOT / f"00_Scope/W1/{sub}/audit-dup.md").unlink()

        # ⑨ pin은 노드 id에도 걸린다 (#36)
        #    되돌리면 — `_plan_move`의 `cluster_gate.pinned` 검사를 지우면 실패한다.
        nid = contract.parse(ROOT / "00_Scope/W1/AudX/audit-bind.md").id
        core.ledger_append(VAULT.pins, {"kind": "pin", "target": nid,
                                       "reason": "감사 회귀 시험"})
        r = _w(node_moves.move_nodes, VAULT, ["audit-bind"], "00_Scope/W1/AudZ")
        core.ledger_append(VAULT.pins, {"kind": "unpin", "target": nid,
                                       "reason": "시험 종료"})
        check("노드 id pin이 이동을 막는다 (#36)",
              not r.get("ok") and any("pin" in v for v in
                                      r.get("violations", [])), r)

        # ⑩ `overview`는 별칭 해소 결과를 내지 않는다 (#36)
        try:
            import mcp_server as _srv
            ov = _srv.overview(session="audit-sub-sess")
            check("표면이 별칭 정본 키를 노출하지 않는다 (#36)",
                  "session_canonical" not in ov, sorted(ov))
        except Exception as e:
            check("표면이 별칭 정본 키를 노출하지 않는다 (#36)", False, repr(e))

        # ⑪ `_raw`는 `\r`이 섞여도 계속 이어 쓸 수 있다 (#27)
        #    되돌리면 — `raw.read_exact`를 `read_text`로 되돌리면 둘째가 실패한다.
        write.bind_session(VAULT, "audit-raw-sess", "W1", "감사 회귀 시험")
        r1 = _w(_append_round, "audit-raw-sess", "audit-crlf",
                "도구 출력\r\n두 줄", "응답")
        check("전제: `\\r`이 든 라운드가 기록된다 (#27)", r1.get("ok"), r1)
        r2 = _w(_append_round, "audit-raw-sess", "audit-crlf", "다음", "응답2")
        check("그 뒤에도 append가 성립한다 (#27)", r2.get("ok"), r2)
        n = _rounds_of("audit-raw-sess", "audit-crlf")
        check("라운드 수가 정확히 세어진다 (#27)", n == 2, n)
        mine.append(ROOT / r1["path"])

        # ⑫ scope 기억의 해시 사슬이 `\r`에 끊기지 않는다 (#27)
        #    되돌리면 — `scope_memory._read`를 `read_text`로 되돌리면 실패한다.
        smp = SM.sm_path(VAULT, "W1")
        prior = smp.read_bytes() if smp.is_file() else None
        restore.append((smp, prior))
        w = _w(SM.replace, VAULT, "audit-raw-sess", "첫 줄\r\n둘째 줄",
               SM._read(smp) and core.sha256_bytes(
                   SM._read(smp).encode("utf-8")) or None)
        rd = _w(SM.read, VAULT, "audit-raw-sess")
        check("쓰기가 낸 해시와 읽기의 해시가 같다 (#27)",
              w.get("ok") and rd.get("hash") == w.get("hash"), (w, rd))

        # ⑬ 연속 3회 거부는 재시도 지시를 **거둔다** (#34)
        #    되돌리면 — `stop` 분기를 지워 retry를 늘 붙이면 셋째가 실패한다.
        big = "가" * (SM.LIMIT + 40)
        seen = []
        for _ in range(3):
            seen.append(_w(SM.replace, VAULT, "audit-raw-sess", big,
                           rd.get("hash") if not seen else seen[-1].get("hash")))
        last = " ".join(seen[-1].get("violations", []))
        check("3회째 거부가 재시도를 지시하지 않는다 (#34)",
              "다시 보내지 마라" in last and "다시 보내라" not in last, last)
        check("3회째 거부가 접으라고 말한다 (#34)", "접고" in last, last)
        #    읽기가 연속을 끊는다
        _w(SM.read, VAULT, "audit-raw-sess")
        again = _w(SM.replace, VAULT, "audit-raw-sess", big, rd.get("hash"))
        check("읽기 뒤의 거부는 다시 1회째다 (#34)",
              "다시 보내지 마라" not in " ".join(again.get("violations", [])),
              again)

        # ⑭ 손상된 대장 위에서는 쓰지 않는다 (#30·#31)
        #    되돌리면 — `routing_errors`를 지우면 파일이 남아 둘째가 실패하고,
        #    `approvals.ledger_is_damaged`를 지우면 셋째가 `unprotected`가 되어 실패한다.
        rj = VAULT.routing
        rprior = rj.read_bytes() if rj.is_file() else b""
        restore.append((rj, rprior))
        rj.write_bytes(rprior + b'{"kind": "bind", "session": "x", '
                                b'"scope": "W1", "rid": "not-a-rid"}\n')
        victim = ROOT / "00_Scope/W1/audit-damaged.md"
        r = _w(write.create_node, VAULT, "audit-damaged", "손상 위 쓰기", "본문",
               "fable-5", session="audit-damage-sess", space="00_Scope/W1")
        check("라우팅 대장이 손상되면 쓰지 않는다 (#31)", not r.get("ok"), r)
        check("파일도 남지 않는다 (#31)", not victim.exists(), victim.exists())
        rj.write_bytes(rprior)

        aj = VAULT.ledger / "approvals.jsonl"
        aprior = aj.read_bytes() if aj.is_file() else b""
        restore.append((aj, aprior))
        approval_acts.protect(VAULT, "00_Scope/W1/AudZ", "감사 회귀 시험")
        good = aj.read_bytes()
        aj.write_bytes(good.replace(b'"rid": "0', b'"rid": "Z', 1))
        check("승인 대장이 손상되면 영역은 미보호가 아니라 stale이다 (#30)",
              A.state(VAULT, "00_Scope/W1/AudZ") == "stale", A.state(VAULT, "00_Scope/W1/AudZ"))
        check("손상 위에서는 반려가 파일을 건드리기 전에 거부된다 (#30)",
              _raises(lambda: approval_acts.revert(VAULT, "00_Scope/W1/AudZ", "sha256:x",
                                       "sha256:y"))())
        aj.write_bytes(good)
        approval_acts.unprotect(VAULT, "00_Scope/W1/AudZ", "시험 종료")

        # ⑮ 성립할 수 없는 영역은 지정되지 않는다 (#37)
        check("대장 구획은 보호영역이 될 수 없다 (#37)",
              _raises(lambda: approval_acts.protect(VAULT, "00_Scope/Workbench/_ledger", "시험"))())
        check("vault 루트는 보호영역이 될 수 없다 (#37)",
              _raises(lambda: approval_acts.protect(VAULT, ".", "시험"))())

        # ⑯ 표면 AST 검사가 scope 기억 통로를 본다 (#37)
        check("SURFACE_MODULES가 scope 기억 통로를 담는다 (#37)",
              "osk/scope_memory.py" in validate.SURFACE_MODULES,
              validate.SURFACE_MODULES)

        # ⑰ mtime 탐침은 프로세스마다 유일하다 (#32)
        #    되돌리면 — 고정 이름으로 되돌리면 둘째가 실패한다(같은 이름 재사용).
        seen_probe = set()
        real_mkstemp = graph.tempfile.mkstemp

        def _spy(*a, **kw):
            fd, nm = real_mkstemp(*a, **kw)
            seen_probe.add(nm)
            return fd, nm
        graph.tempfile.mkstemp = _spy
        try:
            graph._probe_mtime_granularity(VAULT, rounds=2)
            graph._probe_mtime_granularity(VAULT, rounds=2)
        finally:
            graph.tempfile.mkstemp = real_mkstemp
        check("탐침 두 번이 서로 다른 파일을 쓴다 (#32)",
              len(seen_probe) == 2, sorted(seen_probe))
        check("탐침 파일이 남지 않는다 (#32)",
              not any(Path(n).exists() for n in seen_probe), sorted(seen_probe))

        # ⑱ fetch_git 체크아웃은 기기의 autocrlf에 흔들리지 않는다 (#41)
        #    되돌리면 — `config core.autocrlf false`를 지우면 실패한다.
        with tempfile.TemporaryDirectory() as _td:
            src = Path(_td) / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"a\nb\n")
            for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                        ["config", "user.name", "t"],
                        ["config", "core.autocrlf", "false"],
                        ["add", "-A"], ["commit", "-qm", "x"],
                        ["tag", "v0.0.1"]):
                subprocess.run(["git", "-C", str(src), *cmd],
                               check=True, capture_output=True)
            gcfg = Path(_td) / "gitconfig"
            gcfg.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
            dest = Path(_td) / "dst"
            dest.mkdir()
            _old = os.environ.get("GIT_CONFIG_GLOBAL")
            os.environ["GIT_CONFIG_GLOBAL"] = str(gcfg)
            try:
                tree = update_source.fetch_git(str(src), "v0.0.1", dest)
                got = (tree / "f.txt").read_bytes()
            finally:
                if _old is None:
                    os.environ.pop("GIT_CONFIG_GLOBAL", None)
                else:
                    os.environ["GIT_CONFIG_GLOBAL"] = _old
            check("정본 체크아웃이 기기 설정과 무관하게 바이트 동일하다 (#41)",
                  got == b"a\nb\n", got)

        # ⑲ 발행이 비준증빙과 릴리스 워크플로를 지우지 않는다 (#41)
        man = publish.parse_manifest(
            ENGINE / "scripts" / "publish-manifest.txt")
        check("발행 매니페스트가 release.json을 보존한다 (#41)",
              "release.json" in man["keep"], man["keep"])
        check("발행 매니페스트가 릴리스·회귀 수트 워크플로를 보존한다 (#41)",
              {".github/workflows/release.yml", ".github/workflows/ci.yml"}
              <= set(man["keep"]), man["keep"])

        # ⑳ 엔진과 **독립된** 복구 부트스트랩도 롤백을 끝낸다 (#24)
        #    되돌리면 — `scripts/recover.py`의 `_fsync_file`을 읽기 전용 핸들로
        #    되돌리면 Windows에서 EBADF가 나 rc=2로 중단되고 표식이 남는다.
        #    엔진이 반쯤 교체된 상태에서 유일하게 남는 길이 이것이라, 엔진 쪽
        #    같은 결함을 고쳐도 이 사본이 낡으면 복구는 여전히 불가능하다.
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "osk_recover_audit", ENGINE / "scripts" / "recover.py")
        _rec = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_rec)
        _rf = ROOT / "_governance/audit-recov.md"
        _rf.write_bytes("원본\n".encode("utf-8"))
        _orig = _rf.read_bytes()
        mine.append(_rf)
        os.chmod(_rf, 0o600)           # mode 항목이 있어야 그 경로를 지난다
        update_txn.txn_begin(VAULT, "txnAUDR", "v9.9.9", ["_governance/audit-recov.md"])
        _rf.write_bytes(b"HALF\n")     # 반쯤 적용된 상태를 모사
        _rc = _rec._recover(ROOT)
        check("독립 복구 부트스트랩이 롤백을 끝낸다 (#24)",
              _rc == 0 and _rf.read_bytes() == _orig
              and not update_txn.txn_manifest(VAULT).exists(), (_rc, _rf.read_bytes()))
    finally:
        for p, data in reversed(restore):
            try:
                if data is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(data)
            except OSError:
                pass
        for p in mine:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        for d in ("00_Scope/W1/AudX", "00_Scope/W1/AudY", "00_Scope/W1/AudZ",
                  "00_Domain/AudD"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

def test_governance_amend_secrets_and_region():
    """Mechanism §9 1항(비밀값 경계)·§3 4항(영역 tree 제외) 개정의 고정.

    이 둘은 **조문이 정본**이라 코드만 고칠 수 없었던 자리다. 그래서 검사도
    둘을 함께 본다 — 동작이 맞는가, 그리고 구현이 조문과 같은가.

    무엇을 망가뜨리면 실패하는가:
      · 패턴의 경계를 `\\b`로 되돌리면 → 직결 양성 넷이 실패한다
      · 조문 표만 고치고 코드를 안 고치면(또는 그 반대) → 동치 단언이 실패한다
      · `region_tree.SKIP_DIRS`에서 `_scope_memory`를 빼면 → 보호영역 단언 둘이 실패한다
    """
    from osk import approvals as A, scope_memory as SM, secrets, approval_acts, region_tree
    reg = "00_Scope/Workbench"
    smp, prior, protected = SM.sm_path(VAULT, "W1"), None, False
    try:
        # ① 한글 조사가 직결된 토큰도 걸러진다 (#29)
        cases = [
            ("github-token", "토큰 ghp_" + "a" * 36 + "를 썼다"),
            # 완성된 키 형상을 **소스에 두지 않는다.** 정식 릴리스의 전제
            # 검사는 스냅샷의 전 파일을 이 필터로 훑고 `osk/secrets.py`만
            # 면제하므로, 여기 완전형을 적으면 이 파일의 `aws-access-key`
            # 적중으로 **릴리스 선언이 항상 막힌다**(경계를 고친 뒤로는 뒤에
            # 한글이 붙어도 매치한다 — 그것이 이 개정의 요지다).
            ("aws-access-key", "키 AKIA" + "IOSFODNN7EXAMPLE" + "이다"),
            ("openai-style-key", "sk-ant-api03-" + "x" * 30 + "가 새었다"),
            ("google-api-key", "AIza" + "b" * 35 + "로 불렀다"),
        ]
        for name, sample in cases:
            out, hits = secrets.filter_text(sample)
            check(f"조사 직결 토큰이 걸러진다: {name} (#29)",
                  name in hits and sample != out, (hits, out[:40]))
        #    ASCII가 앞뒤에 붙으면 여전히 토큰이 아니다 — 경계를 넓힌 것이
        #    아니라 비ASCII만 경계로 인정한 것이다.
        for sample in ("NOTAKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLEX"):
            _o, hits = secrets.filter_text(sample)
            check(f"ASCII 인접은 토큰이 아니다: {sample[:24]} (#29)",
                  not hits, hits)
        check("비밀값 fixture가 통과한다 (#29)",
              not secrets.self_test(), secrets.self_test())

        # ② 구현이 §9 1항의 표를 **그대로** 담는다 (#29)
        #    조문이 필터의 정본이므로, 한쪽만 고치는 것이 다시 가능해지면 안 된다.
        declared = validate.declared_secret_patterns(VAULT)
        check("규범에서 패턴 표를 읽는다 (#29)",
              declared is not None and len(declared) == len(secrets.PATTERNS),
              declared and sorted(declared))
        check("구현과 규범의 패턴이 동치다 (#29)",
              not validate.secret_pattern_drift(VAULT),
              validate.secret_pattern_drift(VAULT))

        #    이 저장소의 **소스 자신**이 자기 필터에 걸리지 않아야 한다.
        #    정식 릴리스의 전제는 스냅샷 전 파일을 이 필터로 훑고 `osk/secrets.py`
        #    만 면제하므로, 어딘가에 완성된 키 형상이 있으면 선언이 **항상**
        #    막힌다. 경계를 고치면서 이 위험이 커졌다 — 뒤에 한글이 붙어 통과하던
        #    문자열이 이제 적중한다. fixture는 문자열 조합으로 쓴다.
        src_hits = []
        for _p in sorted(ENGINE.rglob("*.py")) + sorted(
                ENGINE.parent.glob("*.md")):
            if "__pycache__" in _p.parts:
                continue
            if _p.name == "secrets.py" and "osk" in _p.parts:
                continue                       # guard_secrets와 같은 면제
            _o, _h = secrets.filter_text(
                _p.read_text(encoding="utf-8", errors="ignore"))
            if _h:
                src_hits.append((_p.name, sorted(set(_h))))
        check("소스가 자기 비밀값 필터에 걸리지 않는다 — 릴리스 차단 방지 (#29)",
              not src_hits, src_hits)

        # ③ scope 기억은 영역 tree에 들지 않는다 (#37)
        #    되돌리면 — 케이던스가 쓸 때마다 영역이 pending이 되고, 반려가 다른
        #    기기의 공유 기억까지 지운다.
        prior = smp.read_bytes() if smp.is_file() else None
        write.bind_session(VAULT, "gov-amend-sess", "W1", "개정 회귀 시험")
        st0 = _w(SM.read, VAULT, "gov-amend-sess")
        r = _w(SM.replace, VAULT, "gov-amend-sess", "개정 전 기억", st0.get("hash"))
        check("전제: scope 기억을 쓸 수 있다 (#37)", r.get("ok"), r)
        approval_acts.protect(VAULT, reg, "개정 회귀 시험")
        protected = True
        check("전제: 지정 직후 clean (#37)", A.state(VAULT, reg) == "clean",
              A.state(VAULT, reg))
        r = _w(SM.replace, VAULT, "gov-amend-sess", "개정 뒤 기억", r.get("hash"))
        check("전제: 그 위에 다시 쓴다 (#37)", r.get("ok"), r)
        check("scope 기억 쓰기가 영역을 pending으로 만들지 않는다 (#37)",
              A.state(VAULT, reg) == "clean", A.state(VAULT, reg))

        # ④ 제외 구획은 영역의 루트로도 지정되지 않는다 (#37)
        check("scope 기억 구획은 보호영역이 될 수 없다 (#37)",
              _raises(lambda: approval_acts.protect(VAULT, "00_Scope/Workbench/_scope_memory",
                                        "시험"))())

        # ⑤ **개정 전 승인본의 이행** — 이 규칙이 바뀌기 전에 지정된 영역의
        #    승인본은 `_scope_memory/*`를 담고 있다. 그 상태에서
        #      · 복원 계획이 그 자리를 건드리지 않아야 하고(공유 기억 보호),
        #      · 반려는 **파괴 전에** 멈춰야 하며,
        #      · 한 번의 승인으로 해소돼야 한다.
        #    되돌리면 — `tree_table_for_region`의 걸러내기를 지우면 첫 단언이,
        #    `revert`의 사전 거부를 지우면 둘째가 실패한다.
        #    개정 전 승인본을 **실제로 그 영역의 현행 승인본으로** 세운다 —
        #    저장소에 manifest만 넣어 두면 반려가 기존 base-CAS에 먼저 걸려
        #    이행 관문을 태우지 못한다(첫 판이 그래서 뮤턴트를 살렸다).
        old_mem = "과거 기억\n".encode("utf-8")
        # 승인본에만 있고 지금은 없는 파일을 하나 넣는다 — 반려가 **되살리는**
        # 쪽도 함께 시험하려는 것이고, 그래야 이행이 만드는 manifest가 이 vault에
        # 한 번도 저장된 적 없는 것이 된다(그러지 않으면 지정 시점의 manifest와
        # 우연히 같아져 "저장했는가"를 묻는 단언이 헛돈다).
        keep_rel = reg + "/audit-keep.md"
        keep_body = "승인본에만 있던 파일\n".encode("utf-8")
        legacy_entries = [[rel, region_tree.store_put(VAULT, p.read_bytes())]
                          for rel, p in region_tree.region_files(VAULT, ROOT / reg)]
        legacy_entries.append([keep_rel, region_tree.store_put(VAULT, keep_body)])
        legacy_entries.append([reg + "/_scope_memory/W1.md",
                               region_tree.store_put(VAULT, old_mem)])
        legacy_entries.sort(key=lambda e: e[0])
        legacy_hash = region_tree.store_put(VAULT, region_tree.manifest_blob(legacy_entries))
        core.ledger_append(A.ledger_path(VAULT), {
            "kind": "approve", "region": reg, "base": A.approved_hash(VAULT, reg),
            "accepted": legacy_hash, "reason": "개정 전 형상 모사"})
        check("개정 전 승인본의 제외 구획 항목이 드러난다 (#37)",
              region_tree.legacy_excluded(VAULT, legacy_hash, reg)
              == [reg + "/_scope_memory/W1.md"], region_tree.legacy_excluded(VAULT, legacy_hash, reg))
        tbl = region_tree.tree_table_for_region(VAULT, reg, legacy_hash)
        check("복원 table에서 그 자리가 빠진다 — 공유 기억을 덮지 않는다 (#37)",
              tbl is not None and reg + "/_scope_memory/W1.md" not in tbl, tbl)
        cs = A.changeset(VAULT, reg)
        check("파일 차이가 없어도 pending이고 그 사유를 알린다 (#37)",
              A.state(VAULT, reg) == "pending" and cs is not None
              and cs.get("legacy_excluded"), (A.state(VAULT, reg), cs))

        #    **반려는 막히지 않는다.** 막으면 그 영역의 일반 파일 변경을 버릴
        #    길이 사라져, 버리려던 것을 승인해야만 벗어나는 정반대 결과가 된다.
        #    되돌리면 — `revert`의 이행(`record_base`)을 지우면 최종 확인에서
        #    실패해 `extra`가 지워진 채 기록이 안 남는다.
        extra = ROOT / (reg + "/audit-extra.md")
        extra.write_text("에이전트가 더한 것 — 버릴 것\n", encoding="utf-8")
        _now_mem = smp.read_bytes()
        rec = approval_acts.revert(VAULT, reg, legacy_hash, region_tree.working_tree_hash(VAULT, reg),
                       reason="개정 전 승인본에서의 반려")
        check("개정 전 승인본에서도 반려가 성립한다 (#37)",
              rec.get("kind") == "revert", rec)
        check("버리려던 일반 파일 변경이 실제로 버려진다 (#37)",
              not extra.exists(), extra.exists())
        check("승인본에만 있던 파일은 되살아난다 (#37)",
              (ROOT / keep_rel).read_bytes() == keep_body,
              (ROOT / keep_rel).exists())
        check("반려가 공유 기억은 건드리지 않는다 (#37)",
              smp.read_bytes() == _now_mem, smp.read_bytes()[:30])
        check("그 반려가 새 형상의 baseline을 세운다 (#37)",
              A.state(VAULT, reg) == "clean"
              and not region_tree.legacy_excluded(VAULT, A.approved_hash(VAULT, reg), reg),
              (A.state(VAULT, reg), region_tree.legacy_excluded(VAULT, A.approved_hash(VAULT, reg), reg)))
        check("이행된 승인본은 저장소에서 해석된다 (#37)",
              region_tree.tree_table_for_region(VAULT, reg, A.approved_hash(VAULT, reg)) is not None,
              A.approved_hash(VAULT, reg))

        # ⑥ 줄바꿈만 다른 것은 변경집합이 그렇게 말한다 (#39)
        #    EOL 고정 이행에서는 영역 전체가 "수정"으로 보이는데 `git diff`는
        #    빈 출력이다 — 내용이 그대로임을 표면이 말하지 않으면 사용자는
        #    무엇을 승인하는지 모르는 채 승인하게 된다.
        #    되돌리면 — `changeset`의 `eol_only` 계산을 지우면 실패한다.
        eolf = ROOT / (reg + "/audit-eol.md")
        eolf.write_bytes("한 줄\n두 줄\n".encode("utf-8"))
        approval_acts.approve(VAULT, reg, A.approved_hash(VAULT, reg),
                  expect_work=region_tree.working_tree_hash(VAULT, reg), reason="EOL 시험 기준")
        eolf.write_bytes("한 줄\r\n두 줄\r\n".encode("utf-8"))   # 줄바꿈만 바꾼다
        cs2 = A.changeset(VAULT, reg)
        check("줄바꿈만 바뀌어도 수정으로 잡힌다 — 판정은 raw 바이트다 (#39)",
              A.state(VAULT, reg) == "pending"
              and reg + "/audit-eol.md" in (cs2 or {}).get("modified", []), cs2)
        check("그것이 **줄바꿈만** 다르다고 표시된다 (#39)",
              reg + "/audit-eol.md" in (cs2 or {}).get("eol_only", []), cs2)
        #    내용이 실제로 다르면 그 표시가 붙지 않는다
        eolf.write_bytes("한 줄\n다른 줄\n".encode("utf-8"))
        cs3 = A.changeset(VAULT, reg)
        check("내용이 다르면 줄바꿈 표시가 붙지 않는다 (#39)",
              reg + "/audit-eol.md" not in (cs3 or {}).get("eol_only", []), cs3)
        eolf.unlink(missing_ok=True)
        approval_acts.approve(VAULT, reg, A.approved_hash(VAULT, reg),
                  expect_work=region_tree.working_tree_hash(VAULT, reg), reason="EOL 시험 정리")

        # ⑦ 내용 주소 저장소는 줄바꿈 변환 대상이 아니다 (#39)
        #    `.gitattributes`가 `-text`로 못박지 않으면 CRLF를 담은 blob이
        #    이행 중에 바뀌어 그 승인본이 통째로 해석 불능이 된다(실측).
        ga = (ENGINE.parent.parent / ".gitattributes").read_text(
            encoding="utf-8")
        check("`.gitattributes`가 저장소 전체를 LF로 못박는다 (#39)",
              any(l.split()[:1] == ["*"] and "eol=lf" in l
                  for l in ga.splitlines() if l.strip()
                  and not l.startswith("#")), ga[:80])
        check("내용 주소 저장소는 변환에서 제외된다 (#39)",
              any("_ledger/approved/objects/" in l and "-text" in l
                  for l in ga.splitlines()), ga[:80])

        # ⑧ **바이트 그대로인 구획**은 실제 git 판정에서도 변환 밖이다 (#39)
        #    줄이 있는지가 아니라 git이 그 경로를 어떻게 보는지를 묻는다 —
        #    패턴이 틀리면(`_raw/*` 대 `**/_raw/**`) 줄은 있는데 적용이 안 된다.
        #    `_raw/`는 append 접두부 판정이 raw 바이트이고(§9 4항), scope 기억은
        #    `canon`이 개행을 접지 않아 CAS 해시가 개행에 민감하다. 변환이 끼면
        #    확정된 원자료가 조용히 바뀌고, 되돌릴 수단이 없다(실측: CRLF 6개를
        #    담은 기록이 이행 뒤 0개).
        #    되돌리면 — 두 `-text` 줄 중 하나를 지우면 그 구획이 실패한다.
        lab = Path(tempfile.mkdtemp(prefix="osk-attr-"))
        try:
            _g = lambda *a: subprocess.run(("git",) + a, cwd=str(lab),
                                           capture_output=True, text=True)
            _g("init", "-q", "-b", "main")
            (lab / ".gitattributes").write_bytes(ga.encode("utf-8"))
            for space, path in (("_cited", "00_Scope/W/_cited/.records/claude-x.txt"),
                                ("_raw", "00_Scope/W/_raw/2026-09-02.md"),
                                ("_scope_memory", "00_Scope/W/_scope_memory/W.md"),
                                ("일반 노드", "00_Person/누구.md")):
                out = _g("check-attr", "text", "--", path).stdout.strip()
                verdict = out.rsplit(": ", 1)[-1]
                want = "unset" if space != "일반 노드" else "auto"
                check(f"git이 {space}를 EOL 변환 {'밖' if want == 'unset' else '안'}"
                      f"으로 본다 (#39)", verdict == want, out)
        finally:
            rmtree_force(lab)

        # ⑨ **문서에 적힌 이행 명령 그대로** 돌려, 그것이 append-only 원자료의
        #    blob을 건드리지 않는지 본다 (#39). `check-attr`만 보는 ⑧은 이걸
        #    잡지 못한다 — `-text`를 받은 구획은 clean 필터가 사라져 오히려
        #    작업 트리 바이트가 그대로 새 blob이 되기 때문이다. `autocrlf=true`
        #    기기의 현실(blob은 LF, 작업 트리는 CRLF, status는 clean)을 그대로
        #    세우고 실측한다: 문서대로면 무변, 구판 명령(`--renormalize .`)이면
        #    과거 기록의 blob이 LF→CRLF로 소급 수정된다.
        #    명령을 **문서에서 읽어** 쓰므로 문서가 되돌아가면 여기서 실패한다.
        setup = (ENGINE.parent.parent / "docs/SETUP.md").read_text(
            encoding="utf-8")
        cmds = [l.strip() for l in setup.splitlines()
                if l.strip().startswith("git add --renormalize")]
        check("이행 명령이 문서에 한 줄로 있다 (#39)", len(cmds) == 1, cmds)
        lab2 = Path(tempfile.mkdtemp(prefix="osk-renorm-"))
        try:
            _g = lambda *a: subprocess.run(("git",) + a, cwd=str(lab2),
                                           capture_output=True, text=True)
            _g("init", "-q", "-b", "main")
            _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
            _g("config", "core.autocrlf", "true")
            raw_rel = "00_Scope/W/_raw/2026-09-02.md"
            crlf = bytes([13, 10])
            body = ("## 1" + chr(10) + chr(10) + "붙여넣은 로그" + chr(10)
                    ).encode("utf-8").replace(bytes([10]), crlf)
            #    정상 승인 객체 하나 — blob은 옳고(LF) 구판 checkout이 작업
            #    트리만 CRLF로 펼쳐 놓는다. 문서의 명령이 이것을 stage하면
            #    옳던 blob이 CRLF가 되어 이름과 내용이 갈린다(리뷰 지적).
            _ob = ("# 노드" + chr(10)).encode("utf-8")
            _oh = hashlib.sha256(_ob).hexdigest()
            obj_rel = ("00_Scope/Workbench/_ledger/approved/objects/"
                       + _oh[:2] + "/" + _oh[2:])
            for rel, data in ((raw_rel, body), (obj_rel, _ob),
                              ("00_Person/누구.md",
                               ("# 누구" + chr(10)).encode("utf-8"))):
                (lab2 / rel).parent.mkdir(parents=True, exist_ok=True)
                (lab2 / rel).write_bytes(data)
            _g("add", "-A"); _g("commit", "-q", "-m", "구판 커밋(속성 없음)")
            #    autocrlf=true의 체크아웃 — 작업 트리만 CRLF가 된다
            _g("rm", "--cached", "-r", ".", "-q"); _g("reset", "--hard", "-q")
            before = _g("rev-parse", "HEAD:" + raw_rel).stdout.strip()
            check("전제: blob은 LF인데 작업 트리는 CRLF이고 clean이다 (#39)",
                  crlf in (lab2 / raw_rel).read_bytes()
                  and not _g("status", "--porcelain").stdout.strip(),
                  _g("status", "--porcelain").stdout)
            (lab2 / ".gitattributes").write_bytes(ga.encode("utf-8"))
            _g("add", ".gitattributes"); _g("commit", "-q", "-m", "attrs")
            r = subprocess.run(shlex.split(cmds[0]), cwd=str(lab2),
                               capture_output=True, text=True)
            check("문서의 이행 명령이 성립한다 (#39)", r.returncode == 0,
                  r.stderr[:200])
            staged = [x for x in _g("diff", "--cached", "--name-only")
                      .stdout.splitlines() if x.strip()]
            check("이행이 append-only 원자료를 stage하지 않는다 (#39)",
                  raw_rel not in staged, staged)
            check("이행이 정상 승인 객체도 stage하지 않는다 (#39)",
                  obj_rel not in staged, staged)
            if staged:
                _g("commit", "-q", "-m", "정규화")
            check("과거 기록의 blob이 그대로다 (#39)",
                  _g("rev-parse", "HEAD:" + raw_rel).stdout.strip() == before,
                  (before, _g("rev-parse", "HEAD:" + raw_rel).stdout.strip()))
            #    3단계 재전개가 그 구획을 blob 그대로 되펼친다
            _g("rm", "--cached", "-r", ".", "-q"); _g("reset", "--hard", "-q")
            check("재전개가 원자료를 blob 바이트 그대로 펼친다 (#39)",
                  crlf not in (lab2 / raw_rel).read_bytes(),
                  (lab2 / raw_rel).read_bytes()[:40])
            check("승인 객체의 내용이 이행 뒤에도 이름의 digest와 맞는다 (#39)",
                  hashlib.sha256((lab2 / obj_rel).read_bytes()).hexdigest()
                  == _oh, (lab2 / obj_rel).read_bytes()[:40])
        finally:
            rmtree_force(lab2)

        # ⑩ **승인 객체는 어느 쪽을 무조건 택해도 틀린다** (#39)
        #    `-text`는 변환을 멈출 뿐 이미 어긋난 것을 고르지 못한다. 두 경우가
        #    다 현실이다: ⓐ 정상 LF 객체를 구판 checkout이 작업 트리만 CRLF로
        #    펼쳐 놓은 것 — stage하면 옳던 blob이 깨진다. ⓑ 원본이 CRLF라
        #    구판 정규화로 blob이 깨진 것 — 빼 두면 재전개가 작업 트리까지
        #    덮어 양쪽 다 잃는다. 내용 주소에는 파일 이름이라는 심판이 있으므로
        #    그것으로 가른다(`reconcile_store`).
        #    되돌리면 — 판정을 한쪽으로 고정하면 두 경우 중 하나가 실패하고,
        #    fail-closed를 지우면 셋째 검사가 실패한다.
        def _reconcile_case(tag, name_body, work_body, idx_body, expect):
            """이름(digest)·작업 트리·색인을 각각 세워 판정을 본다."""
            # 시험 저장소의 루트도 `Vault.open()`이 주는 것처럼 정규 경로여야 한다 —
            # 러너 임시 경로가 별칭(RUNNER~1·/var)이면 보관소 경로만 날것으로 남는다.
            lab3 = Path(os.path.realpath(tempfile.mkdtemp(prefix="osk-obj-")))
            try:
                _h = hashlib.sha256(name_body).hexdigest()
                rel3 = ("00_Scope/Workbench/_ledger/approved/objects/"
                        + _h[:2] + "/" + _h[2:])
                (lab3 / rel3).parent.mkdir(parents=True, exist_ok=True)
                (lab3 / rel3).write_bytes(work_body)
                res = region_tree.reconcile_store(_vault_at(lab3), lambda rel: idx_body)
                got = ("stage" if res["stage"] else
                       "restore" if res["restore"] else
                       "broken" if res["broken"] else "none")
                check(f"승인 객체 판정 — {tag} (#39)",
                      got == expect and res["checked"] == 1, (got, res))
                return res
            finally:
                rmtree_force(lab3)

        _lf = ("# 노드" + chr(10)).encode("utf-8")
        _crlf = _lf.replace(bytes([10]), crlf)
        #    ⓐ blob이 옳고 작업 트리만 구판 checkout으로 CRLF가 된 것
        _reconcile_case("정상 LF 객체는 색인이 정본이다",
                        _lf, _crlf, _lf, "restore")
        #    ⓑ 원본이 CRLF라 구판 정규화로 색인 쪽이 깨진 것
        _reconcile_case("원본이 CRLF면 작업 트리가 정본이다",
                        _crlf, _crlf, _lf, "stage")
        #    ⓒ 어느 쪽도 이름과 맞지 않는 것
        _broken = _reconcile_case("양쪽 다 이름과 다르면 판정 불능이다",
                                  _lf, b"amount to nothing" + bytes([10]),
                                  b"and to nothing else" + bytes([10]),
                                  "broken")
        check("판정 불능이 하나라도 있으면 이행하지 않는다 (#39)",
              _broken["ok"] is False, _broken)
    finally:
        (ROOT / (reg + "/audit-keep.md")).unlink(missing_ok=True)
        (ROOT / (reg + "/audit-extra.md")).unlink(missing_ok=True)
        if protected:
            try:
                approval_acts.unprotect(VAULT, reg, "시험 종료")
            except Exception:
                pass
        try:
            if prior is None:
                smp.unlink(missing_ok=True)
            else:
                smp.write_bytes(prior)
        except OSError:
            pass
