# osk-system 시작 안내서

[English](GETTING-STARTED.md) · **한국어**

Claude Code나 Codex는 쓰고 있지만 osk-system은 처음 설정하는 사람을 위한 따라하기
안내서다. 빈 폴더에서 출발해 검증까지 마친 설정으로 끝난다. 마지막에는 에이전트가
첫 기억을 쓰고, 그것을 다시 찾아 읽어 낸다. v4.0.0 릴리스 기준이다.

단계마다 더 자세한 내용은 운용 참고서인 [SETUP.md](SETUP.md)로 이어지는 링크를
따라가면 된다.

## 시작하기 전에

- **`<vault>`**: vault 폴더의 절대 경로(`/Users/you/my-osk-vault`,
  `C:/osk/my-osk-vault` 등). Windows에서도 슬래시(`/`)로 적는다 —
  PowerShell·JSON·TOML 어디서나 이스케이프 없이 통한다. 경로에는 **공백을 넣지
  않는다.** 훅 명령은 평범한 명령줄이라, 공백 없는 경로여야 어느 셸에서도 따옴표가
  필요 없다. Windows에서 `C:/osk` 같은 폴더를 쓰면 공백이 든 사용자 이름도 피할 수
  있다.
- **`my-app`**: 자신의 프로젝트 저장소 하나를 가리키는 예시 이름.
- **셸.** Windows 명령은 PowerShell용이고, macOS/Linux 명령은 bash나 zsh용이다.
  `.venv`로 시작하는 명령은 vault 루트에서 실행한다.
- **`osk`라는 명령은 없다.** 엔진은 vault 자신의 Python으로 실행한다 —
  `python -m osk.cli …`, `python -m osk.update …`. Windows에서 `osk`를 치면 화상
  키보드가 뜬다. SETUP.md에 `osk validate`라고 적힌 곳은
  `python -m osk.cli validate`로 읽는다.
- **화면 문구.** 엔진과 훅의 메시지는 대부분 한국어이고, fork 점검 사유와 일부
  진단은 영어로 나온다. 이 안내서는 화면에 나오는 문구를 그대로 인용한다.

## 얻게 되는 것

- **직접 소유하는 vault.** 평범한 Markdown 파일로 된 Git 저장소다. 어떤 편집기로도
  읽을 수 있고, Obsidian에서는 그래프로 둘러볼 수 있다.
- **도구 12종을 갖춘 MCP 서버.** 에이전트는 이 도구들로 지식 *노드*를 검색하고
  읽고 쓴다. 모든 쓰기는 엔진이 검증한다.
- **Claude Code·Codex용 훅.** 하는 일은 셋이다.
  - 새 세션마다 그 프로젝트의 기억을 건넨다.
  - 끝난 대화 라운드를 하나하나 포착한다.
  - 대화를 지식으로 바꾸는 검토의 시점을 잡는다.
- **사람이 정하는 권위.** 보호로 지정한 폴더는 사용자가 마지막으로 승인한 상태를
  엔진이 따로 보존한다. 에이전트의 수정은 변경집합으로 대기하며, 승인하거나
  반려하는 것은 사용자만 할 수 있다.
- **선택 기능.** 개인 원격과의 Git 동기화, Claude나 ChatGPT 구독으로 도는
  백그라운드 검토, 지식만 보여 주는 Obsidian 그래프.

## 5분 개념 정리

**vault와 릴리스.** 내 vault는 정본 저장소
<https://github.com/lpaiu-cs/osk-system> 의 *인스턴스*다. 프레임워크 파일
(`_governance/`, `docs/`)은 릴리스로만 바뀌고, 릴리스는 `osk.update`로 적용한다.
엔진 파일을 직접 고치지 않으며, 공개 저장소에서 `git pull`하지도 않는다.

**Space.** 지식은 최상위 폴더 세 곳에 담긴다.

| 폴더 | 담는 것 |
|---|---|
| `00_Scope/` | 한 프로젝트나 활동에 딸린 지식. *scope*마다 폴더가 하나다 |
| `00_Domain/` | 여러 프로젝트에 두루 통하는 지식. 주제별로 정리한다 |
| `00_Person/` | 자신의 기록과 자신에 관한 지식: 선호, 목표, 메모 |

v3.21 이전에 만든 vault는 옛 이름(`= Scope` 등)을 그대로 쓰고 있을 수 있다. 그래도
지원된다. [space-layout-migration.md](space-layout-migration.md)(영문)를 본다.

**scope와 세션 키.** 프로젝트마다 `00_Scope/my-app/` 같은 scope가 하나씩 생긴다.

- 훅은 모든 세션에 그 세션이 도는 저장소의 이름을 붙인다. `~/code/my-app`에서라면
  *세션 키*는 `my-app`이다.
- Git 워크트리는 본 저장소의 이름을 받는다. Git 밖의 폴더는 자기 폴더 이름을 쓴다.
- 무관한 다른 저장소가 이미 그 이름을 소유했으면 훅은 `my-app-<뿌리 커밋 앞 8자>`를
  준다. 소유자는 그 결속을 처음 쓴 저장소이며, 그 뿌리 커밋이 결속과 함께 기록된다.
- 처음 성공한 쓰기가 그 키를 scope 하나에 영구히 결속한다. 그 뒤로 그 저장소의
  모든 세션은 어느 기기에서든 그 scope에 착지한다.

**노드, 군집, 허브.** *노드*는 엔진이 검증하는 짧은 머리말(frontmatter)이 달린
Markdown 파일 하나다. 머리말에는 다음이 들어간다.

- id
- 생성·수정 시각
- 작성자(`author`)
- 작성 모델(`drafter`) — 그 노드를 쓴 모델
- 80자 이내의 한 줄 요약(`summary`)

노드의 제목은 곧 파일 이름이며, 한 vault 안에서 두 노드가 같은 제목을 가질 수
없다. 노드끼리는 Obsidian처럼 `[[제목]]`으로 링크한다. 노드가 모인 폴더가
*군집*이다. 군집마다 *허브*가 있다 — 폴더와 이름이 같은 노드로, 그 폴더에서 가장
먼저 쓴다. MCP를 거친 쓰기는 검증되지만, 손으로 고친 파일은 `validate`를 돌리기
전까지 검사되지 않는다.

**원료(raw) 기록.** 훅은 끝난 대화 라운드를 scope의 `_raw/` 폴더에 포착한다.
라운드 하나는 사용자의 메시지와 그에 대한 에이전트의 보이는 답변이다. 원료는
덧붙이기만 할 수 있고(append-only) 검색에는 나오지 않는다. 노드는
`<기록 경로>#12` 같은 좌표로 라운드를 근거로 인용한다.

**scope 기억.** scope마다 1,500자 이내의 짧은 메모가 하나 있다. 지금 중요한 배울
점을 담으며, 훅이 모든 세션의 시작에 넣어 준다. 메모가 차면 오래 갈 지식은 노드로
옮긴다. 자리를 내려고 덜어 낸 줄은 기록해 두었다가 나중에 처분하는데, 훅은 이 일을
*정돈*이라 부른다. 처분은 그 줄을 노드로 증류하거나, 기존 노드에 합치거나, 폐기하는
것이다.

**보호영역.** CLI의 `protect` 명령으로 폴더를 보호할 수 있다. 그 뒤로 그 폴더의 모든
변경은 에이전트가 했든 사용자가 했든 `status`에 `pending`으로 나타나고, 대화형
단말에서 `approve`하거나 `revert`할 때까지 대기한다. 에이전트는 MCP로 무엇도 승인할
수 없다. 선의의 실수를 막는 장치이지, 디스크에 쓸 수 있는 사람을 막는 보안 장치는
아니다.

