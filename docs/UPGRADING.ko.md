# osk-system 판 올리기

[English](UPGRADING.md) · **한국어**

메이저 판이 바뀌는 릴리스를 적용하기 전에 읽는다. 평소의 갱신은
[시작 안내서의 최신 상태 유지](GETTING-STARTED.ko.md)와 [SETUP](SETUP.md)에 있다.

## v3.x에서 v4로

v4는 v3가 쓴 레이아웃과 대장을 모두 계속 읽는다([FORMAT §8](FORMAT.md#8-compatibility-promise)).
다만 v3가 통과시키던 배치와 값 몇 가지를 더는 받지 않고, v3가 적지 않던 것을 몇
가지 기록한다. 노트와 기록을 옮기거나 고쳐 쓰지는 않는다.

명령은 [시작 안내서 2단계](GETTING-STARTED.ko.md)처럼 `PYTHONPATH`를 설정하고 vault
루트에서 실행한다. 예시는 macOS/Linux 형식(`.venv/bin/python`)이다. Windows에서는
`.venv\Scripts\python.exe`를 쓴다. `00_Scope`로 시작하는 경로는 자기 Space 루트
(`00_Scope`, `Scope`, `= Scope`)로 바꿔 읽는다. 옛 루트를 손으로 개명하지 않는다
([Space 레이아웃 이행](space-layout-migration.md)).

### 갱신

1. vault를 비공개 원격에 push하거나 따로 백업한다.
2. 한 기기에서 평소대로 갱신한다: `.venv/bin/python -m osk.update --to v4.0.0 --apply`로
   계획을 읽고, 같은 명령을 한 번 더 실행해 적용한다. v3.20.x에서 올라올 때는
   [SETUP](SETUP.md)의 첫 전환 안내를 따른다.
3. osk를 쓰는 Claude Code·Codex 세션을 모두 다시 시작한다. `requirements.txt`가
   갱신됐으면 의존성을 다시 설치한다.
4. **`_governance`의 보호를 기록한다** — `.venv/bin/python -m osk.cli status`의
   `protected_regions`에 없을 때만이다. v4를 적용한 v3 업데이터는 이 지정을 하지
   못한다. 같은 태그의 갱신을 한 번 더 계획하고 확인한다:
   `.venv/bin/python -m osk.update --to v4.0.0 --apply`를 두 번. 파일은 바꾸지 않고
   `"governance_protected": "established"`를 보고한다. 한 기기에서만 한다. 계획이
   `"protect": "withheld"`라고 하면 `unattested`에 실린 파일이 릴리스와 다르다.
   흔히 이전 `--adopt`가 남긴 `<파일>.local-<버전>` 사본이다 — 필요한 내용은 다른
   곳에 챙기고 사본을 지운 뒤 같은 태그의 갱신을 다시 한다. 통치 문서를 일부러
   고쳤다면 검토한 뒤 `.venv/bin/python -m osk.cli protect _governance`로 직접 지정한다.
5. `.venv/bin/python -m osk.cli validate`를 실행해 보고되는 것을 고친다(아래).
6. 커밋한다. 다른 기기에서는 pull하고, 세션을 다시 시작하고, 의존성이 바뀌었으면
   다시 설치한다. vault에 쓰는 기기는 모두 갱신한다 — v3 기기는 v4가 거부하는
   것을 여전히 만들 수 있다.

### v4가 처음 돌 때 기록하는 것

모두 덧붙이기만 하고 고쳐 쓰지 않는다. 커밋하거나 동기화 데몬에 맡긴다.

- `00_Scope/Workbench/_ledger/routing.jsonl`: Git 저장소에서 v4로 세션을 처음 시작할
  때 세션 키마다 소유 기록 한 행(사유 `저장소 동일성 기록`). 다시 적지 않는다.
- `approvals.jsonl`: `_governance`의 `protect` 한 행(4단계)과
  `_ledger/approved/objects/`의 내용 주소 객체.
- `rechecks.jsonl`(새 대장): `derived-from` 쌍마다 사유 `기준선`의 `bound` 한 행.
  첫 세션 시작·노드 쓰기·성장 실행 중 처음 온 것이 한 번 적는다. 그 뒤로는 노드가
  인용한 것이 바뀌면 그 노드가 재검토 후보가 된다 — 인용한 노드의 본문, 인용한
  파일의 내용, 인용한 제목의 범위다. 노드의 요약이나 링크만 바뀐 것은 세지 않는다.
  [SETUP](SETUP.md)의 근거 재검토 절을 본다.
- 각 프로젝트 저장소의 Git 디렉터리에 `osk-repo-identity`: vault 밖의 캐시이며
  Git이 추적하지 않는다.

### v4가 더는 받지 않는 것

`validate`가 하나하나 이름을 댄다. 조치하기 전까지 파일은 그대로다.

| v3는 통과시켰다 | v4 | 할 일 |
|---|---|---|
| 이름이 `_`나 `.`로 시작하는 폴더 안의 노드(`_inbox`, `W1/_drafts`), 이름이 `.`로 시작하는 노드 파일 | 노드 자리가 아니다: 색인·열람·검색·쓰기에서 빠진다. `validate`가 `비노드 구획에 노드형 파일: <경로>`로 실패한다 | [옮긴다](#밑줄점-폴더에서-노드-옮기기) |
| Space 루트 바로 아래의 노드 파일(`00_Scope/note.md`) | 노드 자리가 아니다. `validate`가 보고한다 | 군집 안으로 옮긴다 |
| `user`·`agent`가 아닌 `author`, 소문자 모델명이 아닌 `drafter`, 달력에 없는 `created`·`updated` 시각 | 계약 위반: `validate`가 실패하고 그 노드를 갱신할 수 없다 | frontmatter의 값을 고친다 |
| `id`가 같은 두 파일, 파일 시스템 링크로 두 자리에서 닿는 한 노드 파일 | 이름으로도 id로도 읽기·쓰기·이동을 거부하고, 거부문이 해소 방법을 알린다 | 한 벌만 남기고 다른 사본이나 링크를 지운다 |
| 참조 규칙을 깨는 이동(다른 scope를 Link하는 Scope 노드, `_raw`를 인용하는 Domain 노드) | `move_nodes`가 쓰기 전에 거부한다 | 참조를 고친 뒤 옮긴다 |

### 세션 키

- 폴더 이름은 같고 뿌리 커밋이 다른 저장소들은 더는 세션 키를 나누지 않는다. v4로
  먼저 세션을 시작한 저장소가 이름을 갖고, 나머지는 `<이름>-<뿌리 커밋 앞 8자>`를
  받아 결속 없이 시작한다.
- Git 서브모듈과 bare 저장소의 워크트리는 `modules` 대신 자기 이름을 쓰며, 역시
  결속 없이 시작한다.
- 그런 저장소가 전에 쓰던 scope에 계속 쓰려면 첫 쓰기에 그 scope를 `space`로 준다.
  여러 키가 한 scope를 나눠 쓸 수 있다. Git 밖의 폴더는 여전히 이름으로 키를 나눈다.

### 밑줄·점 폴더에서 노드 옮기기

v4는 대신 옮기지 않는다 — 개명은 대장·승인본·`_raw` 좌표가 기록한 경로를 바꾸기
때문이다. `validate`가 보고한 폴더마다:

1. 그 폴더가 Scope이면 미처분 퇴출부터 처분한다. `.venv/bin/python -m osk.cli tidy list`로
   보고 `tidy settle`로 기록한다.
2. 옮기기 전에 보호영역을 본다. `.venv/bin/python -m osk.cli status`의
   `protected_regions`가 영역과 그 상태를 보인다.
   - 폴더 자신이나 그 아래 폴더가 보호영역이면, 영역마다 미처리 변경집합을 먼저
     `approve` 또는 `revert`로 처분한다. 그다음
     `.venv/bin/python -m osk.cli unprotect <옛 경로>`로 해제한다. 보호 지정은
     경로에 묶여 있어 이동을 따라가지 않는다. 해제하지 않고 옮기면 세 가지가
     막힌다. 옛 경로는 디렉터리가 없어 승인할 수 없고, 미처리 변경이 남아 해제할
     수도 없다. 새 경로는 보호 밖에 남는다.
   - 폴더를 담은 상위 폴더만 보호영역이면 여기서 할 일은 없다.
3. 폴더와 허브를 접두 없는 이름으로 옮긴다. 허브는 폴더와 이름이 같은 노드다:

   ```bash
   git mv "00_Scope/W1/_drafts" "00_Scope/W1/drafts"
   git mv "00_Scope/W1/drafts/_drafts.md" "00_Scope/W1/drafts/drafts.md"
   ```

   2에서 해제한 영역은 곧바로 새 경로로 다시 지정한다:
   `.venv/bin/python -m osk.cli protect <새 경로>`. 지정할 때의 작업본이 초기
   승인본이 되므로, 다른 것을 고치기 전에 지정한다. 그 뒤에 고친 것은 그 영역의
   변경집합이 되어 5에서 검토한다.

   허브의 제목은 파일 이름을 따라 바뀌므로, 부모 허브의 `[[_drafts]]`처럼 허브를
   가리키던 링크를 고친다.
4. 최상위 Scope를 옮겼다면(예: `00_Scope/_inbox` → `00_Scope/inbox`):
   - scope 기억 `00_Scope/Workbench/_scope_memory/_inbox.md`를 `inbox.md`로 옮긴다.
   - 그 안의 `_raw/`도 함께 옮겨졌으므로, 옛 경로를 적은 `derived-from` 좌표
     (`00_Scope/_inbox/_raw/…#N`)는 해석되지 않는다. `validate`의 `dangling_refs`가
     그것들을 알리니, 각각 옛 경로를 새 경로로 바꾼다.
   - 그 scope에 결속됐던 세션 키를 다시 결속한다. 키는 세션 시작에 보이는
     `session="…"`다. 명령이 없으므로 새 결속을 덧붙이는 엔진 함수를 부른다
     (Mechanism §6-2 6항: 결속의 변경도 새 기록의 추가다):

     ```bash
     .venv/bin/python -c "from osk import write; print(write.bind_session('<키>', 'inbox', 'v4 upgrade'))"
     ```

5. 보호영역의 변경집합을 검토한다. 폴더를 담은 보호영역에서는 이동이, 3에서 다시
   지정한 영역에서는 그 뒤에 고친 링크·좌표가 변경집합이다. 검토한 뒤
   `.venv/bin/python -m osk.cli approve <영역>`으로 승인한다.
6. 커밋하고, 세션을 다시 시작하고, `validate`를 다시 실행한다.

보호영역을 해제하지 않고 이미 옮겼다면, 폴더와 허브를 원래 이름으로 되돌린 뒤 2부터
다시 한다.

이름이 `.`로 시작하는 노드 파일은 점을 뺀 이름으로 바꾼다.

### 그 밖에 눈에 띌 변화

- v3의 `update_node`는 한 노드의 모든 제목을 같은 근거로 보았다. `[[X#A]]` 옆에
  `[[X#B]]`를 더해도 바뀌지 않았고, 하나를 빼면 둘 다 빠졌다. v4는 제목마다 따로
  둔다. 한 노드의 여러 절에 기대는 노드가 있으면 그 절들이 모두 인용돼 있는지 본다.
- `derived-from`에 준 노드 id는 그 노드의 제목 링크로 저장된다.
- 새 경고(실패가 아니다): `governance_unprotected`, `duplicate_edges`, `rechecks`.
- 설치된 `mcp` 패키지가 모르는 인자를 거부하지 못하면 MCP 서버가 시작하지 않는다.
  `_governance/_engine/constraints.txt`가 CI에서 검증한 판을 고정한다.
