# 성장 상태 판독

`growth status`는 현재 대기량과 기간별 기록을 JSON으로 읽는다. 포착·복구 접수·검토 승인·노드
쓰기를 수행하지 않고, 성장 worker도 실행하지 않는다. 보고서의 `ok`는 각 구역을 읽었는지 뜻한다.
백로그가 없다는 뜻이나 지식이 유용하게 성장했다는 뜻으로 쓰지 않는다.

vault 루트에서 실행한다. Windows 예시는 다음과 같다.

```powershell
$env:PYTHONPATH = '_governance/_engine'
.venv\Scripts\python.exe -B -m osk.cli growth status --since '2026-10-01T00:00:00+09:00' --until '2026-10-03T00:00:00+09:00'
```

POSIX에서는 `PYTHONPATH=_governance/_engine .venv/bin/python -B -m osk.cli growth status`를 쓴다.
`--since`를 생략하면 읽을 수 있는 전체 이력, `--until`을 생략하면 관측 시작 시각까지다.
시각에는 시간대가 필요하며 시작은 포함하고 끝은 제외한다. 현재 대기량에는 이 기간 필터를 적용하지 않는다.

## 분모와 완료 경계

| 구역 | 단위와 판정 |
|---|---|
| `integration` | 이 기기·vault의 저장된 대화 커서. 포착 라운드, 최초 검토 대기, 복구 검토, 포착 대기·오류를 분리 |
| `reviews` | 기간 안에 기록한 대화 검토 결정. `preserved`, `summary`, `no_value`, `deferred`를 그대로 구별 |
| `runs` | 기간 안에 결과가 기록된 성장 실행. 선택 작업 수, 기록된 `ok`, native 종료 코드, 작업별 처분을 별도 표시 |
| `preservation` | 기간 안의 보존 결정이 참조한 서로 다른 저장 영수증. 본문·출처와 현재 배치를 실제 파일에 다시 대조 |
| `organization` | 현재 정돈 대기 군집의 본문 구간. `remaining_units`의 분모는 `units_in_pending_scopes` |
| `evictions` | 현재 미처분 퇴출 항목과 전체 이력의 처분 기록. 노드 보존이나 의미 판단의 성공률이 아님 |
| `recheck_history` | 기간 안의 기준선·새 근거 결속·자동 이어받음·직접 판독 유지·본문 수정 기록 |
| `rechecks` | 현재 노드와 근거 상태의 후보 쌍. 기준선 대기와 사용자 상신도 표시 |

`review_pending_rounds`는 최초 검토 대기와 복구 검토의 합집합이다. 두 집합에 든 라운드는 한 번만 센다.
`repair_jobs`는 작업 수이고 `repair_rounds`는 원문 라운드 수다. 아직 포착하지 못한 native 꼬리는
저장 커서만으로 셀 수 없어 `uncaptured_rounds: null`로 낸다. 포착 대기 대화 수를 라운드 수에 더하지 않는다.
기존 커서를 읽은 값이므로 이번 보고서가 새 복구 작업을 등록하지 않는다. 새로 발견한 영수증 불일치는
`preservation`에 따로 나타난다.

`native_exit_zero`는 모델 프로세스가 0으로 종료된 실행 수다. `recorded_nonempty_complete`는
과거 실행이 빈 선택 없이 완료로 기록된 수다. 당시 결과를 소급 정정하거나 지금도 유효하다고 인증하는 값은
아니다. 현재 영수증 검증 결과와 별도로 읽는다. 같은 본문이 남아 있어도 허브 연결이 사라졌으면
`body_and_sources: complete`, `placement: pending`이 동시에 나올 수 있다.

재검토 작업은 근거 제거, 대상 소실, dangling, 사용자 상신으로도 큐에서 닫힐 수 있다.
`runs.items[].queues.recheck`는 이 사유를 구별하며 직접 판독 성공에 더하지 않는다.
퇴출 큐인 `runs.items[].queues.eviction`은 완료 상태(`by_status`), 검토 결정(`by_review`),
처분(`by_settlement`)을 각각 집계한다. `node`·`merged`·`discarded`·`deferred`와 검토 미기록
(`unreviewed`)을 구별하며, 처분 미기록은 `unsettled`다. 처분 뒤에도 검토가 없거나 보류되면
완료 상태는 `pending`일 수 있다. 세 집계는 같은 작업의 서로 다른 분류이므로 합산하지 않는다.
노드 수, 영수증 수, `no_value` 결정을 의미적 성장이나 후속 재사용 횟수로 바꾸지 않는다.
이 보고서에서 `semantic_growth`·`downstream_reuse`는 `not_measured`다.

