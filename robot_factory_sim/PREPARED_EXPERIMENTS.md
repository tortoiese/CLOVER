# 시나리오 v3 실행 입력 준비와 paired 실행

이 기능은 설계 목록을 `clover-scenario-v1` JSON으로 변환하고, 셀별로 고정한 작업 trace·초기 상태·seed를 같은 정책 묶음에 전달한다. 기본 GUI·휴리스틱 가중치·기본 시간 모드는 바꾸지 않는다.

## 범위

- S00~S11 기본46조건과 X01~X04 복합16조건을 지원한다.
- 합성 참조 지도를 명시적으로 선택하거나 `clover-design-references-v1` 파일을 제공한다. 합성 지도는 팀의 최종 공장 지도라는 뜻이 아니다. 대부분 작은 기전 지도이며 S11만 별도64/256노드 격자 코호트다.
- 기본4정책과 native4가족의 평가 범위를 설계에서 읽는다. E0의 기능 검사 대상은7정책이다.
- 동일 작업·UID·처리 noise를 고정하며, burst는 warmup 이후 도착시각만 변환해 실제 작업 수를 보존한다. 우선순위만 바꾸면 납기를 유지한다. 목적지 비중은 입력이며 실제 층간 이동은 실행 결과다.
- 결정적12셀은 요청 seed가3개여도 입력을 한 번만 만든다. 3seed 후보 묶음은 `12 + (34+16)×3 = 162`개다. 이를 186개의 독립 수요 반복으로 세지 않는다.
- S01은 두 지역의 가까운 로봇을 첫 작업에 바로 쓰는 선택과 이후 작업을 함께 보는 선택을 대조하는 사전 지정 기전 지도다. 이 지도에서의 차이를 공장 일반화 근거로 쓰지 않는다.
- 입력 검사는 스키마·폭을 반영한 초기 도달성·변경 축·해시를 확인한다. 실제 자원 사용·충전 반복·부하 적합성까지 증명하는 검사는 아니다.

## 단계와 시간창

| 단계 | 생성하는 입력 | 해석 |
|---|---|---|
| `e0` | 전 셀180초·warmup0. S09 사건60초/복구90초 | 기능·기전 smoke. 성능 추천이나 정상상태 근거로 쓰지 않음 |
| `e1-candidate` | 참조 지도별 후보 창. 일반 stream600+7200초, batch300초·warmup0, S09 1200초·warmup0, S10 7800초·warmup0 | 보정 전 training 후보. 최종 설정/λ/지도 동결 아님 |

S09의 일반 사건300~420초를 warmup600초 앞에 놓으면 측정창에서 교란이 사라진다. 합성 참조는 과도 응답 창을 별도로 두고, 외부 참조의 사건이 warmup 안에 들어가면 거부한다. 취소 검사를 위한 작업1개를 사건1초 전에 모든 S09 대조군에 공통으로 넣는다. 따라서 이 가족의 수요는 Poisson+공통 probe이며 취소군만 작업을 더 넣지 않는다.

퇴역·취소에는 복구 사건을 만들지 않는다. 복구 가능한 설비/충전 정지·통로 폭만 복원한다. 입력 취소 대상은 정책 실행 뒤 바꾸지 않는다.

## 입력 준비

아래 명령은 저장소의 `robot_factory_sim` 디렉토리에서 실행한다. 경로는 쓰기 가능한 새 폴더로 바꿔도 된다. 기존 출력 폴더는 덮어쓰지 않는다.

```bash
python -m simulation.evaluation prepare-design \
  --design ../docs/scenarios/experiment_design_v3.json \
  --synthetic-reference --phase e0 --seeds 1001 \
  --rh-settings examples/design_v3/e0_math.json \
  --output /tmp/clover-v3-e0-inputs
```

출력은 `manifest.json`, `references.json`, `design.json`, 실제 `inputs/*.json`, 전체 수학·D1 설정 파일이다. E0의62개 입력은 일부 대표 조건만 실행해도 미리 검증할 수 있다. 생성된 `references.json`은 지도·초기 상태·별칭·기준률을 편집할 수 있는 외부 참조 파일이다.

```bash
python -m simulation.evaluation prepare-design \
  --design ../docs/scenarios/experiment_design_v3.json \
  --references /path/to/reviewed-references.json \
  --phase e1-candidate --seeds 1001 1002 1003 \
  --rh-settings examples/design_v3/e1_candidate_math.json \
  --output /tmp/clover-v3-training-inputs
```

