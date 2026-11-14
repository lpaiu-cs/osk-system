"""회귀 수트의 한 갈래: 글의 형태 — 제목, 태그 방어, 코드 구획, 파서의 관문.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import hashlib, shutil, tempfile
from pathlib import Path
import yaml

from regression.support import _age_all, check, node_text, rmtree_force, ROOT, VAULT, _w
from osk import cli, core, graph, validate, contract, write, placement  # noqa: E402
import osk.signatures as S  # noqa: E402


# ── 16. 제목은 모든 기기에서 파일명·Link 대상이 될 수 있어야 한다 ────────
def test_portable_title():
    """제목이 곧 파일명이자 Link 대상이므로, 한 기기에서만 표현 가능한 이름은
    다른 기기의 체크아웃이나 Link 해소를 깨뜨린다. 검사 대상이 **원본 문자열**
    이어야 한다는 것도 여기서 고정한다 — `strip()` 뒤를 검사하면 후행 공백과
    양끝 제어문자가 검사를 통과한 뒤 파일명에 그대로 들어간다."""
    valid = ["정상 제목", "대괄호[있음", "괄호(있음)", "밑줄_있음", "점.중간.있음"]
    invalid = {
        "": "빈 제목", " ": "공백뿐", "foo ": "후행 공백", " foo": "선행 공백",
        "foo\t": "후행 탭", "foo\n": "후행 개행", "a\tb": "중간 제어문자",
        "a\u2028b": "줄 구분 문자", "a\u2029b": "문단 구분 문자", "a\x85b": "NEL",
        ".foo": "선행 점", "foo.": "후행 점",
        "foo/bar": "슬래시", "foo\\bar": "역슬래시", "foo:bar": "콜론",
        'foo"bar': "따옴표", "foo|bar": "파이프", "foo?bar": "물음표",
        "foo*bar": "별표", "foo<bar": "부등호", "foo#1": "샵(Link 절단)",
        "foo]bar": "닫는 대괄호(Link 절단)",
        "CON": "예약 장치명", "CON.md": "확장자 붙은 예약 장치명", "COM1": "예약 포트명",
        "COM¹": "예약 포트명(위첨자)", "LPT³.txt": "확장자 붙은 예약 포트명(위첨자)",
        "NUL.tar.gz": "다중 확장자 예약 장치명",
        "CONIN$": "콘솔 장치명", "CONOUT$.md": "확장자 붙은 콘솔 장치명",
        "COM1 .foo": "장치명 뒤 공백", "LPT1  .x": "장치명 뒤 공백 여러 개",
        "가" * 85: "UTF-8 258바이트(ext4 상한 초과)",
        "a" * 253: "ASCII 256바이트(ext4 상한 초과)",
    }
    valid += ["COMMENT", "CON2", "COM0", "가" * 84, "a" * 252]
    bad_ok = [t for t in valid if write.title_errors(t)]
    check("적격 제목은 통과한다", not bad_ok, bad_ok)
    missed = [f"{k!r}({why})" for k, why in invalid.items()
              if not write.title_errors(k)]
    check("부적격 제목은 전부 거부된다", not missed, missed)

    # 이식성 키 — 대소문자·유니코드 정규화만 다른 이름은 같은 경로다
    import unicodedata as _ud
    check("대소문자만 다른 이름은 같은 키",
          write.portable_name_key("Example") == write.portable_name_key("example"))
    check("NFC·NFD만 다른 이름은 같은 키",
          write.portable_name_key(_ud.normalize("NFC", "가나"))
          == write.portable_name_key(_ud.normalize("NFD", "가나")))
    check("서로 다른 이름은 다른 키",
          write.portable_name_key("Example") != write.portable_name_key("Exampl3"))

    # create_node가 대소문자 변종을 거부한다 (Linux에서만 나던 구멍)
    made = ROOT / "00_Scope/W1/Regr-Case.md"
    try:
        write.create_node(VAULT, title="Regr-Case", summary="이식성 시험", body="본문",
                          drafter="sonnet-5", space="00_Scope/W1")
        try:
            write.create_node(VAULT, title="regr-case", summary="충돌해야 한다", body="본문",
                              drafter="sonnet-5", space="00_Scope/W1")
            check("대소문자만 다른 형제 이름은 거부된다", False, "생성되어 버렸다")
        except write.WriteError as e:
            check("대소문자만 다른 형제 이름은 거부된다",
                  "Regr-Case" in str(e.violations), e.violations)
    finally:
        made.unlink(missing_ok=True)
        (ROOT / "00_Scope/W1/regr-case.md").unlink(missing_ok=True)


# ── 17. 경로 키는 기기 표기에 의존하지 않는다 (OS 무관 고정) ───────────
def test_posix_rel_is_os_independent():
    """`posix_rel`이 항상 슬래시 표기를 내는지 **실행 OS와 무관하게** 고정한다.

    pin·DENY 회귀는 실제 파일시스템을 쓰므로 Windows에서만 이 결함을 잡는다 —
    POSIX에서는 수정 전 코드도 슬래시를 내서 회귀가 처음부터 없었던 것처럼
    통과한다. 그래서 여기서는 Pure*Path로 두 표기를 직접 만들어 불변식 자체를
    못박는다. 이 시험은 파일시스템을 건드리지 않는다."""
    from pathlib import PureWindowsPath, PurePosixPath
    win = core.posix_rel(PureWindowsPath('C:\\vault\\00_Scope\\W2\\a.md'), PureWindowsPath('C:\\vault'))
    check("Windows 표기도 슬래시로 접힌다", win == "00_Scope/W2/a.md", win)
    pos = core.posix_rel(PurePosixPath("/vault/00_Scope/W2/a.md"),
                         PurePosixPath("/vault"))
    check("POSIX 표기는 그대로다", pos == "00_Scope/W2/a.md", pos)
    check("두 표기가 같은 키로 접힌다", win == pos, (win, pos))

    # 이 결함의 소비자 두 곳과 같은 형태로, 규칙 문자열 대조가 성립하는지
    d = core.posix_rel(PureWindowsPath('C:\\v\\_governance\\_engine\\osk\\x.py'), PureWindowsPath('C:\\v\\_governance'))
    check("DENY 조각이 걸린다", "_engine/" in d, d)
    pin = core.posix_rel(PureWindowsPath('C:\\v\\00_Scope\\W2'), PureWindowsPath('C:\\v')) + "/"
    check("pin 대상 표기와 일치한다", pin == "00_Scope/W2/", pin)


# ── 23. 옵시디언 태그 방어 (Mechanism §8 7항) ───────────────────────────────
def test_obsidian_tag_defense():
    """`#숫자`에 조사·가운뎃점이 직결되면 옵시디언 태그가 된다(순수 숫자만은
    태그가 아님). 거부 없이 공백 하나를 넣어 태그화만 끊는다 — 종결 문자와
    코드 구획은 불변, `_raw/`는 전사 보존이라 적용하지 않는다."""
    from osk import scope_memory as sm
    f = write.space_numeric_tags
    check("조사 직결은 띄운다", f("#1227은 문제") == "#1227 은 문제")
    check("가운뎃점도 띄운다", f("#1227·1228") == "#1227 ·1228")
    check("종결 문자는 불변",
          f("이슈 #2027, PR #114 적용(#2071)") == "이슈 #2027, PR #114 적용(#2071)")
    check("인라인 코드는 불변", f("`#1227은` 밖 #1227은") == "`#1227은` 밖 #1227 은")
    check("펜스 안은 불변",
          f("```\n#1227은\n```\n#1227은") == "```\n#1227은\n```\n#1227 은")
    check("raw anchor 불변", f("[[rec#3]] 근거") == "[[rec#3]] 근거")

    # 쓰기 경로 통합 — 저장된 본문이 이미 방어된 형태다
    r = _w(write.create_node, VAULT, "태그 방어 시험", "s", "이슈 #1227은 심각했다.",
           "fable-5", space="00_Scope/W1")
    check("생성 통과", r.get("ok"), r)
    stored = (ROOT / "00_Scope/W1/태그 방어 시험.md").read_text(encoding="utf-8")
    check("노드 본문에 방어 반영", "#1227 은 심각했다" in stored, stored[-80:])

    # scope 기억 경로
    r2 = _w(sm.replace, VAULT, "WTag", "- #1227·1228 병합 건", None, "00_Scope/W1")
    check("scope 기억 쓰기 통과", r2.get("ok"), r2)
    # 성공은 전문을 싣지 않으므로(§9-2 5항) 저장물을 되읽어 확인한다
    check("scope 기억 방어 반영",
          "#1227 ·1228" in _w(sm.read, VAULT, "WTag", "00_Scope/W1").get("text", ""), r2)

    # 제목 거부문 — 원인은 지목하되 대체 표기는 처방하지 않는다(사용자 결정)
    r3 = _w(write.create_node, VAULT, "PR#1 판정", "s", "본문", "fable-5",
            space="00_Scope/W1")
    check("제목 # 거부 유지", r3.get("ok") is False, r3)
    v = " ".join(r3.get("violations", []))
    check("거부가 원인을 지목", "링크로 가리킬 수" in v, v)
    check("거부가 대체 표기를 처방하지 않는다", "PR-1" not in v, v)


def test_code_regions_are_not_prose():
    """태그 방어·Link 추출·목차가 **같은 코드 판정**을 쓴다(v3.22.2). 구판은
    `~~~`·들여쓰기 코드·``이중 백틱``·```` 속 ```·목록 속 펜스에서 `#123abc`를
    `#123 abc`로 고쳐 썼고, 같은 자리의 `[[...]]` 예시를 Link로 세어 scope
    경계 거부와 검증기 FAIL을 불렀다 — 목차만 그곳을 코드로 봤다."""
    import mcp_server
    forms = {
        "tilde": "~~~\n{c}\n~~~",
        "indented": "    {c}",
        "double-tick": "x ``{c}`` y",
        "nested": "````markdown\n```\n{c}\n```\n````",
        "list-fence": "- item\n    ```\n    {c}\n    ```",
        "single-tick": "x `{c}` y",
        "backtick": "```\n{c}\n```",
    }
    xs = ROOT / "00_Scope/RegrXS"
    xs.mkdir(exist_ok=True)
    for stem, nid in (("RegrXS", "xshb"), ("regr-xs-target", "xstg")):
        (xs / f"{stem}.md").write_text(
            f'---\nid: "260801-zzzz-{nid}"\ncreated: "2026-08-01 00:00 (KST)"\n'
            'updated: "2026-08-01 00:00 (KST)"\nauthor: "user"\ndrafter: "user"\n'
            f'summary: "{stem}"\n---\n\n본문\n', encoding="utf-8", newline="\n")
    try:
        for label, form in forms.items():
            code = form.format(c="#123abc [[regr-code-example]]")
            body = f"본문 #9x 와 [[W1]].\n\n{code}\n"
            name = f"regr-code-{label}"
            r = _w(write.create_node, VAULT, name, "코드 구획", body, "fable-5",
                   space="00_Scope/W1")
            check(f"[{label}] 생성 통과", r.get("ok"), r)
            if not r.get("ok"):
                continue
            n = contract.parse(ROOT / r["path"])
            check(f"[{label}] 생성이 코드 바이트를 보존", code in n.body, n.body)
            check(f"[{label}] 글의 태그 방어는 그대로", "#9 x" in n.body, n.body)
            check(f"[{label}] 코드 속 [[..]]는 Link가 아니다",
                  n.wikilinks() == ["W1"], n.wikilinks())
            h = hashlib.sha256((ROOT / r["path"]).read_bytes()).hexdigest()
            u = _w(write.update_node, VAULT, name, body=body + "\n추가 #7y\n", expect_hash=h)
            n2 = contract.parse(ROOT / r["path"])
            check(f"[{label}] 본문 교체도 코드 바이트를 보존",
                  u.get("ok") and code in n2.body and "#7 y" in n2.body, (u, n2.body))
            r = _w(write.create_node, VAULT, f"regr-xs-{label}", "코드 구획",
                   "예:\n\n" + form.format(c="[[regr-xs-target]]") + "\n", "fable-5",
                   space="00_Scope/W1")
            check(f"[{label}] 코드 속 scope 밖 예시는 거부 사유가 아니다", r.get("ok"), r)

        # 외부 작성 노드 — 요약만 고쳐도 본문 전체가 다시 접히는 통로다
        def external(stem, nid, body):
            f = ROOT / f"00_Scope/W1/{stem}.md"
            f.write_text(f'---\nid: "260801-zzzz-{nid}"\ncreated: "2026-08-01 00:00 (KST)"\n'
                         'updated: "2026-08-01 00:00 (KST)"\nauthor: "user"\n'
                         'drafter: "user"\nsummary: "external"\n---\n\n' + body,
                         encoding="utf-8", newline="\n")
            return f
        ext_body = "메모.\n\n~~~\nrun #123abc\n~~~\n\n    ref #42x\n"
        ext = external("regr-code-ext", "cext", ext_body)
        u = _w(write.update_node, VAULT, "regr-code-ext", summary="요약만 고친다")
        check("요약 편집이 외부 노드의 코드를 고치지 않는다",
              u.get("ok") and ext.read_text(encoding="utf-8").endswith(ext_body),
              (u, ext.read_text(encoding="utf-8")))
        external("regr-code-xsext", "cxsx", "구문:\n\n~~~\n[[regr-xs-target]]\n~~~\n")
        rep = validate.run(VAULT, cli.published_tools)
        bad = [m for d in rep["fail"] for v in d.values()
               for m in (v if isinstance(v, list) else [v]) if "regr-code-" in str(m)]
        check("코드 속 scope 밖 예시로 검증기가 FAIL하지 않는다", not bad, bad)
        u = _w(write.update_node, VAULT, "regr-code-xsext", summary="무관한 요약 편집")
        check("코드 속 scope 밖 예시가 무관한 편집을 막지 않는다", u.get("ok"), u)

        # 목차도 같은 판정 — 코드 속 제목은 숨고, 글의 제목과 Link는 남는다
        toc = "# 앞\n\n~~~\n# 틸드\n~~~\n\n    # 들여쓰기\n\n- item\n    ```\n    # 목록\n    ```\n\n## 뒤\n"
        check("목차는 코드 속 제목을 숨긴다",
              [x["title"] for x in mcp_server._node_view(toc, "outline")["headings"]]
              == ["앞", "뒤"], mcp_server._node_view(toc, "outline"))
        check("문단 뒤 4칸 행은 코드가 아니다(문단을 끊지 못한다)",
              write.space_numeric_tags("글\n    #1x [[A]]") == "글\n    #1 x [[A]]"
              and contract.Node(ext, {}, "글\n    [[A]]").wikilinks() == ["A"])
        check("열고 닫는 백틱 수가 다르면 코드가 아니다",
              write.space_numeric_tags("``#1x` #2x") == "``#1 x` #2 x")
    finally:
        rmtree_force(xs)


def test_code_region_block_boundaries():
    """코드 판정의 블록 경계(v3.22.2 리뷰). `* * *`·`- - -` 주제 구분선을 목록
    항목으로 읽으면 유령 목록 들여쓰기가 남는다 — 그 안에서 연 펜스가 들여쓰지
    않은 코드 행에서 닫혀 `#123abc`가 `#123 abc`로 바뀌고(요약 편집에서도),
    진짜 닫는 펜스가 새 펜스를 열어 뒤의 Link가 사라졌다. 목록 항목 문단의
    게으른 연속행(들여쓰지 않은 이음 행)이 목록을 닫으면 빈 행 뒤 항목 안의
    4칸 문단을 코드로 읽어 Link를 놓친다 — scope 밖 직접 Link가 위상 검사
    없이 저장됐다."""
    import mcp_server
    ex = "#123abc [[regr-blk-example]]"
    cases = {
        "hr-fence": ("* * *\n\n  ```css\np { color: #123abc; }\n  ```\n\n[[W1]]\n",
                     "  ```css\np { color: #123abc; }\n  ```\n"),
        # 문단 바로 밑 `- - -`도 목록이 아니다 — 뒤의 빈 행 + 4칸은 코드
        "hr-under-para": (f"글\n- - -\n\n    {ex}\n\n[[W1]]\n", f"    {ex}\n"),
        # 목록 내용보다 덜 들여쓴 구분선은 목록을 닫는다
        "hr-ends-list": (f"- 항목\n* * *\n\n    {ex}\n\n[[W1]]\n", f"    {ex}\n"),
        # 게으른 연속행은 항목을 잇는다 — 빈 행 뒤 4칸은 항목 안의 문단이다
        "lazy": ("- 항목\n이어지는 설명\n\n    [[W1]]\n", ""),
        # 반례: 목록이 정말 끝나면(빈 행 뒤 덜 들여쓴 문단·제목·빈 항목) 4칸은 코드
        "list-ends": (f"- 항목\n\n글\n\n    {ex}\n\n[[W1]]\n", f"    {ex}\n"),
        "lazy-then-ends": (f"- 항목\n이어지는 설명\n\n글\n\n    {ex}\n\n[[W1]]\n",
                           f"    {ex}\n"),
        "heading-ends-list": (f"- 항목\n## 제목\n\n    {ex}\n\n[[W1]]\n", f"    {ex}\n"),
        "empty-item": (f"-\n이음 아님\n\n    {ex}\n\n[[W1]]\n", f"    {ex}\n"),
    }
    xb = ROOT / "00_Scope/RegrXB"
    xb.mkdir(exist_ok=True)
    for stem, nid in (("RegrXB", "xbhb"), ("regr-xb-target", "xbtg")):
        (xb / f"{stem}.md").write_text(
            f'---\nid: "260801-zzzz-{nid}"\ncreated: "2026-08-01 00:00 (KST)"\n'
            'updated: "2026-08-01 00:00 (KST)"\nauthor: "user"\ndrafter: "user"\n'
            f'summary: "{stem}"\n---\n\n본문\n', encoding="utf-8", newline="\n")
    try:
        for i, (label, (body, block)) in enumerate(cases.items()):
            name = f"regr-blk-{label}"
            r = _w(write.create_node, VAULT, name, "블록 경계", body, "fable-5", space="00_Scope/W1")
            check(f"[{label}] 생성 통과", r.get("ok"), r)
            if r.get("ok"):
                n = contract.parse(ROOT / r["path"])
                check(f"[{label}] 생성이 코드 바이트를 보존", block in n.body, n.body)
                check(f"[{label}] 글의 Link만 센다", n.wikilinks() == ["W1"], n.wikilinks())
            # 외부 작성 노드 — 요약 편집·본문 교체가 코드를 다시 접는 통로다
            f = ROOT / f"00_Scope/W1/{name}-ext.md"
            f.write_text(f'---\nid: "260801-zzzz-blk{i}"\ncreated: "2026-08-01 00:00 (KST)"\n'
                         'updated: "2026-08-01 00:00 (KST)"\nauthor: "user"\n'
                         'drafter: "user"\nsummary: "external"\n---\n\n' + body,
                         encoding="utf-8", newline="\n")
            u = _w(write.update_node, VAULT, f"{name}-ext", summary="요약만 고친다")
            check(f"[{label}] 요약 편집이 코드를 고치지 않는다",
                  u.get("ok") and f.read_text(encoding="utf-8").endswith(body),
                  (u, f.read_text(encoding="utf-8")))
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            u = _w(write.update_node, VAULT, f"{name}-ext", body=body + "\n추가 #7y\n", expect_hash=h)
            n = contract.parse(f)
            check(f"[{label}] 본문 교체도 코드 바이트를 보존",
                  u.get("ok") and block in n.body and "#7 y" in n.body, (u, n.body))
            check(f"[{label}] 본문 교체 뒤에도 글의 Link만 센다",
                  n.wikilinks() == ["W1"], n.wikilinks())
        check("목차도 게으른 연속행 뒤 항목 속 제목을 본다",
              [x["title"] for x in mcp_server._node_view(
                  "- 항목\n이어지는 설명\n\n    # 항목 속 제목\n", "outline")["headings"]]
              == ["항목 속 제목"])
        r = _w(write.create_node, VAULT, "regr-blk-xs-lazy", "블록 경계",
               "- 항목\n이어지는 설명\n\n    [[regr-xb-target]]\n", "fable-5",
               space="00_Scope/W1")
        check("게으른 연속행 뒤 항목 속 scope 밖 Link는 위상 검사가 거부한다",
              r.get("ok") is False and "regr-xb-target" in " ".join(r.get("violations", [])), r)
        r = _w(write.create_node, VAULT, "regr-blk-xs-code", "블록 경계",
               "- 항목\n\n글\n\n    [[regr-xb-target]]\n", "fable-5", space="00_Scope/W1")
        check("목록이 끝난 뒤 4칸 코드 속 scope 밖 예시는 거부 사유가 아니다", r.get("ok"), r)
    finally:
        rmtree_force(xb)
        # 공유 W1 군집을 되돌린다 — 남으면 허브 미직결 목록(20건 상한)에서
        # 뒤 시험의 노드를 밀어낸다
        for f in (ROOT / "00_Scope/W1").glob("regr-blk-*.md"):
            f.unlink()


def test_code_region_commonmark_rules():
    """코드 판정의 CommonMark 컨테이너·블록 규칙(v3.22.2 차등 퍼징). 공유 판정이
    구판보다 나빠진 자리들이다 — 글을 코드로 읽으면 `#123abc` 태그 방어가 빠지고
    Link가 사라지며(scope 경계 검사도 함께 빠진다), 코드를 글로 읽으면 코드
    바이트가 바뀌고 예시가 Link로 센다. 각 본문의 `#1ab [[X]]`는 모두 글이거나
    모두 코드다."""
    import mcp_server
    cases = {  # 이름: (본문, 글인가)
        # 인용(>)도 컨테이너다 — 빈 인용·HTML 블록 뒤 행은 게으르게 잇지 못한다
        "empty-quote-in-item": ("- >\nb\n    ~~~ #1ab [[X]]", True),
        "html-comment-ends-list": ("- 항목\n<!-- 주석 -->\n  ```\n#1ab [[X]]", False),
        "html-in-item": ("- <div>\n텍스트\n  ```\n#1ab [[X]]", False),
        "empty-quote": (">\n    #1ab [[X]]", False),
        "quote-lazy": ("> 인용\n    #1ab [[X]]", True),
        "quote-fence": ("> [!note]\n> ```\n> #1ab [[X]]\n> ```", False),
        "quote-fence-ends": ("> ```\n> 코드\n#1ab [[X]]", True),
        "quote-list-para": ("> - a\n>\n>     #1ab [[X]]", True),
        # 표지 뒤 5칸 이상이면 항목은 들여쓰기 코드로 시작한다 — 문단이 아니다
        "item-starts-code": ("-     code\nf\n  ```\n#1ab [[X]]\n  ```", False),
        # 문단을 끊는 첫 항목은 빈 항목·1 아닌 번호일 수 없다 — 문단의 글이다
        "ord2-under-para": ("글\n2. ~~~ #1ab [[X]]", True),
        "empty-under-para": ("글\n*\n      #1ab [[X]]", True),
        "ord2-phantom-fence": ("글\n2. b\n    ~~~\n    #1ab [[X]]\n    ~~~", True),
        "setext-dash": ("제목\n-\n  ```\n#1ab [[X]]\n  ```", False),
        # 반례: 형제 항목·인용 뒤 항목은 문단을 끊는 자리가 아니다
        "sibling-item": ("1. a\n2. b\n\n       #1ab [[X]]", False),
        "quote-then-ord2": ("> 인용\n2. 항목\n\n       #1ab [[X]]", False),
        # 빈 항목은 빈 행에서 닫히고, 빈 채로 시작하는 항목의 내용 열은 표지 뒤 1칸이다
        "empty-item-blank": ("-\n\n  ```\n#1ab [[X]]\n  ```", False),
        "blank-start-width": ("-    \n    #1ab [[X]]", True),
        # 빈 행은 공백·탭뿐이다(NBSP·전각 공백 행은 글), CRLF 본문도 같은 판정
        "nbsp-line": ("글\n \n    #1ab [[X]]", True),
        "ideographic-line": ("글\n　\n    #1ab [[X]]", True),
        "crlf": ("-\r\n  ~~~\r\n#1ab [[X]]", True),
        # 인라인 코드는 문단 안에서 행을 넘어 닫히고, `\``의 첫 백틱은 글자다
        "span-crosses-line": ("`#1ab [[X]]``\n`", False),
        "span-closes-next-line": ("`a\n`` `b` #1ab [[X]] ``", True),
        "escaped-tick": ("이슈는 \\``#1ab [[X]]`` 로 표기", True),
        "escaped-backslash": ("\\\\`#1ab [[X]]`", False),
        "span-stops-at-blank": ("`a\n\nb` #1ab [[X]]", True),
        "span-stops-at-item": ("- `a\n- b` #1ab [[X]]", True),
    }
    for label, (body, prose) in cases.items():
        tags = write.space_numeric_tags(body)
        links = contract.Node(Path("x.md"), {}, body.replace("\r\n", "\n")).wikilinks()
        check(f"[{label}] 태그 방어는 글에만",
              tags == (body.replace("#1ab", "#1 ab") if prose else body), tags)
        check(f"[{label}] 글의 Link만 센다", links == (["X"] if prose else []), links)
    outline = {"- 항목\n<div>\n    # 제목": [], "* >\n목\n    ```\n  # 제목": ["제목"],
               "3.     e\n]\n    ######": []}
    for body, want in outline.items():
        got = [h["title"] for h in mcp_server._node_view(body, "outline")["headings"]]
        check(f"목차 {body!r}", got == want, got)


def test_format_alignment():
    """엔진은 Mechanism이 정한 배치·표기대로 읽고 쓴다.

    무엇을 망가뜨리면 실패하는가:
      · Space 루트 바로 아래 파일을 노드로 보면 → 루트 파일 단언(§1 2항)
      · 비노드를 확장자 없는 이름으로만 색인하면 → 임베드 단언(§8 5항)
      · 허브를 `<이름>.md` 철자로만 찾으면 → 허브 단언(§1 4항)
      · 새 id 근거를 맨값으로 적으면 → 제목 위키링크 단언(§8 2항)
      · 검증기가 growth.jsonl을 빼면 → 성장 대장 단언(§3 2항)
      · `---`로 감싼 사건 헤더도 읽으면 → 사건 헤더 단언(§4 4항)"""
    rootfile = ROOT / VAULT.domain / "regr-rootfile.md"
    img = ROOT / "_sources" / "regr-diagram.png"
    hubdir = ROOT / VAULT.domain / "RegrHubCase"
    growth = VAULT.ledger / "growth.jsonl"
    gbefore = growth.read_bytes() if growth.exists() else None
    case = VAULT.ledger / "case" / "CASE-2026-9300.md"
    made = [ROOT / "00_Scope/W1/regr-fmt-src.md", ROOT / "00_Scope/W1/regr-fmt-use.md",
            ROOT / "00_Scope/W1/regr-fmt-doc.md.md", ROOT / "00_Scope/W1/regr-fmt-doc.md"]
    try:
        rootfile.write_text(node_text("260925-0000-rootfile"), encoding="utf-8")
        check("Space 루트 바로 아래 파일은 노드 자리가 아니다",
              placement.space_of(VAULT, rootfile)[0] == "support", placement.space_of(VAULT, rootfile))
        check("배치 검증기가 루트 바로 아래 노드형 파일을 보고한다",
              any("regr-rootfile.md" in v for v in placement.layout_violations(VAULT)))
        rootfile.unlink()

        img.parent.mkdir(exist_ok=True)
        img.write_bytes(b"\x89PNG")
        check("임베드는 확장자까지 쓴 파일 이름으로 해석된다",
              graph.Index(VAULT).resolve("regr-diagram.png")[0] == "nonnode")

        hubdir.mkdir(parents=True, exist_ok=True)
        check("허브 파일이 없는 군집", placement.hub_file(hubdir) is None)
        (hubdir / "RegrHubCase.MD").write_text(node_text("260925-0001-hubcase1"),
                                               encoding="utf-8")
        check("확장자 대소문자와 무관하게 허브를 찾는다",
              placement.hub_file(hubdir) is not None)
        shutil.rmtree(hubdir)

        ra = _w(write.create_node, VAULT, "regr-fmt-src", "s", "근거.", "fable-5",
                space="00_Scope/W1")
        rb = _w(write.create_node, VAULT, "regr-fmt-use", "s", "쓴다.", "fable-5",
                space="00_Scope/W1", edges={"derived-from": ra["id"]})
        text = made[1].read_text(encoding="utf-8")
        check("새 id 근거는 제목 위키링크로 적는다",
              rb.get("ok") and 'derived-from: "[[regr-fmt-src]]"' in text
              and ra["id"] not in text, text[:400])
        _w(write.update_node, VAULT, "regr-fmt-use", add_edges={"derived-from": ra["id"]})
        check("같은 근거를 id로 다시 더해도 한 번만 앉는다",
              made[1].read_text(encoding="utf-8").count("regr-fmt-src") == 1)
        # 제목에 든 `.md`는 제목의 일부다 — 해석된 노드의 제목이 근거의 동일성이다
        rd = _w(write.create_node, VAULT, "regr-fmt-doc.md", "s", "문서 이름.", "fable-5",
                space="00_Scope/W1")
        rp = _w(write.create_node, VAULT, "regr-fmt-doc", "s", "짧은 이름.", "fable-5",
                space="00_Scope/W1")
        _w(write.update_node, VAULT, "regr-fmt-use", add_edges={"derived-from": [rd["id"], rp["id"]]})
        got = str(contract.parse(made[1]).meta.get("derived-from"))
        check("`.md`로 끝나는 제목과 짧은 제목은 다른 근거로 남는다",
              "[[regr-fmt-doc.md]]" in got and "[[regr-fmt-doc]]" in got, got)
        _w(write.update_node, VAULT, "regr-fmt-use", remove_edges={"derived-from": rd["id"]})
        got = str(contract.parse(made[1]).meta.get("derived-from"))
        check("id로 빼면 그 노드의 제목 링크가 빠진다",
              "[[regr-fmt-doc.md]]" not in got and "[[regr-fmt-doc]]" in got, got)

        with open(growth, "a", encoding="utf-8") as f:
            f.write('{"kind": "plan"}\n')
        check("검증기가 growth.jsonl의 rid 손상을 보고한다",
              "growth.jsonl" in str(validate.run(VAULT, cli.published_tools)["fail"]))

        case.write_text("---\ncase_no: CASE-2026-9300\n---\n\n본문\n", encoding="utf-8")
        check("`---`로 감싼 사건 헤더는 읽지 않는다", S.parse_case(case) is None)
    finally:
        rootfile.unlink(missing_ok=True)
        img.unlink(missing_ok=True)
        shutil.rmtree(hubdir, ignore_errors=True)
        case.unlink(missing_ok=True)
        for p in made:
            p.unlink(missing_ok=True)
        if gbefore is None:
            growth.unlink(missing_ok=True)
        else:
            growth.write_bytes(gbefore)
        _age_all()


# ── 판독 계약: 깊이 가드와 탭 (v3.7.0) ──────────────────────────────────
def test_parse_guards():
    """C 로더로 바꾸면서 (a) 프로세스를 죽이는 입력과 (b) 거부되던 것이
    통과하게 되는 입력을 둘 다 막았는가."""
    d = Path(tempfile.mkdtemp(prefix="osk-parse-"))
    HEAD = "id: 260830-0000-0000\ncreated: 2026-08-30 00:00 (KST)\n"

    def parse(fm, name):
        f = d / f"{name}.md"
        f.write_text(f"---\n{fm}\n---\n본문\n", encoding="utf-8")
        try:
            contract.parse(f)
            return None
        except ValueError as e:
            return str(e)

    # (a) 깊이 — C 로더는 깊은 중첩에서 네이티브 스택 오버플로로 **프로세스를
    # 죽인다**(파이썬 예외가 아니라 종료코드 127). 로더에 닿기 전에 막는다.
    deep = parse(HEAD + "summary: " + "[" * 3000 + "]" * 3000, "deep")
    check("깊은 중첩은 로더에 닿기 전에 거부된다 — 프로세스가 살아 있다",
          deep is not None and "깊이" in deep, deep)
    check("거부가 상한을 밝힌다", deep and str(contract._MAX_FM_DEPTH) in deep, deep)
    check("실 vault 최대 깊이(3)는 통과",
          parse(HEAD + "summary: " + "[" * 3 + "]" * 3, "d3") is None)
    check("상한 바로 아래는 통과",
          parse(HEAD + "summary: " + "[" * contract._MAX_FM_DEPTH
                + "]" * contract._MAX_FM_DEPTH, "at") is None)
    # 블록 컬렉션은 괄호 없이 겹친다 — 같은 크래시다. 표지 사이가 탭이어도, 줄을 U+2028로 갈라도, 복합 키여도
    # 로더에 닿기 전에 막는다.
    for label, fm in (("블록 시퀀스", HEAD + "edges:\n  " + "- " * 30000 + "x"),
                      ("탭으로 띄운 표지", HEAD + "edges:\n  " + "-\t" * 30000 + "x"),
                      ("U+2028로 가른 줄", HEAD + "edges:\u2028  " + "- " * 30000 + "x"),
                      ("복합 키", HEAD + "? " * 30000 + "x"),
                      ("들여쓰기", HEAD + "a:\n" + " " * 30000 + "b: 1")):
        refused = parse(fm, "block")
        check(f"깊이 겹친 블록은 로더에 닿기 전에 거부된다 — {label}",
              refused is not None and "줄 머리" in refused and str(contract._MAX_FM_LEAD) in refused, (refused or "")[:120])
    check("목록을 든 평범한 frontmatter는 통과", parse(HEAD + "edges:\n  - a\n  - b:\n      - c", "list") is None)
    check("줄 머리의 상한까지는 로더가 읽는다",
          parse(HEAD + "edges:\n" + "- " * (contract._MAX_FM_LEAD // 2) + "x", "lead") is None)

    # (b) 탭 — 두 로더가 갈리는 것은 **평문 스칼라**뿐이다. 그 자리만 순수
    # 로더에게 맡겨 구판과 같은 판정을 받는다. 전부 거부하면 구판이 받아들이던
    # 노드가 새로 broken이 된다.
    check("평문 스칼라의 탭은 거부 — 구판과 같다",
          parse(HEAD + "summary: a\tb", "plain") is not None)
    for label, fm in (("겹따옴표", HEAD + 'summary: "a\tb"'),
                      ("홑따옴표", HEAD + "summary: 'a\tb'"),
                      ("블록 스칼라", HEAD + "summary: |-\n  a\tb"),
                      ("주석", HEAD + "summary: ok  # note\there")):
        check(f"탭이 {label} 안에 있으면 통과 — 구판이 받아들이던 것이다",
              parse(fm, f"tab-{abs(hash(label))}") is None)

    # 두 로더가 같은 규율을 갖는지 — 폴백 경로가 살아 있는지도 여기서 본다
    for L in contract._loaders():
        try:
            yaml.load("id: a\nid: b\n", Loader=L)
            check(f"{L.__name__}: 중복 키 거부", False, "통과해 버렸다")
        except yaml.YAMLError:
            check(f"{L.__name__}: 중복 키 거부", True)
    shutil.rmtree(d, ignore_errors=True)


def test_node_parse_raises_only_its_kinds():
    """노드 파일의 판독이 올리는 것은 둘뿐이다 — 읽지 못하면 `OSError`, 노드로 해석되지 않으면 `ValueError`.
    받는 쪽은 그 둘만 받는다(코드 규범 7항). 다른 형이 새면 사람이 고치거나 동기화로 들어온 파일 하나가 색인을
    만드는 프로세스를 죽인다."""
    hostile = {
        "frontmatter 없음": b"hello",
        "닫히지 않음": b"---\nid: x\n",
        "UTF-8이 아님": b"---\n\xff\n---\n",
        "YAML 문법": b"---\na: [1\n---\n",
        "중복 키": b"---\na: 1\na: 2\n---\n",
        "매핑이 아님": b"---\n- 1\n---\n",
        "깊은 흐름": b"---\na: " + b"[" * 300 + b"]" * 300 + b"\n---\n",
        "깊은 블록": b"---\na:\n  " + b"- " * 30000 + b"x\n---\n",
        "없는 날짜": b"---\nd: 2026-13-45\n---\n",
        "너무 큰 정수": b"---\nn: " + b"9" * 6000 + b"\n---\n",
        "파이썬 태그": b"---\na: !!python/object:os.system x\n---\n",
        "병합 키 오용": b"---\n<<: 1\n---\n",
        "평문 스칼라의 탭": b"---\na:\tb\n---\n",
        "NUL": b"---\na: \x00\n---\n",
        # 로더는 YAMLError가 아닌 것도 낸다 — 키가 될 수 없는 값은 중복 키 검사에서 TypeError, 태그에 맞지 않는
        # 값은 로더 안에서 AttributeError·KeyError다. 형을 세어 받으면 끝이 없다.
        "목록을 키로": b"---\n? [a, b]\n: v\n---\n",
        "매핑을 키로": b"---\n? {a: b}\n: v\n---\n",
        "제 자신을 담은 키": b"---\n? &k [*k]\n: v\n---\n",
        "날짜가 아닌 timestamp": b"---\na: !!timestamp not-a-date\n---\n",
        "참거짓이 아닌 bool": b"---\na: !!bool maybe\n---\n",
    }
    for label, data in hostile.items():
        try:
            contract.parse_bytes("hostile.md", data)
            kind = "parsed"
        except ValueError:
            kind = "ValueError"
        except Exception as e:                      # 이 시험이 잡으려는 것 — 계약 밖의 형
            kind = type(e).__name__
        check(f"해석되지 않는 노드는 ValueError다 — {label}", kind == "ValueError", kind)
    check("읽히는 노드는 읽힌다", contract.parse_bytes("ok.md", b"---\nid: x\n---\nbody\n").id == "x")
    with tempfile.TemporaryDirectory(prefix="osk-node-parse-") as td:
        try:
            contract.parse(Path(td) / "absent.md")
            kind = "parsed"
        except OSError:
            kind = "OSError"
        check("읽지 못한 노드는 OSError다", kind == "OSError", kind)
        # 사건 파일의 머리는 순수 로더로 읽는다 — 깊이 겹치면 RecursionError였다. 머리를 읽지 못한 것으로 본다.
        case = Path(td) / "CASE-2026-1.md"
        case.write_text("parties:\n  " + "- " * 3000 + "x\n\n본문\n", encoding="utf-8")
        try:
            header = S.parse_case(case)
            kind = "returned"
        except Exception as e:
            header, kind = None, type(e).__name__
        check("깊이 겹친 사건 머리는 읽지 못한 머리다", kind == "returned" and header is None, kind)
        case.write_text("status: !!bool maybe\n\n본문\n", encoding="utf-8")
        try:
            header = S.parse_case(case)
            kind = "returned"
        except Exception as e:
            header, kind = None, type(e).__name__
        check("태그에 맞지 않는 값이 든 사건 머리도 읽지 못한 머리다", kind == "returned" and header is None, kind)
    # 그런 파일 하나는 깨진 파일로 따로 서고, 색인은 선다.
    bad = ROOT / "00_Scope/W1/regr-key-list.md"
    try:
        bad.write_text("---\n? [a, b]\n: v\n---\n본문\n", encoding="utf-8")
        try:
            index = graph.Index(VAULT)
            broken, others = "regr-key-list" in index.broken, len(index.nodes)
            kind = "built"
        except Exception as e:
            broken, others, kind = False, 0, type(e).__name__
        check("키가 될 수 없는 값이 든 노드 하나가 색인을 죽이지 않는다", kind == "built" and broken and others > 0,
              (kind, broken, others))
    finally:
        bad.unlink(missing_ok=True)
