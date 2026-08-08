"""osk.update — 정본 릴리스를 인스턴스로 갱신 (하류 쪽).

구현 근거: Mechanism §1-2(정본과 갱신), 시행령 §10 6항(인스턴스는 갱신으로
정본 릴리스를 받아들이고, 비준증빙에 없는 것은 받지 않는다).

기본 동작은 **보고**다. 쓰기는 `--apply`로만 하고, 가드는 전부 fail-closed다.

축의 분리 — 데이터 동기화 데몬은 인스턴스 자신의 원격만 다루고(vault_sync),
갱신은 정본 저장소에서 프레임워크를 받는다. 두 축은 섞이지 않는다.

적용 범위는 릴리스 안의 발행 매니페스트가 정한다(별도 갱신 매니페스트 없음):
- MAP 대상 → 적용   - KEEP(정본 저장소 전용) → 건너뜀
- SKEL → 없는 자리에만 골격
그리고 무엇이 와도 **인스턴스 소유 바닥**(Mechanism §1-2 5항)에는 쓰지
않는다 — `= ` Space 루트 아래(골격 제외)·`_ledger/`·`_raw/`·`_sources/`·
`.osk/`. 바닥은 매니페스트가 아니라 이 모듈의 상수다.

적용 규율 (Mechanism §1-2 6항):
- 직전 적용 상태(저널의 인과 극대)와 같은 파일 → 덮는다.
- 로컬 수정이 있는 파일 → 덮지 않는다. 문서는 `<이름>.upstream-<버전>`
  사본을 옆에 두어 표면화하고, **엔진 파일의 로컬 수정은 갱신 전체를
  중단한다** — 엔진을 고치는 자리는 정본이다.
- 기존 인스턴스의 최초 편입은 `--adopt`로 현재 릴리스를 기준선 삼는다.
"""
from __future__ import annotations
import argparse, errno, json, os, re, shutil, subprocess, sys, tarfile, tempfile
from contextlib import contextmanager
from pathlib import Path

from .core import (ROOT, LEDGER, causal_maxima, ledger_append, ledger_read,
                   resolve_one, sha256_file, posix_rel)
from ._portalock import lock_exclusive, unlock
from . import publish

UPDATE_JOURNAL = LEDGER / "update.jsonl"     # 운영 저널 — 권위 대장이 아니다
CONFIG = ROOT / ".osk" / "config.json"       # 인스턴스 소유 로컬 설정
TXN_DIR = ROOT / ".osk" / "txn"              # 크래시-안전 트랜잭션 영역(비동기화)
TXN_MANIFEST = TXN_DIR / "manifest.json"     # 존재 = 미완료 트랜잭션(복구 대상)
TXN_BACKUP = TXN_DIR / "backup"
DEFAULT_UPSTREAM = "https://github.com/lpaiu-cs/osk-system.git"
ATTESTATION = "release.json"
ENGINE_PREFIX = "_governance/_engine/"
VERSION_RE = r"^v\d+\.\d+\.\d+$"             # 릴리스·태그·자동 탐색이 공유하는 계약

# 인스턴스 소유 바닥 — 릴리스·매니페스트가 무엇을 말하든 쓰지 않는다.
# (골격 .gitkeep은 디렉터리가 없을 때만 예외 — _skel에서 별도 처리)
FLOOR_HEADS = ("= Domain", "= Person", "= Scope",
               "_ledger", "_raw", "_sources", ".osk", ".git")


class UpdateError(RuntimeError):
    """갱신 중단 — 인스턴스에 아무것도 쓰지 않는다."""


def _floor(rel: str) -> bool:
    head = rel.split("/", 1)[0]
    if head in FLOOR_HEADS:
        return True
    return any(f"/{seg}/" in f"/{rel}" for seg in ("_ledger", "_raw"))


def _within(base: Path, rel: str) -> Path | None:
    """rel을 base 안으로 봉쇄한 **정규 절대 경로** — 아니면 None. release 증빙
    key와 (다기기 병합되는) 저널 path는 **신뢰 밖 입력**이므로, 어느 I/O 전에도
    이 봉쇄를 통과한다. 두 겹으로 막는다: ①`.`/`..` segment·절대경로를 문자열
    단계에서 거부(정규화 전 판정 우회 차단 — `docs/../= Scope/`로 바닥 재진입
    금지) ②남은 심볼릭 재배치는 realpath로 흡수해 base 안인지 확인. 반환값의
    base-상대(canonical)에만 floor·I/O를 걸어야 한다."""
    try:
        p = Path(rel)
        if not p.parts or p.is_absolute() \
                or any(seg in ("..", ".") for seg in p.parts):
            return None
        broot = Path(os.path.realpath(base))
        real = Path(os.path.realpath(base / p))
        if real == broot:
            return None                          # base 자신은 파일 대상이 아니다
        real.relative_to(broot)                  # 벗어나면 ValueError
        return real
    except (ValueError, OSError, TypeError):
        return None


def _canon_rel(base: Path, rel: str) -> str | None:
    """봉쇄된 canonical base-상대 경로(posix) — 탈출·재진입·**경로 정체성 훼손**은
    None. floor·I/O 판정은 raw 문자열이 아니라 이 canonical 경로에 건다. realpath가
    lexical 경로와 다르면(경로 구성요소에 symlink) 다른 프레임워크 파일로 write가
    재지정된 것이므로 거부한다 — symlink 탈출만이 아니라 ROOT **내부** alias도 막는다
    (예: `docs/SETUP.md -> _engine/osk/core.py`)."""
    p = _within(base, rel)
    if p is None:
        return None
    try:
        canon = p.relative_to(Path(os.path.realpath(base))).as_posix()
    except ValueError:
        return None
    if canon != Path(rel).as_posix():            # lexical ≠ realpath → symlink 재지정
        return None
    return canon


