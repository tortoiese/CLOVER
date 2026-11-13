# 본 실험 전 검수와 보완 계획

검수일: 2026-11-14. 검수 기준: PR #17 head `23bf2463f22ed90e37418bf0a7ef64153a8d9403`와 그 위의 실패 보존 보완. **현재 판정: 본 실험 시작 보류.** 파일럿 완료와 실제 환경·새 지도/seed에서의 검증 완료를 구분한다. 아래 미완료 항목을 해결한 뒤 실험용 코드·입력·프로토콜을 동결한다.

## 우선 해결할 항목

| 우선순위 | 항목·근거 | 필요한 보완 | 통과 기준 |
|---|---|---|---|
| P1 / R3 | 운영2초는 soft budget이다. 기존 E1에서2초 초과 결정19건, 그중 accepted/COMMITTED3건이 약2.121/2.170/2.211초에 적용됐다 | 검증·공개 여유를 계산 예산에 남기고, 본 실험이 soft budget 효과를 평가하는지 엄격한 완료 제한을 평가하는지 명시한다. accepted와 기한 내 accepted를 분리한다 | 전체 지연·P95/P99·초과·fallback·pending을 보고하고, 정한 기한 규칙을 모든 정책에 동일 적용한다. 늦은 해를 기한 내 성공으로 세지 않는다 |
| P1 / R1 | 실행 참조가 `synthetic_reference_not_team_factory`·`UNCALIBRATED`다. 실제 팀 지도/외부 trace 보정이 끝나지 않았다 | 좌표/층/방향/폭·설비/충전 슬롯·초기 SOC/캐빈·적합 설비·납기·도착 UID·에너지·절대 λ를 확정한다 | 각 조건의 JSON·출처·단위·hash를 동결하고 정책 간 완전한 입력 일치와 실제 경로/병목 노출을 확인한다 |
| P1 / R4 | 긴 stream/반복 충전의 안정성 검수가 미완료다. S11의16/16 대15/16 차이에는180초 종료 경계가 포함됐다 | 공통 warmup/측정창과 도착 중단/공통 drain을 사전에 정한다. nominal과 stress의 부하·구조 에너지/고장 허용 기준을 구분한다 | 여러 충전 순환, 구간별 처리율/WIP·SOC·미완료를 확인한다. 정책별 완료 시점에 맞춰 관측창을 다르게 늘리지 않는다 |
| P1 / R2 | 현재 동결 비교는 반환 입력4개에 조건부이며 미반환 hard timeout 관측은 없다 | worker로 보내기 전 관측을 content-addressed JSON으로 남기는 경로와 실패 관측의 분석을 추가한다 | 입력이 존재하는 성공/실패/timeout 모두 같은 선택 규칙으로 포함한다. capture 자체 실패는 입력 없음과 실패 이유로 남긴다 |
| P1 / R5~R7 | prepare는 `e0/e1-candidate`, 분석 목적은 `e0/training`만 지원한다. `heldout_enabled=false`이고 E2/E3 입력·분리가 동결되지 않았다 | E1 training/validation으로 설정을 정한 뒤 E2의 새 지도/seed와 E3 stress를 구분하고, 주 지표·실용 차이·실패 처리·선택 규칙을 사전 등록한다 | 같은 지도에서 정책끼리 paired 입력을 쓰되 기술 반복·고정 batch 재명명을 독립 seed로 세지 않는다. 충분한 독립 지도/수요 반복과 추론 가정을 확인한다 |
| P1 / 기준 코드 | PR #13/#15/#16/#17이 모두 open이다. main은 `b14cbe9` | 검토된 기능 브랜치 또는 main의 실제 SHA와 교차 플랫폼 CI를 확정한 뒤 본 실험에 사용한다 | 실험 전후 source hash와 Git blob 일치, 환경/패키지/설정·입력 동결, 기존 기록의 source 보존. 이번 검수에서 병합하지 않았다 |

이 항목들은 새 알고리즘이 전부 필요하다는 뜻이 아니라, 실험의 공정성·시간/입력 규약·결론 범위를 확정하기 위한 완료 조건이다. [상위 PRD](PRD_ALGORITHM_EXPERIMENTS_v2.md)의 R1~R7 완료 처리와 연결한다.

main 병합 자체가 실험의 필수 조건은 아니다. 검증된 기능 브랜치의 commit을 고정해도 된다. 다만 main에서 최신 실험 기능이 모두 제공된다고 가정하거나, 계속 바뀌는 브랜치 이름만 기록해서는 안 된다.

