"""회귀 수트의 한 갈래: 발행과 새 설치 — 가드, 매니페스트, 스냅샷 검증.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, node_text, ROOT, skip, VAULT, _vault_at
from osk import cli, core, validate, publish  # noqa: E402


# ── 14aa. 발행 절차 v2 — 매니페스트·가드 (모두 fail-closed) ─────────────
def _pub_fixture(td):
    """사설 mini-vault + 가짜 공개 저장소 + 매니페스트."""
    pub = Path(td) / "public"
    pub.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(pub)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(pub), "config", k, v], check=True)
    (pub / "LICENSE").write_text("MIT\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(pub), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(pub), "commit", "-qm", "init"], check=True)
    man = Path(td) / "manifest.txt"
    man.write_text(
        "MAP  _governance/ -> _governance/\n"
        "KEEP LICENSE\n"
        "DENY _ledger/\n"
        "DENY __pycache__/\n"
        "SKEL 00_Scope/\n", encoding="utf-8")
    return pub, man


def test_publish_manifest():
    with tempfile.TemporaryDirectory() as td:
        _pub, man = _pub_fixture(td)
        m = publish.parse_manifest(man)
        check("MAP·KEEP·DENY·SKEL 파싱",
              m["map"] == [("_governance/", "_governance/")]
              and m["keep"] == ["LICENSE"] and "_ledger/" in m["deny"]
              and m["skel"] == ["00_Scope/"], m)

        def raises(f):
            try:
                f(); return False
            except publish.PublishError:
                return True
        bad = Path(td) / "bad.txt"
        bad.write_text("PUBLISH everything\n", encoding="utf-8")
        check("미정의 지시어는 PublishError",
              raises(lambda: publish.parse_manifest(bad)))
        noarrow = Path(td) / "noarrow.txt"
        noarrow.write_text("MAP a/ b/\n", encoding="utf-8")
        check("MAP에 화살표가 없으면 거부",
              raises(lambda: publish.parse_manifest(noarrow)))
        empty = Path(td) / "empty.txt"
        empty.write_text("# 주석뿐\n", encoding="utf-8")
        check("MAP 없는 매니페스트 거부",
              raises(lambda: publish.parse_manifest(empty)))


def test_publish_guards():
    """가드는 전부 fail-closed다 — 하나라도 걸리면 아무것도 쓰지 않는다."""
    gov = ROOT / "_governance"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "records").mkdir(exist_ok=True)
    doc, rec = gov / "PubDoc.md", gov / "records" / "pub-rec.md"
    led = gov / "_ledger"
    mine = []
    try:
        # 통치 문서·사료는 특수한 노드 — 계약을 갖춘다 (시행령 §10 1항)
        doc.write_text(node_text("260802-pppp-0001", "발행 시험 문서"),
                       encoding="utf-8")
        rec.write_text(node_text("260802-pppp-0010", "발행 시험 사료"),
                       encoding="utf-8")
        led.mkdir(exist_ok=True)
        (led / "secret.jsonl").write_text('{"a":1}\n', encoding="utf-8")
        mine = [doc, rec, led / "secret.jsonl"]
        with tempfile.TemporaryDirectory() as td:
            pub, man = _pub_fixture(td)
            m = publish.parse_manifest(man)
            items = publish.collect(VAULT, m)
            rels = {r for _s, r in items}
            check("DENY가 대장을 제외한다",
                  not any("_ledger" in r for r in rels), sorted(rels))
            check("통치 문서와 사료는 포함",
                  "_governance/PubDoc.md" in rels
                  and "_governance/records/pub-rec.md" in rels, sorted(rels))

            # 통치 문서는 서명 없이 발행된다 — 비준은 정본 확정·비준증빙이고
            # 서명은 인스턴스의 수용 기록이다 (헌법 14조 1항·시행령 §10 2항).
            # 통치 구획 안의 노드형은 특수 노드로서 정상이다.
            check("통치 구획의 노드형은 지식 가드에 걸리지 않는다",
                  not publish.guard_knowledge(items),
                  publish.guard_knowledge(items))

            # 지식 유출 — 통치 구획 밖의 노드형 파일
            man2 = Path(td) / "m2.txt"
            man2.write_text("MAP  00_Scope/W1/ -> nodes/\n", encoding="utf-8")
            leak = ROOT / "00_Scope/W1/pub-leak.md"
            leak.write_text(node_text("260802-pppp-0002", "새어나갈 노드"),
                            encoding="utf-8")
            try:
                i2 = publish.collect(VAULT, publish.parse_manifest(man2))
                check("Space의 노드형 파일은 차단",
                      any("노드형" in e for e in publish.guard_knowledge(i2)),
                      publish.guard_knowledge(i2))
            finally:
                leak.unlink(missing_ok=True)

            # 비밀값
            rec.write_text('token = "ghp_' + "A" * 36 + '"\n', encoding="utf-8")
            se = publish.guard_secrets(items)
            check("비밀값이 들어 있으면 차단", any("비밀값" in e for e in se), se)
            check("보고에 비밀값 자체는 싣지 않는다",
                  not any("ghp_" in e for e in se))
            rec.write_text(node_text("260802-pppp-0010", "발행 시험 사료"),
                           encoding="utf-8")

            # 보고 모드는 아무것도 쓰지 않는다
            before = sorted(str(x.relative_to(pub)) for x in pub.rglob("*")
                            if x.is_file() and ".git/" not in str(x))
            p_items = [(s, r) for s, r in items]
            rep = publish.plan(pub, m, p_items)
            check("보고가 add를 정확히 센다",
                  len(rep["add"]) == len(p_items), rep)
            check("KEEP 파일은 remove에 들어가지 않는다",
                  "LICENSE" not in rep["remove"], rep["remove"])
            check("보고는 공개 트리를 건드리지 않는다",
                  sorted(str(x.relative_to(pub)) for x in pub.rglob("*")
                         if x.is_file() and ".git/" not in str(x)) == before)

            # 매니페스트 밖 파일이 디스크에 있어도 커밋되지 않는다
            # (`git add -A`가 가드를 통째로 우회한 실사고의 고정)
            _rep = validate.run(VAULT, cli.published_tools)
            check("가드 전제: 검증기 PASS", _rep["verdict"] == "PASS",
                  _rep["fail"])
            # 비ASCII 이름 — git ls-files의 기본 출력은 이 이름을 따옴표와
            # 8진 이스케이프로 감싼다. 그 문자열을 그대로 쓰면 want와 어긋나
            # 매번 remove로 잡히고 스테이지도 삭제도 빗나간다.
            ko = gov / "records" / "한글 사료.md"
            ko.write_text(node_text("260802-pppp-0011", "한글 이름 사료"),
                          encoding="utf-8")
            mine.append(ko)
            items = publish.collect(VAULT, m)
            p_ko = publish.plan(pub, m, items)
            check("비ASCII 경로가 인용부호 없이 판독된다",
                  not any(r.startswith('"') for r in p_ko["remove"]),
                  p_ko["remove"][:3])
            check("새 한글 파일은 add로 잡힌다",
                  "_governance/records/한글 사료.md" in p_ko["add"], p_ko["add"])
            stray = pub / "stray-leftover.md"
            stray.write_text(node_text("260802-pppp-0003", "떠도는 잔재"),
                             encoding="utf-8")
            # KEEP 파일의 **로컬 수정**은 발행이 검증하지 않는다(사설에 원본이
            # 없어 `items`에 들지 않으므로 `guard_secrets`도 `plan`도 못 본다).
            # 그러니 커밋에 실려서도 안 된다 — 실리면 CI workflow 같은 특권
            # 파일의 조용한 변경이 검증 밖으로 공개된다.
            keepf = pub / "LICENSE"
            keep_before = keepf.read_bytes() if keepf.is_file() else None
            keepf.write_text("로컬에서 고친 KEEP\n", encoding="utf-8")
            rep_a = publish.run(VAULT, pub, apply=True, message="가드 시험",
                                manifest=man)
            check("적용: 커밋됨", rep_a.get("committed"), rep_a)
            _kshow = subprocess.run(
                ["git", "-C", str(pub), "show", "HEAD:LICENSE"],
                capture_output=True, text=True)
            check("KEEP의 로컬 수정은 발행 커밋에 실리지 않는다",
                  _kshow.returncode != 0
                  or "로컬에서 고친" not in _kshow.stdout, _kshow.stdout[:60])
            check("그래도 KEEP 파일은 디스크에 남는다", keepf.is_file())
            if keep_before is not None:
                keepf.write_bytes(keep_before)
            tracked = subprocess.run(
                ["git", "-C", str(pub), "ls-files", "-z"], capture_output=True,
                text=True).stdout.split("\0")
            check("매니페스트 밖 파일은 커밋되지 않는다",
                  "stray-leftover.md" not in tracked, tracked)
            check("그 파일은 디스크에 그대로 남는다", stray.exists())
            check("한글 이름 파일은 실제로 커밋된다",
                  "_governance/records/한글 사료.md" in tracked, tracked)
            p_after = publish.plan(pub, m, publish.collect(VAULT, m))
            check("두 번째 발행에서 remove로 되잡히지 않는다",
                  not p_after["remove"] and not p_after["add"], p_after)
            stray.unlink()

            # 적용 — 가드를 직접 통과시킨 뒤 build로
            publish.build(p_items, m, pub)
            check("통치 문서가 공개에 있다", (pub / "_governance/PubDoc.md").exists())
            check("대장은 공개에 없다",
                  not (pub / "_governance/_ledger").exists())
            check("골격이 생긴다", (pub / "00_Scope/.gitkeep").exists())
    finally:
        for f in mine:
            f.unlink(missing_ok=True)
        (gov / "_ledger").rmdir() if led.is_dir() and not any(led.iterdir()) else None
        if (gov / "records").is_dir() and not any((gov / "records").iterdir()):
            (gov / "records").rmdir()


# ── 15b. 정본 릴리스와 갱신 (Mechanism §1-2 · 시행령 §10 6항) ────────────
def test_current_version_equivalent_heads():
    """#41: 판본 문자열뿐 아니라 같은 릴리스 증빙인 병렬 극대만 수렴한다."""
    from osk import update_state
    old = {"rid": core._make_rid(1754000000000, 0), "parents": [],
           "kind": "done", "version": "v3.16.0", "attest": "sha256:" + "a" * 64}
    a = {"rid": core._make_rid(1754000000001, 0), "parents": [old["rid"]],
         "kind": "done", "version": "v3.17.1", "attest": "sha256:" + "b" * 64}
    b = dict(a, rid=core._make_rid(1754000000002, 0))
    check("현재 판본: 동일 릴리스 병렬 적용은 동치로 수렴한다",
          update_state.current_version(VAULT, [old, a, b]) == "v3.17.1")
    check("현재 판본: 병렬 기록의 물리 순서와 무관하다",
          update_state.current_version(VAULT, [old, b, a]) == "v3.17.1")
    for title, changed in (
            ("서로 다른 판본", {"version": "v3.16.1"}),
            ("같은 판본의 서로 다른 증빙", {"attest": "sha256:" + "c" * 64}),
            ("구판·현행 기록 혼합", {"attest": None})):
        check(f"현재 판본: {title}는 미확정을 유지한다",
              update_state.current_version(VAULT, [old, a, dict(b, **changed)]) is None)
    legacy_a = {k: v for k, v in a.items() if k != "attest"}
    legacy_b = {k: v for k, v in b.items() if k != "attest"}
    check("현재 판본: 증빙 없는 구판의 병렬 기록은 추측하지 않는다",
          update_state.current_version(VAULT, [old, legacy_a, legacy_b]) is None)
    check("현재 판본: 구판 단일 기록과 빈 저널은 종전대로 해석한다",
          update_state.current_version(VAULT, [old, legacy_a]) == "v3.17.1"
          and update_state.current_version(VAULT, []) is None)
    for invalid in ("", "sha256:broken", ["same"]):
        check(f"현재 판본: 같은 값이어도 부적격 증빙은 동치 근거가 아니다 {invalid!r}",
              update_state.current_version(VAULT, [old, dict(a, attest=invalid),
                                      dict(b, attest=invalid)]) is None)
    next_done = dict(b, parents=[a["rid"]], version="v3.17.2",
                     attest="sha256:" + "c" * 64)
    check("현재 판본: 선형 갱신은 최신 인과 기록을 반환한다",
          update_state.current_version(VAULT, [old, a, next_done]) == "v3.17.2")


