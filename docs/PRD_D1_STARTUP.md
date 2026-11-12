# 공개용 실행 PRD

원본 PRD의 로컬 파일 링크를 저장소 경로로 정리한 사본이다. 요구사항은 구현 전 명세이며, 실제 완료 여부는 D1_STARTUP.md와 P4 검수 보고서로 확인한다.
원본 문서 SHA-256: `49ee2cad4873bc7cc3dcd24e5d11d7c2137bac815aaf106acc76a3194ff89a4e`.

# PRD v1.0 — 전체 정책 D1과 초기화·시간초과 개선

작성일: 2026-11-13 KST

상태: **구현 전 실행 명세**. 이 문서의 요구사항·설정·테스트는 앞으로 구현할 내용이다.
이번 문서 작성으로 D1 또는 worker 개선이 구현된 것으로 표시하지 않는다.

## 1. 목적과 쉬운 설명

목표는 두 가지다.

1. 휴리스틱·MILP·CP-SAT이 답을 계산하는 동안에도 공장이 움직이게 하고, 늦게 나온 답을 확인한 뒤 적용한다.
2. 계산기를 처음 켜는 비용과 켜진 계산기로 답하는 비용을 구분하고, 시작 실패·멈춤·재시작까지 관리한다.

예를 들어 오전 10초의 공장 상태를 보고 0.7초 동안 계산했다면, 그 답을 10초에 바로 적용하지 않는다.
10.7초까지 공장에서 일어난 이동·작업 도착·취소를 반영하고 다시 확인한다.
확인·적용 준비에 든 시간도 반영해야 한다. 잘못된 답은 폐기하고 대체 판단을 하는 시간도 기록한다.

완료 결과는 **일곱 정책을 같은 시간 규칙으로 비교할 수 있는 headless 실행 기반**이다.
특정 알고리즘의 승리나 모든 요청의 0.5초 이내 완료를 완료 조건으로 삼지 않는다.

## 2. 기존 계획과의 관계

상위 계획은 알고리즘 선택 실험 PRD v2 §7.1·§9·§17 (기준 로컬 산출물)이다.
이 문서에는 snapshot부터 검증·commit까지의 지연, cold/warm 구분, 실패 비용 공개 원칙이 있다.
이전 RH PRD §9.4·§11 (기준 로컬 산출물)에는 준비 상한·요청 제한·취소 원칙도 있다.

그러나 현재 일곱 정책의 공통 제안 계약, 로봇 내부 충전 판단의 처리, 준비 완료 handshake,
재시작 상태 전이, 적용 비용의 선반영, 새 결과/replay 계약과 구체적 회귀 게이트를 묶은 명세는 없다.
본 문서는 그 두 작업의 실행 명세를 보완한다. 상위 PRD 전체를 교체하지 않는다.

### 2.1 기준 소스와 실제 확인 상태

- 검수한 로컬 소스 (기준 로컬 산출물): `common-model-v1.9-native-profile`.
- Python 소스 hash: `fdf37077ee32d75edb4154b2e0e24c374934a293658d5bb1095177c30ea7f289`.
- 관측 schema v6, 계획/모델 v8, headless 결과 v4. 원격 main 최신 상태를 뜻하지 않는다.
- 직전 검수 보고서 (기준 로컬 산출물): Python 3.11·3.14 각각 358개 테스트, 261회 실행·재생·지표 재계산 확인.

| 확인한 현재 동작 | 남은 문제 |
|---|---|
| RH 네 정책은 요청 지연을 SimPy 시간에 반영 | 반환 후 재검증/commit은 `apply_wall_seconds`로 측정만 함 |
| 실패 시 SafeFallback으로 복구 | fallback 계산 비용은 별도 공장 지연으로 반영하지 않음 |
| baseline·optimized·legacy는 직접 배정 | 전체 정책 D1 suite를 runner가 거부 |
| 로봇이 should_charge/choose_charger/charge_target 호출 | dispatch만 지연시켜도 충전 판단이 D1을 우회할 수 있음 |
| worker를 요청 시 spawn하고 이후 재사용 | 준비 완료 handshake·독립 준비 상태·재시도 정책이 없음 |
| watchdog·RSS 제한·강제 회수 있음 | 준비와 운영 실패를 구분하는 결과·replay 계약 보완 필요 |
| native 모델 구성 계측·캐시·연결 제거 구현 | cold 0.5초 사례의 timeout 자체는 해결되지 않음 |

mixed60 native 12회 중 이전·수정본 모두 11회에 fallback이 있었다.
수정본 cold 요청의 부모 시간 중앙값은 약 0.749초, import 중앙값은 약 0.430초였다.
이 수치는 기능 검수 환경의 관측값이며 실제 공장 SLA 또는 전체 운영 실패율이 아니다.

## 3. 범위와 유지할 조건

대상 정책: `baseline`, `optimized`, `assignment_milp_legacy`, `rh_milp_grid`,
`rh_cpsat_linear`, `rh_cpsat_interval`, `rh_cpsat_native`.

