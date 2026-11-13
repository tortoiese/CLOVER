# 자유 시작시각 CP-SAT: 선택 구간과 로봇 행동 순서

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

2026-11-12 · core `common-model-v1.9-native-profile`.
새 정책 ID는 `rh_cpsat_native`다. 기존 `rh_cpsat_interval`은 고정 격자 arc 선택 구현으로 유지한다.
기본 optimized·가중치·GUI는 바꾸지 않았다.

## 표현이 달라진 부분

기존 grid는 시작 tick마다 별도 행동 arc를 만든다. native는 로봇별 행동 순서의 각 자리에
시작·종료·위치·배터리 정수 변수를 두고 기하 경로/작업/충전 대안을 선택한다.
행동마다 가능한 시작 tick을 별도 arc로 나열하지 않는다.
CP-SAT의 시간은 정수다. 여기서 자유 시작은 선택한 해상도의 정수 domain 안에서 자유롭다는 의미다.
기본 native_step=0.1초이며 무한 정밀 실수 최적화는 아니다.

```text
p[r,n,a] ∈ {0,1}, sum_a p[r,n,a] = active[r,n]
active[r,n+1] <= active[r,n]
S[r,n] >= E[r,n-1], active이면 S[r,n] < H
선택 시 직전 위치 = a.origin, 다음 위치 = a.destination
서비스: E = S + route_ticks + work_ticks
충전:   E = S + route_ticks + charge_units
B_next = B_previous - sum(p * energy) + gain
B_previous >= sum(p * energy), 0 <= B_next <= capacity
작업별 sum(service presence) + defer = 1
```

서비스는 계속 이동+작업의 복합 선택이다. 미선택 자리는 직전 위치/종료/SOC를 그대로 잇는다.
행동 사이 빈 시간은 실제 결과의 wait arc로 복원하고 공유 executor가 첫 prefix만 실행한다.
서비스 후/terminal의 예비량과 충전소 복귀 에너지, 초기 안전 hold, nominal 시간·sigma 의미도 유지한다.

## 자원과 캐빈

로봇 행동, 설비 작업과 충전, 통로/교차로/캐빈에 presence가 있는 optional interval을 사용한다.
로봇/단일 슬롯은 NoOverlap, 여러 충전 슬롯과 정수 mm 통로 폭은 Cumulative다.
통제 구간의 모든 자원은 같은 진입~후미 해제 구간을 사용한다.
현재 actual/prefix/future 의무는 기존 자원 달력에서 고정 차단 interval로 가져온다.
시작 내림·종료 올림의 보수적 변환은 선택한 시간 해상도에서 적용한다.

캐빈의 각 선택 운행은 같은 행동 presence를 쓰는 circuit node이기도 하다.
직전 운행의 목적층과 다음 출발층이 같을 때만 연결하고 시간 선후를 제약한다.
depot의 최초 층/시각은 현재 가용 상태 또는 증명된 보호 운행의 하차 시각/층이다.
미선택 node는 자기 loop, 요청 없는 빈 회송/순간이동은 없다. 승객 capacity=1을 유지한다.
실제 실행에서도 자원 승인과 source_floor 재검증을 거친다.

## 충전은 가변 길이지만 기존 quantum 의미를 보존한다

q = floor(rate * time_unit / 0.001)로 단위 시간당 가용 충전량을 정한다.
N개 quantum을 한 interval로 합치고 0 <= gain <= q*N을 둔다.
이동은 첫 quantum에만 포함된다. 중간 배터리는 증가만 하므로 최종 상한으로 용량 초과를 막을 수 있다.
N>1이면 충전 종료가 H 이하여야 한다. H 이후 시작하는 추가 quantum을 허용하지 않는다.
이동 후 첫 quantum 하나가 tail에 끝나는 경우는 기존 복합 충전 arc와 같이 허용한다.
같은 해상도의 grid에서는 이 충전을 N개의 연속 충전으로 다시 나눌 수 있다.
실제 executor는 기존과 같이 확정 gain/rate만큼 충전하고 실제 점유를 반영한다.

## 행동 개수: 전체 domain과 명시적 제한 domain

