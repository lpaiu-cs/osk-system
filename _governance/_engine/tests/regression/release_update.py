"""회귀 수트의 한 갈래: 릴리스와 갱신 — 한 시나리오로 이어지는 긴 시험 하나.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import errno, json, os, shutil, stat, subprocess, tempfile
from pathlib import Path

from regression.support import check, ENGINE, MINI, node_text, POSIX_MODE, rmtree_force, ROOT, skip, VAULT, _vault_at
from osk import cli, core, validate  # noqa: E402


def test_release_and_update():
    from osk import _portalock, release, update, update_plan, update_source, update_state, update_txn
    def confirmed_update(*args, **kwargs):
        # The fixture supplies the user response. Checkpoint semantics are tested
        # independently in test_update_review.py, including stale/refused retries.
        report = update.run(VAULT, *args, **kwargs)
        if report.get("approval_required"):
            report = update.run(VAULT, *args, **kwargs)
        return report

    installed_engine = ROOT / "_governance/_engine"
    had_engine = installed_engine.exists()

    def git(root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)
        if args[:1] == ("init",):
            # 픽스처 저장소의 줄바꿈을 **기기 설정에서 떼어 낸다.**
            #
            # 증빙 해시는 git object에서 뜨고(`release.tree_hashes`) 갱신의 대조는
            # 작업 트리 바이트로 한다. 전역·시스템 `core.autocrlf`가 켜진
            # 기기(Git for Windows 설치 기본값이 `true`다)에서는 그 둘이 갈려
            # **전 텍스트 파일이 "해시 불일치"**가 되고, 이 시나리오는 첫 단계에서
            # 예외로 죽는다. 러너는 예외를 FAIL 한 줄로 접으므로 그 뒤의 검사
            # 101개가 **한 번도 실행되지 않았다** — 트랜잭션·롤백·복구·사이드카·
            # 드리프트·바닥 불변·데몬 tick 거부 전부가 그 안에 있다.
            for _k, _v in (("core.autocrlf", "false"), ("core.eol", "lf")):
                subprocess.run(["git", "-C", str(root), "config", _k, _v],
                               check=True, capture_output=True)

    mine = []
    try:
        with tempfile.TemporaryDirectory() as td:
            can = Path(td) / "canonical"
            (can / "_governance/_engine/scripts").mkdir(parents=True)
            # 릴리스는 **스냅샷 안의 엔진**으로 검증하므로 정본 픽스처도 엔진을
            # 담는다(실제 정본은 프레임워크 자신이므로 항상 담고 있다).
            shutil.copytree(ENGINE / "osk", can / "_governance/_engine/osk",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (can / "_governance/records").mkdir()
            (can / "docs").mkdir()
            (can / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조."),
                encoding="utf-8")
            (can / "_governance/records/갱신 사료.md").write_text(
                node_text("260802-uupd-0003", "갱신 사료"), encoding="utf-8")
            (can / "_governance/_engine/eng_upd.py").write_text("X = 1\n",
                                                                encoding="utf-8")
            (can / "_governance/_engine/scripts/publish-manifest.txt").write_text(
                "MAP  _governance/ -> _governance/\nMAP  docs/ -> docs/\n"
                "KEEP LICENSE\nKEEP README.md\nDENY _ledger/\nDENY __pycache__/\n"
                "SKEL 00_Scope/\nSKEL = UpdSkel/\n", encoding="utf-8")
            (can / "docs/UPD-SETUP.md").write_text("# 설치\n", encoding="utf-8")
            (can / "README.md").write_text("readme\n", encoding="utf-8")
            (can / "LICENSE").write_text("MIT\n", encoding="utf-8")
            git(can, "init", "-q", "-b", "main")
            git(can, "config", "user.email", "t@t")
            git(can, "config", "user.name", "t")
            git(can, "add", "-A")
            git(can, "commit", "-qm", "base")

            def _rel(ver, root=None):
                """선언 + 안내대로 작업 트리 맞추기. release는 작업 트리를
                건드리지 않으므로(외부 수정 보호), 픽스처가 사용자를 대신해
                `git checkout <ver> -- release.json`을 수행한다."""
                root = root if root is not None else can
                rep_ = release.run(VAULT, ver, apply=True, root=root)
                subprocess.run(["git", "-C", str(root), "checkout", ver, "--",
                                "release.json"], capture_output=True)
                return rep_

            rep = _rel("v9.0.0")
            check("릴리스 선언: 증빙 생성·커밋·태그",
                  rep["applied"] and rep.get("tagged") == "v9.0.0", rep)
            att = json.loads((can / "release.json").read_text(encoding="utf-8"))
            check("증빙은 자신을 담지 않는다", "release.json" not in att["files"])
            check("증빙이 전 파일을 덮는다",
                  "_governance/UpdDoc.md" in att["files"]
                  and "README.md" in att["files"], sorted(att["files"])[:5])

            def uerr(f):
                try:
                    f()
                    return None
                except (release.ReleaseError, update_state.UpdateError) as e:
                    return str(e)
            check("중복 버전은 선언 전에 거부(버전 불변)",
                  "이미 선언된 버전" in (uerr(lambda: release.run(VAULT,
                      "v9.0.0", apply=True, root=can)) or ""))

            # 보고 모드 — 아무것도 쓰지 않는다. KEEP은 정본 저장소 전용.
            r0 = confirmed_update(source="bundle", bundle=str(can))
            check("갱신 보고: applied=False", r0["ok"] and not r0["applied"], r0)
            check("KEEP은 적용 대상이 아니다",
                  "_governance/UpdDoc.md" in r0["add"]
                  and all("README" not in x and "LICENSE" not in x
                          for x in r0["add"]), r0["add"])
            check("보고는 쓰지 않는다", not (ROOT / "docs/UPD-SETUP.md").exists())

            # 최초 편입 — 빈 저널에서 adopt는 사전 존재(다른 내용) 파일도 정본으로
            # 기준선 삼는다. adopt는 이 최초 편입에만 허용된다(P2).
            (ROOT / "_governance").mkdir(parents=True, exist_ok=True)
            (ROOT / "_governance/UpdDoc.md").write_text(
                "기존 인스턴스의 다른 내용\n", encoding="utf-8")
            check("최초 편입 전 저널은 비어 있다", not update_state.has_history(VAULT))
            # **덮기가 시작조차 되지 않는다** — 내용이 다른 사이드카가 이미 있으면
            #   adopt는 중단한다(#41 · PR 리뷰). 진행하면 지금 파일의 로컬 수정이
            #   어디에도 남지 않는다: 사이드카는 못 쓰고 원본은 덮이며 pre-image는
            #   성공 뒤 `update_txn.txn_clear`가 지운다. conflict의 `held`는 원본이 제자리에
            #   남아 안전하지만 adopt는 그렇지 않다.
            #   되돌리면 — `if ad_blocked: raise`를 지우면 아래 셋이 실패한다.
            _pre = (ROOT / "_governance/UpdDoc.md").read_bytes()
            _blk = ROOT / "_governance/UpdDoc.md.local-v9.0.0"
            _blk.write_bytes("남이 먼저 차지한 사이드카".encode("utf-8"))
            mine.append(_blk)
            try:
                confirmed_update(source="bundle", bundle=str(can), apply=True,
                           adopt=True)
                _blocked_msg = None
            except update_state.UpdateError as e:
                _blocked_msg = str(e)
            check("사이드카가 이미 있고 다르면 adopt가 중단한다 (#41)",
                  _blocked_msg is not None
                  and "UpdDoc.md.local-v9.0.0" in _blocked_msg, _blocked_msg)
            check("중단한 adopt는 원본을 덮지 않았다 (#41)",
                  (ROOT / "_governance/UpdDoc.md").read_bytes() == _pre,
                  (ROOT / "_governance/UpdDoc.md").read_bytes()[:30])
            check("중단한 adopt는 남의 사이드카도 건드리지 않았다 (#41)",
                  _blk.read_bytes() == "남이 먼저 차지한 사이드카".encode("utf-8"),
                  _blk.read_bytes()[:30])
            _blk.unlink()                  # 치우고 정상 편입을 이어 간다
            # 적용 — 파일·골격·저널
            r1 = confirmed_update(source="bundle", bundle=str(can), apply=True,
                            adopt=True)
            check("adopt 최초 편입: 사전 존재 파일을 정본으로 덮는다",
                  "정본 규범 문서" in
                  (ROOT / "_governance/UpdDoc.md").read_text(encoding="utf-8"))
            # **덮은 것을 옆에 남기고 그렇다고 말한다** (#41 4항).
            #   편입의 뜻이 "현재 릴리스를 기준선 삼는다"이므로 덮는 것은 맞다.
            #   맞지 않았던 것은 그것이 보고에서 평범한 `update`와 구별되지 않고,
            #   성공하면 `update_txn.txn_clear`가 pre-image까지 지워 되돌릴 길이 없다는 것이다.
            #   되돌리면 — `plan`의 `adopted` 집계나 `update_plan.adopt_sidecar_plan`을 지우면
            #   아래 셋 중 하나가 실패한다.
            _side = ROOT / f"_governance/UpdDoc.md.local-{r1['version']}"
            mine.append(_side)
            check("adopt가 덮은 로컬 내용을 사이드카로 남긴다 (#41)",
                  _side.is_file()
                  and _side.read_text(encoding="utf-8") == "기존 인스턴스의 다른 내용\n",
                  _side.read_bytes()[:40] if _side.exists() else "부재")
            check("adopt가 덮은 자리를 보고에서 구별한다 (#41)",
                  "_governance/UpdDoc.md" in r1.get("adopted", [])
                  and "_governance/UpdDoc.md" in r1["update"], r1.get("adopted"))
            check("덮지 않은 갱신은 adopted에 들지 않는다 (#41)",
                  all(x not in r1.get("adopted", [])
                      for x in ("docs/UPD-SETUP.md",
                                "_governance/records/갱신 사료.md")),
                  r1.get("adopted"))

            # **내용이 다른 사이드카가 이미 있으면 덮기를 시작하지 않는다** (#41).
            #   conflict의 `held`가 안전한 것은 원본이 제자리에 남기 때문이다.
            #   adopt는 원본을 덮으므로 같은 규율을 쓰면, 사이드카를 못 쓴 채
            #   원본을 덮어 로컬 수정이 어디에도 남지 않는다(pre-image는 성공 뒤
            #   `update_txn.txn_clear`가 지운다). 되돌리면 — `blocked`를 `held`처럼 흘려
            #   보내면 아래 둘이 실패한다.
            _b = ROOT / "_governance/UpdDoc2.md"
            _bside = ROOT / "_governance/UpdDoc2.md.local-v9.9.9"
            mine += [_b, _bside]
            _b.write_bytes("잃으면 안 되는 로컬 수정".encode("utf-8"))
            _bside.write_bytes("남이 먼저 차지한 사이드카".encode("utf-8"))
            _fake = {"adopted": [("x", "_governance/UpdDoc2.md")]}
            _w, _k, _blocked, _c = update_plan.adopt_sidecar_plan(VAULT,
                _fake, "v9.9.9", set())
            check("내용이 다른 adopt 사이드카는 blocked로 갈린다 (#41)",
                  _blocked == ["_governance/UpdDoc2.md.local-v9.9.9"]
                  and not _w and not _k, (_w, _k, _blocked, _c))
            check("그때 원본과 사이드카를 둘 다 손대지 않는다 (#41)",
                  _b.read_bytes() == "잃으면 안 되는 로컬 수정".encode("utf-8")
                  and _bside.read_bytes()
                  == "남이 먼저 차지한 사이드카".encode("utf-8"),
                  (_b.read_bytes()[:20], _bside.read_bytes()[:20]))
            check("편입 후 관리 이력이 생긴다", update_state.has_history(VAULT))
            mine += [ROOT / "_governance/UpdDoc.md",
                     ROOT / "_governance/records/갱신 사료.md",
                     ROOT / "_governance/_engine/eng_upd.py",
                     ROOT / "docs/UPD-SETUP.md",
                     ROOT / "_governance/_engine/scripts/publish-manifest.txt"]
            check("적용: 파일이 들어온다",
                  (ROOT / "_governance/UpdDoc.md").exists()
                  and (ROOT / "docs/UPD-SETUP.md").exists(), r1)
            # 허용 밖 골격(`= UpdSkel`)은 만들어지지 않고, 허용 루트는 이미 있으면
            # 그대로 둔다 — 골격은 **최상위 Space 루트 셋**에만 허용된다 (P1)
            check("허용 밖 SKEL은 만들어지지 않는다",
                  not (ROOT / "= UpdSkel").exists())
            check("_allowed_skel: 최상위 Space 루트만 허용",
                  update_plan._allowed_skel(VAULT, "00_Scope") is not None
                  and update_plan._allowed_skel(VAULT, "00_Domain") is not None
                  and update_plan._allowed_skel(VAULT, "00_Scope/UserData") is None
                  and update_plan._allowed_skel(VAULT, "00_Scope/UserData/newdir") is None
                  and update_plan._allowed_skel(VAULT, "= NotASpace") is None)
            recs = core.ledger_read(update_state.journal_path(VAULT))
            check("저널: begin·apply·done",
                  {"begin", "apply", "done"} <= {r.get("kind") for r in recs})
            check("현재 버전 판정", update_state.current_version(VAULT) == "v9.0.0")

            # 멱등 — 재실행은 전부 same
            r2 = confirmed_update(source="bundle", bundle=str(can))
            check("재실행은 전부 same",
                  not r2["add"] and not r2["update"] and not r2["conflict"], r2)

            # 문서 드리프트 → 덮지 않고 사이드카
            (ROOT / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "로컬 개정."),
                encoding="utf-8")
            r3 = confirmed_update(source="bundle", bundle=str(can))
            check("로컬 수정 문서는 conflict",
                  "_governance/UpdDoc.md" in r3["conflict"], r3)
            r4 = confirmed_update(source="bundle", bundle=str(can), apply=True)
            side = ROOT / "_governance/UpdDoc.md.upstream-v9.0.0"
            mine.append(side)
            check("사이드카가 생기고 원본은 보존",
                  side.exists() and "로컬 개정" in
                  (ROOT / "_governance/UpdDoc.md").read_text(encoding="utf-8"), r4)

            # 엔진 드리프트 → 갱신 전체 중단. 이미 관리 중이면 adopt는 force로
            # 쓰이지 않는다(최초 편입 전용) — 거부된다(P2).
            (ROOT / "_governance/_engine/eng_upd.py").write_text("X = 2\n",
                                                                 encoding="utf-8")
            e = uerr(lambda: confirmed_update(source="bundle", bundle=str(can),
                                        apply=True))
            check("엔진 로컬 수정은 갱신 전체 중단", e is not None and "엔진" in e, e)
            e2 = uerr(lambda: confirmed_update(source="bundle", bundle=str(can),
                                         apply=True, adopt=True))
            check("관리 중 인스턴스에서 adopt는 거부(최초 편입 전용)",
                  e2 is not None and "최초 편입" in e2, e2)
            # 사용자가 로컬 수정을 정리(정본 내용으로 복원) → 이후 갱신 정상
            (ROOT / "_governance/_engine/eng_upd.py").write_text("X = 1\n",
                                                                 encoding="utf-8")

            # 사용자가 사이드카 충돌을 수용(로컬 개정 폐기, upstream v9.0.0 복원)
            # → 이 문서의 baseline과 일치해 다음 갱신이 깨끗이 덮을 수 있다.
            (ROOT / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조."),
                encoding="utf-8")

            # 갱신이 통치 문서를 덮으면 그 차이가 통치 구획 보호영역의 변경집합
            # 으로 남는다 — 사용자의 승인이 수용 기록이다 (Mechanism §1-2 6항 ·
            # 시행령 §10 2항). 여기서는 updater가 통치 문서를 실제로 덮는지만 본다
            # (수용=승인의 생애는 보호영역 fixture가 소진한다).
            gp = ROOT / "_governance/UpdDoc.md"
            (can / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조 개정."),
                encoding="utf-8")
            git(can, "add", "-A")
            git(can, "commit", "-qm", "gov v2")
            _rel("v9.0.1")
            confirmed_update(source="bundle", bundle=str(can), apply=True)
            check("갱신이 통치 문서를 정본 내용으로 덮는다",
                  "1조 개정" in gp.read_text(encoding="utf-8"))
            check("갱신 후 현재 버전 갱신", update_state.current_version(VAULT) == "v9.0.1")

            # 비준증빙 위반 — 변조·부재는 중단. 증빙 밖 미추적 파일은 허용.
            (can / "docs/UPD-SETUP.md").write_text("# 변조\n", encoding="utf-8")
            e = uerr(lambda: confirmed_update(source="bundle", bundle=str(can)))
            check("해시 불일치는 중단", e is not None and "해시 불일치" in e, e)
            (can / "docs/UPD-SETUP.md").write_text("# 설치\n", encoding="utf-8")
            # 증빙 밖(미추적) 파일은 적용되지 않으므로 갱신을 막지 않는다 —
            # 적용은 오직 증빙이 모는 파일만 하고 그 하나하나가 해시 검증된다
            # (pyc·.DS_Store 부산물이 딸린 디렉터리 bundle을 못 쓰게 하지 않는다)
            (can / "sneaky.md").write_text("x\n", encoding="utf-8")
            r7 = confirmed_update(source="bundle", bundle=str(can))
            check("증빙 밖 파일은 갱신을 막지 않고 적용되지도 않는다",
                  r7["ok"] and not (ROOT / "sneaky.md").exists(), r7)
            (can / "sneaky.md").unlink()
            e = uerr(lambda: confirmed_update(source="bundle",
                                        bundle=str(Path(td) / "noatt-없는트리")))
            check("비준증빙 없는 출처는 거부", e is not None, e)

            # 갱신 기본은 브랜치 HEAD가 아니라 최신 정식 릴리스 태그다 (P2)
            check("갱신 기본은 최신 릴리스 태그",
                  update_source.latest_release_tag(str(can)) == "v9.0.1",
                  update_source.latest_release_tag(str(can)))

            # 삭제 전파 — 정본에서 빠진 파일은 인스턴스에서도 제거(로컬 무수정) (P1)
            (can / "_governance/DropMe.md").write_text(
                node_text("260802-uupd-0009", "곧 삭제될 규범"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add DropMe")
            _rel("v9.0.2")
            confirmed_update(source="bundle", bundle=str(can), apply=True)
            drop = ROOT / "_governance/DropMe.md"; mine.append(drop)
            check("삭제 전 파일이 관리된다", drop.exists())
            git(can, "rm", "-q", "_governance/DropMe.md")
            git(can, "commit", "-qm", "drop DropMe")
            _rel("v9.0.3")
            rdel = confirmed_update(source="bundle", bundle=str(can), apply=True)
            check("정본에서 빠진 파일은 인스턴스에서도 삭제된다",
                  not drop.exists()
                  and "_governance/DropMe.md" in rdel.get("removed", []), rdel)

            # 삭제 대상이라도 로컬 수정이 있으면 보존·보고 (P1)
            (can / "_governance/KeepMe.md").write_text(
                node_text("260802-uupd-000a", "삭제되나 보존"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add KeepMe")
            _rel("v9.0.4")
            confirmed_update(source="bundle", bundle=str(can), apply=True)
            keep = ROOT / "_governance/KeepMe.md"; mine.append(keep)
            keep.write_text(node_text("260802-uupd-000a", "로컬에서 고쳤다"),
                            encoding="utf-8")
            git(can, "rm", "-q", "_governance/KeepMe.md")
            git(can, "commit", "-qm", "drop KeepMe")
            _rel("v9.0.5")
            rkeep = confirmed_update(source="bundle", bundle=str(can), apply=True)
            check("로컬 수정된 삭제 대상은 보존된다",
                  keep.exists()
                  and "_governance/KeepMe.md" in rkeep.get("remove_conflict", []),
                  rkeep)

            # 사전존재·동일내용 파일도 기준선을 남긴다 — 다음 릴리스 drift 오판 금지 (P1)
            probe = "_governance/_engine/adopt_probe.py"
            (can / probe).write_text("P = 1\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add probe")
            _rel("v9.0.6")
            (ROOT / probe).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / probe).write_text("P = 1\n", encoding="utf-8")  # 저널 없이 존재
            mine.append(ROOT / probe)
            rp = confirmed_update(source="bundle", bundle=str(can))
            check("사전존재·동일내용은 rebaseline", probe in rp["rebaseline"], rp)
            confirmed_update(source="bundle", bundle=str(can), apply=True)   # 기준선 기록
            (can / probe).write_text("P = 2\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "probe v2")
            _rel("v9.0.7")
            # 기준선이 없었다면 base=None→engine_drift로 갱신 전체가 중단됐을 것
            rp2 = confirmed_update(source="bundle", bundle=str(can), apply=True)
            check("기준선 덕에 엔진 drift 오판 없이 깨끗이 갱신",
                  (ROOT / probe).read_text(encoding="utf-8") == "P = 2\n", rp2)

            # 동명 브랜치가 태그를 가리지 못한다 — refs/tags 명시 fetch (P1)
            # 태그 v9.0.0과 동명인 브랜치를 HEAD(다른·최신 커밋)에 만들어 둔다
            git(can, "branch", "v9.0.0", "HEAD")
            tf = update_source.fetch_git(str(can), "v9.0.0", Path(td) / "ftag")
            relf = json.loads((tf / "release.json").read_text(encoding="utf-8"))
            check("동명 브랜치가 있어도 태그의 커밋을 받는다",
                  relf["version"] == "v9.0.0", relf.get("version"))
            git(can, "branch", "-q", "-D", "v9.0.0")

            # 손상된 update.jsonl은 검증기가 FAIL로 잡는다 — 단순 로그가 아니다 (P2)
            uj = VAULT.ledger / "update.jsonl"
            orig_uj = uj.read_text(encoding="utf-8") if uj.exists() else ""
            with open(uj, "a", encoding="utf-8") as _f:
                _f.write('{"rid":"bad-rid","kind":"apply","path":"X",'
                         '"hash":"h"}\n')
            repv = validate.run(VAULT, cli.published_tools)
            check("손상된 update.jsonl은 검증기 FAIL",
                  repv["verdict"] == "FAIL"
                  and any("update.jsonl" in str(v) for v in repv["fail"]),
                  repv["fail"])
            uj.write_text(orig_uj, encoding="utf-8")

            # 선언 도중 외부 커밋이 들어오면 **CAS**가 막고 아무것도 남지 않는다 (P1)
            def _head(r):
                return subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
            _real_bt = release.build_attestation

            def _outsider_commits(root_, ver_, ref_="HEAD"):
                att_ = _real_bt(root_, ver_, ref_)
                # 증빙을 뜬 뒤·설치 전에 다른 프로세스가 커밋한다
                (can / "outsider.md").write_text("외부 작업\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(can), "add", "-A"],
                               capture_output=True)
                subprocess.run(["git", "-C", str(can), "commit", "-qm",
                                "outsider"], capture_output=True)
                return att_
            release.build_attestation = _outsider_commits
            try:
                e = uerr(lambda: release.run(VAULT, "v9.3.0", apply=True, root=can))
            finally:
                release.build_attestation = _real_bt
            outsider_head = _head(can)
            check("외부 커밋이 끼어들면 CAS가 선언을 막는다",
                  e is not None and "CAS" in e, e)
            check("외부 커밋은 파괴되지 않는다(HEAD 유지)",
                  (can / "outsider.md").exists()
                  and subprocess.run(["git", "-C", str(can), "log", "-1",
                                      "--format=%s"], capture_output=True,
                                     text=True).stdout.strip() == "outsider",
                  outsider_head)
            check("실패 선언의 태그는 남지 않는다",
                  not subprocess.run(["git", "-C", str(can), "tag", "-l",
                                      "v9.3.0"], capture_output=True,
                                     text=True).stdout.strip())

            # 변조 pre-commit hook은 --no-verify로 무력화 — self-invalid 릴리스 방지 (P2)
            probe2 = can / "_governance" / "hooktouch.md"
            probe2.write_text(node_text("260802-uupd-000c", "훅 미변조 확인"),
                              encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add hooktouch")
            hook = can / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            # 훅이 tracked 파일을 수정·stage하려 시도한다(우회되면 원본 유지)
            hook.write_text(
                "#!/bin/sh\n"
                'printf "TAMPERED\\n" >> "_governance/hooktouch.md"\n'
                'git add "_governance/hooktouch.md"\n', encoding="utf-8")
            hook.chmod(0o755)
            before = probe2.read_text(encoding="utf-8")
            _rel("v9.4.0")
            check("release 커밋은 변조 hook을 태우지 않는다(--no-verify)",
                  probe2.read_text(encoding="utf-8") == before
                  and "TAMPERED" not in probe2.read_text(encoding="utf-8"))
            hook.unlink()

            # clean-tree 면제는 정확히 release.json만 — release.json.bak은 아니다 (P2)
            (can / "release.json.bak").write_text("x\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add bak")
            _rel("v9.1.0")
            (can / "release.json.bak").write_text("변조\n", encoding="utf-8")
            check("release.json.bak 수정은 clean-tree에서 면제되지 않는다",
                  "깨끗하지 않다" in (uerr(lambda: release.run(VAULT,
                      "v9.1.1", apply=True, root=can)) or ""))
            git(can, "checkout", "-q", "--", "release.json.bak")

            # release 보고가 직전 릴리스 대비 삭제(removed)를 포함한다 (P3)
            (can / "_governance/RmRep.md").write_text(
                node_text("260802-uupd-000b", "삭제 보고 대상"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add RmRep")
            _rel("v9.2.0")
            git(can, "rm", "-q", "_governance/RmRep.md")
            git(can, "commit", "-qm", "rm RmRep")
            rrep = release.run(VAULT, "v9.2.1", apply=False, root=can)
            check("release 보고가 removed에 삭제 파일을 담는다",
                  "_governance/RmRep.md" in (rrep.get("removed") or []), rrep)

            # 인스턴스 소유 바닥·SKEL 봉쇄 — 악의 매니페스트도 못 쓴다(엔진 상수) (P1)
            # apply_set 단계에서 봉쇄되는지를 본다(쓰기 없음 — 대량삭제 부작용 회피)
            ev = Path(td) / "evil"
            (ev / "_governance/_engine/scripts").mkdir(parents=True)
            (ev / "00_Scope").mkdir()
            (ev / "00_Scope/침투.md").write_text("x\n", encoding="utf-8")
            (ev / "_governance/x/_ledger").mkdir(parents=True)
            (ev / "_governance/x/_ledger/x.jsonl").write_text("{}\n",
                                                              encoding="utf-8")
            (ev / "_governance/_engine/scripts/publish-manifest.txt").write_text(
                'MAP  00_Scope/ -> 00_Scope/\nMAP  _governance/ -> _governance/\n'
                'SKEL 00_Scope/Workbench/_ledger\nSKEL ../escape\n',
                encoding="utf-8")
            att2 = {"version": "v9.6.6", "at": core.now_iso(),
                    "files": {core.posix_rel(f, ev): core.sha256_file(f)
                              for f in ev.rglob("*") if f.is_file()}}
            (ev / "release.json").write_text(
                json.dumps(att2, ensure_ascii=False), encoding="utf-8")
            ev_rel = update_source.load_release(ev)
            ev_targets, ev_skel, ev_skipped = update_plan.apply_set(VAULT, ev, ev_rel)
            ev_dests = [d for _s, d in ev_targets]      # apply_set는 (src,dest) 사상
            check("Space 바닥은 target이 아니라 skip(침투 차단)",
                  "00_Scope/침투.md" not in ev_dests
                  and any("침투" in s for s in ev_skipped), (ev_dests, ev_skipped))
            check("_ledger 조각도 target이 아니다",
                  "_governance/x/_ledger/x.jsonl" not in ev_dests, ev_dests)
            check("SKEL의 바닥 파고들기·루트 탈출은 허용 밖",
                  ev_skel == []
                  and sum("SKEL 허용 밖" in s for s in ev_skipped) == 2,
                  (ev_skel, ev_skipped))
            check("_allowed_skel: 빈 Space 루트만 허용",
                  update_plan._allowed_skel(VAULT, "00_Scope") is not None
                  and update_plan._allowed_skel(VAULT, "00_Scope/Workbench/_ledger") is None
                  and update_plan._allowed_skel(VAULT, "../evil") is None
                  and update_plan._allowed_skel(VAULT, "_ledger") is None)

            # 경로 봉쇄 — 증빙 key·저널 path의 vault 탈출 차단 (P1)
            check("resolve_within: 정상 상대경로는 통과",
                  core.resolve_within(ROOT, "_governance/Constitution.md") is not None)
            check("resolve_within: 상위 탈출·절대경로는 봉쇄",
                  core.resolve_within(ROOT, "_governance/../../payload") is None
                  and core.resolve_within(ROOT, "../escape") is None
                  and core.resolve_within(ROOT, "/etc/passwd") is None)
            esc = {"version": "v1.0.0",
                   "files": {"_governance/../../payload": "sha256:00"}}
            check("증빙 경로 탈출은 대조 단계에서 중단",
                  any("트리 밖" in e for e in update_source.verify_attestation(can, esc)),
                  update_source.verify_attestation(can, esc))

            # ROOT 내부 symlink alias는 경로 정체성 훼손으로 거부 — 다른 파일로
            # write 재지정 차단 (P1). docs가 _governance로 가는 symlink라 하자.
            syroot = Path(td) / "syroot"
            (syroot / "_governance").mkdir(parents=True)
            (syroot / "_governance" / "target.md").write_text("t\n",
                                                              encoding="utf-8")
            try:
                (syroot / "docs").symlink_to(syroot / "_governance")
                sy_ok = True
            except OSError:
                sy_ok = True                     # symlink 불가 환경이면 통과 처리
            if (syroot / "docs").is_symlink():
                check("ROOT 내부 symlink alias(docs->_governance)는 거부",
                      core.canon_rel(syroot, "docs/target.md") is None
                      and core.canon_rel(syroot, "_governance/target.md")
                      == "_governance/target.md")

            # 적용 트랜잭션 — 도중 실패 시 파일 원상복구, 저널에 apply 안 남김 (P1)
            trbase = Path(td) / "txn"
            (trbase / "_governance/_engine/scripts").mkdir(parents=True)
            (trbase / "_governance/A.md").write_text(
                node_text("260802-uupd-000d", "A"), encoding="utf-8")
            (trbase / "_governance/B.md").write_text(
                node_text("260802-uupd-000e", "B"), encoding="utf-8")
            (trbase / "_governance/_engine/scripts/publish-manifest.txt"
             ).write_text("MAP  _governance/ -> _governance/\nDENY _ledger/\n"
                          "DENY __pycache__/\n", encoding="utf-8")
            att_t = {"version": "v1.0.0", "at": core.now_iso(),
                     "files": {core.posix_rel(f, trbase): core.sha256_file(f)
                               for f in trbase.rglob("*") if f.is_file()}}
            (trbase / "release.json").write_text(
                json.dumps(att_t, ensure_ascii=False), encoding="utf-8")
            # _write_atomic이 B.md write에서 OSError를 던지게 해 트랜잭션 실패 유발
            # (A는 써진 뒤 B에서 실패 → 원상복구로 A·B 모두 사라져야 한다)
            real_wa = update_txn.write_atomic

            def _boom(dst, data):
                if dst.name == "B.md":
                    raise OSError("boom")
                return real_wa(dst, data)
            uj0 = VAULT.ledger / "update.jsonl"
            j_before = uj0.read_text(encoding="utf-8") if uj0.exists() else ""
            update_txn.write_atomic = _boom
            try:
                et = uerr(lambda: confirmed_update(source="bundle", bundle=str(trbase),
                                             apply=True))
            finally:
                update_txn.write_atomic = real_wa
            j_after = uj0.read_text(encoding="utf-8") if uj0.exists() else ""
            check("적용 중 실패는 UpdateError로 중단",
                  et is not None and "원상복구" in et, et)
            check("실패한 트랜잭션은 파일을 남기지 않는다(A·B 부재)",
                  not (ROOT / "_governance/A.md").exists()
                  and not (ROOT / "_governance/B.md").exists())
            new_j = j_after[len(j_before):]
            check("실패 트랜잭션은 apply 저널을 안 남긴다(begin·rollback만)",
                  '"kind": "apply"' not in new_j and '"kind": "rollback"' in new_j,
                  new_j[-300:])

            # 크래시-안전 트랜잭션: 커밋 여부로 rollback/roll-forward를 결정한다 (P1)
            victim_rel = "_governance/UpdDoc.md"
            victim = ROOT / victim_rel
            orig_v = victim.read_bytes()

            def _stage_txn(txn):
                """_txn_begin으로 실제 프로토콜대로 트랜잭션을 시작해 둔다."""
                update_txn.txn_begin(VAULT, txn, "v9.9.9", [victim_rel])

            # ① 미커밋(done 없음) → pre-image로 rollback
            _stage_txn("txnAAA")
            victim.write_bytes(b"HALF-APPLIED\n")     # 크래시로 남은 부분 적용
            act = update_txn.txn_recover(VAULT, [{"kind": "begin", "txn": "txnAAA"}])
            check("미커밋 트랜잭션은 pre-image로 rollback",
                  act == "rollback" and victim.read_bytes() == orig_v
                  and not update_txn.txn_manifest(VAULT).exists(), act)
            # ② 커밋됨(done(txn) 있음) → 파일은 새 판 유지, 표식만 정리
            _stage_txn("txnBBB")
            victim.write_bytes(b"NEW-VERSION\n")
            act = update_txn.txn_recover(VAULT, [{"kind": "done", "txn": "txnBBB"}])
            check("커밋된 트랜잭션은 roll-forward(파일 유지·표식 정리)",
                  act == "roll-forward"
                  and victim.read_bytes() == b"NEW-VERSION\n"
                  and not update_txn.txn_manifest(VAULT).exists(), act)
            victim.write_bytes(orig_v)
            # ③ 백업 손상 → fail-closed(백업 보존·중단)
            _stage_txn("txnCCC")
            (update_txn.txn_backup(VAULT) / "000000").write_bytes(b"CORRUPT\n")
            ec = uerr(lambda: update_txn.txn_recover(VAULT, [{"kind": "begin",
                                                    "txn": "txnCCC"}]))
            check("백업 손상은 fail-closed로 중단하고 txn 영역을 보존",
                  ec is not None and "손상" in ec and update_txn.txn_manifest(VAULT).is_file(),
                  ec)
            update_txn.txn_clear(VAULT)
            victim.write_bytes(orig_v)

            # 커밋 전 apply 기록은 판정에서 보이지 않는다 (파일↔저널 정합, P1)
            uncommitted = [
                {"rid": "00000000-0000-7000-8000-00000000c001", "parents": [],
                 "kind": "apply", "txn": "T1", "path": "F", "hash": "sha256:aa"},
            ]
            check("미커밋 apply는 baseline이 되지 않는다",
                  update_state.last_applied_hash(uncommitted, "F") is None
                  and "F" not in update_state.managed_paths(uncommitted)
                  and not update_state.has_history(VAULT, uncommitted))
            committed_recs = uncommitted + [
                {"rid": "00000000-0000-7000-8000-00000000c002",
                 "parents": ["00000000-0000-7000-8000-00000000c001"],
                 "kind": "done", "txn": "T1"}]
            check("done(txn) 이후에는 같은 기록이 baseline이 된다",
                  update_state.last_applied_hash(committed_recs, "F") == "sha256:aa"
                  and update_state.has_history(VAULT, committed_recs))

            # 미커밋 기록은 **후보**에서만 빠지고 인과 사슬은 남아야 한다 —
            # 목록에서 빼면 parents 간선이 끊겨 정상 복구·재적용 뒤에도 baseline이
            # 사라진다(T1 커밋 → T2 크래시·롤백 → T3 커밋). (P1)
            def _r(n):
                return f"00000000-0000-7000-8000-{n:012d}"
            chain3 = [
                {"rid": _r(11), "parents": [], "kind": "apply", "txn": "T1",
                 "path": "F", "hash": "sha256:old"},
                {"rid": _r(12), "parents": [_r(11)], "kind": "done", "txn": "T1"},
                {"rid": _r(13), "parents": [_r(12)], "kind": "apply", "txn": "T2",
                 "path": "F", "hash": "sha256:mid"},     # 크래시·롤백(미커밋)
                {"rid": _r(14), "parents": [_r(13)], "kind": "apply", "txn": "T3",
                 "path": "F", "hash": "sha256:new"},
                {"rid": _r(15), "parents": [_r(14)], "kind": "done", "txn": "T3"},
            ]
            check("미커밋이 사슬을 끊지 않는다(재적용 baseline 유지)",
                  update_state.last_applied_hash(chain3, "F") == "sha256:new"
                  and update_state.managed_paths(chain3) == {"F": "sha256:new"},
                  (update_state.last_applied_hash(chain3, "F"),
                   update_state.managed_paths(chain3)))
            check("미커밋만 있으면 직전 커밋 baseline이 남는다",
                  update_state.last_applied_hash(chain3[:3], "F") == "sha256:old",
                  update_state.last_applied_hash(chain3[:3], "F"))
            rm3 = chain3 + [
                {"rid": _r(16), "parents": [_r(15)], "kind": "remove",
                 "txn": "T4", "path": "F"},
                {"rid": _r(17), "parents": [_r(16)], "kind": "done", "txn": "T4"}]
            check("커밋된 remove가 극대면 관리에서 빠진다",
                  update_state.last_applied_hash(rm3, "F") is None
                  and update_state.managed_paths(rm3) == {})

            # release 증빙은 **커밋 트리**에서 뜬다 — guard 이후 working tree가
            # 바뀌어도 증빙이 커밋과 어긋나지 않는다(TOCTOU) (P1)
            wt_victim = can / "docs/UPD-SETUP.md"
            wt_orig = wt_victim.read_bytes()
            head_h = release.tree_hashes(can)
            wt_victim.write_bytes(b"EXTERNAL EDIT\n")        # 외부 프로세스 모사
            check("tree_hashes는 working tree 오염과 무관(커밋 트리를 읽는다)",
                  release.tree_hashes(can) == head_h,
                  "docs/UPD-SETUP.md")
            wt_victim.write_bytes(wt_orig)

            # 태그 전 전수 재대조 — 증빙이 커밋 트리와 다르면 선언을 중단한다 (P1)
            _real_ba = release.build_attestation

            def _bad_ba(root_, ver_, ref_="HEAD"):
                att_ = _real_ba(root_, ver_, ref_)
                k = sorted(att_["files"])[0]
                att_["files"][k] = "sha256:" + "0" * 64      # 어긋난 증빙
                return att_
            pre_head_r = subprocess.run(
                ["git", "-C", str(can), "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            release.build_attestation = _bad_ba
            try:
                ebad = uerr(lambda: release.run(VAULT, "v9.5.0", apply=True, root=can))
            finally:
                release.build_attestation = _real_ba
            post_head_r = subprocess.run(
                ["git", "-C", str(can), "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            tags_r = subprocess.run(["git", "-C", str(can), "tag", "-l", "v9.5.0"],
                                    capture_output=True, text=True).stdout.strip()
            check("증빙≠커밋 트리면 태그 전에 중단한다",
                  ebad is not None and "커밋 트리가 증빙과 다르다" in ebad, ebad)
            check("중단 시 커밋·태그가 남지 않는다",
                  post_head_r == pre_head_r and not tags_r,
                  (post_head_r == pre_head_r, tags_r))

            # CAS 구조에서는 index·working tree 변조가 릴리스 커밋에 **영향을
            # 주지 못한다** — 커밋을 object로 직접 만들기 때문이다(공격 벡터 소멸).
            _real_run = release.subprocess.run

            def _tamper_index(cmd, *a, **k):
                if isinstance(cmd, list) and "commit-tree" in cmd:
                    (can / "release.json").write_text(
                        json.dumps({"version": "vTAMPERED", "at": "x",
                                    "files": {}}), encoding="utf-8")
                    _real_run(["git", "-C", str(can), "add", "--",
                               "release.json"], capture_output=True)
                return _real_run(cmd, *a, **k)
            release.subprocess.run = _tamper_index
            try:
                rep_t = _rel("v9.6.0")
            finally:
                release.subprocess.run = _real_run
            _tagged_att = json.loads(subprocess.run(
                ["git", "-C", str(can), "show", "v9.6.0:release.json"],
                capture_output=True, text=True).stdout)
            check("index 변조는 릴리스 커밋에 영향을 주지 못한다",
                  rep_t.get("applied") and _tagged_att["version"] == "v9.6.0",
                  _tagged_att.get("version"))
            check("태그는 검증한 그 커밋에 붙는다",
                  subprocess.run(["git", "-C", str(can), "rev-parse", "v9.6.0^{commit}"],
                                 capture_output=True, text=True).stdout.strip()
                  == rep_t.get("commit"), rep_t.get("commit"))

            # 매니페스트가 증빙에 없으면 control plane으로 쓰지 않는다 (P2)
            _noman = Path(td) / "noman"
            (_noman / "_governance/_engine/scripts").mkdir(parents=True)
            (_noman / "_governance/X.md").write_text(
                node_text("260802-uupd-000f", "증빙된 파일"), encoding="utf-8")
            (_noman / "_governance/_engine/scripts/publish-manifest.txt"
             ).write_text("MAP  _governance/ -> _governance/\n", encoding="utf-8")
            _att_nm = {"version": "v1.0.0", "at": core.now_iso(),
                       "files": {"_governance/X.md":
                                 core.sha256_file(_noman / "_governance/X.md")}}
            (_noman / "release.json").write_text(
                json.dumps(_att_nm, ensure_ascii=False), encoding="utf-8")
            enm = uerr(lambda: update_plan.apply_set(VAULT, _noman,
                                                update_source.load_release(_noman)))
            check("증빙에 없는 매니페스트는 적용 범위로 쓰지 않는다",
                  enm is not None and "비준증빙에 없다" in enm, enm)

            # 실행 비트가 붙은 파일은 릴리스에서 거부 — 증빙이 mode를 안 싣는다 (P2)
            _exe = can / "runme.sh"
            _exe.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            _exe.chmod(0o755)
            subprocess.run(["git", "-C", str(can), "add", "-A"],
                           capture_output=True)
            # mode는 **색인에** 박는다. Windows에는 실행 비트가 없고 git의
            # `core.fileMode`도 그 기본이 false라, `chmod`만으로는 100755가
            # 색인에 들어가지 않아 이 검사가 그 기기에서만 조용히 무력해진다.
            subprocess.run(["git", "-C", str(can), "update-index",
                            "--chmod=+x", "runme.sh"], capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "exe"],
                           capture_output=True)
            eexe = uerr(lambda: release.tree_hashes(can))
            check("100755(실행 비트)도 릴리스에서 거부",
                  eexe is not None and "지원하지 않는" in eexe, eexe)
            subprocess.run(["git", "-C", str(can), "rm", "-q", "-f", "runme.sh"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "drop exe"],
                           capture_output=True)

            # tree_hashes는 git object tree를 읽는다 — symlink는 지원 밖으로 거부 (P2)
            (can / "linky").symlink_to("README.md")
            subprocess.run(["git", "-C", str(can), "add", "-A"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "symlink"],
                           capture_output=True)
            esym = uerr(lambda: release.tree_hashes(can))
            check("symlink 등 비정규 mode는 릴리스에서 거부",
                  esym is not None and "지원하지 않는" in esym, esym)
            subprocess.run(["git", "-C", str(can), "rm", "-q", "--cached",
                            "linky"], capture_output=True)
            (can / "linky").unlink()
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "drop link"],
                           capture_output=True)

            # 사이드카 충돌 판정은 **삭제 예정 경로**도 본다 (P2)
            _p_rm = {"conflict": [("_governance/UpdDoc.md", "_governance/A.md")],
                     "remove": ["_governance/A.md.upstream-v9.9.9"]}
            _coll = update_plan.sidecar_plan(VAULT,
                _p_rm, can, "v9.9.9",
                set() | set(_p_rm["remove"]))[3]
            check("삭제 예정 경로와 겹치는 사이드카는 충돌로 잡는다",
                  "_governance/A.md.upstream-v9.9.9" in _coll, _coll)

            # 디렉터리 rollback 실패는 fail-closed — 허용은 ENOENT·ENOTEMPTY뿐 (P2)
            update_txn.txn_begin(VAULT, "txnDF", "v9.9.9", ["_governance/dfx/f.md"])
            _saved_rmdir = Path.rmdir

            def _boom_rmdir(self):
                raise OSError(errno.EACCES, "denied")
            try:
                Path.rmdir = _boom_rmdir
                edf = uerr(lambda: update_txn.txn_recover(VAULT,
                    [{"kind": "begin", "txn": "txnDF"}]))
                check("디렉터리 복구의 권한 오류는 fail-closed",
                      edf is not None and "디렉터리 복구 실패" in edf, edf)
            finally:
                Path.rmdir = _saved_rmdir
                update_txn.txn_clear(VAULT)

            # 새 디렉터리도 rollback 대상 — 파일만 되돌리면 SKEL 잔재가 남는다 (P2)
            deep = "_governance/_engine/newpkg/sub/mod.py"
            update_txn.txn_begin(VAULT, "txnDIR", "v9.9.9", [deep])
            man_d = json.loads(update_txn.txn_manifest(VAULT).read_text(encoding="utf-8"))
            check("manifest가 새 디렉터리를 기록한다",
                  "_governance/_engine/newpkg" in (man_d.get("dirs") or [])
                  and "_governance/_engine/newpkg/sub" in (man_d.get("dirs") or []),
                  man_d.get("dirs"))
            update_txn.write_atomic(ROOT / deep, b"X = 1\n")   # 디렉터리까지 생성
            check("적용으로 깊은 디렉터리가 생긴다",
                  (ROOT / "_governance/_engine/newpkg/sub").is_dir())
            act_d = update_txn.txn_recover(VAULT, [{"kind": "begin", "txn": "txnDIR"}])
            check("rollback이 파일과 새 디렉터리를 모두 되돌린다",
                  act_d == "rollback"
                  and not (ROOT / deep).exists()
                  and not (ROOT / "_governance/_engine/newpkg").exists(), act_d)

            # 사이드카는 사용자 작업을 덮지 않는다 (P1)
            sc_dest = "_governance/UpdDoc.md"
            sc_side = sc_dest + ".upstream-v9.9.9"
            (ROOT / sc_side).write_text("사용자 수동 병합 작업\n", encoding="utf-8")
            mine.append(ROOT / sc_side)
            _pfake = {"conflict": [("_governance/UpdDoc.md", sc_dest)]}
            w, kept, held, coll = update_plan.sidecar_plan(VAULT, _pfake, can, "v9.9.9", set())
            check("다른 내용의 기존 사이드카는 손대지 않는다(보존·보고)",
                  not w and not kept and sc_side in held and not coll,
                  (w, kept, held, coll))
            (ROOT / sc_side).write_bytes((can / "_governance/UpdDoc.md").read_bytes())
            w2, kept2, held2, _c = update_plan.sidecar_plan(VAULT, _pfake, can, "v9.9.9", set())
            check("같은 내용이면 그대로 인정(재기록 없음)",
                  not w2 and sc_side in kept2 and not held2, (w2, kept2, held2))
            _c2 = update_plan.sidecar_plan(VAULT, _pfake, can, "v9.9.9", {sc_side})[3]
            check("사이드카가 관리 파일과 겹치면 충돌로 잡는다",
                  sc_side in _c2, _c2)
            (ROOT / sc_side).unlink(missing_ok=True)

            # 저널에 부분 행이 남아도 복구에 도달한다 — 크래시가 append 도중일
            # 때 엄격 판독으로 막히면 '다음 실행이 복구'가 성립하지 않는다 (P1).
            # 실제 저널을 오염시키지 않도록 임시 저널로 격리해 검사한다.
            _vault_apart = _vault_at(Path(td) / "uj_partial")
            _tmp_j = update_state.journal_path(_vault_apart)
            _tmp_j.parent.mkdir(parents=True)
            _tmp_j.write_text('{"kind": "done", "txn": "TOK"}\n'
                              '{"kind": "begin", "txn": "TPART"',   # 잘린 행
                              encoding="utf-8")
            lenient = update_txn.journal_lenient(_vault_apart)
            check("관대한 판독은 부분 행을 건너뛰고 나머지를 읽는다",
                  [r.get("txn") for r in lenient] == ["TOK"], lenient)
            try:
                core.ledger_read(_tmp_j)
                _strict_failed = False
            except ValueError:
                _strict_failed = True     # 엄격 판독은 손상으로 본다
            check("엄격 판독은 같은 저널에서 손상을 보고한다", _strict_failed)
            # 부분 행뿐인 저널에서도 복구는 수행된다(목록이 비어도 rollback)
            victim2 = ROOT / "_governance/UpdDoc.md"
            orig_v2 = victim2.read_bytes()
            update_txn.txn_begin(VAULT, "txnPART2", "v9.9.9", ["_governance/UpdDoc.md"])
            victim2.write_bytes(b"HALF\n")
            actp = update_txn.txn_recover(VAULT, [])          # done 기록을 못 읽은 상황
            check("판독 가능한 done이 없으면 복구는 rollback",
                  actp == "rollback" and victim2.read_bytes() == orig_v2, actp)

            # 같은 버전인데 릴리스 identity가 다르면 거부 — 태그 force-move 방어 (P1)
            check("적용 완료 기록에 릴리스 identity(attest)가 남는다",
                  any(r.get("kind") == "done" and r.get("attest")
                      for r in core.ledger_read(VAULT.ledger / "update.jsonl")))
            _ver_now = update_state.current_version(VAULT)
            if _ver_now:                     # 같은 버전으로 다시 릴리스(force-move 모사)
                forced = Path(td) / "forced"
                # 작업 트리만 복사한다 — 정본의 `.git`은 새 이력으로 갈아 낄
                # 것이고, 읽는 동안 git 정비가 고치면 복사가 죽는다.
                shutil.copytree(can, forced, ignore=shutil.ignore_patterns(".git"))
                (forced / "_governance/UpdDoc.md").write_text(
                    node_text("260802-uupd-0002", "정본 규범 문서", "위조판."),
                    encoding="utf-8")
                subprocess.run(["git", "-C", str(forced), "init", "-q", "-b", "main"],
                               capture_output=True)
                for _k, _v in (("user.email", "t@t"), ("user.name", "t"),
                               ("core.autocrlf", "false"), ("core.eol", "lf")):
                    subprocess.run(["git", "-C", str(forced), "config", _k, _v],
                                   capture_output=True)
                subprocess.run(["git", "-C", str(forced), "add", "-A"],
                               capture_output=True)
                subprocess.run(["git", "-C", str(forced), "commit", "-qm", "forced"],
                               capture_output=True)
                _rel(_ver_now, forced)
                efm = uerr(lambda: confirmed_update(source="bundle", bundle=str(forced),
                                              apply=True))
                check("같은 버전·다른 identity는 거부(force-move 방어)",
                      efm is not None and "identity" in efm, efm)

            # 다기기 병렬 적용 — 값이 같은 극대는 동치로 수렴한다 (P2)
            def _rr(n):
                return f"00000000-0000-7000-8000-{n:012d}"
            forked = [
                {"rid": _rr(31), "parents": [], "kind": "apply", "txn": "TA",
                 "path": "F", "hash": "sha256:same"},
                {"rid": _rr(32), "parents": [_rr(31)], "kind": "done", "txn": "TA"},
                # 다른 기기가 같은 릴리스를 적용한 뒤 union 병합된 갈래
                {"rid": _rr(33), "parents": [], "kind": "apply", "txn": "TB",
                 "path": "F", "hash": "sha256:same"},
                {"rid": _rr(34), "parents": [_rr(33)], "kind": "done", "txn": "TB"},
            ]
            check("값이 같은 병렬 극대는 동치로 수렴(baseline 유지)",
                  update_state.last_applied_hash(forked, "F") == "sha256:same",
                  update_state.last_applied_hash(forked, "F"))
            conflicted = forked[:3] + [
                {"rid": _rr(35), "parents": [_rr(33)], "kind": "done", "txn": "TB"}]
            conflicted[2] = dict(conflicted[2], hash="sha256:other")
            check("값이 갈리는 병렬 극대는 여전히 미확정",
                  update_state.last_applied_hash(conflicted, "F") is None,
                  update_state.last_applied_hash(conflicted, "F"))

            # 저널 디렉터리 엔트리 내구화 — 표식을 지우기 전에 저널의 이름이
            # 살아 있어야 baseline이 유실되지 않는다 (P1)
            _fs_calls = []
            _real_fsd = update_txn._fsync_dir
            try:
                update_txn._fsync_dir = lambda d: (_fs_calls.append(str(d)),
                                               _real_fsd(d))[1]
                update_txn.fsync_journal_home(VAULT)
            finally:
                update_txn._fsync_dir = _real_fsd
            check("저널 홈과 그 조상이 ROOT까지 내구화된다",
                  str(update_state.journal_path(VAULT).parent) in _fs_calls
                  and str(ROOT) in _fs_calls, _fs_calls[:4])

            # 삭제됐던 파일의 rollback은 **권한까지** 되돌린다 (P2)
            _permf = ROOT / "_governance/permtest.md"
            _permf.write_text("x\n", encoding="utf-8")
            os.chmod(_permf, 0o600)
            mine.append(_permf)
            update_txn.txn_begin(VAULT, "txnPERM", "v9.9.9", ["_governance/permtest.md"])
            _manp = json.loads(update_txn.txn_manifest(VAULT).read_text(encoding="utf-8"))
            check("manifest가 pre-image의 mode를 담는다",
                  _manp["entries"][0].get("mode")
                  == (0o600 if POSIX_MODE
                      else stat.S_IMODE(_permf.stat().st_mode)),
                  _manp["entries"][0].get("mode"))
            _permf.unlink()                       # remove를 적용한 상태를 모사
            update_txn.txn_recover(VAULT, [{"kind": "begin", "txn": "txnPERM"}])
            check("삭제 rollback이 내용을 복원한다",
                  _permf.exists()
                  and _permf.read_text(encoding="utf-8") == "x\n",
                  _permf.exists())
            if POSIX_MODE:
                check("삭제 rollback이 권한도 복원한다",
                      stat.S_IMODE(_permf.stat().st_mode) == 0o600,
                      oct(stat.S_IMODE(_permf.stat().st_mode)))
            else:
                skip("삭제 rollback이 권한도 복원한다",
                     "Windows는 POSIX mode를 보존하지 않는다")

            # roll-forward도 저널 홈 내구화를 재시도한 뒤에 표식을 지운다 (P1)
            _fs2 = []
            _real_fsd2 = update_txn._fsync_dir
            update_txn.txn_begin(VAULT, "txnRF", "v9.9.9", [])
            try:
                update_txn._fsync_dir = lambda d: (_fs2.append(str(d)),
                                               _real_fsd2(d))[1]
                actrf = update_txn.txn_recover(VAULT, [{"kind": "done", "txn": "txnRF"}])
            finally:
                update_txn._fsync_dir = _real_fsd2
            check("roll-forward가 저널 홈을 내구화한 뒤 표식을 지운다",
                  actrf == "roll-forward"
                  and str(update_state.journal_path(VAULT).parent) in _fs2
                  and not update_txn.txn_manifest(VAULT).exists(), (actrf, _fs2[:3]))

            # chmod 결과까지 내구화한다 (P2)
            _permf2 = ROOT / "_governance/permtest2.md"
            _permf2.write_text("y\n", encoding="utf-8")
            os.chmod(_permf2, 0o600)
            mine.append(_permf2)
            update_txn.txn_begin(VAULT, "txnPERM2", "v9.9.9", ["_governance/permtest2.md"])
            _permf2.unlink()
            _ff = []
            _real_ffile = update_txn._fsync_file
            try:
                update_txn._fsync_file = lambda q: (_ff.append(str(q)),
                                                _real_ffile(q))[1]
                update_txn.txn_recover(VAULT, [{"kind": "begin", "txn": "txnPERM2"}])
            finally:
                update_txn._fsync_file = _real_ffile
            check("권한 복원 뒤 파일 메타데이터를 내구화한다",
                  str(_permf2) in _ff, _ff[:3])
            if POSIX_MODE:
                check("그 복원이 mode까지 되돌린다",
                      stat.S_IMODE(_permf2.stat().st_mode) == 0o600,
                      oct(stat.S_IMODE(_permf2.stat().st_mode)))
            else:
                skip("그 복원이 mode까지 되돌린다",
                     "Windows는 POSIX mode를 보존하지 않는다")

            # 정상 원자 교체도 mode를 **fsync 앞에서** 확정한다 — 뒤에 chmod하면
            # 그 메타데이터가 내구화되지 않아 권한만 0600으로 남을 수 있다 (P2).
            #
            # 검사하는 것은 *어느 호출을 쓰느냐*가 아니라 **mode 확정이 fsync보다
            # 앞서느냐**다. 호출 이름으로 검사하면 fchmod가 없는 환경(Windows)의
            # chmod 대체 경로는 영영 검증되지 않고, 그 환경에서는 수트가 fchmod를
            # 읽는 자리에서 먼저 죽는다. 그래서 POSIX에서도 fchmod를 지운 채 한 번
            # 더 돌려 **양쪽 분기를 같은 불변식으로** 검사한다.
            #
            # `scripts/recover.py`는 이 함수의 의도적 중복이므로 같은 잣대를 댄다 —
            # 중복이 갈라지는 순간을 여기서 잡는다.
            def _mode_before_fsync(write, tag, drop_fchmod):
                order = []
                real_fchmod = getattr(os, "fchmod", None)
                real_chmod, real_fsync = os.chmod, os.fsync
                f = ROOT / ("_governance/permtest3-%s.md" % tag)
                f.write_text("z\n", encoding="utf-8")
                real_chmod(f, 0o640)
                mine.append(f)
                try:
                    if drop_fchmod:
                        if real_fchmod is not None:
                            del os.fchmod              # 대체 경로를 강제한다
                    elif real_fchmod is not None:
                        os.fchmod = lambda fd_, m_: (order.append("mode"),
                                                     real_fchmod(fd_, m_))[1]
                    os.chmod = lambda p_, m_: (order.append("mode"),
                                               real_chmod(p_, m_))[1]
                    os.fsync = lambda fd_: (order.append("fsync"),
                                            real_fsync(fd_))[1]
                    write(f, b"new\n")
                finally:
                    if real_fchmod is not None:
                        os.fchmod = real_fchmod
                    os.chmod, os.fsync = real_chmod, real_fsync
                return (("mode" in order and "fsync" in order
                         and order.index("mode") < order.index("fsync")),
                        stat.S_IMODE(f.stat().st_mode) == 0o640
                        and f.read_bytes() == b"new\n",
                        order[:4])

            import importlib.util as _ilu
            _rspec = _ilu.spec_from_file_location(
                "osk_recover_probe", ENGINE / "scripts" / "recover.py")
            _rmod = _ilu.module_from_spec(_rspec)
            _rspec.loader.exec_module(_rmod)
            for _write, _who in ((update_txn.write_atomic, "update"), (_rmod._write_atomic, "recover")):
                for _tag, _drop, _via in ((_who + "a", False, "fchmod"),
                                          (_who + "b", True, "chmod 대체")):
                    if _drop is False and not hasattr(os, "fchmod"):
                        continue                       # Windows — 그 분기가 없다
                    _o, _k, _d = _mode_before_fsync(_write, _tag, _drop)
                    check("%s: 원자 교체는 mode를 fsync 앞에서 확정한다(%s)"
                          % (_who, _via), _o, _d)
                    if POSIX_MODE:
                        check("%s: 원자 교체가 기존 권한을 유지한다(%s)"
                              % (_who, _via), _k)
                    else:
                        skip("%s: 원자 교체가 기존 권한을 유지한다(%s)"
                             % (_who, _via),
                             "Windows는 POSIX mode를 보존하지 않는다")

            # 트랜잭션 정리 실패는 fail-closed (P2)
            update_txn.txn_begin(VAULT, "txnCLR", "v9.9.9", [])
            _saved = shutil.rmtree
            try:
                shutil.rmtree = lambda *a, **k: None   # 정리가 안 되는 상황
                ecl = uerr(lambda: update_txn.txn_clear(VAULT))
                check("트랜잭션 정리 실패는 fail-closed",
                      ecl is not None and "정리 실패" in ecl, ecl)
            finally:
                shutil.rmtree = _saved
                update_txn.txn_clear(VAULT)

            # has_history — apply/remove/done 이력 유무 (adopt 게이팅 근거) (P2)
            check("has_history: 빈/이력",
                  not update_state.has_history(VAULT, [])
                  and not update_state.has_history(VAULT, [{"kind": "begin"}])
                  and update_state.has_history(VAULT, [{"kind": "apply"}]))

            # 디렉터리 bundle은 snapshot을 뜬다 — 검증 후 원본 변경 TOCTOU 차단 (P1)
            snb = Path(td) / "snapbundle"
            (snb / "docs").mkdir(parents=True)
            (snb / "docs/x.md").write_text("orig\n", encoding="utf-8")
            snap = update_source.fetch_bundle(str(snb), Path(td) / "snapdst")
            (snb / "docs/x.md").write_text("MUTATED\n", encoding="utf-8")  # 이후 변경
            check("디렉터리 bundle은 snapshot이라 원본 변경에 영향 없다",
                  (snap / "docs/x.md").read_text(encoding="utf-8") == "orig\n"
                  and snap != snb)

            # 동시 데몬 잠금 — update가 mutation 잠금을 잡으면 데몬 tick은 건너뛴다
            # (git repo인 can으로 검사 — once는 is_git_repo를 먼저 본다) (P1)
            import sync_daemon as _sd
            _lp = _sd._lock_path(can, "osk-mutation.lock")
            _held = open(_lp, "w")
            try:
                _portalock.lock_exclusive(_held, blocking=False)   # update처럼 선점
                check("데몬은 update가 잡은 mutation 잠금에서 tick을 건너뛴다",
                      _sd.once(can) == "locked")
            finally:
                _portalock.unlock(_held); _held.close()

            # 데몬이 pending 트랜잭션 표식을 보면 tick을 거부한다 (P1)
            _tm = can / ".osk" / "txn" / "manifest.json"
            _tm.parent.mkdir(parents=True, exist_ok=True)
            _tm.write_text('{"txn":"x","entries":[]}', encoding="utf-8")
            try:
                check("데몬은 미완료 트랜잭션 표식에서 tick을 거부",
                      _sd.once(can) == "pending-txn", _sd.once(can))
            finally:
                shutil.rmtree(can / ".osk", ignore_errors=True)

            # 데몬 실행 중에는 갱신하지 않는다 — 구버전 데몬 bootstrap 방어 (P1)
            _sing = open(_sd._lock_path(ROOT, "osk-sync.lock"), "w")
            try:
                _portalock.lock_exclusive(_sing, blocking=False)   # 데몬처럼 선점
                eds = uerr(lambda: confirmed_update(source="bundle", bundle=str(can),
                                              apply=True))
                check("데몬 실행 중 갱신은 거부",
                      eds is not None and "데몬" in eds, eds)
            finally:
                _portalock.unlock(_sing); _sing.close()

            # MAP 사상 — publish.collect과 같은 src/a -> dst/a (P2)
            man_id = {"map": [("_governance/", "_governance/"),
                              (".mcp.json.example", ".mcp.json.example")],
                      "deny": [], "skel": [], "keep": []}
            check("MAP 항등 사상",
                  update_plan._map_dest("_governance/C.md", man_id) == "_governance/C.md"
                  and update_plan._map_dest(".mcp.json.example", man_id)
                  == ".mcp.json.example")
            man_nz = {"map": [("src/", "dst/")],
                      "deny": [], "skel": [], "keep": []}
            check("MAP 비항등 사상(src/a -> dst/a)·미매칭은 None",
                  update_plan._map_dest("src/a/b.md", man_nz) == "dst/a/b.md"
                  and update_plan._map_dest("other/x", man_nz) is None)

            # 버전 계약 — release와 updater 자동 탐색이 같은 vX.Y.Z를 쓴다 (P2)
            check("release: 느슨한 버전(v2.2)은 선언 전에 거부",
                  "vX.Y.Z" in (uerr(lambda: release.run(VAULT,
                      "v2.2", apply=True, root=can)) or ""))
            check("release: 비형식(vfoo)도 거부",
                  "vX.Y.Z" in (uerr(lambda: release.run(VAULT,
                      "vfoo", apply=True, root=can)) or ""))
            badrel = Path(td) / "badrel"; badrel.mkdir()
            (badrel / "release.json").write_text(
                json.dumps({"version": "v2.2", "files": {}}), encoding="utf-8")
            check("load_release: 느슨한 version은 증빙 판독에서 거부",
                  "version 형식" in (uerr(
                      lambda: update_source.load_release(badrel)) or ""))

            # ROOT 내부 `..` 재진입도 거부 — floor 판정 우회 차단 (P1)
            check("resolve_within/canon_rel: ROOT 내부 재진입(../)은 거부, 정상은 통과",
                  core.resolve_within(ROOT, "docs/../00_Scope/x") is None
                  and core.canon_rel(ROOT, "00_Scope/../_governance/x") is None
                  and core.canon_rel(ROOT, "_governance/x") == "_governance/x")

            # skip은 적용 baseline을 가리지 않는다 — conflict 사건과 상태 분리 (P2)
            j = [{"rid": "00000000-0000-7000-8000-00000000ba01",
                  "parents": [], "kind": "apply", "path": "F",
                  "hash": "sha256:aaa"},
                 {"rid": "00000000-0000-7000-8000-00000000ba02",
                  "parents": ["00000000-0000-7000-8000-00000000ba01"],
                  "kind": "skip", "skipped_path": "F", "why": "conflict"}]
            check("skip 뒤에도 baseline은 마지막 apply",
                  update_state.last_applied_hash(j, "F") == "sha256:aaa"
                  and "F" in update_state.managed_paths(j))

            # 명시 ref/pin은 정식 태그여야 한다 — 브랜치로 경계 우회 금지 (P2)
            check("tag_exists: 태그만 인정(브랜치·부재는 False)",
                  update_source.tag_exists(str(can), "v9.0.1")
                  and not update_source.tag_exists(str(can), "main")
                  and not update_source.tag_exists(str(can), "master")
                  and not update_source.tag_exists(str(can), "v0.0.0"))
            check("git: 비형식 ref(main)는 형식에서 거부(네트워크 전)",
                  "vX.Y.Z" in (uerr(
                      lambda: confirmed_update(source="git", ref="main")) or ""))

            # 현재 판본은 인과 극대로 판정한다(물리 마지막 행이 아니다) —
            # sibling last_applied_hash와 같은 규율, union 병합 대장이므로.
            chain = [
                {"rid": "00000000-0000-7000-8000-000000000001",
                 "parents": [], "kind": "done", "version": "vA", "at": "t1"},
                {"rid": "00000000-0000-7000-8000-000000000002",
                 "parents": ["00000000-0000-7000-8000-000000000001"],
                 "kind": "done", "version": "vB", "at": "t2"},
            ]
            check("선형 사슬은 인과 첨단(tip)을 낸다",
                  update_state.current_version(VAULT, chain) == "vB",
                  update_state.current_version(VAULT, chain))
            # 두 기기의 동시 갱신(비교 불능 분기) — done[-1]이라면 물리 마지막
            # 하나를 결정적인 양 내지만, 인과 극대는 미확정 None이다(fail-closed)
            fork = chain[:1] + [
                {"rid": "00000000-0000-7000-8000-0000000000ff",
                 "parents": [], "kind": "done", "version": "vX", "at": "t9"}]
            check("동시 갱신(비교 불능 분기)은 미확정 None",
                  update_state.current_version(VAULT, fork) is None,
                  update_state.current_version(VAULT, fork))

    finally:
        # 뒷정리 — 이후 기준선 PASS 유지
        for f in mine:
            f.unlink(missing_ok=True)
        # 이 시험이 설치한 부분 엔진을 후속 발행 시험에 남기지 않는다.
        if not had_engine and installed_engine.exists():
            assert installed_engine.resolve().is_relative_to(MINI.resolve())
            rmtree_force(installed_engine)
        for d in (ROOT / "= UpdSkel", ROOT / "docs",
                  ROOT / "_governance/_engine/scripts",
                  ROOT / "_governance/_engine", ROOT / "_governance/records"):
            try:
                (d / ".gitkeep").unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                pass
