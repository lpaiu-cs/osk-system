"""값이 새로 든 노드 본문 편집을 변경 경로와 새 값의 종류로 나눠 센다 — 추상화 검토의 두 기준 비교.

docs/abstraction-review.md §10의 실측이다. vault의 git 이력과 재검토 대장만 읽고 집계만 낸다.

- 변경 경로: 재검토(재검토 대장의 `updated`가 그 편집 구간에 있다) · 섞임(재검토와 다른 경로가 한
  동기화 커밋에 함께 들었다) · 원료 추가(`derived-from`에 노드가 늘었다) · 근거 추가(근거 절에 새 인용
  좌표가 들었다) · 직접 수정(어느 것도 아니다)
- 새 값의 종류: 사본(편집 시점의 `derived-from` 노드 본문에 있다) · 근거의 수 · 자기 값 · 원료 없음
- 값: 단위·소수점이 붙었거나 세 자리 이상인 수, 그리고 근거의 수. 날짜·인용 좌표·링크 이름 속 수는 뺀다.
변경 경로 기준은 재검토·섞임 편집에, 첫 기록 노드 기준은 거기에 더해 사본·근거의 수가 든 편집에 손댄다.
수치는 기계 판독의 상한이다 — 값이 결론에 드는지, 같은 사실인지는 판단하지 않는다.

사용: python benchmarks/abstraction_paths.py <vault 루트> [--since YYYY-MM-DD] [--central]
      python benchmarks/abstraction_paths.py --selftest
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime

WL = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
IDRE = re.compile(r"\b\d{6}-[0-9a-z]{4}-[0-9a-z]{4,8}\b")
KO = r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"
NUM = re.compile(r"\d+(?:[.,]\d+)*(?:%|배|개|건|곳|번|회|초|분|시간|일|주|명|줄|자|턴|MB|GB|KB|ms)?")
KNUM = re.compile(KO + r"\s?(?:개|건|곳|번|가지|명|source|출처|Scope|scope|사례|재현|단|층)")
COUNT = re.compile(r"(?:\d+|" + KO + r")\s?(?:개의\s)?(?:source|출처|Scope|scope)")
COORD = re.compile(r"[0-9a-f]{8}#\d+(?:[–-]\d+)?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\b\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")
PATHS = ("재검토", "섞임", "원료 추가", "근거 추가", "직접 수정")
KINDS = ("사본", "근거의 수", "자기 값", "원료 없음")


def split(text):
    """frontmatter, 본문(근거 절 제외 — 재검토 상태와 같은 정의), 근거 절."""
    fm, body = "", text
    if text.startswith("---") and (end := text.find("\n---", 3)) > 0:
        fm, body = text[3:end], text[end + 4:]
    m = re.search(r"^## 근거\s*$", body, re.M)
    return fm, (body[:m.start()] if m else body), (body[m.start():] if m else "")


def dfrom(fm):
    # 제목 링크와 id 목록 두 표기가 이력에 섞여 있다
    m = re.search(r"^derived-from:(.*?)(?=^\S|\Z)", fm, re.M | re.S)
    return [t.strip() for t in WL.findall(m.group(1))] + IDRE.findall(m.group(1)) if m else []


def node_id(fm):
    m = re.search(r'^id:\s*"?([0-9a-z-]+)', fm, re.M)
    return m.group(1) if m else None


def values(s):
    s = DATE.sub(" ", COORD.sub(" ", WL.sub(" ", s)))     # 링크 이름·인용 좌표·날짜 속 수는 값이 아니다
    # 단위·소수점이 붙었거나 세 자리 이상인 값만 — 맨숫자 1·2자리와 연도는 우연히 겹친다
    return Counter(t for t in NUM.findall(s) + KNUM.findall(s)
                   if not t.isdigit() or (len(t) >= 3 and not re.fullmatch(r"(19|20)\d\d", t)))


class Blobs:
    def __init__(self, root):
        self.p = subprocess.Popen(["git", "-C", root, "-c", "core.quotepath=off", "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.cache = {}

    def get(self, sha, path):
        key = f"{sha}:{path}"
        if key not in self.cache:
            self.p.stdin.write((key + "\n").encode("utf-8"))
            self.p.stdin.flush()
            hdr = self.p.stdout.readline().decode("utf-8", "replace").rstrip("\n")
            if hdr.endswith((" missing", " ambiguous")):    # 이 줄에는 공백 섞인 경로가 그대로 찍힌다
                self.cache[key] = None
            else:
                data = self.p.stdout.read(int(hdr.split()[2]))
                self.p.stdout.read(1)
                self.cache[key] = data.decode("utf-8", "replace")
        return self.cache[key]


def measure(root, since=None):
    spaces = [s for s in ("00_Scope", "00_Domain", "00_Person", "= Scope", "= Domain", "= Person")
              if os.path.isdir(os.path.join(root, s))]
    scope = next(s for s in spaces if s.endswith("Scope"))

    def is_node(p):
        parts = p.split("/")
        if parts[0] not in spaces or not p.endswith(".md") or any(s.startswith("_") for s in parts[1:]):
            return False
        if parts[0] == scope and len(parts) > 2 and parts[1] == "Workbench":
            return False
        return not (len(parts) >= 3 and os.path.splitext(parts[-1])[0] == parts[-2])    # 허브

    log = subprocess.run(["git", "-C", root, "-c", "core.quotepath=off", "log", "--format=@@%H %cI",
                          "--name-only", "--", *spaces], capture_output=True, text=True, encoding="utf-8").stdout
    hist, head = defaultdict(list), None
    for line in log.splitlines():
        if line.startswith("@@"):
            head = line[2:].split(" ", 1)
        elif line.strip() and head and is_node(line.strip()):
            hist[line.strip()].append((head[0], head[1]))
    blobs = Blobs(root)
    where = defaultdict(set)                    # 제목·id → 이력 속 경로(이동·개명 뒤에도 같은 노드)
    for p, commits in hist.items():
        where[os.path.splitext(p.split("/")[-1])[0]].add(p)
        if (t := next((b for sha, _ in commits if (b := blobs.get(sha, p)) is not None), None)) is not None:
            if nid := node_id(split(t)[0]):
                where[nid].add(p)
    ledger = os.path.join(root, scope, "Workbench", "_ledger", "rechecks.jsonl")
    rows = [json.loads(line) for line in open(ledger, encoding="utf-8")] if os.path.exists(ledger) else []
    updated = defaultdict(list)
    for e in rows:
        if e.get("result") == "updated":
            updated[e["node"]].append(e["at"])

    out = {"edits": 0, "value_edits": 0, "cells": Counter(), "weeks": defaultdict(Counter),
           "own": Counter(), "first": None, "last": None, "rows": rows, "where": where, "is_node": is_node,
           "spaces": spaces}
    for path, commits in hist.items():
        prev = None
        for sha, at in reversed(commits):                   # 오래된 것부터
            text = blobs.get(sha, path)
            if text is None:
                prev = None
                continue
            fm, body, evid = split(text)
            if prev is not None and body.strip() != prev[1].strip() and (since is None or at >= since):
                out["edits"] += 1
                out["first"] = min(out["first"] or at, at)
                out["last"] = max(out["last"] or at, at)
                new = values(body) - values(prev[1])
                if not new:
                    prev = (fm, body, at, evid)
                    continue
                out["value_edits"] += 1
                nid = node_id(fm)
                rc = bool(nid) and any(prev[2] < u <= at for u in updated.get(nid, []))
                ev = bool(set(COORD.findall(evid)) - set(COORD.findall(prev[3])))   # 새 줄이나 늘어난 좌표 구간
                src = bool(set(dfrom(fm)) - set(dfrom(prev[0])))
                origin = ("섞임" if ev or src else "재검토") if rc else \
                    "근거 추가" if ev else "원료 추가" if src else "직접 수정"
                srcs = [split(b)[1] for t in dfrom(fm) for sp in where.get(t, ())
                        if (b := blobs.get(sha, sp)) is not None]
                srcs = [DATE.sub(" ", COORD.sub(" ", WL.sub(" ", s))) for s in srcs]
                kind = "근거의 수" if any(COUNT.search(k) for k in new) else \
                    "사본" if any(k in s for k in new for s in srcs) else \
                    "원료 없음" if not srcs else "자기 값"
                out["cells"][(origin, kind)] += 1
                y, w, _ = datetime.fromisoformat(at).isocalendar()
                week = out["weeks"][f"{y}-W{w:02d}"]
                week[origin] += 1
                week["변경 경로 기준"] += origin in ("재검토", "섞임")
                week["첫 기록 노드 기준"] += origin in ("재검토", "섞임") or kind in ("사본", "근거의 수")
                if kind in ("자기 값", "원료 없음"):
                    out["own"][path] += 1
            prev = (fm, body, at, evid)
    return out


def central(root, out):
    """자기 값이 세 번 이상 든 노드 가운데 지금 셋 이상이 derived-from으로 기대는 노드, 그리고 그 노드가
    바뀐 뒤 기대는 노드의 재검토 결과."""
    leaning, ids = defaultdict(set), {}
    for top in out["spaces"]:
        for dp, _, fn in os.walk(os.path.join(root, top)):
            for f in fn:
                rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
                if out["is_node"](rel):
                    fm = split(open(os.path.join(root, rel), encoding="utf-8", errors="replace").read())[0]
                    stem = os.path.splitext(f)[0]
                    ids[stem] = node_id(fm)
                    for t in set(dfrom(fm)):
                        leaning[t].add(stem)
    alive = {os.path.splitext(p.split("/")[-1])[0] for p, n in out["own"].items()
             if n >= 3 and os.path.exists(os.path.join(root, p))}
    deps = {s: leaning.get(s, set()) | leaning.get(ids.get(s), set()) for s in alive}
    hubs = {ids[s] for s in alive if len(deps[s]) >= 3 and ids.get(s)}
    res = Counter(e["result"] for e in out["rows"] if e.get("target") in hubs
                  and e.get("reason") not in ("이어받음", "기준선"))
    return len(alive), Counter(min(len(d), 3) for d in deps.values()), len(hubs), res


def selftest():
    fm, body, ev = split('---\nid: 261102-0abc-defg1234\nderived-from: ["[[A]]", 260801-0000-abcd]\n---\n'
                         'x 12건, 2026-10-01, [[B 300]] `c38d8ef2#107`, 세 Scope 7\n## 근거\n- `c38d8ef2#108–109`\n')
    assert dfrom(fm) == ["A", "260801-0000-abcd"] and node_id(fm) == "261102-0abc-defg1234"
    assert set(values(body)) == {"12건", "세 Scope"}, values(body)
    assert COORD.findall(ev) == ["c38d8ef2#108–109"]
    print("selftest ok")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["--selftest"]:
        return selftest()
    ap = argparse.ArgumentParser(description="추상화 검토의 두 기준 비교 실측")
    ap.add_argument("root")
    ap.add_argument("--since", help="이 날짜 이후 편집만 센다(YYYY-MM-DD)")
    ap.add_argument("--central", action="store_true", help="중심이면서 늘 최신인 노드의 집계")
    a = ap.parse_args()
    out = measure(a.root, a.since)
    n = out["value_edits"] or 1
    cp = sum(c for (o, _), c in out["cells"].items() if o in ("재검토", "섞임"))
    fr = sum(c for (o, k), c in out["cells"].items() if o in ("재검토", "섞임") or k in ("사본", "근거의 수"))
    print(f"본문 편집 {out['edits']}건 · 그중 값이 새로 든 편집 {out['value_edits']}건 "
          f"({(out['first'] or '')[:10]} ~ {(out['last'] or '')[:10]})")
    print(f"변경 경로 기준이 손대는 편집 {cp} ({100 * cp / n:.1f}%) · 첫 기록 노드 기준 {fr} ({100 * fr / n:.1f}%)")
    print("\n변경 경로 \\ 새 값  " + "".join(f"{k:>9s}" for k in KINDS))
    for o in PATHS:
        print(f"  {o:10s}" + "".join(f"{out['cells'][(o, k)]:>9d}" for k in KINDS))
    cols = PATHS + ("변경 경로 기준", "첫 기록 노드 기준")
    print("\n주        " + " ".join(f"{c:>7s}" for c in cols))
    for wk in sorted(out["weeks"]):
        print(f"{wk}  " + " ".join(f"{out['weeks'][wk][c]:>7d}" for c in cols))
    if a.central:
        alive, dist, hubs, res = central(a.root, out)
        print(f"\n최신성 노드(자기 값이 세 번 이상 든 노드) 중 지금 남은 {alive}개 — 기대는 노드 0개 {dist[0]} · "
              f"1개 {dist[1]} · 2개 {dist[2]} · 3개 이상 {dist[3]}")
        print(f"셋 이상이 기대는 {hubs}개가 바뀐 뒤 기대는 노드의 재검토: {dict(res)}")


if __name__ == "__main__":
    main()
