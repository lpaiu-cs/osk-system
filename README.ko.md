<p align="center">
  <a href="README.md">English</a> · <strong>한국어</strong>
</p>

<p align="center">
  <img src="docs/assets/readme/hero.svg" alt="osk-system — 쌓이는 세션이 지식이 되도록" width="100%">
</p>

<p align="center">
  <strong>쌓이는 세션이 지식이 되도록.</strong><br>
  에이전트와의 대화가 출처와 연결을 갖춘 지식으로 자라고, 다음 작업에서 다시 쓰이는 로컬 Markdown 기억
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-18232d?style=for-the-badge&amp;logo=python&amp;logoColor=efc875" alt="Python"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-18232d?style=for-the-badge&amp;logo=modelcontextprotocol&amp;logoColor=ffffff" alt="Model Context Protocol"></a>
  <a href="https://daringfireball.net/projects/markdown/"><img src="https://img.shields.io/badge/Markdown-18232d?style=for-the-badge&amp;logo=markdown&amp;logoColor=ffffff" alt="Markdown"></a>
  <a href="https://obsidian.md/"><img src="https://img.shields.io/badge/Obsidian-18232d?style=for-the-badge&amp;logo=obsidian&amp;logoColor=b6a0ef" alt="Obsidian"></a>
  <a href="https://git-scm.com/"><img src="https://img.shields.io/badge/Git-18232d?style=for-the-badge&amp;logo=git&amp;logoColor=f08d75" alt="Git"></a>
</p>

<p align="center">
  <a href="https://github.com/lpaiu-cs/osk-system/releases"><img src="https://img.shields.io/github/v/release/lpaiu-cs/osk-system?style=flat-square&amp;color=9bd8c4&amp;labelColor=18232d" alt="최신 릴리스"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-e8c47c?style=flat-square&amp;labelColor=18232d" alt="MIT 라이선스"></a>
  <img src="https://img.shields.io/badge/data-local%20files-9bd8c4?style=flat-square&amp;labelColor=18232d" alt="로컬 파일에 저장">
</p>

<p align="center">
  <a href="#시작하기">시작하기</a> ·
  <a href="docs/GETTING-STARTED.ko.md">시작 안내서</a> ·
  <a href="#어떻게-자라나">어떻게 자라나</a> ·
  <a href="docs/SETUP.md">설치·운용 가이드</a> ·
  <a href="#governance">설계와 통치</a>
</p>

---

