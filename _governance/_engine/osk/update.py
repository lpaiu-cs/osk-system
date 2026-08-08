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
import argparse, json, os, re, subprocess, sys, tarfile, tempfile
from pathlib import Path

from .core import (ROOT, LEDGER, causal_maxima, ledger_append, ledger_read,
                   resolve_one, sha256_file, posix_rel)
from . import publish

UPDATE_JOURNAL = LEDGER / "update.jsonl"     # 운영 저널 — 권위 대장이 아니다
CONFIG = ROOT / ".osk" / "config.json"       # 인스턴스 소유 로컬 설정
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
    """로컬 반입 — 디렉터리 또는 tar(.tar/.tar.gz/.tgz)."""
    p = Path(src).expanduser()
    if p.is_dir():
        return p
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
    man_path = tree / "_governance/_engine/scripts/publish-manifest.txt"
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

def last_applied_hash(recs: list[dict], rel_path: str) -> str | None:
    r = resolve_one(recs, rel_path, "path")
    return r.get("hash") if r and r.get("kind") == "apply" else None


def managed_paths(recs: list[dict]) -> dict[str, str]:
    """지금 이 인스턴스가 정본으로부터 관리 중인 경로 → 기준선 해시.
    경로별 인과 극대가 `apply`면 관리 중, `remove`면 삭제됨(last_applied_hash와
    같은 규율). 삭제 전파의 '직전 관리 집합'이 여기서 나온다."""
    out = {}
    for p in {r.get("path") for r in recs if r.get("path")}:
        r = resolve_one(recs, p, "path")
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


def run(source: str | None = None, ref: str | None = None,
        bundle: str | None = None, apply: bool = False,
        adopt: bool = False) -> dict:
    cfg = load_config().get("upstream", {})
    source = source or ("bundle" if bundle else cfg.get("source", "git"))
    self_tag = None
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
            return out

        v = rel["version"]
        # 적용은 하나의 트랜잭션이다 — 파일 조작을 먼저 전부(백업 뜨며) 끝낸
        # 뒤에만 apply/remove 저널을 남긴다. 도중 OSError면 백업으로 되돌려
        # 혼합 상태(프레임워크 절반만 새 판)를 남기지 않는다(P1). 저널을
        # 파일 조작 뒤로 미루므로, 실패 시 append-only 대장에 잘못된 baseline이
        # 박히지 않는다. begin/done은 크래시 감지의 경계다.
        ledger_append(UPDATE_JOURNAL,
                      {"kind": "begin", "version": v, "adopt": bool(adopt)})
        backup: dict = {}                        # abs path -> bytes | None(부재)

        def _stage(path: Path):
            if path not in backup:
                backup[path] = path.read_bytes() if path.is_file() else None

        applied, removed, sidecars, made_skel = [], [], [], []
        try:
            for src, dest in p["add"] + p["update"]:
                dp = ROOT / dest
                _stage(dp)
                _write_atomic(dp, (tree / src).read_bytes())
                applied.append(dest)
            for path in p["remove"]:
                dp = ROOT / path                 # path는 이미 canonical(P1)
                _stage(dp)
                dp.unlink(missing_ok=True)
                removed.append(path)
            for src, dest in p["conflict"]:
                side = ROOT / (dest + f".upstream-{v}")
                _stage(side)
                _write_atomic(side, (tree / src).read_bytes())
                sidecars.append(posix_rel(side, ROOT))
            for d in skel:                       # 이미 _allowed_skel로 봉쇄·검증됨
                if not d.exists():
                    d.mkdir(parents=True)
                    gk = d / ".gitkeep"
                    _stage(gk)
                    gk.write_text("", encoding="utf-8")
                    made_skel.append(posix_rel(d, ROOT))
        except OSError as e:
            for path, data in backup.items():    # best-effort 원상복구
                try:
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_atomic(path, data)
                except OSError:
                    pass
            ledger_append(UPDATE_JOURNAL,
                          {"kind": "rollback", "version": v, "why": str(e)[:200]})
            raise UpdateError(f"갱신 적용 중 실패 — 원상복구했다: {e}")

        # 파일 조작 성공 후에만 적용 상태를 저널에 남긴다(baseline·삭제).
        for dest in applied:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "version": v, "path": dest,
                "hash": sha256_file(ROOT / dest)})
        for _src, dest in p["rebaseline"]:       # 내용 동일·기준선만 갱신
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "version": v, "path": dest,
                "hash": sha256_file(ROOT / dest)})
        for path in removed:
            ledger_append(UPDATE_JOURNAL,
                          {"kind": "remove", "version": v, "path": path})
        # skip은 conflict **사건**이지 적용 상태 변경이 아니다 — `skipped_path`로
        # 남겨 baseline/관리 판정(`path` 키)이 이를 보지 않게 한다(P2).
        for _src, dest in p["conflict"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "version": v, "skipped_path": dest,
                "why": "로컬 수정 — upstream 사본을 옆에 두었다"})
        for path in p["remove_conflict"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "version": v, "skipped_path": path,
                "why": "upstream 삭제됐으나 로컬 수정 — 보존한다"})
        ledger_append(UPDATE_JOURNAL, {
            "kind": "done", "version": v, "applied": len(applied),
            "removed": len(removed), "conflicts": len(sidecars)})
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
