# osk-system 설치·운용

인스턴스의 실행 방법. 규범이 아니라 **운용 문서**다.

체계 자체의 규범은 `_governance/`(헌법·시행령·Mechanism·Workbench 계약)에
있다. 그 **정본은 정본 저장소** <https://github.com/lpaiu-cs/osk-system> 이고,
각 인스턴스는 릴리스를 갱신으로 받는다(아래 '정본 릴리스와 갱신').

## 구성

```
_governance/
  Constitution.md 등   통치 문서 4종 + records/ (사료) — Space 밖 통치 구획의 특수 노드
  _engine/
    osk/               엔진 — 계약·승인·인과 DAG·검색·검증기·릴리스·갱신
    mcp_server.py      외부 표면(MCP, stdio) — 도구 12종
    sync_daemon.py     동기화 데몬(git만; 검색·색인은 서빙하지 않는다)
    vault_sync.py      순수 git 헬퍼
    tests/             회귀 수트
    scripts/           발행 매니페스트, launchd/systemd 예시
= Scope/ = Domain/ = Person/   지식 공간
= Scope/Workbench/_ledger/     대장 — 승인·pin·세션 라우팅·갱신 저널 (append-only)
```

## 준비

Python 3.12. 실의존성은 네 가지뿐이다 — `mcp`, `pydantic`, `PyYAML`, `rank-bm25`.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r _governance/_engine/requirements.txt
```

`mcp`는 major가 묶여 있다(`>=1.28,<2`). 외부 표면이 1.x의 `FastMCP`로 쓰였는데
2.0이 그 모듈을 없앴다 — **`requirements.txt`를 갱신으로 받았으면 pip을 다시
돌려야** 이미 만든 venv에 반영된다.

## Windows

엔진은 Windows에서도 돈다(잠금은 `msvcrt`, tz는 `tzdata` 패키지로 보충한다).
이 문서의 명령은 POSIX 표기이니 아래 셋만 바꿔 읽는다.

Git Bash에서 Windows 네이티브 도구를 쓰면 `/repos/...`·`/sdcard/...` 인자가
경로로 변환될 수 있다. [네이티브 명령 경계](WINDOWS-SHELL.md)의 호출별 예외,
출력 보존, 쓰기 후 본문·해시 검증 규칙을 따른다. OSK 버전 회귀와는 별개다.

| | POSIX | Windows (PowerShell) |
|---|---|---|
| 인터프리터 | `python3.12` | `py -3.12` |
| venv 실행 파일 | `.venv/bin/python` | `.venv\Scripts\python.exe` |
| 환경변수 + 명령 | `VAR=값 명령` | `$env:VAR="값"; 명령` |

준비와 CLI는 이렇게 된다:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r _governance\_engine\requirements.txt
$env:PYTHONPATH="_governance\_engine"; .venv\Scripts\python.exe -m osk.cli validate
```

환경변수는 그 세션에만 남는다. 여러 명령을 이어 쓸 것이면 `$env:PYTHONPATH`를
한 번만 두고 이후 명령에서는 생략한다. `cmd.exe`라면 `set VAR=값`을 별도 줄에
쓴다(`set` 뒤 값에 따옴표를 붙이면 따옴표까지 값이 된다).

데몬의 상시 실행은 `scripts/`의 launchd·systemd 예시에 해당하는 것이 없다 —
**작업 스케줄러**에 `.venv\Scripts\python.exe _governance\_engine\sync_daemon.py`를
등록하고 환경변수 `SYNC_ENABLED=1`을 준다.

## MCP 서버

에이전트가 이 체계를 다루는 **유일한 외부 표면**이다. 도구는 열둘이며 그
목록의 정본은 Mechanism §6-2 7항이다 —
`overview` `search` `read_node` `run_validators` `create_node` `update_node`
`move_nodes` `move_cluster` `record_candidate` `append_raw` `read_raw`
`scope_memory`.

`_raw/` 세션 기록은 작업 검색에서 빠지므로(헌법 11조 3항) `read_raw`는 질의가
아니라 **좌표**를 받는다. 노드의 `derived-from`에 든 `[[경로#N]]`을 그대로 넣으면
그 라운드가 열린다 — 근거에서 증거로 가는 데 번역이 끼지 않는다. 좌표를 모르면
`space`로 기록 목록부터, 경로만으로 라운드 목차부터 본다.

보호영역 권위와 pin은 **표면에 영구히 노출하지 않는다**(Mechanism §6-2 2항).
지정·해제·승인·반려의 발의는 사용자 전속이므로 아래 CLI에만 있다.

Claude Code에 user scope로 등록:

```bash
claude mcp add --scope user osk-system -- <REPO>/.venv/bin/python <REPO>/_governance/_engine/mcp_server.py
```

설정 파일을 직접 쓰는 클라이언트(Antigravity의 `~/.gemini/config/mcp_config.json`,
Codex 등)는 `.mcp.json.example`을 그대로 베끼고 `<REPO>`만 바꾼다. 전송은 stdio다.

Windows에서는 두 경로의 실행 파일 부분이 `.venv\Scripts\python.exe`가 되고,
JSON 안의 역슬래시는 `\\`로 이스케이프한다 —
`"command": "C:\\vault\\.venv\\Scripts\\python.exe"`.

대상 인스턴스는 `OSK_VAULT_ROOT`로 바꿀 수 있다. 지정하지 않으면
`_governance/_engine/`에서 두 단계 위(= `_governance`의 부모)를 vault 루트로 본다.

