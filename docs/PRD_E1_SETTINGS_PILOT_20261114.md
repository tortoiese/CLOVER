# E1 설정 실행 가능성 파일럿

작성일: 2026-11-14. 상태: 실행 전 동결. 상위 계획은 [실험 PRD](PRD_ALGORITHM_EXPERIMENTS_v2.md)의 R2/R3다.

실행 후 상태: 아래 사전 계획을 유지하고 [60회 실행·검수 결과](E1_SETTINGS_PILOT_REVIEW_20261114.md)를 추가했다. 설정 스크린은 완료했고 R2/R3 전체는 계속 진행 중이다.

## 목표와 범위

현재 구현으로 계산 제한과 계획 범위가 실제 운영 결과에 어떤 영향을 주는지 점검한다. 실제 팀 지도·수요 JSON이 없는 상태이므로 합성 참조를 사용한다. 이번 결과는 E1 설정 후보를 좁히는 자료이며 본 실험, 통계적 우열 판정, 실제 공장 추천이 아니다. 새 알고리즘이나 휴리스틱 가중치 변경은 하지 않는다.

순수 solver 성능과 closed-loop 운영 성능은 다르다. 이번에는 계산 중 공장이 움직이는 공통 staged D1에서 정책·기동·계획 거절·fallback을 포함해 평가한다. 같은 snapshot·후보·목적식을 고정한 backend 대조는 후속 R2 작업으로 남긴다.

## 동결 입력

[프로토콜 JSON](../robot_factory_sim/examples/e1_screening_20261114/protocol.json)에 전체 실행 순서·제한·seed를 고정한다. 같은 폴더의 references/settings JSON을 prepare-design의 외부 참조로 사용한다.

| 조건 | 목적 | 관측창 / warmup | 입력 |
|---|---|---|---|
| S01_eligible_1 | 배정·경로 계획과 fallback 차이 | 300 / 0초 | 고정 batch 8개 |
| S04_mixed_soc | 서로 다른 초기 SOC의 충전 압박 | 450 / 0초 | 수요 seed 1001, 9개 |
| S08_two_cars | 두 캐빈과 층간 계획 | 300 / 0초 | 고정 batch 8개 |
| S11_budget_2 | 64노드에서 계산·운영 예산 | 180 / 0초 | 수요 seed 1001, 16개 |

위 작업 수는 실행 전 materialize로 확인한 값이다. 이 관측창은 실행 가능성 검사용 단축 창이다. stream 안정 상태나 로봇별 반복 충전은 보장하지 않는다. S04의 구조 에너지·고장은 반드시 별도로 보고하며, 발생한 경우 에너지 자립 운용이라는 결론을 내리지 않는다. 모든 조건은 미보정 합성 입력이다.

세 설정에서 지도·trace·초기 로봇/SOC·초기 캐빈·납기·작업 UID·시간창이 완전히 같아야 한다. 정책별로 입력이나 부하를 보정하지 않는다. 준비된 JSON의 파일 SHA256, scenario/trace/initial 해시를 교차 대조한다.

## 비교와 실행 예산

정책은 baseline, optimized, rh_milp_grid, rh_cpsat_linear, rh_cpsat_native 5종이다. legacy/interval은 이번 설정 스크린에서 제외한다. 기본 정책과 가중치·기본 D0는 유지한다.

| 설정 | horizon | solver timeout | 비교 목적 |
|---|---:|---:|---|
| h40_t025 | 40초 | 0.25초 | 기준 대비 계산 제한만 축소 |
| h40_t2 | 40초 | 2초 | 기준 설정 |
| h60_t2 | 60초 | 2초 | 기준 대비 horizon만 확대 |

공통 수학 설정: step 2초, 후보 작업 최대8, sigma0, 재계획 간격5초, watchdog3초, native_actions4, native_step0.1초. 나머지는 준비 manifest의 전체 Settings를 따른다. math timing_mode는 D0로 두고 실행 계층에 공통 D1을 적용한다. MILP/CP-SAT 내부 seed는42·worker1, 수요/run seed는1001이다. 둘은 다른 난수다.

