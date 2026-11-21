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

운영 검사는 호스트 lock으로 직렬 실행한다. run 900초/job 3600초 제한과 2GiB 디스크 여유 하한을 적용하고 XZ로 결과·원시 이벤트·CAS를 보존한다. `--limit 1`은 짝지은 입력 1개(정책 4회)만 실행하며 전체 620회 분모를 줄이지 않는다. 재실행 시 NOT_STARTED인 job만 실행한다. 실패·중단한 job을 자동 재시도하거나 덮어쓰지 않는다. 다른 실행으로 인한 타이밍 오염을 피하기 위해 실험 중 네이티브 solver 테스트를 병행하지 않는다.

초기 M1 저장 설정은 gzip이었다. 첫 고부하 조건의 arm당 약15~18MB를 남은600회에 적용하면 현재 디스크 예산을 초과하므로, 물리·수요·solver·시간 설정을 유지하고 실행 후 압축만 XZ로 바꾼다. `repack_static_canaries.py`는 이 변경에 한해 성공한 M1 20회의 원본을 수정하지 않고 별도 캠페인으로 복사한다. 원래/새 입력과 실행 도구의 동일성, 모든 저장 바이트 hash와 압축 해제한 원본 hash·크기를 확인하고 `storage_copy.json`에 도구 hash와 출처를 남긴다. 복사된 M1은 추가 solver 실행이나 독립 표본이 아니다. 새 캠페인에서도 원시 감사·기록 재생·디스크 예산 readiness를 다시 통과해야 한다. 한 번의 연구 실행을 재압축한 사본은 표본수에 중복 합산하지 않는다.

## 결과를 읽는 법

`campaign.json`에 미실행을 포함한 전체 상태, `readiness.json`에 기술 게이트 근거, 각 job 폴더에 압축 원시자료, `report.json`과 `REPORT.md`에 상태와 정책 차이를 남긴다. 계획 호출 시도·해 반환·feasible 보고·실제 prefix 반영은 서로 다른 개수다. worker 호출에 진입했다고 native solver 실행을 증명하는 것은 아니므로 반환된 native 시간도 별도 기록한다. prefix 반영은 작업 완료 수가 아니다.

주 지표는 처리량, 전체/긴급 납기, 시작 대기와 끝나지 않은 대기, 60초 backlog와 추세, 자원 대기, 에너지 장부, 계산 단계·메모리·fallback이다. 완료된 대기 P95 옆에는 미시작 개수와 열린 대기 나이도 같이 본다. 120초 초과 대기를 교착이라고 부르지 않는다. 에너지는 battery-unit이다.

M2/M3의 seed 5개는 탐색 실험이다. 유의성·최종 알고리즘 우승·실제 공장 SLA 충족을 주장하지 않는다. 독립 확인 실험 M4의 표본수·새 지도·선택 기준 등록은 후속이다. 실행 중 지도 변경과 기존 pause_admission 오류 수정도 별도 후속으로 남긴다.

## 2026-11-20 검수와 실행 상태

M1 기술검사20/20 SUCCESS, G1 PASS, 원시자료20개 감사와 기록 재생4/4 일치다. [검수 요약](evidence/implementation_review_20261120.json)과 [readiness 영수증](evidence/readiness_20261120.json)을 확인할 수 있다. XZ 복사는 같은20회 실행의 저장 형식 전환이며 총 운영 실행수에 다시 더하지 않는다.

이 검수를 통과한 `7445390` 분리 checkout에서 M2a를 시작했다. M2a→M2b→M3는 직렬 실행하며 실패·예산 부족·RH native 사용/primary 반영0이 있으면 후속 단계를 보류한다. M2 개발360회와 M3 지도검증240회는 현재 진행할 대상이며 완료된 결과가 아니다. 실행 manifest와 단계별 보고서가 실제 진행 상태의 기준이다.

## 2026-11-21 중단 확인과 결과 보고

현재 실행은 `STOPPED_INTERRUPTED`다. 원래 `RUNNING` 표시만 남아 있었고 실제 driver 및 관련 실행 프로세스는 없었다. 원본 상태 파일을 별도 보존하고 현재 상태를 정정했다. 종료 원인·정확한 종료 시각은 확인되지 않았다.

