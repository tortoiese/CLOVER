# native 실패 계측·동일 문제 구성 개선·suite profile

2026-11-13 추가: 전체 정책의 D1·새 RPC 계측·초기화/재시작은 [D1_STARTUP.md](D1_STARTUP.md)를 참고한다.
아래 D1 미완료 설명은 v1.9 당시의 기존 RH 시범 경로에 해당한다.

2026-11-12 · `common-model-v1.9-native-profile`.
기본 optimized·가중치·GUI는 유지한다. 이번 변경은 native의 실패 계측과 모델 구성 비용을 다룬다.
알고리즘·시간 해상도·horizon·방문 상한을 줄여서 빨라진 결과로 표시하지 않는다.

## 실패해도 측정한 시간을 남긴다

이전에는 NativeModel 생성이 끝난 뒤 Report에 시간을 옮겼다.
생성 중 timeout이면 전체 시간/실패 이유는 남지만 import/build 등의 세부 시간이 0으로 남았다.
PhaseTimer를 builder 바깥에서 소유해 예외가 나도 시작한 단계의 누적 시간을 보존한다.

Report의 새 필드는 다음과 같다.

| 필드 | 의미 |
|---|---|
| timing_stage | 마지막으로 시작한 단계. route_prepare/parent_memory_limit 등도 구분 |
| timing_complete | native 단계 계측을 정상적으로 닫아 반환했는지. 완전한 D1 구현 여부와 다름 |
| stage_seconds | 실제로 시작한 단계만 들어 있는 서로 겹치지 않는 시간 기록 |
| work_counters | 구성 도중까지의 변수/제약 수, 복귀 계산 hit/miss, 캐빈 연결 considered/pruned/kept |

native_import·setup·robot_model·resource_model·cabin_model·model_validate·fingerprint,
incumbent_validate·primary/secondary setup/search·solution_validate를 구분한다.
부모/worker는 worker_dispatch·route_prepare·worker_response를 별도로 남긴다.
없는 단계는 시작하지 않았거나 측정할 수 없었던 것이므로 0초로 채우지 않는다.
부모가 worker를 강제 종료하면 내부 어느 단계였는지 추측하지 않고 parent 상태와 알려진 전체 지연만 보존한다.
모델 구성 실패의 변수/제약은 부분 구성 수이며 실행 가능한 모델/해를 만들었다는 증거가 아니다.

기존 build_seconds는 import/build 등을 포함한 요약이며 model_seconds/compile_seconds와 겹친다.
요약 필드를 모두 더하지 않는다. 단계별 원인 분석에는 stage_seconds를 사용한다.
controller 반환 후 재검증/commit은 여전히 apply_wall_seconds다. worker의 timing_complete가 전체 요청/D1 비용을 뜻하지 않는다.

## 같은 가능 일정에서 불필요한 구성을 줄인다

native_cache=true는 한 snapshot 안의 (로봇, 위치)별 복귀 예비량을 저장한다.
준비된 경로의 최소 수평 거리를 미리 index로 만들고 같은 계산을 재사용한다.
도달 불가능한 경우의 None도 그대로 저장한다. 해가 없다는 조건을 0 에너지로 바꾸지 않는다.
cache는 모델별로 새로 만들어 layout·다른 로봇/SOC 입력으로 넘어가지 않는다.

native_pruning=true는 캐빈의 도착층과 같은 출발층 후보만 찾고 다음 연결을 제거한다.

- 같은 로봇의 뒤 행동에서 앞 행동으로 돌아가는 연결: 로봇의 양수 시간 순서와 모순.
- 같은 로봇·같은 행동 위치의 서로 다른 대안: 동시에 선택할 수 없음.
- 같은 대안 내부의 역순 캐빈 사용: 경로/시간 순서와 모순.
- 앞 운행의 가능한 가장 빠른 종료도 뒤 운행의 가장 늦은 시작보다 늦는 연결.

운행 후보·작업·자원·배터리 조건은 유지한다. 새 후보 제한을 추가하지 않는다.
캐시만 켜고 끌 때는 model_hash도 같아야 하며, 연결 제거는 표현이 달라져 model_hash가 달라질 수 있다.
두 경우 모두 semantic_spec_hash와 작은 두 목적 최적값이 유지되는지 검사한다.
native_cache/native_pruning은 실행 방식 옵션이며 공통 의미 해시에서는 제외한다.
native_reference4.json은 native_bounded4.json과 같은 수학적 범위에서 두 최적화를 끈 비교 설정이다.

## headless profile 명령

```bash
python -m simulation.evaluation profile \
  --manifest /tmp/clover-suite/manifest.json \
  --output /tmp/clover-profile.json
```

저장된 결정의 hash·source·manifest/result 정체성을 검사한다. solver를 다시 돌리지 않는다.
source·scenario hash·policy·정확한 settings·D0/D1이 다른 데이터는 별도 group이다.
각 단계의 표본 수·평균·P50/P95/P99·최대와 unrecorded 수를 반환한다.
cold worker 수·부모 경과 시간·soft 예산 초과·fallback 이유·종료 때 pending 결정도 남긴다.
soft 예산 초과는 부모가 관측한 전체 요청 경과 시간이 timeout보다 큰 경우다. native solve만의 deadline 준수율과 다르다.
NOT_RUN_BUDGET은 시작하지 않은 실행이며 정책 실행 실패로 세지 않는다.
구버전 Report에 stage_seconds가 없으면 0초로 추정하지 않는다.
RH timing이 없는 기존 정책의 group도 timing_available=false로 보존한다.

이 명령은 설명 통계다. run별 paired 효과·신뢰구간·생존분석·환경별 추천은 구현하지 않았다.
결정 수를 독립적인 환경 반복 수로 해석하지 않는다.

## 비교와 남은 D1 범위

모델 구성 벤치마크는 같은 코드에서 최적화 on/off를 번갈아 실행한다.
CPP/OR-Tools 초기 import 비용은 별도 보존하고, warm build 시간을 전체 운영 결정 시간과 혼동하지 않는다.
기존과 같은 0.5초/10초 예산을 유지한 물리 실행도 따로 검수한다.
콜드 시작·import가 예산 대부분을 쓰면 구성 개선만으로 첫 요청의 시간초과가 사라진다고 주장할 수 없다.

현재 D1은 RH의 관측→첫 검증까지 요청 지연 시범이다.
반환 후 재검증/commit·실패 시 fallback 계산 지연과 기존 세 정책의 D1은 아직 남아 있다.
완전한 D1을 구현하려면 기존 정책의 배정/충전/선점 결정을 관측과 분리된 제안으로 만들고,
상태를 바꾸기 전 지연·재검증을 거치는 공통 적용 경로가 필요하다.
현재 엔진에 배정을 먼저 적용한 뒤 timeout만 추가하는 방식으로 완료 처리하지 않는다.

이번 단계의 관측 schema는 v6, 계획/모델은 v8, headless 결과는 v4다.
이전 소스/계획은 해당 이전 버전에서 replay한다. 기본 정책 변경이나 실제 6대 시나리오의 최종 추천은 수행하지 않았다.
