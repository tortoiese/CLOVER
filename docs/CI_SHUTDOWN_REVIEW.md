# PR #12 GUI 종료·Windows 회귀 테스트 보완

2026-11-13. 대상: `gui/test_shutdown.py`, `agent/test_rag.py`, `gui/test_rh_gui.py`.

## 확인한 실패

[PR #12의 최초 CI](https://github.com/tortoiese/CLOVER/actions/runs/43388889126/job/131763794478)에서 macOS/Python3.11은 447 passed, 1 failed였다. 실패는 `test_close_waits_for_agent_and_discards_late_actions`의 `[True, True]` 검사였고 실제 값은 `[True, False]`였다. 나머지 5개 OS/Python 조합과 CodeQL은 성공했다.

기존 테스트는 요청을 100ms 뒤, 창 닫기를 200ms 뒤에 예약하고 fake agent는 400ms 뒤 응답했다. GUI 이벤트 처리가 늦어지면 종료 시점에도 요청이 진행 중이라는 보장이 없다. `AgentBridge.isRunning()`만으로 요청 슬롯의 연결까지 완료됐다고 볼 수도 없다.

기존 fixture를 그대로 사용하고 GUI 스레드에 700ms 지연을 주자, agent가 먼저 끝나고 창은 정상 종료했지만 기존 검사의 예상 `[True, True]`와 다른 `[False, False]`가 나왔다. 이는 기존 검사의 시간 가정이 깨지는 재현이며, CI의 정확한 스케줄링을 재현했다고 주장하는 것은 아니다.

## 수정

- 실제 Qt 요청 슬롯 연결을 확인한 뒤 요청을 한 번 전송한다.
- fake agent가 요청 시작을 알리고, 테스트가 허용하기 전에는 응답하지 않도록 Event로 순서를 제어한다.
- 종료 요청이 보류되고 창이 유지되는지, 반복 종료 요청도 안전한지 검사한다.
- 응답을 허용한 뒤 실제 `response_ready`와 `create_order` 조치가 도착했음을 확인한다. 응답을 받지 못했기 때문에 조치가 0건인 상황은 통과할 수 없다.
- 늦은 조치가 실행되지 않고 worker/agent가 종료된 뒤 창이 닫히는지 확인한다.
- 일반 상황과 GUI 스레드에 600ms 지연을 준 상황을 각각 검사한다. 지연은 경합 스트레스이며 요청/종료 순서를 정하는 수단이 아니다.
- 실패 경로에서도 응답 보류를 풀고 스레드·창을 정리한다. watchdog은 무한 대기를 막는 상한이며 성공 조건은 실제 상태다.

`MainWindow`, `AgentBridge`, 알고리즘·기본 정책·종료 제한값은 변경하지 않는다. 테스트의 기존 성공 조건을 완화하지 않았으며 요청 진행과 실제 늦은 응답 도착 검사를 추가했다.

연결 관찰에는 현재 고정 의존성 PySide6의 [QObject.isSignalConnected](https://doc.qt.io/qtforpython-6.11/PySide6/QtCore/QObject.html#PySide6.QtCore.QObject.isSignalConnected)를 사용한다. 이는 준비 조건의 관찰이고, 요청 처리 순서는 별도의 시작/응답 Event로 보장한다.

## 검증 범위

- Python3.11 종료 테스트: 일반·GUI 지연 2조건 통과.
- 같은 QApplication에서 일반·GUI 지연을 각10회 반복: 20/20 통과.
- Python3.14 종료 및 RH GUI 통합 테스트: 13개 통과.
- Python3.11 전체 회귀: 449 passed, 실패·오류·스킵 0. 종료 테스트를 2조건으로 확장해 기존448개에서 1개 늘었다.
- 오류 주입: 종료 중 응답의 조치를 실행하도록 guard를 우회하자 두 조건 모두 `assert not actions`에서 실패했다. 테스트가 해당 보호 동작의 회귀를 실제로 검출한다. 원본 파일을 바꾸지 않는 임시 monkeypatch로 실행했다.
- 수정 파일 Ruff와 `git diff --check` 통과.
- 전체 회귀 및 원격 OS별 검증은 PR의 최신 head 검사 결과를 따른다. 최초 실패 기록을 삭제하거나 성공으로 바꾸지 않는다.

기존 `design_review_v3.json`은 문서 검수 시점 `f614e21`의 main 기준 증거다. 이번 테스트 변경은 그 이후 보완이다. 평가 도구의 source hash는 테스트 Python 파일도 포함하므로 새 hash가 생기지만, 이 변경을 알고리즘의 물리 동작이나 새 62조건의 성능 실험 결과로 해석하지 않는다.

## 후속 Windows 검사에서 확인한 별도 실패

종료 보완 `5ef13d0`에서 기존 종료 테스트는 6환경 모두 통과했다. 그러나 [같은 PR CI](https://github.com/tortoiese/CLOVER/actions/runs/43416690979)에서 Windows3.11의 RAG 폴더 정리와 Windows3.14의 초기 상태 대기가 각각 실패했다. 두 환경 모두 448 passed, 1 failed였으며 macOS/Ubuntu 4환경은 각각449개를 통과했다. 이 기록도 보존한다.

- **RAG 정리**: `ChromaStore.close()`가 호출된 뒤 `TemporaryDirectory.cleanup()`에서 `chroma.sqlite3`의 WinError32가 발생했다. RAG 테스트의 자원 범위는 유지하고, 저장소를 먼저 닫은 뒤 임시 폴더 삭제에서 해당 공유 위반만 최대5초 재시도한다. 실제 삭제가 끝나야 성공이다. 다른 권한 오류나 계속 남는 잠금은 실패로 보존하며, 전체 테스트 재시도나 `ignore_errors`를 사용하지 않는다.
- **GUI 초기화**: 기본 공장의 worker 준비와 GUI 상태 전달을 모두 일반8초 대기 안에 넣고 있었다. 초기화 완료는 별도30초 watchdog, 준비 뒤 상태 전달은2초로 나누고 실제 초기화 시간을 결과에 남긴다. 운영 중 명령의2초 기준·30개 응답 검사·150ms 목표 보고는 유지한다. 이 테스트로 cold startup이2초/150ms 안에 끝났다고 주장하지 않는다.

새 정리 검사는 일시적 WinError32 이후 실제 삭제, 영구 잠금의 실패, 다른 권한 오류의 실패를 각각 확인한다. 초기화9초를 주입하면 기존8초 방식은 실패하고 보완 방식은 통과했다. 이는 별도 초기화 예산의 재현이며 CI의 정확한 스케줄링 원인을 단정하는 것은 아니다. 이 보완도 제품 코드가 아닌 테스트 수명·관측 범위의 변경이다.

후속 로컬 검수: 관련28개 통과, Python3.11 전체452 passed(실패·오류·스킵0), 변경3테스트의Ruff와 `git diff --check` 통과. 449개에서 파일 정리 검증3조건이 추가됐다. 최종 원격 결과는 이 보완을 포함한 PR 최신head의 checks를 따른다.