def test_fresh_install_protects_governance():
    """새 설치(GETTING-STARTED 2단계) — 태그 clone 위의 두 번째 `--apply`가 통치
    구획을 비준증빙 내용 그대로 보호영역으로 지정한다(사용자 판정 2026-09-24).
    v3.22.1 실측: 안내를 따르면 protected_regions `{}`로 끝났고, 통치 문서를 직접
    고쳐도 validate PASS·status 침묵이었다. 고쳐 둔 통치 문서는 축복하지 않고,
    미보호 설치에는 status·validate가 경고한다(실패가 아니다).

    정본은 이 저장소의 `_governance/`를 임시 git 저장소에 담아 로컬 태그로
    만든다(네트워크 없음). 설치는 안내 그대로 자식 프로세스가 clone 안의 엔진으로
    돈다 — 수트의 OSK_VAULT_ROOT를 물려주지 않는다."""
    from osk import release
    src, tag = ENGINE.parents[1], "v9.40.0"
    ls = subprocess.run(["git", "-C", str(src), "ls-files", "-z", "--",
                         "_governance", ".gitattributes"],
                        capture_output=True, text=True, encoding="utf-8")
    if ls.returncode != 0 or not ls.stdout:
        skip("새 설치의 통치 구획 보호", "엔진이 git 작업 트리 안에 있지 않다")
        return

    def git(root, *a):
        r = subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                           text=True, encoding="utf-8", timeout=300)
        assert r.returncode == 0, (a, r.stderr)
        return r.stdout

    with tempfile.TemporaryDirectory() as td:
        td = Path(os.path.realpath(td))
        can = td / "canon"
        for rel in filter(None, ls.stdout.split("\0")):
            if (src / rel).is_file():
                (can / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / rel, can / rel)
        for space in ("00_Scope", "00_Domain", "00_Person"):
            (can / space).mkdir(exist_ok=True)
            (can / space / ".gitkeep").write_bytes(b"")
        git(can, "init", "-q", "-b", "main")
        for k, v in (("user.name", "fixture"), ("user.email", "f@x"),
                     ("core.autocrlf", "false"), ("core.eol", "lf")):
            git(can, "config", k, v)
        git(can, "add", "-A")
        git(can, "commit", "-qm", "canon")
        (can / "release.json").write_bytes(json.dumps(
            release.build_attestation(can, tag), ensure_ascii=False).encode())
        git(can, "add", "release.json")
        git(can, "commit", "-qm", "release")
        git(can, "tag", tag)

        env = {k: v for k, v in os.environ.items() if k != "OSK_VAULT_ROOT"}

        def install(name, edit=None):
            v = td / name
            git(td, "clone", "-q", "-c", "core.autocrlf=false", "--branch", tag,
                str(can), str(v))
            (v / ".osk").mkdir()
            (v / ".osk/config.json").write_text(json.dumps(
                {"upstream": {"source": "git", "url": str(can), "pin": None}}),
                encoding="utf-8")
            if edit:
                edit(v)
            e = dict(env, PYTHONPATH=str(v / "_governance/_engine"),
                     PYTHONIOENCODING="utf-8")

            def run(*a):
                r = subprocess.run([sys.executable, "-m", *a], cwd=v, env=e,
                                   capture_output=True, text=True,
                                   encoding="utf-8", timeout=600)
                try:
                    return r.returncode, json.loads(r.stdout)
                except ValueError:
                    return r.returncode, {"stdout": r.stdout[-300:],
                                          "stderr": r.stderr[-600:]}
            first = run("osk.update", "--to", tag, "--apply")
            second = run("osk.update", "--to", tag, "--apply")
            return v, run, first, second

        def brief(code, r):                   # 실패 보고는 판정에 쓴 필드만
            return code, {k: r[k] for k in ("governance", "governance_protected",
                                            "applied", "stdout", "stderr") if k in r}

        v, run, (c1, r1), (c2, r2) = install("fresh")
        check("새 설치: 첫 --apply는 통치 구획 지정을 확인 대상으로 보인다",
              c1 == 2 and r1.get("approval_required")
              and r1.get("governance", {}).get("protect") == "establish"
              and "보호영역으로 지정" in r1.get("instruction", ""), brief(c1, r1))
        check("새 설치: 확인한 재시도가 적용과 함께 통치 구획을 지정한다",
              c2 == 0 and r2.get("applied")
              and r2.get("governance_protected") == "established", brief(c2, r2))
        _, st = run("osk.cli", "status")
        check("새 설치: 통치 구획이 clean 보호영역이고 경고가 없다",
              st.get("protected_regions") == {"_governance": "clean"}
              and "warnings" not in st, st)
        ledger = v / "00_Scope/Workbench/_ledger/approvals.jsonl"
        rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()
                if x.strip()] if ledger.is_file() else []
        check("새 설치: 지정 기록은 protect 한 행이고 확인한 검토에 결속된다",
              [(x.get("kind"), x.get("region")) for x in rows]
              == [("protect", "_governance")]
              and r2.get("review_id", "?") in rows[0].get("reason", ""), rows)
        bylaws = v / "_governance/Bylaws.md"
        bylaws.write_bytes(bylaws.read_bytes() + "\n로컬 수정\n".encode())
        _, st = run("osk.cli", "status")
        check("새 설치 뒤 통치 문서 직접 수정은 pending으로 드러난다",
              st.get("protected_regions") == {"_governance": "pending"}, st)

        def edit(vault):
            b = vault / "_governance/Bylaws.md"
            b.write_bytes(b.read_bytes() + "\n로컬 수정\n".encode())
        v, run, (c1, r1), (c2, r2) = install("edited", edit)
        check("고쳐 둔 통치 문서가 있으면 지정을 보류하고 그 경로를 보인다",
              c1 == 2 and r1.get("governance", {}).get("protect") == "withheld"
              and "_governance/Bylaws.md" in r1["governance"].get("unattested", []),
              brief(c1, r1))
        check("보류된 설치의 적용은 지정 없이 끝난다(축복하지 않는다)",
              c2 == 0 and r2.get("applied")
              and r2.get("governance_protected") == "withheld"
              and not (v / "00_Scope/Workbench/_ledger/approvals.jsonl").exists(),
              brief(c2, r2))
        _, st = run("osk.cli", "status")
        check("미보호 통치 구획은 status가 경고한다",
              st.get("protected_regions") == {}
              and any("_governance" in w for w in st.get("warnings", [])), st)
        cv, rep = run("osk.cli", "validate")
        check("미보호 통치 구획은 validate가 경고하되 FAIL로 세지 않는다",
              cv == 0 and rep.get("verdict") == "PASS"
              and "_governance" in rep.get("warnings", {}).get(
                  "governance_unprotected", ""),
              (cv, rep.get("fail"), rep.get("warnings", {}).get(
                  "governance_unprotected")))