**검토.** 검토는 새로 쌓인 원료 라운드를 읽고 중요한 것을 노드나 scope 기억으로
남긴다.

- 기본값에서는 사용자의 9번째와 15번째 메시지에서 훅이 에이전트에게 이 세션 안에서
  검토하라고 요청한다.
- *fork 검토*를 켜면 성공한 최종 답변 9회마다 백그라운드 fork가 검토를 맡는다. 같은
  하네스와 모델을 사용자의 구독으로 쓴다.

**대장.** 운영 scope인 `00_Scope/Workbench/`는 엔진의 append-only 대장을
`_ledger/*.jsonl`에 둔다. scope 결속, 승인, 갱신, 퇴출이 여기에 기록된다. scope
기억도 이곳의 `_scope_memory/`에 있다. 이 파일들은 절대 손으로 고치지 않는다.

## 준비물

- **Python 3.11 이상.** README와 SETUP은 3.12를 쓰고, 아래 명령도 그렇다. 3.11
  이상의 인터프리터라면 명령의 버전만 바꿔 쓰면 된다.
- **Git.** 커밋할 수 있도록 `user.name`과 `user.email`을 설정해 두고, 개인 원격에
  push할 수 있는 인증 정보도 갖춘다.
- **Claude Code나 Codex.** 하나만 연결해도, 둘 다 연결해도 된다.
- **vault용 빈 비공개 Git 저장소.** README나 라이선스 없이 만든다. 필수는 아니지만
  권장한다 — vault에는 대화 기록과 개인 메모가 쌓이기 때문이다.

## 1단계: vault 만들기

