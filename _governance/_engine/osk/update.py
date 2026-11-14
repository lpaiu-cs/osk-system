"""osk.update — 정본 릴리스를 인스턴스로 갱신 (하류 쪽).

구현 근거: Mechanism §1-2(정본과 갱신), 시행령 §10 6항(인스턴스는 갱신으로
정본 릴리스를 받아들이고, 비준증빙에 없는 것은 받지 않는다).

기본 동작은 **보고**다. 첫 `--apply`는 변경집합을 제시하고 거부한다.
사용자 명시 재승인 뒤 같은 변경집합의 재시도 한 번만 비대화형 적용한다.

축의 분리 — 데이터 동기화 데몬은 인스턴스 자신의 원격만 다루고(vault_sync),
갱신은 정본 저장소에서 프레임워크를 받는다. 두 축은 섞이지 않는다.

적용 범위는 릴리스 안의 발행 매니페스트가 정한다(별도 갱신 매니페스트 없음):
- MAP 대상 → 적용   - KEEP(정본 저장소 전용) → 건너뜀
- SKEL → 없는 자리에만 골격
그리고 무엇이 와도 **인스턴스 소유 바닥**(Mechanism §1-2 5항)에는 쓰지
않는다 — `00_Scope`·`00_Domain`·`00_Person`과 이름이 다른 같은 Space의 루트 아래(골격 제외)·`_ledger/`·`_raw/`·`_sources/`·
`.osk/`. 바닥은 매니페스트가 아니라 `update_plan`의 상수다.

적용 규율 (Mechanism §1-2 6항):
- 직전 적용 상태(저널의 인과 극대)와 같은 파일 → 덮는다.
- 로컬 수정이 있는 파일 → 덮지 않는다. 문서는 `<이름>.upstream-<버전>`
  사본을 옆에 두어 표면화하고, **엔진 파일의 로컬 수정은 갱신 전체를
  중단한다** — 엔진을 고치는 자리는 정본이다.
- 기존 인스턴스의 최초 편입은 `--adopt`로 현재 릴리스를 기준선 삼는다.

이 모듈에는 계획과 적용이 남는다: 실행(`run`), 사용자의 확인(`confirm`·`withdraw`), 통치 구획의 검토,
진입점(`main`). 받은 것의 기록은 `update_state`, 릴리스를 받고 대조하는 일은 `update_source`, 무엇을 쓸 수
있는지와 계획은 `update_plan`, 크래시-안전 트랜잭션은 `update_txn`, 데몬을 세워 두는 일은 `daemon_control`이 맡는다.
"""
from __future__ import annotations
import argparse, json, os, re, sys, tempfile, time
from pathlib import Path

from .core import ledger_append, ledger_damage, ledger_read, sha256_file, sha256_bytes, posix_rel, local_lock_path
from .core import fsync_dir as _fsync_dir, mkdirs_durable as _mkdirs_durable
from . import approvals, daemon_control, update_check, update_plan, update_source, update_state, update_txn, approval_acts, region_tree
from .errors import LedgerDamage
from .vault import Vault


