<p align="center">
  <a href="README.md">English</a> · <strong>한국어</strong>
</p>

<p align="center">
  <img src="docs/assets/readme/hero.svg" alt="osk-system — 오래 가는 기억, 사람이 정하는 권위" width="100%">
</p>

<p align="center">
  <strong>에이전트의 기억을, 근거를 따라 읽을 수 있는 지식으로.</strong><br>
  로컬 Markdown 지식 그래프 · 추적 가능한 근거 · 사용자가 결정하는 승인
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
  <a href="#사용하기">사용 예시</a> ·
  <a href="docs/SETUP.md">설치·운용 가이드</a> ·
  <a href="#governance">설계와 통치</a>
</p>

---

> **상태: 개발자 공개 베타.** 관리자가 Windows 11에서 매일 쓰고 있고, CI가
> Windows와 Linux에서 테스트 수트를 돌린다. macOS는 아직 검증하지 않았다.
> vault는 Git 저장소다 — 갱신하기 전에 자신의 비공개 원격에 push해 두거나
> 따로 백업한다. [알려진 한계](#알려진-한계)를 먼저 본다.

## 기억이 쌓여도, 근거는 흐려지지 않게

osk-system은 **사용자가 확인한 지식과 에이전트가 만든 후보를 구별하는**
장기 기억 체계다. 지식은 로컬 파일에, 근거는 추적 가능한 참조에,
승인은 노드와 분리된 기록부에 남긴다.

<p align="center">
  <a href="docs/assets/readme/obsidian-graph.png"><img src="docs/assets/readme/obsidian-graph.png" alt="실제 osk-system 보관함의 Obsidian 전체 그래프: 초록색과 보라색 지식 노드들이 군집을 이루며 연결된 모습" width="680"></a>
  <br>
  <sub>지식이 쌓인 실제 보관함의 Obsidian 그래프. 새 인스턴스의 지식 공간은 비어 있습니다. 클릭하면 원본 크기로 볼 수 있습니다.</sub>
</p>

| 필요한 것 | osk-system의 방식 |
|---|---|
| 다음 대화에서도 이어지는 맥락 | 프로젝트별 Scope 기억과 MCP 검색·열람 |
| 출처를 확인할 수 있는 회상 | 지식 노드에서 근거 노드·대화의 특정 라운드까지 추적 |
| 사람이 검토하는 변경 | 보호영역 승인본과 작업본의 차이를 변경집합으로 보존 |
| 직접 읽고 보관할 수 있는 지식 | Markdown 파일, Obsidian 그래프, 선택적인 Git 동기화 |

## 기술 스택

Python 엔진과 MCP 도구를 중심으로, Markdown에 지식을 저장하고 BM25로 검색한다.
Obsidian은 지식을 탐색하는 선택적 화면이고, Git 동기화도 선택 사항이다.
자동 포착·주기 검토 어댑터는 현재 **Codex와 Claude Code**를 지원한다.
런타임 의존성은 [requirements.txt](_governance/_engine/requirements.txt)를 본다.

## 시작하기

**처음이라면** [시작 안내서](docs/GETTING-STARTED.ko.md)를 따라간다. 빈 폴더에서
에이전트의 첫 기억까지, macOS·Linux·Windows 명령과 단계별 확인 방법을 함께 담았다.

**v3 vault를 갱신한다면** v4를 적용하기 전에 [판 올리기](docs/UPGRADING.ko.md)를 읽는다.
v4가 더는 읽지 않는 배치와 옮기는 법이 있다.

**에이전트에게 맡기기.** Claude Code나 Codex에 이렇게 붙여 넣는다:

```text
이 기기에 osk-system을 설치해 줘. https://github.com/lpaiu-cs/osk-system/blob/main/docs/INSTALL-AGENT.md 를 읽고 순서대로 따라 해. 새 vault 밖의 무언가를 바꾸기 전에는 나에게 먼저 물어 봐.
```

에이전트가 최신 릴리스를 clone하고 [설치 도구](docs/SETUP.md#설치-도구-setup)로 계획을 보여 준
뒤, 확인을 받아야 적용한다.

**직접 설치한다면** Python 3.11 이상과 Git을 준비한다(아래 명령은 3.12 기준이니 설치한 판본으로
바꿔 쓴다). `main`이 아니라 릴리스 태그에서 시작한다(새 태그는
[릴리스 페이지](https://github.com/lpaiu-cs/osk-system/releases)에 있다).
macOS·Linux 기준:

```bash
git clone --branch v4.0.0 https://github.com/lpaiu-cs/osk-system.git my-osk-vault
cd my-osk-vault
git switch -c main
python3.12 -m venv .venv
.venv/bin/python -m pip install -r _governance/_engine/requirements.txt
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli --help
```

이어서 릴리스 기준선을 기록한다([시작 안내서 2단계](docs/GETTING-STARTED.ko.md#2단계-엔진-설치와-릴리스-기준선-기록)).
그래야 이후 갱신이 릴리스 파일과 직접 고친 파일을 구별한다. v4.1.0부터는
`python _governance/_engine/scripts/setup.py --interactive`가 이것과 아래의 호스트 등록을
한 번에 한다 — 계획을 보여 주고, 쓰기 전에 확인을 받는다.

1. [.mcp.json.example](.mcp.json.example)의 `<REPO>`를 인스턴스의 절대경로로
   바꿔 MCP 클라이언트에 등록한다. Codex는 TOML을 읽으므로 `codex mcp add`로
   등록한다. 클라이언트별 등록·훅 설정과 Windows 명령은
   [설치·운용 가이드](docs/SETUP.md)를 따른다.
2. Obsidian을 쓴다면 `my-osk-vault` 폴더를 보관함으로 연다.
3. 동기화를 켜기 전에는 원격을 **자신의 비공개 저장소**로 바꾼다.
   개인 노트·대장·대화 기록을 공개 정본에 올리지 않는다.

> MCP 연결만으로 자동 포착과 주기 검토가 켜지지는 않는다.
> 해당 하네스의 훅 설정이 필요하다. 아래 [지원 범위](#harness-coverage)를 확인한다.

## 사용하기

<p align="center">
  <a href="docs/assets/readme/obsidian-note-local-graph.png"><img src="docs/assets/readme/obsidian-note-local-graph.png" alt="왼쪽에는 osk-system 설계 노트의 속성·본문·관련 기록, 오른쪽에는 로컬 그래프가 열린 Obsidian 사용 화면" width="100%"></a>
  <br>
  <sub>지식 노트와 로컬 그래프를 나란히 읽는 실제 사용 화면. 클릭하면 원본 크기로 볼 수 있습니다.</sub>
</p>

**회상 → 근거 열람 → 검토한 지식 반영.** MCP를 연결한 에이전트에게 다음처럼
요청할 수 있다:

```text
이 프로젝트의 이전 결정을 찾아줘. 요약만 보고 결론 내리지 말고 근거 노드도 읽어줘.

이번 작업에서 확인한 결과를 프로젝트 Scope에 반영하고,
근거를 연결한 뒤 아직 내 검토가 필요한 부분을 알려줘.
```

Obsidian에서는 전체 그래프로 군집 간 연결을 둘러보고, 노트와 로컬 그래프를
나란히 열어 주변 맥락을 읽는다. **그래프는 연결을 보여주며, 승인 여부를 판정하는
화면은 아니다.** 보호영역의 현재 상태는 엔진의 `status`로 확인하고,
사용자의 승인·반려 절차에서 변경집합을 검토한다.

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli status
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli search "프로젝트 결정"
```

## Problem

에이전트가 쓰는 지식 저장소에서는 한 가지가 계속 흐려진다 — **무엇이 사용자가
확인한 것이고 무엇이 에이전트가 만든 후보인가.** 그 구별이 흔들리면 저장소는
"그럴듯한 것이 쌓인 곳"이 되고, 회상은 신뢰할 수 없게 된다.

osk-system은 그 구별을 사람의 기억이나 관행이 아니라 **기계가 틀리지 않게
판정하도록** 만든 체계다. 세 구조가 그것을 떠받친다.

**권위를 노드 밖으로 뺐다.** 승인은 노드 안의 필드가 아니라 별도 대장의
기록이다 — 사용자가 마지막으로 승인한 구획 전체의 상태(**승인본**)를 엔진이
따로 보존한다. 에이전트가 노드를 고쳐도 권위를 고칠 수 없고, 고치면 그 차이가
**변경집합**으로 남아 사용자의 승인이나 반려를 기다린다. 지정·해제·승인·반려의
발의는 사용자 전속이다.

**판정을 시간이 아니라 인과로 한다.** 대장의 각 기록은 직전 head를 참조해
인과 DAG를 이루고, 판정은 시간순 마지막이 아니라 **인과 극대**다. 여러 기기의
병합으로 극대가 하나로 정해지지 않으면 보수적으로 미확정이며, 갈래 전부를 잇는
사용자의 새 기록이 이를 해소한다. 모르면 유효라고 하지 않는다.

**외부 표면이 계약 검증의 강제 지점이다.** 에이전트가 파일을 손으로 쓰면 노드
계약도 참조 위상도 거치지 않는다. 그래서 MCP 도구를 두되, 그은 선은 "읽기
전용"이 아니라 **권위 비노출**이다 — 보호영역 권위와 pin은 영구히 노출하지
않고, 노드 쓰기는 검증을 통과시켜 연다.

관통하는 태도가 하나 있다. **강제할 수 없는 것을 강제한 척하지 않는다.** 권한
검사는 적용 봉투를 기계로 평가할 수 있기 전까지 언제나 보류를 낸다.

보호영역은 선의의 실수를 막고 되돌리는 장치이며, **vault에 임의로 쓸 수 있는 상대에
대한 보안 경계가 아니다.** [엔진의 알려진 한계](_governance/_engine/README.md#알려진-한계)를 본다.

## Harness coverage

자동 포착·주기 통합·구독 fork의 하네스 어댑터는 현재 **Codex와 Claude Code**에
한정되어 있다. 다른 MCP 클라이언트에서 도구를 호출할 수 있다는 사실만으로
세션 훅과 자율적인 지식 성장이 연결되었다고 보지 않는다.

| 실행 조건 | 대화 검토 경로 |
|---|---|
| 지원 하네스의 훅과 구독 CLI가 연결되고 로그인·판본 검사를 통과 | 성공한 최종 답변 **Stop 9회마다** 같은 하네스·모델의 백그라운드 fork |
| CLI 미설정·미설치·미로그인·구독 인증 불확실·판본 불일치·보존 불가능한 권한·Codex 비Git·미신뢰 작업 폴더 등 | **검토 경고**와 함께 현재 세션의 **UserPromptSubmit 9·15턴 통합**으로 전환 |
| 아직 어댑터가 없는 하네스 | 자동 포착·계수·fallback을 보장하지 않음. 하네스별 연결과 검증이 필요 |

입력 계수는 백그라운드 모드에서도 유지한다. 실행 경로가 바뀌어도 검토 대기와
두 계수를 초기화하지 않으며, 전환 시 이미 9턴을 넘긴 대기는 바로 안내한다.
로그인이 복구되면 Stop 실행으로 돌아간다. 구독 확인 실패를 API 과금으로 대체하지 않는다.

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
  ([Problem](#problem) 참고).
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
§3(승인 기록부)과 §6-2(외부 표면)가 위에서 말한 세 구조의 물리 사양이다.

통치 문서의 비준은 이 정본 저장소에 대한 사용자의 확정 행위이며, 정식
릴리스의 비준증빙(`release.json`, 파일별 내용 해시 목록)으로 고정된다.
버전 릴리스 선언은 비대화형으로 수행하며 별도 승인을 요구하지 않는다.
인스턴스 갱신의 첫 적용 요청은 변경집합과 하네스 재시작 필요성을 보여주고
멈춘다. 사용자의 명시적 재승인 뒤 같은 변경집합을 적용하며, 보호 중인 통치
구획의 수용 기록도 함께 남긴다. 통치 구획을 한 번도 지정하지 않은 설치라면
그 구획이 비준증빙과 정확히 같을 때 같은 재승인으로 보호를 지정한다. 승인은
수용의 기록이지 효력의 요건이 아니다
([docs/SETUP.md](docs/SETUP.md)).

## Not included

지식 코퍼스(개념·결정·프로젝트 노드), 승인 기록부를 비롯한 모든 대장,
운영 세션 기록.

빈 `00_Scope/`·`00_Domain/`·`00_Person/` 디렉터리는 내려받은 사람이 자기
인스턴스를 시작할 자리다 — 규범이 정하는 배치(Mechanism §1)와 동형이다.

설치·운용 방법은 [docs/SETUP.md](docs/SETUP.md)를 본다.

## License

[MIT](LICENSE)
