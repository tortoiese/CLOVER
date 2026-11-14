# 본 실험 전 보완 검수 결과

2026-11-14. 사용자의 **합성 연구환경 검증** 선택을 적용했다. 판정은 **합성 파일럿 실행 기반 검증 완료**다. 본 실험의 반복 실행·통계적 우세·환경별 추천이 완료됐다는 뜻은 아니다. 기본 optimized·휴리스틱 가중치·D0·기존 D1 v1 soft 규약·수학 목적은 유지했다.

최종 실험 코드: `89afcdcce55ee3b38106d571084ddd2001754f36`, source hash `192e456b789a4f940588631079177d75062770b5e374c81098d87141c4e360c5`. Python179개가 해당 Git HEAD blob과 일치했고 실행 전후 source가 같았다. 문서 후속 커밋은 이 실험 소스를 소급 변경하지 않는다. [기계 판정](scenarios/experiment_hardening_20261114/evidence/receipt.json), [실행 사용법](../robot_factory_sim/STUDY_PROTOCOL.md), [보완 PRD](PRD_EXPERIMENT_HARDENING.md)를 함께 본다.

## 수정한 문제

| 항목 | 구현·검수 결과 | 해석 경계 |
|---|---|---|
| R3 정상 계획 기한 | opt-in D1 v2. 2초에서 admission0.1초·publication0.2초를 먼저 확보하고 admission 전후/공개 직전 확인. 기한 이후 정상 계획 거절 | SimPy 주입 시간과 publication coverage의 계약. OS hard realtime이나 fallback 포함 전체 cycle2초 보장이 아님 |
| R3 지표 | accepted/timely accepted/late accepted·cycle 초과·pending·dispatch/auxiliary·P95/P99를 분리. 창 끝의 검열 요청을 분모에서 구분 | control 요청 수는 작업 수/독립 표본 수가 아님. pending 분위수/비율을0으로 채우지 않음 |
| R2 실패 입력 | RH snapshot을 RPC 전에 CAS JSON으로 보존. hash·경로·replay·export 검증. 반환 없는 hard timeout도 강제 실패 회귀에서 확인 | 관측 capture가 없으면 없는 입력을 생성하지 않음. export의 경로/모델 재생성은 분석 작업임 |
| R1 합성 입력 | 기존 v3에서 대표12가족×4phase=48조건 생성. trace·좌표/층/폭·SOC·납기·seed·설정·출처를 명시 | 기존 전체62조건을 모두 새 본 실험으로 실행한 것이 아님. 실제 공장 권장 제원/성능이 아님 |
| R4 관측 기간 | 공통 warmup~duration·도착 중단/drain 규약 검증. 완료 충전 세션과 구간별 처리율/WIP·fault/rescue 감사 | 고정 창의 관측/노출 PASS이며 정상상태/시스템 안정성 증명이 아님 |
| R5/R6 동결 | freeze/run-study와 registered analysis. train/validation/heldout/stress, 새 geometry/seed·source/환경/trace/설정/지표 잠금 | 이름·노드 ID·화면 크기 변경만으로 새 지도 인정 안 함. 기술 반복/설계 변형을 독립 표집으로 인정 안 함 |
| R5/R6 실패 보존 | 모든 case/arm 사전 기록·RUNNING checkpoint·evaluator 오류 뒤 중단·미실행 보존·새 출력 강제 | 프로세스 강제 종료 시 마지막 RUNNING은 남을 수 있음. 이때도 전체 계획/분모는 남음 |
| 보정 자료 무결성 | event 재구성뿐 아니라 scenario/trace/initial hash도 확인. 세션 전체 충전량과 정확한 측정창 에너지의 범위를 구분 | 자료의 일치 검사는 실제 공장 타당성이나 임시 reference μ의 보정을 대신하지 않음 |

## 최종 소스에서 실행한45회

- D1 v2: S01 공동배정, S04 혼합 SOC/충전, S08 캐빈2개, S11 격자/예산의4조건×baseline/optimized/RH MILP/grid CP-SAT linear/native CP-SAT의5정책 = **20회**. 각 phase에1조건을 둔 기능 검수이며 training/validation/heldout/stress라는 이름만으로 확증 실험이 되지 않는다.
- 개발 관측창: 대표12가족×training/validation = **24회**, D0 optimized만 사용했다. 모델의 에너지/자원/작업 장부와 정해진 창·충전 노출을 검수하는 중립 참조 실행이며 D1 알고리즘 우열 비교가 아니다.
- 별도 충전 노출: **1회**, D0 optimized,4설비/4로봇·배터리20·충전1슬롯/0.25 unit/s·초기 SOC60%·λ0.04 tasks/s·총7800초/warmup600초.

45회 모두 실행 창·장부 검수를 통과했다. D1의20회는 원시 지표·입력 일치·worker 해제를 확인하고, 저장된 결정으로 **20회 native 계산 없는 replay**도 모두 일치했다. 개발/충전의25개 calibration 보고서를 원시 stream에서 다시 재구성해 모두 일치했다. SUCCESS는 정해진 창의 실행 완료이며 모든 작업 완료나 정책의 성능 우세를 뜻하지 않는다.