포함: 정책 관측·제안·검증·적용 분리, D1, 준비/재시작, 실패 대체, 시간·수명 로그, replay, headless 검수.
범위 밖: GUI 확장, 사용자 DB 변경, 팀원의 실제 여섯 환경 본 실험, 최종 알고리즘 추천·paired CI 구현.

- 기본 `optimized`, 기존 가중치, 충전 기준, MILP 목적함수·후보 범위를 보존한다.
- D0는 계속 지원하고 기본값도 유지한다. 기존 소스·결과는 별도 보관한다.
- 통로 mm·slot, 원자적 확보, 후미 해제, 캐빈 capacity=1, 작업·에너지 장부를 보존한다.
- legacy의 40개 후보·60초 cutoff·HiGHS 설정은 정책 범위로 계속 공개한다.
- 새로운 공통 SOC 예비량 기준을 휴리스틱에 강제해서 기존 정책을 조용히 바꾸지 않는다.
  실제 물리적 실행 가능성 검사와 각 정책이 선택한 에너지 예비 전략을 구분한다.
- 코드 변경은 별도 전달 소스에 하고 기준 소스는 수정하지 않는다. GitHub 배포는 이 PRD의 작업 범위에 넣지 않는다.

## 4. 시간 계약 — 먼저 고정할 핵심 설계

### 4.1 공장 시간과 측정 시간

`wall`은 monotonic 시계로 측정한 계산 시간, `sim`은 공장 사건의 가상 시각이다.
시간 변환 계수는 1 wall-sec = 1 sim-sec다. 실제 파일 저장·통계 집계·원시 로그 flush 비용은 연구 도구 비용으로 별도 기록한다.
정책 판단·IPC·검증·적용 준비 비용을 로그 비용으로 돌려서 제외하지 않는다.

headless는 한 시각의 불변 입력으로 계산한 후 해당 지연의 반환 사건을 예약할 수 있다.
계산을 마쳤다는 이유로 반환 사건보다 먼저 작업·충전·예약을 적용해서는 안 된다.
그 사이의 공장 사건은 원래 시각 순서대로 처리하며, 계산기는 그 미래 사건을 미리 읽지 않는다.

### 4.2 단계와 순서

```text
관측 τ → 불변 입력 → 제안 계산 → 계산 지연만큼 공장 진행
       → 현재 상태에서 적용 준비·검증 → 그 계산 지연과 적용 여유만큼 공장 진행
       → 최신 의존 상태 확인 → 원자적 적용 또는 폐기
       → 필요하면 현재 상태로 대체 제안 계산 → 대체 계산 지연 → 검증·적용
```

각 계산 구간은 서로 겹치지 않게 기록한다. 요청 전체 시간과 stage 합계를 섞어 중복 합산하지 않는다.
재검증이 다시 필요하면 새 단계·새 지연으로 기록한다. 무료 재검증·무료 fallback을 허용하지 않는다.
새로운 계산마다 당시의 관측 시각을 남긴다.
공통 제어 대기열에서 이미 흐른 `queue_sim_wait_seconds`와 IPC에서 실제 기다린 `ipc_queue_wall_seconds`를 분리한다.
전자는 종단간 지연에 포함하지만 다시 timeout으로 더하지 않는다. 후자는 해당 계산 구간의 wall 비용에 한 번 포함한다.

### 4.3 commit 비용을 뒤늦게 더하지 않기

실제 배정부터 한 뒤 commit 시간을 측정해 나중에 기다리면, 기다리는 동안 작업이 이미 사라지고 로봇이 예약되는 오류가 생긴다.
따라서 검증과 적용 준비는 **부작용 없는 transaction**으로 만든다. 큐·owner·배정·충전·예약 변경 내역을 준비하되 공개하지 않는다.

마지막 원자적 공개도 CPU 시간이 들므로 다음의 **보수적 시간 규약 `d1-staged-budget-v1`**을 사용한다.
이는 모든 단계를 실제 경과 시간과 정확히 같은 값으로 주입한다는 주장이 아니다.

1. 관측·제안·현재 상태 검증·transaction 준비의 측정 시간은 실제 값으로 지연한다.
2. 마지막 최신 상태 확인·최종 물리 검사·transaction 공개에는 사전 지정한 여유 시간 `publication_allowance_seconds`를 추가로 선반영한다.
3. 그 여유 시간 동안에도 공장 사건은 진행한다. 공개 시점의 최신 상태에서 다시 확인한다.
4. 마지막 구간의 실제 CPU 시간을 측정하고, 선반영한 여유 안에 들어왔는지 검사한다.
5. 초과하면 `TIMING_COVERAGE_VIOLATION`으로 실행 전체를 표시한다. 원시 기록·안전하게 정산된 상태는 보존하지만,
   이 실행을 정상 D1 근거로 사용하지 않는다. 초과 후 시간을 더해 이미 적용한 시각이 맞았다고 처리하지 않는다.

한 번의 수락 시도에서 충족할 조건은 다음과 같다.

```text
injected_delay = Σ 측정한 공개 전 계산 구간 + publication_allowance
measured_control_cost = Σ 동일 계산 구간 + 마지막 확인·공개 실제 시간
coverage_ok = 마지막 확인·공개 실제 시간 <= publication_allowance
coverage_ok인 경우 injected_delay >= measured_control_cost
```

