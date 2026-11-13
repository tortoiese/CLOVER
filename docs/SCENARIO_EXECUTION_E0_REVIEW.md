# 시나리오 v3 실행 입력·E0 검수

작성일: 2026-11-13. [PRD §22~23](PRD_ALGORITHM_EXPERIMENTS_v2.md#execution-inputs-20261113)의 후속 실행 기록이다.

## 완료 범위와 해석

설계 목록을 실제 실행 JSON으로 변환하는 `prepare-design`, 고정 입력·seed를 정책별로 동일하게 실행하는 `run-prepared`를 구현했다. 합성 참조의 E0 입력62개와 E1 후보162개를 생성·검증했다. E1 후보는 결정적12조건을 중복 생성하지 않은 `12 + 50×3`개이며 **실제 E1 성능 실험은 실행하지 않았다.**

E0 물리 실행185회는 모두 `SUCCESS`이고, 원시 로그에서 재구성한 지표와 저장 지표, 저장 결정에 따른 replay가 각각185/185 일치했다. 성공은 관측창 종료·불변식·기록의 정상 처리를 뜻한다. 모든 작업의 완료나 solver의 최적해 획득을 뜻하지 않는다.

모든 지도는 `synthetic_reference_not_team_factory`, 부하 보정은 `UNCALIBRATED`다. 대부분 작은 기전 지도이며 S11만64/256노드다. 팀 최종 지도, λ 보정, E1 튜닝, E2/E3, 통계적 우수성, 알고리즘 선택 결론까지 완료한 결과로 쓰지 않는다.

## 실행 구성

| 묶음 | 조건·정책 | 횟수 |
|---|---|---:|
| `controls_d0` | 나머지50조건의 사전 지정 baseline 자원 대조 | 50 |
| `representatives_d0` | 12가족 대표조건 ×7정책 | 84 |
| `representatives_d1` | 6가족 대표조건 ×7정책, 공통 staged D1 | 42 |
| `charge_window_900_d0` | 동일 충전 수요를900초까지 관찰, 슬롯1/4 ×baseline | 2 |
| `mixed_soc_d1` | 사전 선언한 S04 혼합SOC 조건 ×7정책 | 7 |
| 합계 | 독립 seed185개나 전체62조건×7정책 비교를 뜻하지 않음 | **185** |

처음176회 구성을 [protocol](scenarios/e0_execution_20261113/protocol.json)에 동결했다. 짧은 충전 관측의 미노출을 확인한 뒤 원래 결과를 보존하고 [추가 관측 규약](scenarios/e0_execution_20261113/charge_window_amendment.json)으로2+7회를 추가했다. seed나 정책별 수요를 결과에 맞춰 바꾸지 않았다.

7정책은 baseline, optimized, assignment_milp_legacy, rh_milp_grid, rh_cpsat_linear, rh_cpsat_interval, rh_cpsat_native다. E0는 horizon30초·격자2초·solver0.25초·native_actions4의 빠른 기능 설정이다. E1 후보의40초·2초 solver 설정과 성능 순위를 합치지 않는다. D1은 실제 준비/결정 비용을 기록하는 공통 `d1-staged-budget-v1`이며, 고정 예산과 공개 여유를 이용하는 지연 규약이다.

## 감사 기준과 증거

- 동일 정책 묶음의 scenario/trace/initial 해시, 예상 정책 수, 수요 유입 수와 전체 상태 합을 대조했다. 모든 실행의 시작·종료 소스가 같다.
- 최종 불변식·에너지 수지, 원시 사건의 연속 순서·시간·digest·통행 용량을 검사했다. 에너지 오차 허용값은1e-7 battery-unit이다.
- `analyze`로 자원 대기·캐빈 단계를 원시 로그에서 재구성하고, `replay`로 저장한 입력·결정·지연에 따른 사건 동일성을 확인했다. replay는 새 solver 품질의 독립 반복이 아니다.
- D1 49회는 결정 coverage 및 process/connection/RPC thread/application process 해제를 확인했다.
- [전체 판정·파일 해시](scenarios/e0_execution_20261113/receipt.json), [185회 관측 CSV](scenarios/e0_execution_20261113/summary.csv), [자원 노출 감사](scenarios/e0_execution_20261113/reference_exposure.json), [준비한226개 입력의 메타데이터](scenarios/e0_execution_20261113/prepared_index.json)를 보존한다. 입력 메타데이터는 E0 62+E1 후보162+충전 연장2이며 실행185회와 다른 분모다. CSV는 원시 관측 요약이며 신뢰구간·추천 표가 아니다.

원시 결과·준비 입력은 로컬 `outputs/CLOVER_v3_experiments_20261113/`에 후속 검수까지 약0.7GB로 보존했다. Git에는 작은 판정·해시·요약만 넣었다. 원시 파일이 없는 환경에서 receipt만으로 replay가 된다고 표시하지 않는다.

185회와 입력 묶음의 정확한 코드 commit은 `4f6dc5e748735318cdb4fa068b86f4e8db9b894c`, 소스 해시는 `ce930fb2f8c26ea2a0911d3394863ef7da2bc2a61f78211450a2a706d9dbab30`이다. manifest의 기존 `base_commit`은 모델 계보 필드이며 이 작업의 실행 commit을 뜻하지 않는다. receipt의 commit과157개 소스 파일의 Git object 해시를 별도 대조했고 불일치는0개였다. 이후 수정 코드의 결과와 이 동결 결과를 같은 소스 실행으로 합치지 않는다.

## 실제로 확인한 자원 노출

| 대조 | 사전 지정 baseline D0에서 관측한 것 | 해석 경계 |
|---|---|---|
| S00 무수요 | 유입0·완료0 | 빈 입력 처리 확인 |
| S02 폭1.3/2.2/3.1m | 제어 통로 최대 점유900/1800/2700mm, 동일 로봇의1/2/3대 점유 | 반복 왕복 운송·포화 용량의 실증은 아님 |
| S03 교차로 on/off | on에서 실제 `intersection:(1, 200.0, 200.0)` 점유1slot, off에서는 없음 | 일반 대기 채널 이름만으로 교차로 존재를 판단하지 않음 |
| S04 슬롯1/4 | 180초에는 두 조건 모두 충전0. 같은 seed·λ·초기 상태로900초까지 늘리자 충전158.325/168.825unit, 충전 대기541.6/0 robot·s | 작업 완료는 둘 다21/27. 처리량 개선으로 표현하지 않음 |
| S08 캐빈1/2 | 1개 조건은E0만4층 이동, 2개 조건은E0/E1 각각2층 이동 | 다른 정책에도 두 캐빈 사용을 강요하지 않음 |
| S09 교란 | 정지/폭 변경60초·복원90초 적용. 영구 퇴역은 복원 없음. 고정 probe 취소1건 | 사건 적용이 모든 정책의 유의미한 성과 차이를 보장하지 않음 |
| S11 지도 | 기준64노드·별도256노드 | 동일 지도에 단순 노드 수만 추가한 대조로 취급하지 않음 |

충전900초 대조에서는 두 조건 모두 구조 에너지24unit, fault8/9회가 발생했다. 장부는 맞지만 에너지 자립 운용이나 안정 상태는 아니다. E1에서는 로봇별 충전 순환·SOC·고장/구조·충전 전후 구간별 처리율을 보고, 지속 가능한 대조와 의도적 충전 과부하를 구분해야 한다. 처음180초의 작업 trace가 연장 입력의 정확한 prefix임을 확인했다.

S01의 사전 지정 작은 결정적 배치에서는8건 모두 완료했고 optimized의 마지막 완료57.1초, RH MILP·공통 선형/격자 interval CP-SAT은23.5초였다. 이 수치는 공동 배정 차이를 확인하는 한 기전 사례다. 같은 창 안의 완료 수는 모두8건이므로 처리량 증가율로 발표하지 않는다. native는 이 빠른 예산에서 계획 채택0회·fallback3회였고57.1초였다. native 자체의 일반적 열등성으로 해석하지 않는다. 다른 대표조건에서는 native 계획 채택도 확인했다.

## 검수 중 발견한 종료 경합과 후속 검사

동결 commit의 GitHub Actions는 Ubuntu/macOS/Windows ×Python3.11/3.14에서 각각483개를 통과했다. 그러나 Linux의 대형 IPC 차단·watchdog 검사에서, 소유자가 통신 연결을 닫은 뒤 `send`가 재개되며 `TypeError`가 스레드 밖으로 빠져나가는 경고가 발생했다. CI 성공만으로 경고가 없는 것으로 기록하지 않는다.

공통 통신 교환에서 **실제로 닫힌 연결**의 `TypeError`만 transport 실패로 보존하도록 보완했다. 열린 연결의 실제 프로그래밍 오류는 계속 드러나게 했다. D0 solver/D1 RPC가 같은 처리를 사용하며, send/recv ×closed/open의4개 회귀 검사와 기존 대형 차단 실험을 통과했다.

수정 commit은 `ddc94f457732ee3e85b6fa05c31ae35bbbfe6871`, 소스 해시는 `556d428ba1f98dce50f083a20d3a0351c31f0888e42d14cd2c540367415ae68e`다. Python3.11/3.14 전체 검사에서 각각487개가 통과했고 unhandled thread warning을 오류로 취급했다. [GitHub Actions](https://github.com/tortoiese/CLOVER/actions/runs/43435970526)의 Ubuntu/macOS/Windows ×3.11/3.14도 각487개를 통과했고6개 job 모두 unhandled thread warning이 없다. 의존 라이브러리의 deprecation warning은 남아 있다. [로컬 검증 판정](scenarios/e0_execution_20261113/local_validation.json)과 [수정 CI 판정](scenarios/e0_execution_20261113/ci_ipc_fix.json)을 보존한다.

수정 후 새 입력 묶음으로 S02 좁은 통로·S04 혼합SOC·S08 두 캐빈의3조건 ×7정책 ×D0/D1, **별도42회**를 실행했다. 입력 scenario/trace/initial 해시는 원래 조건과 같고 소스 해시만 새 코드에 연결했다. 전42회 SUCCESS·원시 지표·replay·불변식 검수를 통과했으며 D1 21회는 coverage/소유 자원 해제도 통과했다. D0 baseline3개의 물리 사건 digest도 수정 전과 같다. [사전 규약](scenarios/e0_execution_20261113/ipc_canary_protocol.json)과 [42회 판정](scenarios/e0_execution_20261113/ipc_canary_receipt.json)을 구분해 보존했다. 앞의185회 판정을 새 소스로 다시 이름 붙이지 않았다.

## main 69bb83c 통합 검수

작업 중 원격의 PR #11·#12가 병합되어 main이 `69bb83ccae87cc6fa95f89125b129d990188360c`로 바뀌었다. 해당 main을 기능 브랜치에 충돌 없이 merge했고 PR #13의 base도 main으로 변경했다. 이 작업에서 PR #13을 main에 병합한 것은 아니다.

통합 코드 `aec9b5360e6580061712467ffba811fcf5e230d6`에서 Python3.11/3.14 전체 **각499개**가 통과했다. 처음 제한된 샌드박스에서는 추가된 HTTP 모의 서버3검사가 localhost bind의 `PermissionError`로 실패했다. 실패 기록을 남기고 로컬 서버를 허용한 환경에서 전체 검사를 통과했으며 코드를 바꿔 건너뛰지 않았다. [로컬 통합 판정](scenarios/e0_execution_20261113/local_main_integration.json)에 두 결과의 JUnit 해시가 있다. [GitHub 통합 CI](https://github.com/tortoiese/CLOVER/actions/runs/43436824192)의6개 조합도 각499개 통과·unhandled thread warning0이며 [CI 판정](scenarios/e0_execution_20261113/ci_main_integration.json)을 보존한다.

통합 소스 해시는 `05722215372a262cd554a0e9c1a22327f5a2354934eb1cf79a115ef29298b41d`다. 새 입력을 준비해 같은3조건×7정책×D0/D1의 **추가42회**를 실행했고 전부 SUCCESS·수요/에너지/통행 불변식·원시 지표·replay가 일치했다. D1 21회의 coverage/자원 해제, D0 baseline3개의 원래 물리 digest 일치도 확인했다. [통합 사전 규약](scenarios/e0_execution_20261113/main_canary_protocol.json)과 [통합42회 판정](scenarios/e0_execution_20261113/main_canary_receipt.json)을 보존한다.

| 검수 단계 | 정확한 코드 | 실행 횟수 | 판정 |
|---|---|---:|---|
| 시나리오 E0·노출 보완 | `4f6dc5e` | 185 | 원시/재생185회 일치 |
| IPC 종료 보완 | `ddc94f4` | 42 | 원시/재생42회 일치 |
| 최신 main 통합 | `aec9b53` | 42 | 원시/재생42회 일치 |

총269회는 소스별 기능 검수 실행 수다. 269개의 독립 seed나 E1 본 실험으로 합치지 않는다. 마지막 통합 뒤의 판정·PRD 편집은 Python 소스를 바꾸지 않았다. 기본 optimized·휴리스틱 가중치·기본 D0는 유지한다.

## 추가 PR #14 통합·headless 순회 경계

최종 검사 도중 PR #14가 main `3dfb4ba3100e81599a6d9720efa8f5768ea96c39`에 병합됐다. 이 main도 충돌 없이 기능 브랜치에 통합했다. PR #14의 `roam=True`는 GUI의 유휴 로봇 순회·초기 층 분산을 위한 옵션이고 engine 기본값은False다. 고정 입력 실험은 runner에서 `roam=False`를 명시해 이 기본값에 의존하지 않도록 했다. GUI 순회를 포함한 실행은 다른 운용 조건이므로 공식 headless 결과와 구분한다. ROI의 새 환산 가정도 headless의 battery-unit 지표를 대체하지 않는다.

무수요 다층 입력을 baseline/optimized로 각각 실행해 이동거리·소비 에너지·캐빈 층 이동이0이고 replay가 일치하는 회귀2개를 추가했다. 이 코드는 `77cbac9346e8b32d2093469bee981b025843d2e1`에 동결했고, Python3.11 전체 **502개**와 새 시나리오 모듈33개를 통과했다. [로컬 판정](scenarios/e0_execution_20261113/local_pr14_integration.json)에 JUnit 해시를 보존한다. Python3.14와 운영체제별 최신 자동 판정은 [PR #13 checks](https://github.com/tortoiese/CLOVER/pull/13/checks)를 참조한다.

PR #14 통합 소스의 S08 두 캐빈 조건×7정책×D0/D1 **추가14회**를 실행했고, 전부 SUCCESS·원시 지표·replay·불변식 검수를 통과했다. D1 7회의 coverage/소유 자원 해제와 D0 baseline의 원래 물리 digest 일치도 확인했다. [사전 규약](scenarios/e0_execution_20261113/roam_canary_protocol.json)과 [14회 판정](scenarios/e0_execution_20261113/roam_canary_receipt.json)은 기존 세 소스 결과와 분리했다. 최종 합계는185+42+42+14=**283회 기능 검수**이며 독립 반복 수나 본 실험 수가 아니다. 앞의269회는 해당 세 소스의 보존 기록이다.

## 다음 단계와 완료 조건

1. **R1 실제 입력**: 팀의 지도·방향·거리·폭·초기 SOC/캐빈 층·설비·납기를 참조 JSON에 반영한다. 실제 고정 trace의 설계별 대응 import는 별도 구현한다.
2. **R2/R3 E1 준비**: 같은 장비·입력에서 H/격자/후보/방문 상한/예산을 비교하고 build/solve/검사/공개·stale/fallback·RSS를 확인한다. 휴리스틱 가중치는 고정한다.
3. **R4 부하·관측 보정**: λ를 이름상의 정격이 아닌 실제 흐름·병목 사용으로 보정한다. 충전 반복, 과도 응답, 과부하 backlog, 유한 배치를 구분하고 공통 관측창을 동결한다.
4. **R5~R7 통계·검증**: paired 차이·신뢰구간·실패/검열을 구현한 뒤 새로운 지도/seed의E2와E3를 진행한다. 그 전에는 환경별 최종 정책 선택이나 기본 정책 변경을 완료로 표시하지 않는다.

입력 생성·실행·재생 명령은 [PREPARED_EXPERIMENTS.md](../robot_factory_sim/PREPARED_EXPERIMENTS.md)에 있다. 과거 입력은 해당 소스로 분리한 checkout에서 재생한다. 소스 변경을 허용하는 옵션으로 무결성 검사를 우회하지 않는다.