> **상태: 개발자 공개 베타.** 관리자가 Windows 11에서 매일 쓰고 있다. CI는
> Windows와 Linux에서 테스트 수트를 돌리고, macOS에서는 결과가 병합을 막지 않는
> 작업으로 돌린다 — macOS에서의 일상 사용은 아직 검증하지 않았다. vault는 Git
> 저장소다 — 갱신하기 전에 자신의 비공개 원격에 push해 두거나 따로 백업한다.
> [알려진 한계](#알려진-한계)를 먼저 본다.

**에이전트로 설치하기.** Claude Code·Codex·Kiro·Antigravity에 이 한 줄을 붙여 넣는다:

```text
https://raw.githubusercontent.com/lpaiu-cs/osk-system/main/docs/INSTALL-AGENT.md 를 읽고 이 기기에 osk-system을 설치해 줘.
```

## 왜 만들었나

LLM wiki를 만들어 달라는 한 줄 요청에서 출발했다. 돌아온 것은 일관성 없는 저장 체계,
불필요한 정보 덩어리, 사람이 읽기 불편한 구조였다. 그대로 두면 이런 기억은 근거 없는
그럴듯한 기억 그래프로 흘러간다.

그래서 osk-system은 헌법부터 만들었다. **무엇을 기억으로 남길지, 이미 있는 내용과 어떻게
합치거나 연결할지, 커진 주제를 언제 나눌지** — 이 선택을 규칙으로 정해 두고, 엔진과
에이전트가 그 규칙대로 기억을 키운다. 목표는 있어 보이기만 한 위키가 아니라 사람도 읽을 수
있고 실제로 쓰이는 작업 공간이다. 기록이 끝없이 쌓이는 보관함이 아니라, 경험이 다음 판단에
쓰이는 지식으로 자라는 공간이다.

헌법은 첫 문장에서 이 체계를 이렇게 정의한다.

> osk-system은 사용자에게 속한 기억을 출처·맥락·관계·권위와 함께 지속시키고,
> 이후의 이해·판단·감사·작업에 다시 작동시키는 system이다.

<p align="center">
  <a href="docs/assets/readme/obsidian-graph.png"><img src="docs/assets/readme/obsidian-graph.png" alt="실제 osk-system 보관함의 Obsidian 전체 그래프: 초록색과 보라색 지식 노드들이 군집을 이루며 연결된 모습" width="680"></a>
  <br>
  <sub>지식이 자란 실제 보관함의 Obsidian 그래프. 새 인스턴스의 지식 공간은 비어 있습니다. 클릭하면 원본 크기로 볼 수 있습니다.</sub>
</p>

| 필요한 것 | osk-system의 방식 |
|---|---|
| 다음 대화에서도 이어지는 맥락 | 세션을 시작할 때 그 프로젝트의 공유 기억을 싣고, 필요한 노드는 검색해 읽는다 |
| 흩어지지 않고 자라는 지식 | 같은 주장은 기존 노드에 갱신하고, 새 지식은 기존 기억과 연결되며 자란다 |
| 출처를 따라갈 수 있는 회상 | 노드에서 근거 노드와 대화의 특정 라운드까지 따라간다 |
| 사람이 읽고 고칠 수 있는 기억 | 로컬 Markdown 파일, Obsidian 그래프, 선택적인 Git 동기화 |

## 어떻게 자라나

대화가 지식이 되는 순환은 다섯 단계다. 훅을 이은 호스트(Claude Code·Codex·Kiro·
Antigravity)에서는 세션의 흐름 안에서 돈다 — 포착은 기계가 하고, 검토와 증류는 훅의
안내를 받은 에이전트가 한다.

1. **포착** — 훅이 대화의 모든 라운드를 `_raw/`에 남긴다. 발화와 답변은 원문 그대로,
   도구 호출과 결과는 참조로 남는다. 원문은 노드가 아니라 근거다.
2. **검토·증류** — 9·15턴마다(구독 fork를 쓰면 최종 답변 9회마다 백그라운드에서)
   에이전트가 새 라운드와 공유 기억을 함께 검토해, 오래 쓸 것을 노드로 증류한다. 노드는
   근거를 `derived-from`으로 — 대화의 라운드까지 — 참조한다.
3. **연결·정돈** — 같은 주장이면 새 노드를 만들지 않고 기존 노드를 갱신한다. 새 노드가 먼저
   서고, 주제를 덮는 허브가 나중에 그 노드를 잇는다. 주제가 커지면 허브가 갈래를 나눈다.
   한 프로젝트를 넘어 쓰이는 지식은 Domain으로 다시 증류한다(정기 실행이나 요청으로).
4. **회상** — 다음 세션이 시작되면 그 프로젝트의 공유 기억(scope 기억)이 문맥에 실리고,
   에이전트는 검색으로 노드와 근거를 읽는다.
5. **재검토** — 근거가 바뀌면 그것을 인용한 노드가 재검토 후보가 되고, 그 영향은 인용을
   따라 번진다.

**노드는 미래 재사용성의 함수다.** 다음 검색·판단·작업에 반복해서 쓰일 것을 노드로 남기고,
한 번 쓰고 끝날 작업 상태나 쉽게 다시 계산할 산출물은 남기지 않는다. 그리고 **모든 지식은
주변부에서 시작해 연결을 통해 중심부로 자란다** — 중심과 주변은 층이 아니라 연속된
스펙트럼이다.

## 세 Space

노드는 주로 어디에 쓰이는가에 따라 세 Space 중 하나에 속한다.

| Space | 담는 것 |
|---|---|
| **Scope** | 프로젝트·활동 단위로 형성되는 기억. 그 활동의 대화 원문(`_raw/`)도 여기 보관한다 |
| **Domain** | 특정 프로젝트에 묶이지 않고 여러 맥락에서 재사용되는 지식 |
| **Person** | 사용자상 — 사용자가 남긴 기록, 사용자에 관한 이해, 위임. 에이전트의 추론은 사용자의 확인 없이 사용자의 사실로 확정하지 않는다 |

통치 문서는 어느 Space에도 속하지 않는다 — system을 규정하는 규범은 system이 조직하는
지식이 아니기 때문이다.

## 당신의 기억, 당신의 주권

- **기억은 사용자에게 속한다.** 에이전트는 사용자의 주권 아래 기억을 운용한다. 세션을 넘는
  위임은 대상·범위·조건을 적은 위임 노드를 사용자가 승인해야 성립하고, 범위가 불명확하면
  에이전트는 보류한다.
- **에이전트를 막지 않는다.** 에이전트는 노드를 직접 쓰고 고치며, 모든 노드는 내용을 정한
  주체(author)와 초안을 쓴 주체(drafter)를 함께 적는다. MCP 쓰기는 노드 계약 검증을 거친다.
- **신뢰는 승인 도장이 아니라 출처·관계·이력에서 나온다.** 근거를 따라 읽을 수 있어야 믿을
  수 있다.
- **보호영역은 되돌릴 수 있게 한다.** 사용자가 고른 구획(통치 문서와 위임은 상설)에서도
  수정은 곧바로 반영된다. 다만 사용자가 마지막으로 승인한 상태(**승인본**)를 엔진이 따로
  보존하고, 차이는 **변경집합**으로 남아 사용자가 승인하거나 되돌린다. 지정·해제·승인·반려는
  사용자 전속이다. 이 기록은 노드 밖의 대장에 있고, 여러 기기의 기록은 시간 순서가 아니라
  인과(DAG)로 판정한다.
- **강제할 수 없는 것을 강제한 척하지 않는다.** 보호영역은 선의의 실수를 되돌리는 장치이지,
  vault에 임의로 쓸 수 있는 상대에 대한 보안 경계가 아니다. 권한 검사는 기계로 평가할 수
  없으면 보류를 낸다. [엔진의 알려진 한계](_governance/_engine/README.md#알려진-한계)를 본다.

## 기술 스택

Python 엔진과 MCP 도구를 중심으로, Markdown에 지식을 저장하고 BM25로 검색한다.
Obsidian은 지식을 탐색하는 선택적 화면이고, Git 동기화도 선택 사항이다.
자동 포착·주기 검토 어댑터는 현재 **Codex·Claude Code·Kiro·Antigravity**를 지원한다.
런타임 의존성은 [requirements.txt](_governance/_engine/requirements.txt)를 본다.

## 시작하기

**에이전트에게 맡기기.** Claude Code·Codex·Kiro·Antigravity에 이 한 줄을 붙여 넣는다:

```text
https://raw.githubusercontent.com/lpaiu-cs/osk-system/main/docs/INSTALL-AGENT.md 를 읽고 이 기기에 osk-system을 설치해 줘.
```

에이전트가 vault를 만들 자리와 켤 선택 기능을 묻고, 최신 릴리스를 clone한 뒤
[설치 도구](docs/SETUP.md#설치-도구-setup)를 돌린다. 설치 도구는 계획을 보여 주고,
확인을 받아야 적용한다.

**처음이라면** [시작 안내서](docs/GETTING-STARTED.ko.md)를 따라간다. 빈 폴더에서
에이전트의 첫 기억까지, macOS·Linux·Windows 명령과 단계별 확인 방법을 함께 담았다.

**v3 vault를 갱신한다면** v4를 적용하기 전에 [판 올리기](docs/UPGRADING.ko.md)를 읽는다.
v4가 더는 읽지 않는 배치와 옮기는 법이 있다.

**직접 설치한다면** Python 3.11 이상과 Git을 준비한다. `main`이 아니라 릴리스 태그에서
시작한다(새 태그는 [릴리스 페이지](https://github.com/lpaiu-cs/osk-system/releases)에 있다):

```bash
git clone --branch v4.1.1 https://github.com/lpaiu-cs/osk-system.git my-osk-vault
cd my-osk-vault
git switch -c main
python _governance/_engine/scripts/setup.py --interactive
```

`python`이 없으면 `python3`를, Windows에서는 `py -3.12`를 쓴다. 설치 도구는 `.venv`를 만들어
의존성을 설치하고 릴리스 기준선을 기록한다 — 그래야 이후 갱신이 릴리스 파일과 직접 고친
파일을 구별한다. 이어서 이 기기에서 찾은 호스트(Claude Code·Codex·Kiro·Antigravity)에 MCP
서버와 훅 세 개를 등록한다. 쓰기 전에 계획을 보여 주고 확인을 받는다. 그 뒤
`python _governance/_engine/scripts/setup.py doctor`로 연결을 점검한다. 같은 과정을 한
명령씩 따라가려면 [시작 안내서 2단계](docs/GETTING-STARTED.ko.md#2단계-엔진-설치와-릴리스-기준선-기록)를 본다.

- **다른 MCP 클라이언트:** [.mcp.json.example](.mcp.json.example)을 복사해 `<REPO>`를 vault의
  절대경로로 바꾼다(Windows의 인터프리터는 `.venv/Scripts/python.exe`). 클라이언트별 등록·훅
  설정과 Windows 명령은 [설치·운용 가이드](docs/SETUP.md)를 따른다.
- **Obsidian:** `my-osk-vault` 폴더를 보관함으로 연다.
- **동기화:** 켜기 전에 원격을 **자신의 비공개 저장소**로 바꾼다. 개인 노트·대장·대화 기록을
  공개 정본에 올리지 않는다.

> MCP 서버만으로는 자동 포착과 주기 검토가 켜지지 않는다 — 훅이 켠다. 설치 도구는 둘 다
> 등록한다. 클라이언트를 손으로 이었다면 훅도 함께 등록한다. [지원 범위](#harness-coverage)를 본다.

## 사용하기

<p align="center">
  <a href="docs/assets/readme/obsidian-note-local-graph.png"><img src="docs/assets/readme/obsidian-note-local-graph.png" alt="왼쪽에는 osk-system 설계 노트의 속성·본문·관련 기록, 오른쪽에는 로컬 그래프가 열린 Obsidian 사용 화면" width="100%"></a>
  <br>
  <sub>지식 노트와 로컬 그래프를 나란히 읽는 실제 사용 화면. 클릭하면 원본 크기로 볼 수 있습니다.</sub>
</p>

**회상 → 근거 열람 → 기억에 반영.** 훅을 이었다면 포착과 회상은 저절로 일어난다.
필요할 때는 에이전트에게 이렇게 요청한다:

```text
이 프로젝트의 이전 결정을 찾아줘. 요약만 보고 결론 내리지 말고 근거 노드도 읽어줘.

이번 작업에서 확인한 결과를 프로젝트 기억에 반영하고, 근거를 이어줘.
```

Obsidian에서는 전체 그래프로 군집 간 연결을 둘러보고, 노트와 로컬 그래프를
나란히 열어 주변 맥락을 읽는다. **그래프는 연결을 보여주며, 승인 여부를 판정하는
화면은 아니다.** 보호영역의 현재 상태는 엔진의 `status`로 확인하고,
사용자의 승인·반려 절차에서 변경집합을 검토한다.

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli status
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli search "프로젝트 결정"
```

## Harness coverage

자동 포착·주기 통합의 하네스 어댑터는 현재 **Codex·Claude Code·Kiro·Antigravity**에
있다. 구독 fork는 Codex와 Claude Code뿐이고, Kiro와 Antigravity는 늘 현재 세션의 9·15턴에
통합한다. 다른 MCP 클라이언트에서 도구를 호출할 수 있다는 사실만으로
세션 훅과 자율적인 지식 성장이 연결되었다고 보지 않는다.

| 실행 조건 | 대화 검토 경로 |
|---|---|
| 지원 하네스의 훅과 구독 CLI가 연결되고 로그인·판본 검사를 통과 | 성공한 최종 답변 **Stop 9회마다** 같은 하네스·모델의 백그라운드 fork |
| CLI 미설정·미설치·미로그인·구독 인증 불확실·판본 불일치·보존 불가능한 권한·Codex 비Git·미신뢰 작업 폴더·연속 2회 끝나지 못한 fork 검토(하루 뒤 다시 시도) 등 | **검토 경고**와 함께 현재 세션의 **입력 9·15턴 통합**(UserPromptSubmit, Antigravity는 PreInvocation)으로 전환 |
| 아직 어댑터가 없는 하네스 | 자동 포착·계수·fallback을 보장하지 않음. 하네스별 연결과 검증이 필요 |

입력 계수는 백그라운드 모드에서도 유지한다. 실행 경로가 바뀌어도 검토 대기와
두 계수를 초기화하지 않으며, 전환 시 이미 9턴을 넘긴 대기는 바로 안내한다.
로그인이 복구되거나, fork 검토가 연속 2회 끝나지 못한 뒤 하루가 지나면 Stop 실행으로 돌아간다.
구독 확인 실패를 API 과금으로 대체하지 않는다.

**캐시 재사용은 별도 검증 대상이다.** 실제 Codex 앱의 최종 답변 직후 같은 Sol
모델로 fork하여 98.74% 적중을 확인했다. 같은 원세션도 기본 CLI 구성은 0%였으므로,
앱에서 출발하면 앱용 도구 정의를 자동 보존한다. 판본·모델별 보편적 보장은 아니며,
구현·설치·캐시 적중·실제 노드 성장을 구별한다.
[설치와 fallback 동작](docs/SETUP.md), [실험 결과와 한계](docs/response-growth.md)를 본다.

노드 갱신의 단위는 같은 **주장과 적용 조건**이다. 같은 프로젝트의 다음 단계라는
이유로 실행 일지를 계속 누적하지 않는다. 조직 검토는 읽은 구간의 판단을 기록하며,
일부 구간만 읽은 상태를 군집 전체 완료로 세지 않는다. [구간 검토와 안전한 분화](docs/hub-growth-review.md).

하네스 커버리지 확대는 후속 과제다. 새로운 하네스마다 대화/완료 식별자, 전사
저장 경계, 훅 이벤트, 구독 인증, 일회성 실행과 실패 시 대체 경로를 검증해야 한다.

## 알려진 한계

- **Git 밖의 폴더는 이름으로 공유한다.** 세션 키는 저장소 폴더의 이름이다
  (워크트리는 본 저장소로 접히고, 서브모듈·bare 저장소는 자기 이름). v4부터
  이름이 같아도 뿌리 커밋이 그 키의 소유자와 다른 무관한 Git 저장소는
  `<이름>-<뿌리 앞 8자>` 키를 받아 남의 Scope 기억을 받지도 쓰지도 않는다.
  Git 밖의 폴더, 커밋 없는 저장소, 얕은 사본은 동일성이 없어 여전히 폴더
  이름으로 공유한다.
- **백그라운드 검토는 원세션의 권한을 물려받는다.** 구독 fork 검토는 검토
  대상 세션의 Claude Code 권한 모드, 또는 Codex 승인·샌드박스 정책 그대로
  무인 실행된다. 의도된 설계다. fork는 그 대화를 다시 읽으며, 대화에 섞인
  신뢰할 수 없는 텍스트도 함께 읽는다.
- **노드 본문에는 비밀값 필터가 없다.** 의도된 설계다. 필터는 raw 전사,
  Scope 기억, raw 기록에서 증류한 쓰기에만 걸린다. 노트는 커밋되고 동기화되니
  비밀값을 적지 않는다.
- **보호영역은 보안 경계가 아니다.** 선의의 실수를 막고 되돌리는 장치다
  ([당신의 기억, 당신의 주권](#당신의-기억-당신의-주권) 참고).
- **Windows의 Antigravity는 인용 없는 경로가 필요하다.** 훅을 `cmd`로 돌리는데 cmd가
  인용된 경로를 읽지 못한다. 그래서 vault와 Python 경로에 공백과 cmd 특수문자
  (`& | < > ^ % ! ( ) , ; =`)가 없어야 한다. 설치 도구는 그런 경로면 쓰기 전에 멈춘다 —
  다른 호스트만 이으려면 `--harness`로 고른다.
- **자율 성장은 실험 단계다.** 효과는 아직 측정 중이다
  ([이슈 #20](https://github.com/lpaiu-cs/osk-system/issues/20)).
- **통치 문서는 한국어로만 제공된다.**

## Governance

체계를 규정하는 규범은 어느 지식 Space에도 속하지 않는 상설 통치 구획
`_governance/`에 있다 — system을 규정하는 규범은 system이 조직하는 지식이
아니기 때문이다.

| 문서 | 무엇을 정하는가 |
|---|---|
| [Constitution.md](_governance/Constitution.md) | **헌법** — Space·노드·참조 위상·위임·보호영역·사건·개정의 기본 질서 |
| [Bylaws.md](_governance/Bylaws.md) | **시행령** — 헌법이 맡긴 운영 규칙: 노드 계약, `_raw` 계약, 군집과 pin, 율령, 위임 운영, 보호영역, 사건부 |
| [Mechanism.md](_governance/Mechanism.md) | **물리 최소 사양** — 배치 선언표, id·시각 형식, 대장 규약, 외부 표면(MCP) 계약, 링크 문법, 비밀값 필터 |
| [Workbench-Contract.md](_governance/Workbench-Contract.md) | **Workbench 계약** — 운영 활동 scope의 특별 지위와 정돈 규율 |

처음 읽는다면 **Constitution → Bylaws → Mechanism** 순서를 권한다. Mechanism의
§3(승인 기록부)과 §6-2(외부 표면)가 위에서 말한 보호영역과 MCP 쓰기의 물리 사양이다.

통치 문서의 비준은 이 정본 저장소에 대한 사용자의 확정 행위이며, 정식
릴리스의 비준증빙(`release.json`, 파일별 내용 해시 목록)으로 고정된다.
버전 릴리스 선언은 비대화형으로 수행하며 별도 승인을 요구하지 않는다.
인스턴스 갱신의 첫 적용 요청은 변경집합과 하네스 재시작 필요성을 보여주고
멈춘다. 사용자의 명시적 재승인 뒤 같은 변경집합을 적용하며, 보호 중인 통치
구획의 수용 기록도 함께 남긴다. 통치 구획을 한 번도 지정하지 않은 설치라면
그 구획이 비준증빙과 정확히 같을 때 같은 재승인으로 보호를 지정한다. 승인은
수용의 기록이지 효력의 요건이 아니다. 새 릴리스는 기기마다 하루 한 번 세션 시작이나
`overview`로 알리고, 갱신은 사용자가 요청할 때만 한다
([docs/SETUP.md](docs/SETUP.md)).

## Not included

지식 코퍼스(개념·결정·프로젝트 노드), 승인 기록부를 비롯한 모든 대장,
운영 세션 기록.

빈 `00_Scope/`·`00_Domain/`·`00_Person/` 디렉터리는 내려받은 사람이 자기
인스턴스를 시작할 자리다 — 규범이 정하는 배치(Mechanism §1)와 동형이다.

설치·운용 방법은 [docs/SETUP.md](docs/SETUP.md)를 본다.

## License

[MIT](LICENSE)
