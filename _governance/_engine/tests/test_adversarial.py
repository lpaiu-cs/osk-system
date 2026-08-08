#!/usr/bin/env python3
"""적대적 시나리오 하네스 — 갱신·릴리스의 불변식을 기계로 소진시킨다.

왜 별도 하네스인가: `test_regression.py`는 **알려진** 결함을 영속 고정한다.
이 파일은 아직 모르는 결함을 찾는다 — 실제 프로세스를 **SIGKILL로 죽이고**,
악의 릴리스와 동시 데몬을 조합해 무작위로 돌린 뒤, 매 시행마다 불변식을
검사한다. 리뷰가 한 겹씩 벗겨 온 층(경로 봉쇄·원자성·동시성)을 사람이 아니라
반복 시행이 훑게 하는 것이 목적이다.

검사하는 불변식 (Mechanism §1-2):
  I1 혼합 상태 금지 — 어느 시점에 죽어도 파일 전체가 구판이거나 신판이다.
  I2 파일↔저널 정합 — 커밋된 baseline은 디스크의 실제 해시와 일치한다.
  I3 인스턴스 소유 바닥 불변 — 릴리스·매니페스트가 무엇을 말해도 침범 없다.
  I4 경로 봉쇄·정체성 — vault 밖 파일이 생기지 않는다.
  I5 복구 수렴 — 크래시 후 재실행하면 정상 상태로 수렴한다(멱등).

실행:
    python3 _governance/_engine/tests/test_adversarial.py [--trials 12] [--seed 7]
"""
from __future__ import annotations
import argparse, hashlib, json, os, random, shutil, signal, subprocess, sys, time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
PY = sys.executable
PASS: list[str] = []
FAIL: list[str] = []
# 커버리지 신호 — 하네스가 **실제로** 위험 구간을 때렸는지. 0이면 시나리오가
# 무해하게 통과한 것이므로 검사 통과 자체가 의미 없다(타이밍·설계를 고쳐야 한다).
HIT = {"pending_txn": 0, "rollback": 0, "roll_forward": 0, "half_applied": 0,
       "attack_blocked": 0, "kill_too_late": 0}


def check(name: str, cond: bool, detail=None) -> bool:
    (PASS if cond else FAIL).append(
        name if cond else f"{name} — {detail!r}")
    return bool(cond)


def sh(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=120, **kw)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return sh("git", "-C", str(root), *args)


def sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


# ── 정본·인스턴스 픽스처 ─────────────────────────────────────────────────

def node(nid: str, summary: str, body: str = "본문") -> str:
    """통치 문서·사료는 **특수한 노드**이므로 계약(frontmatter)을 갖춘다.
    id의 날짜부와 created가 정합해야 한다(Mechanism §2 1·2항)."""
    return (f"---\nid: {nid}\ncreated: 2026-08-02 16:00 (KST)\n"
            f"updated: 2026-08-02 16:00 (KST)\nauthor: user\ndrafter: fable-5\n"
            f'summary: "{summary}"\n---\n\n{body}\n')


FRAMEWORK = {                       # 정본이 관리하는 프레임워크 파일들
    "_governance/Constitution.md": node("260802-advc-0001", "헌법", "1조."),
    "_governance/Mechanism.md": node("260802-advc-0002", "최소 사양", "§1."),
    "_governance/records/rec.md": node("260802-advc-0003", "사료"),
    "_governance/_engine/osk/mod_a.py": "A = 1\n",
    "_governance/_engine/osk/mod_b.py": "B = 1\n",
    "_governance/_engine/osk/mod_c.py": "C = 1\n",
    "docs/SETUP.md": "# 설치\n",
}
# mutation 구간을 실측 가능한 길이로 만든다 — 파일이 몇 개뿐이면 write 구간이
# 너무 짧아 무작위 SIGKILL이 그 구간에 떨어지지 않고, 검사가 무해하게 통과한다.
for _i in range(150):
    FRAMEWORK[f"_governance/_engine/osk/pad_{_i:02d}.py"] = f"PAD = {_i}\n"
