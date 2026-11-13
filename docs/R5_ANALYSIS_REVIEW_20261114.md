# R5 paired 분석 기반 검수

작성일: 2026-11-14 KST. [R5 계획](PRD_PAIRED_ANALYSIS_R5.md)의 첫 구현·인수 기록이다. 구현 코드는 `691458e3b340c1e66701046dc5380459b236b72b`, 분석기 Python 소스 해시는 `75016001011187ca9bf4272169911a5698770bf32c87800f6690c280312d5f3a`다. Git object와 실제170개 Python 파일의 해시를 대조했다.

## 구현과 검수 결과

`comparison-plan`으로 명시적 조건·지도·반복·정책·지표 초안을 만들고, `compare`가 manifest/result/raw를 검증해 JSON·CSV·해석 노트를 출력한다. 원래 시뮬레이션 코드를 실행하거나 solver를 다시 풀지 않는다. 분석기의 소스/환경과 입력 해시도 별도로 남긴다. 사용법은 [PAIRED_ANALYSIS.md](../robot_factory_sim/PAIRED_ANALYSIS.md)에 있다.

| 검수 | 결과 |
|---|---|
| Python3.11 전체 회귀 | 553 passed, 실패/오류/skip0 |
| Python3.14 새 분석 회귀 | 51 passed, 실패/오류/skip0 |
| 새 모듈 lint | E4/E7/E9/F/I/E501 통과,120자 기준 |
| planning/decisions/evaluation 실행 오류 lint | E9/F63/F7/F82 통과 |
| 원래 저장 자료 | 283건의 원본 result 해시 일치 |
| paired 분석 | 9개 소스/모드 묶음의231건,33개 block |
| 단일 정책 자원 대조 | baseline52건 별도 유지 |
| 새 성능 시뮬레이션 | 0회 |
| E0 구간·p값 | 0개. 보류 정책이 정상 작동함 |

회귀는 paired t의 SciPy 기준값·고정 bootstrap, Holm의 계산 불가 가설 분모 유지, 실패/부분/미실행, 중복 입력·반복, 소스/설정/관측창 혼합, 지도 재명명·지도 간 공통 seed, 시작 검열·취소/거절, 납기 대상 UID 차이,0분모, 미완료 batch, 자료/분석기 변경, 원시 로그 손상·경로 이탈, 출력 덮어쓰기 방지를 포함한다. 모델 실행을 사용한 CLI 인수와 JSON/CSV 무결성 검사도 통과했다. 이는 통계 구현 검증이며 실제 공장 성능의 실증은 아니다.

근거: [자료 재분석 판정](scenarios/r5_analysis_20261114/receipt.json), [사전 검수 구성](scenarios/r5_analysis_20261114/protocol.json), [로컬 검사·JUnit 해시](scenarios/r5_analysis_20261114/local_validation.json).

## 기존283건을 어떻게 사용했는가

| 저장 묶음 | 모드 | 정책 실행 수 |
|---|---|---:|
| 최초 대표12조건 | D0 | 84 |
| 최초 대표6조건 | D1 | 42 |
| 최초 혼합SOC | D1 | 7 |
| IPC 보완 후 대표3조건 | D0 / D1 | 21 / 21 |
| main69bb 통합 후 대표3조건 | D0 / D1 | 21 / 21 |
| PR #14 순회 분리 후 다층 | D0 / D1 | 7 / 7 |
| paired 분석 소계 | 소스·모드별 분리 | **231** |
| 나머지 조건 baseline·충전 연장 대조 | D0 | **52** |

9개 분석에서 대조는 `optimized`, 후보는 baseline·legacy MILP·RH MILP·세 CP-SAT으로 고정했다. 다른 코드·예산·D0/D1을 하나의 통계 집단에 합치지 않았다. baseline만 있는52건에는 실행하지 않은 후보 점수나0점을 만들지 않았다.