620회 중 저장 완료61회, 원시 SUCCESS이지만 압축·체크포인트 미완료1회, 미기록/미실행558회다. 원시 결과62개와 압축 완료 보관파일4,241개의 검수를 통과했다. 주 비교는 네 정책이 모두 끝난 단층·평시 입력10개·40회이며, 기술검사20회와 불완전 배치의2개 결과는 별도로 공개한다.

[결과 종합 보고서](../../STATIC_SCENARIO_RESULTS_20261121.md)에 조건별 처리량·납기·에너지·대기·fallback·중단 상태와 남은 검증을 정리했다. 관측된10개 입력에서 휴리스틱 대비 처리량 상대차 평균은 MILP+4.0% / CP-SAT+5.5%였으나, 큰 개선이 고부하·충전1슬롯의 한 입력에 집중됐다. 그 입력을 제외한9개 평균은+0.3%/+0.5%다. 전체 시나리오의 우승이나 기본 정책 변경을 확정할 근거는 아직 부족하다.

M2b 급증180회와 M3 지도검증240회는 미실행이다. job033의 미완료 저장과 미실행2정책에 대한 복구 규칙을 정하고, 실행 관리의 상태/종료 확인을 보완한 뒤 남은 실험을 진행해야 한다. 이번 보고서 작성에서 solver 재실행은 하지 않았다.

## 2026-11-21 재개 절차

`resume_static_scenario_study.py`는 원래 환경·입력 묶음·application source·기존 실행 도구 hash가 같은지 확인하고 새 재개 세션을 등록한다. 수학 모델·기본 정책·가중치·수요·SOC·개별 실행900초·결정2초 예산은 바꾸지 않는다.

먼저 job033의 검수된 원시 MILP를 새 하위 폴더에 복사해 압축하고, 원래 JSON·이벤트·부분압축파일은 보존한다. 복사본의 논리 hash와 원시 이벤트/입력 CAS를 검수한 뒤 SUCCESS로 승격한다. 새 solver 실행으로 세지 않는다. 그다음 같은 입력에서 미실행인 baseline/CP-SAT만 원래 정책 순서대로 실행한다. 실패·취소·시간초과 정책은 자동으로 다시 돌리지 않는다.

감시 프로세스는 기존 단계 실행기를 별도 자식 프로세스로 실행한다. 15초 heartbeat, PID+생성 시각, 자식 종료코드와 종료 영수증을 남긴다. 단계별 native 사용/실제 계획 반영이 없거나 실패·예산 문제가 있으면 후속 단계로 넘어가지 않는다. macOS에서는 분리 세션과 실행 중 idle sleep 방지로 장시간 실행을 시작한다. 상태는 저장된 RUNNING만 읽지 않고 실제 생존 여부로 판단한다.

```sh
python scripts/resume_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --session resume_20261121 --mode supervise
python scripts/resume_static_scenario_study.py --bundle /absolute/path/static_inputs --output /absolute/path/static_runs --mode status
```

`resume_sessions/<session>/protocol.json`에 실행 전에 남은558개 arm·입력 순서·도구hash·예산·세션을 기록한다. 중단 배치는 이전 압축 구간의 정확한 경과 시간이 없으므로, 재개 구간의 active wall 한도3600초를 별도로 적용하고 원래 배치 전체가3600초 이내였다고 주장하지 않는다. 개별 정책900초와2초 결정 예산은 그대로다. 이 교차 세션 배치와 기존 세션을 보존하고, 최종 분석에서 포함/제외 민감도를 함께 보고한다.

신규 재개 회귀11개가 로컬 Python3.11/3.14에서 통과했다. 원본·부분압축 보존, 압축된 CAS 복구, 원래 정책 순서, 미실행만 선택, 원시 검수 전 승격 거부, 자식 실패 종료 기록, PID 재사용·접근 거부·고아 자식·heartbeat 지연 판정을 검증했다. 현재 확보자료 종합 보고서는 중단 시점의 고정 이력으로 유지한다.

실제 재개 세션 `resume_20261121_01`에서 job033 복구와 누락2정책 실행이 성공했고,64회 저장 완료·556회 미실행 시점에서 M2a를 계속 수행 중이다. [재개 영수증](evidence/resume_20261121.json)을 참고한다. 최신 실행 상태는 위 status 명령으로 실제 프로세스와 함께 확인한다.
