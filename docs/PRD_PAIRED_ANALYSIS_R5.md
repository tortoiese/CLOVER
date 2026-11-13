# R5 후속 구현: 실행 묶음의 공정한 paired 분석

이 계획은 [전체 PRD §13·R5](PRD_ALGORITHM_EXPERIMENTS_v2.md#13-통계-분석과-우세-판정)의 첫 구현 단계다. 실제 팀 지도 확정과 무관하게 분석 기반을 구현하되, 합성 E0 결과로 본 실험 완료나 알고리즘 추천을 선언하지 않는다. PR #13이 검토 중이므로 후속 분석은 별도 브랜치에서 진행한다.

## 구현·인수 범위

1. `comparison-plan`: 기존 prepared parent 또는 suite에서 명시적 조건·지도·반복·대조/후보·지표 목록을 생성한다. 자동 생성은 E0 탐색 초안이며 실제 sampling 설계의 사전 등록이 아니다.
2. `compare`: 저장된 manifest/result/raw를 읽고 입력·초기 상태·소스·환경·시간 모드·계산 설정·측정창을 검증한다. 소스/모드/설정이 다른 반복은 같은 조건에 합치지 않는다. 과거 자료는 원래 해시를 유지한 읽기 전용 분석이며 solver를 다시 실행하지 않는다.
3. 정책 차이는 후보−대조로 고정한다. 처리량·납기 위반율·에너지/완료·WIP·종료 미완료·충전 대기·관측된 시작P95·runner에 기록된 wall 비용·완료된 유한 batch makespan을 단위와 방향을 붙여 출력한다. 납기 대상 UID가 정책별로 다르면 해당 비율 비교를 보류한다.
4. 실패·부분 실행·미실행·누락을 전체 계획 분모에 남긴다. 완료된 비교쌍의 평균은 기술 통계로 표시하고, 예정된 쌍 하나라도 불완전하면 해당 추론을 보류한다. 납기 대상 없음·완료0의 비율은 null이며0으로 대체하지 않는다.
5. 프로젝트의 최소 게이트는 지도별 서로 다른 stochastic 수요10쌍, 지도 집계는 검토된 서로 다른 환경3개다. 이 숫자 자체가 독립성이나 충분한 검정력을 증명하지 않는다. E0·결정적 입력·동일 입력 반복·검열P95·표본 부족·미검토 장비는 신뢰구간을 보류한다.
6. 지도별 mean difference의 pointwise Student t 구간·양측 p값과 고정 seed의 paired percentile bootstrap 민감도를 함께 출력한다. 지도 집계는 지도 평균을 균등 가중하고 지도 단위를 resample한다. 같은 수요 seed가 지도 간 재사용된 경우 교차 군집 방법이 구현될 때까지 집계 추론을 보류한다.
7. 계획에 포함된 조건·정책·지표·지도/집계 전체를 한 Holm family로 취급한다. 계산 불가 가설도 보정 분모에서 제거하지 않는다. Holm은 p값 보정이며 개별 구간의 동시 보장을 뜻하지 않는다.
8. JSON·효과 CSV·해석 노트를 새 출력 폴더에 저장하고 기존 분석을 덮어쓰지 않는다. 자동 추천·동등성·확정적 우세 판정은 생성하지 않는다.

중간 parent checkpoint에서 아직 식별되지 않은 계획 슬롯도 별도 분모에 보존한다. 분석기의 source/환경과 계획·manifest/result/events 해시를 남기고, 계산 중 자료/소스 변경을 거부한다. 시작률은 취소·거절을 포함한 전체 도착 cohort와 함께 공개한다. t 근사·bootstrap의 가정은 반복 수 하한만으로 보장되지 않는다. 실행 방법과 지표 정의는 [PAIRED_ANALYSIS.md](../robot_factory_sim/PAIRED_ANALYSIS.md)를 따른다.

## 검수

- 알려진 차이의 평균/구간을 SciPy 기준과 대조하고 paired 전체 단위 bootstrap의 고정 seed 재현성을 확인한다.
- 실패/부분/미실행·결정적 seed 재명명·입력 변조·경로 이탈·중복 arm·소스/설정 혼합·지도 재명명·지도별 공통 seed·검열·0분모를 음성 회귀로 검사한다.
- 실제 작은 paired suite의 CLI 실행, 저장 raw 검증 및 JSON/CSV를 대조한다.
- 기존 E0 저장 자료에도 적용해 원래 소스를 변경하지 않고 기술 통계와 보류 사유가 나오는지 확인한다. 이 실행을 새로운 성능 반복으로 세지 않는다.

## 남는 작업

R1 실제 지도/trace import, R2/R3 튜닝·장비 비용 보정, R4 관측창·충전 반복·drain, 검열 생존 분석, 교차 군집 추론, 배포 가중치, 검정력/사전 효과 기준, 사전 등록된 E2/E3 및 R7 선택 규칙은 후속 게이트다. 이 분석기의 탐색적 구간·보정 p값만으로 최종 우수성이나 기본 정책 변경을 정당화하지 않는다.

방법 근거: [NIST paired interval](https://itl.nist.gov/div898/handbook/prc/section3/prc312.htm), [SciPy paired test](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_rel.html), [paired bootstrap](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html), [Holm 보정 정의](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html). 최소 반복 수·추론 보류 정책은 해당 문서의 정리가 아니라 본 프로젝트의 보수적인 구현 기준이다.