# SKEL이 파고들 수 없는 보호 구획 — 골격은 빈 Space 루트만 만든다.
SKEL_FORBIDDEN = ("_ledger", "_raw", "_sources", ".osk", ".git")


def _allowed_skel(s: str) -> Path | None:
    """SKEL이 만들어도 되는 절대 경로 — 아니면 None. 골격은 **루트 안으로
    봉쇄된 빈 Space 루트**만 만든다(Mechanism §1-2 5항의 바닥은 매니페스트가
    무엇을 말하든 지켜진다). `../` 탈출·보호 구획 파고들기·비Space 경로는 거부."""
    p = _within(ROOT, s)                         # `..`·절대경로·심볼릭 탈출은 None
    if p is None:
        return None
    try:
        parts = p.relative_to(Path(os.path.realpath(ROOT))).parts
    except ValueError:
        return None
    if not parts or not parts[0].startswith("= "):
        return None                              # Space 루트(`= `)만 골격 대상
    if any(seg in SKEL_FORBIDDEN for seg in parts):
        return None
    return p


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"로컬 설정 판독 실패 {CONFIG}: {e}")
    return {"upstream": {"source": "git", "url": DEFAULT_UPSTREAM, "pin": None}}


# ── 출처 — 전송만 다르고 검증은 같다 (Mechanism §1-2 3항) ────────────────

def latest_release_tag(url: str) -> str | None:
    """정본의 정식 릴리스 태그 중 최신 semver(`vX.Y.Z`) — 없으면 None.
    갱신의 기본은 브랜치 HEAD가 아니라 태그다(Mechanism §1-2 3항) — HEAD를
    받으면 릴리스 이후의 개발 커밋이 딸려 와 attestation과 어긋난다."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UpdateError(f"정본 태그 조회 실패({url}): {r.stderr.strip()[-200:]}")
    best = None
    for line in r.stdout.splitlines():
        name = line.rsplit("refs/tags/", 1)[-1].strip()
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", name)
        if m:
            key = tuple(int(g) for g in m.groups())
            if best is None or key > best[0]:
                best = (key, name)
    return best[1] if best else None


def tag_exists(url: str, tag: str) -> bool:
    """정본에 `refs/tags/<tag>`가 실제로 있는가 — 명시 ref가 브랜치로
    릴리스 경계를 우회하는 것을 막는다(`--branch`는 태그·브랜치 모두 받는다)."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url,
                        f"refs/tags/{tag}"], capture_output=True, text=True,
                       timeout=60)
    return r.returncode == 0 and bool(r.stdout.strip())


def fetch_git(url: str, tag: str, dest: Path) -> Path:
    """정본의 그 **태그가 가리키는 정확한 커밋**을 얕게 받는다. `clone --branch`는
    동명 브랜치(`refs/heads/<tag>`)를 대신 고를 수 있으므로 쓰지 않는다 —
    `refs/tags/<tag>`를 명시 fetch해 FETCH_HEAD(태그의 peeled 커밋)에 detached
    checkout한다. 이렇게 해야 태그 경계가 브랜치로 우회되지 않는다."""
    d = dest / "tree"
    def _g(*a, **k):
        r = subprocess.run(["git", *a], capture_output=True, text=True,
                           timeout=300, **k)
        if r.returncode != 0:
            raise UpdateError(f"정본 fetch 실패({url} {tag}): "
                              f"{r.stderr.strip()[-300:]}")
        return r
    _g("init", "-q", str(d))
    _g("-C", str(d), "fetch", "-q", "--depth", "1", url,
       f"refs/tags/{tag}")                        # FETCH_HEAD = 태그의 커밋
    _g("-C", str(d), "checkout", "-q", "--detach", "FETCH_HEAD")
    return d


