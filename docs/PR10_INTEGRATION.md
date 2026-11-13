# PR #10 통합 검수 — 2026-11-13

## 통합 기준과 필요한 파일

기준 main은 a28522d, 기존 PR #10은 d63276d, 최신 P4는 8770b66이다.
기존 PR #10에 P4를 merge하여 두 이력을 보존하고 충돌 24파일을 해소했다. 강제 push나 main 직접 덮어쓰기를 사용하지 않는다.

PR #10의 필요한 부분은 GUI에서 정책 선택, RH 설정, 여러 실행 정책의 그래프/상태 표시,
비동기 계산, 정책 활성화 시 이미 저장한 주문 진행률 보존이다.
실제 작업·충전·통행은 최신 P4의 공통 모델과 검증된 prefix 실행기를 사용한다.

| 구분 | 통합 선택 |
|---|---|
| resource_pool/corridor/robot/path_graph/engine 등 물리 계층 | P4의 traffic-mm-bundle-v1, 작업·에너지 장부·단일 승객 캐빈·후미 해제 유지 |
| planning/decisions/evaluation | P4 v8/v5 모델·4개 RH backend·staged D1·원시 로그/재생 유지 |
| main_window/planning_settings/factory_panel/dashboard/state/sim_worker | PR #10의 라이브 제어 기능을 현재 계약과 정책 이름으로 이식. main의 통행 표시도 함께 유지 |
| 새 planning/live.py | 기존 native child와 독립 validator/publish_rh를 재사용하는 owner-thread submit/poll adapter. 계산만 별도 프로세스, 큐·물리 적용은 소유 스레드 |
| 기본 정책·DB·가중치·config.py | 기존 optimized/D0·두 기본 비교 정책 유지. DB 스키마 변경 없음 |
| 운영안 비교/ROI·RAG | main/P4의 기존 기능 유지. 예전 PR #10의 별도 실험·전력 환산 체계를 새 모델에 섞지 않음 |
| requirements·CI | 현재 고정 planning 의존성 포함, macOS/Windows/Linux × Python 3.11/3.14에서 전체 테스트·native worker·Qt 수명 검사 |
| 예전 문서/benchmark JSON | d63276d의 과거 증거로 보존하고 현재 결과와 구분하는 머리말 추가 |

## 대체되어 제거한 파일

삭제는 이 PR에만 있던 구 모델 구현의 통합 정리다. 과거 commit d63276d에서 원본을 재현할 수 있다.
기존 main/P4의 기능 또는 테스트를 삭제하지 않는다.

- `simulation/planning/warm_start.py`: 현재 `planning/incumbent.py`의 초기해·parity 검수로 대체.
- `simulation/energy.py`: 현재 plant/engine의 실제 에너지 장부·보존 불변식으로 대체.
- `simulation/wait_metrics.py`: 현재 `evaluation/resource_metrics.py`의 대기 episode·검열·원시 재분석으로 대체.
- `simulation/deadlock.py`: 구 자원 객체에 의존하는 별도 추론을 섞지 않음. 현재 관측은 장기 대기와 실제 교착 증명을 구분.
- `simulation/experiments.py`, `experiment_replay.py`, `scenario_suite.py`, `scenario_benchmark.py`, `benchmark_planning.py`: 현재 evaluation run/suite/replay/analyze/profile, JSON 시나리오와 tools/p4_pilot.py로 대체. 예전 모델의 우세 수치를 새 모델 결과로 표시하지 않음.
- 구 `test_rh_planning.py`, `test_rh_runtime.py`, `test_prd_acceptance.py`: 구 schema/자원 계약 전용. 기존 P4 전체 430개 회귀(공통 모델·독립 손계산·backend parity·prefix·자원 수명·시간초과·RSS·취소·D1·재생)를 유지하고 새 live/GUI 인수 테스트를 추가.

## 실행 의미와 검수 중 보완

GUI 정책은 baseline, optimized, assignment_milp_legacy, rh_milp_grid,
rh_cpsat_linear, rh_cpsat_interval, rh_cpsat_native의 현재 이름을 사용한다.
비활성 정책을 선택하면 기본 두 정책과 선택 정책을 관측한 구성/남은 주문으로 0초부터 재시작한다.
새 선택 전에 아직 DB에 flush하지 않은 완료 진척도 보존한다.

라이브 시간 규약은 `live-owner-poll-v1`이다. Qt의 시간 진행 중 frozen input을 계산하고 반환 뒤 최신 cohort/에너지/자원을 검사한다.
headless의 D0나 `d1-staged-budget-v1`과 같은 실험이라고 부르지 않는다.
실시간 창은 운용 데모이고, 공정한 외부 trace/초기 상태 비교와 최종 정책 선택 근거는 headless runner로 만든다.

