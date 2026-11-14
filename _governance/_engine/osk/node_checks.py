"""osk.node_checks — 노드를 쓰기 전의 검사와, 쓴 뒤 돌려주는 되먹임.

엣지와 근거의 계약(시행령 §1), 참조 위상(헌법 8조), 대상 없는 링크, 본문 치환의 관측 증명(CAS,
Mechanism §6-2 4항)을 본다. 쓰기(`write`)와 이동(`node_moves`)이 같은 검사를 쓴다."""
from __future__ import annotations
import os, re, tempfile
from pathlib import Path
from .core import ID_RE, CASE_RE, sha256_file
from . import contract, evidence, graph, nodefile, rechecks, placement, topology
from .errors import WriteError
from .vault import Vault


def open_cases(vault: Vault) -> list[str]:
    """열린(docketed) 사건 번호 — conflicts 거부에 실어 보낸다."""
    return sorted(no for no, c in graph.load_cases(vault).items()
                  if str(c.get("status")) == "docketed")


WORKBENCH_STATE = ("Workbench의 작업 상태(scope 기억 포함)는 근거 또는 권위의 출처로 참조하지 "
                   "않는다(Workbench 계약 4.2). 근거는 그 지식이 나온 곳이다.")


def check_edges(vault: Vault, edges: dict | None, idx, *, adding: bool = True) -> list[str]:
    """술어와 **대상 값의 형**을 함께 본다. 스키마(표면)와 통로(여기) 이중으로
    거는 이유는 CLI·Bash 경유가 스키마를 통과하지 않기 때문이다 — 검증은
    통로에, 교육은 스키마에(10차 정정 ①).

    `derived-from`은 노드만 받는다(헌법 제8조 5항). 더하는 비노드 대상은 거부하고 근거 절로
    보낸다 — 빼는 것(`adding=False`)은 받는다. 이행 전의 옛 비노드 대상을 지울 수 있어야 한다.

    `idx`는 호출부가 잠금 안에서 **한 번 지어** 내려보낸다 (v3.7.0). 기본값을
    두지 않는 것은 규율이다 — 기본값이 있으면 새 호출부가 색인을 또 짓는 것이
    언제나 가능해지고, 증폭은 조용히 돌아온다."""
    errs = []
    for pred, targets in (edges or {}).items():
        if pred not in contract.PREDICATES:
            errs.append(f"계약 외 술어: {pred} "
                        f"(쓸 수 있는 것: {', '.join(contract.PREDICATES)})")
            continue
        for t in (targets if isinstance(targets, list) else [targets]):
            if not isinstance(t, str) or not t.strip():
                errs.append(f"엣지 대상은 비어 있지 않은 문자열이어야 한다: "
                            f"{pred} → {t!r}")
                continue
            if pred == "derived-from" and not re.match(ID_RE, t.strip()):
                # 노드 근거는 **제목 위키링크**로 단다(Mechanism §8 2항).
                #
                # 경로 표기는 쓰지 않는다. 근거로 삼았던 "경로·이름은 상태라
                # 끊어진다"는 이름에 대해서는 사실이 아니다 — `move_node`는
                # 디렉토리만 바꾸고 이름 색인은 경로와 무관하다. 오히려
                # **경로형이 이동에 끊어진다**(resolve의 `/` 갈래는 그 자리에
                # 파일이 없으면 dangling). 게다가 `contract.target_stem`이
                # 경로형과 이름형을 같은 키로 접으므로 resolve 밖에서는
                # 구별되지도 않는다. 제목은 전역에서 유일하니 경로가 필요 없다.
                s = t.strip()
                inner = s[2:-2].strip() if s.startswith("[[") and s.endswith("]]") else s
                target = inner.split("|", 1)[0].strip()
                found = idx.resolve(target.split("#", 1)[0].strip())
                if "/" in inner and idx.resolve(inner)[0] == "node":
                    errs.append(
                        f"derived-from의 노드 근거에 경로 표기는 쓰지 않는다: {t} — "
                        f"경로는 이동에 끊어진다. 그 노드의 **제목**을 쓰라")
                elif adding and found[0] == "nonnode" and found[1][0] == "workbench":
                    # 근거 절로 옮겨도 안 된다 — 진단은 계약 4.2가 맡는다
                    errs.append(f"작업 상태는 근거로 쓰지 않는다: {t} — {WORKBENCH_STATE}")
                elif adding and found[0] != "node" and (
                        found[0] in ("nonnode", "external") or evidence.kind(target) != "name"):
                    # 인용 기록·라운드 좌표·URL·별칭 좌표·경로는 모양으로, 이름은 해석으로 가른다
                    errs.append(
                        f"derived-from은 노드만 받는다: {t} — 비노드 근거는 노드 끝 `# 관측` 절에 "
                        f"`- 날짜 · `좌표` · 지지|조건|반례 — 보인 것` 한 줄로 적는다(헌법 제8조 5·6항). "
                        f"인용 라운드의 좌표는 cite_round·read_cited가 주는 coordinate다")
                continue
            if pred == "conflicts" and not re.match(CASE_RE, t.strip()):
                # 존치 상호 치환은 표면으로 만들 수 없다(닭-달걀) — 에이전트가 달 수
                # 있는 conflicts는 열린 사건 참조뿐이다 (설계 rev.3 §5)
                opened = open_cases(vault)
                errs.append(
                    f"conflicts는 열린 사건 번호(CASE-<연도>-<일련>)만 표면에서 달 수 "
                    f"있다: {t} — 지금 열린 사건: "
                    f"{', '.join(opened) if opened else '없음'} "
                    f"(노드 간 존치 상호 치환은 판결 적용 절차의 일이다)")
    return errs


