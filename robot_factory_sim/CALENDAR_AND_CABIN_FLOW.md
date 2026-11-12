# 소유자별 자원 달력과 여러 운행의 캐빈 상태 경로

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

2026-11-12 · `common-model-v1.9-native-profile`.
기본 `optimized`, 휴리스틱 가중치와 GUI는 유지한다. 세 RH backend가 같은 수학적 의미를 사용한다.

## 1. 자원 달력

이전 방식은 현재 점유와 확정 prefix의 전체 footprint를 구간 전체에서 차감했다.
통로를 이미 통과하고 설비에서 작업 중인 경우에도 과거 통로·캐빈 예약이 남을 수 있었다.
실제 충전 슬롯과 같은 소유자의 prefix 슬롯도 중복 차감할 수 있었다.

`Snapshot.holds`는 resource·소유자 instance·수요·단위·절대 시작/해제 시각·출처를 기록한다.
실제 claim은 합산하고, 같은 소유자의 실제 점유와 계획 의무를 겹쳐 두 번 차감하지 않는다.

```text
owner_load[r,k,t] = max(sum(actual claims), max(prefix/future obligations))
residual[k,t] = max(0, capacity[k] - sum(owner_load[r,k,t]))
sum(new action demands[k,t] * x) <= residual[k,t]
```

다른 소유자의 점유는 합산한다. 요청된 대기자도 앞으로 슬롯을 사용할 의무로 남긴다.
따라서 앞 충전의 종료가 알려져도 기존 대기자가 있으면 새 작업에 슬롯을 즉시 준다고 가정하지 않는다.
알 수 없는 외부 점유·대기는 집계 count의 부족분을 opaque 소유자로 보완한다.
drain 중 현재 점유가 새 용량보다 많아도 기존 로봇을 지우지 않고 잔여량 0으로 다룬다.

현재 확실히 기록하는 실제 해제 상한은 다음과 같다.

- 이미 이동을 마친 통로의 마지막 protected tail 대기: 정해진 deadline 뒤 해제.
- 시작한 RH 정량 충전: 확정 gain/rate의 timeout 뒤 해제.
- 실제 탑승 후 보호된 캐빈 운행: 고정된 이동·하차 문 timeout 뒤 해제와 도착층.

실제 작업시간 표본, 앞으로의 대기, 일반 휴리스틱 충전 종료를 미리 읽거나 추정해 확정 시각으로 쓰지 않는다.
해제를 모르면 end=null이며 **이번 관측 모델에서는** 계속 차단한다. 다음 관측에서 실제 해제를 반영한다.
실제 구간 완료를 해당 prefix의 epoch/token/시각/행동과 segment index로 식별해 지난 구간 의무를 제거한다.
진행 중 구간은 후미 해제 전까지 점유한다. 상세 증명·실행 경계는 `INFLIGHT_CALENDAR.md`를 참고한다.

시작은 tick 내림, 알려진 해제는 tick 올림으로 변환한다. 반열림 `[start,end)`를 사용한다.
MILP/CP linear는 tick별 residual 행, CP interval은 같은 residual을 만드는 고정 차단 interval을 사용한다.
독립 validator는 행동 시작/끝뿐 아니라 용량이 바뀌는 시각도 검사한다.
고정 의무만으로 불가능한 행동은 생성 전에 제외해 불필요한 변수·tail 크기를 줄인다.

## 2. 캐빈은 시간뿐 아니라 층도 이어져야 한다

NoOverlap만으로는 앞 운행이 2층에서 끝난 뒤 다음 운행이 1층에서 갑자기 시작하는 오류를 막지 못한다.
각 기하 경로에 대해 가능한 캐빈 출발 상태별 변형을 만들고, 빈 회송·승차·탑승 이동·하차 시간을 함께 계산한다.
기하 경로 자체는 기존 K=1이다. 서로 다른 공간 경로를 탐색하는 기능을 추가한 것은 아니다.

로봇의 선택된 운행은 캐빈 상태 `(출발층, 시작 tick)`에서 `(목적층, 종료 tick)`으로 가는 arc이기도 하다.
캐빈의 idle arc는 같은 층에서 다음 tick으로만 이동한다.

