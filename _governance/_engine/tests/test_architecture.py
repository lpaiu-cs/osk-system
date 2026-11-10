"""구조 규범의 기준선 시험 — docs/CODE-STANDARD.md의 1·2·3·7·9항을 `ast`로 잰다.

엔진을 import하지 않는다(표준 라이브러리만) — 엔진이 깨져 있어도 돈다.

재는 것은 다섯이다.
- 층(1항): 아래층이 위층을 import하는 줄.
- 지연 import(2항): 진입점(surface) 밖에서 함수 안에 둔 엔진 내부 import, 그리고 그것까지 넣었을 때의 순환.
- 밑줄 접근(3항): 다른 모듈의 `_이름`을 쓰는 곳.
- 넓은 예외(7항): `except Exception`·`except BaseException`·맨 `except`.
- 함수 크기(9항): 본문 60줄 또는 분기 15를 넘는 함수.

지금의 위반은 `architecture_baseline.json`에 적혀 있고, 시험은 **실측이 기준선과 같을 때만** 통과한다.
위반이 늘면 코드를 고치고, 줄면 기준선을 낮춘다 — 낮추지 않으면 그만큼의 여유로 다음 위반이 숨는다.

    python tests/test_architecture.py            # 대조. 다르면 exit 1
    python tests/test_architecture.py --update   # 기준선을 지금 상태로. 합계가 느는 갱신은 거부한다
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("architecture_baseline.json")
FUNCTION_LINES, FUNCTION_BRANCHES = 60, 15

KERNEL, MODEL, STORE, SERVICE, SURFACE = range(5)
# 모듈의 층 — 위층만 아래층을 import한다. 새 모듈은 여기에 올려야 시험이 돈다.
# 한 모듈에 책임이 섞인 것(`write`·`evidence`·`rechecks`)은 갈 곳이 아니라 지금 주로 하는 일로 배정했다.
LAYERS = {
    KERNEL: ["osk", "osk._portalock", "osk.epoch", "osk.layout", "osk.core", "osk.errors"],
    MODEL: ["osk.contract", "osk.naming", "osk.prose", "osk.secrets", "osk.raw_view"],
    STORE: ["osk.graph", "osk.raw", "osk.evidence", "osk.roots", "osk.workspaces", "osk.signatures", "osk.routing",
            "osk.approvals", "osk.scope_memory", "osk.evictions", "osk.rechecks", "osk.transcripts",
            "osk.native_cli", "osk.harness", "osk.harness.base", "osk.harness.runs", "osk.harness.claude",
            "osk.harness.codex", "osk.harness.kiro", "osk.harness.antigravity"],
    SERVICE: ["osk.write", "osk.search", "osk.authority", "osk.organization", "osk.distillation",
              "osk.validate", "osk.integration", "osk.growth", "osk.growth_daily", "osk.growth_status",
              "osk.response_growth", "osk.update", "osk.release", "osk.publish", "osk.update_check",
              "osk.setup", "osk.services", "osk.doctor", "osk.evidence_migration", "osk.hook_text", "osk.sync_status",
              "osk._fixture_approvals", "vault_sync"],
    SURFACE: ["osk.cli", "mcp_server", "sync_daemon", "scripts.growth_run", "scripts.setup",
              "scripts.recover", "scripts.osk_turn_ledger", "scripts.configure_obsidian_graph",
              "scripts.hooks.claude_session_start", "scripts.hooks.claude_prompt_submit",
              "scripts.hooks.capture_stop"],
}
LAYER_OF = {name: layer for layer, names in LAYERS.items() for name in names}
BROAD = {"Exception", "BaseException"}
# 합계는 모두 "작을수록 좋다" — `--update`는 이 가운데 하나라도 느는 갱신을 거부한다.
# `tangled_modules`는 여기 없다: 순환에 든 모듈을 둘로 가르면 의존은 그대로인데 수가 는다. 기준선과
# 같은지는 보되 느는 갱신을 막지는 않는다. 끝의 기준(0)은 같다.
TOTALS = ("upward_imports", "lazy_imports", "two_module_cycles", "private_access",
          "broad_except", "oversize_functions", "excess_lines", "excess_branches")


def sources() -> dict[str, str]:
    """제품 코드의 모듈 이름 → 소스. 시험은 세지 않는다."""
    files = [*ENGINE.glob("*.py"), *(ENGINE / "osk").rglob("*.py"), *(ENGINE / "scripts").rglob("*.py")]
    out = {}
    for path in sorted(files):
        parts = path.relative_to(ENGINE).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        out[name] = path.read_text(encoding="utf-8")
    return out


def _package(name: str, is_package: bool) -> list[str]:
    parts = name.split(".")
    return parts if is_package else parts[:-1]


def _imports(name: str, tree: ast.Module, known: set[str], is_package: bool):
    """엔진 내부 import마다 `(대상 모듈, 함수 안인가, 줄, 묶인 이름 → 모듈, 가져온 밑줄 이름)`."""
    def inside_function(node, stack):
        return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for n in stack)

    def visit(node, stack):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name if alias.name in known else None
                if target:
                    bound = {alias.asname: target} if alias.asname else {alias.name.split(".")[0]: alias.name.split(".")[0]}
                    yield target, inside_function(node, stack), node.lineno, bound, []
        elif isinstance(node, ast.ImportFrom):
            base = _package(name, is_package)
            base = base[:len(base) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            for alias in node.names:
                sub = f"{module}.{alias.name}" if module else alias.name
                if sub in known:                       # `from . import write` — 모듈을 묶는다
                    yield sub, inside_function(node, stack), node.lineno, {alias.asname or alias.name: sub}, []
                elif module in known:                  # `from .write import X` — 이름을 가져온다
                    private = [alias.name] if alias.name.startswith("_") and not alias.name.startswith("__") else []
                    yield module, inside_function(node, stack), node.lineno, {}, private
        for child in ast.iter_child_nodes(node):
            yield from visit(child, [*stack, node])

    yield from visit(tree, [])


def _dotted(node) -> str | None:
    """`a.b.c` 꼴의 표현식을 점 이름으로. 그 밖은 None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _private_access(name: str, tree: ast.Module, bound: dict[str, str], known: set[str]) -> list[str]:
    """`모듈._이름` 접근 — 묶인 이름이 가리키는 엔진 모듈이 자기 자신이 아닐 때."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__")):
            continue
        base = _dotted(node.value)
        if not base:
            continue
        head, _, rest = base.partition(".")
        target = f"{bound[head]}.{rest}" if head in bound and rest else bound.get(head)
        if target in known and target != name:
            found.append(f"{node.lineno}: {target}.{node.attr}")
    return found


def _broad_except(tree: ast.Module) -> int:
    def broad(kind) -> bool:
        if kind is None:
            return True
        kinds = kind.elts if isinstance(kind, ast.Tuple) else [kind]
        return any(isinstance(k, ast.Name) and k.id in BROAD for k in kinds)
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and broad(n.type))


def _branches(fn) -> int:
    """함수 하나의 분기 수 — 안에 든 함수·클래스의 몫은 그쪽에서 센다."""
    count, stack = 0, list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.match_case)):
            count += 1
        elif isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            count += len(node.ifs)
        stack.extend(ast.iter_child_nodes(node))
    return count


def _functions(tree: ast.Module):
    """`(점 이름, 본문 줄 수, 분기 수)` — 본문은 docstring 다음 문장부터 센다."""
    def visit(node, path):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = child.body[1:] if ast.get_docstring(child, clean=False) is not None else child.body
                lines = child.end_lineno - body[0].lineno + 1 if body else 0
                yield ".".join([*path, child.name]), lines, _branches(child)
                yield from visit(child, [*path, child.name])
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, [*path, child.name])
            else:
                yield from visit(child, path)

    yield from visit(tree, [])


def _tangled(edges: dict[str, set[str]]) -> int:
    """순환에 든 모듈 수 — 서로 닿는 모듈이 둘 이상인 묶음(강결합 성분)의 크기 합."""
    def reach(start):
        seen, stack = set(), [start]
        while stack:
            for nxt in edges.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen
    closure = {m: reach(m) for m in edges}
    return sum(1 for m in edges if m in closure[m])


def measure(srcs: dict[str, str], layer_of: dict[str, int]) -> dict:
    """소스 묶음의 구조 실측 — 기준선 파일과 같은 모양."""
    known = set(srcs)
    upward, lazy, private, broad, oversize = {}, {}, {}, {}, {}
    edges = {m: set() for m in known}
    for name, text in srcs.items():
        tree = ast.parse(text)
        bound, imported_private, counted = {}, 0, set()
        for target, in_function, line, names, privates in _imports(name, tree, known, _is_package(name, srcs)):
            bound.update(names)
            if target == name:
                continue
            imported_private += len(privates)
            edges[name].add(target)
            if (line, target) in counted:              # 한 import 문은 이름을 여럿 가져와도 한 번 센다
                continue
            counted.add((line, target))
            if layer_of[target] > layer_of[name]:
                upward[f"{name} -> {target}"] = upward.get(f"{name} -> {target}", 0) + 1
            if in_function and layer_of[name] != SURFACE:
                lazy[name] = lazy.get(name, 0) + 1
        hits = len(_private_access(name, tree, bound, known)) + imported_private
        if hits:
            private[name] = hits
        if n := _broad_except(tree):
            broad[name] = n
        for qualname, lines, branches in _functions(tree):
            if lines > FUNCTION_LINES or branches > FUNCTION_BRANCHES:
                key = f"{name}:{qualname}"
                while key in oversize:                 # 같은 이름을 두 번 정의한 모듈
                    key += "'"
                oversize[key] = [lines, branches]
    cycles = sorted(f"{a} <-> {b}" for a in edges for b in edges[a] if a < b and a in edges[b])
    report = {"upward_imports": upward, "lazy_imports": lazy, "two_module_cycles": cycles,
              "tangled_modules": _tangled(edges), "private_access": private, "broad_except": broad,
              "oversize_functions": oversize}
    return {"limits": {"function_lines": FUNCTION_LINES, "function_branches": FUNCTION_BRANCHES},
            "totals": totals(report), **{k: _sorted(v) for k, v in report.items()}}


def _is_package(name: str, srcs: dict[str, str]) -> bool:
    return any(other.startswith(name + ".") for other in srcs)


def _sorted(value):
    return dict(sorted(value.items())) if isinstance(value, dict) else value


def totals(report: dict) -> dict:
    over = report["oversize_functions"].values()
    return {"upward_imports": sum(report["upward_imports"].values()),
            "lazy_imports": sum(report["lazy_imports"].values()),
            "two_module_cycles": len(report["two_module_cycles"]),
            "tangled_modules": report["tangled_modules"],
            "private_access": sum(report["private_access"].values()),
            "broad_except": sum(report["broad_except"].values()),
            "oversize_functions": len(over),
            "excess_lines": sum(max(0, lines - FUNCTION_LINES) for lines, _ in over),
            "excess_branches": sum(max(0, branches - FUNCTION_BRANCHES) for _, branches in over)}


def differences(actual: dict, baseline: dict) -> list[str]:
    """기준선과 다른 곳 — 사람이 읽는 줄로."""
    out = []
    for key in actual:
        now, was = actual[key], baseline.get(key)
        if now == was:
            continue
        if isinstance(now, dict) and isinstance(was, dict):
            out += [f"{key}: {k} {was.get(k, '없음')} → {now.get(k, '없음')}"
                    for k in sorted(set(now) | set(was)) if now.get(k) != was.get(k)]
        elif isinstance(now, list) and isinstance(was, list):
            out += [f"{key}: + {x}" for x in now if x not in was] + [f"{key}: - {x}" for x in was if x not in now]
        else:
            out.append(f"{key}: {was} → {now}")
    return out


def self_test() -> None:
    """자가 계량 — 재는 코드가 0만 세고 있지 않은지 작은 표본으로 본다."""
    big = "def big(x):\n" + "".join(f"    if x == {i}:\n        x += 1\n" for i in range(31)) + "    return x\n"
    sample = {
        "osk": "",
        "osk.low": "_hidden = 1\n\ndef f():\n    from . import high\n    return high.g()\n",
        "osk.high": ("from . import low\nfrom .low import _hidden\n\n"
                     "def g():\n    \"\"\"doc.\"\"\"\n    try:\n        return low._hidden\n"
                     "    except Exception:\n        return None\n\n" + big),
        "edge": "def main():\n    from osk import low\n    return low.f()\n",
    }
    got = measure(sample, {"osk": KERNEL, "osk.low": KERNEL, "osk.high": SERVICE, "edge": SURFACE})
    want = {"upward_imports": {"osk.low -> osk.high": 1}, "lazy_imports": {"osk.low": 1},
            "two_module_cycles": ["osk.high <-> osk.low"], "tangled_modules": 2,
            "private_access": {"osk.high": 2}, "broad_except": {"osk.high": 1},
            "oversize_functions": {"osk.high:big": [63, 31]}}
    for key, value in want.items():
        assert got[key] == value, f"자가 계량 실패 — {key}: {got[key]} != {value}"
    assert got["totals"]["excess_lines"] == 3 and got["totals"]["excess_branches"] == 16, got["totals"]


def main(argv: list[str]) -> int:
    self_test()
    srcs = sources()
    unplaced = sorted(set(srcs) - set(LAYER_OF))
    gone = sorted(set(LAYER_OF) - set(srcs))
    if unplaced or gone:
        print(f"층 배정표(LAYERS)가 코드와 다르다 — 배정 없음 {unplaced}, 없는 모듈 {gone}")
        return 1
    actual = measure(srcs, LAYER_OF)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    if "--update" in argv:
        grown = [k for k in TOTALS if baseline and actual["totals"][k] > baseline["totals"][k]]
        if grown:
            print("합계가 느는 갱신은 하지 않는다 — 코드를 고친다: "
                  + ", ".join(f"{k} {baseline['totals'][k]} → {actual['totals'][k]}" for k in grown))
            return 1
        BASELINE.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print("기준선을 썼다:", json.dumps(actual["totals"], ensure_ascii=False))
        return 0
    if baseline is None:
        print(f"기준선이 없다 — {BASELINE.name}. `--update`로 만든다")
        return 1
    diff = differences(actual, baseline)
    if not diff:
        print("구조 규범: 기준선과 같다", json.dumps(actual["totals"], ensure_ascii=False))
        return 0
    grown = [k for k in TOTALS if actual["totals"][k] > baseline["totals"][k]]
    print("\n".join(diff[:40]) + (f"\n… 외 {len(diff) - 40}줄" if len(diff) > 40 else ""))
    print("위반이 늘었다 — 코드를 고친다(docs/CODE-STANDARD.md): " + ", ".join(grown) if grown else
          "기준선이 낡았다 — `python tests/test_architecture.py --update`로 낮춘다")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