def validate_render(vault: Vault, path: Path, meta: dict, body: str,
                     idx) -> tuple[bytes, list[str]]:
    """**렌더된 바이트를 되읽어** 검증한다 — 쓰려던 것이 아니라 쓸 것을
    검증해야 한다(7차 중대 A). 직렬화가 깨뜨린 것은 dict 검사로는 안 보인다."""
    data = nodefile.render(meta, body)
    fd, tmp = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            back = contract.parse(Path(tmp))
        except (OSError, ValueError) as e:
            return data, [f"직렬화 왕복 실패 — 쓰지 않았다: {e}"]
        # 되읽은 노드에 **목적지 경로**를 씌운다 — contract.validate가 stem을 보므로
        # 임시 파일명을 그대로 두면 stem에 걸린 규칙이 통째로 무력화된다(7차 계열)
        back = contract.Node(path=path, meta=back.meta, body=back.body,
                             fm_keys=back.fm_keys)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # 왕복 비교는 **논리값**으로 한다 — Predicate Edge의 단일 원소 리스트
    # `[x]`와 스칼라 `x`는 대상이 하나로 같다. _edge_value가 단일 목록을
    # 맨값으로 접으므로, 표현 차이(리스트↔스칼라)를 불일치로 오판하면 손으로
    # 쓴 `derived-from: [<id>]` 노드가 무관한 갱신마다 거부된다.
    def _norm(m):
        out = {}
        for k, v in m.items():
            if k in contract.PREDICATES and isinstance(v, list) and len(v) == 1:
                v = v[0]
            out[k] = str(v)
        return out
    if _norm(back.meta) != _norm(meta):
        return data, [f"직렬화 왕복 불일치 — 쓰지 않았다: {sorted(back.meta)} vs {sorted(meta)}"]
    return data, _validate_node(vault, path, back, body, idx)