실제 비용·선반영 여유·여유의 남은 양을 모두 기록한다. 같은 비교 block에는 같은 시간 규약과 여유를 적용한다.
위 식은 관측 이후의 제어 계산 비용 식이다. 그 전에 발생한 trigger 대기·eager 준비·backoff는 별도 sim 구간으로 기록한다.
마지막 공개 사건은 SimPy의 원자적 사건이다. 그 사건 내부에서 다른 공장 사건을 실행하지 않는다.
단순 dependency 비교만으로 충분하다고 가정하지 않는다. 현재 자원 용량·로봇·작업·충전 상태·경로를 확인해야 한다.
변경 불가능한 큰 모델 검증은 앞 단계에서 하고, 최신 물리 조건은 공개 단계에서도 검사한다.

이 설계는 상위 PRD의 실제 지연 포함 목표를 보수적으로 구현하는 구체화다.
정확한 실측 지연 방식과 결과를 합산하지 않는다. 여유 0초로 무조건 통과시키거나 미계측 잔여 비용을 숨기지 않는다.
M0에서 이 시간 계약을 작은 가짜 시계 예제로 먼저 검증한다. 구현이 이 계약을 지키지 못하면 M1 이후로 넘어가지 않는다.

### 4.4 시간 이동과 stale

- 원본 solve의 목적값·bound·hash는 요청 당시의 증거다. 적용 사본은 현재 시각의 OPTIMAL로 표시하지 않는다.
- RH의 대기·행동 시작·자원 구간·충전량·배터리 조건은 실제 적용 시각에 맞게 다시 확인한다.
- epoch·layout·로봇 instance·참여 작업 소유자·능력·필요 자원 변경은 의존 범위에 따라 폐기한다.
- 무관한 새 일반 작업이 들어왔다는 이유만으로 매번 전체 결과를 폐기하지 않는다.
  영향 있는 긴급 도착은 폐기/재계획 사유로 명시한다. 과거 제안에 새 작업을 몰래 추가하지 않는다.
- 적용 준비와 공개 사이의 변경도 검사한다. 준비 당시 검사가 통과했다는 사실만으로 적용하지 않는다.
- 재검증 시도 상한 이후에는 예산을 가진 대체 판단 또는 명시적 hold로 끝낸다. 무한 재검증하지 않는다.

## 5. 전체 정책의 공통 결정 계약

### 5.1 입력과 제안

`DecisionObservation`은 현재 관측 가능한 전체 작업·로봇·설비·점유·정책 상태의 불변 값이다.
살아 있는 engine/robot/resource/SimPy handle을 worker에 전달하지 않는다.
RH의 제한된 작업 후보 snapshot을 모든 정책의 관측으로 재사용하지 않는다.
관측은 공통이고, 각 정책의 후보 선정 범위는 정책별로 보존한다.
실제 미래 처리 표본·미도착 수요·미발생 고장은 입력에서 제외한다.

`DecisionProposal`은 request/epoch/generation/정책 버전·관측 hash·행동·의존 read-set·정책 상태의 변경 제안을 가진다.
행동은 배정, 충전 요청, 충전 목표 변경, 선점 요청, 대기, RH prefix를 구분한다.
실제 상태를 바꾸지 않고 JSON/pickle로 직렬화 가능해야 한다.

### 5.2 정책별 주의점

| 정책/경로 | 보존할 동작과 분리 대상 |
|---|---|
| baseline | 순번·FIFO·긴급 전용 배정·반응 충전. 순번 변경도 제안에 포함하고 폐기된 제안으로 순번을 진행시키지 않음 |
| optimized | 작업 정렬·점수·설비 선택·예측/기회 충전·critical 선점. task.station_id도 적용 전에 변경하지 않음 |
| legacy MILP | SciPy 배정 계산과 incumbent 검사, 기존 충전 규칙. 격리하되 후보·목적·solver 설정은 유지 |
| RH 네 정책 | 기존 계획기·독립 validator·prefix 실행 재사용, 공통 지연/적용 controller에 연결 |
| RH SafeFallback | 기존의 유휴 배정·도달 가능한 충전, 진행 중 prefix 비선점 의미를 유지하고 대체 비용도 지연 |
| 로봇 내부 충전 | should_charge·choose_charger·매 충전 step의 charge_target 판단을 누락 없이 공통 결정 경로로 연결 |

로봇의 긴급 방전 처리·물리 충전량 정산·교통 승인/안전 해제는 공통 plant의 진행이다.
정책이 새 목적지·충전 목표·선점 대상을 선택하는 계산과 구분한다.
충전 목표 변경 답이 오는 동안에는 마지막 승인 목표와 실제 용량을 넘겨 충전하지 않는다.
새 목표가 없으면 기존 승인 목표에서 안전하게 대기/종료한다. 정책 판단을 무료로 매 step 재실행하지 않는다.

### 5.3 요청과 적용 수명

