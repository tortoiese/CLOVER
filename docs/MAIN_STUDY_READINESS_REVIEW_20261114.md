# 합성 본 실험 준비 검수 결과

2026-11-14. **준비 판정 READY. 본 실험 성능 실행은0회**다. 이 판정은 정상 조건의 동결 실험을 시작할 수 있고 스트레스의 지원 한계를 기록할 수 있다는 뜻이다. 256노드에서 발견한 시간 계측 실패를 성공이나 우세로 취급하지 않는다.

최신 main `7172fde` 위의 구현을 검증했다. 실험 코드 `2e5cf4b4a5e19ae5bb067aace5ab5e19452f7b6f`, Python188개, source `fac6d32ab3cbc9168b6a945344b9d699da7012f8706432c3591abdd4bfa0c3ad`다. 원시 코드/입력/환경/판정은 [준비 판정](scenarios/main_study_20261114/evidence/receipt.json), [실제 readiness](scenarios/main_study_20261114/evidence/readiness.json), [동결 등록](scenarios/main_study_20261114/evidence/registration.json)에 있다. 이후 문서 커밋은 이 실행 소스를 바꾸지 않는다. [PR #21](https://github.com/tortoiese/CLOVER/pull/21).

## 무엇을 평가하는가

baseline·optimized·RH MILP·grid CP-SAT linear를 같은 입력/초기 SOC/평가창/2초 D1 규약에서 비교한다. S01/S04/S08/S11은 native CP-SAT도 비교한다. MILP와 linear는 공통 grid 표현의 backend 비교이며 native는 시간 표현·행동 범위도 달라 순수 solver 차이로 부르지 않는다. 본 실험은 운영 궤적 비교라 정책별 상태가 갈라진 뒤 snapshot도 달라진다. 완전히 같은 고정 Model/Hint의 순수 계산 비교는 별도 R2 경로를 사용한다. 기본 정책·가중치·수학 목적은 유지한다.

핵심6가족은 여유·통로 폭·충전 슬롯·평탄/버스트·캐빈 수·지속 부하다. 14조건 각각 독립 생성 지도6개×수요10seed다. 보조6가족은 공동배차·교차로·설비 집중·로봇 속도 차이·복구·64노드다. 스트레스는256노드·강한 복구 부하·긴 충전 창이다. 전체62조건 조합을 모두 실행하는 설계가 아니다.

| 본 실험 구분 | 입력 블록 | 정책 실행 |
|---|---:|---:|
| 핵심14조건 | 840 | 3660 |
| 보조6조건 | 54 | 234 |
| 스트레스3조건 | 27 | 126 |
| 합계 | 921 | 4020 |

한 블록은 같은 지도·수요·초기 상태를 공유한4~5정책이다. 지도는 같은 유형의 토폴로지 안에서 x/y 격자 간격을 독립 변형하고 초기 SOC를35~80%에서 정한다. 추론 범위는 이 합성 생성분포다. 정책/폭/슬롯/캐빈 비교에 입력을 사후 조정하지 않는다. 지도와 정책 순서는 사전 고정한 무작위 순서다.

## 부하를 정한 근거

고정 D0 optimized의 포화 수요에서 training3회·다른 지도의validation2회를 관측했다. 마지막3구간에 backlog와 양의 완료량이 있고 작업/자원/에너지 감사·fault/rescue 기준을 만족해야 한다. 이는 유한 창의 참조 서비스율이며 물리적 최대 용량이 아니다. 새 지도의 자유주행+처리 시간 비율로 입력만 사용해 이전하고, 알고리즘별로 λ를 다시 맞추지 않는다.

| 가족 | 참조 평균(tasks/s) | 표준편차 | validation 관측/예측 비 |
|---|---:|---:|---:|
| S00 | 0.140889 | 0.005389 | 0.962~1.017 |
| S02 | 0.101333 | 0.004000 | 0.928~1.027 |
| S03 | 0.054222 | 0.008572 | 1.057~1.299 |
| S04 | 0.041778 | 0.001540 | 0.971~1.001 |
| S05 | 0.180444 | 0.012602 | 1.022~1.070 |
| S06 | 0.274667 | 0.009615 | 0.985~0.993 |
| S07 | 0.097778 | 0.013684 | 0.960~0.974 |
| S08 | 0.064444 | 0.005389 | 0.869~0.930 |
| S09 | 0.149778 | 0.004073 | 0.986~1.014 |
| S10 | 0.097778 | 0.002776 | 0.988~1.015 |
| S11 | 0.096444 | 0.006012 | 1.030~1.044 |

S01은8작업의 유한 batch라 λ·μ·ρ가없다. S00은 참조의0.25배, S02/S04/S08은0.9배를 수준 간 공통 사용한다. S06은0.7배의 같은 실현 작업 수를 압축하고, S10은1.1/1.3배를 사용한다. 실제 통로/캐빈 노출·혼잡·과부하 여부는 결과로 확인한다. [55회 근거](scenarios/main_study_20261114/evidence/capacity_summary.json).

## 실제 통과 근거와 알려진 한계

- 최종 소스에서 참조55회·개발 관측창72회·D1 canary57회, 총184회 사전 점검을 수행했다. 이를184개 독립 표본으로 세지 않는다. 본 실험의heldout/stress 실행 폴더는 아직 만들지 않았다.
- 개발 관측창72회는 모두 PASS이며 D0 optimized로만 검수했다. S04는 측정창 안에서 시작·끝나고 용량10% 이상 채운 충전을 로봇별2회 이상 요구한다. [개발 장부](scenarios/main_study_20261114/evidence/development_summary.json). D1의training/validation 전 정책 실행312회를 완료했다는 뜻은 아니다.
- 정상 canary52회는 모두 full observation·장부·worker 해제 검수를 통과했다. 대조 optimized와 RH 후보의 fault/rescue는0을 요구한다. baseline의 반응형 정책으로 생긴 서비스 고장/구조는 원시 장부에 남기고 안전 추천을 보류한다. S04 baseline의 실제 충전량이0이면 결합 미검증으로 READY를 막는다. 별도256노드5회까지 포함한 저장 결정57회 replay가 원시 결과와 일치했다. 각 RH backend는 정상 canary에서 채택된 계획이 있다. 기한 이후 정상 계획 채택0회다.
- 전체 canary의 fault 합계45, 구조 에너지 합계126 battery-unit, 관측된 worker RSS 최댓값193.05 MiB다. RSS는 주기적 관측값이며 정확한 순간 최대값은 아니다. [요청별 요약](scenarios/main_study_20261114/evidence/canary_telemetry.json).

충전 결합 수정 후에도 baseline의 반응형 충전은 일부 압박 조건에서 고장/구조를 남긴다. 같은 입력으로 D0 baseline을 추가2회 진단했고 D0에서도 다음 고장이 관측됐다. 따라서 충전 적용이 아예 누락된 결합 오류와 기존 정책의 에너지/예약 판단 한계를 구분하며, baseline을 안전한 정책으로 추천하지 않는다. 이2회는 독립 수요 반복이나 본 실험으로 세지 않는다.

| 조건 | D0 고장 | D1 고장 | D0 충전(unit) | D1 충전(unit) |
|---|---:|---:|---:|---:|
| S04 | 23 | 19 | 486.90 | 415.30 |
| S09 | 26 | 26 | 687.06 | 656.41 |

[추가 모드 진단](scenarios/main_study_20261114/evidence/charging_mode_comparison.json). 고장 수만으로 우열을 정하지 않고 처리량·미완료·에너지·노출을 함께 본다.

256노드의 지원 한계는 다음과 같다. 시간 반영 실패는 실패 상태 그대로 남기며 해당 성능은 추론하지 않는다. 정상 조건의 허용 기준이나2초 예산을 늘리지 않았다. 임의 오류·누락·장부 불일치는 여전히 READY를 막는다.

| 정책 | 스트레스 canary 상태 | 해석 |
|---|---|---|
| rh_milp_grid | TIMING_COVERAGE_VIOLATION | 성능 추론 보류 |
| rh_cpsat_linear | TIMING_COVERAGE_VIOLATION | 성능 추론 보류 |
| rh_cpsat_native | TIMING_COVERAGE_VIOLATION | 성능 추론 보류 |

v2 관측 시간이 끝나는 경우에는 `OBSERVATION_BUDGET_EXCEEDED`와 fallback/HOLD를 기록·재생한다. 완료되지 않은 관측이나 solver 입력을 만들지 않는다. v1의 soft 시간 규약은 유지한다. 충전을 기다리는 로봇의 이전 작업 상태가 남는 문제와 충전 요청보다 새 작업을 먼저 배정하는 D1 결합 문제도 수정했다. 정책 임계치/목표/가중치를 바꾸지 않고 기존 idle의 충전 우선 순서를 보존하며, 대기 작업이 있는 실제 충전·작업 완료·고장/구조0·replay 회귀로 확인했다. 과거 실패한 준비3묶음은 원래 source·원시 기록·실패 사유와 함께 별도 보존했다. 이를 최종 표본에 합치거나 성공으로 수정하지 않았다.

## 분석과 자원 잠금

일반 핵심 조건은 처리량+0.1 tasks/min 초과가 실용 개선 기준이다. S06은 긴급 작업 납기 위반율5%p 초과 감소가 주 기준이며 처리량 감소0.1 tasks/min 이내를 함께 요구한다. 전체 납기 악화2%p·완료 작업당 상대 에너지 증가10% 이내와 안전/완결성 guardrail도 확인한다. 정책별 긴급 작업 분모가 다르거나0이면 그 비율의 추론을 보류한다.

지도 안의10seed paired 차이를 평균하고 지도 사이 추론을 한다. 핵심150가설에 실용 기준을 귀무값으로 하는 Holm 보정을 고정했다. t/bootstrap 구간은pointwise이고 표본 수만으로 검정력·분포 가정이 보장되지 않는다. 보조/스트레스는 기술통계다. 차이가 불확실하면 무결론을 허용하고 유리한 seed만 추가/삭제하지 않는다. 자동 profile 선택과 환경별 추천/R7은 본 실험 이후의 별도 작업이다.

실측 canary에서 작업 수·정책·가족·그래프·로봇 수에 맞춰 확대한 계획치는 **wall 117.8시간, 저장 12.26GB**다. wall2배/storage3배 여유가 포함되며 보장이 아니다. 준비 당시 여유 18.36GB와2GB 예비 공간을 확인했다. 정책당900초·블록6000초, 직렬 실행1개, 블록 사이 디스크 중단 규칙을 적용한다. 실행 기간이 긴 배치 실험이다.

결과·이벤트·계획 CAS는 XZ로 전체 바이트를 보존한다. 기존 gzip도 읽는다. CRC/논리 SHA와 저장 SHA를 구분해 감사·replay·모델 export를 검증했다. 최종 준비 출력 전체 1588파일, 저장 249.4MB의 [파일 inventory](scenarios/main_study_20261114/evidence/raw_inventory.json)가 있다. 본 실험의 원시 자료 용량과는 구분한다.

## 바로 실행할 위치

작업 디렉터리: `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/work/clover_main_study_ready_20261114/robot_factory_sim`

Python: `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/work/clover_prd_gate311_20261026/bin/python`

bundle: `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/outputs/CLOVER_MAIN_STUDY_20261114_v4/bundle6`

동결 study hash: `d6c0c3ae7b523fcf314f19b0eb679907b8305813cde3396f134b7309448af441`

로컬 실행 파일 `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/outputs/CLOVER_MAIN_STUDY_20261114_v4/RUN_MAIN.md`, [실행/재개/수집 안내](MAIN_STUDY_RUNBOOK.md), [정확한 argv](scenarios/main_study_20261114/evidence/commands.json)를 사용한다. 같은 명령을 반복하면 새 블록으로 이어가며, 실패/부분 기록을 덮어쓰지 않는다. 수집은 모든 예정 블록이 있어야 하고 변경·중복·누락을 거부한다. [실제 CLI 차단 검수](scenarios/main_study_20261114/evidence/negative_cli_v4.json)와 [XZ 감사/재생](scenarios/main_study_20261114/evidence/positive_cli_v4.json). 준비 차단은 최종 소스의 작은 fixture에서, 누락 수집 차단은 실제 본 실험 bundle에서 확인했다. 본 실험은 실행하지 않았다.

코드 검수: 최종 수정 관련 Python3.11 108개·3.14 109개 통과, Ruff/diff 검사 통과. 앞선 전체3.11 검수는621개 통과/기존 GUI 응답시간1건 제외였고, 실험 코드2e5cf4b의 Ubuntu/macOS/Windows×Python3.11/3.14 여섯 CI 조합은 각각632개 전체 검사를 통과했고 unhandled thread warning은0이었다. [코드 CI 근거](scenarios/main_study_20261114/evidence/code_ci.json)와 [실행 로그](https://github.com/tortoiese/CLOVER/actions/runs/43600291866/attempts/2)를 따른다. 최초 GUI 응답 시간/비동기 수락 검사2건의 실패와 재실행 근거는 [처음 CI 기록](scenarios/main_study_20261114/evidence/code_ci_initial_v4.json)에 보존한다. 시험 제한 시간을 바꾸지 않았다. 문서 후속 커밋의 체크는 PR #21에서 확인한다. 기본값을 바꾸거나 본 실험을 미리 실행하지 않았다.