def _validate_node(vault: Vault, path: Path, node, body: str, idx) -> list[str]:
    """계약 + 그 노드에서 **나가는 참조**의 위상. 전역 검사는 하지 않는다 —
    남이 만든 위반 때문에 내 쓰기가 막히면 안 된다 (설계 D10)."""
    errs = list(contract.validate(node))
    kind = placement.space_of(vault, path)
    for pred in contract.PREDICATES:
        for t in node.edges(pred):
            errs += topology_of(vault, idx, kind, path.stem, t, pred, node.id)
    for t in node.wikilinks():
        errs += topology_of(vault, idx, kind, path.stem, t, None, node.id)
    for t in evidence.coordinates(body):
        errs += topology_of(vault, idx, kind, path.stem, t, rechecks.EVIDENCE, node.id)
    return errs


def topology_of(vault: Vault, idx, kind, stem, name, pred, node_id=None) -> list[str]:
    if pred == rechecks.EVIDENCE:
        # 근거 절의 좌표도 참조다(헌법 제8조 2·3항). 인용 기록은 모양으로 가린다 — 줄인 좌표는
        # 경로가 아니라 색인이 해석하지 못한다. vault의 비노드 파일은 Link와 같은 규칙을 지나고,
        # URL·외부 루트는 vault 밖이다.
        k = evidence.kind(name)
        if k == "round":
            return ([f"Domain의 인용 기록 직접 참조(근거 절): {stem} → {name} — 근거는 노드로 "
                     f"증류해 derived-from으로 참조한다(헌법 8조 3항)"] if kind[0] == "domain" else [])
        if k != "path":
            return []
        name = name.split("#", 1)[0].strip()
    if pred == "conflicts":
        # 사건 표지는 참조 위상의 예외다(헌법 8조). 표면에서는 열린 사건에
        # 당사자로서 다는 것만 허용한다 — 전역 topology_check와 같은 의미론.
        case = graph.load_cases(vault).get(name)
        if not case:
            return [f"실재하지 않는 사건 참조: {stem} → {name}"]
        if str(case.get("status")) != "docketed":
            return [f"열린 사건이 아니다: {stem} → {name} ({case.get('status')})"]
        if node_id not in [str(x) for x in (case.get("parties") or [])]:
            return [f"그 사건의 당사자가 아니다: {stem} → {name} (헌법 12조 5항)"]
        return []
    r = idx.resolve(name)
    if r[0] in ("external", "dangling"):
        # 대상이 아직 없어도 **경로가 소속을 말한다.** 헌법 8조 3항의 금지는
        # 파일의 실재를 조건으로 걸지 않는데, 구판은 dangling을 먼저 걸러
        # "없는 `_raw` 라운드"를 Domain 노드에 다는 것이 통과했다(그 파일이
        # 생기는 순간 검증기 FAIL이 된다).
        if r[0] == "dangling" and kind[0] == "domain" and placement.is_record_ref(str(name)):
            return [f"Domain의 인용 기록 직접 참조: {stem} → {name} — 대상이 아직 "
                    f"없어도 그 자리는 인용 기록 구획이다. 근거는 노드로 증류해 "
                    f"참조한다(헌법 8조 3항)"]
        return []                       # dangling은 경고이지 위반이 아니다
    if r[0] == "ambiguous":
        return [f"모호 참조(같은 이름 또는 같은 id의 노드가 여럿): {stem} → {name}"]
    tkind = r[1]
    # 작업 상태는 **소속과 무관하게** 근거가 되지 못한다 — Workbench 계약 4.2의
    # 주어에는 소속 제한이 없다("Workbench의 작업 상태(비노드)는 근거 또는
    # 권위의 출처로 참조하지 않는다"). 구판은 이 검사가 아래 `scope` 갈래 **안**에
    # 있어 Domain·Person 노드가 작업 상태·scope 기억을 근거로 걸 수 있었다.
    if tkind[0] == "workbench":
        return [f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} — {WORKBENCH_STATE}"]
    if kind[0] == "domain" and tkind[0] == "raw":
        return [f"Domain의 인용 기록 직접 참조: {stem} → {name}"]
    if kind[0] == "scope":
        # 비노드(인용 기록·원자료·대장)에는 소속 제약이 없다(헌법 8조 3항, 2026-10-13 개정).
        # 대장은 Workbench 아래에 있어도 작업 상태가 아니라 비노드 제도 기록이다(Workbench 계약 2.3)
        ok = ((tkind[0] == "scope" and tkind[1] == kind[1])
              or tkind[0] in ("raw", "domain", "person", "workbench-transit",
                              "sources", "governance", "ledger"))
        if not ok:
            # (작업 상태 갈래는 소속과 무관하므로 위로 올라갔다.)
            # 무엇을 하라는 말이 없으면 받는 쪽이 scope를 바꿔 보다 두 번
            # 거부당한다(표면 감사 실측). 옮기는 중이면 순서가 답이다 —
            # 노드를 먼저 옮기고 그 다음에 잇는다. 아니면 domain 경유다.
            return [f"scope 간 직접 참조: [{kind[1]}] {stem} → {name} {tkind} — "
                    f"옮기는 중이면 노드를 먼저 옮기고 그 다음에 이어라. "
                    f"scope를 넘어 공유할 지식이면 domain 노드로 증류해 경유한다"
                    f"(헌법 8조 3항)"]
    return []