## CLI

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli --help
```

| 명령 | 하는 일 |
|---|---|
| `validate` | 검증기 수트 전체 |
| `status` | 체계 현황 |
| `search` / `view` | 작업 검색 / 열람 검색 |
| `check` | 권한 사전 검사 |
| `validators` | **사용자 전속** — 검증기 활성화 현황·전환 (Mechanism §6-1) |
| `raw append` / `raw status` | `_raw/` 세션 기록 — 훅 경로(아래) |
| `integration capture` / `integration status` / `integration prompt` / `integration review` | 실제 대화별 포착·통합 대기·검토 결과 |
| `integration list` / `integration catchup` | 알려진 대화의 통합 대기 목록·종료 꼬리 따라잡기 |
| `growth plan` / `growth prompt` / `growth run` / `growth review` | Scope 비교 후보·미리보기·한정 실행·Domain 검토 결과 |
| `sm show` / `sm write` | scope 기억 — SessionStart 훅 경로(아래) |
| `tidy list` / `tidy prompt` / `tidy settle` | 정돈 — 미처분 퇴출 항목의 목록·전용 세션 프롬프트·처분 기록 (Mechanism §9-3, 아래) |
| `protect` / `unprotect` | **사용자 전속** — 보호영역 지정·해제 |
| `approve` / `revert` | **사용자 전속** — 변경집합 승인·반려 |
| `store-reconcile` | 내용 주소 저장소를 파일 이름 기준으로 판독·이행 (EOL 이행) |
| `update` / `release` | 갱신 / 릴리스 선언 — 인자를 그대로 위임한다 |

보호영역 권위(`protect`·`unprotect`·`approve`·`revert`)는 MCP로 노출되지 않는다.
헌법 10조 1~2항이 지정·해제·승인·반려를 사용자에게 전속시키고, 표면은 그 경계를
물리적으로 지킨다(Mechanism §6-2 2항). 이 명령들은 대화형 단말을 요구하며,
표준입력이 단말이 아니면 묻지 않고 중단한다 — 파이프로 무인 승인이 성립하지
않게 한다.

`check`는 적용 봉투를 기계로 평가할 수 있기 전까지 **언제나 보류를 낸다**. 강제할
수 없는 것을 강제한 척하지 않는다.

### scope 기억 주입 훅 (`sm show`)

scope 기억은 그 scope에서 **지금 살아 있는 배울 점**이며 상한이 있다(Mechanism §9-2).
모든 세션과 기기가 같은 것을 보므로 **세션 한정 작업 상태는 적지 않는다.**
상한은 저장 용량의 제한이 아니라 **승격의 문턱**이다 — 넘기면 쓰기를 거부하고, 자리값
못하는 엔트리를 먼저 정리한 뒤 그래도 모자라면 노드로 올리게 한다.

**전문이 세션 시작에 문맥에 있어야 이 압력이 작동한다.** 도구를 불러야 보이는 것이면
보이지 않고, 보이지 않는 것은 통합되지 않는다. `CLAUDE.md`에 "쓰라"고 적어 두는
것으로는 부족하다 — 지시는 읽히지만 눈앞에 없으면 쓰이지 않는다.

```bash
.venv/bin/python -m osk.cli sm show --session <세션 키>
```

**세션 키는 저장소 이름처럼 세션이 바뀌어도 같은 값이어야 한다**(`open-hwp`·`rhwp` 꼴).
첫 성공이 그 키를 영구 결속하므로, 하네스가 주는 1회용 대화 id(UUID)를 그대로 넘기면
다음 세션이 같은 기억에 닿지 못한다 — 쓰기 표면은 UUID 꼴 키를 거부한다(Mechanism §6-2 6항).
동봉된 훅은 **본 저장소 디렉터리 이름**을 키로 쓴다(워크트리는 `git-common-dir`의 부모로
접히므로 워크트리마다 키가 갈리지 않는다).

**출력은 JSON이 아니라 전문 그대로다.** 훅이 이 값을 문맥에 그대로 넣으므로, 감싸는
껍데기가 있으면 훅마다 벗기는 코드를 쓰게 된다. 결속이 없으면 빈 출력이고 주입할 것도
없다 — 오류가 아니다. 상태 전체가 필요하면 `--json`을 준다.

결속이 아직 없으면 **빈 출력에 종료코드 0**이다 — 새 저장소의 첫 세션이 그 상태이므로
오류가 아니다. 훅은 stdout을 그대로 쓰면 된다.

**같은 훅이 정돈도 싣는다**(Mechanism §9-3). scope 기억의 상한 초과 거부 직후에 잘려 나간
줄은 퇴출 기록부 `_ledger/evictions.jsonl`에 `evict`로 남는다(§9-2 12항) — 엔진은 자르지
않으며 호출자가 스스로 뺀 것을 적을 뿐이고, 거부와 무관한 평소의 정리는 적지 않는다.
결속이 선 세션이 시작되면 훅은 그 scope의 미처분 항목 중 **오래된 것부터 3건**과
Workbench의 경유 노드를 함께 실어 첫 도구 호출에 처분을 함께 실으라고 지시한다. 벽이
아니다 — 본 작업이 먼저면 넘어가도 되고 항목은 대장에 남는다. 출구는 노드로 증류·기존
노드에 통합·폐기이며, 어느 쪽이든 `settle`을 적어야 처분이다:

```bash
.venv/bin/python -m osk.cli tidy list                                  # scope별 미처분·나이
.venv/bin/python -m osk.cli tidy settle <rid> node --target "<노드 제목>"   # 증류 (통합은 merged)
.venv/bin/python -m osk.cli tidy settle <rid> discarded                # 폐기
```

건너뛴 것은 `osk status`의 `evictions`에 보인다. 가장 오래된 항목이 **14일**을 넘으면
훅이 "정돈이 밀렸다"를 주입문 맨 앞에 세우고, 그래도 밀리면 `tidy prompt`가 **전용 정돈
세션의 프롬프트**를 낸다 — 새 세션에 붙여 넣으면 되며, 스케줄러가 있으면 그 프롬프트로
`claude -p`를 띄울 수 있다(세션 키는 그 scope의 정본 키, 승인은 우회하지 않는다).

쓰기는 stdin으로 전문을 받아 **전체 치환**한다. 기존 내용이 있으면 `--expect-hash`가
필수다. **이 명령은 git을 부르지 않는다** — 동기화는 데몬이 맡는다.

```bash
printf '%s' "$새전문" | .venv/bin/python -m osk.cli sm write   --session <키> --expect-hash <방금 받은 hash>
```

### 케이던스 훅 (`scripts/hooks/claude_prompt_submit.py`)

주입은 세션 시작 한 번으로 끝나지 않는다. **UserPromptSubmit** 훅이 user 턴을
세어, **9턴**에 기억의 지금 전문·해시·여유·세션 키를 주입하며 "다음 도구 호출에
**함께** 실어라"를 지시하고, **15턴**까지 갱신이 없으면 다시 주입하되 단독 턴을
허용한다. 공유 기억의 해시 변화는 이 대화가 통합됐다는 증거가 아니다.
완료된 원문과 실제 검토 결과를 대화별로 확인하며, 같은 대화의 재개에도 대기를 유지한다.

```
<인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_prompt_submit.py
```

- 계수·검토 대기는 **기기 로컬**이다(vault 루트·하네스·실제 대화 ID 단위).
  지식과 원문은 vault에 남는다. 기억이 비어 있어도 통합 시점은 알린다.
- 두 훅 모두 **엔진을 import한다** — 등록한 인터프리터가
  인스턴스의 `.venv`여야 한다(의존이 없으면 조용히 아무것도 하지 않는다).
- 두 훅 모두 주입문에 **그 세션의 키**를 싣는다. `scope_memory`·`append_raw`의
  `session` 인자에 그 값을 그대로 쓴다 — 지어낸 키는 첫 성공에 영구 결속되어
  대장에 다시 오지 않을 행을 남긴다.

상한 초과 뒤에는 **복구 대기**를 세션 시작과 9·15턴에 싣는다. 복구할 때는 새
요약 추가 대신 현재 저장본의 엔트리를 골라 정리하고, 남길 지식은 기존 노드에
먼저 보존한다. 아직 덜어 낸 줄이 없어 `evict`가 0건이어도 동작한다. 복구 대기는
CLI `status`의 `scope_recovery`에 별도로 보이며, 읽기·주입·같은 내용의 재저장으로
사라지지 않는다. 이는 거부된 새 초안을 보관하는 기능이 아니다.

### Codex에도 훅을 등록한다

Codex의 MCP 등록과 훅 등록은 별개다. `~/.codex/hooks.json`에 다음처럼 등록한다.
`<PYTHON>`은 인스턴스 가상환경의 실행 파일, `<ENGINE>`은 그 인스턴스의
`_governance/_engine` 절대 경로로 바꾼다. 명령은 해당 기기의 셸에서 실행
가능해야 한다. 기존 hooks.json이 있으면 이벤트 목록에 추가하며 덮어쓰지 않는다.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear|compact",
      "hooks": [{
        "type": "command",
        "command": "<PYTHON> <ENGINE>/scripts/hooks/claude_session_start.py",
        "timeout": 30,
        "statusMessage": "osk scope 기억과 복구 확인"
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "<PYTHON> <ENGINE>/scripts/hooks/claude_prompt_submit.py",
        "timeout": 30,
        "statusMessage": "osk 기억 통합 시점 확인"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "<PYTHON> <ENGINE>/scripts/hooks/capture_stop.py",
        "timeout": 30,
        "statusMessage": "osk 완료된 원문 포착"
      }]
    }]
  }
}
```

