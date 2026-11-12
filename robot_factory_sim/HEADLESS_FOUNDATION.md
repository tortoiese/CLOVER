# 공통 모델·headless 실험 기반 v1

2026-11-13 추가: 전체 일곱 정책의 새 D1·준비/재시작·v5 결과는 [D1_STARTUP.md](D1_STARTUP.md)를 참고한다.
아래 문서의 D0와 RH 한정 D1 시범은 유지되는 기존 경로다. 최신 core는 `common-model-v2.0-staged-d1`이다.

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

기준 main: `a28522d9dd9aee238c27fdc2404cab2b1b10c60d`.
공통 plant와 재현 실험 기반에 기존 배정 MILP 및 RH MILP/CP-SAT linear/구간 정책을 연결했다.
기본 정책은 `optimized`를 유지한다. 현재 버전은 `common-model-v1.9-native-profile`이다.
현재 자원 달력과 캐빈 상태 경로의 구현·제한은 `CALENDAR_AND_CABIN_FLOW.md`에 정리했다.
RH 사용법·수식·범위는 `ROLLING_HORIZON_MILP.md`를 참고한다.
정책별 구현과 RH 한정 D1 시범의 정확한 범위는 `ALGORITHMS_AND_D1.md`에 정리했다.

## 실행

`robot_factory_sim` 디렉터리에서 실행한다. Python 3.11 이상을 사용한다.

```bash
python -m pip install -r requirements-headless.txt
python -m simulation.evaluation examples --output /tmp/clover-inputs
python -m simulation.evaluation validate --scenario /tmp/clover-inputs/narrow.json
python -m simulation.evaluation run --scenario /tmp/clover-inputs/narrow.json \
  --policy optimized --seed 1001 --output /tmp/clover-one
python -m simulation.evaluation suite --seeds 1001 1002 1003 --output /tmp/clover-36
python -m simulation.evaluation replay --run /tmp/clover-36/narrow__optimized__1001.json
```

기본 suite는 6환경 × 3seed × 2정책 = 36회, 각 600 sim-sec를 실행한다.
기능 확인용으로 `suite --duration 60`을 지정할 수 있다.
여러 사용자 파일은 `suite --scenario a.json b.json`으로 실행한다.
suite 출력 디렉터리는 새 경로여야 하며 이전 결과를 덮어쓰지 않는다.
run도 같은 case의 이벤트 파일을 덮어쓰지 않는다.
사용자 DB를 읽거나 쓰지 않는다. 기본 headless는 GUI·Qt·RAG·SciPy를 import하지 않는다.
기존 배정 MILP를 명시적으로 선택할 때만 SciPy가 필요하다.

## 팀원이 준비할 입력

`examples`가 출력하는 JSON을 기준으로 편집한다. schema는 `clover-scenario-v1`이다.
지원하지 않는 필드·단위·부적합 작업·중복 UID는 검증에서 거부한다.

| 항목 | 의미 |
|---|---|
| `id` | 영문·숫자·밑줄·하이픈의 고유한 시나리오 이름 |
| `units` | 도면 좌표 px, 1px=.1m, 폭·길이 m, 시간 sim-s, 에너지 battery-unit |
| `factory` | 기존 FactoryConfig의 설비·충전기·로봇·노드·엣지·캐빈 구성 |
| `initial_robots` | 모든 활성 로봇의 x/y/floor/soc_pct. 첫 dispatch 전에 적용 |
| `initial_cabins` | 모든 캐빈의 초기 정수 층. 캐빈이 없으면 빈 객체 |
| `plant` | work_drain_per_sec, fault_recovery_time, rescue_battery_pct |
| `duration/warmup` | 종료 시각과 측정 시작 시각. 측정창은 `[warmup,duration)` |
| `check_interval` | 독립 상태·에너지·입력 보존 검사 주기, 기본 1초 |
| `workload` | 미리 만드는 poisson 수요 또는 직접 지정한 trace |
| `interventions` | 발생 시각까지 숨겨 두는 취소·가용성·퇴역·안전 drain 통로 폭 변경 |

엣지 폭과 로봇 치수는 m이다. 도면 좌표에는 배율을 중복 적용하지 않는다.
엣지 길이는 기하 좌표에서 계산한다. 다른 `edge.distance`를 넣어 실제 이동길이를 덮어쓰는 방식은 거부한다.
각 작업은 하나의 공정이다. 적합 설비는 그 작업의 operation과 같은 유형이어야 한다.
`required_stations`를 다단계 생산 순서로 사용하는 기능은 이번 모델에 포함하지 않는다.

### 작업 입력

```json
{"kind":"poisson","rate":0.08,"priority":2,"deadline_slack":90,"fixed_station":true}
```