def run(vault: Vault, source: str | None = None, ref: str | None = None,
        bundle: str | None = None, apply: bool = False,
        adopt: bool = False) -> dict:
    """기본은 보고. `apply`면 **가장 먼저** mutation 잠금을 잡고 미완료 트랜잭션을
    복구한 뒤에야 상태를 판정한다(Mechanism §1-2 7항) — 복구가 판정 뒤에 오면
    half-applied 상태가 drift·adopt 거부로 오판되어 복구에 도달하지 못한다."""
    if not apply:
        return _run_locked(vault, source, ref, bundle, False, adopt)
    reviewed = None
    # The first attempt must show the plan while the daemon and MCP can stay up.
    # Crash recovery is different: restore the already-authorized pre-image first.
    if not update_txn.txn_manifest(vault).exists():
        preview = _run_locked(vault, source, ref, bundle, False, adopt)
        with daemon_control.exclusive(local_lock_path("osk-update-review.lock", vault.root),
                        "다른 업데이트 확인이 진행 중이다 — 잠시 후 재시도한다"):
            ticket = local_lock_path(CONFIRMATION, vault.root)
            try:
                pending = json.loads(ticket.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pending = {}
            if (not isinstance(pending, dict)
                    or pending.get("review_id") != preview["review_id"]
                    or not isinstance(pending.get("at"), (int, float))
                    or not 0 <= time.time() - pending["at"] < 3600):
                return _request_approval(vault, preview)
            ticket.unlink()  # One retry only; even a failed apply needs fresh consent.
            reviewed = preview["review_id"]
    # 잠금 순서는 데몬과 같다: **싱글턴 → mutation**. 싱글턴(osk-sync.lock)은
    # probe로 잠깐 잡았다 놓으면 그 틈에 구버전 데몬이 떠 버리므로(TOCTOU),
    # 갱신 **수명 내내 보유**한다 — 구·신 데몬 모두 이 잠금을 먼저 잡기 때문에
    # 새 mutation 규약을 모르는 구 데몬의 동시 실행까지 함께 막힌다. 데몬이
    # 쥐고 있으면 `daemon_control.daemon_stopped`가 멈추고 적용 뒤 다시 띄운다.
    daemon: dict = {}
    with daemon_control.daemon_stopped(vault, daemon),\
            daemon_control.exclusive(daemon_control.sync_lock_path(vault),
                       "다른 갱신이 vault를 잠갔다 — 잠시 후 다시 실행한다"):
        # 복구는 어떤 상태 판정보다 먼저다(잠금 안에서). 저널은 **관대하게**
        # 읽는다 — 크래시가 append 도중이면 부분 행이 남는데, 엄격 판독으로
        # 여기서 실패하면 정작 복구에 도달하지 못한다.
        recovered = update_txn.txn_recover(vault, update_txn.journal_lenient(vault))
        if recovered == "rollback":
            try:
                ledger_append(update_state.journal_path(vault),
                              {"kind": "rollback",
                               "why": "미완료 트랜잭션 크래시 복구"})
            except Exception:      # 격리: 저널에 못 적어도 파일 복구는 유효하다(아래)
                pass                    # 저널이 손상돼 기록을 못 남겨도 **파일
                                        # 복구는 유효하다**. 손상 자체는 아래
                                        # 정상 경로의 엄격 판독이 보고한다.
        rep = _run_locked(vault, source, ref, bundle, True, adopt, reviewed)
        if recovered:
            rep["recovered"] = recovered
    if daemon:
        rep["daemon"] = daemon
    return rep


CONFIRMATION = "osk-update-confirmation.json"   # 기기 로컬 확인표 {review_id, at}


def confirm(vault: Vault, review_id: str) -> None:
    """이 갱신 계획이 확인됐다고 적는다 — 1시간 안의 다음 `--apply` 한 번이, 잠금 안에서
    다시 세운 계획이 이 `review_id`일 때만 적용한다. 설치 도구(`osk.setup`)는 사용자가
    확인한 자기 계획에 담긴 `review_id`를 여기 적고 적용을 한 번만 부른다."""
    update_txn.write_atomic(local_lock_path(CONFIRMATION, vault.root), json.dumps(
        {"review_id": review_id, "at": time.time()}).encode())


def withdraw(vault: Vault) -> None:
    """확인표를 지운다 — 사용자에게 보이지 않은 계획의 확인이 남지 않게."""
    local_lock_path(CONFIRMATION, vault.root).unlink(missing_ok=True)


def _request_approval(vault: Vault, report: dict) -> dict:
    """A workflow checkpoint, not proof of a human utterance or a security boundary."""
    confirm(vault, report["review_id"])
    return dict(report, ok=False, approval_required=True, instruction=(
        "여기서 멈추고 사용자에게 이번 업데이트 변경사항과 하네스 재시작 필요성을 "
        "설명한 뒤 명시적 재승인을 요청하라. 기존 구현·릴리스 승인으로 대신하지 마라. "
        "사용자 재승인을 받기 전에는 자동 재시도하지 마라. 재승인 후 같은 명령을 "
        "1시간 안에 한 번 재시도하면 적용한다 — 이 vault의 동기화 데몬은 적용 단계가 "
        "멈추고 끝나면 다시 띄운다. 릴리스·로컬 변경집합이 달라지거나 적용이 실패하면 "
        "새 확인을 요구한다. 적용 후 모든 연결 하네스의 MCP 서버를 재시작해야 한다."
        + _PROTECT_NOTE.get(report.get("governance", {}).get("protect"), "")))


# 통치 구획 지정 여부는 확인 대상의 일부다 — 사용자가 무엇에 답하는지 알게 한다.
_PROTECT_NOTE = {
    "establish": " 이 적용은 통치 구획(_governance)을 비준증빙 내용 그대로 "
                 "보호영역으로 지정한다 — 이후 통치 문서의 로컬 수정은 pending으로 "
                 "드러난다. 이것도 함께 알린다.",
    "withheld": " 통치 구획에 비준증빙과 다른 로컬 내용(governance.unattested)이 "
                "있거나 승인 기록부가 손상돼 보호영역으로 지정하지 않는다 — 사용자가 "
                "차이를 검토한 뒤(손상은 수동 복구가 먼저다) 대화형 단말에서 "
                "`osk protect _governance`로 지정할 수 있다고 함께 알린다.",
}


def _governance_review(vault: Vault, tree: Path, p: dict, side_write: list, ad_write: list,
                       targets: list, rel: dict) -> dict:
    """Hash the exact post-update region, including pre-existing local changes."""
    recs = approvals.records(vault)
    state = approvals.state(vault, "_governance", recs)
    if state == "stale":
        raise update_state.UpdateError("통치 구획 승인본이 stale이다 — 사용자 검토로 먼저 해소한다")
    files = {rel: sha256_file(path) for rel, path in
             region_tree.region_files(vault, vault.root / "_governance")}
    before = sha256_bytes(region_tree.manifest_blob(sorted(files.items())))
    base = approvals.approved_hash(vault, "_governance")
    prior = approvals.changeset(vault, "_governance") if base else None
    for src, dest in p["add"] + p["update"] + side_write:
        if dest.startswith("_governance/") and not region_tree.excluded_rel(dest, "_governance"):
            files[dest] = sha256_file(tree / src)
    for data, dest in ad_write:
        if dest.startswith("_governance/") and not region_tree.excluded_rel(dest, "_governance"):
            files[dest] = sha256_bytes(data)
    for dest in p["remove"]:
        files.pop(dest, None)
    out = {"state": state, "base": base, "before": before,
           "after": sha256_bytes(region_tree.manifest_blob(sorted(files.items()))),
           "existing_changes": prior if state == "pending" else None}
    if state != "unprotected":
        return out
    # 통치 구획은 상설 보호영역이다(헌법 10조 1항). 지정 이력이 없는 설치에서는
    # 확인받은 적용이 지정까지 한다 — 단 적용 뒤 구획이 **비준증빙이 정한 내용과
    # 정확히 같을 때만**. 로컬 차이(충돌 사이드카·증빙 밖 파일 포함)를 초기
    # 승인본으로 축복하지 않고 보고한다. 사용자가 해제한 구획은 다시 지정하지
    # 않는다(해제는 제도의 개정과 함께 한다 — 시행령 §6 1항).
    if any(r.get("region") == "_governance" for r in recs):
        out["protect"] = "released"
        return out
    attested = {dest: rel["files"][src] for src, dest in targets
                if dest.startswith("_governance/")
                and not region_tree.excluded_rel(dest, "_governance")}
    diff = sorted(k for k in files.keys() | attested.keys()
                  if files.get(k) != attested.get(k))
    out["protect"] = "withheld" if diff or approvals.ledger_is_damaged(vault, recs) else "establish"
    if diff:
        out["unattested"] = diff[:20]
    return out


def _release_tree(cfg: dict, source: str, ref: str | None, bundle: str | None, td: str) -> tuple:
    """정본의 릴리스를 임시 폴더에 받는다 — `(트리, 요청한 태그)`. 꾸러미에서 받으면 태그는 None이다."""
    self_tag = None
    if source == "git":
        url = cfg.get("url", update_state.DEFAULT_UPSTREAM)
        pin = ref or cfg.get("pin")
        if pin is not None:
            # 명시 ref/pin도 정식 릴리스 태그여야 한다 — 브랜치(`--to main`)로
            # 태그 경계를 우회하지 못하게, 형식·실재를 함께 확인한다(§1-2 3항).
            if not re.match(update_state.VERSION_RE, str(pin)):
                raise update_state.UpdateError(f"명시 ref/pin은 vX.Y.Z 태그여야 한다: {pin}")
            if not update_source.tag_exists(url, pin):
                raise update_state.UpdateError(f"정본에 그 릴리스 태그가 없다: {pin}")
            tag = pin
        else:
            tag = update_source.latest_release_tag(url)   # 기본은 최신 정식 태그(HEAD 아님)
            if tag is None:
                raise update_state.UpdateError(
                    "정본에 정식 릴리스 태그(vX.Y.Z)가 없다 — 정본에서 "
                    "먼저 릴리스하거나 --to로 참조를 지정한다")
        tree = update_source.fetch_git(url, tag, Path(td))
        self_tag = tag                       # 증빙 version이 이 태그와 같아야 한다
    elif source == "bundle":
        if not bundle:
            raise update_state.UpdateError("bundle 출처에는 --from <경로>가 필요하다")
        tree = update_source.fetch_bundle(bundle, Path(td))
    else:
        raise update_state.UpdateError(f"미정의 출처: {source} (git·bundle)")
    return tree, self_tag


def _attested(vault: Vault, tree: Path, self_tag: str | None) -> tuple:
    """받은 릴리스가 제 증빙과 맞고, 같은 버전으로 이미 적용한 것과 같은 릴리스인가 — `(증빙, 그 해시)`."""
    rel = update_source.load_release(tree)
    if self_tag is not None and rel["version"] != self_tag:
        raise update_state.UpdateError(
            f"요청 태그와 증빙 version이 다르다 — 태그 {self_tag} 의 "
            f"release.json은 {rel['version']} 이다(태그 위조·오지정 차단)")
    errs = update_source.verify_attestation(tree, rel)
    if errs:
        raise update_state.UpdateError("비준증빙 대조 실패 — 아무것도 쓰지 않았다:\n  "
                          + "\n  ".join(errs[:10]))
    # **버전 불변성**: 같은 version이 다른 릴리스 identity로 오면 거부한다.
    # 태그 이름과 증빙의 version만 맞춰서는, 원격에서 태그를 다른 커밋으로
    # force-move한 뒤 자기일관적 증빙만 갖추면 그대로 통과한다(엔진까지
    # 덮인다). 증빙 파일 자체의 해시를 identity로 삼아 저널에 고정한다.
    attest_id = sha256_file(tree / update_state.ATTESTATION)
    for r in update_txn.journal_lenient(vault):
        if (r.get("kind") == "done" and r.get("version") == rel["version"]
                and r.get("attest") and r["attest"] != attest_id):
            raise update_state.UpdateError(
                f"같은 버전인데 릴리스 identity가 다르다 — 중단"
                f"(태그 force-move·위조 의심): {rel['version']}\n"
                f"  적용된 증빙: {r['attest']}\n  받은 증빙: {attest_id}")
    return rel, attest_id


def _review_id(vault: Vault, out: dict, _dests: set, side_write: list, ad_write: list, skel: list,
               adopt: bool) -> str:
    """확인의 대상을 내용에 묶는 값 — 계획, 건드릴 자리의 지금 내용, 저널."""
    # Bind consent to content, not mtime or the version label. The second read
    # under the mutation lock catches changes during the confirmation window.
    paths = sorted(_dests | {s for _, s in side_write + ad_write}
                   | {posix_rel(d / ".gitkeep", vault.root) for d in skel})
    local = [(path, sha256_file(vault.root / path) if (vault.root / path).is_file()
              else "directory" if (vault.root / path).exists() else None) for path in paths]
    identity = {"root": str(vault.root), "plan": out, "local": local,
                "adopt": adopt, "journal": update_txn.journal_lenient(vault)}
    return sha256_bytes(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _require_journal(vault: Vault) -> None:
    """갱신 저널이 기록 가능한가 — 아니면 아무것도 쓰기 전에 멈춘다."""
    # 저널이 **기록 가능한지 먼저 본다.** `update_txn.txn_begin`이 manifest를 남긴
    # 뒤에 `ledger_append`가 손상으로 죽으면(그 호출은 아래 try 밖이다)
    # 표식만 남아 데몬이 영원히 `pending-txn`으로 tick을 거부하고, 다음
    # `--apply`가 같은 자리에서 또 죽으며 표식을 다시 만든다 — 지워도 벗어날
    # 수 없는 상태다(실측). 손상 위에 이력을 더 쌓지 않는다(Mechanism §3 2항).
    try:
        _dmg = ledger_damage(ledger_read(update_state.journal_path(vault)), update_state.journal_path(vault))
    except (OSError, LedgerDamage) as e:
        raise update_state.UpdateError(f"갱신 저널을 읽지 못했다 — 아무것도 쓰지 않았다: {e}")
    if _dmg:
        raise update_state.UpdateError(
            "갱신 저널이 손상됐다 — 아무것도 쓰지 않았다 "
            "(Mechanism §3 8항의 수동 복구가 먼저다):\n  "
            + "\n  ".join(_dmg[:5]))


def _journal_done(vault: Vault, txn: str, v: str, p: dict, applied: list, removed: list, sidecars: list,
                  attest_id: str) -> None:
    """파일 조작이 끝난 뒤 적용 상태를 저널에 남긴다. 마지막 `done`이 이 트랜잭션을 확정한다."""
    # 파일 조작 성공 후에 적용 상태를 저널에 남긴다. 이 기록들은 마지막
    # `done(txn)`이 append될 때까지 `committed()`가 판정에서 가린다 —
    # 중간에 죽어도 파일(pre-image 복구)과 판정이 어긋나지 않는다.
    for dest in applied:
        ledger_append(update_state.journal_path(vault), {
            "kind": "apply", "txn": txn, "version": v, "path": dest,
            "hash": sha256_file(vault.root / dest)})
    for _src, dest in p["rebaseline"]:        # 내용 동일·기준선만 갱신
        ledger_append(update_state.journal_path(vault), {
            "kind": "apply", "txn": txn, "version": v, "path": dest,
            "hash": sha256_file(vault.root / dest)})
    for path in removed:
        ledger_append(update_state.journal_path(vault), {"kind": "remove", "txn": txn,
                                       "version": v, "path": path})
    # skip은 conflict **사건**이지 적용 상태 변경이 아니다 — `skipped_path`로
    # 남겨 baseline/관리 판정(`path` 키)이 이를 보지 않게 한다(P2).
    for _src, dest in p["conflict"]:
        ledger_append(update_state.journal_path(vault), {
            "kind": "skip", "txn": txn, "version": v, "skipped_path": dest,
            "why": "로컬 수정 — upstream 사본을 옆에 두었다"})
    for path in p["remove_conflict"]:
        ledger_append(update_state.journal_path(vault), {
            "kind": "skip", "txn": txn, "version": v, "skipped_path": path,
            "why": "upstream 삭제됐으나 로컬 수정 — 보존한다"})
    ledger_append(update_state.journal_path(vault), {
        "kind": "done", "txn": txn, "version": v, "attest": attest_id,
        "applied": len(applied), "removed": len(removed),
        "conflicts": len(sidecars)})


def _run_locked(vault: Vault, source: str | None, ref: str | None, bundle: str | None,
                apply: bool, adopt: bool, reviewed: str | None = None) -> dict:
    cfg = update_state.load_config(vault).get("upstream", {})
    source = source or ("bundle" if bundle else cfg.get("source", "git"))
    # adopt는 **최초 편입** 전용이다 — 이미 관리 이력이 있으면 거부한다. 안 그러면
    # 정상 관리 인스턴스에서 로컬 수정(엔진 포함)을 아무 때나 덮는 force가 된다.
    if adopt and update_state.has_history(vault):
        raise update_state.UpdateError(
            "이미 갱신 관리 이력이 있다 — adopt는 최초 편입에만 허용된다. "
            "로컬 수정은 정본에서 고치거나 로컬 사본을 정리한 뒤 갱신한다")
    with tempfile.TemporaryDirectory() as td:
        tree, self_tag = _release_tree(cfg, source, ref, bundle, td)
        rel, attest_id = _attested(vault, tree, self_tag)
        targets, skel, skipped = update_plan.apply_set(vault, tree, rel)
        p = update_plan.plan(vault, tree, targets, adopt)
        dests = lambda pairs: [d for _s, d in pairs]
        out = {"ok": True, "applied": False, "version": rel["version"],
               "current": update_state.current_version(vault), "files": len(targets),
               "add": dests(p["add"]), "update": dests(p["update"]),
               "same": p["same"], "rebaseline": dests(p["rebaseline"]),
               "conflict": dests(p["conflict"]), "engine_drift": p["engine_drift"],
               "adopted": dests(p["adopted"]),
               "remove": p["remove"], "remove_conflict": p["remove_conflict"],
               "skipped": len(skipped)}
        if p["engine_drift"]:
            raise update_state.UpdateError(
                "엔진 파일에 로컬 수정이 있다 — 갱신 전체를 중단한다. 엔진을 "
                "고치는 자리는 정본이다(Mechanism §1-2 6항). 기존 인스턴스의 "
                "최초 편입이면 --adopt로 현재 릴리스를 기준선 삼는다:\n  "
                + "\n  ".join(p["engine_drift"][:10]))
        # (잠금·미완료 트랜잭션 복구는 run()이 이미 끝냈다 — 여기는 잠금 안이다.)
        v = rel["version"]
        # 사이드카는 사용자의 수동 병합 작업 파일이다 — 덮을 것/인정할 것/보존할
        # 것을 먼저 가른다. 관리 파일과 경로가 겹치면 중단한다.
        _dests = {d for _s, d in targets} | set(p["remove"])
        side_write, side_kept, side_held, side_collide = update_plan.sidecar_plan(vault,
            p, tree, v, _dests)
        # adopt가 덮을 로컬 내용도 같은 규율로 옆에 남긴다 — 덮기 전 바이트를
        # 여기서 읽어 둔다(아래 적용 루프가 그 자리를 먼저 덮을 수 있다).
        ad_write, ad_kept, ad_blocked, ad_collide = update_plan.adopt_sidecar_plan(vault,
            p, v, _dests)
        out["sidecar_held"] = side_held
        out["adopt_blocked"] = ad_blocked
        if ad_blocked:
            raise update_state.UpdateError(
                "adopt가 덮을 자리의 사이드카가 이미 있고 내용이 다르다 — "
                "아무것도 쓰지 않았다. 진행하면 지금 파일의 로컬 수정이 어디에도 "
                "남지 않는다(성공 뒤 pre-image도 지워진다). 그 사이드카를 확인해 "
                "필요한 것을 챙긴 뒤 옮기거나 지우고 다시 실행하라:\n  "
                + (chr(10) + "  ").join(ad_blocked[:10]))
        side_collide = side_collide + ad_collide
        if side_collide:
            raise update_state.UpdateError(
                "사이드카 경로가 정식 관리 파일과 겹친다 — 갱신을 중단한다"
                "(관리 파일을 되덮게 된다):\n  " + "\n  ".join(side_collide[:10]))
        governance = _governance_review(vault, tree, p, side_write, ad_write, targets, rel)
        out["governance"] = governance
        out["restart_required"] = True
        out["attestation"] = attest_id
        out["layout"] = vault.space_roots
        out["review_id"] = _review_id(vault, out, _dests, side_write, ad_write, skel, adopt)
        if not apply:
            if update_txn.txn_manifest(vault).is_file():
                out["pending_txn"] = True
            return out
        if reviewed != out["review_id"]:
            return _request_approval(vault, out)
        _require_journal(vault)
        txn = os.urandom(8).hex()                # 파일과 저널을 묶는 트랜잭션 id
        # 이번에 건드릴 파일(canonical 상대)의 pre-image를 영속 백업하고 manifest를
        # 기록한 **뒤에만** target을 건드린다. 이후 크래시는 다음 실행이 판정한다:
        # done(txn)이 있으면 roll-forward, 없으면 rollback.
        touch = [dest for _s, dest in p["add"] + p["update"]]
        touch += list(p["remove"])
        touch += [side for _s, side in side_write]
        touch += [side for _b, side in ad_write]
        touch += [posix_rel(d / ".gitkeep", vault.root) for d in skel if not d.exists()]
        accept_governance = governance["base"] and governance["after"] != governance["base"]
        establish = governance.get("protect") == "establish"
        if accept_governance or establish:
            touch.append(posix_rel(approvals.ledger_path(vault), vault.root))
        update_txn.txn_begin(vault, txn, v, touch)
        ledger_append(update_state.journal_path(vault), {"kind": "begin", "txn": txn,
                                       "version": v, "adopt": bool(adopt)})
        applied, removed, sidecars, made_skel = [], [], [], []
        try:
            for local, side in ad_write:      # **덮기 전에** 로컬을 옆에 남긴다
                update_txn.write_atomic(vault.root / side, local)
                sidecars.append(side)
            sidecars += ad_kept
            for src, dest in p["add"] + p["update"]:
                update_txn.write_atomic(vault.root / dest, (tree / src).read_bytes())
                applied.append(dest)
            for path in p["remove"]:              # path는 이미 canonical(P1)
                tp = vault.root / path
                tp.unlink(missing_ok=True)
                _fsync_dir(tp.parent)             # 삭제 엔트리 내구화(전원 차단)
                removed.append(path)
            for src, side in side_write:          # 없던 사이드카만 쓴다
                update_txn.write_atomic(vault.root / side, (tree / src).read_bytes())
                sidecars.append(side)
            sidecars += side_kept                 # 이미 같은 것은 그대로 인정
            for d in skel:                        # 이미 _allowed_skel로 봉쇄·검증됨
                if not d.exists():
                    _mkdirs_durable(d)            # 만든 각 조상의 부모까지 내구화
                    update_txn.write_atomic(d / ".gitkeep", b"")
                    made_skel.append(posix_rel(d, vault.root))
            if accept_governance:
                approval_acts.approve_locked(vault, "_governance", governance["base"],
                                          governance["after"],
                                          f"osk.update {v}; user confirmation: {reviewed}")
            if establish:                     # 확인받은 증빙 tree 그대로만 지정
                approval_acts.protect(vault, "_governance",
                                  f"osk.update {v}; user confirmation: {reviewed}",
                                  expect_work=governance["after"], _locked=True)
        except (OSError, ValueError) as e:
            update_txn.txn_recover(vault, ledger_read(update_state.journal_path(vault)))   # done(txn) 없음 → rollback
            ledger_append(update_state.journal_path(vault),
                          {"kind": "rollback", "txn": txn, "version": v,
                           "why": str(e)[:200]})
            raise update_state.UpdateError(f"갱신 적용 중 실패 — 원상복구했다: {e}")

        _journal_done(vault, txn, v, p, applied, removed, sidecars, attest_id)
        # 커밋 기록의 **디렉터리 엔트리**까지 내구화한 뒤에만 표식을 지운다 —
        # 저널이 이번에 처음 생겼으면 이름이 유실될 수 있고, 그러면 파일은 새
        # 판인데 복구 표식도 없어 baseline이 통째로 사라진다.
        update_txn.fsync_journal_home(vault)
        update_txn.txn_clear(vault)                              # 커밋 완료 — 트랜잭션 영역 정리
        out.update(applied=True, applied_files=len(applied),
                   removed=removed, sidecars=sidecars, skel_created=made_skel,
                   governance_accepted=bool(accept_governance),
                   governance_protected=("established" if establish
                                         else governance.get("protect")),
                   note="엔진이 갱신되었으면 실행 중인 MCP 서버를 재시작한다 — "
                        "이 기기의 동기화 데몬은 갱신이 멈췄다 다시 띄운다(daemon)")
        return out


def main(argv=None):
    vault = Vault.open()
    ap = argparse.ArgumentParser(
        prog="osk-update",
        description="정본 릴리스로 갱신 — 기본은 보고, 쓰기는 --apply")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", help="버전 태그 pin (기본: 설정의 pin 또는 최신 릴리스 태그)")
    ap.add_argument("--from", dest="bundle", help="로컬 bundle 경로 (오프라인)")
    ap.add_argument("--source", choices=("git", "bundle"))
    ap.add_argument("--adopt", action="store_true",
                    help="기존 인스턴스의 최초 편입 — 현재 릴리스를 기준선 삼는다")
    ap.add_argument("--check", action="store_true",
                    help="정본의 최신 릴리스 태그만 묻는다 — 받지도 적용하지도 않는다")
    ap.add_argument("--ack", metavar="VERSION",
                    help="알리는 새 릴리스를 에이전트가 받았다고 기록한다 — 기록 전까지 세션 시작마다 "
                         "다시 알린다. 갱신하지 않는다")
    a = ap.parse_args(argv)
    if a.ack:
        if a.check or a.apply or a.adopt or a.to or a.bundle or a.source:
            sys.exit("[중단] --ack는 다른 선택지와 함께 쓰지 않는다 — 받음만 기록한다")
        try:
            rep = update_check.ack(vault, a.ack)
        except update_state.UpdateError as e:
            sys.exit(f"[중단] {e}")
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if not rep["synced"]:
            raise SystemExit(1)
        return
    if a.check:
        if a.apply or a.adopt or a.to or a.bundle or a.source:
            sys.exit("[중단] --check는 다른 선택지와 함께 쓰지 않는다 — 태그만 묻는다")
        try:
            rep = update_check.check(vault)
        except update_state.UpdateError as e:
            sys.exit(f"[중단] {e}")
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if rep.get("error"):
            raise SystemExit(1)
        return
    try:
        rep = run(vault, a.source, a.to, a.bundle, a.apply, a.adopt)
    except update_state.UpdateError as e:
        sys.exit(f"[중단] {e}")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if rep.get("approval_required"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
