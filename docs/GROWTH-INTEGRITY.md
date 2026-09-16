# 참조와 군집 조직의 지속 검토

지식을 저장한 뒤에도 잘못된 주소와 낡은 허브가 남을 수 있다. 저장 성공만으로 정돈을 완료 처리하지 않고, 기존 통합 훅과 성장 실행이 변경된 Scope의 실제 저장본을 다시 검토한다.

## 저장과 정돈

- 쓰기 응답의 `reference_review`는 미해석 Link·PE, 원료를 향한 항해, 없는 raw 라운드를 구분한다. 저장은 보존 우선으로 완료하고 미결 항목은 파일에서 다시 발견한다.
- 노드 주소는 정확한 제목 또는 지원되는 ID다. vault 원료는 루트 기준 경로와 실재하는 라운드, 외부 레포 문서는 확인한 URL을 사용한다. 가짜 빈 노드로 미해석 근거를 닫지 않는다.
- 통합 훅은 자기 Scope만 안내한다. 별도 성장 실행은 변경 Scope를 제한된 수만큼 순환한다. 정돈은 기존 본문을 읽고 구조 유지·입구 통합·하위 군집 분화 중 선택한다. 개수 임계값이나 모델 실행기를 추가하지 않는다.
- 같은 Scope의 비고정 배치는 시행령 §3의 상시 위임 범위다. pin·보호영역·최상위 군집 신설·경계 이동은 기존 규율을 따른다.

## 완료와 재개

```powershell
$env:OSK_VAULT_ROOT = (Get-Location).Path
$env:PYTHONPATH = '_governance/_engine'
& .venv/Scripts/python.exe -m osk.cli organization plan --scope Arel-Wars-2
& .venv/Scripts/python.exe -m osk.cli organization plan --scope Arel-Wars-2 --preview
```

`plan`은 최초 선택 key를 보존한다. 실제 MCP 쓰기·이동 후 `--preview`로 현재 snapshot을 읽고 아래 형태의 UTF-8 JSON을 같은 실행 문맥의 `organization review`에 stdin으로 보낸다. 훅 안내에는 절대경로를 결속한 실행 명령도 제공한다.

```json
{"key":"<처음 선택한 key>","scope":"Arel-Wars-2","outcome":"complete","reason":"본문을 읽고 현재 갈래를 유지한 이유","after":"<마지막으로 읽은 snapshot>","intentional":[]}
```

완료는 현재 snapshot, 선택된 ID의 잔존, 비어 있지 않은 지식 본문, 지역 허브의 구성원·하위 허브 연결, 남은 이동과 참조를 검사한다. 상위 허브가 분화된 구성원을 계속 직접 나열하면 재검토한다. 일반 노드의 교차 Link는 막지 않는다. 질문 또는 의도된 원료 Link는 `{id,relation:'Link',ref,reason}` 단위로 유지할 수 있으나 PE 미해석은 예외로 닫지 않는다.

이 검사는 의미 보존을 증명하지 않는다. 짧지만 틀린 본문, 근거와 무관한 주장, 부적절한 분화는 에이전트의 판독과 별도 평가 대상이다. 보류는 `outcome:'deferred'`와 다음 행동을 남긴다. 빈 Scope가 됐더라도 미완료 선택에서 사라진 ID는 다음 실행의 대기에 남는다.

`move_nodes`는 실행 전 전부 검사하지만 여러 파일 이동은 하나의 트랜잭션이 아니다. I/O 중단 시 이동 의도와 이미 옮긴 항목·남은 항목을 보존한다. ID의 현재 위치를 확인해 남은 작업만 이어가고 `hub_links` 양쪽을 반영한다.

증류 영수증의 역사적 원료·대상 기록은 변경하지 않는다. 동일 ID·동일 내용·같은 최상위 군집의 이동은 보존 증거를 유지하며 현재 허브 연결은 별도로 검사한다. 본문 변경이나 경계 이동은 재판독 대상이다. 조직 점검 오류는 기존 raw 통합 안내를 삼키지 않는다.

## Obsidian 지식 그래프

Obsidian 기본 그래프는 내부 링크로 연결된 Markdown을 그린다. raw의 Markdown 형식은 원문 보존 형식이며 OSK 노드라는 뜻이 아니다.

```powershell
& .venv/Scripts/python.exe _governance/_engine/scripts/configure_obsidian_graph.py --vault-root .
& .venv/Scripts/python.exe _governance/_engine/scripts/configure_obsidian_graph.py --vault-root . --apply
```

첫 명령은 미리보기다. 적용은 `.obsidian/graph.json`의 검색식·미해석 대상 표시·태그 표시만 바꾸고 기존 바이트 백업과 적용 후 hash를 남긴다. Scope·Domain·Person 안의 `id`·`summary` 보유 문서를 투영하고 `_` 경로의 원료를 제외한다. 기존 확대·색상·고립 노드 표시 등은 유지한다. 파일 자체나 일반 검색의 제외 목록은 바꾸지 않는다.

이는 노드 계약 전체 검사나 dangling 수리를 대신하지 않는다. 원료를 도표에서 감추는 것과 실제 주소를 고치는 것은 각각 검증한다. 설정 파일 적용과 실제 Obsidian 화면 확인도 별개로 보고한다.

## 검증 경계

격리 vault의 경계 시험과 실제 MCP를 사용하는 독립 에이전트 실험을 구분한다. raw→Scope와 다음 독립 세션의 Scope→Domain에서 실제 본문 판독·변경·근거 배선·후속 재사용을 확인해야 성장 증거다. 명시적으로 요청한 백테스트는 자연 발생한 훅 실행의 자율성 증거로 세지 않는다.