도착은 정책을 실행하기 전에 seed로 만든다. 도착 시각·목적지 선택·서비스 noise는 각각 별도 stream이다.
설비 수를 바꿔도 같은 seed·rate·duration의 도착 시각과 작업별 noise가 달라지지 않는다.
v1.1에서 목적지 stream을 분리했으므로 v1의 Poisson 생성 결과와 seed만으로 같은 trace를 뜻하지 않는다.
실제 trace와 source hash를 기록하며, 과거 결과는 과거 소스에서 재현한다.
`fixed_station=true`면 지정된 설비만 적합하다. false면 같은 공정 유형의 구성된 설비가 적합 대상이다.
시작 시 꺼진 설비도 수요·적합 대상에서 제거하지 않는다. 실제 배정 시에는 현재 가동 여부를 확인한다.
rate=0이면 빈 trace다. 내부 생성기는 동시에 실행되지 않는다.

직접 지정하는 trace 예:

```json
{"kind":"trace","jobs":[
  {"uid":"job-1","at":0,"station":"S0","allowed":["S0"],
   "operation":"inspection","priority":2,"noise":0,"deadline":30}
]}
```

UID는 중복될 수 없고 시각은 오름차순이다. 미래 도착·실제 noise는 plant에만 있으며 스케줄러 입력에 미리 제공되지 않는다.
trace의 모든 수요를 수락한다. 입력 크기 상한 100,000건을 넘으면 명시적으로 거부하며 일부를 조용히 잘라내지 않는다.

intervention kind는 `cancel_task`, `station_active`, `charger_active`, `robot_retire`, `edge_width`이다.
target은 작업 UID 또는 구성 ID이며 edge ID는 문자열로 쓴다. cancel/retire의 value는 true다.
동일 시각에서는 trace 주입 process를 intervention보다 먼저 등록한다. 기존 dispatch의 FIFO 이벤트 순서도 재현된다.
`robot_retire`는 통행 안전 경계까지 보류하는 운영 명령이다. 이동 도중 강제로 멈추는 기계 고장과 구분한다.
`edge_width` 역시 기존 점유를 보호하는 운영 변경이다.

## 공통 모델 수정

- 작업 소모를 진행 시간에 따라 정산한다. 고갈 시 미완료로 재큐잉하고 복구 중 설비 슬롯을 유지한다.
- 재시도는 같은 logical UID의 새 attempt이며 작업은 처음부터 다시 처리한다. 이미 사용한 에너지는 보존한다.
- 작업·이동·충전의 진행분과 구조 주입 에너지를 장부에 포함한다.
- 외부 취소는 terminal 상태로 한 번만 처리한다. 이미 진입한 통로는 출구·후미 여유까지 점유한다.
- 보류한 선점 요청이 교체되면 이전 예약 작업을 대기열로 돌려준다. 취소된 요청을 뒤늦게 재배정하지 않는다.
- 물리 거리, 자유 흐름 이동시간, Dijkstra의 가상 비용을 분리한다. 실제 이동시간에는 문·빈 캐빈 회송·교차로·후미 여유가 들어간다.
- 충전기 복귀 경로가 없으면 에너지 0으로 취급하지 않는다. 안전하게 수행할 수 없는 작업은 대기한다.
- 복귀 에너지의 출발점은 실제 작업 접근 노드다. 설비 중심 좌표가 접근 노드와 다를 때 중심으로 순간이동한 것으로 계산하지 않는다.
- 캐빈 승객 capacity는 1만 지원한다. 2 이상은 실제 합승 상태기계가 없으므로 명시적으로 거부한다.
- GUI의 최근 500건 표본과 전체 누적 평균을 분리한다. 연구 결과의 분위수는 전체 관측값에서 구한다.
- 데모 stochastic 생성기의 대기 상한 40은 유지하되 모든 제안·거부를 registry/event에 기록한다. λ=0도 지원한다.

PR9의 mm/slot·원자적 구간 확보·후미 여유·drain 계약은 유지한다.
휴리스틱의 기존 가중치는 유지하고 거리·에너지 단위와 충전 예상량의 단위 오류를 수정했다.

## 결과와 검사

각 run은 `*.events.jsonl`, `*.json`을 저장한다. suite는 매 run마다 manifest를 갱신하고 마지막에 summary.csv를 쓴다.

- 입력: scenario/initial/trace/source hash, 실제 trace, seed, 라이브러리 버전.
- 작업: offered, rejected, completed, cancelled, unfinished, terminal 사유·attempt·시각.
- 서비스: 측정창 처리량, 전체 due cohort 위반, 완료 관측 지연 분포, 미관측 시작·미완료 나이.
- 에너지: 초기·충전·구조·주행·작업·잔량·보존 오차, 측정창 에너지와 완료당 소비.
- 점유: 묶음 요청·승인·해제 이벤트, 캐빈 승차·도착, 실제 불변식 검사.
- 계산: dispatch wall 시간. 이벤트 digest는 wall 시간 값을 제외한 물리 이벤트를 식별한다.

