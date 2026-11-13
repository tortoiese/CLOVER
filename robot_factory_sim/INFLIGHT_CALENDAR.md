# 진행 중 캐빈 운행과 완료 구간의 자원 달력

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

2026-11-12 · `common-model-v1.9-native-profile`.
기본 optimized 정책·가중치·GUI는 유지한다. 기존 공통 수식은 `CALENDAR_AND_CABIN_FLOW.md`를 참고한다.

## 왜 고쳤는가

이전 모델은 현재 바쁜 캐빈의 해제를 알 수 없으므로 새 경로를 제외했다.
안전한 방식이지만 이미 탑승해 2층으로 가고 있고 하차 완료 시각도 정해졌다면, 그 이후의 운행까지 막을 필요는 없다.
또한 경로 전체가 끝나기 전에는 이미 통과한 통로와 캐빈의 prefix 의무가 남았다.

이번 구현은 실제로 확정된 것만 추가로 관측한다. 아직 줄을 서거나 빈 캐빈을 부르는 단계의 미래를 추정하지 않는다.

## 실제 실행이 보장하는 해제 증명

Robot은 승차 문 대기까지 끝난 뒤 실제 cabin request를 가진 상태에서 다음을 고정한다.

```text
release_at = loaded_start + abs(destination - actual_floor) * frozen_travel_seconds + frozen_alighting_seconds
release_floor = destination
release_owner = robot instance + display ID
```

탑승 중 이동과 하차 문은 같은 deadline을 유지한다. 직접 SimPy interrupt가 들어와도 제어 명령을 보류하고
남은 시간만 기다린다. 문 대기를 처음부터 재시작해 release_at을 넘기지 않는다.
탑승 뒤 travel_time/door_time 설정이 바뀌어도 그 운행은 시작 시 고정한 값으로 끝난다.
퇴역·재계획·충전 등의 제어는 하차와 슬롯 해제 후 기존 제어 경로로 처리한다.
빈 회송과 승차 전 문 대기는 계속 중단 가능하며 확정 해제 정보가 없다.

capture는 실제 Elevator 객체·resource.users·request.proc·Robot.process가 일치하는 경우만 증명으로 기록한다.
같은 표시 ID의 다른 로봇, 외부 process의 이동, 큐 대기에는 이 증명을 붙이지 않는다.
현재 actual hold와 같은 owner/slot/end가 없거나 일부 필드가 누락된 증명은 MODEL_INVALID로 거부한다.
현재 시각 이후의 유한 해제·정수 목적층·공장 층 범위를 검사한다.

## 계획의 고정 캐빈 상태 전이

관측 시각 τ에 캐빈이 1.25층이고 10초에 2층 하차가 끝난다면, step=2초·τ=4초의 모델은 다음과 같다.

```text
고정 운행 F: (1.25층, tick 0) → (2층, tick 3), F = 1
잔여 cabin capacity: tick [0,3)는 0, tick 3부터 1
새 운행: 고정 운행 이후의 층 상태에서 이어지는 선택 arc
```

캐빈 flow의 최초 공급은 실제 관측 층에서 시작한다. 고정 운행은 상수 1인 정수 변수로 연결하고,
새 로봇 운행은 기존 선택 변수를 사용한다. 기존 운행이 끝나는 층을 과거 현재 층과 혼동하지 않는다.
캐빈에 대기자가 있거나, 이후 확정 prefix의 다른 사용이 불명확하면 그 의무가 달력에 남아 용량을 차단한다.
대기자의 미래 행선지·처리시간을 미리 읽어 새로운 운행을 끼워 넣지 않는다.

MILP·CP linear는 같은 정수 행, CP interval은 같은 고정 점유 interval과 캐빈 상태 flow를 사용한다.
초기해도 고정 운행을 포함한 뒤의 층에서 시작한다. 독립 validator는 새 운행 시작 시각·출발층을
증명된 하차 시각/층과 비교한다. 실제 슬롯 승인 시 source_floor 재검증도 유지한다.
joint_cabins=false는 관측된 고정 운행 이후 새 compound 운행 한 번을 허용하는 비교 설정이다.

## 이동 구간을 번호로 해제

RouteInput.use_sections는 각 통로/교차로 의무가 속한 원래 경로의 시작·끝 segment index를 보존한다.
CabinUse.segment_index도 원래 경로 번호다. Robot의 진행 정보는 epoch/token/snapshot time/Arc가 같은
정확한 prefix에만 유효하다. 시간이 예상보다 지났다는 이유로 완료 처리하지 않는다.

- 통제 구간 전체를 통과하고 실제 후미 여유가 끝났을 때만 completed segment index를 넘긴다.
- 그보다 앞선 통로/교차로의 prefix 의무를 제거한다. 아직 이동 중인 구간의 실제 claim은 유지한다.
- 중간 구간의 protected tail deadline은 실제 claim과 해당 prefix에 같은 유한 end를 사용한다.
- 캐빈을 하차하고 슬롯을 해제한 뒤에는 해당 캐빈 prefix를 제거한다. 이후의 통로 의무는 계속 남는다.
- 현재 탑승한 캐빈의 actual/prefix에는 같은 해제 시각을 사용한다. 동일 캐빈의 더 나중 사용까지 같이 해제하지 않는다.

같은 소유자의 actual/prefix 최댓값 결합과 다른 소유자 합산은 유지한다.
따라서 이미 쓴 자원의 무기한 예약과 같은 점유의 중복 차감은 줄이면서 실제 안전 점유는 보존한다.

## 실행 예제

inflight_cabin_batch는 폭 0.6m 로봇 R0이 위층의 좁은 설비로 가고, 폭 1.2m R1이 위층 캐빈 입구에서 기다린다.
위층 통로 폭은 1.3m라 큰 로봇은 첫 작업을 수행할 수 없다. 두 번째 아래층 작업은 4.5초에 도착한다.
R0의 실제 탑승 운행이 진행 중인 상태에서 R1의 다음 운행을 계획할 수 있는지 확인하는 결정적 기능 사례다.
여러 seed에서 결과가 같아도 독립적인 통계 반복으로 해석하지 않는다.

```bash
python -m simulation.evaluation suite \
  --scenario examples/headless/inflight_cabin_batch.json \
  --policies optimized rh_milp_grid rh_cpsat_linear rh_cpsat_interval \
  --rh-settings examples/planning/joint_cabin.json --seeds 1001 1002 1003 \
  --output /tmp/clover-inflight
```

## 범위와 남은 것

관측 schema는 v6, 계획/모델은 v8, headless 결과는 v4다. 이전 계획의 자동 변환은 지원하지 않는다.
이전 run은 해당 소스로 replay한다. 새 결과에도 실제 입력·해제 정보·floor flow·실행/실패 기록을 보존한다.

완료한 것은 이미 탑승한 보호 운행의 고정 상태 전이와 실제 완료 구간의 점진적 의무 해제다.
승차 전 빈 회송·큐와 아직 시작하지 않은 미래 운행은 여전히 보수적으로 차단한다.
주행 중 구간 전체의 해제를 미래 속도/배터리/구조 시간을 추정해 계산하지 않는다.
K=1 기하 경로·고정 시간 격자·승객 capacity=1이라는 범위도 유지한다.

D1은 여전히 RH 요청 지연 시범이다. 진행 중 캐빈 층이 변하면 보수적인 route 재검증이 계획을 거부할 수도 있다.
모든 정책의 완전한 D1, 자유 시작시각 native의 확대 검증, 실제 6대 시나리오의 held-out 효과 검증과 환경별 추천은 남아 있다.
