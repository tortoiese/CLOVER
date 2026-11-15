# 본 실험 진행 검수 — 2026-11-15

확인 시각: **2026-11-15T15:56:53.523366+09:00**. 본 실험은 **종료되지 않았고 RUNNING**이다. 기존 PID 37509의 연속 실행을 확인했으며 중복 프로세스나 재실험을 시작하지 않았다.

- 종료된 입력 묶음: **324/921 (35.2%)**.
- 기록된 정책 결과: **1398/4,020**. 현재 실행 중 묶음에서 먼저 끝난 정책도 포함한다.
- 상태별 결과: `{'SUCCESS': 1325, 'TIMING_COVERAGE_VIOLATION': 73}`. SUCCESS는 관측창 실행 완료이며 고장0·성능 우세를 뜻하지 않는다.
- heldout 현황: `{'planned': 894, 'recorded_shards': 324, 'terminal_shards': 324, 'statuses': {'SUCCESS': 254, 'PARTIAL': 70}}`. 스트레스27묶음은 heldout 이후 실행한다.
- 디스크 여유: **17.63GB**. 준비 당시 전체 저장 계획치12.26GB와 예비2GB를 현재도 수용한다.

## 확인한 사항

동결 source·환경·하드웨어·study/작업 등록 SHA가 그대로이며 현재 기록의 정책별 scenario/trace/initial hash가 일치한다. 기록된 에너지 장부 오차 기준 초과는0건이다. 실행 코드·가중치·기한·예비시간·seed·시나리오를 바꾸지 않았다.

시간 계측 초과는 `{'publication cost exceeded prepaid allowance': 73}`로 기록됐다. 반영 비용이 미리 예약한0.2초를 넘겨 해당 run의 성능 추론을 보류하는 상태다. 일부 정상 조건에서도 발생했으므로 전체 실행 뒤 조건별 지원 범위/가드레일 판정에 반영한다. 실패를 성공으로 바꾸거나 제외한 뒤 우세를 주장하지 않는다. 비교 결과를 본 뒤 예산을 늘리거나 유리한 seed만 재실행하지 않는다.

## 이어서 진행

기존 supervisor가 heldout → stress → 등록 분석 순서로 계속 실행한다. 중단되지 않았으므로 resume/재시작 명령을 추가 실행하지 않았다. 실험 중에는 이 worktree의 Python을 변경하지 않는다. 예상 밖 평가기 오류나 디스크 예비 공간 부족은 중단 기록을 남기며 결과를 보존한다.

실시간 상태는 로컬 `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/outputs/CLOVER_MAIN_EXECUTION_20261114/STATUS.md`, `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/outputs/CLOVER_MAIN_EXECUTION_20261114/progress.json`, `/Users/kangjaemin/Documents/Codex/2026-09-19/new-chat/outputs/CLOVER_MAIN_EXECUTION_20261114/supervisor.log`를 확인한다. 이 문서는 위 시각의 검수 snapshot이며 자동 갱신 화면이 아니다.

[진행 snapshot](scenarios/main_study_20261114/evidence/execution_checkpoint_20261115.json), [준비 검수](MAIN_STUDY_READINESS_REVIEW_20261114.md), [실행 안내](MAIN_STUDY_RUNBOOK.md).

시간 반영 비용의 보완은 본 실험 완료 뒤 별도 소스/프로토콜로 검토한다. 현재 배치에는 변경된 결과를 섞지 않으며, 실패 빈도·조건별 지원 범위와 불완전한 비교의 보류 사유를 최종 분석에 포함한다.
