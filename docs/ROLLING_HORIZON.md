# Rolling Horizon MILP / CP-SAT 사용·검수 가이드

두 RH 정책은 같은 관측 입력, 시간 확장 그래프, 정수 에너지, 목적함수와 독립 검증기를 사용한다. MILP는 OR-Tools의 SCIP, CP-SAT은 OR-Tools CP-SAT으로 같은 정수 선형 모델을 푼다. 기존 `optimized`는 가중치 휴리스틱, `assignment_milp_legacy`는 이전 배정 모델이다. 기본 정책은 `optimized`로 유지한다. [검수 기록](RH_VALIDATION.md)에 적용 조건·불리한 결과·최종 인수의 남은 목표를 명시했다.

## 실행과 GUI

Python 3.11 이상에서 `robot_factory_sim/requirements.txt`를 설치하고 그 디렉터리에서 실행한다. 검수 시에는 별도 DB를 사용한다.

```sh
ROBOT_FACTORY_DB=/tmp/clover-review.db CLOVER_RAG_ENABLED=0 CLOVER_RAG_PROVIDER=local CLOVER_RAG_ALLOW_NETWORK=0 python -B main.py
```

- 상단 스케줄러에서 RH-MILP 또는 RH-CP-SAT을 선택한다.
- 기본 활성 엔진은 FIFO와 가중치 휴리스틱이다. RH를 선택하면 선택한 RH 하나를 추가하고 모든 활성 엔진을 같은 0초부터 다시 시작한다. MILP에서 CP-SAT으로 새 활성 정책을 바꿀 때도 재시작한다. 활성 정책 사이에서 표시만 바꾸면 재시작하지 않는다.
- 오른쪽 그래프는 활성 엔진을 같은 시간축에 표시한다. 왼쪽 공장은 현재 선택한 정책을 보여준다. 미실행 정책은 표시한다.
- `RH 설정`에서 H, 재계획 간격, 시간 격자 0.5/1/2초, 계산 예산, 여유 시간, 후보 수, arc/RSS 상한과 범위를 바꾼다. 설정 변경 시 계산 중 요청을 무효화하고 진행 중 물리 행동은 안전 경계까지 보호한다.
- 오른쪽 `계획·예약` 탭에서 범위, 두 단계의 상태/목적값/경계, 후보 수, 예약 시간, backend/model 버전과 실패 이유를 확인한다.
- `운영안 비교`는 현재 공장 또는 내장 시나리오를 복사해 독립 실험을 실행한다. 내장 시나리오를 선택해도 공장 DB와 주문은 바뀌지 않는다. 다섯 정책 전체 평가도 선택할 수 있다.
- 비용 입력은 라이브 ROI와 독립 비교가 같은 계산 함수를 쓴다. 로봇별 전력 환산은 `{"R0": [2.0, 0.9]}`처럼 Wh/배터리단위와 충전효율을 입력한다. 미입력은 null이며 실제 0원/0Wh와 구분한다. 추가 정책 비용은 선택 정책에 적용한다. ROI는 반복별 비율의 평균 대신 합산 이익/합산 비용으로 집계한다.

## 모델과 지원 범위

1. 첫 목적 F1은 우선순위·대기 나이를 반영한 미배정 작업 가중합이다.
2. F1이 증명된 최적이면 그 값을 고정하고, 그렇지 않으면 검증된 F1보다 악화되지 않도록 제한해 F2를 푼다. F2는 정규화한 대기·납기 지연·실제 수평 이동거리·충전 비용의 정수 합이다.
3. 공통 실행 가능 힌트는 탐색 시작점을 제공한다. 독립 행 검사에 통과한 F1=0 힌트가 있으면, 비음수 목적의 하한 0에 도달했으므로 F1 최적성을 증명할 수 있다. 이 증거는 `NONNEGATIVE_ZERO_WITNESS`로 기록한다. 힌트 사용 여부로 목적식이나 가능 영역은 바뀌지 않는다.
4. 전체 H 계획 중 첫 작업/연속 충전 구간의 접두부만 원자적으로 확정한다. 첫 이동이 재계획 경계 이후에 있으면 앞부분의 대기만 실행하고 미래 작업을 배정하거나 예약하지 않는다. 작업·충전이 전혀 없는 전 구간 보류 해는 명시적 대체 실행 대상으로 남긴다. 확정 중 오류는 큐·소유권·예약·배정 상태를 함께 되돌린다. 확정하지 않은 미래 예약은 점유하지 않는다.

기본 H=60초, 격자 1초, 재계획 5초, soft 2초, hard 3초, cold startup 10초, 후보 8개, 최대 32개, arc 기본 60,000개, planner RSS 기본 1GiB다. N=floor(H/격자)다. Decimal을 사용해 소비·예비량은 올림, 가용량·충전량은 내림, 비용은 half-up으로 정수화한다. 에너지 단위는 배터리 0.001, 비용 scale은 1,000,000이다. 실제 수평 거리와 엘리베이터 시간을 분리한다.

