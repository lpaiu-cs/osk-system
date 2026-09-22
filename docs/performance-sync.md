# #17 동기화 잠금 개선 실측 — 2026-09-22

동기화의 fetch·push 대기가 모든 MCP 쓰기와 같은 mutation 잠금을 점유했다.
전송은 잠금 밖에서 하고, working tree를 바꾸는 커밋·rebase와 보낼 SHA 확인만 잠금 안에서 수행한다.

fetch 결과는 호출마다 유일한 ref로 보존하므로 다른 fetch가 FETCH_HEAD나 origin/main을 바꿔도
이번 적용 대상은 바뀌지 않는다. 적용 직전에는 미완료 갱신·진행 중 Git 작업·브랜치를 다시 검사한다.
잠금을 놓은 뒤에는 검증한 SHA만 push하므로, 그 뒤에 생긴 커밋이나 수정은 이번 전송에 끼어들지 않는다.
거부 시 새 fetch부터 한 번 재시도한다. #72의 배율 충돌 처리·실패 시 전체 rebase 원복도 유지한다.

## 실측

Windows 11 / Ryzen 7 9800X3D / NTFS / Python 3.11.9.
동일 생성기의 1,918개 노드로 시작해 30개를 생성한 합성 vault, 실제 로컬 bare 원격을 사용했다.
비교 전 측정은 `0024dd2`, 변경은 #72가 합쳐진 `86c9745` 위에서 수행했다.
쓰기 프로세스는 전송 명령 시작 시 실제 update_node를 호출했다.
수정 전에는 pull 시작, 수정 후에는 fetch 시작에 도착한다. 임의 시각의 모든 쓰기 대기 분포가 아니다.

| 조건 | 반복 | 잠금 보유 p50 ms: 전 → 후 | 해당 쓰기 대기 p50 ms: 전 → 후 |
|---|---:|---:|---:|
| local-sync | 30 | 1001.91 → 569.26 | 1008.381 → 0.048 |
| injected-network-delay | 5 | 3490.77 → 558.62 | 4044.461 → 0.042 |
| real-push-retry | 1 | 7055.99 → 1070.72 | 7061.183 → 0.042 |
| incoming-16mib | 1 | 5556.14 → 563.63 | 5046.100 → 0.047 |

지연 조건은 각 전송 명령에 **1.2초를 주입**한 실험이며 WAN 지연 실측이 아니다.
재시도는 peer가 실제로 원격을 앞서게 해 첫 push를 거부시켰고 두 차례 전송에 합계 4.8초를 주입했다.
수정판의 재시도 잠금 보유는 두 적용 구간의 **합**이다. 단 한 건인 재시도·대량 수신은 분포 보장이 아니다.
p95·p99·쓰기 자체 보유 시간·동기화 전체 시간은
[CSV](../benchmarks/results/sync-20260922.csv)에 기록했다. 분위수는 (n−1)q 순위의 선형 보간이다.

지연 조건의 전체 동기화 시간은 3.55→3.47초다. 네트워크 자체를 빠르게 만든 결과가 아니라
다른 쓰기가 네트워크 완료를 기다리지 않게 만든 결과다.
fetch 중 쓰기는 이번 커밋에 포함됐으며, 원격 HEAD와 노드 내용을 되읽어 확인했다.

## 검증과 한계

독립 프로세스로 fetch·push 동안 잠금 획득 가능 / rebase 동안 불가를 검사한다.
FETCH_HEAD·origin/main 교체, push 직전 새 로컬 커밋, fetch 중 미완료 갱신·브랜치 변경,
실제 push 거부, 통신 실패 때 로컬 보존을 검증한다.

데몬 싱글턴과 updater의 singleton → mutation 순서는 유지한다.
적용 구간의 Git 작업은 여전히 약 0.56초 잠금을 점유한다. 그때 도착한 쓰기는 기다린다.
Windows CRT의 1초 재시도 방식도 이번 PR에서는 변경하지 않았다.
통신 실패 때 로컬 커밋은 유지하지만, fetch가 먼저이므로 그 커밋 시점은 fetch 시한 뒤가 될 수 있다.
Git hook·partial clone 등 내부에 별도 네트워크 작업을 넣는 사용자 설정은 측정 대상이 아니다.
실 vault·실행 중 데몬·MCP에는 적용하지 않았다.

## 재현

Windows에서 엔진 의존성이 설치된 Python을 사용한다. 반드시 존재하지 않는 새 실험 경로를 지정한다.

```powershell
py -3.11 benchmarks/bench_sync.py run --engine E:/path/to/patch/_governance/_engine --root E:/tmp/osk-sync-benchmark --output after-sync.jsonl --nodes 1918 --repeat 30 --slow-repeats 5 --delay 1.2
py -3.11 _governance/_engine/tests/test_sync_network.py
py -3.11 _governance/_engine/tests/test_regression.py
```

측정기는 bench_index.py의 동일 합성 fixture 생성기를 재사용한다. 기준판용 측정기는 같은 쓰기와
실제 Git 전송을 사용하되 pull 안에서 쓰기를 시작하고 잠금 해제 뒤 획득되는지 확인했다.