def fetch_bundle(src: str, dest: Path) -> Path:
    """로컬 반입 — 디렉터리 또는 tar(.tar/.tar.gz/.tgz). **snapshot**을 만든다:
    디렉터리 bundle을 원본 그대로 반환하면 검증 후 write 때까지 같은 파일을 다시
    읽어 TOCTOU가 열린다(검증 직후 원본이 바뀌면 미검증 bytes 적용). tar처럼
    temp tree로 복사해, 이후 검증·plan·write가 전부 이 snapshot만 보게 한다."""
    p = Path(src).expanduser()
    if p.is_dir():
        out = dest / "tree"
        shutil.copytree(p, out, symlinks=True,   # symlink는 보존 → _within이 거른다
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return out
    if p.is_file() and p.suffix in (".tar", ".gz", ".tgz"):
        out = dest / "tree"
        out.mkdir()
        with tarfile.open(p) as tf:
            tf.extractall(out, filter="data")   # 경로 탈출 차단
        inner = [d for d in out.iterdir() if d.is_dir()]
        return inner[0] if len(inner) == 1 and not any(
            f.is_file() for f in out.iterdir()) else out
    raise UpdateError(f"bundle이 아니다(디렉터리·tar만): {src}")


# ── 비준증빙 대조 ────────────────────────────────────────────────────────

def load_release(tree: Path) -> dict:
    ap = tree / ATTESTATION
    if not ap.exists():
        raise UpdateError("정본 릴리스가 아니다 — 비준증빙(release.json)이 없다"
                          " (시행령 §10 6항)")
    try:
        rel = json.loads(ap.read_text(encoding="utf-8"))
    except ValueError as e:
        raise UpdateError(f"비준증빙 판독 실패: {e}")
    if not isinstance(rel.get("files"), dict) or not rel.get("version"):
        raise UpdateError("비준증빙 형식 위반 — version·files가 필요하다")
    if not re.match(VERSION_RE, str(rel["version"])):
        raise UpdateError(f"비준증빙 version 형식 위반(vX.Y.Z): {rel['version']}")
    return rel


def verify_attestation(tree: Path, rel: dict) -> list[str]:
    """전수 대조 — 증빙의 파일이 다 있고 해시가 맞다. 적용은 오직 증빙이
    모는 파일(`rel["files"]`)만 하고 그 하나하나를 해시로 검증하므로, 증빙
    밖에 있는 디스크 파일은 적용 자체가 되지 않는다 — 트리에 딸린 미추적
    부산물(pyc·.DS_Store)로 갱신을 막지 않는다(그건 안전이 아니라 오탐이다).

    증빙 key는 신뢰 밖 입력이다 — I/O 전에 tree 안으로 봉쇄한다(경로 탈출 차단)."""
    errs = []
    for p, h in sorted(rel["files"].items()):
        f = _within(tree, p)
        if f is None:
            errs.append(f"증빙 경로가 트리 밖으로 벗어난다(경로 봉쇄 실패): {p}")
        elif not f.is_file():
            errs.append(f"증빙의 파일이 없다: {p}")
        elif sha256_file(f) != h:
            errs.append(f"해시 불일치: {p}")
    return errs


# ── 적용 집합 — 릴리스 안의 발행 매니페스트가 정한다 ─────────────────────

def _map_dest(ap: str, man: dict) -> str | None:
    """증빙 경로(정본 source)를 발행 매니페스트 MAP으로 인스턴스 경로에 사상한다
    — publish.collect과 같은 `src/a -> dst/a` 사상이다(dst-prefix 매칭이 아니라).
    비항등 MAP도 올바로 반영하고, 어느 MAP에도 안 걸리면 None(매니페스트 밖)."""
    for s, d in man["map"]:
        if not s.endswith("/"):
            if ap == s:
                return d
        elif ap.startswith(s.rstrip("/") + "/"):
            return d.rstrip("/") + "/" + ap[len(s.rstrip("/")) + 1:]
    return None


def apply_set(tree: Path, rel: dict):
    """((source, dest) 사상 목록, 골격 절대경로 목록, 건너뜀 보고).
    KEEP은 정본 저장소 전용이라 제외. dest는 floor·루트 봉쇄를 통과한 것만."""
    man_rel = "_governance/_engine/scripts/publish-manifest.txt"
    # 매니페스트는 적용 범위를 정하는 **control plane**이다. 증빙 밖 파일은
    # 적용 대상이 아니지만(허용), 그런 파일이 증빙된 bytes의 목적지·정책을
    # 지배해서는 안 된다 — 반드시 증빙에 있고 이미 해시 검증된 것이어야 한다.
    if man_rel not in rel["files"]:
        raise UpdateError(
            f"발행 매니페스트가 비준증빙에 없다 — 적용 범위를 신뢰할 수 없다: "
            f"{man_rel}")
    man_path = tree / man_rel
    if not man_path.exists():
        raise UpdateError("릴리스에 발행 매니페스트가 없다 — 적용 범위 불명")
    man = publish.parse_manifest(man_path)
    keep = set(man["keep"])
    targets, skipped = [], []
    for ap in sorted(rel["files"]):
        if ap in keep or ap == ATTESTATION:
            skipped.append(f"{ap} (정본 저장소 전용)")
            continue
        if publish._denied(ap, man["deny"]):
            skipped.append(f"{ap} (DENY)")
            continue
        dest = _map_dest(ap, man)
        if dest is None:
            skipped.append(f"{ap} (매니페스트 밖)")
            continue
        # dest는 신뢰 밖 입력에서 파생된다 — canonical 경로로 봉쇄한 뒤 그
        # canonical에 floor를 건다(정규화 전 문자열 판정 우회 차단, P1).
        cdest = _canon_rel(ROOT, dest)
        if cdest is None or _floor(cdest):
            skipped.append(f"{dest} (바닥·경로 봉쇄 — 쓰지 않는다)")
            continue
        targets.append((ap, cdest))
    # SKEL은 허용 골격(루트 봉쇄된 빈 Space 루트)만 통과시킨다
    skel = []
    for s in man["skel"]:
        d = _allowed_skel(s.rstrip("/"))
        if d is None:
            skipped.append(f"{s} (SKEL 허용 밖 — 쓰지 않는다)")
        else:
            skel.append(d)
    return targets, skel, skipped


# ── 저널 — 직전 적용 상태의 정본 ─────────────────────────────────────────

def _done_txns(recs: list[dict]) -> set:
    return {r.get("txn") for r in recs
            if r.get("kind") == "done" and r.get("txn")}


def _is_committed(r: dict, done: set) -> bool:
    """`done`이 그 트랜잭션의 커밋 지점이다(Mechanism §1-2 7항). 커밋 전에 죽어
    남은 `apply`/`remove`는 파일이 pre-image로 복구되므로 상태의 후보가 아니다.
    `txn`이 없는 유산 기록은 그대로 후보다(호환 경계)."""
    return not r.get("txn") or r["txn"] in done


def committed(recs: list[dict]) -> list[dict]:
    """커밋된 기록만 남긴 목록 — **존재 검사 전용**(has_history). 인과 판정에는
    쓰지 않는다: 목록에서 빼면 그 기록이 잇던 parents 간선도 끊겨 남은 기록들이
    거짓 분기가 된다. 판정은 `causal_maxima(..., candidate=...)`로 후보만
    제한한다."""
    done = _done_txns(recs)
    return [r for r in recs if _is_committed(r, done)]


def _committed_maximum(recs: list[dict], rel_path: str) -> dict | None:
    """그 경로의 **커밋된 기록 중 인과 극대** — 유일할 때만 돌려준다.
    조상 관계는 전체 저널의 DAG로 계산한다(미커밋 기록이 사슬을 끊지 않게)."""
    done = _done_txns(recs)
    m = causal_maxima(recs, rel_path, None, "path",
                      candidate=lambda r: _is_committed(r, done))
    return m[0] if len(m) == 1 else None


def last_applied_hash(recs: list[dict], rel_path: str) -> str | None:
    r = _committed_maximum(recs, rel_path)
    return r.get("hash") if r and r.get("kind") == "apply" else None


def managed_paths(recs: list[dict]) -> dict[str, str]:
    """지금 이 인스턴스가 정본으로부터 관리 중인 경로 → 기준선 해시.
    경로별(커밋된) 인과 극대가 `apply`면 관리 중, `remove`면 삭제됨
    (last_applied_hash와 같은 규율). 삭제 전파의 '직전 관리 집합'이 여기서 나온다."""
    out = {}
    for p in {r.get("path") for r in recs if r.get("path")}:
        r = _committed_maximum(recs, p)
        if r and r.get("kind") == "apply":
            out[p] = r.get("hash")
    return out


def current_version(recs: list[dict] | None = None) -> str | None:
    """현재 판본 — `done` 기록의 **인과 극대**(물리 마지막 행이 아니다).
    update.jsonl은 union 병합되는 대장이므로 파일 순서는 정본이 아니다
    (core 판정 계약 · sibling `last_applied_hash`와 같은 규율). 극대가 여럿
    (다기기 동시 갱신)이면 미확정으로 None."""
    recs = ledger_read(UPDATE_JOURNAL) if recs is None else recs
    maxima = causal_maxima(recs, "done", field="kind")
    return maxima[0].get("version") if len(maxima) == 1 else None


def has_history(recs: list[dict] | None = None) -> bool:
    """이 인스턴스가 이미 갱신 관리 이력을 가졌는가 — **커밋된** apply·remove·
    done 중 하나라도 있으면 참. adopt(최초 편입)의 허용 판정에 쓴다. 커밋 전에
    죽어 남은 기록으로 adopt가 막히면 복구가 불가능해지므로 committed로 본다."""
    recs = committed(ledger_read(UPDATE_JOURNAL) if recs is None else recs)
    return any(r.get("kind") in ("apply", "remove", "done") for r in recs)


# ── 크래시-안전 트랜잭션 (Mechanism §1-2 7항) ────────────────────────────
#
# 프로토콜 — 파일과 저널을 **하나의 txn id**로 묶는다:
#   ①pre-image 백업 fsync → ②manifest(txn) 원자 기록·fsync  ← 트랜잭션 시작
#   ③저널 begin(txn) → ④파일 조작 → ⑤저널 apply/remove(txn) → ⑥저널 done(txn)
#   ⑦manifest 제거                                            ← 트랜잭션 종료
# 복구 판정은 결정적이다: manifest의 txn이 저널에 `done`이면 **roll-forward**
# (파일은 이미 새 판이므로 manifest만 정리), 없으면 **rollback**(pre-image 복구).
# 커밋 전 apply/remove는 `committed()`가 판정에서 가린다 — 파일과 저널이 어긋나지
# 않는 이유다. 복구 실패는 fail-closed로 백업을 보존한 채 중단한다.

def _fsync_dir(d: Path) -> None:
    """디렉터리 엔트리를 내구화 — 파일만 fsync하면 rename·create·unlink가 유실될
    수 있다. 전원 차단까지 계약하므로 실패를 삼키지 않는다: 디렉터리 fsync 개념이
    없는 파일시스템(EINVAL·ENOTSUP)만 예외로 넘기고, 그 밖의 오류는 올려
    트랜잭션이 durability 없이 성공한 척하지 못하게 한다."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except FileNotFoundError:
        return                      # 이미 사라진 디렉터리 — 내구화할 대상이 없다
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOTSUP, errno.EACCES, errno.EPERM):
            return
        raise
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)


def _mkdirs_durable(d: Path) -> list[Path]:
    """`d`까지의 없는 조상을 만들고, **만든 각 디렉터리의 부모를 fsync**한다.
    `mkdir(parents=True)` 뒤 자신만 fsync하면 그 엔트리를 소유한 부모가 내구화되지
    않아 전원 차단 시 디렉터리째 유실된다(그 안의 파일은 done 이후에도 사라진다).
    반환: 새로 만든 디렉터리(깊은 순) — 트랜잭션 rollback이 되돌릴 대상이다."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    created = []
    for q in reversed(missing):     # 얕은 곳부터 만든다
        q.mkdir(exist_ok=True)
        created.append(q)
        _fsync_dir(q.parent)        # 그 엔트리를 소유한 부모를 내구화
    created.reverse()
    return created


def _planned_dirs(rels: list[str]) -> list[str]:
    """이 트랜잭션이 새로 만들 수 있는 디렉터리(canonical 상대, 얕은 순).
    manifest에 미리 담아 두면 rollback이 **비어 있는 것만** 되돌릴 수 있다 —
    SKEL은 디렉터리 존재 자체가 적용 결과이므로 잔재로 남겨서는 안 된다."""
    out: set[str] = set()
    root_real = Path(os.path.realpath(ROOT))
    for rel in rels:
        p = (ROOT / rel).parent
        while True:
            try:
                cp = p.relative_to(ROOT)
            except ValueError:
                break
            if not cp.parts or p.exists() or Path(os.path.realpath(p)) == root_real:
                break
            out.add(cp.as_posix())
            p = p.parent
    return sorted(out, key=lambda s: (s.count("/"), s))


def _txn_clear() -> None:
    """트랜잭션 영역을 지운다. 정리도 상태 전이의 일부이므로 **성공을 확인**한다 —
    표식이 남으면 데몬이 모든 tick을 거부하고 다음 갱신이 남은 백업과 충돌한다."""
    if TXN_DIR.exists():
        shutil.rmtree(TXN_DIR, ignore_errors=True)
    if TXN_DIR.exists() or TXN_MANIFEST.exists():
        raise UpdateError(
            f"트랜잭션 영역 정리 실패 — 수동 개입 필요(권한·파일시스템 확인): "
            f"{TXN_DIR}")
    _fsync_dir(TXN_DIR.parent if TXN_DIR.parent.exists() else ROOT)


def _txn_begin(txn: str, version: str, touch: list[str]) -> None:
    """건드릴 파일(canonical ROOT-상대 경로)의 pre-image를 영속 백업·fsync한 뒤
    manifest를 원자 기록한다. manifest 기록이 트랜잭션 시작의 커밋 지점이므로,
    그 전에 죽으면 target은 아직 손대지 않은 상태다. 경로는 절대경로로 굳히지
    않는다 — 복구 시 다시 봉쇄·정체성 검증하기 위해 canonical 상대로 남긴다."""
    _txn_clear()
    # `.osk/`까지 처음 만들 수 있다 — 만든 각 디렉터리의 부모(최상단은 ROOT)를
    # 내구화해야 target은 남고 복구 표식만 유실되는 창이 닫힌다.
    _mkdirs_durable(TXN_BACKUP)
    entries = []
    for i, rel in enumerate(touch):
        key = f"{i:06d}"
        p = ROOT / rel
        existed = p.is_file()
        h = None
        if existed:
            data = p.read_bytes()
            bp = TXN_BACKUP / key
            _write_atomic(bp, data)              # 백업 자체도 fsync된다
            h = sha256_file(bp)
        entries.append({"rel": rel, "backup": key, "existed": existed,
                        "hash": h})
    _fsync_dir(TXN_BACKUP)
    _write_atomic(TXN_MANIFEST, json.dumps(
        {"txn": txn, "version": version, "entries": entries,
         "dirs": _planned_dirs(touch)},           # rollback이 되돌릴 새 디렉터리
        ensure_ascii=False).encode())
    _fsync_dir(TXN_DIR)


def _txn_pending() -> dict | None:
    """미완료(또는 미정리) 트랜잭션 manifest — 없으면 None. 판독 실패는 손상이며
    **지우지 않는다**(유일한 복구 자료다) — 호출부가 fail-closed로 중단한다."""
    if not TXN_MANIFEST.is_file():
        return None
    try:
        man = json.loads(TXN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UpdateError(
            f"트랜잭션 manifest 손상 — 자동 복구 불가, 수동 개입 필요: {e} "
            f"(백업은 {TXN_DIR} 에 보존했다)")
    if not isinstance(man.get("entries"), list) or not man.get("txn"):
        raise UpdateError(
            f"트랜잭션 manifest 형식 위반 — 수동 개입 필요 (보존: {TXN_DIR})")
    return man


def _txn_recover(recs: list[dict]) -> str | None:
    """미완료 트랜잭션을 결정적으로 처리한다 — 'rollback'|'roll-forward'|None.

    저널에 그 txn의 `done`이 있으면 커밋된 것이므로 파일은 새 판이 정답이고
    manifest만 정리한다(roll-forward). 없으면 pre-image로 되돌린다(rollback).
    복구 대상 경로는 다시 봉쇄·정체성 검증하며, 하나라도 되돌리지 못하면
    백업을 보존한 채 중단한다(fail-closed)."""
    man = _txn_pending()
    if man is None:
        return None
    txn = man["txn"]
    if any(r.get("kind") == "done" and r.get("txn") == txn for r in recs):
        _txn_clear()                             # 커밋됨 — 파일은 그대로 둔다
        return "roll-forward"
    for e in man["entries"]:
        rel = e.get("rel")
        cp = _canon_rel(ROOT, str(rel)) if rel else None
        if cp is None:
            raise UpdateError(
                f"복구 경로가 봉쇄·정체성 검증에 실패했다 — 중단(보존: {TXN_DIR}): {rel}")
        p = ROOT / cp
        try:
            if e.get("existed"):
                bp = TXN_BACKUP / str(e.get("backup"))
                if not bp.is_file():
                    raise UpdateError(
                        f"백업이 없다 — 복구 불가, 수동 개입 필요: {rel}")
                if e.get("hash") and sha256_file(bp) != e["hash"]:
                    raise UpdateError(
                        f"백업이 손상됐다 — 복구 불가, 수동 개입 필요: {rel}")
                _write_atomic(p, bp.read_bytes())
            else:
                p.unlink(missing_ok=True)
                _fsync_dir(p.parent)
        except OSError as ex:
            raise UpdateError(
                f"복구 실패 — 중단(백업 보존: {TXN_DIR}): {rel} {ex}")
    # 파일을 되돌린 뒤, 이 트랜잭션이 만든 디렉터리 중 **비어 있는 것만** 깊은
    # 순서로 제거한다 — SKEL은 디렉터리 존재 자체가 적용 결과라 잔재로 남으면
    # 'pre-image로 복구'가 성립하지 않는다. 비어 있지 않으면(사용자 파일 등) 둔다.
    for rel in sorted(man.get("dirs") or [], key=lambda s: -s.count("/")):
        cp = _canon_rel(ROOT, str(rel))
        if cp is None:
            raise UpdateError(                   # 경로 정체성이 바뀌었다 — 중단
                f"복구 대상 디렉터리가 봉쇄·정체성 검증에 실패했다 "
                f"— 중단(보존: {TXN_DIR}): {rel}")
        d = ROOT / cp
        try:
            d.rmdir()
        except OSError as ex:
            # 허용하는 것은 둘뿐이다: 이미 없다(ENOENT), 사용자 파일이 생겨
            # 비어 있지 않다(ENOTEMPTY/EEXIST). 권한·I/O 오류는 복구 실패이므로
            # 파일 복구와 같은 규율로 fail-closed한다.
            if ex.errno in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                continue
            raise UpdateError(
                f"디렉터리 복구 실패 — 중단(백업 보존: {TXN_DIR}): {cp} {ex}")
        _fsync_dir(d.parent)
    _txn_clear()
    return "rollback"


@contextmanager
def _exclusive(path: Path, busy: str):
    """비차단 배타 잠금 — 경합이면 `busy` 사유로 중단한다. **획득한 경우에만**
    해제한다(소유하지 않은 구간의 unlock은 Windows에서 실제 해제 연산이라
    원래 결과를 덮는 오류를 낸다)."""
    f = open(path, "w")
    ok = False
    try:
        try:
            lock_exclusive(f, blocking=False)
            ok = True
        except OSError:
            raise UpdateError(busy)
        yield
    finally:
        if ok:
            unlock(f)
        f.close()


def _sync_lock_path() -> Path:
    """sync 데몬과 **공유하는 mutation 잠금** — 데몬 싱글턴 잠금이 아니다.
    데몬의 once()가 working-tree를 건드리는 구간에 이 잠금을 잡으므로, update가
    이걸 잡으면 데몬이 그 사이 혼합 상태를 커밋·push하지 못하고 tick을 건너뛴다."""
    from sync_daemon import _lock_path
    return _lock_path(ROOT, "osk-mutation.lock")


# ── 계획과 적용 ──────────────────────────────────────────────────────────

def plan(tree: Path, targets: list, adopt: bool) -> dict:
    """targets는 (source, dest) 사상. add/update/rebaseline/conflict는 파일을
    읽어야 하므로 (source, dest)를, engine_drift/remove/remove_conflict는 보고·
    삭제만 하므로 dest 문자열을 담는다."""
    recs = ledger_read(UPDATE_JOURNAL)
    add, same, rebaseline = [], [], []
    update, conflict, engine_drift = [], [], []
    dests = set()
    for src, dest in targets:
        dests.add(dest)
        s, d = tree / src, ROOT / dest
        if not d.exists():
            add.append((src, dest))
            continue
        if d.read_bytes() == s.read_bytes():
            # 내용은 upstream과 같다 — 기준선이 없거나(adopt·수동 동기화) 낡았으면
            # 저널만 갱신한다. 안 하면 다음 릴리스에서 이 파일이 drift로 오판된다.
            if last_applied_hash(recs, dest) != sha256_file(d):
                rebaseline.append((src, dest))
            else:
                same.append(dest)
            continue
        base = last_applied_hash(recs, dest)
        drifted = base is None or sha256_file(d) != base
        if drifted and not adopt:
            if dest.startswith(ENGINE_PREFIX):
                engine_drift.append(dest)
            else:
                conflict.append((src, dest))
        else:
            update.append((src, dest))
    # 삭제 전파 — 직전까지 관리하던 파일이 새 릴리스에서 빠졌으면 인스턴스에서도
    # 제거한다(안 하면 하류가 정본과 다른 프레임워크를 실행한다). 로컬 수정이
    # 있는 삭제 대상은 지우지 않고 보존·보고한다. 바닥은 애초에 관리 대상이 아니다.
    remove, remove_conflict = [], []
    for p, h in sorted(managed_paths(recs).items()):
        cp = _canon_rel(ROOT, p)                 # 병합된 저널 path도 봉쇄(P1)
        if cp is None or cp in dests or _floor(cp):
            continue
        rp = ROOT / cp
        if not rp.exists():
            continue
        (remove if sha256_file(rp) == h else remove_conflict).append(cp)
    return {"add": add, "same": len(same), "rebaseline": rebaseline,
            "update": update, "conflict": conflict, "engine_drift": engine_drift,
            "remove": remove, "remove_conflict": remove_conflict}


def _sidecar_plan(p: dict, tree: Path, version: str, dests: set) -> tuple:
    """conflict 사이드카를 (쓸 것, 이미 같아 인정할 것, 손대지 않을 것)으로 가른다.

    사이드카는 사용자가 **수동 병합에 쓰는 작업 파일**이다. 같은 버전을 다시
    적용할 때 무조건 덮으면 그 작업이 영구히 사라진다(성공 후 pre-image도
    지워지므로 복구 경로가 없다). 그래서 내용이 incoming과 같을 때만 그대로
    인정하고, 다르면 **덮지 않고 보고**한다.

    `dests`에는 이번에 **적용할 경로와 삭제할 경로를 모두** 넘긴다 — 삭제 예정
    경로와 사이드카 이름이 겹치면 '보존·인정'으로 보고한 파일이 뒤의 삭제 단계에서
    사라져 보고가 거짓이 된다. 겹치면 갱신을 중단한다."""
    write, kept, held, collide = [], [], [], []
    for src, dest in p["conflict"]:
        side = dest + f".upstream-{version}"
        if side in dests:                        # 관리·삭제 예정 경로와 충돌
            collide.append(side)
            continue
        sp = ROOT / side
        if not sp.exists():
            write.append((src, side))
        elif sp.read_bytes() == (tree / src).read_bytes():
            kept.append(side)                    # 이미 같다 — 그대로 인정
        else:
            held.append(side)                    # 사용자 작업 — 손대지 않는다
    return write, kept, held, collide


def _write_atomic(dst: Path, data: bytes) -> None:
    _mkdirs_durable(dst.parent)     # 새 조상 각각의 부모까지 내구화
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)          # rename 자체의 내구성 (전원 차단 대비)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(source: str | None = None, ref: str | None = None,
        bundle: str | None = None, apply: bool = False,
        adopt: bool = False) -> dict:
    """기본은 보고. `apply`면 **가장 먼저** mutation 잠금을 잡고 미완료 트랜잭션을
    복구한 뒤에야 상태를 판정한다 — 복구가 판정 뒤에 오면 half-applied 상태가
    drift·adopt 거부로 오판되어 복구에 도달하지 못한다(Mechanism §1-2 7항)."""
    if not apply:
        return _run_locked(source, ref, bundle, False, adopt)
    # 잠금 순서는 데몬과 같다: **싱글턴 → mutation**. 싱글턴(osk-sync.lock)은
    # probe로 잠깐 잡았다 놓으면 그 틈에 구버전 데몬이 떠 버리므로(TOCTOU),
    # 갱신 **수명 내내 보유**한다 — 구·신 데몬 모두 이 잠금을 먼저 잡기 때문에
    # 새 mutation 규약을 모르는 구 데몬의 동시 실행까지 함께 막힌다.
    from sync_daemon import _lock_path as _dlp
    with _exclusive(_dlp(ROOT, "osk-sync.lock"),
                    "동기화 데몬이 실행 중이다 — 갱신 전에 데몬을 멈춘다"
                    "(구버전 데몬은 갱신의 잠금 규약을 모른다)"), \
            _exclusive(_sync_lock_path(),
                       "다른 갱신이 vault를 잠갔다 — 잠시 후 다시 실행한다"):
        # 복구는 어떤 상태 판정보다 먼저다(잠금 안에서).
        recovered = _txn_recover(ledger_read(UPDATE_JOURNAL))
        if recovered == "rollback":
            ledger_append(UPDATE_JOURNAL,
                          {"kind": "rollback", "why": "미완료 트랜잭션 크래시 복구"})
        rep = _run_locked(source, ref, bundle, True, adopt)
        if recovered:
            rep["recovered"] = recovered
        return rep