def dangling_of(vault: Vault, path: Path, meta: dict, body: str, idx) -> list[str]:
    """그 노드의 미해석 참조 — 위반이 아니라 경고. 응답에 실어 에이전트가
    조용히 dangling을 쌓지 않게 한다(`list_nodes` 제거의 부작용 차단)."""
    node = meta if isinstance(meta, contract.Node) else\
        contract.Node(path=path, meta=meta, body=body)
    return sorted({r["ref"].split("#", 1)[0] for r in topology.reference_review(vault, node, idx)
                   if r["resolution"] == "dangling"})


CLAIM_GUIDANCE = (
    "기존 노드 갱신은 같은 주장과 적용 조건을 정정할 때 우선한다. 같은 프로젝트·절차의 다음 단계라는 "
    "이유만으로 한 노드에 합치지 않는다. 독립적으로 검증·재사용할 결론은 기존 또는 새 노드에 보존한다. "
    "노드의 분할은 대상에 결론·조건·근거를 먼저 보존하고 되읽은 뒤 허브 양쪽을 연결하고, 마지막에 원문의 "
    "해당 구간만 짧은 결론과 링크로 접는다. 사실과 변경 이유를 보존하되 중복 문단을 영구히 쌓지 않는다. "
)


def size_feedback(path, body: str) -> dict:
    """Reading-cost hint, not a storage cap or permission to discard knowledge."""
    hub = placement.is_hub(path)
    # ponytail: fixed triage thresholds; calibrate from review costs if they become noisy.
    if len(body) <= (6000 if hub else 12000):
        return {}
    return {"organization_advice": {
        "body_chars": len(body), "hub": hub, "read_view": "outline",
        "instruction": CLAIM_GUIDANCE + "길이는 삭제·강제 분할의 근거가 아니다. 목차와 필요한 절부터 읽고, "
                       "고칠 자리는 read_node(view=고칠 문장)으로 앞뒤 문단째 받아 그 body를 old_text로 고친다. "
                       "현재 결론·적용 조건·근거를 한 곳에 유지하라. 단계별 실행 일지를 계속 덧붙이지 말고 "
                       "레포의 실행 상세는 레포 기록을 인용하라. 허브에는 탐색 경로를 남기고 "
                       "독립적으로 재사용되는 주장만 기존 또는 별도 노드로 증류하라. "
                       "정정 전후의 판단과 출처는 보존하며 미검토 부분은 보류한다."}}


def edge_report(node) -> dict:
    """응답의 엣지 — 저장된 근거를 **좌표째** 싣는다. `Node.edges`는 해석용이라
    raw 라운드 앵커를 버린다. 그래서 한 기록의 다른 라운드들이 같은 경로로
    되풀려 보였다(2026-09-25 실측: `#42`·`#34`·`#41`이 같은 경로 셋으로 보고됐다)."""
    refs = node.references()
    return {p: [r for rel, r in refs if rel == p] for p in contract.PREDICATES}