엔진당 제안 계산은 한 번에 하나다. 계산 중 trigger는 최신 한 건으로 합치고 큐를 무한히 늘리지 않는다.
충전 관련 판단도 같은 제어 대기열에 넣는다. 물리 점유를 먼저 잡아 계산 대기열의 우선권으로 쓰지 않는다.
제안 계산 큐의 대기 시간도 시간 지표에 포함한다.
합친 trigger의 사유와 대상 로봇 집합은 보존한다. 마지막 로봇의 충전 요청이 앞선 작업 배정 요청을 지우지 않게 한다.
trigger 발생 시각과 실제 관측 시각은 구분하고, 최신 요청의 관측은 계산 시작 시점에 만든다.
관측하지 않은 대기 trigger를 이미 snapshot을 계산한 요청처럼 기록하지 않는다.

```text
OBSERVED → QUEUED → COMPUTING → PROPOSAL_READY
         → ADMISSION_PREPARING → PUBLICATION_WAIT
         → ACCEPTED / REJECTED / CANCELLED / PENDING_AT_END
REJECTED → FALLBACK_COMPUTING → 동일한 검증·공개 절차 또는 HOLD
reset/close → epoch 무효화 → pending 폐기 → worker·요청 회수
```

transaction은 중복 로봇·작업·계획 소유자를 사전에 검사한다. 원자적 적용이 어려운 행동은 순서 있는 명시적 부분 적용 receipt를 남긴다.
부분 적용을 전체 성공으로 표시하지 않으며 미적용 작업은 장부상 대기 상태를 유지한다.
폐기·실패·rollback 후 orphan 예약·incoming 충전자·제거된 작업이 남으면 실패다.

## 6. worker 준비·운영·재시작

### 6.1 준비 완료의 정의

프로세스가 살아 있다는 사실과 계산 준비가 끝났다는 사실을 구분한다.
준비 RPC는 실제 사용하는 정책/backend 의존성을 import하고 필요한 객체를 생성한 뒤 버전·능력·세대 정보를 ACK한다.
다른 backend를 켜거나 수요를 미리 풀지 않는다. 생성만 한 경우와 첫 solve까지 한 경우를 서로 다른 bootstrap_kind로 기록한다.
이번 기본 준비는 import·backend 생성까지이며 실제 공장 입력에 대한 solve를 준비 단계로 숨기지 않는다.

heuristic·legacy도 D1에서는 불변 입력을 받는 격리된 정책 worker를 사용한다.
모든 정책에 같은 프로세스 수명·queue·상한 규칙을 적용하되 실제로 쓰는 의존성만 import한다.
D0의 동기 경로는 유지한다. 현재 RH의 격리/감시 구현을 확장하고 별도 중복 supervisor를 만들지 않는다.

```text
STOPPED → STARTING → IMPORTING → READY → BUSY → READY
                         └ 실패/상한 → UNAVAILABLE
READY/BUSY → crash/RSS/hard limit → REAPING → BACKOFF → STARTING
재시도 상한 → DISABLED_FOR_RUN → fallback 또는 hold
모든 상태 → close → CLOSED
```

ACK/응답에는 worker_session_id·generation·request_id·engine epoch를 넣는다.
PID 재사용, 이전 세대의 늦은 응답, transport 오류를 정상 결과로 오인하지 않는다.
한 engine은 최대 한 정책 worker만 소유한다. 회수 완료 전 새 worker를 중복 생성하지 않는다.

### 6.2 시작 모드

| 모드 | 시간 처리 | 비교 용도 |
|---|---|---|
| on_demand | 첫 유효 결정에서 시작. 준비 비용은 그 요청의 cold 지연과 deadline에 포함 | 실제 초기화 비용 확인 |
| eager | sim t=0부터 미리 준비. 준비 시간만큼 공장은 진행하며 그동안 도착한 작업은 보존 | 준비된 운영 상태 검토 |

eager에서도 준비 시간·그 기간의 backlog·미처리 시간을 삭제하지 않는다.
초기 준비 완료 시각을 정책별로 측정창 시작으로 삼지 않는다. paired 실험의 공통 측정창은 입력에 미리 고정한다.
준비 완료 이전에는 정책 배정을 적용하지 않는다. 안전한 plant 동작은 계속된다.
준비 실패 시 bounded fallback을 허용하되 그 판단 지연과 출처를 기록한다.
재시작은 eager run에서도 운영 중 cold recovery 비용으로 다시 기록한다.

### 6.3 예산과 실패

- startup 상한, 정책 제안 soft 예산, 부모 hard watchdog, 종단간 SLA, fallback 예산, run/suite 예산을 분리한다.
- on_demand에서 readiness만으로 종단간 예산을 썼다면 남은 예산을 검사한다.
  새 solve 예산을 무료로 추가하지 않는다. 준비를 끝내 다음 요청에 재사용하더라도 첫 요청의 실패는 보존한다.
- ready worker의 backend timeout은 기존 의미를 보존한다. worker 초기화를 옮겼다는 이유로 같은 설정에서 solve 시간을 몰래 늘리지 않는다.
- hard watchdog은 IPC 송신·응답과 native hang도 포함한다. RSS는 준비·운영 모두 감시하고 sample peak임을 표시한다.
- 준비 실패는 STARTUP_TIMEOUT/STARTUP_MEMORY_LIMIT/STARTUP_UNAVAILABLE/STARTUP_TRANSPORT_ERROR로 구분한다.
- 운영 중 SOLVE_TIMEOUT/HARD_TIMEOUT/MEMORY_LIMIT/WORKER_CRASH/STALE_GENERATION도 구분한다.
  원래 solver 상태와 상위 실패 분류는 별도 필드다.