[효과 요약 CSV](scenarios/r5_analysis_20261114/effect_summary.csv)는 지도별 조건·후보·지표1584행이다. 한 run을 지표·후보별로 펼친 기술 통계 행이며1584개의 독립 실험이 아니다. [baseline 대조 목록](scenarios/r5_analysis_20261114/unpaired_reference_runs.json)도 별도로 보존했다. 전체 계획·분석 JSON·집계 행·노트는 로컬 `outputs/CLOVER_R5_analysis_20261114/`에 있으며 Git에는 판정·해시·요약만 넣었다.

모든 값은 기존 합성·보정 전 E0 자료다. 단일 반복/결정적 입력이므로 신뢰구간·p값·우수성 판정은 출력하지 않았다. 최소10쌍/3환경 규칙 자체도 충분한 검정력이나 독립성의 증명이 아니다.

## 지표를 구분해야 하는 실제 예

최초 S01의 사전 지정 결정적 batch는 모든 정책이 같은8건을180초 안에 완료했다. 따라서 측정창 처리량은 모두2.667건/분이다. 마지막 완료 시각은 다음과 달랐다.

| 정책 | 완료/유입 | 첫 도착~마지막 완료 |
|---|---:|---:|
| baseline | 8/8 | 57.1s |
| optimized | 8/8 | 57.1s |
| 배정 legacy MILP | 8/8 | 28.0s |
| RH MILP grid | 8/8 | 23.5s |
| CP-SAT 공통 선형 | 8/8 | 23.5s |
| CP-SAT 고정 격자 interval | 8/8 | 23.5s |
| CP-SAT native | 8/8 | 57.1s |

RH MILP의 batch 완료 시간 차이는−33.6s지만 처리량 차이는0이다. 이를 처리량 개선율로 바꾸지 않는다. native는 이0.25초 solver·30초 horizon 기능 설정에서 계획 채택0회·fallback3회였다. 물리 성과는 fallback까지 포함한 기록 정책의 실행 결과이며 pure solver 품질 순위가 아니다. 다른 대표조건에서는 native 계획 채택도 있었다. per-decision build/solve/validate/commit·RSS·fallback 분석은 기존 profile/raw와 R2/R3 검수에서 함께 확인해야 한다.

최초 S00 여유 조건의 RH MILP 처리량 차이는0, S10의 λ0.26 조건에서는 휴리스틱5→MILP8건/분이었다. 이 값 역시 단일 입력·짧은 E0의 기술 통계다. favorable 사례만을 최종 알고리즘 선택 근거로 쓰거나 정상 상태 용량·통계적 유의성으로 발표하지 않는다.

## 완료 경계와 다음 단계

| 항목 | 상태 |
|---|---|
| R5 v1 읽기 전용 검증·paired 효과·표준 구간/민감도/보정·보류 규칙·산출물 | 지원 범위 구현·기초 검수 완료 |
| 실제 E1 반복 자료의 효과 크기·불확실성 추정 | 미완료. 이번 E0에는 구간을 만들지 않음 |
| 교차 군집·검열 생존 분석·배포 가중치·검정력/사전 효과 기준 | 후속 구현·실험 설계 필요 |
| R1 실제 팀 지도/trace·λ 확정 | 미완료 |
| R2/R3 solver/비용 튜닝, R4 기간·충전 반복·drain | 미완료 |
| 사전 등록 E2/E3·R7 환경별 추천 | 미완료 |

기본 휴리스틱·가중치·MILP/CP-SAT 모델·기본 D0는 바꾸지 않았다. 원격 main의 PR #14 이력은 `b14cbe9`로 재작성되어 있었지만 검수 기준 `3dfb4ba`와 파일 tree는 같았고 가상 병합 충돌도0이었다. 상위 PR #13이 열린 상태이므로 후속 분석 PR은 그 브랜치를 기준으로 분리한다. main이나 PR #13을 이 작업에서 병합하지 않는다.