def test_publish_binds_checked_bytes():
    """검사 후 원본 변경은 plan·설치·커밋의 바이트를 바꾸지 않는다."""
    src = ROOT / "_governance" / "publish-snapshot.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    checked = b"reviewed-marker\r\n"
    changed = b"changed-after-check-marker\r\n"
    src.write_bytes(checked)
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(src, ns=(fixed_ns, fixed_ns))
    held_guard = publish.guard_vault
    try:
        with tempfile.TemporaryDirectory() as td:
            pub, man = _pub_fixture(td)
            subprocess.run(["git", "-C", str(pub), "config", "core.autocrlf", "false"],
                           check=True)
            man.write_text(
                "MAP _governance/publish-snapshot.txt -> reviewed.txt\n"
                "KEEP LICENSE\n", encoding="utf-8")
            # run이 파일 내용 가드를 완료한 직후 원본을 변경한다.
            def changed_after_content_guards(root):
                src.write_bytes(changed)
                return held_guard(root)
            publish.guard_vault = changed_after_content_guards
            result = publish.run(VAULT, pub, apply=True, manifest=man,
                                 message="Fix publish checked-byte fixture")
            committed = subprocess.run(
                ["git", "-C", str(pub), "show", "HEAD:reviewed.txt"],
                check=True, capture_output=True).stdout
            check("발행의 원본 변경 hook이 실행됐다", src.read_bytes() == changed)
            check("발행 커밋은 검사한 바이트다", committed == checked, committed)
            check("발행 설치도 검사한 바이트다",
                  (pub / "reviewed.txt").read_bytes() == checked)
            check("발행은 스냅샷의 파일 시각을 보존한다",
                  (pub / "reviewed.txt").stat().st_mtime_ns == fixed_ns)
            check("발행 응답의 경로는 공개 경로다",
                  result["add"] == ["reviewed.txt"] and result["files"] == 1,
                  result)

            # 보고에서도 같은 snapshot을 비교하고 공개 파일은 건드리지 않는다.
            src.write_bytes(checked)
            before = subprocess.run(
                ["git", "-C", str(pub), "rev-parse", "HEAD"],
                check=True, capture_output=True).stdout
            reported = publish.run(VAULT, pub, manifest=man)
            after = subprocess.run(
                ["git", "-C", str(pub), "rev-parse", "HEAD"],
                check=True, capture_output=True).stdout
            check("발행 보고는 검사한 바이트를 비교한다",
                  reported["same"] == 1 and not reported["change"], reported)
            check("발행 보고는 공개 커밋을 만들지 않는다",
                  before == after and not reported["applied"])
    finally:
        publish.guard_vault = held_guard
        src.unlink(missing_ok=True)