D1 공통 설정: on-demand 기동, startup10초, soft deadline2초, publication allowance0.2초, fallback0.1초, memory1024MiB, request watchdog12초. D1에 주입한 시간은 측정한 배타적 단계와 선불 publication 여유의 합이다. 실제 전체 wall time과 같은 숫자라고 표현하지 않는다. baseline/optimized도 같은 D1 규약을 쓴다.

4조건×5정책×3설정=60회다. 조건·설정의 12개 block 순서는 seed20261114로 미리 섞는다. 각 block의 정책 입력 순서도 별도 고정 난수로 섞으며, runner의 실제 실행 순서는 child manifest에 남긴다. 수요 seed는 바꾸지 않는다. 같은 장비에서 하나씩 직렬 실행하고 다른 native 계산·시험을 병행하지 않는다.

run wall limit120초, 전체 실행 예산1800초다. 전체 예산에 도달하면 남은 arm을 미실행으로 보존한다. 이 한도는 실시간 의사결정2초와 별개다. 실행 제한으로 잘린 run도 실패·미완료 분모에서 지우지 않는다. 최대한도는 준비·분석·해제·저장까지 엄밀히 포함하는 OS 강제 종료 타이머는 아니다.

## 변경·실패 규칙

실행 전 데이터/문서와 Git commit을 동결하고 모든 앱 Python 파일이 Git blob과 일치함을 확인한다. source_hash는 `75016001011187ca9bf4272169911a5698770bf32c87800f6690c280312d5f3a`다. 실행 중 Python 소스 변경이나 동시 native 실행을 금지한다.

공통 publication0.2초 위반이 검출될 때만, 해당 조건의 **세 설정·모든 정책**을 공통0.5초로 한 번 추가 실행할 수 있다. 원본을 보존하고 별도 cohort로 분석하며 전체 예산 잔여 내에서만 수행한다. 이외 timeout·불리한 성능을 이유로 특정 정책만 재시도하지 않는다. 수정이 필요한 코드 오류는 원본 실패와 소스를 남긴 뒤 별도 소스로 검수한다.

## 인수 기준과 산출물

- 준비된 세 묶음의 입력·해시 동일성과 source freeze를 통과한다.
- 모든 예정 arm을 성공·실패·부분·미실행 중 하나로 남긴다. SUCCESS는 시뮬레이션/감사가 정상 종료됐다는 뜻이며 모든 작업 완료를 의미하지 않는다.
- 원시 이벤트 재구성, 저장 결정 replay, 에너지 보존, 작업 상태 합계, 자원 용량/소유권, source/trace/initial 일치, D1 coverage·worker 해제를 감사한다. wall-limit 경계가 replay와 다르면 해당 한계를 남긴다.
- 처리량·미완료·납기·전체 batch makespan(전체 완료일 때만), 충전 대기/소비·구조 에너지·fault, accepted/fallback·사유, 측정/주입 시간·build/solve·메모리 기록을 요약한다. 미기록 단계는 0으로 채우지 않는다.
- 설정별로 R5 분석을 분리하고 목적을 training으로 명시한다. 조건별 seed1, batch 고정, 실제 지도 없음이므로 CI/p값·최종 추천을 내리지 않는다. 설정3개를 독립 seed3개로 세지 않는다. 지도 간 같은 seed를 독립 반복으로 합치지 않는다.
- 원시 데이터는 workspace outputs에 보존하고 재현용 입력·프로토콜·파일 해시·감사 receipt·요약 CSV·검수 보고서는 Git에 저장한다. 실행 후 상위 PRD의 완료 경계도 갱신한다.

## 이후 작업

실제 팀 지도/trace·λ·납기를 확정하고 충분한 충전 순환/관측창을 보정한다(R1/R4). 이번 결과로 training 설정 후보를 검토하고 같은 snapshot 대조, 복수 지도·수요 seed의 E1 training/validation을 사전 동결한다(R2/R3). R5 통계 기반을 이용하되 E2 held-out·E3 stress·환경별 선택 규칙(R6/R7)은 별도로 검증한다.
