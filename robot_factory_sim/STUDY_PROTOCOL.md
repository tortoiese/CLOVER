# 합성 연구용 실험 동결과 실행

사용자가 실제 공장 자료 대신 **합성 연구환경 검증**을 선택했다. 좌표·수요·SOC·납기·부하는 연구 입력이며 실제 공장 제원/성능으로 해석하지 않는다. 기존 `prepare-design`의 E0/E1 경로와 결과는 그대로 보존한다.

## 시간과 지표

기본 optimized, 휴리스틱 가중치, 수학 목적, 기본 D0와 기존 D1 v1 soft 규약은 유지한다. 연구 spec의 D1 v2는 전체 2초 중 admission reserve 0.1초와 publication allowance 0.2초를 계산 예산에서 먼저 뺀다. 관측 저장·기동·IPC 비용도 남은 시간을 줄인다. admission 전후와 실제 공개 직전에 기한을 확인한다. 경계를 넘은 정상 계획은 거절하고 기존 안전 fallback/HOLD를 사용한다.

이것은 SimPy에 주입한 시간과 선불 publication coverage에 대한 규약이다. OS의 hard realtime 보장이나 fallback까지 포함한 전체 cycle 2초 보장이 아니다. publication의 실제 wall 비용이 allowance를 넘으면 coverage 위반을 보존하며 소급해서 유효 성공으로 세지 않는다.

- `control.summary.by_kind.dispatch`와 `auxiliary`를 나눈다. 같은 기록 수를 작업 수나 독립 표본 수로 해석하지 않는다.
- 관측창에 trigger와 그 deadline이 모두 들어오는 요청만 기한률 분모에 쓴다. 종료 경계에 걸린 요청은 검열, 기한이 지났는데 pending인 요청은 cycle 초과다.
- accepted와 timely accepted, late accepted, fallback, finalized/pending, 지연 P50/P95/P99/max를 보존한다. pending이 있으면 paired latency quantile/fallback 비율은 미정으로 남긴다. 지연 표본의 분위수는 완료된 결정만의 기술통계다.
- RH snapshot은 RPC 전에 `planning_inputs/<content hash>.json`에 저장한다. worker가 반환하지 못해도 `freeze-snapshot --decision INDEX`로 export 가능하다. 이때 경로/공통 모델 준비는 분석 단계에서 재생성한 것이며 원래 worker가 준비를 끝냈다는 뜻이 아니다. capture 실패/기동 전 실패에는 존재하지 않는 입력을 만들지 않는다.

## 입력과 관측창

spec은 `clover-study-spec-v1`이며 출처·검수자, 전체 수학/D1 설정, 주 지표·실용 차이·guardrail·선택 규칙, selection 근거와 모든 case를 포함한다. `docs/scenarios/experiment_hardening_20261114/`의 JSON을 예제로 쓴다.

case에는 phase, condition, layout, seed, stochastic, scenario 전체, window, load를 명시한다. stream을 trace로 바꾼 경우 generator와 seed를 `demand_generation`에 남긴다. λ는 tasks/s, reference μ와 산정 방법은 정책별 사후 재조정 없이 공통이다. materialized trace의 평균 실현 도착률과 설계 λ는 구분한다.

현재 runner의 창은 `[warmup, duration)`이다. `arrival_cutoff + drain_seconds == duration`, `drain_in_measurement=true`를 요구한다. drain을 쓸 때는 도착을 실제 trace에서 중단해야 한다. `finite_batch`, `finite_stream`, `transient_stress`는 유한 기간 결과다. `steady_stream`은 drain을 허용하지 않고 로봇별 최소 2회 완료 충전 관측을 요구한다. 이것만으로 정상상태가 증명되는 것은 아니다.

`calibrate-window`는 원시 event hash·자원 재구성·에너지 장부·전체 창을 검사한다. 구간별 처리율/WIP, 로봇별 완료/중단/진행 중/창 이전 시작 세션과 충전량을 남긴다. 완료 세션은 창 안에서 시작·종료하고 사전 지정한 용량 비율 이상을 충전해야 한다. 충전 시작 횟수나 총충전량/배터리 용량만으로 반복 순환을 인정하지 않는다. nominal은 fault/rescue가 없어야 하며 stress는 이를 결과로 보존한다.