def reference_feedback(vault: Vault, path, meta, body, idx, previous=None) -> dict:
    node = contract.Node(path=path, meta=meta, body=body)
    refs = topology.reference_review(vault, node, idx, previous)
    out = {"dangling": sorted({r["ref"].split("#", 1)[0] for r in refs
                               if r["resolution"] == "dangling"})}
    if refs:
        out["reference_review"] = {
            "status": "pending", "items": refs,
            "instruction": "내용은 저장됐다. 참조 검토는 미결이다. 노드 근거는 search/read_node의 정확한 제목으로 "
                           "derived-from에, 비노드 근거는 관측 절의 좌표로 적는다 — 인용 라운드는 coordinate, "
                           "vault 원료는 루트 기준 경로, 외부 루트는 별칭 좌표, 그 밖은 확인한 URL이다. "
                           "원료는 지식 노드나 허브가 아니다. 빈 가짜 노드를 만들지 말라. "
                           "organization plan --scope <scope>가 다음 검토로 이어받는다."}
    return {**out, **size_feedback(path, body)}


def cas(path: Path, expect_hash: str | None, body_given: bool) -> None:
    """CAS는 **본문 전체 치환**에 결속한다 (Mechanism §6-2 4항). 서명이
    폐지됐으므로 부분 변경(엣지 델타·summary)에는 expect_hash를 요구하지
    않는다 — 보호영역의 승인/반려는 별도 표면(대화형 단말)의 일이다.
    거부 응답에 현재 해시를 담지 않는다 — 관측 증명이 연극이 되지 않게."""
    if expect_hash:
        # 응답은 언제나 `sha256:<hex>`를 주지만, 접두를 떼어 보내는 호출자가
        # 있다. 그대로 비교하면 "그 사이 노드가 변경됐다"는 **거짓 진단**이 되어
        # 멀쩡한 상태를 다시 읽게 만든다 — 값이 같은지는 여전히 그대로 본다.
        expect_hash = expect_hash.strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", expect_hash):
            expect_hash = "sha256:" + expect_hash.lower()
    if body_given and not expect_hash:
        # **값을 주지 않는다.** 주면 읽지 않고도 통과할 수 있어 "해시는 관측의
        # 증거"(Mechanism §6-2 4항)가 연극이 된다 — 실제로 거부문의 해시를 그대로
        # 되보내는 것으로 전문 덮기가 성립했다.
        #
        # 구판이 값을 실은 것은 표면 감사의 지적("이름만 주고 값을 안 주면
        # `read_node`로만 빠져나올 수 있는데 그 도구는 그걸 금한다") 때문이었다.
        # 그 곤경은 실재했으나, v3.10.0이 **앵커 편집**을 열어 답이 달라졌다 —
        # 부분 편집이면 해시가 아예 필요 없다. 전문을 통째로 갈아야 하는 경우는
        # 읽는 것이 옳고, 그때의 `read_node`는 "해시만 알려고" 부르는 것이 아니다.
        raise WriteError(
            "본문 전체 치환에는 읽은 상태의 해시(expect_hash)가 필요하다 — "
            "쓰지 않았다",
            ["고칠 자리가 정해져 있으면 `old_text`/`new_text` 앵커 편집을 쓰라 "
             "— 해시가 필요 없다. `read_node(view=고칠 문장)`이 그 자리를 앞뒤 문단째 "
             "주고, 그 `body`가 곧 `old_text`다. 전문을 통째로 갈아야 하면 `read_node`로 지금 "
             "본문을 읽고 그 응답의 `hash`를 그대로 넣어라."])
    if expect_hash and sha256_file(path) != expect_hash:
        raise WriteError(
            "그 사이 노드가 변경됐다 — 다시 읽고 재시도하라 (CAS 불일치)")