- max restart와 sim-time backoff를 고정한다. 실패마다 재시작하며 공장을 멈추거나 CPU를 무한 소모하지 않는다.
- fallback 실행 실패·예산 초과 시 명시적 HOLD, 이후 trigger에서 재시도한다.
- close/reset/한도 종료 후 자식 프로세스·RPC thread·connection·지연 적용 process가 남으면 실패다.

## 7. 기능 요구사항과 수용 기준

| ID | 요구사항 | 통과 기준 | 구현 단계 / 검사 |
|---|---|---|---|
| D-01 | 전체 정책의 부작용 없는 관측/제안 | 제안 전후 물리·작업·정책 상태 불변, 미래 입력 미포함 | M1 / T-01 |
| D-02 | 기존 정책 의미 보존 | D0 차등 검사에서 선택·순번·충전·선점·물리 digest 일치 또는 명시적 버그 수정 근거 | M1 / T-02 |
| D-03 | 전체 판단 경로 연결 | 긴급 배정·저SOC·기회 충전·충전 목표 변경·fallback의 우회 호출 0 | M1,M3 / T-07,T-08 |
| D-04 | 측정 비용의 공장 지연 | 가짜 시계로 계산한 적용 시각 일치, 지연 중 도착·이동·처리 진행 | M0,M3 / T-03,T-04 |
| D-05 | 적용 준비와 공개 분리 | 공개 전 큐 제거/배정/예약 0, 공개 사이 변경 검출 | M0,M3 / T-01,T-05 |
| D-06 | 공개 비용의 보수적 포함 | 실제 마지막 비용≤선반영 여유, 초과는 timing violation으로 보존 | M0,M3 / T-03,T-09 |
| D-07 | stale·시간 이동 검증 | 취소·고장·SOC·경로·epoch 변화에 잘못된 적용 0, 무관 도착 처리 명세 일치 | M3 / T-04,T-05,T-06 |
| D-08 | 중복·부분 적용 제어 | 중복 owner 0, 부분 적용 receipt와 남은 작업 상태 일치 | M3 / T-06 |
| D-09 | 실패 대체 지연 | fallback 계산·검증·공개 비용 반영, 무한 재검증/재시도 0 | M3,M4 / T-08,T-14 |
| D-10 | 모든 정책 혼합 D1 suite | 7개 정책에 같은 시간/시작 규약 적용, D0/D1 혼합 비교는 거부 | M5 / T-15 |
| W-01 | readiness handshake | 실제 선택 backend 준비 ACK 이전 solve·적용 0 | M2 / T-10 |
| W-02 | cold/eager 비용 공개 | 초기 준비·backlog·재시작 비용과 첫 요청 비용 확인 가능 | M4 / T-11,T-12 |
| W-03 | 준비·운영 상한 분리 | 강제 hang·OOM·transport 오류가 정해진 상한 안에 종료·분류 | M2,M4 / T-10,T-12,T-13 |
| W-04 | bounded 재시작·세대 검사 | 재시도 상한 준수, 이전 응답 적용 0, 동시 owned worker≤1 | M4 / T-12,T-13 |
| W-05 | 종료 자원 회수 | 정상·오류·reset·run timeout 뒤 orphan 자원 0 | M2,M4 / T-13,T-14 |
| E-01 | 새 결과·설정 schema | 구버전 prototype을 완전 D1으로 재분류하지 않음, 설정 충돌 명시 거부 | M5 / T-15 |
| E-02 | 전체 비용·실패 기록 | 없는 시간은 unknown, 실패·미시작·pending·coverage violation 구분 | M5 / T-09,T-10,T-14,T-16 |
| E-03 | 기록 기반 replay | native 재계산 없이 결정·지연·상태 전이 재사용, 물리 digest/장부 일치 | M5 / T-16 |
| E-04 | 같은 문제의 성능 비교 | model/spec 범위·입력·예산 고정, cold/warm/restart 및 D0/D1 별도 집계 | M6 / T-17,T-18 |

## 8. 설정·결과·replay 계약

### 8.1 설정

기존 RH 수학 설정과 별도로 `DecisionSettings`를 둔다. 비 RH 정책에도 전달한다.
CLI는 신규 `--decision-settings`로 읽고 기존 `--rh-settings`와 책임을 분리한다.
동일 시간 모드가 서로 다르게 지정되면 명시적으로 거부한다.
이전 RH `timing_mode=D1`은 prototype임을 보존하고 새 전체 정책 모드로 조용히 변환하지 않는다.

