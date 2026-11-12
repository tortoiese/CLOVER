# 다중 알고리즘 구현과 D1 지연 적용 범위

2026-11-13 추가: [D1_STARTUP.md](D1_STARTUP.md)의 새 공통 controller로 일곱 정책 모두 staged D1을 지원한다.
아래 표와 D1 시범 설명은 별도로 보존한 기존 D0/v4 경로에 관한 것이다. 신규 v5와 혼합 집계하지 않는다.

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

2026-11-11, `common-model-v1.9-native-profile`.
기본 `optimized` 정책·휴리스틱 가중치·GUI는 유지한다.
유효 작업 초기해·단계별 계측과 최적성 증명의 구분은 `WARM_START_PROFILE.md`를 참고한다.
종료 시 대기 검열·실제 캐빈 이동·원시 로그 analyze는 `RESOURCE_OBSERVABILITY.md`에 정리했다.
현재 소유자별 자원 달력과 여러 운행의 캐빈 상태 경로는 `CALENDAR_AND_CABIN_FLOW.md`를 참고한다.
현재 로컬 소스는 main `a28522d9dd9aee238c27fdc2404cab2b1b10c60d`에서 수정한 사본이다.

## 선택 가능한 일곱 정책

| CLI policy | 실제 결정 방법 | 계산 지연 모드 |
|---|---|---|
| `baseline` | 기존 순차 배정과 충전 규칙 | D0 |
| `optimized` | 기존 가중치 휴리스틱·예측 충전 | D0 |
| `assignment_milp_legacy` | SciPy/HiGHS로 유휴 로봇·현재 작업·설비의 이진 배정 | D0 |
| `rh_milp_grid` | 동일 service-flow 정수 모델을 SCIP으로 계산 | D0, D1 시범 |
| `rh_cpsat_linear` | 같은 변수·정수 행·목적·후보·시간 격자를 CP-SAT으로 계산 | D0, D1 시범 |
| `rh_cpsat_interval` | 같은 arc 모델에서 자원 행을 optional interval 제약으로 교체 | D0, D1 시범 |
| `rh_cpsat_native` | 자유 시작/가변 충전 optional interval·로봇 순서·캐빈 circuit, 명시적 전체/제한 domain | D0, D1 시범 |

기존 배정 MILP는 한 번에 배정 수를 최대화하고 우선순위·거리·에너지 비용을 줄인다.
충전 계획은 기존 규칙이며 60초를 초과하는 이동+작업 후보를 제외하고 작업 후보를 40개로 제한한다.
HiGHS의 0.25초 soft limit을 사용한다. RH처럼 부모가 강제 종료하는 프로세스 격리는 없다.
`legacy_planning`의 solver 상태·gap을 보존한다. 시간 제한 solve를 재실행한 legacy replay의 동일성은 일반적으로 보장하지 않는다.
같은 물리 모델을 쓴다고 같은 최적화 문제를 푸는 것은 아니다. 세 RH 모델만 동일 의미의 비교 대상이다.

## CP-SAT 구현

`planning/backend.py`에 compiler 세 개를 두고, 입력·행 생성·독립 검증·실행기는 공유한다.
MILP/CP-SAT linear는 모든 정수 변수의 범위와 모든 행을 그대로 옮긴다.
두 단계 목적과 전체 timeout, 한 worker, 고정 난수 seed도 같게 설정한다.
1차는 weighted defer 최소화, 2차는 대기·예상 지각·거리·충전 비용 최소화다.
첫 단계 최적성이 증명됐을 때만 정확히 고정하며, 미증명 incumbent이면 악화하지 않는 상한을 사용한다.

interval 방식은 각 행동 후보를 선택 BoolVar와 연결된 optional fixed-size interval로 만든다.
로봇별 `NoOverlap`, 용량 1 자원에 `NoOverlap`, 나머지 정수 mm/slot 자원에 `Cumulative`를 적용한다.
구간은 반열림 `[start,end)`이며 PR9의 전체 구간 점유·후미 해제 의미를 유지한다.
시간별 잔여 용량은 고정 차단 interval로 옮긴다. 원래 tick별 capacity 행은 제거하고,
배터리·로봇 흐름·작업 coverage와 캐빈 층/시간 상태 흐름 행은 유지한다.
반환 해는 원래의 **모든** 정수 행으로 다시 검사하고 별도의 물리 validator도 통과해야 한다.

이는 **고정 시간 격자의 arc 선택에 native 자원 제약을 적용한 구현**이다.
캐빈은 관측된 초기 층에서 여러 운행을 잇는 정수 상태 흐름을 공유한다.
자유 시작시각·가변 충전·행동 순서·캐빈 Circuit은 별도 `rh_cpsat_native`로 구현했다.
같은 해상도/전체 domain의 작은 문제 동등성과, 다른 해상도/제한 domain의 운영 비교를 구분한다.
`semantic_hash`는 세 grid RH의 정수 문제를 식별한다. MILP/linear의 `model_hash`는 같고 interval의 표현 hash는 다르다.
보고서의 variables/constraints는 공통 정수 모델 크기이며 solver 내부의 presolve 후 크기는 아니다.

