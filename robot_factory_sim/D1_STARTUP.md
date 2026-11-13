# 전체 정책 D1과 준비·재시작

2026-11-13 · `common-model-v2.0-staged-d1`.

일곱 정책에서 관측·계산·검증·대체 판단의 시간을 공장 가상 시간에 반영한다.
작업 배정, 충전소 선택, 충전 목표 갱신, 선점도 같은 제안/적용 경로를 사용한다.
기본 GUI와 `optimized` 가중치·기본 D0는 유지한다. 이번 실행 경로는 명시적으로 선택하는 headless 기능이다.

## 시간 의미

`d1-staged-budget-v1`은 측정한 공개 전 계산 비용과 마지막 공개를 위한 선반영 여유를 합친 보수적 규약이다.
순수 계산은 불변 입력으로 실행한 다음 그 반환 지연 동안 공장 사건을 진행한다.
판단을 적용할 때는 최신 상태를 다시 확인한다. 공개 전에는 작업을 큐에서 제거하거나 물리 자원을 잡지 않는다.

마지막 검사·공개 비용의 기본 여유는 0.1초이며 정책별로 다르게 주지 않는다.
실제 마지막 비용이 여유를 넘으면 `TIMING_COVERAGE_VIOLATION`으로 실행을 표시한다.
뒤늦게 시간을 더해서 이미 적용한 시각이 맞았다고 처리하지 않는다.
이는 모든 비용을 실제 값과 정확히 같은 지연으로 주입한다는 주장이 아니다.
큰 입력/다른 장비에서 여유가 충분한지는 다시 측정해야 한다.

시간 기록의 `phases`는 서로 겹치지 않는 제어 구간이다.
내부 solver/IPC 보고서는 해당 구간과 겹칠 수 있으므로 요약 시간들을 중복 합산하지 않는다.
`queue_sim_wait_seconds`는 이미 흐른 대기 시간이며 다시 timeout으로 더하지 않는다.
아직 반환하지 않은 단계는 `pending_phase`, 실제 알려진 준비/계산 결과는 별도로 보존한다.

## 제안과 적용

baseline·optimized·legacy는 기존 selector를 독립된 값 작업공간에서 실행한다.
작업 목록은 RH의 `max_tasks`로 잘라서 다른 정책에 주지 않는다. 미래 작업·실제 미래 처리 noise는 제공하지 않는다.
순번·설비 선택 등 정책 상태도 제안에 포함하고 적용할 때만 변경한다.
RH의 공통 관측과 수학 입력은 같은 sim 시각에 캡처한다.

무관한 신규 작업은 원래 제안에 추가하지 않고 실제 큐에 보존한다.
참여 작업의 취소·소유권·로봇 상태·SOC·능력·경로 변경 등은 적용 시 검사한다.
새 긴급 도착은 재계획 사유로 처리한다. RH 시간 이동 후의 계획을 현재 시각의 최적해로 표시하지 않는다.
부분 적용은 receipt에 명시하며, 성공한 소유권과 미적용 작업을 보존한다.
실패 대체 계산도 비용과 출처를 기록한다. 대체 예산이 부족하면 HOLD한다.

충전 중 새 목표를 판단하는 동안에는 추가 충전을 잠시 보류한다.
승인 목표·충전기 용량·실제 에너지 장부를 보존한다. 충전 세션 종료 시 승인 cache를 제거한다.
이 보수적 제어 지연은 D1 성과에 포함되며, 원래 D0 정책의 실행 결과와 혼동하지 않는다.

## 준비와 수명

READY는 선택한 정책 의존성 import·backend 생성이 끝났다는 ACK다. 준비 중 공장 입력의 solve를 숨기지 않는다.
모든 정책의 D1 계산은 기존 supervisor를 확장한 격리 worker를 사용한다.
준비·운영 중 RSS/IPC/hang을 감시하고, 응답의 session/generation/request/epoch를 검사한다.
준비 실패와 운영 실패는 구분한다. 회수 후에만 새 세대를 만든다.

