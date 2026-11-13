# 실행 가능한 RH MILP v6

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

기본 `optimized` 정책과 기존 가중치는 유지한다. GUI 선택지는 변경하지 않았다.
headless CLI 또는 `SimulationEngine(mode="rh_milp_grid")`에서 선택해 실행한다.
기본은 **D0**, 기하 경로 K=1과 캐빈의 여러 운행 상태 경로이며 전체 PRD 완료를 뜻하지 않는다.
동일 모델의 CP-SAT linear/구간 compiler와 RH D1 시범은 `ALGORITHMS_AND_D1.md`를 참고한다.
소유자별 점유·해제 달력과 캐빈의 상세 수식/제한은 `CALENDAR_AND_CABIN_FLOW.md`에 정리했다.

## 실행

Python 3.11 이상에서 `requirements-planning.txt`를 설치한다. OR-Tools 9.15.6755/SCIP와 psutil 7.2.2를 사용한다.
일반 headless 실행에는 기존 `requirements-headless.txt`만 필요하다. solver는 별도 worker에서 지연 import한다.

`robot_factory_sim` 폴더에서:

```bash
python -m simulation.evaluation run --scenario examples/headless/joint_batch.json --policy rh_milp_grid --rh-settings examples/planning/quality.json --seed 1001 --output /tmp/clover-rh-one
python -m simulation.evaluation suite --scenario examples/headless/joint_batch.json --policies optimized rh_milp_grid --rh-settings examples/planning/quality.json --seeds 1001 1002 1003 --output /tmp/clover-rh-batch
python -m simulation.evaluation replay --run /tmp/clover-rh-one/joint_batch__rh_milp_grid__1001.json
```

기본 suite는 기존처럼 baseline/optimized 36회다. RH 비교는 `--policies`로 명시한다.
출력 경로는 새 경로여야 한다. `quality.json`은 작은 문제의 정확성·해 품질 검사용이며
`smoke.json`은 짧은 예산에서 실패·fallback까지 확인하는 기능 검사 설정이다. 운영에 가장 좋은 설정이라는 뜻은 아니다.

## 수학적 의미

각 로봇은 `(위치, tick)` 상태에서 시작하는 한 개의 연속 경로를 선택한다.
이진 `x[r,a]`는 행동 arc, `u[j]`는 작업 defer, 정수 `B[r,t]`는 배터리, `g[a]`는 충전 증가량이다.

```text
각 상태: outgoing(x) - incoming(x) = 초기 공급 또는 terminal 수요
작업 j: Σ service_arc(j) x + u[j] = 1
자원 k, tick t: Σ use(a,k,t) x <= 소유자별 자원 달력의 잔여 용량[k,t]
배터리: B[t+1] = B[t] - 시작하는 행동 소비량 + 끝나는 충전 증가량
충전: 0 <= g[a] <= floor(rate * step / energy_quantum) * x[a]
배터리: 0 <= B <= floor(capacity / energy_quantum)
작업 종료·terminal: B >= reserve + 도달 가능한 충전소까지 복귀 에너지
```

현재 위치에 그대로 머무는 안전 hold는 초기 SOC가 낮거나 충전소가 없어도 허용한다.
그 외 서비스를 수행하고 복귀할 수 없는 행동은 선택하지 못한다.
서비스 arc는 **이동+작업을 함께 선택**한다. 이동만 시작하고 실제 작업은 계속 defer하는 계획을 만들지 않는다.

H는 새 행동 시작 범위다. H 안에서 시작한 긴 서비스는 H 이후 완료까지 자원·에너지를 유지한다.
tail에서 다른 새 서비스를 시작하지 않는다. 명시된 tail/arc 상한을 넘으면 BUILD_LIMIT을 기록한다.
주행과 작업 소비를 arc 시작에서 미리 차감하는 방식은 소비가 끝날 때까지 필요한 에너지를 확보하는 보수적 표현이다.
충전 증가는 충전 arc 끝에서만 다음 행동이 사용할 수 있다.

통로는 정수 mm, 교차로·설비·충전기는 slot이다. 통제 구간의 전체 자원에 같은 진입~후미 해제 구간을 사용한다.
시간 점유는 시작 내림·끝 올림, 배터리 가용량은 내림·소비와 예비량은 올림으로 변환한다.
서로 다른 정책의 실제 실행은 같은 SimPy arbiter·자원을 사용한다.

목적은 1차로 우선순위·aging을 반영한 weighted defer를 최소화하고,
2차로 예상 도착→작업 시작, 지각, 거리, 충전 점유와 충전량 비용을 줄인다.
1차가 증명된 최적값이면 그 값을 고정한다. 미증명 incumbent이면 악화시키지 않는 상한만 둔다.
2차 실패 시 검증된 1차 incumbent를 유지한다. solver bound/gap은 이 제한된 snapshot 모델의 값이다.
전체 공장 기간의 전역 최적성이나 실제 처리량 우세를 뜻하지 않는다.