공식 API 근거: [CP-SAT Python 문서](https://or-tools.github.io/docs/pdoc/ortools/sat/python/cp_model.html),
[상태·solver 사용법](https://developers.google.com/optimization/cp/cp_solver/).

## D1 시범: 계산 시간을 공장 시간에 반영

D0는 계산 중 공장 시계가 멈춘 것으로 해석한다. D1은 관측 시각 τ의 입력만 사용하고,
관측·worker 시작·준비·compile·solve·IPC·첫 검증까지 실제 걸린 벽시계 시간 L을 측정한다.
결과를 즉시 실행하지 않고 SimPy timeout으로 **τ+L에 반환**한다.
그 사이 작업 도착·취소·이동·작업 처리·에너지 소모 등 기존 공장 이벤트는 진행된다.
응답 대기 중 중복 solve는 시작하지 않는다. 종료 시 아직 반환하지 않은 요청도 `PENDING_AT_END`로 남긴다.

반환 시점에서 관측을 다시 만든다. epoch/layout, 참여 로봇의 상태·SOC, 후보 목록, 경로·캐빈 상태가
달라졌으면 거부한다. 새 작업이 들어와 대기 목록이 바뀐 경우도 `STALE_JOB_SET`으로 보수적으로 거부한다.
새 작업을 과거 최적화에 뒤늦게 끼워 넣지 않는다. 거부 시 현재 상태를 사용한 안전 휴리스틱으로 복구한다.
변화가 허용되는 경우에는 현재 점유·에너지·deadline을 기준으로 계획 전체를 다시 검증한 뒤 첫 prefix만 확정한다.
새 시각의 전역 최적성을 주장하지 않도록 적용 사본의 objective/bound/hash는 비워 두고,
요청 시점의 원본 solve 증거는 `result`에 별도로 유지한다.

각 결정에는 requested_at, time, latency_seconds, apply_wall_seconds, 원본 result,
applied_result, accepted/reason을 기록한다. replay는 저장된 L과 결정을 재사용하고 물리 실행을 다시 검증한다.
native timeout solve를 다시 실행하는 방식이 아니다.

**D1 한계**: 반환 시 두 번째 준비·검증·commit에 걸린 시간은 `apply_wall_seconds`로 측정만 한다.
이를 추가 SimPy 지연으로 주입하는 단계와 baseline/optimized/legacy의 D1은 남아 있다.
따라서 이것은 PRD의 완전한 D1 완료가 아닌 RH의 proposal-latency 시범이다.
runner는 비 RH 정책을 섞은 D1 suite를 실행 전에 거부한다.
서로 다른 D0/D1 결과를 같은 운영 조건의 비교로 합치면 안 된다.

## 실행

`robot_factory_sim`에서 Python 3.11 이상으로 실행한다. 설치는 다음과 같다.

```bash
python -m pip install -r requirements-planning.txt
# 기존 배정 MILP도 비교할 때만 추가:
python -m pip install -r requirements-legacy.txt
```

D0에서 여섯 기존 정책에 같은 입력을 준다.

```bash
python -m simulation.evaluation suite \
  --scenario examples/headless/joint_batch.json \
  --policies baseline optimized assignment_milp_legacy rh_milp_grid rh_cpsat_linear rh_cpsat_interval \
  --rh-settings examples/planning/quality.json --seeds 1001 1002 1003 \
  --output /tmp/clover-six-d0
```

D1 시범은 RH 네 정책끼리 실행한다.

```bash
python -m simulation.evaluation suite \
  --scenario examples/headless/joint_batch.json \
  --policies rh_milp_grid rh_cpsat_linear rh_cpsat_interval \
  --rh-settings examples/planning/d1_quality.json --seeds 1001 1002 1003 \
  --output /tmp/clover-three-d1
python -m simulation.evaluation replay \
  --run /tmp/clover-three-d1/joint_batch__rh_cpsat_linear__1001.json
```

결과 schema는 `clover-headless-run-v4`, 계획은 `clover-service-plan-v8`, 관측 입력은 v6다.
이전 버전 결과는 이전 소스로 재현한다. 작은 공동 배정 예제는 결정적 반례이며 3개 seed가 독립 반복 증거는 아니다.
최적 목적값 동등성·실제 물리 실행·성능 우세는 서로 다른 검증이다.
빠른 테스트의 성공이나 solver OPTIMAL만으로 실제 처리량 또는 전체 공장 최적성이 증명되지 않는다.

`quality.json`도 해당 host에서 5초 예산이 부족하면 timeout/hold/fallback할 수 있다.
`quality_step2.json`은 2초 격자·15초 solve 예산의 별도 정확성 확인 설정이다.
위 D0 명령의 settings 경로를 바꿔 사용할 수 있다. 원래 1초 격자 결과를 덮어쓰지 않는다.
거친 격자는 계산량을 줄이는 대신 대기·자원 구간을 더 보수적으로 반올림한다.
이는 운영 추천값이나 held-out에서 검증한 튜닝 결과가 아니다.

## 남은 연구 작업

1. 구현한 자유 시작시각 native의 더 큰 전체 domain·수치 경계 검증 및 계산량 개선.
2. 현재 비어 있는 캐빈의 여러 운행 상태 경로는 구현했다. 이미 탑승한 운행의 고정 arc와 완료 구간의 의무 해제도 구현했다.
   승차 전 빈 회송·대기열·아직 시작하지 않은 미래 운행의 더 정밀한 처리가 남아 있다. 현재는 불명확한 점유를 보수적으로 차단한다.
3. 모든 정책과 적용 비용까지 포함한 완전한 D1 및 latency 예산 민감도.
4. 검열·빈 캐빈 원시 계측은 구현했다. 전체 대기의 생존분석과 교착 검출은 별도 남은 작업이다.
5. 팀원의 실제 6대 시나리오 지도·수요를 고정하고 튜닝/검증/held-out 분리, paired CI와 환경별 추천.

기존 기능 fixture 6개는 제출된 6대 시나리오의 본 실험을 대체하지 않는다.