MANIFEST = ("MAP  _governance/ -> _governance/\n"
            "MAP  docs/ -> docs/\n"
            "KEEP LICENSE\nKEEP README.md\n"
            "DENY _ledger/\nDENY __pycache__/\nDENY .osk/\n"
            "SKEL = Scope/\nSKEL = Domain/\n")

# 인스턴스 소유 바닥 — 갱신이 절대 건드리면 안 되는 것들(I3)
FLOOR = {
    "= Scope/W1/node.md": node("260802-advf-0001", "인스턴스 지식 노드"),
    "= Scope/Workbench/_ledger/signatures.jsonl": '{"kind":"sign"}\n',
    "= Person/Module/pref.md": node("260802-advf-0002", "인스턴스 선호"),
    "_sources/img.bin": "raw\n",
}


def make_canonical(root: Path, version: str, files: dict) -> None:
    """정본 저장소를 만들고 릴리스를 선언한다(비준증빙 + 태그)."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (root / "_governance/_engine/scripts").mkdir(parents=True, exist_ok=True)
    (root / "_governance/_engine/scripts/publish-manifest.txt").write_text(
        MANIFEST, encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    if not (root / ".git").exists():
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", f"tree {version}")
    # 비준증빙은 엔진의 release 모듈로 만든다(정본 쪽 절차 그대로).
    r = sh(PY, "-m", "osk.release", "--version", version, "--apply",
           env={**os.environ, "PYTHONPATH": str(ENGINE),
                "OSK_VAULT_ROOT": str(root)}, cwd=str(root))
    if r.returncode != 0:            # release는 대화형 전속이라 CLI가 거부한다
        _release_direct(root, version)


def _release_direct(root: Path, version: str) -> None:
    """대화형 요구를 우회해 run()을 직접 부른다 — 픽스처 생성 목적."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from osk import release; release.run(%r, apply=True, root=__import__('pathlib').Path(r'%s'))"
        % (str(ENGINE), version, str(root)))
    r = sh(PY, "-c", code, env={**os.environ, "OSK_VAULT_ROOT": str(root)})
    if r.returncode != 0:
        raise RuntimeError(f"릴리스 픽스처 실패: {r.stderr[-400:]}")


def make_instance(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in FLOOR.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "i@i")
    git(root, "config", "user.name", "i")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "instance base")


