#!/usr/bin/env python3
"""갱신 트랜잭션 복구 부트스트랩 — 엔진과 **독립**이다.

근거: Mechanism §1-2 7항(미완료 트랜잭션은 pre-image로 복구한다).

왜 별도 스크립트인가: 갱신은 엔진 자신(`osk/*.py`)을 교체한다. 그 도중에
프로세스가 죽으면 엔진이 반쯤 교체된 상태가 되어 `osk.update`의 import 자체가
깨질 수 있고, 그러면 정작 복구 코드에 도달할 수 없다. 이 파일은 표준 라이브러리만
쓰고 osk 패키지를 import하지 않으므로 그 상황에서도 돈다.

사용:
    python3 _governance/_engine/scripts/recover.py [--root <vault>] [--apply]

기본은 **보고**다. `--apply`가 있어야 되돌린다. 트랜잭션이 커밋된 뒤(저널에
`done(txn)`) 남은 표식은 파일을 건드리지 않고 표식만 정리한다(roll-forward).
복구 자료(백업)가 없거나 손상됐으면 아무것도 지우지 않고 중단한다(fail-closed).
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sys, tempfile
from pathlib import Path


def _sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _canon_rel(root: Path, rel: str) -> str | None:
    """osk.update._canon_rel과 같은 규율 — 탈출·`..`/`.`·symlink 재지정 거부."""
    p = Path(rel)
    if not p.parts or p.is_absolute() or any(s in ("..", ".") for s in p.parts):
        return None
    try:
        broot = Path(os.path.realpath(root))
        real = Path(os.path.realpath(root / p))
        if real == broot:
            return None
        canon = real.relative_to(broot).as_posix()
    except (ValueError, OSError):
        return None
    return canon if canon == p.as_posix() else None


def _journal_done(root: Path, txn: str) -> bool:
    j = root / "= Scope/Workbench/_ledger/update.jsonl"
    if not j.is_file():
        return False
    for line in j.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue          # 손상 행은 이 판정에서 건너뛴다(보수적으로 미커밋)
        if isinstance(r, dict) and r.get("kind") == "done" and r.get("txn") == txn:
            return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="osk-recover", description=__doc__)
    ap.add_argument("--root", help="vault 루트 (기본: 이 스크립트에서 3단계 위)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else \
        Path(__file__).resolve().parent.parent.parent.parent
    txn_dir = root / ".osk" / "txn"
    manifest = txn_dir / "manifest.json"
    if not manifest.is_file():
        print(json.dumps({"pending": False, "root": str(root)},
                         ensure_ascii=False))
        return 0
    try:
        man = json.loads(manifest.read_text(encoding="utf-8"))
        txn, entries = man["txn"], man["entries"]
    except (OSError, ValueError, KeyError) as e:
        print(f"[중단] manifest 손상 — 수동 개입 필요(보존: {txn_dir}): {e}",
              file=sys.stderr)
        return 2

    committed = _journal_done(root, txn)
    plan = []
    for e in entries:
        cp = _canon_rel(root, str(e.get("rel", "")))
        if cp is None:
            print(f"[중단] 복구 경로가 봉쇄·정체성 검증 실패 — 수동 개입 "
                  f"필요(보존: {txn_dir}): {e.get('rel')}", file=sys.stderr)
            return 2
        plan.append((cp, bool(e.get("existed")), str(e.get("backup")),
                     e.get("hash")))
    rep = {"pending": True, "txn": txn, "version": man.get("version"),
           "committed": committed,
           "action": "roll-forward" if committed else "rollback",
           "files": [c for c, *_ in plan], "applied": False}
    if not a.apply:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    if committed:                     # 파일은 새 판이 정답 — 표식만 정리
        shutil.rmtree(txn_dir, ignore_errors=True)
        rep["applied"] = True
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    for cp, existed, key, h in plan:  # 사전 검증 후 되돌린다(fail-closed)
        bp = txn_dir / "backup" / key
        if existed and (not bp.is_file() or (h and _sha256(bp) != h)):
            print(f"[중단] 백업 부재·손상 — 복구 불가, 수동 개입 필요"
                  f"(보존: {txn_dir}): {cp}", file=sys.stderr)
            return 2
    for cp, existed, key, _h in plan:
        p = root / cp
        try:
            if existed:
                _write_atomic(p, (txn_dir / "backup" / key).read_bytes())
            else:
                p.unlink(missing_ok=True)
        except OSError as e:
            print(f"[중단] 복구 실패 — 백업 보존({txn_dir}): {cp} {e}",
                  file=sys.stderr)
            return 2
    shutil.rmtree(txn_dir, ignore_errors=True)
    rep["applied"] = True
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