def _run_locked(source: str | None, ref: str | None, bundle: str | None,
                apply: bool, adopt: bool) -> dict:
    cfg = load_config().get("upstream", {})
    source = source or ("bundle" if bundle else cfg.get("source", "git"))
    self_tag = None
    # adopt는 **최초 편입** 전용이다 — 이미 관리 이력이 있으면 거부한다. 안 그러면
    # 정상 관리 인스턴스에서 로컬 수정(엔진 포함)을 아무 때나 덮는 force가 된다.
    if adopt and has_history():
        raise UpdateError(
            "이미 갱신 관리 이력이 있다 — adopt는 최초 편입에만 허용된다. "
            "로컬 수정은 정본에서 고치거나 로컬 사본을 정리한 뒤 갱신한다")
    with tempfile.TemporaryDirectory() as td:
        if source == "git":
            url = cfg.get("url", DEFAULT_UPSTREAM)
            pin = ref or cfg.get("pin")
            if pin is not None:
                # 명시 ref/pin도 정식 릴리스 태그여야 한다 — 브랜치(`--to main`)로
                # 태그 경계를 우회하지 못하게, 형식·실재를 함께 확인한다(§1-2 3항).
                if not re.match(VERSION_RE, str(pin)):
                    raise UpdateError(f"명시 ref/pin은 vX.Y.Z 태그여야 한다: {pin}")
                if not tag_exists(url, pin):
                    raise UpdateError(f"정본에 그 릴리스 태그가 없다: {pin}")
                tag = pin
            else:
                tag = latest_release_tag(url)   # 기본은 최신 정식 태그(HEAD 아님)
                if tag is None:
                    raise UpdateError(
                        "정본에 정식 릴리스 태그(vX.Y.Z)가 없다 — 정본에서 "
                        "먼저 릴리스하거나 --to로 참조를 지정한다")
            tree = fetch_git(url, tag, Path(td))
            self_tag = tag                       # 증빙 version이 이 태그와 같아야 한다
        elif source == "bundle":
            if not bundle:
                raise UpdateError("bundle 출처에는 --from <경로>가 필요하다")
            tree = fetch_bundle(bundle, Path(td))
        else:
            raise UpdateError(f"미정의 출처: {source} (git·bundle)")

        rel = load_release(tree)
        if self_tag is not None and rel["version"] != self_tag:
            raise UpdateError(
                f"요청 태그와 증빙 version이 다르다 — 태그 {self_tag} 의 "
                f"release.json은 {rel['version']} 이다(태그 위조·오지정 차단)")
        errs = verify_attestation(tree, rel)
        if errs:
            raise UpdateError("비준증빙 대조 실패 — 아무것도 쓰지 않았다:\n  "
                              + "\n  ".join(errs[:10]))
        targets, skel, skipped = apply_set(tree, rel)
        p = plan(tree, targets, adopt)
        dests = lambda pairs: [d for _s, d in pairs]
        out = {"ok": True, "applied": False, "version": rel["version"],
               "current": current_version(), "files": len(targets),
               "add": dests(p["add"]), "update": dests(p["update"]),
               "same": p["same"], "rebaseline": dests(p["rebaseline"]),
               "conflict": dests(p["conflict"]), "engine_drift": p["engine_drift"],
               "remove": p["remove"], "remove_conflict": p["remove_conflict"],
               "skipped": len(skipped)}
        if p["engine_drift"]:
            raise UpdateError(
                "엔진 파일에 로컬 수정이 있다 — 갱신 전체를 중단한다. 엔진을 "
                "고치는 자리는 정본이다(Mechanism §1-2 6항). 기존 인스턴스의 "
                "최초 편입이면 --adopt로 현재 릴리스를 기준선 삼는다:\n  "
                + "\n  ".join(p["engine_drift"][:10]))
        if not apply:
            if TXN_MANIFEST.is_file():
                out["pending_txn"] = True        # 미완료 트랜잭션 — --apply로 복구
            return out

        # (잠금·미완료 트랜잭션 복구는 run()이 이미 끝냈다 — 여기는 잠금 안이다.)
        v = rel["version"]
        # 사이드카는 사용자의 수동 병합 작업 파일이다 — 덮을 것/인정할 것/보존할
        # 것을 먼저 가른다. 관리 파일과 경로가 겹치면 중단한다.
        side_write, side_kept, side_held, side_collide = _sidecar_plan(
            p, tree, v, {d for _s, d in targets} | set(p["remove"]))
        out["sidecar_held"] = side_held
        if side_collide:
            raise UpdateError(
                "사이드카 경로가 정식 관리 파일과 겹친다 — 갱신을 중단한다"
                "(관리 파일을 되덮게 된다):\n  " + "\n  ".join(side_collide[:10]))
        txn = os.urandom(8).hex()                # 파일과 저널을 묶는 트랜잭션 id
        # 이번에 건드릴 파일(canonical 상대)의 pre-image를 영속 백업하고 manifest를
        # 기록한 **뒤에만** target을 건드린다. 이후 크래시는 다음 실행이 판정한다:
        # done(txn)이 있으면 roll-forward, 없으면 rollback.
        touch = [dest for _s, dest in p["add"] + p["update"]]
        touch += list(p["remove"])
        touch += [side for _s, side in side_write]
        touch += [posix_rel(d / ".gitkeep", ROOT) for d in skel if not d.exists()]
        _txn_begin(txn, v, touch)
        ledger_append(UPDATE_JOURNAL, {"kind": "begin", "txn": txn,
                                       "version": v, "adopt": bool(adopt)})
        applied, removed, sidecars, made_skel = [], [], [], []
        try:
            for src, dest in p["add"] + p["update"]:
                _write_atomic(ROOT / dest, (tree / src).read_bytes())
                applied.append(dest)
            for path in p["remove"]:              # path는 이미 canonical(P1)
                tp = ROOT / path
                tp.unlink(missing_ok=True)
                _fsync_dir(tp.parent)             # 삭제 엔트리 내구화(전원 차단)
                removed.append(path)
            for src, side in side_write:          # 없던 사이드카만 쓴다
                _write_atomic(ROOT / side, (tree / src).read_bytes())
                sidecars.append(side)
            sidecars += side_kept                 # 이미 같은 것은 그대로 인정
            for d in skel:                        # 이미 _allowed_skel로 봉쇄·검증됨
                if not d.exists():
                    _mkdirs_durable(d)            # 만든 각 조상의 부모까지 내구화
                    _write_atomic(d / ".gitkeep", b"")
                    made_skel.append(posix_rel(d, ROOT))
        except OSError as e:
            _txn_recover(ledger_read(UPDATE_JOURNAL))   # done(txn) 없음 → rollback
            ledger_append(UPDATE_JOURNAL,
                          {"kind": "rollback", "txn": txn, "version": v,
                           "why": str(e)[:200]})
            raise UpdateError(f"갱신 적용 중 실패 — 원상복구했다: {e}")

        # 파일 조작 성공 후에 적용 상태를 저널에 남긴다. 이 기록들은 마지막
        # `done(txn)`이 append될 때까지 `committed()`가 판정에서 가린다 —
        # 중간에 죽어도 파일(pre-image 복구)과 판정이 어긋나지 않는다.
        for dest in applied:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "txn": txn, "version": v, "path": dest,
                "hash": sha256_file(ROOT / dest)})
        for _src, dest in p["rebaseline"]:        # 내용 동일·기준선만 갱신
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "txn": txn, "version": v, "path": dest,
                "hash": sha256_file(ROOT / dest)})
        for path in removed:
            ledger_append(UPDATE_JOURNAL, {"kind": "remove", "txn": txn,
                                           "version": v, "path": path})
        # skip은 conflict **사건**이지 적용 상태 변경이 아니다 — `skipped_path`로
        # 남겨 baseline/관리 판정(`path` 키)이 이를 보지 않게 한다(P2).
        for _src, dest in p["conflict"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "txn": txn, "version": v, "skipped_path": dest,
                "why": "로컬 수정 — upstream 사본을 옆에 두었다"})
        for path in p["remove_conflict"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "txn": txn, "version": v, "skipped_path": path,
                "why": "upstream 삭제됐으나 로컬 수정 — 보존한다"})
        ledger_append(UPDATE_JOURNAL, {
            "kind": "done", "txn": txn, "version": v, "applied": len(applied),
            "removed": len(removed), "conflicts": len(sidecars)})
        _txn_clear()                              # 커밋 완료 — 트랜잭션 영역 정리
        out.update(applied=True, applied_files=len(applied),
                   removed=removed, sidecars=sidecars, skel_created=made_skel,
                   note="엔진이 갱신되었으면 실행 중인 서버·데몬을 재시작한다")
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="osk-update",
        description="정본 릴리스로 갱신 — 기본은 보고, 쓰기는 --apply")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", help="버전 태그 pin (기본: 설정의 pin 또는 최신 릴리스 태그)")
    ap.add_argument("--from", dest="bundle", help="로컬 bundle 경로 (오프라인)")
    ap.add_argument("--source", choices=("git", "bundle"))
    ap.add_argument("--adopt", action="store_true",
                    help="기존 인스턴스의 최초 편입 — 현재 릴리스를 기준선 삼는다")
    a = ap.parse_args(argv)
    try:
        rep = run(a.source, a.to, a.bundle, a.apply, a.adopt)
    except UpdateError as e:
        sys.exit(f"[중단] {e}")
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