## 실제 실행과 실패

도착·가용 로봇·layout 변경 및 interval이 재계획을 유발한다.
전체 계획을 실행하지 않고 첫 서비스 또는 연속 충전만 확정한다. 시작이 interval 이후면 짧은 선행 wait만 확정한다.
아직 확정하지 않은 나머지 작업은 대기열에 남는다.

독립 validator는 solver 행을 읽지 않고 경로·행동 연속성·작업 coverage·에너지·자원 interval을 대조한다.
commit 시 epoch·관측 시각·layout·현재 작업과 SOC를 다시 확인한다.
여러 로봇의 배정은 깨우기 전에 함께 준비하며, 중간 예외는 task 참조·대기열·metrics와 이벤트 장부를 롤백한다.

확정된 미래 지침과 실제 소유권은 다르다. 실제 자원은 진입할 때 확보한다.
미지의 현재 점유와 실행 중 prefix의 footprint는 보수적으로 차단하고 다음 관측에서 갱신한다.
실제 처리시간이 길어지면 물리 자원이 점유를 유지한다. 지연·취소·고갈은 원시 기록과 최종 성과에 남는다.

준비·compile·두 목적 계산을 하나의 timeout 안에서 수행한다. 부모 process는 시작·IPC를 포함한 watchdog과 RSS 상한을 따로 감시한다.
한도를 넘거나 native worker가 종료되면 회수하고 다음 요청에서 새 worker를 만든다.
UNAVAILABLE/BUILD_LIMIT/HARD_TIMEOUT/MEMORY_LIMIT/STALE 등은 숨기지 않는다.
반환 해가 없거나 모든 작업을 계속 defer하면 에너지 검사와 실제 자원 승인을 거치는 휴리스틱으로 대체하며,
실행 중인 RH prefix를 자동 선점하지 않는다. fallback 횟수·이유와 실제 완료/소비도 결과에 포함한다.

충전 중 실제 증가량은 설정한 양수 rate를 사용한다. 기존 0.01 하한 때문에 느린 충전기를 더 빠르게 처리하던 경계도 보완했다.

## 캐빈 상태 경로와 제한

기본 joint_cabins=true는 관측된 초기 층에서 여러 compound 빈 회송·승차 운행을 연결한다.
로봇 행동과 캐빈 상태 전이는 같은 선택 변수를 사용하며 idle은 같은 층에 머문다. 순간이동은 허용하지 않는다.
false는 기존 horizon당 한 운행 제한의 비교 설정이다. 실제 실행은 기존 캐빈 객체의 arrive/carry를 사용하며
슬롯 승인 후 계획 출발층과 실제 층이 달라지면 작업을 보존해 재계획한다.

이미 탑승한 보호 운행은 관측된 해제 시각/층의 고정 arc로 연결했다.
승차 전 빈 회송·큐·아직 시작하지 않은 미래 운행의 정밀 고정 arc 삽입은 남아 있다.
해제가 불명확한 점유는 보수적으로 차단한다. 실제 경로 완료 후에는 과거 캐빈·통로 prefix 의무를 제거하므로
관련 없는 설비 작업만으로 모든 캐빈을 차단하지 않는다. 제한으로 인한 fallback과 대기도 최종 성과에 포함한다.

## 재현과 아직 남은 것

결과 schema는 `clover-headless-run-v4`, 관측은 v6, 계획은 `clover-service-plan-v8`다.
각 결정의 관측 입력·반환 schedule·native 상태·bound·실제 commit 여부와 hash를 저장한다.
RH replay는 저장된 결정을 현재 plant에서 다시 검증해 실행한다. 제한 시간 때문에 달라질 수 있는 native solver를 다시 돌렸다고 표시하지 않는다.
따라서 이는 **물리 실행 재현**이며 native 최적성 증명의 재현은 별도 작은 문제 검사로 확인한다.
이전 schema는 새 실행 계획으로 자동 해석하지 않는다.

CP-SAT linear와 고정 격자 native 자원 제약 및 RH 계산 지연 주입 시범을 추가했다.
완전한 D1, 승차 전 빈 회송·대기열의 고정 시간표 통합, 자유 시작시각 native의 확대 검증,
여러 기하 경로 후보와 held-out 통계 검증이 남아 있다.
이번 D0 결과만으로 실제 운영의 최종 알고리즘을 추천하지 않는다.

MILP wrapper의 solver 생성·변수·행·시간 한도 API는 [OR-Tools 공식 MPSolver 문서](https://developers.google.com/optimization/lp/mpsolver)를 따른다.
