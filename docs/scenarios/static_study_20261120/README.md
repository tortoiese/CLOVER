# 고정 지도 시뮬레이션 실행 구성

2026-11-20. 강재민 1인 수행. [상위 계획 v0.2](../../PRD_SIMULATOR_SCENARIO_ADVANCEMENT_20261119.md)를 실행 코드로 옮긴 별도 합성 연구환경이다. 이전 한국 사례 288회와 합산하지 않는다. 기본 정책·가중치·목적함수·결정 시간은 변경하지 않는다.

## 무엇을 비교하나

모든 지도는 단층 60×40m, 로봇 8대, 작업 설비 6개, 충전소 1개다. 기존 3층 사례의 반복이 아니다. 물리 레일·건물 벽을 재현한 도면이 아니라 이동 가능한 노드와 통로를 정의한 그래프이며 장애물은 없다.

| 단계 | 조건과 목적 | 입력 | 운영 실행 |
|---|---|---:|---:|
| M1 D1 | 저부하·중간 급증·충전 부족 급증, 실제 계획 반영/시간 계약 | 3 | 12 |
| M1 D0 | 저부하/고부하 대표, 계산 지연 없는 기전 검사 | 2 | 8 |
| M2a | 부하 3종 × 충전 슬롯 3종, 평시 도착 | 45 | 180 |
| M2b | 같은 조건·같은 작업 수를 급증 형태로 재배치 | 45 | 180 |
| M3 | 다른 지도 3개 × 지정 조건 4종 × 독립 seed 5개 | 60 | 240 |
| 합계 | baseline / optimized / rh_milp_grid / rh_cpsat_linear | 155 | 620 |

입력 묶음에 같은 물리 조건의 D0/D1 기술검사가 별도 포함된다. 고유 수요 표본 수가 155라는 뜻은 아니다. M1 seed, M2 seed, 각 M3 지도의 seed를 분리했다. 같은 조건·지도·seed에서는 네 정책에 동일한 입력 파일을 쓴다.

부하는 λ_ref=0.08건/s의 0.6/1.0/1.3배다. 측정된 처리 용량이나 이용률 ρ가 아니다. 충전 슬롯 1/2/4개, 슬롯당 0.25battery-unit/s이므로 총 충전 용량도 함께 달라진다. 도착은 지수 간격의 평시 trace와, 같은 실제 작업 수·UID·우선순위·납기 여유를 유지하는 burst4다. 이때 순간 부하가 증가하며 총 수요는 유지된다.

## 지도 정의

좌표 단위는 px이고 1px=0.1m다. 정확한 노드·방향·폭은 생성된 JSON의 factory에 저장된다.

| 지도 | 고정 구조 | 해석 범위 |
|---|---|---|
| DEV_GRID_A | (100,200)–(300,200)–(500,200) 중심 통로, 위·아래 6설비, 충전소 (300,350) | 18조건 개발용 기준 |
| VAL_CORRIDOR_A | 좌우 설비군, (260,200)–(340,200) 폭 1.3m 공유 통로, 나머지 폭 5m, 왼쪽 충전 | 처음부터 좁은 통로가 있는 다른 구조 |
| VAL_BYPASS_A | 중앙 폭 1.3m 좌→우 단방향 + y=60의 폭 3.1m 우회 연결 | 방향과 우회 연결이 함께 다른 구조. 순수 우회 효과의 단일축 실험이 아님 |
| VAL_STATION_A | 설비가 x=420/540에 집중, 충전은 (80,350), 긴 접근 경로 | 설비·충전 분리와 이동거리 노출 |

K=1을 유지하므로 우회 지도에서도 알고리즘이 여러 경로 중 최적 경로를 고른다는 결론을 내리지 않는다. 지도 사이의 정책 성능을 직접 한 숫자로 섞기보다 각 지도 안에서 RH−optimized 차이를 비교한다.

## 실행과 게이트

Python 3.11 이상과 저장소 의존성을 설치한다. 아래는 저장소 루트에서 실행한다. 출력 경로는 반드시 새 경로를 사용한다. 입력 생성은 solver를 실행하지 않는다.

```sh
python scripts/materialize_static_scenario_study.py --output /absolute/path/static_inputs
python scripts/audit_static_scenario_study.py --bundle /absolute/path/static_inputs
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase M1_D1
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase M1_D0
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase readiness
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase M2a
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase M2b
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase M3
python scripts/run_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --phase report
```

실행 전 application Python과 실행 도구가 HEAD에 커밋된 상태여야 한다. 입력에는 코드·도구·설정·작업·SOC hash를 봉인한다. 다른 컴퓨터나 코드에서 실행하려면 그 환경에서 새 묶음/캠페인을 생성한다. 기본 수학 설정의 timing_mode=D0은 staged D1의 공통 수학 모델 설정이다. D1 지연 반영은 별도 decision settings를 전달해 켠다. 이 둘을 혼동해 timing_mode만 D1로 바꾸지 않는다.

readiness는 20회 성공, 원시 이벤트·입력 CAS·자원·작업·에너지 검사, 네 정책의 기록 재생, 두 RH 정책의 실제 primary prefix 반영, 충전과 납기 모집단 노출을 확인한다. 단순 READY 플래그로 다음 단계를 열지 않는다. 관측창에 충전·납기가 보이는 것은 정상상태나 충분한 정밀도를 보장하지 않으므로 M1 결과에서 창·시간/용량 예산을 별도로 검토한다. 변경이 필요하면 새 소스/설계/묶음으로 분리한다.

운영 검사는 호스트 lock으로 직렬 실행한다. run 900초/job 3600초 제한과 2GiB 디스크 여유 하한을 적용하고 gzip으로 결과·원시 이벤트·CAS를 보존한다. `--limit 1`은 짝지은 입력 1개(정책 4회)만 실행하며 전체 620회 분모를 줄이지 않는다. 재실행 시 NOT_STARTED인 job만 실행한다. 실패·중단한 job을 자동 재시도하거나 덮어쓰지 않는다. 다른 실행으로 인한 타이밍 오염을 피하기 위해 실험 중 네이티브 solver 테스트를 병행하지 않는다.

## 결과를 읽는 법

`campaign.json`에 미실행을 포함한 전체 상태, `readiness.json`에 기술 게이트 근거, 각 job 폴더에 압축 원시자료, `report.json`과 `REPORT.md`에 상태와 정책 차이를 남긴다. 계획 호출 시도·해 반환·feasible 보고·실제 prefix 반영은 서로 다른 개수다. worker 호출에 진입했다고 native solver 실행을 증명하는 것은 아니므로 반환된 native 시간도 별도 기록한다. prefix 반영은 작업 완료 수가 아니다.

주 지표는 처리량, 전체/긴급 납기, 시작 대기와 끝나지 않은 대기, 60초 backlog와 추세, 자원 대기, 에너지 장부, 계산 단계·메모리·fallback이다. 완료된 대기 P95 옆에는 미시작 개수와 열린 대기 나이도 같이 본다. 120초 초과 대기를 교착이라고 부르지 않는다. 에너지는 battery-unit이다.

M2/M3의 seed 5개는 탐색 실험이다. 유의성·최종 알고리즘 우승·실제 공장 SLA 충족을 주장하지 않는다. 독립 확인 실험 M4의 표본수·새 지도·선택 기준 등록은 후속이다. 실행 중 지도 변경과 기존 pause_admission 오류 수정도 별도 후속으로 남긴다.