## 기동 원인과 버전

기존 `work_context="daily"`는 일괄 검토 큐를 선택하는 코드 경로다. 수동 호출에도 쓰였으므로
과거의 그 값만으로 예약 기동이라고 판단하지 않는다. 원인 기록이 없는 일괄 실행은 `unknown`,
옛 Stop 문맥은 `legacy_stop_context`로 분류한다.

새 실행은 기존 plan에 `invocation`과 적재한 `engine_rev`를 추가로 남긴다. 새 원장을 만들지 않는다.
Stop 검토 진입점은 `stop_hook`을 기록한다. 명시적인 일괄 실행은 기존 `growth run`에
`--invocation manual` 또는 사용자의 보존 요청에 따른 실행이면 `--invocation user_request`를 붙일 수 있다.
생략하면 `unknown`이다. `scheduled`도 명시한 원인 값일 뿐이며, 이 옵션은 작업을 등록하거나 켜지 않는다.
기동 원인 선언은 사용자 발화의 실제 의도나 OS 스케줄러의 호출을 독립 검증한 증거가 아니다.
Stop 전에 사용자가 저장을 직접 요청했는지는 별도 원문 판독이 필요하다.

`runtime`은 이 조회를 실행한 **CLI 프로세스**의 적재 엔진·디스크 엔진·실제 코드 경로를 표시한다.
`applied_version`은 인스턴스 갱신 기록, `manifest_version`은 릴리스 선언 파일의 값이다.
작업 트리에서 엔진을 수정했을 때 그 둘을 현재 코드의 판본으로 오해하지 않는다.

연결된 MCP는 같은 관측 구간에 `overview`로 확인한다. MCP 응답의 `engine_rev`, `engine_disk_rev`,
`engine_observed_at`, `engine_stale`를 보관하고 CLI의 판독과 구별한다. 디스크 판독 실패나 적재판 미상은
`engine_stale: null`이며, 최신 엔진임을 확인한 결과가 아니다. 오래된 서버에 새 필드가 없으면
그 필드를 CLI의 값으로 채워 넣지 않는다.

`--preflight`는 기존 `fork doctor`로 하네스별 CLI 버전·구독 인증·fallback 사유도 조회한다.
모델 추론은 실행하지 않는다. 이 일반 준비 점검은 특정 대화의 모델·권한·전사 동일성 검증을 대신하지 않는다.

## 재현과 제한

`items`의 대화·작업 식별자와 원장 rid를 통해 집계 근거를 찾는다. 원문이나 노드 본문은 출력하지 않지만
운영 경로와 식별자가 포함되므로 실 인스턴스 보고서는 공개 저장소에 커밋하지 않는다.
날짜를 판독할 수 없는 이력은 `unknown_time`, 기간 밖 기록은 `outside_window`에 분리한다.
손상된 구역은 `null`과 `errors`로 표시하고 명령은 실패 코드로 끝난다. 손상을 0건으로 처리하지 않는다.
커서 디렉터리가 비어 있으면 0건이지만, 목록을 읽을 권한이 없으면 대화·검토·보존 구역은 판독 불가다.

관측은 `started_at`부터 `finished_at`까지의 읽기 구간이며 원자적 snapshot이 아니다.
다른 프로세스가 그 사이에 쓴 결과가 섞일 수 있다. 시점 고정 비교에는 동결한 fixture 또는 별도 복제본을 쓴다.
`tests/test_growth_status.py`는 익명 입력을 고정하고, 재판독의 동일 결과와 파일 바이트·mtime 불변을
검사한다. 일반 조회가 기존 `integration status`의 복구 쓰기로 우회하지 않는지도 확인한다.

이 판독은 [v4.2 M1](v4.2-milestones.md)의 범위다. 실제 복구·누적 작업 처리·의미적 승격·후속 효과는
M2–M6의 개별 완료 기준에 따라 검증한다.