def test_publish_validator_uses_snapshot():
    """원본의 동시 복구로 파손된 snapshot이 검증을 우회하지 못한다."""
    gov = ROOT / "_governance" / "publish-validation-race.md"
    other = ROOT / "00_Scope" / "W1" / "publish-unexported-race.md"
    gov.parent.mkdir(parents=True, exist_ok=True)
    good = node_text("260802-pubs-0001", "발행 snapshot 검증")
    original_guard = publish.guard_vault
    try:
        for label, broken in (("발행 대상", gov), ("발행 밖 노드", other)):
            gov.write_text(good, encoding="utf-8")
            content = good if broken == gov else node_text("260802-pubs-0002")
            bad = "\n".join(ln for ln in content.splitlines()
                            if not ln.startswith("summary:")) + "\n"
            broken.write_text(bad, encoding="utf-8")
            with tempfile.TemporaryDirectory() as td:
                pub, man = _pub_fixture(td)
                man.write_text(
                    "MAP _governance/publish-validation-race.md -> _governance/race.md\n"
                    "KEEP LICENSE\n", encoding="utf-8")
                before = subprocess.run(
                    ["git", "-C", str(pub), "rev-parse", "HEAD"],
                    check=True, capture_output=True).stdout
                def repaired_original(root):
                    if broken == other:
                        broken.unlink()
                    else:
                        broken.write_text(good, encoding="utf-8")
                    return original_guard(root)
                publish.guard_vault = repaired_original
                refused = None
                try:
                    publish.run(VAULT, pub, apply=True, manifest=man)
                except publish.PublishError as exc:
                    refused = str(exc)
                after = subprocess.run(
                    ["git", "-C", str(pub), "rev-parse", "HEAD"],
                    check=True, capture_output=True).stdout
                check(f"{label}: snapshot의 계약 위반으로 발행을 거부한다",
                      refused is not None and "노드 계약" in refused, refused)
                check(f"{label}: 거부 전 공개 파일이나 커밋을 바꾸지 않는다",
                      before == after and not (pub / "_governance/race.md").exists())
    finally:
        publish.guard_vault = original_guard
        gov.unlink(missing_ok=True)
        other.unlink(missing_ok=True)