| 설정 | 규약 |
|---|---|
| timing_protocol | D0 또는 신규 d1-staged-budget-v1. 구버전 prototype과 별도 |
| startup_mode | on_demand 또는 eager |
| startup_timeout_seconds | 독립 준비 상한. 초기 검수 제안 10초 |
| end_to_end_soft_deadline_seconds | 관측부터 실제 판단 적용까지의 SLA. 초과 여부는 전체 비용 기준 |
| publication_allowance_seconds | 마지막 최신 검사·공개에 선반영하는 비용. M0에서 고정 |
| max_admission_attempts | 초기 검수 제안 2회. 매 시도 시간 기록 |
| max_worker_restarts | 초기 검수 제안 run당 2회. 최초 생성과 구분 |
| restart_backoff_seconds | 초기 검수 제안 0.1초, 1.0초. 공장 가상 시간으로 반영 |
| fallback_budget_seconds | 대체 계산 상한. 초기 검수 제안 0.1초 |

숫자는 개발 검수의 제안값이다. 공장 권장값이나 성능 보장이 아니다.
publication 여유는 임의 숫자로 성능을 맞추지 않는다. M0 측정과 경계 검사로 선택하고 최종 비교 전에 동결한다.
현재 timeout/watchdog/RSS/모델 한도와 새 예산의 포함 관계를 설정 문서에 표로 남긴다.

### 8.2 결과

headless 결과는 새 v5로 구분한다. 결정 기록은 `clover-decision-record-v1`, worker 수명은 `clover-worker-lifecycle-v1`로 제안한다.
기존 RH 모델의 수학 의미를 바꾸지 않는 경우 모델 버전을 불필요하게 올리지 않는다.
공통 결정 입력·시간 규약은 별도 버전/hash를 가진다.

필수 기록:

- identity: source/정책/모델/시간 규약/설정 hash, request·epoch·worker session·generation.
- 시각: triggered/queued/observed/proposal_ready/admission_started/publication_due/applied_or_rejected/fallback_requested.
- 비용: startup/capture/IPC/route/build/compile/solve/decode/validation/staging/fallback, 공개 실제 비용·선반영 여유.
  queue_sim_wait_seconds와 ipc_queue_wall_seconds는 별도 필드로 보존한다.
- coverage: raw measured cost, injected delay, coverage_ok, 누락/초과 단계, soft/hard deadline 사유.
- 행동: 원본 제안·적용 행동·의존 조건·정책 상태 변경·부분 적용·실패 대체의 출처.
- 종료: pending 단계·미처리 결정·restart 수·자식 회수 결과·전체 실행 실패 사유.

요청 단위 분모와 실행 단위 분모를 구분한다. fallback 실행의 결과도 해당 원 정책 성과에 포함한다.
구버전의 import=0을 실측 0으로 추정하지 않는다. 초기화·재시작은 결정 로그와 겹칠 수 있으므로 수명 합계와 결정 시간을 중복 합산하지 않는다.

### 8.3 replay

기록된 제안·지연·준비/재시작 시각·상태 전이·fallback을 사용한다. replay에서 worker를 다시 켜 실측 시간을 재생성하지 않는다.
검증기·실제 물리 실행·공개 read-set·소유권 검사는 다시 수행한다.
미사용 결정, 과거 세대 응답 수락, 원본/적용 hash 불일치는 실패다.
미완료 시뮬레이션의 pending 기록을 무조건 적용 완료로 바꾸지 않는다.
구버전 실행은 구버전 소스에서 replay한다. 마이그레이션 없는 v4 결과는 새 D1 검증 증거로 쓰지 않는다.

## 9. 변경할 코드 경계

현재 파일을 재사용하면서 다음 책임을 분리한다. 신규 모듈명은 `simulation/decisions/` 패키지 아래로 제안한다.

| 현재 경계 | 변경 책임 |
|---|---|
| [scheduler.py](../robot_factory_sim/simulation/scheduler.py) | 순수 선택 함수와 D0 adapter, 정책 상태 변경 제안 |
| [milp_scheduler.py](../robot_factory_sim/simulation/milp_scheduler.py) | legacy 입력/출력 값 분리, SciPy worker 계산 |
| [planning/controller.py](../robot_factory_sim/simulation/planning/controller.py) | RH adapter, 중복 D1 수명 로직을 공통 controller로 이동 |
| [planning/process.py](../robot_factory_sim/simulation/planning/process.py) | 기존 supervisor 확장, READY·generation·초기화·재시작 RPC |
| [planning/execution.py](../robot_factory_sim/simulation/planning/execution.py) | RH transaction 준비·최신 검증·원자적 공개 연결 |
| [engine.py](../robot_factory_sim/simulation/engine.py) | 공통 decision settings 전달, trigger·epoch·close·상태 변경 경계 |
| [robot.py](../robot_factory_sim/simulation/robot.py) | 충전 판단 우회 제거, 승인된 intent 실행, 실제 에너지 정산 보존 |
| [evaluation/runner.py](../robot_factory_sim/simulation/evaluation/runner.py) | 전체 정책 D1·v5·replay·실패 분류 |
| evaluation CLI/profiling | 결정 설정 입력, 시간 규약·시작 모드·세대별 집계 |

신규 decisions 패키지는 계약/관측, 순수 정책 adapter, 시간 controller, admission transaction, replay로 나눈다.
contracts는 가벼운 값 타입만 두고 GUI·SciPy·OR-Tools·살아 있는 engine을 import하지 않는다.
engine·robot의 타입 참조는 TYPE_CHECKING으로 분리하고 순환 import 검사를 한다.
임시 산출물·실험 로그는 전달 소스 밖 work/outputs에 보관한다.