`main` 브랜치가 아니라 릴리스 태그를 clone한다. `main`은 릴리스 사이에도 움직인다.
갱신기는 파일을 릴리스와 대조하므로, 정확히 한 릴리스에서 출발해야 2단계에서 깨끗한
기준선을 기록할 수 있다. `v4.0.0`은 적힌 그대로 쓰면 된다.
[릴리스 페이지](https://github.com/lpaiu-cs/osk-system/releases)의 최신 태그를 써도
되지만, 그때는 2단계에서도 같은 태그를 쓴다.

vault를 담을 폴더(Windows라면 `C:/osk` 등, 먼저 만들어 둔다)에서 실행한다. 어느
OS에서나 같은 명령이다.

```bash
git clone --branch v4.0.0 https://github.com/lpaiu-cs/osk-system.git my-osk-vault
cd my-osk-vault
git switch -c main
```

vault가 개인 원격을 가리키게 하고 push한다.

```bash
git remote set-url origin <your-private-remote-url>
git push -u origin main
```

vault를 공개 저장소에 push하지 않는다. 아직 개인 원격이 없다면 대신
`git remote remove origin`을 실행한다. 그러지 않으면 나중에 동기화 데몬이 아직
릴리스되지 않은 upstream 변경을 vault로 끌어온다.

**확인:** `git status`가 `On branch main`을 보인다. `git remote -v`에는 개인 원격
URL이 보이고, 원격을 지웠다면 아무것도 나오지 않는다.

## 2단계: 엔진 설치와 릴리스 기준선 기록

**2~4단계를 명령 하나로 할 수도 있다**(v4.1.0 이상). vault 루트에서 Python 3.11
이상으로(Windows는 `py -3.12`) 실행한다:

```bash
python _governance/_engine/scripts/setup.py --interactive
```

`.venv`를 만들어 의존성을 설치하고, 릴리스 기준선을 기록하고, Claude Code·Codex에 MCP
서버와 훅 세 개를 등록한다. 쓰기 전에 계획을 보여 주고 확인을 받는다. 바꾸는 설정 파일은
모두 백업하고, 이 vault의 osk 항목만 건드린다([설치 도구](SETUP.md#설치-도구-setup)).
그다음 "할 일"로 나열된 것(Codex의 훅 신뢰 등)을 하고 [5단계](#5단계-첫-세션)로 간다.
아래 수동 절차는 같은 일을 손으로 한다.

macOS/Linux:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r _governance/_engine/requirements.txt
export PYTHONPATH=_governance/_engine
.venv/bin/python -m osk.cli validate
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r _governance\_engine\requirements.txt
$env:PYTHONPATH = "_governance\_engine"
.venv\Scripts\python.exe -m osk.cli validate
```

`PYTHONPATH`는 터미널을 닫으면 사라진다. 새 터미널에서 `osk.cli`나 `osk.update`를
실행하기 전에는 다시 설정하고, 둘 다 vault 루트에서 실행한다. MCP 서버와 훅에는
필요 없다.

**확인:** `validate`가 출력하는 JSON이 `"verdict": "PASS"`로 끝난다.

이제 릴리스 기준선을 기록한다. 갓 clone한 저장소에는 파일이 어느 릴리스에서 왔는지
기록이 없다. 이것을 기록해 두어야 이후의 갱신이 릴리스 파일과 로컬 수정을 구별한다.
갱신기는 대조하려고 GitHub에서 릴리스를 내려받는다.

macOS/Linux:

```bash
.venv/bin/python -m osk.update --to v4.0.0 --apply
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m osk.update --to v4.0.0 --apply
```

첫 실행은 어떤 파일도 바꾸지 않는다. 계획을 출력한 뒤 종료코드 2와
`"approval_required": true`로 끝난다. `"ok": false`와, 에이전트에게 멈추고
사용자에게 물으라고 지시하는 한국어 `instruction`도 함께 나온다. 둘 다 예상된
결과다. 갓 clone한 vault라면 계획의 `rebaseline`에 프레임워크 파일이 전부
올라온다. 내용은 이미 같으니 기준선만 기록된다. 계획을 검토하고 한 시간 안에
**같은 명령을 한 번 더** 실행하면 적용된다. `--apply`는 언제나 이 확인을
거친다([최신 릴리스로 갱신하기](#최신-릴리스로-갱신하기) 참고).

같은 갱신이 `_governance`를 보호영역으로 지정한다. 깨끗한 clone의 계획에는
`governance.protect`가 `"establish"`로 나오고, 확인한 적용은
`"governance_protected": "established"`를 보고한다. 통치 파일이 릴리스와 다르면
계획에 `"protect": "withheld"`와 차이 파일의 `unattested`가 나오고, 갱신은 그 구획을
보호하지 않은 채 남긴다. 해당 파일을 검토한 뒤 직접 지정한다.

macOS/Linux:

```bash
.venv/bin/python -m osk.cli protect _governance
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m osk.cli protect _governance
```

터미널에서 직접 `y`로 확인한다. 현재 파일이 초기 승인본이 된다.

기준선은 `00_Scope/Workbench/_ledger/update.jsonl`에 기록된다. 직접 커밋하거나,
나중에 동기화 데몬에 맡긴다. 1단계에서 원격을 지웠다면 `git push`는 건너뛴다.

```bash
git add -A
git commit -m "Record osk release baseline"
git push
```

**확인:** `.venv/bin/python -m osk.update`의 `current`가 선택한 버전을 가리킨다
(위 예제에서는 `v4.0.0`).
Windows에서는 `.venv\Scripts\python.exe -m osk.update`를 쓴다. `git status`는
깨끗하다. `osk.cli status`가 `"protected_regions": {"_governance": "clean"}`을
보여 준다.

## 3단계: Claude Code 연결

Codex만 쓴다면 이 단계는 건너뛴다.

**3a. MCP 서버를 user scope로 등록한다.** 그래야 모든 프로젝트에서 쓸 수 있다.

macOS/Linux:

```bash
claude mcp add --scope user osk-system -- <vault>/.venv/bin/python <vault>/_governance/_engine/mcp_server.py
```

Windows (PowerShell):

```powershell
claude mcp add --scope user osk-system -- <vault>/.venv/Scripts/python.exe <vault>/_governance/_engine/mcp_server.py
```

**확인:** `claude mcp list`에 `osk-system: … - ✔ Connected`가 보인다.

**3b. 훅을 등록한다.** MCP 서버는 에이전트에게 도구를 줄 뿐, 훅을 설치하지는
않는다. 아래 세 이벤트를 `~/.claude/settings.json`(Windows:
`%USERPROFILE%\.claude\settings.json`)의 `"hooks"` 객체에 넣는다. 파일이 없으면
만든다. 이미 훅이 있다면 바꿔치지 말고 그 옆에 항목을 추가한다. Windows에서는 세
명령 모두 `.venv/bin/python`을 `.venv/Scripts/python.exe`로 바꾼다.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py",
            "timeout": 30
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_prompt_submit.py",
            "timeout": 30
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/capture_stop.py",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

같은 스크립트가 Claude Code와 Codex를 함께 맡는다. 두 파일 이름에 붙은 `claude_`는
예전 이름이 남은 것이다.

**확인:** 먼저 프로젝트 저장소 하나에서 세션 시작 훅을 손으로 실행해 본다. 그곳의
새 세션이 받을 내용이 출력된다. Windows에서는 Git Bash에서
`.venv/Scripts/python.exe`로 실행한다. PowerShell은 파이프로 넘기는 텍스트 앞에
BOM(byte-order mark)을 붙일 수 있는데, 훅은 그것을 거부한다.

```bash
cd ~/code/my-app
echo '{"session_id":"hook-test"}' | <vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py
```

출력은 `[osk 세션 시작 — session=\"my-app\"]`이 든 JSON 한 줄이다. 키 `my-app`으로
세션이 시작됐다는 뜻이다. 지어낸 세션 ID로 실행했으므로 끝에
`native harness/transcript unavailable` 진단이 붙는데, 예상된 결과다. 그다음 Claude
Code 세션을 **새로** 시작한다. 훅은 시작할 때만 읽히기 때문이다. `/hooks`에 osk
항목 셋이 보인다.

## 4단계: Codex 연결

Claude Code만 쓴다면 이 단계는 건너뛴다.

**4a. MCP 서버를 등록한다.**

macOS/Linux:

```bash
codex mcp add osk-system -- <vault>/.venv/bin/python <vault>/_governance/_engine/mcp_server.py
```

Windows (PowerShell):

```powershell
codex mcp add osk-system -- <vault>/.venv/Scripts/python.exe <vault>/_governance/_engine/mcp_server.py
```

이 명령은 `~/.codex/config.toml`에 `command`와 `args`를 담은
`[mcp_servers.osk-system]` 테이블을 추가한다. Codex는 TOML을 읽으므로
`.mcp.json.example`을 이 파일에 붙여 넣지 않는다.

**확인:** `codex mcp list`에 `osk-system`이 `enabled` 상태로 보인다.

**4b. 훅을 등록한다.** 위치는 `~/.codex/hooks.json`(Windows:
`%USERPROFILE%\.codex\hooks.json`)이다. 파일이 이미 있다면 덮어쓰지 말고 이벤트
목록에 이 항목들을 추가한다. Windows에서는 여기서도 `.venv/Scripts/python.exe`를
쓴다.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear|compact",
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_session_start.py",
        "timeout": 30,
        "statusMessage": "osk: loading scope memory"
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/claude_prompt_submit.py",
        "timeout": 30,
        "statusMessage": "osk: checking review cadence"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "<vault>/.venv/bin/python <vault>/_governance/_engine/scripts/hooks/capture_stop.py",
        "timeout": 30,
        "statusMessage": "osk: capturing the finished round"
      }]
    }]
  }
}
```

Codex는 새로 추가되거나 바뀐 훅 정의를 사용자가 신뢰하기 전까지 건너뛴다. Codex를
시작해 `/hooks`를 열고, osk 항목을 하나씩 확인해 신뢰한 뒤 새 세션을 시작한다.

**확인:** 3b의 손 실행 시험이 저장소의 세션 키를 출력한다. Codex도 시험 방법은
같다. `codex features list`에서 `hooks`가 `true`로 보인다(Codex 0.154의
기본값이다). 새 세션에서 에이전트에게 *"osk 훅이 알려 준 세션 키가 뭐야?"* 하고
물으면 저장소 이름으로 답해야 한다.

**두 호스트를 한 번에 확인:** 각 호스트에서 새 세션을 한 번 연 뒤, `PYTHONPATH`를
설정한 채(2단계) vault 루트에서 `doctor`를 실행한다: `.venv/bin/python -m osk.cli doctor`,
Windows는 `.venv\Scripts\python.exe -m osk.cli doctor`. Claude Code와 Codex마다 MCP와
세 훅이 이 vault를 가리키는지, 훅마다 이 기기에서 마지막으로 불린 시각, 세션 시작 뒤
에이전트가 `overview`를 불렀는지를 보인다. 등록됐는데 한 번도 불리지 않은 훅은 Claude
Code에서는 새 세션을, Codex에서는 `/hooks`의 신뢰를 요구한다. `doctor`는 읽기만 한다.

## 5단계: 첫 세션

Claude Code나 Codex를 vault가 아니라 `~/code/my-app` 같은 **프로젝트 저장소
안에서** 연다. 폴더 이름이 세션 키가 되므로 예시는 `my-app`을 쓴다. 자기 저장소
이름으로 바꿔 읽는다. 처음에는 하네스가 osk 도구마다 허용할지 물을 수 있는데,
허용한다.

아래 예시 프롬프트는 영어로 적었다. 6번의 터미널 확인이 영어 검색어 `test suite`로
노드를 찾기 때문이다. 한국어로 요청해도 되며, 그때는 4번과 6번의 검색어를 노드에
실제로 적힌 낱말로 바꾼다.

**훅이 에이전트에게 건네는 말.** 아직 결속이 없는 저장소라면 세션 시작 문구는
이렇게 시작한다.

```text
[osk 세션 시작 — session="my-app"]
이 세션에서 `overview(session="my-app")`를 한 번 불러 … 아직 scope 결속이 없다. 착지를 추측하지 말고 …
```

세션 키가 `my-app`이니 `overview`를 한 번 부르고, 아직 scope 결속이 없으니 쓸 곳을
짐작하지 말고 프로젝트부터 확인하라는 지시다.

fork 검토를 켜지 않았다면
`[osk 검토 경고 — subscription fork CLI is not configured. …]`도 함께 나온다.
검토가 이 세션 안에서 9번째와 15번째 메시지에 이뤄진다는 뜻이며, 정상이다.

scope가 결속되기 전에는 이 대화의 라운드를 포착할 수 없다. 그래서 두 번째
메시지부터는 메시지마다 훅 문구에 포착 진단(`포착 진단: WriteError: 착지 미정 …`)과
`[osk 케이던스 — user 턴 N]` 줄이 붙는다. 실제로 검토할 때가 된 것은 아니다. 둘 다
아래 2번을 마치면 멈춘다. 검토 경고는 fork 검토를 설정할 때까지 남는다.

1. **둘러보기.** 프롬프트:

   ```text
   Call the osk overview tool with this session's key and show me the result.
   ```

   **확인:** 새 vault라면 결과에 `"clusters": []`와 `"session_scope": null`이
   보인다.

2. **프로젝트의 scope 만들기.** 프롬프트:

   ```text
   Create this project's scope in osk: a hub node titled "my-app" in space "00_Scope/my-app",
   with a one-line summary and a short description of the project. Use this session's key.
   ```

   첫 시도는 일부러 거부되며, 이런 메시지가 나온다.

   > `00_Scope/my-app`는 아직 없는 군집이라 … 새 군집을 만든다

   새 군집을 만드는 요청이니 사용자에게 확인한 뒤, 한 시간 안에 같은 요청을 다시
   보내라는 뜻이다. 에이전트가 물어볼 것이다. 이렇게 답한다.

   ```text
   Yes, create it. Send the same request again.
   ```

   **확인:** 결과에 `"ok": true`, `"path": "00_Scope/my-app/my-app.md"`,
   `"bound_scope": "my-app"`이 보인다.

3. **기록하기.** 프롬프트:

   ```text
   Record in osk, as its own node in this project's scope, that the test suite runs with `make test`.
   Link it from the my-app hub.
   ```

   이제 에이전트는 `space`를 생략할 수 있다. 노드가 어디에 착지할지는 결속이
   정한다. 그런 다음 허브를 고쳐 링크를 더한다.

   **확인:** 결과에 `"ok": true`가 보인다. 이번에는 `bound_scope`가 `null`이다 —
   새로 생긴 결속만 알려 주는 필드이기 때문이다.

4. **찾아서 읽기.** 프롬프트:

   ```text
   Search osk for "test suite", then read the node you found and quote its body.
   ```

   **확인:** `search`는 노드의 제목과 요약을 돌려준다. `read_node`는 `body`와
   `hash`를 돌려준다. 그 hash는 본문 전체를 바꿀 때만 필요하고, 작은 수정은
   `old_text`와 `new_text`로 한다.

5. **scope 기억에 배울 점 남기기**(선택). 프롬프트:

   ```text
   Add one line to this project's osk scope memory: "Tests run with make test."
   ```

   **확인:** 결과에 `"ok": true`와 `"limit": 1500`이 보인다.

6. **터미널에서 확인하기.** `PYTHONPATH`를 설정한 채 vault 루트에서 실행한다.

   macOS/Linux:

   ```bash
   ls 00_Scope/my-app
   .venv/bin/python -m osk.cli search "test suite"
   .venv/bin/python -m osk.cli sm show --session my-app
   ```

   Windows (PowerShell):

   ```powershell
   Get-ChildItem 00_Scope\my-app
   .venv\Scripts\python.exe -m osk.cli search "test suite"
   .venv\Scripts\python.exe -m osk.cli sm show --session my-app
   ```

   **확인:** 폴더에 허브와 새 노드가 보이고, 라운드가 한 번이라도 포착됐다면
   `_raw/`도 보인다. `search`가 새 노드를 돌려준다. `sm show`는 scope 기억에 넣은
   줄을 출력한다. 아직 결속되지 않은 키이거나 scope 기억이 아직 비어 있다면
   `sm show`는 아무것도 출력하지 않고 0으로 끝나는데, 오류가 아니다.

새 파일은 `git add -A`와 `git commit`으로 커밋하거나 동기화 데몬에 맡긴다.
5번에서 한 줄을 남겼다면 `my-app`의 다음 세션은 그 scope 기억을 받고 시작한다.
주입되는 블록은 `[osk scope 기억 — 00_Scope/my-app · N/1500자 · 여유 M자]`로
시작한다. 1,500자 가운데 N자를 썼고 M자가 남았다는 뜻이다.

### 결속된 scope란

- 결속은 `00_Scope/Workbench/_ledger/routing.jsonl`의 한 행으로, 세션 키 `my-app`을
  scope `my-app`에 잇는다.
- 그 키로 하는 이후의 쓰기는 `space` 인자 없이도 그 scope에 착지한다. 노드, scope
  기억, 포착된 대화가 모두 그렇다. 그 저장소의 어느 대화·기기·워크트리에서 왔든
  같다.
- 세션은 정확히 한 scope에 속한다. `my-app`에서 다른 scope로 쓰면 거부되며, 거부
  메시지에 `…에 결속돼 있다`가 나온다. 한 프로젝트를 넘어 쓸모 있는 지식은
  `00_Domain/`에, 자신에 관한 지식은 `00_Person/`에 둔다. 이 두 곳에는 어느
  세션이든 쓸 수 있다.
- 결속은 영구적이고 되돌리는 명령도 없으니 scope 이름은 신중히 고른다. 여러
  저장소가 scope 하나를 함께 쓸 수도 있다 — 저장소마다 첫 쓰기에 같은 `space`를
  주면 된다.
- 대화 ID를 키로 쓰지 않는다. UUID 모양의 키는 `1회용 대화 id`라는 문구와 함께
  거부된다. 다음 대화가 그 기억을 다시 찾을 길이 없기 때문이다.

### 자주 보게 될 훅 메시지

| 시작 문구 | 뜻 |
|---|---|
| `[osk 세션 시작 — session="…"]` | 세션 시작. 세션 키를 보여 주고 에이전트에게 `overview`를 부르라고 한다. `아직 scope 결속이 없다`가 붙으면 아직 결속 전이다. |
| `[osk scope 기억 — 00_Scope/… · N/1500자 · 여유 M자]` | scope 기억. 1,500자 중 쓴 글자 수와 남은 글자 수, 이어서 hash와 전문이 나온다. |
| `[osk 검토 경고 — <이유>. …]` | 적힌 이유로 백그라운드 fork 검토가 돌지 않는다. 검토는 이 세션 안에서 user 턴 9와 15에 이뤄진다. fork 검토를 설정하지 않았다면 정상이다. |
| `[osk 대화 검토 — …]` | fork 검토가 켜져 있다. 성공한 최종 답변 9회마다 한 번씩 돈다. |
| `[osk 케이던스 — user 턴 N]` | 검토할 때가 됐다. 9턴에는 에이전트가 다음 도구 호출에 검토를 함께 싣는다. 15턴에는 한 턴을 통째로 검토에 써도 된다. scope가 결속되기 전에는 포착이 실패해서 메시지마다 나온다(5단계). |
| `[osk 대화별 통합 대기 — …]` | 이 대화의 검토 대기열과 에이전트에게 주는 지시. |
| `[osk 참조·조직 검토]` | 이 scope 노드들의 링크와 허브를 정돈하는 작업. |
| `[osk 정돈 — …]`, `[osk 정돈이 밀렸다 — …]` | 처분을 기다리는 퇴출된 scope 기억 줄. `밀렸다`는 가장 오래된 항목이 14일을 넘겼다는 뜻이다. |
| `[osk scope 복구 대기 — …]` | scope 기억이 상한에 닿았다. 에이전트가 항목을 정리하거나 노드로 옮겨야 한다. |
| `[osk 새 릴리스 — vX.Y.Z · 이 vault vA.B.C]` | 새 릴리스가 나왔다. 같은 알림이 사용자 화면에도 경고로 뜨며, 기기마다 하루 한 번이다. 에이전트에게 갱신을 요청한다([최신 릴리스로 갱신하기](#최신-릴리스로-갱신하기)). 변경집합을 승인하기 전에는 아무것도 적용되지 않는다. |
| `진단`이나 `diagnostic`이 든 문구 | 훅의 한 단계가 실패했다. 작업은 계속되며, 무엇도 완료로 처리되지 않았다. [문제 해결](#문제-해결)을 본다. |

## 자주 쓰는 명령

`PYTHONPATH`를 설정한 채 vault 루트에서 실행한다. 각 명령 앞에
`.venv/bin/python -m osk.cli`를, Windows에서는 `.venv\Scripts\python.exe -m osk.cli`를
붙인다.

| 명령 | 쓰임 |
|---|---|
| `validate` | vault 전체를 검사한다: 노드 계약, 링크, 대장. `"verdict": "PASS"`가 나와야 한다. |
| `status` | 보호영역(`clean` 또는 `pending`), 미처분 퇴출, scope 기억 복구 상태를 본다. |
| `search "<검색어>"` | 노드를 검색한다. 원료 기록은 검색되지 않는다. |
| `sm show --session <키>` | scope 기억을 출력한다. |
| `tidy list` | 아직 처분하지 않은 퇴출된 scope 기억 줄을 나열한다. |
| `integration list` | 포착된 라운드가 검토를 기다리는 대화를 나열한다. |
| `protect <폴더>`, `approve <폴더>`, `revert <폴더>` | 폴더를 보호하거나, 대기 중인 변경집합을 승인하거나 반려한다. `[y/N]`으로 묻고, 대화형 단말이 아니면 실행을 거부한다. |
| `fork doctor` | fork 검토가 돌 수 있는지 점검한다. 읽기 전용이다. |
| `doctor` | 이 기기에서 Claude Code·Codex가 어떻게 이어졌는지 점검한다: MCP·훅 등록, 훅마다 마지막으로 불린 시각, 세션 시작 문구가 에이전트에게 닿았는지, 호스트 판본, fork. 읽기 전용이며, 동작할 수 없는 설정이 있을 때만 종료코드 1이다. |

## 선택: Obsidian으로 vault 둘러보기

그래프가 원료 기록이나 통치 파일 없이 지식 노드만 보여 주게 할 수 있다. 먼저
변경을 미리 본 뒤 적용한다. Obsidian이 이 vault를 열고 있다면 먼저 닫는다 —
Obsidian도 그래프를 쓰는 동안 같은 설정 파일을 직접 고쳐 쓴다.

macOS/Linux:

```bash
.venv/bin/python _governance/_engine/scripts/configure_obsidian_graph.py --vault-root .
.venv/bin/python _governance/_engine/scripts/configure_obsidian_graph.py --vault-root . --apply
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe _governance\_engine\scripts\configure_obsidian_graph.py --vault-root .
.venv\Scripts\python.exe _governance\_engine\scripts\configure_obsidian_graph.py --vault-root . --apply
```

**확인:** 두 번째 명령이 `"applied": true`를 출력한다. 바뀌는 파일은
`.obsidian/graph.json`이고, 이 파일은 이 기기에만 남는다(Git이 무시한다). 그
파일이 이미 있었다면 이전 판이 `.obsidian/graph.before-osk-<해시>.json`으로 옆에
저장된다. 이 백업은 Git이 무시하지 않으므로 `git add -A`와 동기화 데몬이 함께
커밋한다.

그다음 Obsidian에서 *Open folder as vault*로 `<vault>` 폴더를 보관함으로 연다.

## 선택: Git으로 vault 동기화하기

동기화 데몬은 기본적으로 15분마다 돈다. 돌 때마다 다음을 한다.

1. `main`에서 vault 전체를 커밋한다(`git add -A`).
2. `origin main` 위로 rebase한다.
3. push한다.

데몬은 `main`만 동기화하며, `SYNC_ENABLED=1`이 설정되지 않으면 아무 일도 하지
않는다. 시작하기 전에 다음을 갖춘다.

- `origin`이 개인 원격이어야 한다(1단계).
- Git이 묻지 않고 push할 수 있어야 한다. credential helper, 토큰, ssh-agent 가운데
  하나를 쓴다.
- 데몬은 `sync_daemon.py`의 **절대 경로**로 시작한다. `osk.update`는 그 경로로
  데몬을 찾아 다시 띄우므로, 상대 경로로 시작한 데몬은 찾지 못한다.

먼저 한 번만 돌려 본다.

macOS/Linux:

```bash
SYNC_ENABLED=1 .venv/bin/python <vault>/_governance/_engine/sync_daemon.py --once
```

Windows (PowerShell):

```powershell
$env:SYNC_ENABLED = "1"
.venv\Scripts\python.exe <vault>/_governance/_engine/sync_daemon.py --once
```

**확인:** `ok`가 출력된다. vault에 커밋되지 않은 변경이 있었다면 원격에
`sync: <날짜> <시각> (daemon)`이라는 커밋이 생긴다. 변수 없이 실행하면 데몬은
`sync 비활성 — SYNC_ENABLED=1 …`을 내고 끝난다.

데몬을 백그라운드에서 계속 돌리려면:

- **macOS (launchd):**

  ```bash
  cp _governance/_engine/scripts/launchd/com.ltm-vault-daemon.plist.example ~/Library/LaunchAgents/com.example.ltm-vault-daemon.plist
  # Edit the copy: replace <REPO> with your vault path and <HOME> with your home directory.
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.ltm-vault-daemon.plist
  launchctl list | grep ltm-vault-daemon
  ```

  복사본에서 `<REPO>`는 vault 경로로, `<HOME>`은 홈 디렉터리로 바꾼다. 로그는
  `~/Library/Logs/ltm-vault-daemon.log`에 남는다. 템플릿의 `PATH`는 Apple silicon의
  Homebrew(`/opt/homebrew/bin`)를 가정하므로, Intel Mac에서는 `/usr/local/bin`을
  쓴다. 멈추려면 `launchctl bootout gui/$(id -u)/com.example.ltm-vault-daemon`을
  실행한다. plist에서 `SYNC_ENABLED`만 지우면 안 된다 — 데몬이 종료하고 launchd가
  다시 띄우기를 끝없이 되풀이한다.

- **Linux (systemd user service):**

  ```bash
  mkdir -p ~/.config/systemd/user
  cp _governance/_engine/scripts/systemd/ltm-vault-daemon.service.example ~/.config/systemd/user/ltm-vault-daemon.service
  # Edit the copy: replace every <REPO> with your vault path.
  systemctl --user daemon-reload
  systemctl --user enable --now ltm-vault-daemon.service
  loginctl enable-linger "$USER"
  systemctl --user status ltm-vault-daemon
  ```

  복사본의 `<REPO>`는 모두 vault 경로로 바꾼다. 로그는
  `journalctl --user -u ltm-vault-daemon -f`로 읽는다. 멈추려면
  `systemctl --user disable --now ltm-vault-daemon.service`를 실행한다.
  `SYNC_ENABLED` 줄만 지우면 안 된다 — 데몬이 종료하고 systemd가 다시 띄우기를
  끝없이 되풀이한다.

- **Windows:** 저장소에 작업 스케줄러 템플릿은 없다. 열어 둔 터미널에서 데몬을 돌릴
  수는 있다. 위 명령에서 `--once`를 빼고 실행하고, 멈출 때는 Ctrl+C를 누른다.

  늘 켜져 있는 데몬이 필요하면 로그온할 때 시작하는 작업을 등록한다. 작업
  스케줄러에는 작업별 환경 변수가 없으므로, 작업은 `cmd.exe`를 실행해
  `SYNC_ENABLED=1`을 설정한 뒤 vault의 `pythonw.exe`로 `sync_daemon.py`를 **절대
  경로**로 띄운다. PowerShell에서 `<vault>`만 자기 것으로 바꿔 실행한다.

  ```powershell
  $vault = "C:/osk/my-osk-vault"
  $run = "$vault/.venv/Scripts/pythonw.exe $vault/_governance/_engine/sync_daemon.py"
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c set SYNC_ENABLED=1&& start "" ' + $run)
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
  Register-ScheduledTask -TaskName "osk-sync-daemon" -Action $action -Trigger $trigger -Settings $settings
  Start-ScheduledTask -TaskName "osk-sync-daemon"
  ```

  - `1&&`는 붙여 쓴다. `&&` 앞에 공백이 있으면 그 공백까지 값이 되어 데몬이
    `sync 비활성`을 내고 끝난다.
  - `start ""` 덕분에 `cmd.exe`가 곧바로 끝나므로, 콘솔 창은 작업이 시작될 때
    잠깐 깜빡일 뿐이다. `pythonw.exe`는 창을 띄우지 않는다.
  - `-AllowStartIfOnBatteries`가 있어야 배터리로 도는 노트북에서도 작업이
    시작된다.
  - `osk.update`는 동작(action)에 든 그 경로로 이 작업을 찾아, 갱신 뒤 다시
    실행한다.

  **확인:** 아래 명령이 실행 중인 데몬을 보여 준다. venv의 Python은 프로세스 두
  개로 보인다.

  ```powershell
  Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" | Where-Object CommandLine -like '*sync_daemon.py*' | Select-Object ProcessId, CommandLine
  ```

  데몬을 멈추려면 목록에서 자기 vault 경로가 보이는 프로세스마다
  `Stop-Process -Id <ProcessId>`를 실행한다. 작업은 데몬을 띄우자마자 끝나므로
  `Stop-ScheduledTask`로는 멈추지 않는다. 작업을 지우려면
  `Unregister-ScheduledTask -TaskName osk-sync-daemon`을 실행한다.

## 선택: 백그라운드 fork 검토

fork 검토가 없으면 검토는 세션 안에서 9턴과 15턴에 이뤄진다. fork 검토를 켜면
Stop 훅이 성공한 최종 답변 9회마다 대화의 숨은 일회성 *fork*를 띄운다. fork는 원래
대화의 하네스, 모델, 작업 폴더, 권한 모드를 그대로 이어받는다. 아직 검토하지 않은
라운드를 최대 9개 검토하고, 지식은 MCP로 쓴다. 실행은 **구독** 로그인으로만 하며,
유료 API 호출로 대체하는 일은 없다. 기기마다, 하네스마다 따로 켠다.

1. **osk에 쓸 CLI를 알려 준다.** `<vault>/.osk/response-growth.json`을 만든다.
   `.osk/` 아래의 모든 것은 이 기기에만 남는다(Git이 무시한다). 이 파일에는
   `claude`와 `codex` 키만 둘 수 있고, 각 값은 네이티브 CLI의 절대 경로다. Windows
   데스크톱 앱이라면 경로는 이런 모양이다.

   ```json
   {
     "claude": "C:/Users/you/AppData/Roaming/Claude/claude-code/2.1.280/claude.exe",
     "codex": "C:/Users/you/AppData/Local/OpenAI/Codex/bin/0123456789abcdef/codex.exe"
   }
   ```

   - fork는 CLI의 버전이 그 대화를 기록한 버전과 같을 때만 돈다.
   - 위와 같은 Windows 데스크톱 앱 경로라면, 버전이 맞는 형제 폴더가 설치돼 있을 때
     osk가 그 폴더로 바꿔 쓴다. 다른 모양의 경로는 적힌 그대로 쓴다.
   - 데스크톱 앱 경로는 PowerShell에서 아래 명령으로 찾는다. 나온 경로 가운데
     어느 것이든 되며, 하네스마다 하나를 슬래시(`/`)로 바꿔 파일에 적는다.

     ```powershell
     (Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe").FullName
     (Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe").FullName
     ```

   - 따로 설치한 `claude`나 `codex` CLI를 쓴다면(macOS, Linux, Windows 모두) 실제로
     실행하는 바이너리의 절대 경로를 등록한다. macOS나 Linux에서는
     `command -v claude`가, PowerShell에서는 `(Get-Command claude).Source`가 그
     경로를 알려 준다.
   - fork를 원하지 않는 하네스는 빼 둔다.

2. **같은 바이너리로 구독 로그인을 한다.**

   - **Claude:** `<claude> auth login`을 실행하고 claude.ai의 Pro, Max, Team,
     Enterprise 계정 가운데 하나를 고른다. Windows에서는 앱 밖의 일반 PowerShell
     창에서 실행한다.

     ```powershell
     & "C:/Users/you/AppData/Roaming/Claude/claude-code/2.1.280/claude.exe" auth login
     ```

     데스크톱 앱의 로그인은 CLI로 이어지지 않는다. API 키(Console) 로그인은
     거부된다.
   - **Codex:** `<codex> login`을 실행해 ChatGPT로 로그인한다. 그다음
     `<codex> login status`가 `Logged in using ChatGPT`를 보고해야 한다. Codex
     fork는 프로젝트 폴더가 Git 작업 트리이거나, Codex의 `config.toml`에서 신뢰
     표시(trusted)된 프로젝트여야 돈다.

3. **fork가 돌 수 있는지 확인한다.** `fork doctor` 명령은 읽기 전용이며 모델을
   띄우지 않는다. `PYTHONPATH`를 설정한 채(2단계) vault 루트에서 실행한다.

   macOS/Linux:

   ```bash
   .venv/bin/python -m osk.cli fork doctor --harness claude
   .venv/bin/python -m osk.cli fork doctor --harness codex
   ```

   Windows (PowerShell):

   ```powershell
   .venv\Scripts\python.exe -m osk.cli fork doctor --harness claude
   .venv\Scripts\python.exe -m osk.cli fork doctor --harness codex
   ```

   **확인:** 첫 줄이 `claude: background` 또는 `codex: background`다.
   `foreground — <이유>` 같은 줄은 고칠 것을 알려 준다.
   [문제 해결](#문제-해결)을 본다.
   - `--session` 없이 실행하면 `fork doctor`는 실행한 폴더, 여기서는 vault를
     점검한다. 그래서 `my-app`의 Git 상태, Codex 신뢰, `.claude` 설정은 보지
     못한다.
   - 실제 대화 하나를 그 대화의 폴더에서 점검하려면 `--session <대화ID>`를
     붙인다.
     - Claude에서는 `~/.claude/projects/` 아래 전사 파일의 이름에서 `.jsonl`을 뗀
       것이 ID다.
     - Codex에서는 `~/.codex/sessions/` 아래 `rollout-….jsonl` 파일 이름 끝의
       ID다.
   - 전체 보고는 `--json`을 붙여 본다.

**브리지, 프록시, API 키.** fork는 환경에서 `OPENAI_API_KEY`, `CODEX_API_KEY`,
`OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`을 언제나 지운다.

- **Codex.** fork는 공식 백엔드 `https://chatgpt.com/backend-api/codex`로 고정되므로,
  최상위 `openai_base_url`로 걸어 둔 로컬 브리지나 프록시를 거치지 않는다. 설정 층
  가운데 하나라도 아래 중 하나를 정하면 실행을 거부한다. 층은 시스템 파일, 사용자의
  `~/.codex/config.toml`, 각 프로젝트의 `.codex/config.toml`이다.
  - fork가 고정하는 키를 정하는 활성 profile
  - 공식 백엔드가 아닌 `model_providers.openai` base URL
  - 공식이 아닌 `chatgpt_base_url`

  모델이 `chatgpt-web/…` 브리지 경로인 대화도 거부한다.
- **Claude.** 다음 가운데 하나에 해당하면 fork가 실행을 거부한다.
  - 환경에 퍼스트파티가 아닌 `ANTHROPIC_BASE_URL`이 있거나,
    `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`가
    설정돼 있다.
  - `settings.json`(사용자 것, 또는 프로젝트의 `.claude/settings.json`·
    `.claude/settings.local.json`)이 `apiKeyHelper`를 정하거나, `env` 아래에 API
    키·base URL·공급자 전환을 둔다.

`fork doctor`는 이 경우들을 모두 보고한다. fork 결과는 `.osk/growth/runs/`에
남는다. 대화 하나의 결과를 보려면
`.venv/bin/python -m osk.cli integration status --harness <claude|codex> --conversation <대화ID>`
(Windows: `.venv\Scripts\python.exe`)를 `fork doctor`와 같은 방식으로 실행한다.
전체 설계는
[SETUP → 대화별 검토 훅](SETUP.md#대화별-검토-훅-최종-답변-stop-9회)과
[response-growth.md](response-growth.md)(영문)를 본다.

## 최신 릴리스로 갱신하기

릴리스 페이지를 지켜볼 필요는 없다. 세션 시작 훅과 MCP `overview`가 하루에 한 번
정본 저장소에 릴리스 태그를 묻는다(`git ls-remote` — vault의 내용은 보내지 않는다).
새 릴리스가 있으면 Claude Code·Codex 화면에
`osk-system 새 릴리스 v4.1.0 (이 vault는 v4.0.0) — …` 같은 경고가 기기마다 하루 한 번
뜬다. 경고 끝에 그 판의 릴리스 노트 주소가 붙는다. 에이전트에게 **"osk 업데이트해 줘"**라고 요청하면 에이전트가 아래 절차를 함께
밟는다 — 변경집합을 보여 주고 사용자의 승인을 기다린다. 확인은 백그라운드에서 돌므로
알림은 확인한 다음 세션부터 보인다. 지금 확인하려면 `osk.update --check`를 실행한다
(태그만 묻는다). 확인을 끄려면 `.osk/config.json`에 `"update_check": false`를 둔다.

릴리스는 `origin`이 어디를 가리키든 언제나 정본 저장소에서 받는다. `PYTHONPATH`를
설정한 채(2단계) vault 루트에서 실행한다. v3에서 v4처럼 메이저 판을 올릴 때는 먼저
[판 올리기](UPGRADING.ko.md)를 읽는다.

macOS/Linux:

```bash
.venv/bin/python -m osk.update            # report only: "version" (newest release) vs "current"
.venv/bin/python -m osk.update --apply    # prints the changeset, changes nothing, exits 2
```

Windows (PowerShell):

```powershell
.venv\Scripts\python.exe -m osk.update
.venv\Scripts\python.exe -m osk.update --apply
```

첫 줄은 보고만 한다 — `version`(최신 릴리스)과 `current`(지금 판본)를 나란히
보여 준다. `current`가 이미 `version`과 같다면 최신 상태이니 여기서 멈춘다.
그래도 `--apply`를 실행하면 빈 변경집합으로 승인을 요청한다.

그렇지 않다면 첫 `--apply`는 아무것도 바꾸지 않고 변경집합만 출력한다.
변경집합에는 추가(`add`)·갱신(`update`)·삭제(`remove`)할 파일과 사용자가 고친
문서(`conflict`)가 나온다. 그러고는 종료코드 2와 `"approval_required": true`로
끝난다.

변경집합을 읽고 동의하면 한 시간 안에 **같은 명령을 한 번 더** 실행한다. 이 실행이
갱신을 적용하고 0으로 끝난다. 그사이 릴리스나 파일이 바뀌면 갱신기가 다시 확인을
요청한다.

적용 단계가 알아서 처리하는 것:

- **동기화 데몬.** 이 vault의 데몬이 돌고 있으면 적용 단계가 멈췄다가 끝난 뒤 다시
  띄운다. 보고의 `daemon` 필드에 `stopped`와 `restarted`가 보인다. 다시 띄우지
  못하면 `daemon.note`가 직접 띄우라고 알려 준다. Windows에서 데몬을 띄우는 예약
  작업이 없을 때 이렇게 된다.
- **사용자가 고친 문서.** 덮어쓰지 않는다. 새 판은 `<파일>.upstream-<버전>`으로 옆에
  저장되니 직접 병합한다.
- **사용자가 고친 엔진 파일.** 엔진 파일에 로컬 수정이 있으면 갱신 전체가 멈춘다.
  엔진은 정본에서 고친다.

갱신 뒤에는:

- osk를 쓰는 Claude Code·Codex 세션을 모두 **재시작**한다. 갱신을 적용할 때마다
  해야 MCP 서버가 새 엔진을 읽는다. 재시작 전까지 쓰기는 `적재판 … ≠ 디스크판`으로
  거부된다. 메모리에 올라간 엔진과 디스크의 엔진이 다르다는 뜻이다.
- 갱신 목록에 `_governance/_engine/requirements.txt`가 있으면 **의존성을 다시
  설치한다.** 2단계의 `pip install` 줄을 다시 실행하면 된다.
- 갱신된 파일을 **커밋**하거나 데몬에 맡긴다.
- 이 vault를 함께 쓰는 **다른 기기**에서는 `osk.update`를 실행하지 않는다. pull하고,
  세션을 재시작하고, 의존성이 바뀌었으면 다시 설치한다.

**확인:** `--apply` 없이 실행한 `osk.update`가 `current`와 `version`에 같은 값을
보고하고, `validate`는 여전히 `"verdict": "PASS"`로 끝난다.

갱신이 중간에 끊기면 다음 `--apply`가 먼저 복구한다. `osk.update` 자체가 시작되지
않으면 복구 스크립트를 실행한다. 이 스크립트는 표준 라이브러리만 쓴다.

- macOS/Linux: `python3 _governance/_engine/scripts/recover.py --apply`
- Windows: `py -3 _governance\_engine\scripts\recover.py --apply`

## 문제 해결

**설치와 연결**

| 증상 | 해결 |
|---|---|
| `osk`를 치면 화상 키보드가 뜨거나 *command not found*가 나온다 | `osk` 명령은 없다. `PYTHONPATH`를 설정한 채 vault 루트에서 `.venv/bin/python -m osk.cli …`(Windows: `.venv\Scripts\python.exe -m osk.cli …`)를 실행한다. |
| `No module named 'osk'` | 이 터미널에 `PYTHONPATH`가 설정되지 않았거나, vault 루트가 아닌 곳에서 실행했다(2단계). |
| `No module named 'mcp.server.fastmcp'` | 다른 Python을 쓰고 있거나 mcp 2.x가 설치돼 있다. vault의 `.venv` Python을 쓰고, `mcp<2`로 묶인 `_governance/_engine/requirements.txt`를 다시 설치한다. |
| Windows: `Asia/Seoul`에 대한 `ZoneInfoNotFoundError` | venv에 `tzdata` 패키지가 없다. 의존성을 그 venv에 다시 설치한다. |
| `claude mcp list`에 *Connected*가 없거나, Codex가 서버를 시작하지 못한다 | 등록한 명령이 vault의 `.venv` Python(Windows는 `.venv/Scripts/python.exe`)을 써야 한다. 같은 명령을 터미널에서 직접 실행해 오류를 본다. 정상 서버는 아무것도 출력하지 않고 클라이언트를 기다린다. Ctrl+C로 멈춘다. |
| 세션 시작에 osk 문구가 없다 | Claude Code는 시작할 때만 훅을 읽으므로 새 세션을 열고 `/hooks`를 확인한다. Codex에서는 `/hooks`에서 항목을 신뢰한다. 두 하네스 모두 MCP 등록만으로는 훅이 설치되지 않으며, 훅 명령마다 vault의 `.venv` Python을 써야 한다. 3b의 손 실행 시험을 돌려 본다. `doctor`는 등록됐지만 이 기기에서 한 번도 불리지 않은 훅을 가려 준다. |
| 훅 문구에 `착지 미정`이나 `scope 결속이 없다`가 되풀이된다 | 이 저장소의 키가 아직 결속되지 않았다. scope를 만들거나 고른다(5단계 2번). |

**에이전트가 전하는 쓰기 거부**

| 거부 문구 | 뜻과 해결 |
|---|---|
| `아직 없는 군집이라 … 새 군집을 만든다` | 새 최상위 군집에는 한 번의 확인이 필요하다. 확인한 뒤 에이전트가 한 시간 안에 같은 요청을 다시 보내게 한다. |
| `착지가 정해지지 않았다 — space를 지정하라` | 세션이 결속되지 않았는데 쓰기에 `space`가 없다. 한 번은 `session`과 함께 `space="00_Scope/<이름>"`을 넘긴다. |
| `에 결속돼 있다` | 세션이 다른 scope에 결속돼 있다. `space`를 빼거나, 여러 프로젝트에 걸친 지식이라면 `00_Domain/`에 쓴다. |
| `1회용 대화 id` | 대화 UUID를 세션 키로 썼다. 훅이 출력한 키를 쓴다. |
| `군집이 비어 있다` | 새 군집은 폴더 이름과 같은 허브로 시작해야 한다. 허브부터 만든다. |
| `drafter는 하네스명이 아니라 모델명` | `drafter`에는 `claude`나 `codex`가 아니라 모델 자신의 이름을 소문자로, `provider:` 접두사 없이 쓴다. |
| `summary`와 함께 `한도 80`이나 `한 줄`, 또는 *at most 80 characters* | 요약은 80자 이내의 한 줄이며 `[[`를 넣을 수 없다. |
| `앵커 편집` 또는 `expect_hash` | 본문 전체를 바꾸려면 `read_node`의 `hash`가 필요하다. 작은 수정은 `old_text`/`new_text`로 한다. |
| `적재판 … ≠ 디스크판` | MCP 서버가 시작된 뒤 갱신이나 pull로 디스크의 엔진이 바뀌었다. 세션을 재시작한 뒤 요청을 다시 보낸다. |

**갱신과 동기화**

| 증상 | 해결 |
|---|---|
| `osk.update --apply`가 종료코드 2로 끝난다 | 예상된 동작이다. 그 실행은 변경집합을 보여 주기만 한다. 검토한 뒤 한 시간 안에 같은 명령을 다시 실행한다. |
| `[중단] 엔진 파일에 로컬 수정이 있다 …` | 엔진 파일이 기록된 기준선과 다르거나, 기준선이 아예 없다. 표 아래 설명을 본다. |
| `동기화 데몬 잠금이 잡혀 있는데 … 프로세스를 찾지 못했다` | 데몬이 돌고 있지만 상대 경로로 시작돼 갱신기가 찾지 못한다. 데몬을 멈추고 절대 경로로 다시 시작한 뒤 갱신을 다시 실행한다. |
| 데몬이 `sync 비활성 — SYNC_ENABLED=1 …`로 끝난다 | 데몬의 환경에 `SYNC_ENABLED=1`을 설정한다. Windows 작업이라면 [동기화 절](#선택-git으로-vault-동기화하기)의 `cmd.exe` 동작을 쓴다. |
| 데몬이 다른 브랜치나 `main` 부재를 들어 동기화를 거부한다 | 데몬은 `main`만 동기화하며, 다른 브랜치에서 커밋하지 않은 수정을 옮기지 않는다. 작업을 커밋한 뒤 `git switch main`을 실행한다. |
| 갱신 뒤 `<파일>.upstream-<버전>` 파일이 생겼다 | 그 문서를 고친 적이 있어서 새 판을 옆에 저장한 것이다. 변경을 손으로 병합한 뒤 여분 파일을 지운다. |

`엔진 파일에 로컬 수정이 있다` 오류의 원인은 둘 중 하나다.

- **clone이 기준선을 기록한 적이 없다.** 2단계를 건너뛰었거나 `main`에서 clone한
  경우다. `.venv/bin/python -m osk.update --apply --adopt`(Windows:
  `.venv\Scripts\python.exe -m osk.update --apply --adopt`)를 실행해 변경집합을
  검토한 뒤, 같은 명령을 다시 실행한다. 교체되는 파일은 각각
  `<파일>.local-<버전>`으로 남는다. 그 파일들을 고친 적이 없다면 사본은 지운다.
- **엔진 파일을 직접 고쳤다.** 수정을 되돌린다. 엔진은 정본에서 고친다.

**fork 검토** (`fork doctor`가 `foreground —` 뒤에 출력하는 이유)

| 이유 | 해결 |
|---|---|
| `subscription fork CLI is not configured` | `.osk/response-growth.json`이 없거나, 이 하네스의 항목이 없다. |
| `configure an existing absolute native CLI path`, 또는 `[WinError 3] The system cannot find the path specified` | `.osk/response-growth.json`의 경로가 존재하지 않는다. 실제 경로를 찾아(위 1번) 파일을 고친다. |
| `matching Claude Desktop CLI is unavailable` 또는 `matching Codex Desktop CLI is unavailable` | 이 대화를 기록한 CLI 버전이 데스크톱 앱에 더는 없다. 이 대화의 검토는 세션 안에서 이뤄진다. 설치돼 있는 버전으로 기록된 대화는 영향이 없다. |
| `configured CLI version differs from the source harness` | 따로 설치한 CLI의 버전이 대화를 기록한 버전과 다르다. 그 버전의 바이너리를 등록한다. |
| `… subscription login is required; no API fallback` | 그 CLI로 claude.ai(`auth login`) 또는 ChatGPT(`login`) 로그인을 한다. API 키는 절대 쓰이지 않는다. |
| `not a Git worktree or trusted Codex project` | Codex를 Git 저장소에서 실행하거나, Codex에서 그 폴더를 신뢰한다. |
| `non-first-party endpoint`, `unverified API/provider path`, `unverified provider` | 프록시, profile, 공급자, API 설정이 fork를 다른 곳으로 보낼 수 있다. [브리지, 프록시, API 키](#선택-백그라운드-fork-검토)를 본다. |

## 다음으로 읽을 것

- [SETUP.md](SETUP.md)는 운용 참고서다. 먼저 볼 만한 절:
  - [MCP 서버](SETUP.md#mcp-서버)
  - [CLI 명령표](SETUP.md#cli)
  - [scope 기억 주입 훅](SETUP.md#scope-기억-주입-훅-sm-show)
  - [대화별 검토 훅](SETUP.md#대화별-검토-훅-최종-답변-stop-9회)
  - [Codex 훅 등록](SETUP.md#codex에도-훅을-등록한다)
  - [Scope에서 Domain으로 정기 재검토](SETUP.md#scope에서-domain으로-정기-재검토)
  - [동기화 데몬](SETUP.md#동기화-데몬)
  - [정본 릴리스와 갱신](SETUP.md#정본-릴리스와-갱신)
- [response-growth.md](response-growth.md)(영문)는 fork 검토가 어떻게 도는지를 캐시
  실측과 한계와 함께 설명한다.
- [space-layout-migration.md](space-layout-migration.md)(영문)는 `= Scope` 루트를
  쓰는 vault와 v3.20.1에서의 복구를 다룬다.
- [WINDOWS-SHELL.md](WINDOWS-SHELL.md)는 Git Bash가 네이티브 도구에 넘기는 경로
  모양의 인자를 어떻게 바꾸는지 설명한다.
- 규칙은 통치 문서가 정한다. [헌법](../_governance/Constitution.md),
  [시행령](../_governance/Bylaws.md), [Mechanism](../_governance/Mechanism.md)
  순서로 읽는다. [Workbench 계약](../_governance/Workbench-Contract.md)은 운영
  scope를 다룬다.
- [엔진 README](../_governance/_engine/README.md)에는
  [알려진 한계](../_governance/_engine/README.md#알려진-한계)가 정리돼 있다.
- [README](../README.ko.md)는 프로젝트 개요와 설계의 배경을 담는다.
