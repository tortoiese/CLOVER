# E1 설정 파일럿 검수 결과

2026-11-14. [사전 계획](PRD_E1_SETTINGS_PILOT_20261114.md)의 합성4조건×정책5종×설정3종, **60회**를 실행했다. 실행242.04초, SUCCESS60·실패/미실행0이다. 원시 이벤트 재구성·저장 결정 replay·물리 digest·에너지/작업 장부·자원 용량·D1 coverage·worker 해제를 모두 통과했다. SUCCESS와 모든 작업 완료는 다르다.

이 실험은 실제 공장 입력 확정 전의 **단일 seed 설정 점검**이다. 기존 E0 283회와 별도 소스/목적이며 독립 반복으로 합치지 않는다. 세 설정도 seed3개가 아니다. 순수 solver가 아니라 기동·계획 거절·fallback을 포함한 closed-loop 정책을 평가했다.

## 기준과 증거

- 실행/입력 동결 commit: `e82742479b1ca3126217cb8dbfad49d1c1efc902`.
- 앱 Python170개가 Git blob과 일치했고 실행 전후 source hash는 `75016001011187ca9bf4272169911a5698770bf32c87800f6690c280312d5f3a`다. 실행 기준 코드는 PR #15의 `dd2a861`과 동일하다.
- 수요/run seed1001, backend seed42·worker1, 직렬 실행, 단일 로컬 Mac arm64/Python3.11.14. 패키지/시계는 [preflight](scenarios/e1_settings_pilot_20261114/preflight.json)에 고정했다.
- [전체 감사 판정](scenarios/e1_settings_pilot_20261114/receipt.json), [실행 순서와 상태](scenarios/e1_settings_pilot_20261114/execution.json), [60회 요약 CSV](scenarios/e1_settings_pilot_20261114/run_summary.csv), [효과 CSV](scenarios/e1_settings_pilot_20261114/effect_summary.csv), [파일 해시/추론 점검](scenarios/e1_settings_pilot_20261114/publication.json)을 보존한다.
- 원시 result/events는 workspace `outputs/CLOVER_E1_settings_pilot_20261114`에 약444MB 보존했다. Git에는 입력·판정·해시·요약 약2.2MB를 넣는다. Git에 raw가 없으므로 다른 장비에서는 재실행하거나 별도로 원시 묶음을 전달받아야 한다.

## 실제 관측

아래는 기준 h40_t2의 값이다. 단위는 완료시간 sim-second, 대기 robot-second다. 반올림 전 전체 값은 CSV에 있다. 차이를 일반적 우열이나 유의한 개선으로 해석하지 않는다.

| 조건·지표 | baseline | optimized | RH MILP | CP-SAT linear | CP-SAT native |
|---|---:|---:|---:|---:|---:|
| S01 전체8개 batch 완료시간 | 57.84 | 57.84 | 57.33 | 27.24 | 27.28 |
| S08 전체8개 batch 완료시간 | 106.94 | 106.93 | 105.30 | 85.04 | 102.97 |
| S04 완료/도착 | 5/9 | 9/9 | 9/9 | 9/9 | 9/9 |
| S04 충전 대기 | 0 | 277.61 | 403.73 | 403.86 | 647.40 |
| S11 완료/도착 | 13/16 | 16/16 | 15/16 | 15/16 | 15/16 |

S01/S08은 모두 전체8개를 완료했으므로 고정 관측창의 처리량은 정책 간 같다. 차이는 batch를 끝내는 시간에 있다. MILP도 이 두 조건에서 소폭 단축했지만, 이 한 번의 결과만으로 휴리스틱보다 우수하다고 결론 낼 수 없다. CP linear의 완료시간 단축 역시 반환된 계획·fallback을 포함한 관측이다.

S04 baseline은 충전 대기가0인 대신 구조 에너지33unit·fault14회·미완료4개였다. **대기0을 좋은 운용으로 평가하면 안 된다.** 다른4정책은 이번450초에 구조 에너지/fault0이며9개를 완료했지만, RH의 충전 대기는 optimized보다 길었다. 이 관측창으로 장시간 에너지 자립을 입증하지 않는다. S11에서는 세 설정 모두 optimized16개·RH15개·baseline13개였으므로 RH 우세 시나리오로 발표하지 않는다.

