"""구조 규범의 기준선 시험 — docs/CODE-STANDARD.md의 1·2·3·4·7·9항을 `ast`로 잰다.

엔진을 import하지 않는다(표준 라이브러리만) — 엔진이 깨져 있어도 돈다.

재는 것은 여섯이다.
- 층(1항): 아래층이 위층을 import하는 줄.
- 지연 import(2항): 진입점(surface) 밖에서 함수 안에 둔 엔진 내부 import, 그리고 그것까지 넣었을 때의 순환.
- 밑줄 접근(3항): 다른 모듈의 `_이름`을 쓰는 곳.
- 저장소(4항): import할 때 저장소를 열어 고정하는 이름, 그리고 저장소가 필요한데 인자로 받지 않는 함수.
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
    KERNEL: ["osk", "osk._portalock", "osk.epoch", "osk.layout", "osk.vault", "osk.core", "osk.errors"],
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
TOTALS = ("upward_imports", "lazy_imports", "two_module_cycles", "private_access", "vault_fixed_names",
          "vault_unthreaded", "broad_except", "oversize_functions", "excess_lines", "excess_branches")


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


def _bindings(name: str, tree: ast.Module, known: set[str], is_package: bool) -> tuple[dict, dict]:
    """import가 묶은 이름 — `(이름 → 엔진 모듈, 이름 → (엔진 모듈, 그 모듈의 이름))`."""
    mods, names = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in known:
                    mods[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = _package(name, is_package)
            base = base[:len(base) - (node.level - 1)] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            for a in node.names:
                sub = f"{module}.{a.name}" if module else a.name
                if sub in known:
                    mods[a.asname or a.name] = sub
                elif module in known:
                    names[a.asname or a.name] = (module, a.name)
    return mods, names


def _named(module: str, node, mods: dict, names: dict, known: set[str], local: set[str], cls: str | None):
    """표현식이 가리키는 `(모듈, 이름)` — 엔진 밖이거나 지역 이름이면 None."""
    if isinstance(node, ast.Name):
        return None if node.id in local else names.get(node.id, (module, node.id))
    base = _dotted(node.value)
    if not base:
        return None
    if base in ("self", "cls"):
        return (module, f"{cls}.{node.attr}") if cls else None
    head, _, rest = base.partition(".")
    if head in local:
        return None
    target = f"{mods[head]}.{rest}" if head in mods and rest else mods.get(head)
    return (target, node.attr) if target in known else None


def _opens_vault(expr) -> bool:
    """저장소를 정하는 식 — `Vault.open()`·`Vault(...)`·`default_root()`를 부르거나 환경 변수
    `OSK_VAULT_ROOT`를 읽는다."""
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and n.value == "OSK_VAULT_ROOT":
            return True
        if isinstance(n, ast.Call):
            name = (_dotted(n.func) or "").split(".")
            if name[-1] in ("Vault", "default_root") or name[-2:] == ["Vault", "open"]:
                return True
    return False


def _takes_vault(fn, method: bool) -> bool:
    static = any((_dotted(d) or "").split(".")[-1] == "staticmethod" for d in fn.decorator_list)
    args = [*fn.args.posonlyargs, *fn.args.args][1 if method and not static else 0:]
    return bool(args) and args[0].arg == "vault"


def _passed_on(srcs: dict[str, str], trees: dict[str, ast.Module], bound: dict) -> dict:
    """다른 곳의 이름을 제 이름으로 다시 내놓는 것 — `(모듈, 이름) → (원래 모듈, 원래 이름)`. 모듈 머리의
    `from .m import f`와 모듈 본문의 `g = m.f`(튜플 대입 포함)다. `r.f()`가 실제로는 `m.f`임을 따라간다."""
    known = set(srcs)
    out = {}
    for module, tree in trees.items():
        mods, names = bound[module]
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                one = ast.Module(body=[node], type_ignores=[])
                for name, key in _bindings(module, one, known, _is_package(module, srcs))[1].items():
                    out[(module, name)] = key
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
                pairs = [(target, value)]
                if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
                    pairs = list(zip(target.elts, value.elts))
                for t, v in pairs:
                    if isinstance(t, ast.Name) and isinstance(v, (ast.Name, ast.Attribute)):
                        key = _named(module, v, mods, names, known, set(), None)
                        if key and key != (module, t.id):
                            out[(module, t.id)] = key
    return out


def _resolver(srcs: dict[str, str], trees: dict[str, ast.Module]):
    """표현식이 가리키는 `(모듈, 이름)`을 찾는 함수와 그 재료 — `(named, passed, bound)`.

    다른 모듈을 거친 이름을 끝까지 따라간다. `raw.graph.space_of`는 `raw`가 머리에서 import한 `graph`의 것이고
    (모듈 속성 사슬), `write.bind_session`은 `write`가 다시 내놓은 `routing`의 것이다(`_passed_on`).
    `named(module, node, local, cls, mods=None, names=None)` — 엔진 밖 파일(시험)은 제 import 표를 넘긴다."""
    known = set(srcs)
    bound = {m: _bindings(m, trees[m], known, _is_package(m, srcs)) for m in srcs}
    passed = _passed_on(srcs, trees, bound)
    attrs = {}                                 # (모듈, 이름) → 그 이름이 묶은 모듈. 모듈 머리의 import만 속성이 된다
    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                one = ast.Module(body=[node], type_ignores=[])
                for name, target in _bindings(module, one, known, _is_package(module, srcs))[0].items():
                    attrs[(module, name)] = target

    def named(module, node, local, cls, mods=None, names=None):
        if mods is None:
            mods, names = bound[module]
        key = _named(module, node, mods, names, known, local, cls)
        if key is None and isinstance(node, ast.Attribute):
            head, *rest = (_dotted(node.value) or "").split(".")
            target = mods.get(head) if rest and head not in local else None
            for part in rest:
                if target is None:
                    break
                target = f"{target}.{part}" if f"{target}.{part}" in known else attrs.get((target, part))
            if target is not None:
                key = (target, node.attr)
        seen = set()
        while key in passed and key not in seen:
            seen.add(key)
            key = passed[key]
        return key
    return named, passed, bound


def _vault_sets(srcs: dict[str, str], trees: dict[str, ast.Module]) -> tuple[set, set, dict]:
    """4항의 두 집합 — `(import할 때 저장소에서 고정하는 이름, 저장소가 필요한데 받지 않는 함수, 다시 내놓는 이름 표)`.

    고정하는 이름: 모듈 본문이나 클래스 본문에서 저장소를 정하는 식(`_opens_vault`)으로 만든 이름, 그런 이름에서
    다시 계산한 이름, 받지 않는 함수를 **불러서** 얻은 이름. 받지 않는 함수: 고정된 이름을 읽는 함수, 그리고
    `vault`를 첫 인자로 받지 않으면서 그런 함수를 부르는 함수. 단위는 최상위 함수와 메서드다(안에 든 함수는 그
    단위의 몫). 생성자가 `vault`를 받는 클래스의 메서드는 받은 것으로 본다.

    두 수는 옮기는 동안 줄어들기만 한다: 함수를 옮기면 그 함수가 빠지고, 아직 옮기지 않은 호출자는 전에는
    "그런 함수를 부르는 함수"였고 뒤에는 `core.VAULT`를 읽는 함수라 그대로 남는다. 그러려면 호출을 놓치지
    않아야 하므로, 다른 모듈을 거친 이름(`_resolver`)을 따라가고 클래스를 쓰면 그 메서드 전부를 부른
    것으로 본다.

    못 보는 것: `getattr(core, "ROOT")`처럼 이름을 문자열로 읽는 것, `self`가 아닌 객체의 메서드 호출,
    `import *`. `vault`가 첫 인자가 아니면 받은 것으로 치지 않는다."""
    named, passed, _ = _resolver(srcs, trees)

    units, methods, assigns = {}, {}, []       # (모듈, 이름) → (함수, 클래스), (모듈, 클래스) → 메서드들, 본문의 대입들
    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                units[(module, node.name)] = (node, None)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        units[(module, f"{node.name}.{sub.name}")] = (sub, node.name)
                        methods.setdefault((module, node.name), []).append((module, f"{node.name}.{sub.name}"))
                    elif isinstance(sub, (ast.Assign, ast.AnnAssign)) and sub.value is not None:
                        assigns.append((module, node.name, sub))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                assigns.append((module, None, node))
    holds = {c for c in methods
             if (c[0], f"{c[1]}.__init__") in units and _takes_vault(units[(c[0], f"{c[1]}.__init__")][0], True)}

    values = []                                # (이름, [(값이 가리키는 것, 부르는가)], 저장소를 정하는가)
    for module, cls, node in assigns:
        called = {id(c.func) for c in ast.walk(node.value) if isinstance(c, ast.Call)}
        seen = [(named(module, n, set(), cls), id(n) in called) for n in ast.walk(node.value)
                if isinstance(n, (ast.Name, ast.Attribute))]
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for name in (n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)):
            values.append(((module, f"{cls}.{name}" if cls else name), [s for s in seen if s[0]], _opens_vault(node.value)))

    reach, threaded = {}, set()                # 함수가 가리키는 것들, 저장소를 받은 함수
    for key, (fn, cls) in units.items():
        module = key[0]
        local = {a.arg for n in ast.walk(fn) if isinstance(n, ast.arguments)
                 for a in [*n.posonlyargs, *n.args, *n.kwonlyargs, *filter(None, [n.vararg, n.kwarg])]}
        local |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        local -= {name for n in ast.walk(fn) if isinstance(n, ast.Global) for name in n.names}
        if _takes_vault(fn, cls is not None) or (cls is not None and (module, cls) in holds):
            threaded.add(key)
        reach[key] = {named(module, n, local, cls) for n in ast.walk(fn)
                      if isinstance(n, (ast.Name, ast.Attribute)) and not isinstance(n.ctx, ast.Store)} - {None, key}

    def with_methods(keys) -> set:
        return set(keys).union(*(methods.get(k, ()) for k in keys))

    fixed, need = set(), set()
    grew = True
    while grew:
        grew = False
        for name, seen, opens in values:
            if name not in fixed and (opens or any(k in fixed or (call and with_methods({k}) & need) for k, call in seen)):
                fixed.add(name)
                grew = True
        for key in units:
            if key not in need and (reach[key] & fixed or (key not in threaded and with_methods(reach[key]) & need)):
                need.add(key)
                grew = True
    return fixed, need, passed


def _vault(srcs: dict[str, str], trees: dict[str, ast.Module]) -> tuple[dict, dict]:
    """`_vault_sets`의 두 집합을 모듈별 수로."""
    fixed, need, _ = _vault_sets(srcs, trees)

    def count(keys) -> dict[str, int]:
        return {m: sum(1 for k in keys if k[0] == m) for m in sorted({k[0] for k in keys})}
    return count(fixed), count(need)


# 자가 계량의 표본 — 4항. 모듈 이름 → 소스.
_VAULT_SAMPLE = {
    "osk.vault": '''
class Vault:
    @classmethod
    def open(cls):
        return cls()
''',
    # 고정: VAULT·ROOT. 못 받은 함수: reader·caller·passes·Lock.__enter__·locks. takes·opens·shadow·Box는 아니다.
    "osk.held": '''
from .vault import Vault
VAULT = Vault.open()
ROOT = VAULT.root

def reader():
    return ROOT

def caller():
    return reader()

def takes(vault):
    return vault.root

def passes():
    return takes(VAULT)

def opens():
    return takes(Vault.open())

def shadow(ROOT):
    return ROOT

class Box:
    def __init__(self, vault):
        self.vault = vault

    def get(self):
        return takes(self.vault)

class Lock:
    def __enter__(self):
        return ROOT

def locks():
    with Lock():
        pass
''',
    # 다른 모듈에서: 고정은 P(속성 사슬)·K.HERE(클래스 본문), ALIAS는 다시 내놓는 이름이라 아니다.
    # 못 받은 함수: far(가져온 이름)·via(가져온 함수)·chain(속성 사슬)·K.m(클래스 속성). K.s는 받았다(staticmethod).
    "osk.held2": '''
from .held import ROOT, reader
from . import held
ALIAS = held.reader
P = held.VAULT.root

def far():
    return ROOT

def via():
    return reader()

def chain():
    return held.VAULT.root

class K:
    HERE = ROOT

    @staticmethod
    def s(vault):
        return vault

    def m(self):
        return self.HERE
''',
    # 다른 모듈을 거쳐서: 고정은 X(못 받은 함수를 불러 얻은 값). 못 받은 함수: through·alias(다시 내놓은 이름),
    # hop(모듈 속성 사슬 `held2.held.reader`).
    "osk.held3": '''
from . import held2
X = held2.via()

def through():
    return held2.reader()

def alias():
    return held2.ALIAS()

def hop():
    return held2.held.reader()
''',
}
_VAULT_WANT = {"vault_fixed_names": {"osk.held": 2, "osk.held2": 2, "osk.held3": 1},
               "vault_unthreaded": {"osk.held": 5, "osk.held2": 4, "osk.held3": 3}}


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
    trees = {name: ast.parse(text) for name, text in srcs.items()}
    fixed_names, unthreaded = _vault(srcs, trees)
    for name, tree in trees.items():
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
              "tangled_modules": _tangled(edges), "private_access": private,
              "vault_fixed_names": fixed_names, "vault_unthreaded": unthreaded, "broad_except": broad,
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
            "vault_fixed_names": sum(report["vault_fixed_names"].values()),
            "vault_unthreaded": sum(report["vault_unthreaded"].values()),
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
        **_VAULT_SAMPLE,
    }
    got = measure(sample, {"osk": KERNEL, "osk.low": KERNEL, "osk.high": SERVICE, "edge": SURFACE,
                           **{name: KERNEL for name in _VAULT_SAMPLE}})
    want = {"upward_imports": {"osk.low -> osk.high": 1}, "lazy_imports": {"osk.low": 1},
            "two_module_cycles": ["osk.high <-> osk.low"], "tangled_modules": 2,
            "private_access": {"osk.high": 2}, "broad_except": {"osk.high": 1},
            **_VAULT_WANT,
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
        grown = [k for k in TOTALS if baseline and actual["totals"][k] > baseline["totals"].get(k, actual["totals"][k])]
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
    grown = [k for k in TOTALS if actual["totals"][k] > baseline["totals"].get(k, actual["totals"][k])]
    print("\n".join(diff[:40]) + (f"\n… 외 {len(diff) - 40}줄" if len(diff) > 40 else ""))
    print("위반이 늘었다 — 코드를 고친다(docs/CODE-STANDARD.md): " + ", ".join(grown) if grown else
          "기준선이 낡았다 — `python tests/test_architecture.py --update`로 낮춘다")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