D1에는 RH 입력37개가 남았다. dispatch는 trigger66·기한 관측 가능65·채택45/기한 내 채택45·fallback20·기한 뒤 채택0·관측 가능한 cycle 초과0·pending0·창 끝 검열1이었다. auxiliary는 trigger160·관측 가능154·채택154·검열6이며 dispatch 성과와 섞지 않는다. 미반환 timeout 보존은 강제 실패 회귀로 검사했으며37개 모두가 자연 발생 hard timeout 관측이라는 뜻은 아니다.

긴 충전 검수에서 창 안에 시작·종료하고 용량10% 이상 충전한 세션은 R0/R1/R2/R3 각각 **36/36/37/36회**, fault0·구조 에너지0이었다. 구간 처리량은2.25~2.55 tasks/min, WIP는5에서 시작해 종료10이었다. 여러 충전을 관측했다는 사실과 정상상태 증명은 구분한다.

S05 hotspot과 S10 지속 과부하에는 증가한 미완료/WIP가 남는다. 이는 감추거나 완료로 채우지 않는다. 특히 S10의 큰 backlog는 설계한 압박 상황의 결과다. finite-stream/transient 창의 기술통계이며 모든 시나리오가 정상상태라는 주장을 하지 않는다.

입력/결과/원시 이벤트/계획 CAS는 로컬 `outputs/CLOVER_EXPERIMENT_HARDENING_20261114_verified`에191파일·약129.6MB로 보존했다. [원시 파일 inventory](scenarios/experiment_hardening_20261114/evidence/raw_inventory.json), [48입력 index](scenarios/experiment_hardening_20261114/evidence/development_input_index.json), [D1 판정](scenarios/experiment_hardening_20261114/evidence/smoke_receipt.json), [개발/검증 창](scenarios/experiment_hardening_20261114/evidence/development_calibration_receipt.json)과 [검증 창](scenarios/experiment_hardening_20261114/evidence/validation_calibration_receipt.json)에 원본 위치와 SHA를 남겼다. 큰 전체 trace는 생성기로 재현하며 Git에는 입력 index·작은 smoke·핵심 판정/동결 파일을 둔다.

보정 입력 hash 검사를 강화하기 전 `9207a97`의45회는 별도 원본 폴더에 보존했다. 최종 소스에서 같은 전체45회를 다시 검수했다. 두 소스의90회는 독립 수요90seed가 아니며 통계 반복으로 합치지 않는다. 이전 E0/E1/R2 source·결과·실패 자료도 덮어쓰지 않았다.

## 회귀 검사와 알려진 한계

기능 구현 `9207a97`에서 Python3.11의600개 검사(기존 GUI 응답 시간 검사1개 제외), Python3.14 관련114개가 통과했다. 최종 무결성 보완 `89afcdc`의 관련24개는 두 Python 버전에서 각각 통과했다. 선택 모듈 Ruff(E4/E7/E9/F/I/E501·120자), planning/decisions/evaluation 실행 오류 검사와 diff check를 통과했다. [검사 요약](scenarios/experiment_hardening_20261114/evidence/local_tests.json)에 정확한 실행/로그 hash를 남겼다.

로컬 전체601개 최초 실행은600pass/GUI 응답1fail이었다. 실패는 명령30회의2초 응답 검사가 기다리다 종료되는 현상으로, 해당 검사 단독과 수정 전 `2a59111`에서도 재현했다. 당시 다른 앱의 CPU 부하도 관측됐다. GUI 코드·시간 제한을 바꿔 통과시키지 않았다. 로컬 GUI 응답1건은 기존 환경 문제로 분리하며, 전체601개가 로컬에서 한 번에 통과했다고 표시하지 않는다. 이런 환경의 wall time을 하드웨어 일반 성능 순위로 쓰지 않는다. 최신 교차 플랫폼 상태는 PR #17 checks를 따른다.

## 본 실험 전에 남는 연구 작업

1. 프로토타입 reference μ는 `UNCALIBRATED`로 유지했다. 각 환경의 중립 용량 근거와 λ/부하 표기를 검토해 고정해야 한다. 이 상태의 spec을 confirmatory로 바꾸면 동결을 거부한다. 관측창 PASS만으로 μ 보정 완료 표시를 하지 않는다.
2. 대표12가족의 단일 seed/설계 기하 변형을 넘어, 여러 지도·수요 반복의 본 실험을 사전 계획한다. 예제의 `layout_sampling_independent=false`와 E0 추론 보류를 유지한다. 최소10seed/3지도는 유의성이나 검정력의 보장이 아니다.
3. 현재 지원하는 설정 확정은 근거를 명시하는 a-priori 경로다. H40/t2를 최적이라고 확정하지 않았다. training/validation 결과로 자동 최적 profile을 고르는 receipt와 환경별 자동 추천/R7은 미구현이며, 해당 confirmatory 선택 방법은 명시적으로 거부한다.
4. frozen 분석은 사전 등록 일치를 보장한다. guardrail/실용 차이/선택 규칙 등록 자체를 자동 우세 판정으로 표시하지 않는다. 실제 held-out/stress 결과와 불확실성이 쌓인 뒤 선택 결론을 작성한다.

이번 작업으로 고친 것은 시간 규약·입력/실패 보존·관측/장부 검수·단계별 동결/실행/분석 기반이다. main 병합은 수행하지 않았다. 검증된 기능 commit을 고정해 합성 연구를 진행할 수 있다.
