# 본 실험 전 보완 구현 계획

2026-11-14. [사전 검수](PRE_EXPERIMENT_REVIEW_20261114.md)의 미완료 항목을 구현한다. 기본 optimized·휴리스틱 가중치·수학 모델·D0와 기존 D1 v1의 soft 규약은 유지한다. 실제 공장 자료를 임의로 생성하거나 보정 완료로 표시하지 않는다.

## A. 결정 시간과 관측 보존

- 선택 가능한 D1 v2 연구 프로필을 추가한다. 정상 계획은 검증 예비시간+publication allowance를 남겨 계산하고, admission 및 실제 공개 직전에도 기한을 확인한다. 기한 뒤 정상 계획을 적용하지 않는다. 안전 fallback/HOLD의 비용은 계속 측정하며, 전체 cycle의 엄격한 실시간 완료 보장을 주장하지 않는다.
- accepted, timely accepted, 전체 cycle 초과·pending, dispatch/auxiliary의 모집단을 구분한다. v1은 기존 soft 동작을 유지한다. v2는 staged simulated time과 publication coverage에 대한 계약이다.
- RH snapshot은 worker 요청 전에 content-addressed JSON으로 보존한다. 출력 없는 실행은 inline으로 보존한다. 저장 비용도 지연에 반영한다. timeout/error의 입력도 export할 수 있고 replay는 같은 관측 hash를 확인한다. 입력 capture 실패는 없는 입력을 만들어내지 않는다.

## B. 입력·기간·보정

- 프로토콜에서 실제/합성 출처를 명시하고 공통 trace·좌표 단위·초기 SOC/캐빈·납기·λ·기준 용량·관측창을 검증한다. 합성을 실제 자료로 승격하지 않는다.
- 관측창은 현재 runner의 warmup~duration과 정확히 맞춘다. 도착 중단/공통 drain을 선언하며 drain을 측정창에 포함하는 경우 이를 명시한다. steady stream은 drain 없이 고정 창을 사용한다. 정책별 완료 시점으로 기간을 변경하지 않는다.
- calibration 검사는 run 원시 자료와 작업 시각으로 구간 처리율/WIP, 로봇별 완료 충전 세션, rescue/fault를 확인한다. 요구 충전 횟수·관측 길이·nominal/stress 기준은 사전 프로토콜에 둔다. 미검수 입력/기간은 confirmatory 실행을 막는다.

## C. 실험 동결·실행·분석

- `freeze-study`/`run-study` 경로에 training/validation/heldout/stress를 명시한다. 같은 조건 내 정책은 같은 파일/trace/초기 상태/seed·공통 D1 예산을 사용한다.
- heldout은 학습/검증의 지도·seed·중복 입력과 분리한다. 지도 이름 변경·고정 batch seed 재명명을 독립 반복으로 인정하지 않는다.
- source·환경·정책·전체 설정·창·주 지표·분석 설정·학습/검증 선택 근거를 동결한다. confirmatory 단계는 미보정 자료나 불완전한 선택 근거를 거부한다. 모든 arm을 사전 기록하고 오류/미실행을 보존한다.
- heldout/stress 분석은 동결된 프로토콜과 정확히 일치할 때만 해당 phase로 허용한다. E0/training의 기존 분석은 유지한다. CI의 가정·최소 반복 문턱·실패/검열 보류를 유지하며 자동 우세/추천을 만들지 않는다.

## 검수와 완료 경계

시간 경계/재시도/공개 거절·fallback·v1 호환, 실패 입력·CAS 변조·경로 탈출·replay, 관측창·충전/에너지·구간 통계, phase 누수·source/설정 변경·동결 덮어쓰기·미실행 보존과 실제 CLI를 검사한다. 전체 회귀와 직렬 smoke 실험에서 소유 자원·에너지·작업 장부를 감사한다. 이전 결과와 source는 보존한다.

코드/검증 경로의 완료와 실제 데이터/본 실험 결과의 완료는 구분한다. 실제 입력·보정 결과·training/validation·새 지도/seed가 없으면 confirmatory 준비 상태는 계속 미완료다.

## 범위 확정·구현 현황

사용자가 합성 연구환경 검증을 승인했다. 실제 공장 자료의 부재 자체는 합성 검증의 차단 조건에서 제외한다. 연구용 가정·출처와 실제 공장 효과 검증의 경계는 유지한다.

구현 경로는 `d1-primary-deadline-v2`, worker 전 CAS, dispatch/auxiliary 기한 요약, `calibrate-window`, `freeze-study`, `run-study`, `study-analysis-plan`이다. 확인할 입력 예제는 `docs/scenarios/experiment_hardening_20261114/`와 해당 폴더의 생성기다. 12가족×4phase의 전체 입력은 생성기로 만들며, 작은 smoke 입력은 Git에 포함한다. 큰 원시 trace·run은 별도 출력에 보존하고 digest/검수 요약을 공개한다.

confirmatory는 committed Python, 개발 입력별 보정 PASS와 검토된 reference μ·근거를 요구한다. 현재 구현한 선택 경로는 근거를 명시하는 a-priori 설정이다. training/validation 결과에서 최적 profile을 자동 선택하는 receipt, 자동 환경별 추천은 후속 작업이며 구현된 것으로 표시하지 않는다. 주 지표/실용 차이/guardrail/선택 규칙의 등록과 실제 우세 판정은 구분한다.
