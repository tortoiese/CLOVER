# RH 입력 어댑터 — 실행 연결 v5

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

현재 입력 계층은 `rh_milp_grid`의 solver·controller·prefix executor에 연결되어 있다.
기본 정책은 `optimized`다. 실제 실행과 제한 범위는 `ROLLING_HORIZON_MILP.md`를 참고한다.
이전 입력 전용 사본과 구분하며, 최신 자원 달력·캐빈 경로의 범위는 `CALENDAR_AND_CABIN_FLOW.md`에 정리했다.

## 호출과 책임

```python
from simulation.planning.contracts import Settings
from simulation.planning.snapshot import capture, prepare

# SimPy를 소유한 스레드에서, 로봇 time-zero 초기화 이벤트 후에 관측한다.
observation = capture(engine, Settings(horizon=60, step=1, max_tasks=32), token="request-1")
# 관측 결과만 별도 worker에 전달한다. 이 함수는 원래 엔진을 다시 읽지 않는다.
prepared = prepare(observation)
```

`capture`는 엔진·SimPy 자원·변경 가능한 factory 객체를 반환하지 않는다.
현재 로봇의 위치·SOC·가용성, 공정 적합 설비, nominal 처리시간, 대기 작업과 후보 제외,
현재 캐빈의 소수 층 위치, 실제 claim의 소유자·mm/slot 단위·request_id를 고정한다.
실제 처리시간 noise나 미도착 작업·외란은 포함하지 않는다.

`prepare`는 고정된 factory JSON에서 경로망을 재구성한다. 선택된 작업의 적합 설비와 충전기에 대해
현재 위치 및 서비스 위치에서의 K=1 경로를 만든다. 거리(m), 무경합 시간(s), 구간 전체 점유를 분리한다.
수요와 초기 상태를 새로 생성하지 않는다.

## 물리 의미

- 설비 위치는 로봇이 실제로 작업하는 접근 노드다. 도면상의 중심으로 이동 완료 위치를 바꾸지 않는다.
- 같은 통제 구간의 모든 자원은 진입부터 출구 도착과 후미 여유까지 같은 구간을 점유한다.
- 교차로 통과 시간, 캐빈의 빈 회송·문·승차 이동을 포함한다.
- 실제 claim은 protected tail·시작된 정량 RH 충전·탑승 후 보호된 캐빈 운행의 확실한 해제만 기록한다. 나머지는 `end=None`이다.
- 보호된 탑승 운행은 고정 해제 시각/도착층으로 연결한다. 승차 전 빈 회송·대기열 등 해제가 불명확한 캐빈은 `unknown_cabin_release`로 남긴다.
- 캐빈의 가능한 출발층별 경로 변형을 만들고 모델에서 초기 층→여러 운행의 공동 상태 경로를 연결한다.
- `prepare`의 자원 사용 구간은 상대적인 실수 초다. 정수 grid 모델로 옮길 때 시작은 내림·끝은 올림하는 안전 점유 변환과 행동 선후 연결을 추가해야 한다.
- Decimal 에너지 단위 변환은 PR10의 `units.py`를 재사용한다. 재현을 위해 새 observation schema와 PR9 traffic contract를 함께 기록한다.

입력 스키마는 `clover-planning-observation-v6`, 계획/모델은 v8, 자원 계약은 `traffic-mm-bundle-v1`이다.
이전 실행 계획을 자동 변환하거나 실행하지 않는다.

## 경계와 아직 남은 구현

후보의 우선순위와 대기 aging, 제외 사유를 기록한다. 경로 없음·개수 상한·관측 후 변경·예산 초과를 구분한다.
`prepare`의 `deadline`은 협조적 한도다. RH controller는 별도 spawn worker를 사용하므로
전체 결정이 watchdog 또는 RSS 한도를 넘으면 부모가 worker를 강제 종료·회수한다.

복합 service arc·tail·현재 점유·충전·복귀 에너지, MILP backend, 독립 validator,
epoch·상태 버전·작업 UID를 확인하는 prefix commit과 강제 중단 worker를 연결했다.
기본 `joint_cabins=true`에서는 캐빈별 여러 운행의 층/시간 상태 경로를 사용한다.
`false`는 기존 horizon당 캐빈별 한 compound 운행의 비교 옵션이다.
CP-SAT linear/고정 격자 자원 interval compiler와 RH 한정 D1 시범을 연결했다.
이미 탑승한 보호 운행의 고정 arc는 구현했다. `INFLIGHT_CALENDAR.md`에 범위를 정리했다.
승차 전 빈 회송·큐·아직 시작하지 않은 미래 운행의 정밀 고정 arc 삽입,
완전한 D1과 확대 효과 검증은 남아 있다.
소유자는 instance를 포함하며 실제 traffic request/process와 대조한다. 모호한 점유는 opaque 소유자로 남긴다.
같은 소유자의 실제 점유와 prefix 의무는 자원 달력에서 중복 차감하지 않는다.

검사는 실제 이동과의 여섯 환경 대조, 실제 spawn worker의 동일 결과, 미래 표본 미노출,
통제 구간의 전체 자원 묶음, 캐빈 중간 위치, 기존 관측의 불변성, 후보 aging, 상한·단위·schema 거부를 포함한다.