def test_publish_external_engine_preserves_manifest():
    """데이터 vault의 일부 engine 파일을 보존하며 실행 엔진만 보충한다."""
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data-vault"
        validate.make_mini_vault(VAULT, data)
        source = data / "docs" / "safe.txt"
        source.parent.mkdir()
        source.write_text("publish-external-engine-marker\n", encoding="utf-8")
        manifest = data / "_governance" / "_engine" / "scripts" / "publish-manifest.txt"
        manifest.parent.mkdir(parents=True)
        text = ("MAP docs/safe.txt -> docs/safe.txt\n"
                "MAP _governance/_engine/scripts/publish-manifest.txt -> publish-manifest.txt\n"
                "KEEP LICENSE\n")
        manifest.write_text(text, encoding="utf-8")
        pub, _unused = _pub_fixture(td)
        result = publish.run(_vault_at(data), pub, apply=True, manifest=manifest)
        check("외부 엔진·manifest-only 데이터 vault도 발행된다",
              result.get("committed") and result["files"] == 2, result)
        check("실행 엔진 보충은 기존 snapshot manifest를 덮지 않는다",
              (pub / "publish-manifest.txt").read_text(encoding="utf-8") == text)
        check("보충 엔진은 발행 목록과 원본 데이터 vault에 추가되지 않는다",
              not (pub / "_governance/_engine/osk").exists()
              and not (data / "_governance/_engine/osk").exists())


