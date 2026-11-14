# R2 동결 snapshot 비교 검수

2026-11-14. [사전 계획](PRD_FROZEN_SNAPSHOT_R2.md)에 따라 MILP/CP-SAT linear가 같은 관측·후보·route·정수 제약·두 목적·시작해를 사용하도록 구현했다. `freeze-snapshot`과 `benchmark-snapshot` CLI 및 기존 격리 worker의 평가 명령을 추가했다. 일반 `Backend.solve` 호출의 모델·목적·시간 예산 규약, 기본 optimized·가중치·기본 D0는 유지한다.

## 검수 범위

- 코드 동결: `f43908ad1516a8064c02c1ce76024ec117eea2fb`, 앱 Python173개가 Git blob과 일치했다.
- runtime source hash: `04272705152b02292f170d76974ebb6ba365a625edf5dd4065f16b269449252a`.
- Python3.11 전체574검사, Python3.14 관련44검사 통과. 신규21개는 실제 저장 입력·worker/CLI·공통 최적 목적, 손상/영역/시작해 불일치, 예산/반복 간 증명 불일치, 늦은 해·기동 실패·입력/source 변경·해제/분모 보존·전체 호출 계측을 검사한다. [로컬 판정](scenarios/r2_snapshot_20261114/publication.json)에 JUnit 해시를 남겼으며 교차 플랫폼 최신 결과는 후속 PR checks를 따른다.
- 기존 계획/D1/GUI 회귀와 lint·실행 오류 검사·diff check를 통과했다. 과거 E1 60회 원본을 바꾸거나 새 소스에 재귀속하지 않았다.

## 48arm 실행과 감사

관측4개×backend2종×예산0.25/2초×기술 반복3회,48arm을 단일 Mac arm64·Python3.11.14에서 직렬 실행했다. 원본은 PR #16의 h40_t2 MILP 기록이며 최초 worker 반환 입력 중 jobs·available robot이 있는 관측을 선택했다. 두 관측은 원래 STALE_AVAILABILITY로 거절됐던 계획이다. accepted/좋은 목적값을 선택 기준으로 사용하지 않았다.

수정 계측 소스에서 전체46.97초,48arm 모두 실행했다. **기한 내 OPTIMAL36·FEASIBLE9, 늦은 유효 해3**이다. 원시 실행 상태는 PARTIAL, 별도의 감사 판정은 PASS다. 타이밍 실패를 성공으로 바꾸지 않았고 유리한 결과를 얻기 위한 특정 arm 재시도는 하지 않았다. 새 closed-loop 시뮬레이션0회·독립 수요 반복0회이며 CI/p값/최종 추천도 생성하지 않았다.

최초 소스 `8327f84`의48arm 검수 후 내부 report timer가 마지막 action decode 전에 끝남을 확인했다. [최초48arm 증거](scenarios/r2_reported_timing_20261114/publication.json)와 [계측 보완 기록](scenarios/r2_reported_timing_20261114/timing_scope_correction.json)을 그대로 보존했다. 평가 RPC에서 전체 Backend.solve 호출 전후를 측정하고 같은 시작 시각을 deadline에 전달하도록 고친 뒤, 전체48arm을 새 소스·출력 경로에서 재실행했다. 두 묶음의 입력·모델·시작해 해시는4관측 모두 같았다. 누적96arm 중 여기의 표는 **수정본48arm만** 사용하며 시간 결과를 합치지 않는다.

[전체 감사 판정](scenarios/r2_snapshot_20261114/receipt.json), [48arm CSV](scenarios/r2_snapshot_20261114/summary.csv), [실행 상태](scenarios/r2_snapshot_20261114/execution.json), [프로토콜](scenarios/r2_snapshot_20261114/protocol.json), [동결 입력](scenarios/r2_snapshot_20261114/snapshots/)을 보존했다. 수정본 원시 plan/worker 파일 약13.7MB는 workspace `outputs/CLOVER_R2_snapshot_benchmark_20261114_complete_call`에, Git에는 이전 묶음까지 포함한 동결 입력·manifest·판정·해시·CSV 약2.47MB를 넣었다. 별도 장비에서는 원시 묶음을 전달받거나 같은 코드/입력으로 재실행해야 한다.

감사는48개의 입력/모델/시작해 hash·완전한 계획의 독립 제약 검사·시간 분류·해제·계획 분모·source freeze를 확인했다. 모든 보고된 OPTIMAL 목적은 같은 관측 안에서 일치했다. S04/S08/S11은 양쪽 backend가 같은 최적 목적을 증명했으며 S01은 이번 예산에서 CP-SAT만 증명했다. 작은 별도 정답 fixture에서도 두 backend의 parity를 검증했다.

