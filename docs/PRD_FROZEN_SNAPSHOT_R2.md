# R2: 동결 snapshot의 공통 grid backend 비교

작성일 2026-11-14. PR #16 다음 작업이다. 운영 정책·기본값·가중치는 유지한다.

실행 후 상태: 아래 사전 계획을 유지하고 [48arm 실행·감사 결과](R2_SNAPSHOT_REVIEW_20261114.md)를 추가했다. 비교 기반은 구현했고 기능·비용 감사는 통과했다. 실행의3건 soft budget 초과는 별도로 보존하며 R2/R3 전체 완료로 표시하지 않는다.

## 목적

closed-loop에서는 계산 중 로봇이 움직이고 fallback으로 다음 상태도 달라진다. 같은 수요 seed만으로 MILP와 CP-SAT이 같은 문제를 푼다고 볼 수 없다. 실제 기록에서 관측 하나를 명시적으로 골라 입력·경로·후보·정수 제약·두 목적식·시작해를 동결하고, 동일한 공통 grid 모델의 SCIP와 CP-SAT linear를 비교한다. native/interval은 이번 순수 backend 비교에서 제외한다.

## 구현 계약

1. `freeze-snapshot`: 현재 v5 실행 결과와 결정 index를 받아 worker의 반환 plan input을 저장한다. 기한 초과로 거절된 반환 계획도 선택 가능하며 주 기록 `rh_result`와 다르면 거부한다. 기록/입력 해시와 스키마, grid 영역, 설정, 실제 기하로 다시 만든 route 일치 여부를 확인한다. 원래 소스와 현재 생성 소스를 구분한다.
2. 한 공통 sparse Model과 checked Hint를 준비한다. JSON에는 PreparedInput·모델 해시/크기·시작해·목적값·생성 비용·원본 provenance를 저장한다. 디스크 pickle은 사용하지 않는다. 다시 읽을 때 현재 소스·전체 artifact 해시를 검증하고 공통 Model을 한 번 재구성해 저장 해시/시작해를 확인한다.
3. `benchmark-snapshot`: 하나의 공통 Model과 Hint를 각 격리 worker에 전달한다. 같은 정수 제약·순서·두 단계 목적·시작해·seed42/worker1을 쓴다. 운영 `Backend.solve`의 기존 두 단계 계산을 재사용하고 선택적 prebuilt 인자를 추가한다. 기본 호출 경로는 유지한다.
4. 선택 backend를 기동한 뒤 worker 안에서 측정하는 **compile/search/check/decode** 예산을 두 solver에 똑같이 적용한다. 공통 route/model/hint 생성, worker startup, IPC 전송, 독립 계획 검사는 예산 밖에서 각각 측정한다. 요청 watchdog은 계산 예산+명시적 공통 여유이며 D1 운영2초를 증명하는 실험이 아니다. 같은 worker를 다음 arm에 재사용하지 않는다.
5. 기존 SolverProcess의 identity·watchdog·RSS·종료 처리를 재사용한다. 모델을 반환값으로 재구성하거나 불완전한 timeout을0점으로 채우지 않는다. 독립 계획 검사 실패·모델/입력 불일치·UNAVAILABLE·timeout·메모리·미실행을 전부 보존한다.
6. 전체 실행 순서를 seed로 동결하고 직렬 실행한다. 반복은 같은 입력의 기술 반복이며 독립 수요 seed가 아니다. 공통 모델의 두 OPTIMAL 목적 쌍이 다르면 PARITY_MISMATCH로 실패시킨다. FEASIBLE끼리 차이가 나는 것은 기한 내 해 품질의 관측이며 최적성 불일치가 아니다. CI/p값·최종 추천을 내리지 않는다.
7. 출력 덮어쓰기를 거부하고, 실행 전/각 arm/종료 시 소스와 snapshot 파일 변경을 확인한다. 변경 이후 arm은 실행하지 않는다. setup 실패도 전체 계획 분모에 남긴다. CSV와 전체 결과 manifest·원본 SHA256를 저장한다.

## 인수 검수

- 작은 실제 batch에서 두 backend가 동일 모델·시작해로 유효한 계획과 같은 증명된 최적 목적을 반환한다.
- 잘못된 설정/native 입력·손상된 artifact/시작해/모델·중복 예산·출력 덮어쓰기를 거부한다.
- 기한 초과 반환 계획, worker 실패/해제, 예산 미실행·소스 변경을 보존한다.
- 실제 worker를 사용하는 CLI 경로, 기존 다중 backend/계획/D1 회귀, 전체 테스트와 lint를 확인한다.
- 시뮬레이션 과거 원본과 source hash를 바꾸지 않는다. 새 기능의 source freeze 이후 별도 실험을 실행한다.

## 첫 기능·비용 파일럿

PR #16의 h40_t2에서 S01/S04/S08/S11 각각 **rh_milp_grid의 첫 worker 반환 plan 중 jobs가 있고 available 로봇이 있는 입력**을 선택한다. 결과의 좋고 나쁨·accepted·solver 상태로 골라내지 않는다. 해당 조건에 없으면 누락을 기록하며 다른 정책으로 바꾸지 않는다.

4snapshot×2backend×계산 예산0.25/2초×기술 반복3회=48arm이다. 공통 순서 seed20261114, startup10초·요청 여유3초·worker 메모리1024MiB, 전체 예산300초를 사용한다. 단일 장비 직렬 실행이며 후보/model/hint 준비는 arm 전에 끝낸다. 이 실험은 저장된4관측의 기능·비용 점검이고 새 closed-loop 운영 시뮬레이션이나 E1 독립 반복48회가 아니다. 임의 유리한 재시도는 하지 않는다.

선택한 관측은 S08 index4(작업4·가용 로봇1), S04 index1(작업1·가용4), S11 index1(작업1·가용4), S01 index4(작업4·가용1)이다. S08/S01은 원래 운영 중 STALE_AVAILABILITY로 거절된 반환 계획이다. [동결 실행 프로토콜](scenarios/r2_snapshot_20261114/protocol.json)에 원본 SHA256·선택 index를 기록했다. 이 표본은 worker가 입력을 반환한 기록에 조건부이며 입력을 반환하지 못한 hard timeout 관측은 포함하지 못한다. 이를 모든 의사결정의 대표 표본으로 해석하지 않는다.

## 완료 경계

동결 비교 기반·48arm 감사까지 완료해도 R2/R3 전체 완료가 아니다. D1 운영 예산의 검증/공개 여유 배분, 실제 지도·λ·기간/충전 보정, 복수 지도/수요 seed의 training/validation, E2/E3·최종 선택 규칙은 후속 작업이다.

## 최종 계측 검수 보완

최초 코드 `8327f84`로48arm을 실행·감사한 뒤 기존 Backend report의 total이 마지막 action decode/반환 구성 전에 저장됨을 확인했다. 그48arm의 원본·판정·해시는 [이전 계측 묶음](scenarios/r2_reported_timing_20261114/publication.json)에 보존한다. 이는 complete-call 시간의 인수 근거로 사용하지 않는다. 평가 전용 RPC에서 `Backend.solve`를 호출하기 직전부터 반환 직후까지 직접 측정하고 동일 시작 시각을 backend의 deadline 기준으로 전달한다. 동작 해석을 포함한 이 범위를 회귀 검사하며 일반 운영 solve 경로는 바꾸지 않는다. 수정 소스를 별도 동결해 같은 선택 규칙·예산·전체48arm을 새 출력 경로에서 다시 검수한다. 두 소스의 시간 결과를 합치거나 첫 결과의 source hash를 바꾸지 않는다.