native_actions=0은 전체 domain이다. 단위 시간당 최소 한 tick을 쓰므로 H 안에서 새로 시작하는
의미 있는 행동 수의 상한은 floor(H/time_unit)이다. 충전 aggregate도 이를 넘길 필요가 없다.
이 상한만큼 자리를 생성하면 해당 해상도의 grid 행동 경로를 표현할 수 있다.
변수/대안/circuit edge가 많으면 max_arcs·max_cabin_states·deadline·watchdog/RSS 한도로 명시적으로 실패한다.
시간 해상도가 세밀할수록 전체 domain은 커진다. 조용히 행동 수를 잘라 최적이라고 표시하지 않는다.

native_actions=4처럼 양수를 주면 로봇당 최대 4회의 서비스/충전 방문만 허용한다.
가변 길이 충전 interval 하나는 한 방문이다. 이는 명시적인 제한 모델이다.
보고서 action_scope와 semantic_spec_hash에 제한을 포함한다. 그 최적값은 제한 domain의 최적값이다.
native_bounded4.json은 이 설정의 기능/기전 파일럿이며 전체 grid와 같은 문제의 solver 성능 비교가 아니다.

## 목적과 동등성 확인

1차는 기존 weighted defer다. 2차의 대기·지각·거리·충전·충전량·종료 tie 비용도 기존 물리 가중치를 유지한다.
native 단위 u가 기준 grid step Δ를 나누므로 scale=Δ/u는 정수다.
물리 비용을 정수식으로 만들기 위해 2차 목적을 scale배하고 종료 tick tie를 대응시킨다.
Report.secondary_scale로 배율을 보존한다. 해상도가 다를 때 raw 2차 숫자만 비교하지 않는다.

semantic_spec_hash는 입력·route·시간 해상도·목적 기준·전체/제한 domain을 식별한다.
model_hash/semantic_hash는 실제 모델 표현을 식별한다.
native_step=step, native_actions=0인 작은 문제에서는 세 grid compiler와 native의
공통 의미 해시와 두 목적 최적값을 비교한다. 서로 다른 model_hash가 나오는 것은 정상이다.
해상도를 더 세밀하게 하거나 방문 수를 제한하면 공통 의미 해시도 달라진다.

작은 동등성 검사에는 단순 배정·좁은 통로·교차로·진행 중 캐빈·가변 충전·지각 비용을 포함한다.
독립 validator도 행동 연속성·자원·에너지·캐빈 층·해상도·방문 제한을 검사한다.
작은 표본의 동등성이 모든 크기/수치 입력을 전수 증명했다는 의미는 아니다.

## 예산·실패·재현

기존 별도 spawn worker와 부모의 watchdog/RSS 상한을 사용한다.
준비·import·모델 구성·두 단계 solve·검증을 같은 전체 요청 예산 안에서 수행한다.
1차가 증명된 경우에만 정확한 값을 고정하고, 미증명 incumbent는 악화하지 않는 상한을 둔다.
2차 예외/timeout이면 검증된 1차 해를 보존한다. 전체 모델을 구성하지 못하면 부분 행동을 실행하지 않는다.
BUILD_LIMIT/NO_INCUMBENT_TIMEOUT/MODEL_INVALID/UNAVAILABLE와 실제 fallback을 기록한다.
native OPTIMAL은 표시된 해상도/domain의 snapshot 최적값이다. 전체 운영기간의 최적성/처리량 우세가 아니다.

관측 schema v6·계획/모델 v8·headless 결과 v4다. 이전 계획을 자동 변환하지 않는다.
replay는 저장된 결정을 공유 물리 모델에서 재검증·실행하며 native solver를 다시 돌리는 검사가 아니다.
D1은 다른 RH와 같은 요청 지연 시범이다. 적용 비용까지 포함한 전체 정책 D1은 남아 있다.

## 실행

robot_factory_sim에서 requirements-planning.txt를 설치한다. 기존 배정 MILP 비교에는 requirements-legacy.txt도 필요하다.
다음 예제의 native는 0.1초/4방문 제한이고 grid는 step=2초다.

```bash
python -m simulation.evaluation suite \
  --scenario examples/headless/inflight_cabin_batch.json \
  --policies optimized rh_milp_grid rh_cpsat_interval rh_cpsat_native \
  --rh-settings examples/planning/native_bounded4.json --seeds 1001 1002 1003 \
  --output /tmp/clover-native-fine
```

native_coarse_complete.json은 H=8초·native_step=step=2초·전체 행동 domain 설정이다.
같은 unit에서의 모델 표현 차이 검사와 fine/제한 domain의 운영 비교를 구분한다.
기본 정책 변경, 본 실험용 설정 확정, 실제 6대 시나리오의 held-out 추천은 수행하지 않았다.