## 관측된 차이

아래는2초 예산, 같은 관측의 기술 반복3회 중앙값이다. 시간은 **기동·공통 모델 준비·IPC·독립 계획 검사를 제외한 prebuilt backend wall-second**다. 공장 전체 처리량이나 D1 전체 결정시간이 아니다.

| 고정 관측 | MILP 상태·목적 쌍 | MILP 시간 | CP linear 상태·목적 쌍 | CP 시간 |
|---|---|---:|---|---:|
| S01 배정, 작업4·가용 로봇1 | FEASIBLE (6,1324) | 1.24495 | OPTIMAL (4,6305) | 0.64018 |
| S04 혼합 SOC, 작업1·가용4 | OPTIMAL (0,1351) | 0.01704 | OPTIMAL (0,1351) | 0.16368 |
| S08 다층, 작업4·가용1 | OPTIMAL (6,12178) | 0.32310 | OPTIMAL (6,12178) | 0.09738 |
| S11 64노드, 작업1·가용4 | OPTIMAL (0,3086) | 0.05835 | OPTIMAL (0,3086) | 0.18401 |

목적은 첫 비용을 먼저 최소화하고 같을 때 두 번째 비용을 비교하는 사전식 쌍이다. 첫 값은 가중 미배정 벌점이며 작업 개수가 아니다. 따라서 S01의 (4,6305)는 (6,1324)보다 우선한다. 두 번째 숫자가 크다는 이유로 CP 해를 더 나쁘다고 해석하면 안 된다.

0.25초에서는 S08의 CP가 세 번 모두 OPTIMAL (6,12178), MILP는 FEASIBLE (9,4369)이었다. S04/S11은 양쪽 모두 두 예산에서 같은 최적 목적을 증명했다. S01 0.25초의 CP는 세 번 모두 FEASIBLE (6,1324)을 약0.258초 중앙값에 반환해 `SOFT_BUDGET_EXCEEDED`로 남겼고, MILP는 같은 목적의 FEASIBLE을 기한 안에 반환했다. 원시 해와 초과 시간을 유지하며 이 CP3건을 기한 내 성능에 합치지 않았다.

이번 고정 S04/S11에서 MILP가 같은 목적을 더 빨리 증명했으므로 계산 비용의 차이는 관측했다. 그러나 [이전 closed-loop](E1_SETTINGS_PILOT_REVIEW_20261114.md)의 S04에서는 RH의 충전 대기가 optimized보다 길었다. 한 관측의 계산이 빠른 것과 실제 기간 전체의 처리량·충전 운용이 좋은 것은 별도로 검증해야 한다.

## 해석 한계와 다음 단계

이번 표본은 worker가 입력을 반환한 기록에 조건부다. 입력이 남지 않은 hard timeout·여러 상태/지도·지속 과부하를 대표하지 않는다. 관측당 기술 반복3개를 수요 seed3개로 세거나4관측을48개 공장으로 세지 않는다. 실행 장비의 파일 캐시/부하에 따른 시간 변동도 포함한다. 어떤 시나리오 가족 전체에 MILP/CP를 추천할 근거로 쓰지 않는다.

공통 route/model/hint 비용은 동결/재구성 단계에, 선택 backend 기동·독립 검사와 IPC를 포함한 전체 요청 왕복은 별도로 기록했다. 요청 왕복은 IPC 단독 시간이 아니다. prebuilt report의 `build_seconds`는 원래 전체 model 생성 비용이 아니며 manifest의 공통 준비 비용과 CSV의 compile/search 범위를 함께 읽어야 한다. 일부 report 하위 시간은 중첩되므로 모두 합하지 않는다. RSS는 worker의 샘플 peak이며 부모/장비 전체나 순간 peak 보장이 아니다. 원래 실행 source hash와 새 동결 생성 source hash는 구분해 보존했다.

R2의 공통 snapshot 비교 기반과48arm 기능·비용 감사까지 완료했다. 다음은 R3의 D1 검증·공개 여유 배분, 미반환 timeout 관측까지 남기는 입력 관측 범위 확대, 더 다양한 상태와 실제 지도/trace·λ·기간/충전 순환 보정 및 복수 지도/seed training이다. native 표현 대조, E2/E3·환경별 최종 선택 규칙은 미완료다. 이번 결과로 기본 정책이나 모델 계수를 변경하지 않는다.
