---
id: 260830-0000-v374
created: 2026-08-30 23:27 (KST)
updated: 2026-08-30 23:27 (KST)
author: user
drafter: opus-5
summary: "v3.7.4 — 엣지 델타를 누적 상태 위에서 계산한다. 근거 교체 한 호출이 근거를 통째로 지우던 경로를 막았다"
---

# 근거를 바꾸려다 지우던 자리 — v3.7.4

## 적용 시 반드시 할 것

**`osk` 셸 명령은 없다** — 그렇게 치면 Windows 화상 키보드가 뜬다.
`PYTHONPATH=_governance/_engine python -m osk.release|osk.update`.

**데몬은 프로세스째 멈춘다.** 예약 작업 이름은 **`osk-sync-daemon`**이고,
갱신이 데몬 싱글턴을 수명 내내 잡으므로 먼저 멈춰야 한다.

```powershell
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
  Where-Object { $_.CommandLine -like '*sync_daemon*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 어디서 나왔나

v3.7.3의 근거 표기 이관(74노드 / 124간선)을 수행하다 발견했다. 발견 경위가
이 판의 논거 절반이다 — **결함을 재현해 문서로 적은 직후에 내가 그 결함에
걸렸다.** 오타 난 근거를 고치려고 add와 remove를 한 호출에 담았고, add가
삼켜졌다. 규율로 피할 수 있는 종류가 아니다.

## 무엇이 잘못돼 있었나

`update_node`의 두 루프가 **각자 원본 노드에서** 현재값을 다시 읽었다.

```python
for pred, tg in (add_edges or {}).items():
    cur = n.edges(pred)                     # 원본
    meta[pred] = _as_links(pred, cur + new) # meta에 씀
for pred, tg in (remove_edges or {}).items():
    cur = n.edges(pred)                     # 또 원본 — meta를 안 본다
    keep = [t for t in cur if ...]
    meta[pred] = _as_links(pred, keep)      # meta를 덮어씀
    meta.pop(pred, None)                    # 또는 통째로 지움
```

remove가 "add가 없었던 것처럼" 계산한 값으로 `meta`를 덮는다. 원본에 지울
간선이 **하나뿐**이면 `keep`이 비어 술어를 통째로 `pop`하고, **방금 추가한
근거까지 사라진다.** 그러면서 `ok: true`를 낸다.

```
시작   derived-from: 260830-1rs2-p70uupup
update_node(add={derived-from: "근거대상"}, remove={derived-from: 그 id})
  → ok: true
  → derived-from: None          ← 근거가 통째로 없어졌다
```

**드문 호출이 아니다.** "근거를 A에서 B로 바꾼다"는 이관·오타 수정·근거
갱신의 자연스러운 표현이고, 표면의 스키마가 두 인자를 함께 받는다.

곁가지로 `contract.target_stem`이 독스트링과 어긋나 있었다 — *"경로형과
스템형은 같은 대상이므로 마지막 요소의 stem으로 접는다"*고 적었는데 위키링크
괄호를 벗기지 않아 `[[N]]`·`N`·`N]]` 세 키로 갈렸다. 호출부가 `Node.edges`
(이미 벗겨진 값)를 먹이는 자리에서는 안 드러나지만 **호출자 입력은 안 벗겨진
채로 들어온다.** 맨 이름으로 넣으면 중복 판정이 되고 `[[이름]]`으로 넣으면
안 되는 상태였다. v3.7.3 이관에서 맨 이름을 쓴 것이 우연히 맞았다.

## 무엇을 고쳤나

- **두 루프가 누적된 `meta`를 읽는다.** `meta`는 `dict(n.meta)`로 시작해 add가
  쓴 결과를 담으므로, remove가 그것을 보고 계산한다. 순서 의존이 사라졌다.
- **해석을 한 벌로 모았다.** `Node.edges`의 파싱을 `contract.edge_targets()`로
  빼고 원본·저장 표기 양쪽이 같은 함수를 쓴다. 두 벌이면 조용히 갈라진다.
- **`target_stem`이 약속대로 접는다.** 위키링크 괄호를 벗겨 `[[N]]`·`N`·
  `[[= Scope/A/N]]`이 모두 `N`이 된다. id는 접지 않는다.

## 시험과 검출률

수트 895 통과 / 기지 실패 5. **뮤테이션 검출 3/4.**

| 뮤턴트 | |
|---|---|
| add 루프를 원본 읽기로 되돌림 | **놓침 — 동치 뮤턴트** |
| remove 루프를 원본 읽기로 되돌림 | 검출 (7건) |
| `target_stem` 괄호 벗기기 제거 | 검출 (8건) |
| `edge_targets`가 위키링크를 안 벗김 | 검출 (24건) |

첫째가 안 잡히는 이유는 명확하다 — add 루프가 도는 시점에는 `meta[pred]`가
아직 원본과 같으므로(그 사이에 바뀌는 것은 `summary`뿐) 어디서 읽든 값이
같다. **실제로 결함을 고치는 것은 둘째 하나**이고 첫째는 여벌이다.

그래도 남긴다. 두 루프가 서로 다른 출처를 읽는 것이 방금 고친 결함의
정체이므로, 루프 순서를 바꾸는 다음 사람이 같은 함정을 다시 열지 않으려면
둘이 같은 곳을 봐야 한다.

넷째가 24건을 깨뜨린 것은 `edge_targets`가 기존 시험에 넓게 물려 있다는
뜻이며, 해석을 한 벌로 모은 판단을 뒷받침한다.

## v3.7.3 이관이 함께 남긴 것

- **이관 완료**: 74노드 / 124간선을 제목형 근거로. 검증기 PASS, 위상 위반 0.
- **통치 구획 5개는 표면이 거부했다** — `Constitution`·`Bylaws`·`Mechanism`·
  `Workbench-Contract`·`constitution-disposition-table`. 정본에서 고쳐 갱신으로
  도달해야 한다(헌법 3조 6항). **아직 남아 있다.**
- **보호영역은 표면이 막지 않는다.** `= Person/Delegation`·`= Person/Module`의
  7개 노드를 고쳐 승인본 결속이 깨졌고 검증기가 FAIL을 냈다(위임 3요건).
  사용자 재승인으로 해소했다. 통치 구획은 `_reject_governance`가 막아 주지만
  보호영역은 정상 쓰기를 허용하므로(§6-2 8항) **배치 작업 전에 호출자가 대상의
  소속을 훑어야 한다.** 이번에 그러지 않아 값을 치렀다.
- **본문 Link의 id 표기 15간선**은 그대로다. `derived-from`과 달리 본문 전문
  치환이 필요해 성격이 다르다.

## 남은 재료 (3자 독립 검토 지목)

1. 쓰기 응답의 주 손잡이를 이름으로 — id 핸들 `update_node`가 이름의 10배다.
2. Searcher 결합·중복 제거 — `overview`·`read_node`도 `_s()`를 부른다.
3. 읽기 통로의 규모 — 쓰기 1회가 각 프로세스의 다음 읽기에 전수 판독을 물린다.
4. 동기화 데몬의 잠금 보유 — git 네트워크 왕복이 전역 잠금 안에 있다.