def test_publish_preserves_mapped_dot_directories():
    """매니페스트의 점 디렉터리·그 안의 단일 파일도 같은 사본으로 발행한다."""
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data-vault"
        validate.make_mini_vault(VAULT, data)
        source = data / ".github/workflows/check.yml"
        source.parent.mkdir(parents=True)
        checked = b"name: reviewed-workflow\n"
        source.write_bytes(checked)
        (data / ".cache").mkdir()
        (data / ".cache/unmapped.txt").write_text("unmapped", encoding="utf-8")
        actual_guard = publish.guard_vault
        skipped = []
        def snapshot_guard(root):
            skipped.append(not (root / ".cache").exists())
            return actual_guard(root)
        cases = ((".github/", ".github/"),
                 (".github/workflows/check.yml", ".github/workflows/check.yml"),
                 ("./.github/", ".github/"))
        with mock.patch.object(publish, "guard_vault", side_effect=snapshot_guard):
            for i, (src, dst) in enumerate(cases):
                case = Path(td) / str(i)
                case.mkdir()
                pub, man = _pub_fixture(case)
                man.write_text(f"MAP {src} -> {dst}\nKEEP LICENSE\n", encoding="utf-8")
                report = publish.run(_vault_at(data), pub, manifest=man)
                check(f"{src}: 명시된 점 경로의 보고가 가능하다",
                      report["add"] == [".github/workflows/check.yml"]
                      and not (pub / ".github").exists(), report)
                applied = publish.run(_vault_at(data), pub, apply=True, manifest=man)
                committed = subprocess.run(
                    ["git", "-C", str(pub), "show", "HEAD:.github/workflows/check.yml"],
                    check=True, capture_output=True).stdout
                check(f"{src}: 검사한 점 경로의 내용이 발행된다",
                      applied.get("committed") and committed == checked, applied)
            check("MAP 밖 점 디렉터리는 계속 스냅샷에서 제외된다", all(skipped))
            before = subprocess.run(["git", "-C", str(pub), "rev-parse", "HEAD"],
                                    check=True, capture_output=True).stdout
            source.write_text('token = "ghp_' + "A" * 36 + '"\n', encoding="utf-8")
            error = None
            try:
                publish.run(_vault_at(data), pub, apply=True, manifest=man)
            except publish.PublishError as exc:
                error = str(exc)
            after = subprocess.run(["git", "-C", str(pub), "rev-parse", "HEAD"],
                                   check=True, capture_output=True).stdout
            check("MAP된 점 디렉터리도 비밀값 가드를 우회하지 못한다",
                  error is not None and "비밀값" in error and before == after)
