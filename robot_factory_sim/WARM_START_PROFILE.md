# 유효 초기해와 계산 병목 계측

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

2026-11-11 · `common-model-v1.4-incumbent-profile`. 기본 `optimized`와 가중치는 유지한다.
여섯 정책, CP interval의 범위와 D1 시범 한계는 `ALGORITHMS_AND_D1.md`를 함께 참고한다.

## 검수에서 확인한 문제

이전 버전의 `build_seconds`는 요청 시작 시각부터 측정해 worker 시작·입력 준비 시간까지 포함했다.
그 숫자만으로 정수식 생성이 느렸다고 판단할 수 없다. 기존 필드는 호환을 위해 유지하고 세부 필드를 추가했다.

초기해는 모든 작업을 defer하고 모든 로봇이 대기하는 해였다. native 계산이 시간 안에 작업 배정을 찾지 못하면
안전한 작업을 할 수 있어도 전부 대기하는 해를 유지한 뒤 휴리스틱으로 복구했다.

또한 2차 결과의 정수 행 검증이 예외를 내면 그 잘못된 값이 변수에 남을 수 있었다.
예외 시 2차 값을 폐기하고 검증된 1차 해만 반환하도록 고쳤다. 물리 validator는 별도로 유지한다.

## 초기해 생성

`planning/incumbent.py`는 같은 canonical 모델 안에서 가능한 첫 서비스 배정을 구성한다.
로봇 순서를 회전·반전해 최대 8개 순서만 시도하고, 로봇당 첫 서비스 한 개를 선택한다.
선행 대기→이동+작업→후행 대기→terminal의 연속 경로, 모든 배터리 tick과 defer 값을 함께 만든다.

자원은 기존 mm/slot 용량과 `[entry, tail_release)` 점유를 그대로 사용한다.
작업 중복·에너지·예비량·충전기 복귀·캐빈 횟수·선택된 자원 충돌을 확인한다.
마지막에는 **원래 정수 변수 범위와 모든 행**으로 검증한다. CP interval도 같은 행으로 검증한다.
안전한 서비스가 없으면 기존 safe hold를 유지한다. 충전 초기해나 로봇당 여러 서비스의 탐색은 이번 범위가 아니다.

기본 `warm_start_seconds=0.05` 안에서 더 좋은 완성 후보를 보관한다.
이 시간은 전체 결정 예산 안에서 사용하며 후보 검증·한 번의 정렬 등은 취소 불가능한 작은 작업 단위다.
전체 강제 종료는 부모의 wall/RSS watchdog이 담당한다.
`warm_start=false`로 끄면 기존 대기 초기해로 돌아간다. 세 RH backend에 동일하게 적용한다.
변수·행·목적·후보·격자는 바꾸지 않아 같은 입력의 semantic/model hash도 같다.

예산이 끝나도 이미 검증한 작업 초기해를 보존한다. 이 경우는 native solver가 찾은 배정이라고 부르지 않는다.
`solution_source`가 `WORK_WARM_START`인지 `NATIVE_PHASE1`/`NATIVE_PHASE2`인지 확인한다.
누락된 solver 의존성은 `UNAVAILABLE`로 남기고 초기해를 native 실행으로 표시하지 않는다.

## 1차 최적값을 바로 증명할 수 있는 경우

1차 목적은 `Σ weight[j] * defer[j]`다. 계수와 변수 하한이 모두 비음수이고 검증된 해의 값이 0이면
그 해는 정확한 하한 0을 달성했다. 더 작은 값은 불가능하므로 1차 native solve를 반복할 필요가 없다.
이때 1차 값 0을 고정하고 남은 예산을 2차 목적에 제공한다.

- `phase1_proof=NONNEGATIVE_ZERO_INCUMBENT`: 수학적 하한과 검증된 초기해로 증명.
- `native_phase1_status=SKIPPED_ZERO_LOWER_BOUND`: 1차 native solve는 실행하지 않음.
- `phase1_incumbent_source=WORK_WARM_START`: 1차 해의 실제 생성 출처.