## 10. 구현 순서와 중간 완료 게이트

모든 항목의 현재 구현 상태는 **미착수**다. 이전 마일스톤의 테스트 성공은 이 표의 완료를 대신하지 않는다.

| 단계 | 작업 | 통과 후 다음 단계 |
|---|---|---|
| M0 | 시간 계약·transaction 최소 증명, baseline 원본 상태 동결, 예산 포함 관계 확정 | 가짜 시계로 마지막 공개 비용·중간 변경·overrun 검출 증명 |
| M1 | 공통 관측/제안, baseline·optimized·legacy의 순수 선택 분리 | D0 차등 회귀, 제안 부작용 0, 충전/선점 경로 목록 검수 |
| M2 | 공통 supervisor 준비/세대/종료, 정책별 준비 RPC | 준비 hang·RSS·import 실패·late reply·close 검사 |
| M3 | 공통 D1 controller·transaction·RH adapter·충전 경로 연결 | 일곱 정책의 단계별 시각 검사, fallback 지연·상태 보존 |
| M4 | on_demand/eager·재시작/backoff·예산 처리 | 중간 crash·재시작 초과·준비 중 수요·대체 실패 검사 |
| M5 | v5 결과·profile·기록 replay·CLI | schema/hash/경로·설정 충돌 검사, 전체 기록 재생 |
| M6 | 실제 host 회귀·cold/warm/restart 예산 실험·전달물 검수 | 아래 테스트/실험 기준 충족, 남는 지원 제한을 공개 |

M0에서 시간 계약이 실패하면 실제 로봇 배정 코드를 먼저 바꾸지 않는다.
M1의 D0 차등 회귀에서 정책이 달라지면 D1 성능 실험으로 넘어가기 전에 원인을 해결한다.
M3와 M4는 각각 1번·2번의 핵심 구현 단계지만 최종 완료는 M6 검수까지다.

## 11. 필수 테스트

sleep의 실제 길이에 의존하는 assertion 대신 제어 가능한 시계·worker 응답을 주입한다.
실제 subprocess의 hang/RSS/회수 검사는 별도 통합 검사로 둔다.

| ID | 반례/검사 | 기대 결과 |
|---|---|---|
| T-01 | 일곱 정책의 관측/제안 전후 상태 비교 | 큐·task.station·owner·순번·충전 상태·예약이 변하지 않음 |
| T-02 | D0 원본과 분리된 정책의 차등 실행 | 공동 배정·FIFO·긴급·저SOC·기회 충전·동점의 선택/물리 결과 보존 |
| T-03 | 가짜 시계로 capture/compute/validate/stage 비용 주입 | 정확한 단계별 반환·공개 시각, 비용 이중 합산 0 |
| T-04 | 계산·공개 대기 중 도착/취소/작업 완료 | 공장은 진행, 취소 작업 실행 0, 영향 없는 작업 도착은 명세대로 처리 |
| T-05 | 공개 직전 로봇/SOC/설비/통로/캐빈 변경 | 최신 검사로 거부하거나 안전하게 시간 이동, 유령 예약 0 |
| T-06 | transaction 일부 실패/중복 작업/오래된 epoch | rollback 또는 명시적 부분 적용, 장부·owner 보존 |
| T-07 | 충전기 선택·목표 갱신·critical 선점 | D1 우회 행동 0, 승인 목표 초과 충전 0 |
| T-08 | solver 실패 → fallback 계산·검증 → hold | 모든 판단 지연 반영, fallback 예산·재검증 상한 준수 |
| T-09 | 마지막 공개 비용이 allowance 초과 | TIMING_COVERAGE_VIOLATION, 정상 D1으로 표시하지 않음 |
| T-10 | READY 전 solve·backend import 실패·준비 hang/RSS | 조기 적용 0, 준비 원인별 실패, 상한 내 회수 |
| T-11 | on_demand와 eager 준비 중 작업 도착 | cold 지연/초기 backlog 보존, 공통 측정창 유지 |
| T-12 | 운영 중 crash/hang → 재시작 → 정상 응답 | 세대 증가·backoff·비용 기록, 이전 응답 폐기 |
| T-13 | 반복 crash·broken pipe·close/reset/engine timeout | 재시작 상한, orphan process/thread/connection/pending 적용 0 |
| T-14 | 종료 시 계산/검증/준비/공개 대기 중 | 정확한 pending 단계와 미완료 기록, 사후 적용 0 |
| T-15 | 7정책 D1 suite·설정 충돌·구버전 입력 | 일관된 신규 규약 실행, 혼합/충돌/미지원 schema 명시 거부 |
| T-16 | 모든 결과의 기록 replay와 raw analyze | 물리 digest·작업/에너지/자원 장부·지표 재구성 일치 |
| T-17 | 정확한 같은 문제에서 cold/warm 설정 비교 | 수학적 spec·후보 범위 유지, 시간에 의한 stale 차이는 별도 기록 |
| T-18 | 실제 host의 전체 회귀·import·의존성 | Python 3.11/3.14 통과, GUI/DB 기본 동작 보존, 핵심 headless 선택 의존성 분리 |

