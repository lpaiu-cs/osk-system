"""osk.contract — 노드 계약의 파싱과 검증.

구현 근거: 시행령 §1(필수 6필드·summary 80자 무링크·PE 표기),
Mechanism §2(id·시각 형식·drafter 단수·모델명·필드 순서).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .core import ID_RE, TS_RE

def target_stem(name: str) -> str:
    """PE·Link 대상명의 **동일성 키**. 경로형 `[[= Scope/W1/N]]`과 스템형
    `[[N]]`은 같은 대상이므로 마지막 요소의 stem으로 접는다 — 표기 차이가
    중복 등재나 무효한 제거로 새지 않게 한다."""
    s = str(name).strip().rstrip("/").split("/")[-1]
    return s[:-3] if s.endswith(".md") else s


REQUIRED = ["id", "created", "updated", "author", "drafter", "summary"]
PREDICATES = ["derived-from", "conflicts"]  # 헌법 8조 5항 (2술어 체제)
ORDER = REQUIRED  # Mechanism §2 5항 — PE는 그 뒤 상호 순서 무관


@dataclass
class Node:
    path: Path
    meta: dict
    body: str
    fm_keys: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    def edges(self, predicate: str) -> list[str]:
        """PE 대상 목록. 두 표기를 함께 읽는다(시행령 §1 3항 · Mechanism §8 2항):
        노드 대상은 `id`를 **그대로**(`derived-from: 260802-114u-7lo3`),
        비노드 대상과 `conflicts`는 위키링크(`"[[경로#제목]]"`)로 쓴다.
        위키링크는 대상명(경로·stem)만, id형은 스칼라 전체를 돌려준다 —
        소비자(graph.resolve)가 id는 id로, 그 밖은 경로/stem으로 해석한다."""
        v = self.meta.get(predicate)
        if v is None:
            return []
        vals = v if isinstance(v, list) else [v]
        out = []
        for x in vals:
            s = str(x).strip()
            m = re.search(r"\[\[([^\]#|]+)", s)
            if m:
                out.append(m.group(1).strip())   # 비노드/사건 — 위키링크 대상명
            elif s:
                out.append(s)                     # 노드 대상 — id 그대로
        return out

    def wikilinks(self) -> list[str]:
        """본문 Link(임베드 포함). 코드 구획(``` 펜스·인라인 백틱) 안의
        [[...]] 예시는 Link가 아니다 — 제외한다. 펜스의 경계는 **행 시작**의
        ```뿐이다(행 중간의 ```는 인라인 코드일 뿐이며, 이것과 짝지으면 실제
        Link가 가려지거나 예시 Link가 산입된다). 닫히지 않은 펜스는 본문 끝까지
        코드로 본다."""
        body = re.sub(r"(?m)^ {0,3}```[\s\S]*?(?:^ {0,3}```[^\n]*$|\Z)", "", self.body)
        body = re.sub(r"`[^`\n]*`", "", body)
        return [m.group(1).strip()
                for m in re.finditer(r"!?\[\[([^\]#|]+)", body)]


class _StrictLoader(yaml.SafeLoader):
    """중복 키를 last-wins로 삼키지 않는 로더 — `conflicts`를 두 번 쓰면
    앞 엣지가 조용히 사라진다. frontmatter의 중복 키는 오류다."""


def _no_dup_keys(loader, node, deep=False):
    out = {}
    for k, v in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in out:
            raise yaml.constructor.ConstructorError(
                None, None, f"frontmatter 중복 키: {key}", k.start_mark)
        out[key] = loader.construct_object(v, deep=deep)
    return out


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_keys)


def parse(path: Path | str) -> Node:
    p = Path(path)
    t = p.read_text(encoding="utf-8")
    if not t.startswith("---\n"):
        raise ValueError(f"frontmatter 없음: {p}")
    end = t.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"frontmatter 미종결: {p}")
    fm_text = t[4:end]
    try:
        meta = yaml.load(fm_text, Loader=_StrictLoader) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter 파싱 실패: {p} — {e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"frontmatter가 매핑이 아님: {p}")
    keys = [ln.split(":")[0] for ln in fm_text.split("\n")
            if ln and not ln.startswith((" ", "-", "#"))]
    return Node(path=p, meta=meta, body=t[end + 5:], fm_keys=keys)


def validate(node: Node) -> list[str]:
    """계약 위반 목록. 빈 목록 = 통과."""
    errs = []
    m = node.meta
    for k in REQUIRED:
        if k not in m:
            errs.append(f"필수 필드 누락: {k}")
    if errs:
        return errs
    if node.fm_keys[:6] != ORDER:
        errs.append(f"필드 순서 위반: {node.fm_keys[:6]}")
    for k in node.fm_keys[6:]:
        if k not in PREDICATES:
            errs.append(f"계약 외 필드: {k}")
    if not re.match(ID_RE, str(m["id"])):
        errs.append(f"id 형식 위반: {m['id']}")
    for k in ("created", "updated"):
        if not re.match(TS_RE, str(m[k])):
            errs.append(f"{k} 시각 형식 위반: {m[k]}")
    ca = str(m["created"])
    if str(m["id"])[:6] != ca[2:4] + ca[5:7] + ca[8:10]:
        errs.append(f"id·created 시간 정합 위반: {m['id']} vs {ca[:10]}")
    s = m.get("summary")
    if s is None or not str(s).strip():
        errs.append("summary 부재·공백 (내용을 한 줄로 축약해야 한다)")
    else:
        s = str(s)
        if len(s) > 80:
            errs.append(f"summary {len(s)}자 (한도 80)")
        if "[[" in s:
            errs.append("summary에 Link·Predicate Edge 금지")
        if "\n" in s:
            errs.append("summary는 물리적 한 줄이어야 한다")
    if isinstance(m.get("drafter"), list):
        errs.append("drafter는 대표 기초자 하나(단수)")
    dr = str(m.get("drafter", ""))
    if ":" in dr:
        errs.append(f"drafter는 접두 없는 모델명: {dr} (Mechanism §2 4항)")
    elif dr in ("claude", "codex", "claude-code", "gemini-cli"):
        errs.append(f"drafter는 하네스명이 아니라 모델명: {dr}")
    stem = node.path.stem
    sid = str(m["id"])
    for pred in PREDICATES:
        for t in node.edges(pred):
            ts = t.strip().rstrip("/").split("/")[-1]
            ts = ts[:-3] if ts.endswith(".md") else ts
            if ts == stem or t == sid:   # id형·경로형 어느 표기든 자기 참조 적발
                errs.append(f"{pred}가 자기 자신을 가리킨다")  # 근거·충돌은 남과의 관계다
    if node.fm_keys and len(node.fm_keys) != len(set(node.fm_keys)):
        errs.append(f"frontmatter 중복 필드: {node.fm_keys}")
    return errs