## 설정을 바꿨을 때

| 조건·정책의 전체 batch 완료시간 | h40_t025 | h40_t2 | h60_t2 |
|---|---:|---:|---:|
| S01 RH MILP | 55.04 | 57.33 | 61.85 |
| S01 CP linear | 25.23 | 27.24 | 63.35 |
| S01 CP native | 58.71 | 27.28 | 27.90 |
| S08 RH MILP | 102.79 | 105.30 | 105.82 |
| S08 CP linear | 82.60 | 85.04 | 86.08 |
| S08 CP native | 113.93 | 102.97 | 103.01 |

계산 제한 증가나 horizon 확대가 항상 운영 개선을 만들지는 않았다. 예를 들어 S01 h60_t2의 CP linear는 반환 RH report0이며 HARD_TIMEOUT2·END_TO_END_SOFT_DEADLINE1을 기록했다. 해당 완료시간을 CP 계획 자체의 성능이라고 해석할 수 없다. native는0.25초에서 incumbent 확보가 어려운 경우가 있고, 긴 제한에서 개선된 조건도 있었다. 각 조건의 고유 입력은 같지만 D1 실측 wall 비용은 실행마다 달라 운영 경로도 달라질 수 있다. 같은 seed를 다시 돌린다고 새 native 실행의 숫자가 비트 단위로 일치한다고 약속하지 않으며, 저장 결정을 사용한60회 replay는 모두 일치했다.

공통 soft deadline은2초다. solver timeout2초는 준비·직렬화·왕복·검증 시간까지2초 안에 끝난다는 뜻이 아니다. 기동은 on-demand이고 해당 비용도 D1에 반영된다. 이번에는 시간 제한·기동·유효성 검사·공정 진행이 합쳐진 효과를 관찰했으며, 어느 하나만의 인과 효과를 확정하지 않는다.

## 계산·검수 관측

- 전체 control fallback에는 HARD_TIMEOUT14, END_TO_END_SOFT_DEADLINE2, BUILD_LIMIT4, NO_INCUMBENT_TIMEOUT6이 있다. 이들은 run 실패와 다르며, 원래 결과와 fallback 사유를 삭제하거나 좋은 결과로 재시도하지 않았다.
- 반환 RH report313개는 OPTIMAL254·FEASIBLE49·BUILD_LIMIT4·NO_INCUMBENT_TIMEOUT6이다. 이 분모에는 반환되지 못한 timeout 요청이 없다. 또한 OPTIMAL은 해당 유한 후보·격자·목적식 안의 상태이며 전체 공장 운영의 전역 최적을 뜻하지 않는다.
- CSV의 decisions/accepted는 보조 동작·빈 동작도 포함하는 전체 control 기록이다. 실제 반환 RH report 수와 그 기록의 accepted 수를 별도 열에 넣었다. 이 숫자를 작업 배정 수나 고유 작업 수로 부르지 않는다. [후보 제외 관측](scenarios/e1_settings_pilot_20261114/candidate_coverage.json)은 반환 snapshot에서0건이었으며, 미반환 요청의 후보 완전성까지 증명하지 않는다.
- publication 실측 최대0.11871초로 공통 선불0.2초를 넘지 않았다. coverage 위반0, 사전 규칙의0.5초 추가 cohort0회다. 기존 D1 기본 설정은 변경하지 않았다.
- 기록된 worker peak RSS의 전체 최대는248.8125MiB다. 부모 프로세스·장비 전체 peak나 샘플 사이의 순간 peak가 아니다. 반환 report만 보면 최대160.265625MiB이므로 timeout 보고를 제외한 숫자를 전체 peak로 쓰지 않는다.
- 반환 report의 build 최대0.43034초, solve 최대1.64659초다. CSV의 build/solve P95는 해당 필드가 기록된 report만의 요약이며 누락을0으로 채우지 않았다. 측정 control 시간과 시뮬레이션에 주입한 시간은 각각 별도 열/프로파일로 보존했다. 중첩 report 시간을 배타적 단계 합계에 다시 더하지 않는다.
- 설정별 R5 분석을 분리했다. 비교384개·효과 CSV768행은 지표별 요약이며768회 실험이 아니다. 모든 confidence interval·p값·추천은 보류했다. seed1/고정 batch/미보정 지도에서 최소 반복 문턱을 낮추지 않았다.
- 검수 보조 스크립트의 첫 시도에서 출력 하위 폴더 생성이 빠진 오류를 고쳤다. [오류 기록](scenarios/e1_settings_pilot_20261114/audit_setup_failure.json)을 보존했고 새 native 실행 없이 감사를 다시 수행했다. 제품 Python 소스는 변경하지 않았다.