## 이번에 재현하고 수정한 코드 결함

동결 benchmark에서 예상하지 못한 `SolverProcess.request` 예외를 주입했다. 수정 전에는 worker close는 실행됐지만 manifest가 **RUNNING·계획12건·저장 row0건**으로 남았다. 실행이 중단됐을 때 실패한 arm과 나머지 계획을 제대로 추적할 수 없는 결함이다.

보완한 동작은 다음과 같다.

- 전체 계획을 `NOT_STARTED`로 먼저 저장하고, 각 arm을 시작하기 전에 `RUNNING`을 checkpoint한다.
- 예상 밖의 계산/응답 예외는 현재 arm의 `EVALUATION_ERROR`·원인·파일 hash로 남기고 다음 arm은 `NOT_RUN_EVALUATION_ERROR`로 종료한다. 버그가 난 상태에서 새 native 계산을 계속하지 않는다.
- close 예외 또는 해제 미확인은 `DISPOSAL_FAILED`·원인·해제 상태로 남기고 이후 arm도 시작하지 않는다. 정상 worker 실패와 예상 밖의 evaluator 오류를 구분한다.
- 정상·예산 초과·source/입력 변경·실패 기록과 실제 worker CLI 회귀를 유지한다. 운영 scheduler·solver 모델·가중치·기본 정책은 바꾸지 않는다.

실행 중 강제 프로세스 종료는 Python이 최종 기록을 쓸 수 없으므로 마지막 checkpoint가 RUNNING으로 남을 수 있다. 이제 그 경우에도 전체 계획은 사전 저장돼 있다. 같은 출력 디렉터리에 조용히 덮어써 재시작하지 않는다.

재현·검수 근거는 [판정 JSON](scenarios/pre_experiment_review_20261114/receipt.json)과 그 폴더의 예외 재현·D1 예산·통합 검사 기록을 따른다. 기존 E0/E1/R2 결과를 수정하거나 새 소스로 재귀속하지 않았다. 이번에는 새로운 알고리즘 성능 실험을 수행하지 않는다.

코드 보완 `508419fdac525c3e73c6c2fe5436efb6c0cd4430`에서 전체577검사(Python3.11), 관련47검사(Python3.14)를 통과했다. 수정 후 같은 worker 예외를 주입했을 때 PARTIAL·12/12 row, 오류1·미실행11과 실행 전 checkpoint의 RUNNING1·NOT_STARTED11을 확인했고 worker도 닫혔다. 이 fault 검수는 native solve나 새 성능 시뮬레이션이 아니다.

## 2초 예산에서 구체적으로 확인한 내용

기존 E1 60개 run의 control 기록5119개 중 finalized5118·pending1, soft deadline 초과19·초과 accepted3이었다. 이는 보조/빈 동작도 포함하는 **control 기록**이며 독립 작업5119개나 새로운 반복 수가 아니다. 원래 source와 result hash는 [예산 검수](scenarios/pre_experiment_review_20261114/d1_budget_review.json)에 보존한다.

현재 controller는 계산 직전에 남은 시간을 구해 solver timeout을 줄인다. 따라서 이 기능이 전혀 없다는 지적은 맞지 않는다. 남은 보완은 계산 뒤의 admission/공개 비용을 미리 확보하고, 공개 직전의 기한 확인·soft deadline 정책을 일관되게 정의하는 것이다. 현재 `_admit_process`는 준비·선불 publication 뒤에 적용하며 완료 시 `soft_deadline_exceeded`를 기록한다.2초 초과 accepted는 현 soft 규약에서 숨겨진 실패가 아니라, strict2초 보장을 주장할 수 없다는 관측이다.

기존 `profile`은 계산·기동·fallback·pending·초과를 기록하지만 paired 분석의 기본 계산 지표는 주로 run wall이다. 본 실험에는 전체 decision latency와 기한 내 유효 계획률·fallback을 별도 지표로 동결해야 한다. RH 계획·보조 판단·빈 동작을 무조건 합친 accepted 비율만으로 scheduler 품질을 비교하지 않는다. 기동/IPC/검증/공개를 뺀 R2의 prebuilt solve 시간도 전체 운영 지연과 다르다.

## 알고리즘 튜닝에서 남은 확인

