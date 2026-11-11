"""표면 고정 시험 — 밖에서 부르는 표면이 기록한 것과 같다.

구조 개편(docs/v6.0-refactoring.md)은 MCP 도구의 이름·설명·스키마와 `osk` CLI의 명령·옵션을 바꾸지 않는다.
이 시험은 그 둘을 `surface_snapshot.json`과 **전문으로** 대조한다. 도구 설명은 에이전트가 읽는 안내문이라
한 글자가 바뀌어도 동작이 바뀐 것이다.

    python tests/test_surface_snapshot.py            # 대조. 다르면 exit 1
    python tests/test_surface_snapshot.py --update   # 기록을 지금 표면으로

표면을 일부러 바꾸는 PR은 같은 PR에서 `--update`로 기록을 고친다. MCP 도구 목록의 개정은 Mechanism §6-2 7항의
관문을 따로 지난다.

MCP 스키마는 `mcp`와 `pydantic`이 만든다. 그 판이 기록할 때와 다르면 스키마는 대조하지 않고 77(SKIP)로 끝낸다 —
판은 `constraints.txt`가 고정하고 CI는 그 판으로 돈다. `osk.update` 같은 다른 진입점의 인자는 여기서 보지 않는다.
그쪽은 회귀 수트가 명령을 실제로 불러서 지킨다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from importlib import metadata
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).with_name("surface_snapshot.json")
SUITE_SKIP = 77                                   # 회귀 러너의 "환경 때문에 돌지 못했다"
SCHEMA_MAKERS = ("mcp", "pydantic", "pydantic-core")

_TMP = tempfile.TemporaryDirectory(prefix="osk-surface-test-")
os.environ["OSK_VAULT_ROOT"] = str(Path(_TMP.name) / "vault")   # osk import 전에 — 실 vault에 묶이지 않는다
os.environ["OSK_UPDATE_CHECK"] = "0"
sys.path.insert(0, str(ENGINE))


def mcp_tools() -> dict:
    """등록된 도구의 설명과 스키마 — 검증기의 표면 린트(`surface_checks.surface_lint`)와 같은 등록부를 읽는다.
    순서도 표면이다: 도구는 등록한 순서로, 인자는 선언한 순서로 클라이언트에 나간다."""
    import mcp_server
    registry = mcp_server.mcp._tool_manager._tools
    return {name: {"description": tool.description, "inputSchema": tool.parameters}
            for name, tool in registry.items()}


def _option(action: argparse.Action) -> dict:
    """옵션 하나 — 값이 없는 항목은 적지 않는다."""
    default = action.default
    record = {"flags": list(action.option_strings) or [action.dest],
              "action": type(action).__name__,
              "nargs": action.nargs,
              "required": bool(action.required),
              "default": default if isinstance(default, (str, int, float, bool, type(None))) else repr(default),
              "type": getattr(action.type, "__name__", None),
              "choices": sorted(map(str, action.choices)) if action.choices is not None else None,
              "help": action.help}
    return {key: value for key, value in record.items() if value is not None}


def cli_tree(parser: argparse.ArgumentParser) -> dict:
    """파서의 명령과 옵션. 도움말의 줄바꿈처럼 Python 판마다 달라지는 것은 담지 않는다."""
    tree = {"options": [], "commands": {}}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            tree["commands"] = {name: {**({"help": helps[name]} if helps.get(name) else {}), **cli_tree(sub)}
                                for name, sub in sorted(action.choices.items())}
        elif not isinstance(action, argparse._HelpAction):
            tree["options"].append(_option(action))
    exclusive = [{"required": g.required, "dests": [a.dest for a in g._group_actions]}
                 for g in parser._mutually_exclusive_groups]
    return {**tree, "exclusive": exclusive} if exclusive else tree


def surface() -> dict:
    from osk import cli, cli_parser
    return {"generated_with": {name: metadata.version(name) for name in SCHEMA_MAKERS},
            "cli": cli_tree(cli_parser.build_parser()), "mcp_tools": mcp_tools()}


def _leaves(tree: dict) -> int:
    return sum(_leaves(sub) for sub in tree["commands"].values()) if tree["commands"] else 1


def _change(now, was) -> str:
    if isinstance(now, str) and isinstance(was, str) and max(len(now), len(was)) > 80:
        at = next((i for i, (a, b) in enumerate(zip(now, was)) if a != b), min(len(now), len(was)))
        return f"{at}번째 글자부터 다르다: {was[max(0, at - 20):at + 40]!r} → {now[max(0, at - 20):at + 40]!r}"
    return f"{was!r} → {now!r}"


def differences(now, was, path: str) -> list[str]:
    """기록과 다른 자리 — `cli/commands/search/options[1]/default` 같은 경로로."""
    if isinstance(now, dict) and isinstance(was, dict):
        out = []
        for key in sorted(set(now) | set(was)):
            if key not in was:
                out.append(f"{path}/{key}: 새로 생겼다")
            elif key not in now:
                out.append(f"{path}/{key}: 없어졌다")
            else:
                out += differences(now[key], was[key], f"{path}/{key}")
        if set(now) == set(was) and list(now) != list(was):
            out.append(f"{path}: 순서가 바뀌었다 — {list(was)} → {list(now)}")
        return out
    if isinstance(now, list) and isinstance(was, list):
        if len(now) != len(was):
            return [f"{path}: 항목 {len(was)}개 → {len(now)}개"]
        return [d for i, (a, b) in enumerate(zip(now, was)) for d in differences(a, b, f"{path}[{i}]")]
    return [] if now == was else [f"{path}: {_change(now, was)}"]


def main(argv: list[str]) -> int:
    current = surface()
    if not current["mcp_tools"] or not current["cli"]["commands"]:
        print("표면을 읽지 못했다 — 등록된 MCP 도구나 CLI 명령이 없다")
        return 1
    summary = f"MCP 도구 {len(current['mcp_tools'])}개, CLI 명령 {_leaves(current['cli'])}개"
    if "--update" in argv:
        SNAPSHOT.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n",   # 순서를 지킨다
                            encoding="utf-8", newline="\n")
        print(f"표면 기록을 썼다: {summary}")
        return 0
    if not SNAPSHOT.exists():
        print(f"표면 기록이 없다 — {SNAPSHOT.name}. `--update`로 만든다")
        return 1
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    same_makers = recorded["generated_with"] == current["generated_with"]
    diff = differences(current["cli"], recorded["cli"], "cli")
    if same_makers:
        diff += differences(current["mcp_tools"], recorded["mcp_tools"], "mcp_tools")
    if diff:
        print("\n".join(diff[:40]) + (f"\n… 외 {len(diff) - 40}줄" if len(diff) > 40 else ""))
        print("표면이 기록과 다르다 — 의도한 변경이면 `python tests/test_surface_snapshot.py --update`, 아니면 코드를 고친다")
        return 1
    if not same_makers:
        print(f"SKIP: MCP 스키마는 대조하지 않았다 — 스키마를 만드는 패키지의 판이 기록할 때와 다르다 "
              f"({recorded['generated_with']} → {current['generated_with']})")
        return SUITE_SKIP
    print(f"표면: 기록과 같다 ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