## 단계와 잠금

`training`, `validation`, `heldout`, `stress`를 명시한다. heldout은 training/validation의 geometry 및 seed와 겹칠 수 없다. 공장/시나리오 이름, 노드 ID, 화면 크기만 바꾼 지도를 새 지도로 인정하지 않는다. 동일한 물리 입력을 seed 이름만 바꿔 복제하는 것도 거부한다. 기하 변형 몇 개는 모집단의 독립 지도 표집이 아니므로 예제의 `layout_sampling_independent`는 false다.

`freeze-study`는 source manifest·Git HEAD/blob 일치·환경·모든 입력/trace hash·설정·분석을 봉인한다. `run-study`는 동결 source/환경/코드가 다르면 시작하지 않는다. 전체 arm을 먼저 기록하고 예상 밖 evaluator 오류 뒤에는 후속 arm을 시작하지 않는다. 진행 중/미실행/실패를 없애거나 재시작 시 덮어쓰지 않는다. 갑작스러운 프로세스 종료에는 마지막 RUNNING checkpoint와 전체 계획이 남는다.

`study-analysis-plan`으로 등록된 계획을 만들며 지표/정책/phase/입력/설정을 사후 변경하면 분석을 거부한다. 가족별 native 추가 대조는 case별 candidates로 표시한다. pilot의 heldout phase도 분석 purpose는 E0 기술통계이며 유의성/일반화 우세를 선언하지 않는다.

confirmatory는 코드의 모든 Python 파일이 Git HEAD와 일치하고, training/validation 입력별 기간 보정 PASS가 독립 재구성돼야 동결된다. 보정에 heldout/stress seed를 쓰지 않는다. heldout 결과를 보고 관측창/설정을 조정하지 않으며, heldout의 exposure/fault는 결과와 guardrail로 보고한다. 현재 확정 가능한 설정 선택은 **a_priori + 근거**다. 자동 training/validation 최적 profile 선택 receipt는 아직 지원하지 않아 그 방법의 confirmatory 동결을 거부한다. 파일럿의 H40/t2를 최적 설정으로 확정했다는 뜻이 아니다.

주 지표·실용 차이·guardrail·선택 규칙은 등록하지만 자동 추천 판정은 아직 하지 않는다. 분석 결과의 `registered_phase_verified`는 등록 일치만 뜻하며 `heldout_validated`/`recommendation`을 참/알고리즘 이름으로 바꾸지 않는다. 최소 반복/독립 지도/실패·검열/0분모에 대한 기존 추론 보류 규칙을 유지한다.

## 명령

프로젝트의 `robot_factory_sim` 디렉터리에서, 패키지가 설치된 Python을 사용한다. 출력 경로는 매번 새 경로를 지정한다.

```bash
python -m simulation.evaluation freeze-study --spec ../docs/scenarios/experiment_hardening_20261114/smoke_spec.json --output /tmp/clover-study/frozen.json
python -m simulation.evaluation run-study --frozen /tmp/clover-study/frozen.json --phase training --output /tmp/clover-study/training
python -m simulation.evaluation study-analysis-plan --manifest /tmp/clover-study/training/manifest.json --output /tmp/clover-study/training-plan.json
python -m simulation.evaluation compare --plan /tmp/clover-study/training-plan.json --output /tmp/clover-study/training-analysis
python -m simulation.evaluation calibrate-window --run /path/to/run.json --window ../docs/scenarios/experiment_hardening_20261114/charge_window.json --output /tmp/clover-charge-calibration.json
```

validation/heldout/stress는 같은 frozen 파일과 각 phase로 실행한다. 큰 연구를 재시작하거나 설정을 바꾸려면 새 spec/freeze/출력과 이유를 남기며 유리한 seed만 다시 선택하지 않는다. pure backend 비교는 동일 grid Model/Hint의 snapshot 경로를 쓴다. native는 다른 표현·시간 단위·방문 상한을 가진 운영 대조다.