휴리스틱 가중치는 유지한다. MILP/CP의 H·step·후보 수·계산 제한·시작해·두 목적의 시간 배분을 E1에서 비교하고 그 뒤 고정한다. 현재 첫 목적에 남은 시간의60%를 쓰는 절차가 있어, S01의 MILP가2초 예산 중 약1.245초에 FEASIBLE로 끝나는 등 예산을 늘려도 목적 품질이 바로 개선되지 않을 수 있다. 이 상태를 알고리즘의 보편적인 한계로 결론 내리지 않는다.

순수 backend 비교는 현재처럼 같은 grid Model/Hint를 사용한다. native는 시간 단위0.1초·로봇당 최대4회 방문 제한, grid는2초 단위였으므로 표현/범위 효과를 따로 기록한다. 운영 비교의 주 근거는 실제 처리량·대기·에너지·납기·제어 비용이고, 서로 다른 모델의 objective 숫자를 직접 섞어 순위를 매기지 않는다.

## 입력·기간·통계 확정 시 지킬 규칙

1. λ는 고정된 중립 기준의 용량 추정/보정으로 정하고, 비교 정책별 결과를 보고 서로 다르게 맞추지 않는다. reference μ·이동거리·작업당 에너지는 실제 값으로 재검수한다.
2. 유한 batch는 전체 완료 여부와 makespan을 보고한다. stream은 공통 고정 관측창의 처리량·미완료/WIP·납기를 본다. 도착 중단과 공통 drain은 현재 trace 입력으로 표현 가능하나 실제 설계와 종료 한도는 별도로 동결한다.
3. nominal에서 구조 에너지·fault가 발생하면 에너지 자립 운용으로 주장하지 않는다. stress에서 발생한 구조/고장은 결과로 보존한다. 실패·미완료를 성과가 낮다는 이유로 제외하지 않는다.
4. waiting P95가 작업 시작 대기인지 resource queue episode인지 정의한다. 완료된 대기만 보거나 검열을0초로 채우지 않는다. 취소/납기 대상 UID가 다르면 비율 차이를 단순 비교하지 않는다.
5. 최소10 seed/3지도 같은 분석 문턱은 유의성/검정력 보장이 아니다. 수요 변동 파일럿과 실용 차이·독립 지도 수를 기준으로 반복 수를 동결한다. R2 기술 반복3회를 수요 seed3개로 쓰지 않는다.
6. main 연구의 정책은 core4종(baseline/optimized/MILP/grid CP linear), native는 설계된 가족의 추가 대조다. legacy/interval은 기전 대조로 구분한다. 학습에 쓴 지도·seed로 E2 우세를 검증하지 않는다.
7. 긴 소규모 예비 실행으로 wall time·raw 로그량·worker RSS를 실측해 전체 실행 예산/저장 한도를 정한다. raw result/events·최초 실패·재실행 이유·소스/설정/seed/환경을 함께 보존한다.

## 이미 통과한 부분

공통 통행 mm/slot·원자적 확보/해제, headless의 GUI 순회 분리, 같은 입력/hash·단위·에너지/작업/자원 장부, 저장 결정 replay, 공통 grid parity, 실패/미완료·0분모·검열에 따른 추론 보류 기반은 갖춰져 있다. 기존 회귀와 최신 PR의 CI는 기능 인수 근거이며 실제 공장 효과를 증명한 근거와 구분한다.

검수 시작 시 main과 PR17 head의 가상 내용 병합에는 충돌이 없었다. 이는 실제 병합·최종 통합 회귀 완료를 뜻하지 않는다. [통합 검사 기록](scenarios/pre_experiment_review_20261114/integration_review.json)에 기준 SHA와 상태를 남겼다.

## 권장 진행 순서

1. R3의 시간 규약·예산 여유와 모든 관측/실패 기록을 보완한다. 기존 기본 정책과 가중치는 유지한다.
2. 시나리오 담당자와 실제 JSON·λ·기간/충전 검수를 완료하고, 복수 지도/수요 seed의 E1 training/validation을 실행해 설정을 고정한다.
3. 검토·통합된 실제 코드 SHA·환경과 E2/E3 프로토콜·주 지표·선택 규칙을 동결한 뒤 본 실험을 시작한다.

이번 문서/코드 보완만으로 위 미완료 항목을 완료 처리하지 않는다. 현재 판정은 계속 **본 실험 시작 보류**다.