## 다음 작업의 우선순위

1. R2: 같은 snapshot에서 동일 후보·목적·예산을 고정한 backend 대조를 준비한다. 기동/route 준비/build/search/검증/공개 시간을 분리하고,2초 운영 예산 안에 solver를 얼마까지 허용할지 training에서 검토한다. 이번 closed-loop의 작은 MILP 개선이나 CP 개선으로 solver 우열을 확정하지 않는다.
2. R3: h40_t025/h40_t2를 후속 training 후보로 유지하고 h60_t2는 실패 사유 대조로 보존한다. 이는 임시 후보이며 어느 것도 기본값·최종 추천으로 승격하지 않는다. native BUILD_LIMIT/NO_INCUMBENT와 충전 stale/auxiliary fallback의 기전을 점검한다.
3. R1/R4: 팀의 실제 지도·trace·납기·절대 λ를 확정하고 충전 반복/관측창을 보정한다. 이후 복수 지도·수요 seed의 E1 training/validation 프로토콜을 별도 동결한다. 고정 batch에 seed 이름만 바꿔 반복 수를 늘리지 않는다.
4. R6/R7: E2 held-out·E3 stress와 환경별 선택 규칙은 미완료다. 현재 결과만으로 “충전에는 MILP” 같은 자동 추천을 만들지 않는다.

## 같은 입력으로 실행하기

실행 기준 commit에서 프로젝트 의존성을 설치하고 `robot_factory_sim`을 작업 디렉터리로 사용한다. 아래 명령은 기준 설정 한 묶음의 입력 생성 예다. 출력 경로는 새 디렉터리를 사용한다.

```sh
export QT_QPA_PLATFORM=offscreen CLOVER_RAG_ENABLED=0 CLOVER_RAG_ALLOW_NETWORK=0 PYTHONDONTWRITEBYTECODE=1
python -B -m simulation.evaluation prepare-design \
  --design ../docs/scenarios/experiment_design_v3.json \
  --references examples/e1_screening_20261114/references.json \
  --phase e1-candidate --seeds 1001 \
  --cells S01_eligible_1 S04_mixed_soc S08_two_cars S11_budget_2 \
  --rh-settings examples/e1_screening_20261114/h40_t2.json \
  --decision-settings examples/e1_screening_20261114/decision_settings.json \
  --output /tmp/clover-e1-h40-t2
```

세 profile JSON 각각으로 입력을 준비하고 `protocol.json`의 blocks 순서대로 아래 API를 호출하면 동일 설계를 실행할 수 있다. 각 block마다 `policies`의 순서까지 프로토콜을 따른다. 전 과정은 직렬 실행하고 elapsed를 공유한다. driver는 `if __name__ == '__main__':` 아래에서 호출해야 multiprocessing spawn이 재실행되지 않는다.

```python
remaining = max(0.000001, protocol['study_wall_limit_s'] - (time.perf_counter() - started))
run_prepared(prepared_root / block['profile'], unique_block_output,
             timing='D1', cells=(block['cell'],), policies=tuple(block['policies']),
             run_wall_limit=protocol['run_wall_limit_s'], suite_wall_limit=remaining)
```

`run_prepared`는 `simulation.evaluation.prepared`에서 import한다. 전체 프로토콜은 [JSON](../robot_factory_sim/examples/e1_screening_20261114/protocol.json)을 따른다. 감사는 child suite별 `profile`, result별 `analyze`/`replay`, 설정별 training `comparison-plan`/`compare`로 수행한다. 같은 입력의 재실행과 저장 결정 replay는 다른 검증이다.