def run_update(inst: Path, bundle: Path, *extra: str,
               kill_after: float | None = None, kill_in_txn: float | None = None):
    """갱신을 **별도 프로세스**로 돌린다. SIGKILL이므로 파이썬 예외 처리·finally가
    전혀 돌지 않는 진짜 크래시다(전원 차단과 같은 결).

    `kill_in_txn`: 트랜잭션 표식(`.osk/txn/manifest.json`)이 **나타나는 순간**을
    관찰해 그 뒤 지정 시간에 죽인다 — 프로덕션 코드에 시험용 훅을 넣지 않고도
    mutation 구간을 정확히 때린다(무작위 시간 kill은 그 구간을 거의 못 맞춘다)."""
    cmd = [PY, "-m", "osk.update", "--from", str(bundle), "--source", "bundle",
           *extra]
    env = {**os.environ, "PYTHONPATH": str(ENGINE), "OSK_VAULT_ROOT": str(inst)}
    if kill_after is None and kill_in_txn is None:
        return sh(*cmd, env=env, cwd=str(inst))
    p = subprocess.Popen(cmd, env=env, cwd=str(inst),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if kill_in_txn is not None:
        marker = inst / ".osk" / "txn" / "manifest.json"
        deadline = time.time() + 60
        while time.time() < deadline:
            if marker.is_file() or p.poll() is not None:
                break
            time.sleep(0.0005)
        time.sleep(kill_in_txn)
    else:
        time.sleep(kill_after)
    if p.poll() is None:
        os.kill(p.pid, signal.SIGKILL)
    else:
        HIT["kill_too_late"] += 1     # 이미 끝났다 — 이 시행은 크래시를 못 만들었다
    p.wait(timeout=30)
    return p


def recover(inst: Path, apply: bool = True):
    """엔진과 독립된 복구 부트스트랩 — half-applied 엔진에서도 돈다."""
    cmd = [PY, str(ENGINE / "scripts" / "recover.py"), "--root", str(inst)]
    if apply:
        cmd.append("--apply")
    return sh(*cmd)


# ── 불변식 검사 ──────────────────────────────────────────────────────────

def journal(inst: Path) -> list[dict]:
    j = inst / "= Scope/Workbench/_ledger/update.jsonl"
    if not j.is_file():
        return []
    out = []
    for line in j.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def committed_baselines(inst: Path) -> dict[str, str]:
    """저널에서 **커밋된** 트랜잭션의 최종 baseline(경로→해시)."""
    recs = journal(inst)
    done = {r.get("txn") for r in recs if r.get("kind") == "done" and r.get("txn")}
    base: dict[str, str] = {}
    for r in recs:
        if r.get("txn") and r["txn"] not in done:
            continue                 # 미커밋 — 판정에서 제외(committed()와 같은 규율)
        if r.get("kind") == "apply" and r.get("path"):
            base[r["path"]] = r.get("hash")
        elif r.get("kind") == "remove" and r.get("path"):
            base.pop(r["path"], None)
    return base


def check_invariants(tag: str, inst: Path, versions: dict[str, dict],
                    strict: bool = True) -> None:
    """versions: {version: {rel: body}} — 알려진 정본 판본들.

    `strict=False`는 **복구 전**(pending 트랜잭션이 살아 있는) 상태다. 그때는
    파일이 반쯤 새 판일 수 있고 커밋된 baseline과도 어긋나는 것이 정상이다 —
    그것이 pending 표식의 의미다. 그 상태에서 보장되는 것은 바닥 불변(I3)·
    vault 밖 없음(I4)·표식 존재이며, I1(혼합 금지)·I2(baseline 정합)는 복구가
    끝난 뒤의 보장이다."""
    # I3 — 인스턴스 소유 바닥 불변
    for rel, body in FLOOR.items():
        p = inst / rel
        check(f"[{tag}] I3 바닥 불변: {rel}",
              p.is_file() and p.read_text(encoding="utf-8") == body)
    # I4 — vault 밖 유출 없음(부모에 침범 파일이 생기지 않았다)
    outside = inst.parent / "payload"
    check(f"[{tag}] I4 vault 밖 파일 없음", not outside.exists())
    if not strict:
        check(f"[{tag}] 복구 전에는 pending 표식이 있다",
              (inst / ".osk/txn/manifest.json").is_file())
        return
    # I1 — 프레임워크가 어느 한 판본과 정확히 일치(혼합 금지). 파일이 하나도
    #      없으면(미편입) 통과. 부분집합/뒤섞임이면 실패.
    present = {rel: (inst / rel).read_text(encoding="utf-8")
               for rel in {r for v in versions.values() for r in v}
               if (inst / rel).is_file()}
    if present:
        matches = [v for v, files in versions.items()
                   if all(present.get(r) == b for r, b in files.items()
                          if (inst / r).is_file())
                   and set(present) <= set(files)]
        check(f"[{tag}] I1 혼합 상태 없음(어느 한 판본과 일치)",
              bool(matches),
              {r: present[r][:20] for r in sorted(present)[:6]})
    # I2 — 커밋된 baseline은 디스크 실제 해시와 일치
    for rel, h in committed_baselines(inst).items():
        p = inst / rel
        if p.is_file():
            check(f"[{tag}] I2 baseline↔디스크 정합: {rel}", sha(p) == h,
                  (sha(p), h))
        else:
            check(f"[{tag}] I2 baseline이 있는데 파일 부재: {rel}", False)


# ── 시나리오 ─────────────────────────────────────────────────────────────

def scenario_crash_midway(tmp: Path, rnd: random.Random, trial: int) -> None:
    """v1 편입 → v2 적용 중 무작위 시점 SIGKILL → 복구 → 재적용 수렴(I1·I2·I5)."""
    base = tmp / f"crash{trial}"
    can, inst = base / "can", base / "inst"
    v1 = dict(FRAMEWORK)
    make_canonical(can, "v1.0.0", v1)
    make_instance(inst)
    r = run_update(inst, can, "--apply", "--adopt")
    check(f"[crash{trial}] v1 편입 성공", r.returncode == 0, r.stderr[-200:])

    v2 = {k: (v + f"\n# v2 변경 {trial}\n") for k, v in v1.items()}
    can2 = base / "can2"
    shutil.copytree(can, can2)
    shutil.rmtree(can2 / ".git")
    for rel, body in v2.items():
        (can2 / rel).write_text(body, encoding="utf-8")
    make_canonical(can2, "v2.0.0", v2)

    versions = {"v1": v1, "v2": v2}
    # 적용 도중 무작위 시점에 죽인다(전 구간을 훑도록 짧은 창을 무작위로).
    run_update(inst, can2, "--apply", kill_in_txn=rnd.uniform(0.002, 0.06))
    if (inst / ".osk/txn/manifest.json").is_file():
        HIT["pending_txn"] += 1                  # 트랜잭션 도중에 죽였다
    mixed = any((inst / r).read_text(encoding="utf-8") == v2[r]
                for r in v2 if (inst / r).is_file()) and \
        any((inst / r).read_text(encoding="utf-8") == v1[r]
            for r in v1 if (inst / r).is_file())
    if mixed:
        HIT["half_applied"] += 1                 # 실제로 혼합 상태를 만들었다
    check_invariants(f"crash{trial}/kill", inst, versions,
                     strict=not (inst / ".osk/txn/manifest.json").is_file())

    rec = recover(inst)
    check(f"[crash{trial}] 복구 부트스트랩 성공", rec.returncode == 0,
          rec.stderr[-200:])
    try:
        act = json.loads(rec.stdout or "{}").get("action")
        if act == "rollback":
            HIT["rollback"] += 1
        elif act == "roll-forward":
            HIT["roll_forward"] += 1
    except ValueError:
        pass
    check_invariants(f"crash{trial}/recovered", inst, versions)

    r2 = run_update(inst, can2, "--apply")
    check(f"[crash{trial}] I5 재적용 수렴", r2.returncode == 0, r2.stderr[-300:])
    check_invariants(f"crash{trial}/reapplied", inst, versions)
    for rel, body in v2.items():     # 최종 상태는 v2여야 한다
        check(f"[crash{trial}] 최종 v2: {rel}",
              (inst / rel).read_text(encoding="utf-8") == body)


def scenario_malicious_release(tmp: Path, rnd: random.Random, trial: int) -> None:
    """악의 릴리스가 바닥·경로 봉쇄를 뚫는지(I3·I4). 어느 것도 통과해선 안 된다."""
    base = tmp / f"evil{trial}"
    can, inst = base / "can", base / "inst"
    make_canonical(can, "v1.0.0", dict(FRAMEWORK))
    make_instance(inst)
    run_update(inst, can, "--apply", "--adopt")

    attacks = [
        ("바닥 직접 침범", {"= Scope/W1/node.md": "침범\n"},
         "MAP  = Scope/ -> = Scope/\n"),
        ("대장 침범", {"= Scope/Workbench/_ledger/signatures.jsonl": "위조\n"},
         "MAP  = Scope/ -> = Scope/\n"),
        ("경로 탈출", {"_governance/x.md": "탈출\n"},
         "MAP  _governance/ -> ../payload/\n"),
        ("SKEL 바닥 파고들기", {}, "SKEL = Scope/Workbench/_ledger\n"),
        ("SKEL 루트 탈출", {}, "SKEL ../payload\n"),
        ("바닥 재진입(..)", {"_governance/y.md": "재진입\n"},
         "MAP  _governance/ -> docs/../= Scope/\n"),
    ]
    name, files, extra_map = attacks[trial % len(attacks)]
    ev = base / f"evil-{trial}"
    ev.mkdir(parents=True)
    for rel, body in {**FRAMEWORK, **files}.items():
        p = ev / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (ev / "_governance/_engine/scripts").mkdir(parents=True, exist_ok=True)
    (ev / "_governance/_engine/scripts/publish-manifest.txt").write_text(
        MANIFEST + extra_map, encoding="utf-8")
    att = {"version": "v9.9.9", "at": "2026-01-01T00:00:00+09:00",
           "files": {}}
    for f in sorted(ev.rglob("*")):
        if f.is_file():
            att["files"][f.relative_to(ev).as_posix()] = sha(f)
    (ev / "release.json").write_text(json.dumps(att, ensure_ascii=False),
                                     encoding="utf-8")
    r = run_update(inst, ev, "--apply")      # 성공/실패 무관 — 불변식만 본다
    blocked = r.returncode != 0 or "쓰지 않는다" in (r.stdout or "") \
        or "봉쇄" in (r.stdout or "") or "바닥" in (r.stdout or "")
    if blocked or not (inst.parent / "payload").exists():
        HIT["attack_blocked"] += 1
    check_invariants(f"evil{trial}/{name}", inst, {"v1": dict(FRAMEWORK)})


def scenario_daemon_race(tmp: Path, rnd: random.Random, trial: int) -> None:
    """update 크래시 직후 데몬이 혼합 상태를 커밋하지 않는가 — pending 표식 존중."""
    base = tmp / f"race{trial}"
    can, inst = base / "can", base / "inst"
    make_canonical(can, "v1.0.0", dict(FRAMEWORK))
    make_instance(inst)
    run_update(inst, can, "--apply", "--adopt")

    v2 = {k: v + "\n# v2\n" for k, v in FRAMEWORK.items()}
    can2 = base / "can2"
    shutil.copytree(can, can2)
    shutil.rmtree(can2 / ".git")
    for rel, body in v2.items():
        (can2 / rel).write_text(body, encoding="utf-8")
    make_canonical(can2, "v2.0.0", v2)

    run_update(inst, can2, "--apply", kill_in_txn=rnd.uniform(0.002, 0.05))
    pending = (inst / ".osk/txn/manifest.json").is_file()
    code = ("import sys; sys.path.insert(0, r'%s'); import sync_daemon as s;"
            "print(s.once(__import__('pathlib').Path(r'%s')))"
            % (str(ENGINE), str(inst)))
    d = sh(PY, "-c", code, env={**os.environ, "OSK_VAULT_ROOT": str(inst)})
    verdict = (d.stdout or "").strip().splitlines()[-1:] or [""]
    if pending:
        HIT["pending_txn"] += 1
        check(f"[race{trial}] 미완료 트랜잭션 중 데몬 tick 거부",
              verdict[0] in ("pending-txn", "locked"), (verdict, d.stderr[-200:]))
        # 데몬이 손대지 않았음을 확인한 뒤 복구하고 엄격 검사한다.
        check_invariants(f"race{trial}/pending", inst,
                         {"v1": dict(FRAMEWORK), "v2": v2}, strict=False)
        rec = recover(inst)
        check(f"[race{trial}] 복구 성공", rec.returncode == 0, rec.stderr[-200:])
    check_invariants(f"race{trial}", inst, {"v1": dict(FRAMEWORK), "v2": v2})


SCENARIOS = (scenario_crash_midway, scenario_malicious_release,
             scenario_daemon_race)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="osk-adversarial", description=__doc__)
    ap.add_argument("--trials", type=int, default=9)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    rnd = random.Random(a.seed)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="osk-adv-") as td:
        tmp = Path(td)
        for i in range(a.trials):
            fn = SCENARIOS[i % len(SCENARIOS)]
            try:
                fn(tmp, rnd, i)
            except Exception as e:      # noqa: BLE001 — 하네스 자체 실패도 보고
                import traceback
                FAIL.append(f"{fn.__name__}[{i}] 예외: {e!r}\n"
                            + "".join(traceback.format_exc().splitlines(True)[-5:]))
    print(f"적대적 하네스: 통과 {len(PASS)} / 실패 {len(FAIL)} "
          f"(시행 {a.trials}, seed {a.seed})")
    print("커버리지(위험 구간 적중):", json.dumps(HIT, ensure_ascii=False))
    if HIT["pending_txn"] == 0:
        print("  ⚠ 트랜잭션 도중에 한 번도 죽이지 못했다 — kill 타이밍을 넓혀야 "
              "검사가 의미를 갖는다(무해 통과)")
    for f in FAIL:
        print("FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