`station_charge`는 작업장·충전기를 계획하고 교통은 실행 시 자원으로 제어한다. `traffic`은 고정 경로의 통로·교차점과 단일 캐빈의 층/공차 이동/탑승 시간까지 함께 계획한다. 교차점 허가는 도착 전 얻는다. 캐빈 동시 운행 1개가 지원 범위이며 capacity>1 합승 입력은 명시적으로 지원 불가 처리한다. 전체 도로망의 경로 선택까지 전역 최적화하는 모델은 아니다.

시뮬레이션은 실제 SimPy 자원을 최종 기준으로 사용한다. 실제 작업이 예측보다 길어지면 예약을 보수적으로 연장하고 후속 계획을 다시 검증한다. 취소·설비 변경 시 부분 에너지와 작업을 정산하며, 탑승 중 취소는 실제 도착까지 캐빈 점유를 유지한다. 장기 대기와 자원 보유/대기 순환의 교착은 서로 다른 지표로 기록한다.

솔버 시작·양방향 IPC는 전송 스레드, 모델 생성·계산은 별도 프로세스에서 수행한다. hard timeout/RSS 초과/프로세스 고장은 소유한 프로세스만 회수한다. 오류·오래된 해·계획 없음은 검증된 대체 접두부 또는 명시적 안전 대기로 처리한다. 지연시킨 해를 다시 검증해 채택한 경우 `SHIFTED_FEASIBLE`이며 원래 시각의 최적해라고 표시하지 않는다.

## 재현 가능한 비교

```sh
python -B -m simulation.scenario_benchmark --cohort screening --output /tmp/clover-screen --include-cpsat
python -B -m simulation.scenario_benchmark --cohort validation --scenarios alternating_dispatch alternating_dispatch_longer --output /tmp/clover-validation --include-cpsat
python -B -m simulation.benchmark_planning --duration 120 --repeats 3 --output /tmp/clover-five.json
python -B -m simulation.benchmark_planning --replay-experiment /tmp/clover-five.json --output /tmp/clover-replayed.json --wall-limit 900
python -B -m simulation.benchmark_planning --replay /tmp/plan.json --backend milp --output /tmp/plan-milp.json
```

스크리닝 시드는 42~44, 별도 검증 시드는 100~109다. 프로토콜을 실행 전에 저장한다. 주 가설은 교대 물류의 공동 배정 효과이며, 240초/10초 간격의 별도 경우는 기간·부하 민감도 검사다. 단일 충전 슬롯과 기본 3층 공장 결과도 공개한다. 휴리스틱 가중치를 약화하지 않는다. 매 정책에 같은 초기 상태, 외생 도착/고장/취소 trace와 작업별 처리시간 표본을 적용한다. 계획기는 관측 전 미래 도착·고장·난수 표본을 읽지 않는다.

사전 성능 판정은 paired 95% t 구간에서 완료량 차이 하한 ≥ -5%, 대기 P95 차이 상한 ≤ -10%다. 완료/미완료, 진행 중 작업, 취소, 대기 표본 수, 납기 준수, 에너지, 실패 반복, 정상 수락/fallback을 함께 봐야 한다. 시작하지 못한 작업을 제외한 대기 P95만으로 성능 우위를 결론 내리지 않는다. 대수·부하·설비 배치를 바꾼 일반적 우위는 별도 실험이 필요하다. 기본 정책을 RH로 자동 변경하지 않는다.

JSON에는 입력 공장/조건/trace/개입, 소스 hash, OS/Python/패키지 버전, model/objective/unit profile, 실행·실패 상태, 단계별 경계/시간, RSS, task와 action의 안정 ID를 저장한다. 알 수 없는 버전·변조된 입력 digest·비정상 trace는 거부한다. GUI 결과 JSON의 전체 실험도 CLI로 재생할 수 있다. 재생은 같은 입력을 복원하며 시간 제한 솔버의 incumbent과 wall time까지 동일하다고 보장하지 않는다. 사용자 DB에 쓰거나 pickle/JSON 안의 코드를 실행하지 않는다.

## 검수

```sh
QT_QPA_PLATFORM=offscreen CLOVER_RAG_PROVIDER=local CLOVER_RAG_ALLOW_NETWORK=0 python -B -m pytest -q -p no:cacheprovider
```

CI는 macOS/Windows/Ubuntu × Python 3.11/3.14에서 pytest/Qt offscreen, 신규 계획 모듈 Ruff, pip check를 실행한다. [6개 조합 통과 기록](https://github.com/tortoiese/CLOVER/actions/runs/40283424063)이 있다. PR의 최신 커밋 상태는 해당 PR의 Checks에서 확인한다. Qt는 검수한 PySide6 6.11.2, OR-Tools는 9.15.6755로 고정했다. ChromaDB는 public `close()`가 있는 1.5.9 이상을 사용한다. 운영 DB와 RAG 저장소를 소유한 스레드에서 닫고 종료한다.

GUI 반응 목표는 고정 하드웨어에서 별도로 계측한다. 최종 macOS 실제 창의 30명령 측정 3회 P95는 89.4~135.3ms, 정책 활성화 전체 재시작은 223~224ms였다. 관측한 입력을 재사용하고 경로 그래프별 진입점 계산을 최대 1,024개 캐시한다. 명시적 reset·설정 변경은 DB와 새 그래프를 반영한다. 기본 공장 RH 처리량과 수락률의 남은 목표는 이 응답 수치로 대체하지 않는다.