불변식은 queue/current-task/보류 예약을 registry와 독립적으로 대조한다.
새 resource_waits/cabin_phases와 단일 run의 원시 로그 analyze는 `RESOURCE_OBSERVABILITY.md`를 참고한다.
배터리 보존 오차, 부분 통로 확보, 승인 시 용량 초과도 검사한다.
종료 시 남은 작업은 정상적으로 unfinished에 남는다. 이를 제거해서 좋은 평균을 만들지 않는다.

관측된 지연 분위수는 실제 시작·입장 표본의 조건부 통계다. 미완료·검열 수와 함께 읽는다.
납기 대상이 없으면 위반율은 null이다. 아직 끝나지 않은 due 작업도 위반 분모·분자에 포함한다.
대기시간 임계치 초과는 long_wait이며 교착 증명이 아니다. 자원 순환 교착 검출은 아직 구현하지 않았다.

## 시간·실패·재현 범위

기본 runner는 **D0 기반 검수**다. D0의 스케줄러 wall 시간은 공장의 결정 지연으로 주입하지 않는다.
RH 세 정책은 요청 계산 지연을 주입하는 D1 시범도 지원한다. 적용 시 재검증 비용은 별도 측정만 한다.
완전한 모든 정책 D1, 승차 전 빈 회송·대기열의 정밀 고정 시간표와 환경별 통계적 추천은 다음 단계다.
RH는 전체 결정 watchdog과 RSS 상한을 가진 별도 프로세스와 실제 첫 prefix에 연결된다.
36회 성공은 기능 검수이며 어떤 정책의 일반적인 우세를 입증하지 않는다.

run별 wall 한도를 초과하면 TIMEOUT과 부분 상태를 저장한다.
suite 전체 예산 때문에 시작 못 한 run은 NOT_RUN_BUDGET으로 남긴다. 정책 실패로 계산하지 않는다.
물리 검사가 실패하면 FAILED로 기록한다. 성공 표본만 골라 저장하지 않는다.

replay는 저장한 실제 trace와 초기 상태를 사용하고 source hash·scenario hash·trace hash를 검사한다.
동일 버전의 물리 이벤트 digest를 대조한다. wall 시간이나 다른 라이브러리 버전의 bit 단위 재현은 보장하지 않는다.
소스가 수정되면 새 실험을 실행한다. 코드가 바뀐 뒤 과거 run의 replay 검사를 통과한 것으로 처리하지 않는다.

이 배포 사본에는 Git checkout이 없으므로 source_commit은 null이며 실제 Python 파일 hash로 식별한다.
base_commit은 출발점이다. 수정된 로컬 사본을 그 main 커밋 자체라고 표시하지 않는다.

## RH 입력 어댑터의 현재 범위

`simulation.planning.snapshot.capture(engine, Settings(), token)`은 현재 시각의 불변 입력을 만든다.
JSON·pickle 직렬화가 가능하며 미래 도착·작업의 실제 noise·SimPy handle을 포함하지 않는다.
worker에서 `prepare(snapshot, deadline=...)`를 호출하면 K=1 경로와 PR9의 전체 구간 점유 계수를 만든다.
이 과정은 살아 있는 엔진을 다시 읽지 않는다. 후보 제외·경로 없음·route 수 상한·예산 초과를 명시한다.

캐빈의 빈 회송·문·후미 여유를 이동시간에 포함한다. 초기 비어 있는 캐빈의 여러 운행은
공동 층/시간 상태 경로로 연결한다. 실제 지연이 생기면 물리 자원 승인과 출발층 재검증을 거친다.
실제 claim의 해제 시각은 확실한 protected tail·정량 RH 충전·보호된 탑승 운행 종료만 기록하며 나머지는 `end=null`이다.
같은 소유자의 실제 점유와 prefix는 중복 차감하지 않는다. 실제 경로 완료 후 지난 이동 자원의 의무를 제거한다.
보호된 탑승 운행은 증명된 해제 시각/도착층의 고정 상태 전이로 연결한다.
그 외 해제가 불명확한 이동·점유는 `unknown_cabin_release`로 남겨 가짜 해제 시각을 만들지 않는다.
교통 drain 중에는 새 경로 권한을 만들지 않는다.

이 입력은 `clover-planning-observation-v6`다. 계획/모델은 v8이며 이전 실행 계획을 재해석하지 않는다.
이 입력은 composite service arc·tail·에너지 제약, 세 RH backend, 독립 검증기와 실제 prefix 실행에 연결되었다.
