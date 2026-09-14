# Windows 네이티브 명령과 Git Bash 경계

Git Bash/MSYS는 네이티브 프로그램을 실행하기 **전에** `/...`처럼 보이는 인자를
Windows 경로로 바꾼다. 따옴표와 `rtk proxy`만으로는 막히지 않는다. 공식 설명은
[MSYS2의 경로 변환 문서](https://www.msys2.org/docs/filesystem-paths/#process-arguments)에 있다.

OSK 저장소 작업 중 발생했던 사례는 OSK CLI가 아니라 `gh api -X PATCH /repos/...`
호출이다. 원본 로그 시각은 **2026-09-01T15:30:13Z = 9월 2일 00:30:13 KST**다.
`/repos/...`가 `C:/Program Files/Git/repos/...`로 바뀌어 실패했고, `repos/...`로
재시도했다. v3.14.0 이전부터 있던 셸 경계이며 업데이트 회귀로 분류하지 않는다.
ADB의 `/sdcard/...`도 같은 변환 대상이지만 별개의 작업·대상이다.

## 하네스의 공통 실행 규칙

다음 블록은 Claude와 Codex의 `@RTK.md`에 동일하게 적용한다. 이는 호출자가 따를
실행 규칙이며, 모든 셸 명령을 자동 검사하는 차단 훅은 아니다.

<!-- native-boundary:start -->
## Windows native command boundary

- Windows 네이티브 도구는 가능하면 PowerShell에서 `rtk proxy`로 실행한다.
- Git Bash에서 `gh api`의 endpoint는 `repos/...`처럼 선행 `/` 없이 쓴다.
- ADB의 `/sdcard/...` 등 리터럴 `/...` 인자를 네이티브 도구에 넘길 때는 **첫 네이티브 프로세스인 RTK 실행 전** `MSYS2_ARG_CONV_EXCL='*' rtk proxy ...`를 쓴다. 따옴표나 `rtk proxy`만으로 변환이 차단되지는 않는다. 이 설정은 해당 명령에만 적용하고, 로컬 파일 경로는 `C:/...` 또는 상대경로로 명시한다. 전역으로 변환을 끄지 않는다.
- 쓰기·배포·변경 후 검증은 `rtk proxy`로 원래 출력을 보존한다. 성공을 판정하기 전에 `>/dev/null`, `| tail`, 출력 필터로 stdout·stderr를 버리거나 자르지 않는다. `2>&1` 자체는 병합일 뿐이며, 전체 로그를 남겨도 된다. 파이프라인이 필요하면 `set -o pipefail`과 전체 로그 보존을 함께 사용한다.
- 쓰기 명령의 종료코드와 응답을 확인한 뒤 **같은 대상**을 독립적으로 다시 읽어 의도한 본문·해시와 비교한다. GitHub는 GET한 필드, ADB는 지정 기기의 대상 파일 해시, OSK는 `read_node`의 본문·해시를 확인한다. 확인할 수 없거나 불일치하면 완료로 보고하지 않는다. GitHub의 큰 본문은 `--input request.json` 또는 `-F body=@body.md`로 전달한다.
<!-- native-boundary:end -->

`MSYS2_ARG_CONV_EXCL`을 전역으로 설정하면 네이티브 도구에 넘기던 로컬 `/c/...`
경로까지 변환되지 않을 수 있다. 위처럼 호출별로 적용하고 로컬/대상 경로를
구분하는 편을 권한다. OSK 노드 변경은 계속 MCP로 한다.

## 호출 예시

```bash
# Git Bash: 읽기 예시. endpoint에는 선행 /가 없다.
rtk proxy gh api --method GET repos/OWNER/REPO/issues/comments/COMMENT_ID

# ADB: 실제 기기를 -s로 고정하고 전체 출력 보존 후 같은 대상 해시를 읽는다.
# 실제 전송은 해당 파일·기기에 대한 쓰기가 승인됐을 때만 실행한다.
MSYS2_ARG_CONV_EXCL='*' rtk proxy adb -s SERIAL push C:/task/payload.bin /sdcard/payload.bin
MSYS2_ARG_CONV_EXCL='*' rtk proxy adb -s SERIAL shell sha256sum /sdcard/payload.bin
```

GitHub 본문 수정은 `--input request.json` 등 파일 입력으로 실행하고, 같은 객체를
GET해서 JSON을 파싱한 `body`와 의도한 문자열을 비교한다. HTTP/CLI 성공만으로
본문 일치를 대신하지 않는다. ADB는 로컬 파일과 지정 기기의 대상 파일 SHA-256을
비교한다. OSK는 쓰기 응답의 `new_hash`와 후속 `read_node.hash`, 필요한 본문을
대조한다. 나중 쓰기로 판본이 바뀌었다면 성공을 추정하지 말고 다시 판독한다.

## 재현 검사

설치 과정에서 만든 저장소 루트의 `.venv`를 그대로 쓴다.

```powershell
rtk proxy .venv\Scripts\python.exe -B _governance/_engine/tests/test_windows_shell.py
```

설치된 Git Bash → RTK → 네이티브 Python의 **실제 argv**를 관측한다. 네트워크·
기기·vault에는 쓰지 않는다. `/repos`와 `/sdcard` 변환, 선행 슬래시 제거와
명령별 예외의 효과, 설정된 RTK 훅의 예외 보존을 검사한다. Git Bash 또는 RTK가
없으면 명시적으로 SKIP하며, 별도 위치는 `--bash`로 지정한다.

오류 출력 실험은 세 경계를 구분한다. `2>&1`만 쓰면 오류와 종료코드 7이 남고,
`2>&1 | tail -1`은 앞의 오류를 잃으며 기본 파이프라인 종료코드가 0이 된다.
`pipefail`은 종료코드를 되살리지만 잘린 출력은 복구하지 않는다.
로컬 대상 모형에서는 종료코드 0인 무효 쓰기를 본문·해시 되읽기로 검출한다.
이 모형을 실제 원격 기기에 대한 검증 성공으로 세지 않는다.