```text
각 cabin/floor/tick: outgoing(idle + selected trips) - incoming(...) = initial supply or terminal sink
관측된 초기 층의 supply = 1, 나머지 supply = 0, terminal 총 sink = 1
```

운행 selector는 로봇 행동의 같은 x를 사용한다. 요청 없는 무료 회송이나 순간이동은 없다.
각 캐빈은 독립적으로 한 경로를 따르며 승객 capacity는 계속 1이다.
한 horizon에서 여러 운행을 허용하고, 명목상 겹치지 않는 시간과 층 연속성을 같이 지킨다.
0초 운행도 grid에서는 최소 1 tick의 보수적 상태 전이를 사용한다.

`joint_cabins=false`는 기존 캐빈별 한 compound 운행 제한의 비교 옵션이다.
두 표현은 가능한 일정이 다르므로 semantic hash가 달라지는 것이 맞다.
같은 설정의 MILP와 CP linear는 같은 canonical hash이고, CP interval은 같은 semantic hash의 별도 표현이다.

## 3. 실제 실행과 실패

모든 실제 자원은 기존 SimPy/PR9 arbiter가 진입 시 확보하고 안전 경계에서 해제한다.
캐빈 슬롯을 얻었을 때 계획한 source_floor와 실제 층이 달라졌으면 이동을 시작하지 않는다.
planning_cabin_source_changed를 기록하고 슬롯을 놓은 뒤 같은 작업을 재계획한다. 작업을 새 수요로 만들거나 버리지 않는다.
수학적 상태 경로는 현재 입력과 명목 시간의 계획이다. 지연·fallback·외부 제어에서 전체 최적성이 유지된다는 뜻은 아니다.

이미 탑승해 해제 시각/층이 증명된 운행은 고정 arc로 삽입한다. 대기자나 해제가 불명확한 캐빈은 계속 제외한다.
확정된 미래 캐빈 사용도 시작층을 바꾸지 않도록 보수적으로 차단한다.
승차 전 빈 회송·큐·아직 시작하지 않은 미래 캐빈 운행은 고정 arc로 추정하지 않는다.
다만 관련 없는 설비 작업 때문에 모든 캐빈을 막던 전역 busy 제한은 없앴다.

상태·후보 폭증은 max_routes/max_arcs/max_tail_ticks/max_cabin_states로 명시적으로 제한한다.
기본 max_cabin_states는 30000이다. 넘으면 BUILD_LIMIT과 원래 입력을 보존하며, 정책의 fallback도 최종 성과에 포함한다.
초기해는 여전히 로봇당 첫 서비스·캐빈당 보수적인 한 사용부터 구성한다. 그보다 풍부한 joint 해는 native solver가 찾는다.

## 4. 실행과 비교 사례

```bash
python -m simulation.evaluation suite \
  --scenario examples/headless/joint_cabin_batch.json \
  --policies optimized rh_milp_grid rh_cpsat_linear rh_cpsat_interval \
  --rh-settings examples/planning/joint_cabin.json --seeds 1001 1002 1003 \
  --output /tmp/clover-cabin-joint
```

restricted_cabin.json은 joint_cabins만 false인 비교 설정이다.
이 예제는 좁은 목표 지점에 들어갈 수 있는 작은 로봇과, 반대 층의 큰 로봇을 함께 사용한다.
긴 로컬 작업 때문에 작은 로봇 한 대가 두 작업을 H 안에서 시작할 수 없으므로 두 운행의 공동 계획이 필요하다.
모든 정책에 같은 폭·위치·수요를 제공한다. 제출된 실제 6대 시나리오의 검증을 대체하는 입력은 아니다.
기존 배정 MILP의 60초 서비스 후보 제한 때문에 이 예제의 100초 작업은 legacy에서 제외됨을 함께 해석한다.

관측 schema는 v6, 계획과 수학 모델은 v8다. 이전 계획은 자동 재해석하지 않는다.
headless 결과 schema v4, resource analyze 형식은 유지한다. 이전 run의 replay는 이전 소스에서 실행한다.
자유 시작시각 CP의 확대 검증, 모든 정책·재검증 비용까지 포함한 D1과 held-out 추천 실험은 남아 있다.