파일명의 `claude_`는 기존 등록 경로를 유지하기 위한 이름이다. 두 하네스 모두
stdin의 `cwd`·`session_id`를 주며, 두 훅은 stdout에
`{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"…"}}`
형태의 JSON을 출력한다. 케이던스 훅의 `hookEventName`은 `UserPromptSubmit`이다.
`additionalContext` 안의 본문을 문맥에 싣는다. `[osk …]`로 시작하는 평문은
Codex 0.153.4에서 JSON 출력으로 오인되어 주입에 실패하므로 이 봉투를 유지한다.
케이던스에 도달하지 않은 턴은 빈 stdout으로 성공한다.
SessionStart는 `overview` 호출과 안정된 세션 키도 안내한다. 결속이 없으면
착지를 추측하지 말고 확인하라고 지시한다.

Codex는 새로 추가하거나 바뀐 훅 정의를 사용자가 신뢰하기 전까지 건너뛴다.
CLI의 `/hooks`에서 각 정의를 확인하고 신뢰한 뒤 새 세션에서 주입을 확인한다.
설정 파일 존재만으로 실제 실행을 판정하지 않는다. 큰 출력은 기본 문맥 상한에서
파일로 넘겨질 수 있으므로 주입 메시지의 저장 경로가 보이면 그 전문도 확인한다.
계약과 설정 형식은 [Codex Hooks 문서](https://learn.chatgpt.com/docs/hooks)를 따른다.

Personalization에는 저장 경계를 간단히 두어도 된다. 다만 훅의 설치·신뢰·새 세션
실행을 확인하기 전에는 `overview`·scope 읽기와 주기적 통합 안내를 제거하지 않는다.

### 세션 기록 훅 (`raw append`)

표면의 `append_raw`는 에이전트가 **서술한** 라운드를 받는다. 헌법 4조 3항이 명하는
것은 전량 포착이므로, 전사를 그대로 나르는 기계 경로를 따로 둔다 — 통로와 계약은
같고 입력만 하네스에서 온다. 이 명령은 사용자 전속이 아니므로 대화형 확인을 걸지
않는다(걸면 훅에서 쓸 수 없다).

대화 바이트는 **stdin의 JSON 봉투**로 받는다. argv는 임의 바이트를 안전히 나르지
못한다 — 따옴표·길이 상한·콘솔 인코딩이 전부 전사를 훼손한다.

```bash
printf '%s' '{"rounds":[{"user":"…","agent":"…"}]}' \
  | .venv/bin/python -m osk.cli raw append \
      --session <세션 키> --record <대화 이름> --space "= Scope/<이름>"
```

- 봉투는 세 모양을 받는다 — `{"rounds":[…]}` · 라운드 하나(`{"user":…,"agent":…}`) ·
  배열. `session`·`record`·`space`는 봉투에 넣어도 되고, **플래그가 봉투를 이긴다**.
- 배치는 **한 번의 쓰기**다. 중간 라운드가 거부되면 아무것도 쓰지 않는다 — 라운드마다
  따로 쓰면 거부 지점에서 "있었던 대화의 일부"가 남는다.
- 라운드 번호는 엔진이 매긴다. 응답의 `round_refs`가 그대로 `derived-from`의 근거
  표기이고, 거부 시 종료코드는 0이 아니며 `violations`가 이유를 싣는다.
- 출력은 콘솔 코드페이지와 무관하게 **UTF-8 바이트**다.

**중복이 유실보다 위험하다.** 훅은 같은 대화에 여러 번 깨어나는데, 그때마다 처음부터
이어 붙이면 같은 라운드가 다른 번호로 두 번 앉는다. `_raw/`는 append-only라 사후에
되돌릴 수 없다. 엔진은 **직전 꼬리와 내용이 같은 배치를 거부**하지만(Mechanism §9
7항) 그것은 마지막 방어선일 뿐이다 — 중간에 한 라운드만 새것이 섞여 오면 앞의
중복이 함께 들어간다. 어댑터는 붙이기 전에 센다.

```bash
.venv/bin/python -m osk.cli raw status --session <키> --record <이름>
# → {"rounds": 12, "next_index": 13, "damaged": false, …}
```

어댑터는 `rounds`를 읽고 **그 뒤부터만** 보낸다. `damaged`가 참이면 기록의 index 열이
손상된 것이며, 그 위에는 이어 쓰지 않는다(append도 같은 이유로 거부한다).

동봉된 `osk.transcripts`는 Claude/Codex JSONL에서 완료된 라운드를 읽는다.
`integration capture`는 안정된 기록 이름에 저장본 접두부를 대조한 뒤 새 꼬리만
`raw.append_rounds`로 보낸다. 저장 후 응답이나 로컬 커서가 유실돼도 중복 append를
하지 않는다. 미완료 라운드·손상·지원하지 않는 전사 형식은 포착 완료로 세지 않는다.
Codex의 새 `UserMessage` 입력도 포착한다. 새 라운드는 기록 안에 직렬화 판본 표식을
남긴다. 표식이 없는 v3.14 과거 라운드는 당시 `user_message` 형식으로 엄격히 대조해
원문·출처 해시를 유지하고, 이후 라운드는 새 형식으로 이어 쓴다. 새 형식의 불일치를
구형 대조로 넘기지 않는다. 과거 포착기가 생략한 native 입력은 소급해서 raw에 넣지
않으므로 상세 확인에는 원래 전사가 필요하며, 이 한계를 통합 검토 안내에도 표시한다.
Stop 훅도 포착만 하며 작업을 강제로 연장하지 않는다. Stop이 전사의 최종 완료 표식보다
먼저 실행되거나 생략되면 `integration catchup`이 이미 등록된 대화의 꼬리를 따라잡는다.
Claude의 Stop에도 같은 `scripts/hooks/capture_stop.py`를 등록한다.
새 대화에 과거 미완료 지시를 강제로 주입하지 않으며, 전용 성장 실행이 그 검토를 맡는다.
중단된 라운드는 중단 사실을 함께 보존하며 정상 종료나 지식 증류 성공으로 세지 않는다.

파일 읽기 결과는 원본 경로·도구 결과 위치·해시 참조로 대신한다(Bylaws §2 2항).
복합 셸 출력은 파일 전문과 실험 출력의 자동 구분을 보장할 수 없어 원본 전사 참조로
남기고 포착 범위 진단을 기록한다. 이 경우 상세 도구 근거의 재열람에는 하네스 전사를
보관해야 한다. 참조가 있다는 사실을 원문 바이트까지 vault에 보존했다는 뜻으로 읽지 않는다.

```powershell
$env:PYTHONPATH='_governance/_engine'
.venv/Scripts/python.exe -m osk.cli integration capture --harness codex --conversation <대화ID> --session <고정키> --transcript <전사JSONL>
.venv/Scripts/python.exe -m osk.cli integration prompt --harness codex --conversation <대화ID>
```

검토는 새 원문과 기존 scope 기억을 함께 읽고, 검색으로 기존 노드 갱신을 우선한다.
MCP `create_node`·`update_node`의 `distill`에 `{key,sources,hub}`를 주면 근거와
허브 연결까지 확인한다. `key`는 검토 작업의 고정 키, `sources`는 노드 또는 정확한
raw 라운드 참조(선정 시 해시가 있으면 `{ref,hash}`), `hub`는 기존 착지 허브다.
본문 저장 뒤 연결 실패는 `distillation.status="pending"`이다. 같은 요청을 재시도하면
같은 노드에서 이어간다. 다음 worker는 `update_node(name=<기존 id>,distill={resume:<키>})`로
저장된 본문을 재작성하지 않고 연결을 복구한다. `settle`과 `distill`은 함께 주지 않고
증류 완료 뒤 퇴출을 처분한다.

`integration review`의 stdin은 `{through,outcome,reason,targets}` JSON이다.
`through`는 prompt가 낸 snapshot이며, 이후 대화가 늘어도 그 snapshot까지만 닫는다.
`preserved`의 targets는 `[{key:<완료된 증류 키>}]`, `summary`는
`[{text:<실제 scope 기억의 발췌>}]`다. `no_value`는 배울 것이 없다는 판단,
`deferred`는 보류 사유와 다음 조치다. 요약·배울 것 없음·보류를 노드 성장으로 세지 않는다.
다른 대화가 같은 scope를 저장해도 이 검토 결과를 대신할 수 없다.

### Scope에서 Domain으로 정기 재검토

`growth plan`과 `growth prompt`는 쓰기 없는 미리보기다. `growth run`은 알려진 대화의
미포착 꼬리를 따라잡고, 검토할 원문 snapshot과 비교할 Scope 노드 집합·해시를 고정한 뒤
제한된 외부 에이전트 실행에 그 입력을 준다. 짧게 끝난 대화도 이 전용 실행에서 Scope
증류 기회를 갖는다. 이번 실행에서 생긴 Scope 노드는 다음 실행의 Domain 후보가 된다.
기본 한 번에 대화 3개와 Domain 후보 3개, 후보당 최대 8개 노드다.
기존 Scope와 새 Scope의 조합도 비교하며,
같은 입력 집합의 완료된 검토는 반복하지 않는다. 프로세스 종료코드 0만으로 완료하지 않고
실제 Domain 본문·근거·허브와 입력 해시를 확인해야 한다.

전용 에이전트는 마지막 응답에 prompt가 지정한 `osk_reviews` JSON만 반환한다.
실행기는 성공한 최종 응답에서 이 결정을 읽어 기존 `integration review`·`growth review`
검증을 적용한다. 셸 정책 때문에 에이전트의 검토 CLI 실행이 막혀도 이 경로로 등록한다.
도구 출력 속 JSON이나 다른 manifest·선정하지 않은 대화의 결정은 받아들이지 않으며,
`preserved`라는 선언만으로 저장을 인정하지 않는다. 직접 CLI를 사용할 때도
`growth review --manifest <plan rid>`로 같은 검증을 거친다.

에이전트 명령은 JSON argv 배열 파일로 둔다. 명령은 stdin으로 프롬프트를 읽고 종료해야
하며, 이 인스턴스의 osk MCP에 연결돼 있어야 한다. 셸 문자열은 실행하지 않는다.
우선 격리 mini-vault에서 실제 도구 호출을 확인한 뒤 인스턴스에 등록한다.


Codex의 ChatGPT 구독 로그인으로 실행할 때는 `codex login status`가 ChatGPT 로그인을
보고하는지 먼저 확인한다. 별도 API 키나 다른 모델 공급자를 연결하지 않고, 다음처럼
인증 방식을 제한한다. 실제 사용 중인 Codex 실행 파일과 이 인스턴스의 MCP 설정을 쓴다.

```json
[
  "codex", "exec", "--json", "--sandbox", "read-only",
  "-c", "forced_login_method=\"chatgpt\"",
  "-c", "approval_policy=\"on-request\"",
  "-c", "approvals_reviewer=\"auto_review\"",
  "-"
]
```

`exec`의 승인 정책은 `-c approval_policy=...`로 전달한다. 상위 명령의
`-a on-request`만 붙이면 실제 실행은 `never`로 남아 `read_raw`를 거절할 수 있다.
이 설정은 해당 실행에만 적용되며 전역 승인 설정을 바꾸지 않는다. 자동 검토도 개별
호출을 거절할 수 있으므로, 실제 원문 읽기와 노드 저장 결과까지 확인한다.
`forced_login_method`는 ChatGPT 인증으로 제한하며, 사용량은 구독의 Codex 한도에 포함된다.
아래 명령은 한 번만 실행한다. 정기 실행이 필요할 때만 뒤의 스케줄러를 별도로 등록한다.

```powershell
.venv/Scripts/python.exe _governance/_engine/scripts/growth_run.py --command-file .osk/growth-command.json --limit 3 --timeout 600
```

후보가 없으면 모델을 띄우지 않는다. 실패·보류는 완료로 접지 않으며 후속 실행에서
다시 검토할 수 있다. 입력 집합을 읽었다는 사실과 모든 입력을 같은 결론의 근거로
인용하는 것은 다르다. 의미 판정·적용 범위·반례는 에이전트가 설명해야 한다.
작업 증거는 `.osk/growth/runs/`와 Workbench `_ledger/growth.jsonl`에 남는다.
전용 실행은 `OSK_GROWTH_WORKER=1`을 자식 프로세스에 전달한다. OSK의 세 훅은 이때
유지보수 대화를 새 통합 대기로 포착하지 않는다. 실행 기록과 저장 노드·근거는 그대로 남는다.
새 Domain 군집에 필요한 사용자 확인은 자동 실행이 대신하지 않는다. 기존 착지가 없으면
제안할 군집·노드 제목과 필요한 확인을 `deferred`로 남긴다.

Windows 작업 스케줄러 등록은 아래 스크립트를 **별도로 실행할 때** 활성화된다.
기존 동일 이름 작업을 덮어쓰지 않으며 로그인된 사용자 권한으로 하루 한 번 실행한다.
실제 릴리스·인스턴스 갱신·새 MCP 재시작과 이 등록은 구현/시험과 구별한다.

```powershell
./_governance/_engine/scripts/register_growth_task.ps1 -Python "$PWD/.venv/Scripts/python.exe" -CommandFile "$PWD/.osk/growth-command.json" -At '09:00'
```

기본은 공유 지식의 연속성이다. 다른 작업의 대화·미완료 요청을 현재 대화에 합치지는
않지만, 그 작업에서 이미 증류된 지식은 다음 일반 작업에서도 읽을 수 있다. 이는 독립
검토자가 앞선 결론을 보게 될 수 있다는 비용을 갖는다. 독립 검토가 명시된 실행은 별도
고정 입력 vault와 새 하네스 문맥으로 수행한다. 새 대화 ID만으로 정보 격리가 보장되지는 않는다.

## 회귀 수트

```bash
.venv/bin/python _governance/_engine/tests/test_regression.py
```

수트는 자기 프로세스 안에서 임시 mini-vault를 `OSK_VAULT_ROOT`로 가리킨다 —
실 vault를 읽지도 쓰지도 않는다. 검토 세션의 적대 시나리오가 여기에 영속 고정돼
있으므로, 엔진을 고쳤으면 이걸 통과시킨 뒤에 커밋한다.

## 동기화 데몬

git 동기화만 한다. 검색·색인 서빙은 하지 않는다 — 그것은 엔진과 MCP 서버의 일이다.
**명시적 opt-in**이라 `SYNC_ENABLED`가 없으면 즉시 종료한다.

```bash
SYNC_ENABLED=1 .venv/bin/python _governance/_engine/sync_daemon.py --interval 900
```

잠금은 실제 git 디렉터리 안의 `osk-sync.lock`에 둔다(추적 트리로 폴백하지 않는다 —
데몬 자신의 `git add -A`가 잠금 파일을 커밋해 버리기 때문이다).

**동기화 대상은 `main` 고정이다**(`vault_sync.SYNC_BRANCH`). 데몬은 HEAD를 따라가지
않는다 — 어떤 세션이 다른 브랜치를 checkout해 둔 사이에 그 브랜치가 vault의 정본인
것처럼 커밋·push되면 정본이 조용히 갈라지기 때문이다. `pull`·`push`도 `origin main`을
명시하므로 upstream이 잘못 걸려 있어도 엉뚱한 곳으로 새지 않는다.

HEAD가 `main`이 아니면 매 주기 시작에 되돌린다. 되돌릴 수 없는 경우에는 **아무것도
하지 않고** 사유를 낸다.

| 상태 | 동작 |
|---|---|
| `main` | 그대로 동기화 |
| 다른 브랜치·detached, 추적 파일 수정 없음 | `main`으로 전환 후 동기화 (미추적 새 노드는 함께 넘어간다) |
| 다른 브랜치·detached, 추적 파일 수정 있음 | **거부** — 진행 중 작업일 수 있어 옮기지도 감추지도 않는다 |
| 로컬에 `main` 없음 | 거부 |

launchd/systemd 예시는 `_governance/_engine/scripts/`에 있다.

## 줄바꿈(EOL) 이행 — 기기마다 한 번, 승인은 한 번

`.gitattributes`가 저장소 전체를 **LF로 못박는다**. 이 체계의 여러 판정이 파일의
raw 바이트에 걸려 있어서다 — 갱신의 프레임워크 대조, 보호영역의 승인본
tree(Mechanism §3 4항). 기기마다 다른 줄바꿈으로 체크아웃되면 그 판정이 기기에
의존한다.

**단, 바이트가 걸려 있다고 다 LF로 못박는 것은 아니다.** `_raw/`는 append-only
원자료이고 쓰기가 기존 바이트를 접두부로 보존하는 것을 계약으로 삼는다(§9 4항).
`_scope_memory/`의 CAS 해시도 개행에 민감하다(`canon`은 NFC와 앞뒤 공백만
다룬다). 이 둘은 **쓰인 바이트가 그대로 남아야** 하므로 `.gitattributes`가
`-text`로 변환 밖에 둔다 — 아래 이행 절차가 그 자리를 건드리지 않는 근거다.

**증상.** `core.autocrlf=true`(Git for Windows 설치 기본값)인 기기에서는 노드가
CRLF로 체크아웃되어 그 기기의 **모든 보호영역이 "전 파일 수정" pending**이 된다.
거기서 승인하면 반대편 기기가 pending이 되는 핑퐁이다. `git diff`는 빈 출력이라
원인이 보이지 않는다.

**pull만으로는 이행되지 않는다.** `.gitattributes`가 바뀌어도 git은 blob이 그대로인
파일을 다시 체크아웃하지 않는다. 그리고 이 문제를 겪는 기기에서는 blob이 이미
LF인 경우가 대부분이다(`core.autocrlf=true`는 커밋할 때 LF로 정규화하고 체크아웃할
때 CRLF로 되돌린다) — 그래서 이행 기기의 `git add --renormalize .`가 **아무 파일도
바꾸지 않는다.** 실측: `autocrlf=true` 클론이 새 `.gitattributes`를 pull한 뒤에도
작업 트리는 CRLF 그대로였고 `git status`는 clean이었다. 재전개(아래 3단계)를 그
기기에서 직접 돌려야 LF가 된다. 그래서 **승인은 한 기기에서 한 번**이지만
**재전개는 기존 클론마다 한 번**이다. 새로 clone하는 기기는 처음부터 LF로
받으므로 할 일이 없다.

### A. 이행 기기에서 — 한 번

```bash
# 0) 전제 — 진행 중 작업이 없어야 한다
git status --porcelain          # 비어 있어야 한다
osk validate                    # 지금의 protected_regions를 적어 둔다

# 1) 데몬과 MCP 서버를 멈춘다
#    (mutation 잠금 경합과 중간 상태 커밋을 막는다)

# 2) 색인을 새 규칙으로 다시 만든다 — **byte-exact 구획은 빼고**
#    `-text`를 받은 구획은 clean 필터가 사라져, 여기에 포함하면 **작업 트리의
#    바이트가 그대로 새 blob이 된다.** `autocrlf=true` 기기에서는 blob은 LF인데
#    작업 트리가 CRLF인 `_raw` 기록이 clean 상태로 있을 수 있어(체크아웃이
#    바꿔 놓은 것이다), 그 CRLF가 stage되어 **과거 append-only 기록의 blob을
#    소급 수정한다** — 그리고 다른 기기가 그 CRLF를 정확히 받는다(실측:
#    blob LF → CRLF). 이 구획은 blob을 그대로 두고 3단계에서 `-text`로
#    정확히 되펼치는 것이 맞다.
git add --renormalize -- . ':(exclude)**/_raw/**' ':(exclude)**/_scope_memory/**' ':(exclude)**/_ledger/approved/objects/**'
git status --short              # 줄바꿈만 바뀐 파일 목록

# 2b) 승인 객체는 **파일 이름을 심판으로** 이행한다
#     승인 객체에는 어느 쪽을 무조건 택해도 틀리는 두 경우가 다 있다:
#       · 정상 LF 객체 — blob이 옳은데 구판 checkout이 작업 트리만 CRLF로
#         펼쳐 놓았다. stage하면 옳던 blob이 깨져 승인본이 해석 불능이 된다.
#       · 원본이 CRLF인 객체 — 구판 정규화로 blob이 이미 깨졌고 작업 트리가
#         옳다. 빼 두면 재전개가 깨진 blob으로 작업 트리를 덮어 양쪽 다 잃는다.
#     내용 주소에는 심판이 있다 — **파일 이름이 곧 내용의 sha256이다.**
#     맞는 쪽만 남기고, 둘 다 아니면 아무것도 바꾸지 않고 멈춘다.
osk store-reconcile              # 먼저 판독만 해서 무엇을 할지 본다
osk store-reconcile --apply      # 색인·작업 트리를 이름에 맞춘다

git commit -m "chore: 줄바꿈을 LF로 정규화"
#    변경이 없으면 이 커밋은 건너뛴다 — blob이 이미 LF라는 뜻이고 정상이다.
#    2b가 "판정 불능"으로 멈추면 그 객체는 이 이행 **전에** 이미 깨져 있었다는
#    뜻이다 — 그 객체를 참조하는 승인본은 해제 후 재지정이 회복 경로다.

# 3) 작업 트리를 실제로 LF로 펼친다
#    `--renormalize`는 **색인만** 고친다. 엔진은 작업 트리 바이트를 읽으므로
#    이 단계가 없으면 그 기기는 계속 CRLF를 본다.
git rm --cached -r . -q
git reset --hard

# 4) 어긋난 영역을 한 번 승인한다
osk status                      # pending인 영역이 보인다
osk approve "<영역>"            # 변경집합이 "줄바꿈만 다름"으로 표시한다

# 5) **승인 결과를 커밋한다** — 이게 없으면 다른 기기에 도달하지 않는다
#    `osk approve`는 `= Scope/Workbench/_ledger/approvals.jsonl`에 행을 더하고
#    `_ledger/approved/objects/`에 새 manifest·blob을 쓰지만 **git commit은 하지
#    않는다.** 1단계에서 데몬을 멈췄으므로 대신 커밋해 줄 것도 없다. 그대로
#    push하면 2단계의 정규화 커밋만 올라가고, 다른 기기는 정규화된 파일만 받고
#    대응하는 승인본을 못 받아 pending에 머문다.
git add -A
git commit -m "chore: 줄바꿈 이행 — 새 승인본"

# 6) 확인하고 올린다
osk validate                    # 전 영역 clean
git push
```

3단계의 순서가 중요하다. `git checkout --force -- .`로는 **바뀌지 않는다** —
git이 색인과 작업 트리를 같다고 보기 때문이다(실측). `git rm --cached`는 색인만
비우므로 디스크가 빈 순간이 없고, 이어지는 `git reset --hard`가 전 파일을
필터를 거쳐 다시 펼친다.

4단계에서 `osk approve`는 **줄바꿈만 다른 파일을 그렇게 표시한다** — 내용이
그대로임을 보고 승인하는 것이지, 무엇이 바뀌었는지 모르는 채 승인하는 것이
아니다.

### B. 나머지 기존 클론마다 — 한 번씩

승인은 하지 않는다. A가 세운 승인본을 받고, 작업 트리를 그 기준으로 펼치기만
한다.

```bash
# 0) 데몬과 MCP 서버를 멈춘다

# 1) 미커밋 변경을 먼저 정리한다 — 다음 단계가 그것을 버린다
git status --porcelain          # 비어 있어야 한다
#    남은 것이 있으면 버릴 것이 아닌 한 먼저 커밋한다(내용은 보존된다 —
#    새 규칙이 색인에 넣을 때 LF로 정규화하고, 재전개가 그대로 되펼친다).

# 2) A의 정규화·승인본을 받는다
git pull --ff-only

# 3) **이 기기에서도** 작업 트리를 다시 펼친다 — pull은 이걸 하지 않는다
git rm --cached -r . -q
git reset --hard

# 4) 확인 — 승인할 것이 없어야 한다
osk validate                    # 전 영역 clean
```

4단계에서 pending이 남으면 그 영역은 줄바꿈이 아닌 실제 차이가 있다는 뜻이다.
평소대로 검토해 승인하거나 반려한다 — 이행의 일부가 아니다.

**승인본 blob과 원자료는 이 이행에서 변환되지 않는다.** `_ledger/approved/objects/`는
파일 이름이 곧 내용의 sha256이라, `_raw/`·`_scope_memory/`는 쓰인 바이트가 그대로
남아야 하므로 `.gitattributes`가 셋 다 `-text`로 못박는다. 다만 `-text`는 **변환을
멈출 뿐 이미 어긋난 것을 고르지는 못한다** — 승인 객체만 이름이라는 심판을 가지고
있어 2b단계가 그것으로 가른다. 그
줄이 없으면 CRLF를 담은 blob이 이행 중에 바뀌어 그 승인본이 통째로 해석
불능이 된다(실측). 이행 뒤 `osk validate`가 "승인본 미해석"을 보고하면 그
영역은 이 이행 **전에** 이미 그렇게 됐다는 뜻이다 — 그때는 해제 후 재지정이
회복 경로다.

## 정본 릴리스와 갱신

프레임워크(통치 문서·엔진·운용 문서)의 **정본은 정본 저장소**
<https://github.com/lpaiu-cs/osk-system> 다(Mechanism §1-2). 엔진·통치 문서의
저작은 정본에서 하고, 모든 인스턴스는 — 기초자의 것을 포함해 — 릴리스를
**갱신**으로 받아들인다. 데이터 동기화 데몬(위)과는 다른 축이다 — 데몬은
인스턴스 자신의 원격만 다루고 정본에 닿지 않는다.

**정본에서 — 릴리스 선언** (사용자의 비준 행위, 대화형 단말 전속):

```bash
PYTHONPATH=_governance/_engine python3 -m osk.release --version vX.Y.Z --apply
```

깨끗한 작업 트리·검증기 PASS·비밀값 스캔을 전제로, 릴리스 전 파일의 내용
해시 목록인 **비준증빙**(`release.json`)을 만들어 커밋·태그한다. 전제 검사와
증빙은 모두 **그 커밋의 스냅샷**에서 수행되고, 커밋은 작업 트리를 거치지 않고
object로 만들어 **원자적 교체**로 설치된다 — 그 사이 다른 커밋이 들어오면
실패하고 아무것도 남지 않는다. 버전은 불변이다: 같은 태그의 재선언은 거부되고,
인스턴스도 같은 버전에 다른 증빙이 오면 거부한다(태그 force-move 방어).

선언은 **작업 트리를 건드리지 않는다**(외부 수정을 덮지 않기 위해서다). 그래서
선언 직후 작업 트리에는 새 `release.json`이 없다 — 보고가 안내하는 대로 맞춘다:

```bash
git checkout vX.Y.Z -- release.json
```

태그 push는 git으로 직접 한다.

**인스턴스에서 — 갱신**:

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update            # 보고
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update --apply    # 적용
```

`osk.cli`를 거쳐도 같다 — `... -m osk.cli update --apply`. 위임 명령은 인자를
해석하지 않고 그대로 넘긴다(`--help`도 위임 대상의 사용법이 나온다).

- 출처는 둘이다: `git`(기본 — 정본을 태그로 얕게 받는다. `--to vX.Y.Z`로
  버전 고정) · `bundle`(`--from <경로>` — 디렉터리·tar 오프라인 반입).
  로컬 설정은 `.osk/config.json`의 `{"upstream": {"source", "url", "pin"}}`.
- 릴리스는 **비준증빙과 전수 대조**한 뒤에만 적용된다 — 증빙 파일의 누락·해시
  불일치·증빙 부재는 중단이고, 증빙 밖의 미추적 파일은 적용하지 않는다(막지도
  않는다). 적용은 오직 증빙이 모는 파일만 하고 하나하나를 해시로 검증한다.
- 적용 범위는 릴리스 안의 발행 매니페스트가 정하고(별도 갱신 매니페스트
  없음), **인스턴스 소유 바닥**(`= ` Space·`_ledger/`·`_raw/`·`_sources/`·
  `.osk/`)에는 무엇이 와도 쓰지 않는다.
- 로컬 수정이 있는 문서는 덮지 않고 `<이름>.upstream-<버전>` 사본을 옆에
  둔다(병합은 수동). **엔진 파일의 로컬 수정은 갱신 전체를 중단한다** —
  엔진을 고치는 자리는 정본이다. 기존 인스턴스의 최초 편입은 `--adopt`.
- `--adopt`는 방향이 반대다 — "현재 릴리스를 기준선 삼는다"는 뜻이므로 로컬
  수정을 **덮는다.** 대신 덮기 전 내용을 `<이름>.local-<버전>`으로 남기고,
  보고의 `adopted`에 그 경로들을 따로 싣는다. 성공하면 pre-image는 지워지므로
  (`_txn_clear`) 그 사이드카가 유일한 회수 경로다 — 편입 뒤 `adopted`를 보고
  필요한 것을 되살린 다음 사이드카를 지운다. 같은 이름의 사이드카가 이미 있고
  내용이 다르면 **편입을 시작하지 않는다** — 그 사이드카를 쓰지 못한 채 원본을
  덮으면 지금 파일의 수정이 어디에도 남지 않기 때문이다. 그 파일을 확인해
  필요한 것을 챙긴 뒤 옮기거나 지우고 다시 실행한다.
- 갱신이 통치 문서를 덮으면 그 차이가 통치 구획 보호영역의 변경집합으로
  남는다 — diff를 확인하고 `osk approve _governance`로 승인하는 것이
  **수용의 기록**이다(효력 요건은 아니다). 미처리 변경집합은 `osk status`의
  `protected_regions`에 `pending`으로 드러난다.
- 갱신 이력은 `_ledger/update.jsonl`(운영 저널)에 남고, 엔진이 갱신됐으면
  실행 중인 MCP 서버·데몬을 재시작한다.
- **갱신은 인스턴스당 한 번이다.** 한 인스턴스를 여러 기기에서 쓰더라도
  `osk.update`는 정본→인스턴스 경계를 넘을 때만 돌린다 — 프레임워크 파일도,
  현재 판본을 정하는 갱신 저널도 인스턴스 자신의 저장소가 추적하므로, 나머지
  기기는 **평소의 동기화(pull)만으로 같은 판본이 된다**. 판본 판정이 로컬
  상태 파일이 아니라 union 병합되는 저널의 인과 극대이기 때문이다. 그 기기들
  에서 할 일은 둘뿐이다 — 돌고 있는 서버·데몬 재시작(구 코드가 메모리에 남아
  있다), 그리고 `requirements.txt`가 바뀌었으면 pip 재실행.
- 적용은 **크래시-안전 트랜잭션**이다. 갱신이 도중에 죽으면 다음 `--apply`가
  자동 복구한다. 엔진이 반쯤 교체돼 `osk.update` 자체가 안 돌면 엔진과 독립된
  복구 부트스트랩을 쓴다(표준 라이브러리만 사용):

```bash
python3 _governance/_engine/scripts/recover.py --apply
```

  기본은 보고다. 커밋된 트랜잭션은 파일을 두고 표식만 정리하고(roll-forward),
  미커밋이면 pre-image로 되돌린다(rollback). 백업이 없거나 손상되면 아무것도
  지우지 않고 중단한다. 복구가 끝나기 전에는 동기화 데몬도 tick을 거부한다.

## 적대적 하네스

회귀 수트가 **알려진** 결함을 고정한다면, 이 하네스는 아직 모르는 결함을
찾는다 — 갱신 프로세스를 실제로 SIGKILL로 죽이고, 악의 릴리스와 동시 데몬을
조합해 무작위로 돌린 뒤 불변식을 검사한다.

```bash
.venv/bin/python _governance/_engine/tests/test_adversarial.py --trials 12 --seed 7
```

보고 끝의 **커버리지** 줄이 중요하다 — `pending_txn`·`half_applied`가 0이면
위험 구간을 한 번도 때리지 못한 것이므로 "통과"에 의미가 없다(타이밍·규모를
조정해야 한다). 실측에서 `half_applied`를 만든 뒤 복구·재적용이 수렴함을
확인했다.

신규 설치는 버전 0에서의 첫 갱신이다 — 빈 디렉터리에서 `--apply --adopt`로
시작하거나, 정본을 clone한 뒤 자기 원격으로 갈아탄다.

## 배경 기록

구 데몬(검색 서빙 + 동기화 혼성)과 DuckDB 색인은 v2에서 폐기됐다. 그 판단과 경위는
결정 노드에 있다 — `= Person/Decisions/2026-07-02-lavalink-swap-lock-and-daemon-demotion.md`.