`--references`와 `--synthetic-reference`는 동시에 쓰지 않는다. `--cells`로 일부 셀만 준비할 수 있다. `--rh-settings`/`--decision-settings`는 연구 기본 설정 위의 명시적 override이며, 적용된 전체 설정을 저장한다. 수학 설정 `timing_mode`는 D0를 유지하고 공통 D1을 실행 시 선택한다. 구형 RH 전용D1은 혼용을 막기 위해 거부한다.

현재 외부 참조의 stream은 기준 Poisson rate를 입력하고, 도착/목적지/우선순위/noise를 정책 독립적으로 생성한다. 실제 공장 고정 trace를 그대로 실행할 때는 기존 `run`/`suite`를 사용할 수 있다. 실제 trace를 모든 설계 변형에 대응시키는 import·교정은 후속 입력 작업이다. 참조 파일의 `calibration_status` 문자열만 바꾸어도 보정이 검증된 것으로 보지는 않는다.

## 준비한 입력 실행

E0 대표12셀·7정책 D0는84회다. seed는 각 입력에 이미 연결돼 있으므로 `run-prepared`에 별도의 seed 목록을 주지 않는다.

```bash
python -m simulation.evaluation run-prepared \
  --prepared /tmp/clover-v3-e0-inputs --timing D0 \
  --cells S00_low S01_eligible_1 S02_width_1p3 S03_on S04_anchor \
          S05_hotspot_90 S06_burst_4 S07_width_mix S08_two_cars \
          S09_station_active S10_lambda_0p26 S11_budget_2 \
  --run-wall-limit 120 --suite-wall-limit 1200 \
  --output /tmp/clover-v3-e0-d0
```

공통 staged D1의6개 대표셀·7정책은42회다.

```bash
python -m simulation.evaluation run-prepared \
  --prepared /tmp/clover-v3-e0-inputs --timing D1 \
  --cells S01_eligible_1 S02_width_1p3 S04_anchor S06_burst_4 S08_two_cars S11_budget_2 \
  --run-wall-limit 120 --suite-wall-limit 1200 \
  --output /tmp/clover-v3-e0-d1
```

`--policies baseline optimized rh_milp_grid rh_cpsat_linear`로 명시적으로 좁혀 실행할 수 있다. E1 후보의 native는 S01/S04/S08/S11 범위를 넘어 실행하도록 묵시적으로 확대하지 않는다. S11의0.5/2/10초는 D1 결정 예산이다. D0에서는 이 지연을 주입하지 않는다는 설명을 저장하므로, D0의 세 예산 셀을 운영 예산 효과로 해석하면 안 된다.

E0의 `e0_math.json`은 빠른 기능 검사를 위한30초 horizon·0.25초 solver 제한이다. E1 후보의40초·2초 설정과 성과 순위를 합치지 않는다. native의 finer 해상도·방문 제한도 설정에 남긴다. 실제 운용 설정은 E1에서 보정·선택한 뒤 고정한다.

## 결과와 무결성

- 부모 manifest가 셀·수요seed·정책 묶음·실제 D1 설정과 하위 suite 경로를 저장한다. 하위 suite는 기존 전체 사건·에너지·작업·검열·worker 결정을 보존한다.
- 시작 전 전체 입력/trace/초기 상태·설정·소스 해시를 검사한다. 폴더를 통째로 옮길 수 있으나 입력을 바꾸면 새 묶음을 만들어야 한다.
- 실행 도중 소스가 바뀌면 기존 결과를 남기고 나머지를 `SOURCE_CHANGED`로 기록한다.
- 예산 때문에 시작하지 못한 셀/정책은 `NOT_RUN_BUDGET`으로 남긴다. 이를 성공이나0점 성과로 바꾸지 않는다.
- 같은 셀·seed에서 실제 시작된 정책들의 입력 해시를 대조한다. 실패·미완료·fallback은 해당 정책의 결과로 보존한다.
- 생성되는 manifest는 E0/E1 후보에만 해당한다. E2/E3 held-out freeze, 통계적 유의성, 최종 추천을 자동 완료하지 않는다.

개별 결과의 `replay`·`analyze`, 계산 기록의 `profile`은 기존 CLI를 그대로 사용한다. 대표 입력의 자원 노출과 긴 충전 반복을 확인한 다음 실제 지도·λ·시간창을 보정한다.