코드 검수에서는 다음을 보완했다.

1. 구 GUI 상수가 main의 통행 표시 상수를 지우지 않도록 합쳤다. Qt callback 예외가 화면에만 찍히고 테스트가 통과하지 않도록 검사한다.
2. 구 scope 선택을 제거했다. 현재 solver는 지원하는 공통 교통/캐빈 계약을 사용한다.
3. comparison_seed 전달로 GUI 수요가 외부 입력 모드로 잘못 꺼지지 않도록 stochastic 모드를 명시했다.
4. 정책 활성화 전에 주문 진척을 flush하고, 더 느린 비교 정책으로 이동해 저장한 완료 수가 감소하지 않도록 했다.
5. GUI 계산에는 비동기 전송·watchdog/RSS·회수를 사용한다. correctness 인수의 넉넉한 예산과 운영용 예산을 구분한다. cold 성공·150ms 응답을 보장하지 않는다.
6. native의 GUI 시작값은 4방문 제한이며, 큰 전체 domain은 headless에서 설정/프로파일을 명시해 비교한다.

## 검증 결과

기능 검수 commit `63e3057`의 Python source hash는 `dd6f8c51003c66d763af6c9a7baf264b81a10ca517885af2e0069a782dcd89f4`다.
최초 통합 기능 검수: Python 3.11·3.14 각각 446 passed(기존 P4 430 + live/GUI 16).
추가 물리 실행 57/57 SUCCESS, 기록 재생·원시 지표 57/57 일치, 기존 D0 물리 digest 36/36 보존.
기본 config와 최초 기능 통합 commit 63e3057에서 핵심 모델·자원·controller·runner 14파일이 P4와 byte 일치했다. 후속 Windows 계측 수정은 아래와 같이 별도로 기록한다.
기존 P4 591회 결과는 과거 source의 증거로 유지하며 이번 통합의 새 실행 수로 바꾸지 않는다.
GUI 화면 검수 후 후보 수·최근 적용 판정·마지막 반환 계획의 표시를 보완했고, Python 3.11/3.14 각각 live/GUI 16개를 다시 통과했다. 이 후속 수정은 표시 getter/문구와 그 assertion이며 물리·수학·적용 로직을 변경하지 않았다.
원격의 최종 전체 회귀와 main 병합 여부는 PR #10의 현재 checks/merged 상태로 확인한다.
시간이 지난 native 최적 계획이 STALE_HORIZON으로 거부되어 대체 실행한 실제 GUI 기록도 보존했다. solver의 OPTIMAL 상태와 실제 채택을 혼동하지 않는다.

P5/P6의 실제 팀 지도 본 실험, 통계·최종 선택 규칙은 이번 충돌 해결/GUI 통합과 별도다.


## Windows Python 3.11 계측 호환성 보완

첫 최종 head 7368a71의 CI에서는 macOS/Linux 및 Windows3.14가 통과했지만 Windows3.11에서 12개 시간 검사 항목이 실패했다.
짧은 import/prepare/validate 구간이 0초로 기록됐고, 1us 공개 여유 초과도 놓쳤다.
CPython3.11 Windows의 monotonic/GetTickCount64와 고해상도 perf_counter의 차이에 맞춰
계획·검증·RPC·deadline·watchdog·D1 phase·Qt 명령 왕복을 perf_counter 기준으로 통일했다.
서로 다른 시계의 epoch를 빼지 않도록 요청 시작값의 모든 내부 호출자와 테스트도 함께 바꿨다.
환경 manifest에는 실제 clock implementation/resolution/monotonic 정보를 기록한다.

0초 값에 임의 epsilon을 더하거나 기존 >0/초과 판정을 완화하지 않았다.
거친 monotonic이 멈춘 상태에서도 미세 phase 비용을 기록하고 20us 공개 비용이 1us 여유를 넘는지 확인하는 회귀 2개를 추가했다.
수학 제약·목적·자원 단위·기본 정책은 변경하지 않았다. 이 수정의 최종 head는 GitHub checks에서 별도로 검증한다.
직전 57회 물리 증거는 63e3057의 것이며 현재 clock/source로 재표기하지 않는다.

근거: [Python3.11 perf_counter](https://docs.python.org/3.11/library/time.html#time.perf_counter),
[CPython clock 구현 차이 기록](https://github.com/python/cpython/issues/126745),
[첫 CI의 실패 보존](https://github.com/tortoiese/CLOVER/actions/runs/43381140664).