값이 양수이면 native 1차 solve를 수행한다. 미증명 incumbent는 상한만 잠근다.
전체 `OPTIMAL`은 1차 증명과 2차 native 최적성 증명이 모두 있을 때만 표시한다.
2차 timeout/오류나 더 나쁜 해에서는 기존 유효 incumbent를 유지한다.
D1의 적용 시각 재검증 사본에는 과거 최적성 proof를 이어 붙이지 않는다.

## 단계별 프로파일 필드

| 필드 | 측정 범위 |
|---|---|
| `dispatch_to_worker_seconds` | 요청 시작→worker가 입력 수신. capture·cold startup·입력 IPC 포함 |
| `prepare_seconds` | worker의 detached 경로·입력 준비 |
| `model_seconds` | 정수 변수·행·fingerprint 생성 |
| `hint_seconds` | 초기해 생성과 초기해 검증 |
| `import_seconds` | native backend import의 합계 |
| `compile_seconds` | native 변수·행·interval·목적·hint 구성의 합계 |
| `search_seconds` | native Solve 호출 시간의 합계 |
| `solution_check_seconds` | 반환 2차 정수해 검증 |
| `total_seconds` | worker가 결과를 구성할 때까지의 요청 경과시간 |
| `parent_elapsed_seconds` | worker 결과를 부모가 받았을 때까지의 요청 경과시간 |
| `response_seconds` | 위 두 경과시간의 차이: 응답 직렬화·IPC·수신 처리 포함 |
| `worker_cpu_seconds` | 입력 수신 이후 worker의 process CPU 시간 |
| `worker_peak_rss_mib` | 부모가 5ms 간격으로 관측한 worker RSS의 최대값 |
| `worker_reused` | 이 요청이 이미 살아 있던 worker를 재사용했는지 |

세부 시간의 합은 전체 시간과 정확히 같지 않다. Python 제어·decode·일부 검증 등 잔여 시간이 있다.
`dispatch_to_worker_seconds`를 순수 import 또는 순수 프로세스 시작 시간으로 부르지 않는다.
RSS는 표본 최대이며 커널의 정확한 high-water mark가 아니다. CPU는 startup 이전 분량을 포함하지 않는다.
강제 종료 결과는 관측 가능한 부모 경과시간·RSS 등을 남기며 완료하지 못한 단계는 0으로 남을 수 있다.

compile 중에도 주기적으로 deadline을 확인하고 예산이 없으면 불필요한 native 생성을 멈춘다.
단일 native 호출의 선점은 별도 부모 watchdog이 담당한다. timeout과 watchdog의 의미를 섞지 않는다.

## 공정한 비교와 실행

기존 5초·1초 격자 설정에서 on/off를 분리해 실행한다. 결과 경로는 각각 새 경로를 쓴다.
실제 settings는 모든 run 결과에 보관된다. 기본 정책과 휴리스틱 가중치는 바꾸지 않는다.

```bash
python -m simulation.evaluation run --scenario examples/headless/joint_batch.json \
  --policy rh_milp_grid --rh-settings examples/planning/warm_on.json \
  --seed 1001 --output /tmp/clover-warm-on
python -m simulation.evaluation run --scenario examples/headless/joint_batch.json \
  --policy rh_milp_grid --rh-settings examples/planning/warm_off.json \
  --seed 1001 --output /tmp/clover-warm-off
```

동일 입력의 최적값 동등성, 초기해가 실제로 실행되는지, 제한 시간 내 안정성은 별도로 검사한다.
host 부하에 따라 cold/warm 시간과 timeout은 달라질 수 있다. 작은 결정적 batch의 반복을 독립 확률 표본으로 세지 않는다.
현재 초기해는 로봇당 첫 서비스·캐빈당 보수적인 한 사용부터 구성한다. 여러 운행의 joint 상태 경로는
native solver가 확장하며 상세 범위는 `CALENDAR_AND_CABIN_FLOW.md`를 참고한다.
완전한 D1, 승차 전 빈 회송·대기열의 정밀 시간표 통합, held-out 환경별 추천은 남아 있다.