- `on_demand`: 첫 결정에 준비 비용을 포함한다. 준비만으로 예산을 쓰면 그 요청의 실패를 남긴다.
- `eager`: t=0부터 준비하고 그동안의 수요·backlog도 보존한다. 정책별로 측정창을 옮기지 않는다.
- 재시작: 기본 최대 2회, backoff 0.1초/1초. 재준비는 운영 중 cold 비용이다.
- 종료: 자식/RPC thread/connection을 회수하고 지연 적용 generator를 닫는다.

SimPy 환경을 폐기할 때 미래 사건을 실행해 종료 상태를 만드는 방식은 사용하지 않는다.
폐기된 환경에서 process 사건이 pending일 수 있지만 generator가 닫혀 실제 적용을 재개할 수 없는지 검사한다.
초기 준비를 앞당겨도 t=0 충전 판단이 기다리면 cold 지연이 나타날 수 있다. eager의 성능 이득을 가정하지 않는다.

## 실행

`robot_factory_sim`에서 Python 3.11 이상으로 실행한다.

```bash
python -m pip install -r requirements-planning.txt
# assignment_milp_legacy를 비교할 때 추가
python -m pip install -r requirements-legacy.txt

python -m simulation.evaluation suite \
  --scenario examples/headless/joint_batch.json \
  --policies optimized rh_milp_grid rh_cpsat_native \
  --seeds 1001 1002 \
  --rh-settings examples/planning/native_bounded4.json \
  --decision-settings examples/decisions/d1_on_demand.json \
  --output /tmp/clover-staged-d1

python -m simulation.evaluation profile \
  --manifest /tmp/clover-staged-d1/manifest.json \
  --output /tmp/clover-staged-d1-profile.json
python -m simulation.evaluation replay \
  --run /tmp/clover-staged-d1/joint_batch__rh_milp_grid__1001.json
```

출력은 새 경로를 사용해서 기존 실험 결과를 덮어쓰지 않는다.

결과는 `clover-headless-run-v5`, 결정 기록은 `clover-decision-record-v1`, worker 수명은 `clover-worker-lifecycle-v1`이다.
제안·측정 시간·준비/재시작 결과를 보존하고 replay에서는 native solver를 다시 돌리지 않는다.
물리 실행·적용 검증·원시 지표는 다시 검사한다.
profile은 시간 규약·시작 모드·정확한 수학 설정·환경·정책·source를 구분한 설명 통계다.
공통 단계 합계를 native 부모 시간으로 바꾸어 표시하지 않으며, 직접 측정하지 않은 parent 시간은 unknown이다.

기존 D0와 RH 요청 지연 시범의 v4 결과는 별도로 지원한다.
수학 설정의 `timing_mode=D0`는 새 공통 controller와 중복 지연시키지 않기 위한 값이다.
새 시간 의미는 `control.settings.timing_protocol`에 기록한다. 구버전 RH `timing_mode=D1`과 동시에 설정하면 거부한다.
이전 소스의 결과는 그 소스로 재생한다. 최종 held-out 추천이나 GUI의 신규 D1 설정 화면은 이번 범위에 포함하지 않는다.

## 검수 범위

초기 통합본에서 Python 3.11·3.14 각각 430개 테스트를 통과했다.
수정본 134회(기존 D0 36회, D1 기전/충전 84회, 강제 준비 실패 14회)를 재생·원시 지표 재구성과 대조했다.
이전 D0의 36개 물리 사건 digest를 보존했다.
이 수치는 기능 검수이며 모든 solver 요청 성공, 모든 환경의 0.5초 응답, 정책의 일반 우세를 뜻하지 않는다.
알고리즘별 차이와 기간/예산 설정은 별도 P4 파일럿 결과에서 보고한다.

최종 P4 검수(591회·실패 보존·교차로 대조 수정), 설정과 다음 실험 범위는 [P4 보고서](../docs/P4_REVIEW.md)에 있다.