legacy의 시간제한 solve는 서로 다른 실행에서 incumbent가 달라질 수 있다.
T-02의 정확한 차등 검사는 작은 증명 가능한 입력 또는 고정 solver 응답을 사용한다.
그 외 실측 결과 차이를 숨기지 않는다. replay는 solver를 다시 실행해 일치를 기대하는 방식으로 만들지 않는다.

## 12. 예비 시뮬레이션과 비용 실험

팀원의 본 실험을 기다리지 않고 기존의 공동 배정·진행 중 캐빈·충전 경쟁 fixture로 기능을 검수한다.
기존 물리 검수 입력도 유지한다. 새로운 fixture가 필요하면 검증할 실패 기전을 이름과 함께 남긴다.

실측 microbenchmark는 세 정책군의 대표가 아니라 일곱 실제 정책을 대상으로 한다.
최소 기능 조합은 3 fixture × 7 정책 × 2 시작 모드 × 2 seed = **84회**다.
이 숫자는 기능 실행 수이며 통계 표본 수가 아니다. 비용과 runtime을 보고 더 작은 시작 fixture부터 진행한다.
강제 준비 실패·운영 crash·재시작은 별도 fault suite에 넣고 정상 성능 평균과 섞지 않는다.

예산 민감도는 0.5/2/10초를 개발 제안 수준으로 사용한다. 같은 block은 같은 물리 입력·모델 범위·예산·시간 규약을 쓴다.
RH H·격자·native 해상도·방문 상한은 이 실험에서 고정한다.
준비 속도를 보여주려고 작업 수·방문 상한을 줄이지 않는다.

비용 확인은 프로세스 cold 생성·첫 요청·같은 worker 후속 요청·재시작 후 첫 요청을 분리한다.
순서 영향을 줄여 교대 실행하고 독립 세션을 반복한다. pilot 후 반복 수를 고정하며 정상/실패 표본을 모두 보존한다.
wall 비용 측정 중 solver 실험을 동시에 돌려 CPU 경합을 만들지 않는다.

수집 지표: startup P50/P95/P99·실패 원인, 전체 결정/각 단계 비용, 공개 여유·coverage 위반,
종단간 deadline miss, cold/warm/restart 수, RSS sample peak, fallback/stale/hold, 실제 완료·미완료·납기·대기·에너지.
P99의 표본 수를 함께 공개하고 작은 표본의 분위수를 운영 보장으로 부르지 않는다.

비교는 두 층으로 나눈다.

1. 같은 고정 snapshot의 **계산 비교**: 문제 의미·최적값·준비 전후 비용. 공장 지연과 분리.
2. 공장이 진행하는 **D1 실행 비교**: 도착/초기 상태만 같고 이후 상태는 정책·지연에 따라 달라질 수 있음.

D1의 다른 시점에 얻은 snapshot hash가 다르다는 이유로 실험 전체를 잘못됐다고 판정하지 않는다.
반대로 같은 최적값이 나왔다는 이유로 실제 공장 완료량도 같다고 주장하지 않는다.
0.5초 cold timeout이 남아도 기록·상한·대체가 정확하면 기능 완료일 수 있다. 개선 크기는 실측 그대로 보고한다.

## 13. 최종 완료와 전달물

1번 완료 조건: 일곱 정책의 판단 경로 전체가 동일한 신규 D1 규약을 따르고,
검증/적용 준비/fallback 비용을 반영하며, 지원 입력 검수에서 coverage 위반 0·소유권/보존 위반 0을 확인한다.
남는 우회 경로가 있으면 해당 정책을 D1 지원 완료로 표시하지 않는다.

2번 완료 조건: 준비 완료 ACK·두 시작 모드·bounded 재시작·세대별 응답 검사·종료 회수·원인별 비용 기록이 동작한다.
초기화 제거로 solver 해 품질이나 운영 SLA가 좋아졌다는 주장은 별도 실측 근거가 있을 때만 한다.

공통 완료 조건: 기존 D0 회귀, T-01~T-18, 신규 결과의 replay/raw analyze,
실패 표본 포함 실험 기록, source/settings/input hash, 패치 적용·압축 무결성 검수 통과다.
재현되지 않는 실측 timing을 강제로 원본처럼 만들어 통과시키지 않는다.

전달물:

- 새로운 독립 소스 묶음과 이전 v1.9 대비 패치, 원본 SHA 보존 증거.
- D0·D1/cold·D1/eager 예제 설정, 새 CLI 사용법, 비용 포함 관계 표.
- 정책별 지원 범위와 충전/선점/대체 판단의 경로 점검표.
- 테스트 결과·원시 run·worker 수명 로그·결정 로그·replay/지표 검수 결과.
- 단계별 완료표와 short-budget의 남은 한계를 포함한 구현 검수 보고서.

이 작업이 끝나도 팀원 시나리오의 본 실험과 환경별 최종 추천은 별도 남는다.
이번 PRD 작성 단계의 산출물은 본 문서와 문서 검수 기록이며, 실제 구현·새 성능 결과는 아직 없다.
