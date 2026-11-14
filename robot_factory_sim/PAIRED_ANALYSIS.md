# 저장 실험의 paired 분석

동일한 공장·작업·초기 상태의 **후보−대조** 차이를 계산한다. 새 시뮬레이션이나 solver를 실행하지 않는다. `comparison-plan`은 검토할 분석 계획을 만들고 `compare`는 저장된 manifest/result/raw를 검증한 뒤 JSON·효과 CSV·해석 노트를 만든다.

아래 명령은 `robot_factory_sim`에서 실행한다.

```bash
python -m simulation.evaluation comparison-plan \
  --manifest /results/representatives_d0/manifest.json \
  --control optimized \
  --candidates baseline rh_milp_grid rh_cpsat_linear rh_cpsat_interval rh_cpsat_native \
  --output /results/analysis-plan.json

python -m simulation.evaluation compare \
  --plan /results/analysis-plan.json \
  --output /results/paired-analysis
```

분석 출력은 새 폴더여야 한다. 결과는 `analysis.json`, `effects.csv`, `README.md`다. CSV의 빈 값은0이 아니라 계산 불가/추론 보류다. 다른 조건·지도·반복·정책의 행을 섞어 평균 내지 않는다.

## 초안에서 검토할 것

- 자동 초안의 `purpose`는 항상 `e0`다. 이 모드에서는 반복 수와 관계없이 구간·p값을 만들지 않는다. E1의 실제 확정 입력/설정으로 수행한 training 분석만 `training`으로 바꾼다. E2/E3 사전 등록·검증 기능은 아직 없다.
- `blocks`는 조건·지도·반복·수요 seed·실제 suite를 명시한다. prepared parent의 수요 seed가 없는 결정적 조건은 finite batch, 그 외는 stream으로 초안을 만든다. 일반 suite의 수요 성격은 자동 추정하지 않아 `stochastic=false`, `demand_kind=unreviewed`다. 실제 생성 규약을 검토한 뒤 값을 정한다.
- `layout`은 실제 다른 환경을 식별해야 한다. 동일 factory를 이름만 바꾼 경우 집계 추론을 보류한다. 같은 입력이나 수요 seed를 바꿔 부른 반복도 독립 표본으로 세지 않는다.
- `hardware_id`는 검토한 동일 장비/실행 조건의 식별자다. `layout_sampling_independent`도 실험 설계에서 확인한 선언이다. 라벨 자체가 장비 동일성·독립성·대표성을 증명하지 않는다.
- suite 경로는 계획 파일 기준 상대/절대 경로다. 초안은 명시적인 절대 경로를 쓴다. 자료를 옮겨 경로를 수정하면 새 분석 계획 해시로 남긴다. 저장 파일의 원래 소스나 입력 해시는 바꾸지 않는다.

## 숫자의 의미와 보류 규칙

| 항목 | 정의·해석 |
|---|---|
| 처리량 | 측정창 내 완료 건수/분. 모든 batch를 같은 창에 끝내면 차이가0일 수 있음 |
| batch makespan | 선언된 유한 batch의 첫 도착~마지막 완료. 전체 작업이 측정창 안에 완료됐을 때만 정의 |
| 납기 위반율 | 저장된 due-job 집단 기준. 대상이 없으면 null, 정책별 대상 UID가 다르면 비교 보류 |
| 에너지/완료 | 측정창 소비 battery-unit/측정창 완료 건수. 같은 작업 cohort의 개별 에너지 추정이 아님. 완료0은 null |
| WIP·미완료 | 측정창 평균 WIP 및 관측 종료 미완료 수 |
| 충전 대기 | 끝나지 않은 대기의 경계까지 포함한 robot·s |
| 시작 P95 | 관측된 시작만의 P95. 미시작·취소/거절로 시작 cohort가 불완전하면 추론 보류 |
| 기록된 run wall | runner 실행 타이머 값. 사전 검사·최종 해제·저장을 포함하는 CLI 전체 비용이나 결정 지연과 다름 |

실패·부분 관측·미실행·누락을 모두 계획 분모에 보존한다. 완료된 비교쌍의 평균은 그 부분집합의 기술 통계이며 전체 성능 추정으로 부르지 않는다. 예정된 쌍 하나라도 불완전하면 그 집계의 추론은 보류한다. parent가 중간 checkpoint여서 어느 조건인지 아직 알 수 없는 계획 슬롯도 별도로 남긴다.

추론의 프로젝트 하한은 지도별 서로 다른 stochastic 수요10쌍, 지도 평균 집계는 검토된3개 환경이다. 낮춰서 구간을 강제할 수 없다. 이 하한이 정규성·독립성·충분한 검정력을 보장하지는 않는다. t 구간은 독립된 차이의 평균에 t 근사가 적절하다는 가정에 의존한다. 표본 분산이0이거나 수치적으로 거의0인 경우에도 거짓 정밀도를 피하기 위해 구간을 보류한다.

지도별 분석은 run pair 단위, 지도 집계는 균등 가중 지도 평균 단위다. 서로 다른 지도에 같은 수요 seed를 재사용한 경우에는 교차 군집 방법이 필요하므로 현재 집계 구간은 보류한다. 많은 작업을 독립 반복으로 세지 않는다.

구간을 계산할 수 있는 training 자료에는 pointwise Student t 구간·양측 p값과 고정 seed의 paired percentile bootstrap 민감도를 함께 제공한다. Holm은 계획의 조건·정책·지표·지도/집계 전체 family의 p값에 적용하고, 계산 불가 가설도 family 크기에서 빼지 않는다. Holm이 개별 구간을 동시에95%로 보정하는 것은 아니다. 이 도구는 자동 추천·동등성·우세 판정을 만들지 않는다.

NumPy는 headless 의존성에 포함된다. t 기반 추론은 SciPy가 필요하며 없으면 `SCIPY_UNAVAILABLE`로 보류한다. 기존 앱/legacy 설치에는 SciPy가 포함된다. 분석 결과에는 분석기 소스/환경과 입력 계획·manifest/result/events 해시를 기록하고, 계산 도중 자료나 분석기 소스가 바뀌면 저장을 거부한다.

방법 근거와 남은 범위는 [R5 상세 계획](../docs/PRD_PAIRED_ANALYSIS_R5.md)을 따른다. 실제 팀 지도·수요 보정, R2/R3 튜닝, R4 기간·충전 반복, 교차 군집/검열 분석, 사전 등록 E2/E3와 R7 추천은 별도 단계다.
