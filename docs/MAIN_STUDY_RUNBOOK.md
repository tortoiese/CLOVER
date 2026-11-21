# 동결된 합성 본 실험 실행 안내

본 실험의 목적은 **어떤 환경에서 어떤 구현이 유리하고, 차이를 판단하기 어려운 환경은 무엇인지** 확인하는 것이다. 기본 정책은 optimized로 유지한다. 준비 검수와 본 실험 성능 결과를 구분한다. 최신 준비 판정·경로·예상 시간/저장량은 [검수 보고서](MAIN_STUDY_READINESS_REVIEW_20261114.md)를 따른다.

## 평가 대상과 조건

| 구분 | 조건 | 비교 |
|---|---|---|
| 핵심 | S00 저부하, S02 통로 폭3수준, S04 충전슬롯3수준, S06 평탄/버스트2/버스트4, S08 캐빈2수준, S10 지속 과부하2수준 | baseline·optimized·RH MILP·grid CP-SAT linear. S04/S08에는 native CP-SAT 추가 |
| 보조 | S01 공동 배차 batch, S03 교차로, S05 설비 집중, S07 로봇 속도 차이, S09 설비 중단/복구, S11 64노드 | 같은4정책. S01/S11에는 native 추가. 기전/지원 범위 기술통계 |
| 스트레스 | S09 강한 복구 부하, S11 256노드, S04 긴 충전 창 | 실패·지원 한계·예산·충전 노출 관찰. 확증 우세 판정에 포함하지 않음 |

모든 비교는 같은 작업 도착/UID/잡음·초기 SOC·지도·평가창을 공유한다. 폭/슬롯/캐빈 수준 사이에도 수요를 고정한다. S06은 실제 생성된 작업 수와 속성을 보존하며 도착을 압축한다. 목적지 비율과 실제 통로/캐빈 이용률은 구분해 결과로 확인한다.

핵심은 지도별10seed를 평균한 뒤 독립 지도 사이의 차이와 불확실성을 분석한다. 지도 내부의60개 seed를 독립 지도60개처럼 세지 않는다. 6지도 설계는 heldout894블록/3,894정책 실행과 stress27블록/126정책 실행이며 총921블록/4,020정책 실행이다. 저장소 여유만으로 사전 축소한3지도 설계라면 총501블록/2,190정책 실행이다. 실제 채택 설계는 `readiness.json`과 `jobs.json`에 고정된다.

## 이 컴퓨터에서 시작·이어하기

현재 bundle의 절대 경로와 Python 경로를 검수 보고서에서 확인해 변수에 지정한다. `robot_factory_sim` 디렉터리에서 실행하며, 프로토콜과 같은 Python/패키지/컴퓨터를 사용한다.

```sh
export QT_QPA_PLATFORM=offscreen
export CLOVER_RAG_ENABLED=0
export CLOVER_RAG_ALLOW_NETWORK=0
export PYTHONDONTWRITEBYTECODE=1

# CLOVER_PY와 CLOVER_BUNDLE은 검수 보고서의 확정 경로로 지정한다.
"$CLOVER_PY" -B -m simulation.evaluation execute-main-jobs \
  --bundle "$CLOVER_BUNDLE" --phase heldout --limit 10
```

한 블록은 같은 입력의4~5정책을 순서만 무작위로 돌리는 단위다. 위 명령은 새 블록을 최대10개 실행하고 반환한다. 같은 명령을 반복하면 이미 있는 블록을 덮어쓰지 않고 다음 미실행 블록으로 진행한다. `--limit`을 생략하면 해당 phase의 남은 블록을 직렬 실행한다. heldout이 끝나면 다음 명령으로 stress를 진행한다.

```sh
"$CLOVER_PY" -B -m simulation.evaluation execute-main-jobs \
  --bundle "$CLOVER_BUNDLE" --phase stress --limit 10
```

native를 쓰는 launcher는 이 컴퓨터에서 동시에 하나만 실행한다. launcher 잠금과 PID/프로세스 시작시각을 검사하며, 디스크가2GB 미만이면 다음 블록 전에 중단한다. 각 정책900초·블록6,000초 한도를 사용한다. 이 한도는 계산기의 wall time이며 시뮬레이션 시간과 다르다.

중단된 블록의 실패/부분/RUNNING 기록도 보존한다. 같은 입력을 성공할 때까지 자동 반복해 좋은 결과만 고르지 않는다. 수정이나 재실험이 필요하면 원본을 남기고 별도 변경 사유·프로토콜·전 조건 검증을 기록한다. 부분 실행을 보존했다는 사실이 그 결과를 정상 완료로 인정한다는 뜻은 아니다.

## 전체 수집과 등록 분석

모든 예정 블록의 manifest가 생긴 뒤 phase별로 수집한다. 누락·중복·원본 변조·다른 코드/예산의 자료는 거부하며 실패한 정책도 예정 분모에 남긴다. CLI 출력 폴더는 새 경로를 사용한다.

```sh
"$CLOVER_PY" -B -m simulation.evaluation collect-main-jobs \
  --bundle "$CLOVER_BUNDLE" --phase heldout --output "$CLOVER_BUNDLE/heldout_collection.json"
"$CLOVER_PY" -B -m simulation.evaluation study-analysis-plan \
  --manifest "$CLOVER_BUNDLE/heldout_collection.json" --output "$CLOVER_BUNDLE/heldout_analysis_plan.json"
"$CLOVER_PY" -B -m simulation.evaluation compare \
  --plan "$CLOVER_BUNDLE/heldout_analysis_plan.json" --output "$CLOVER_BUNDLE/heldout_analysis"
```

stressも同様にphaseと出力名を替えて別に分析する。JSON/CSVには差・区間・失敗/検閲・保留理由が残る。緊急SLAは入力priority=0の固有母集団を使い、分母0は0%としない。coreの150仮説だけが登録Holm familyであり、補助/ストレスの点推定を確証的な勝敗に混ぜない。

一般核心では処理量+0.1 tasks/min超、S06では緊急納期違反率5%p超の減少を実用基準とする。S06の処理量・共通の全体納期/相対エネルギー・安全guardrailも同時に確認する。点推定の大小だけで採用しない。自動環境選択/R7は別工程であり、このCLIはGUIの既定政策を書き換えない。

## 別環境で準備から再現する場合

Pythonソースをコミットした状態で新しい出力先を指定し、順に `calibrate-reference` → `prepare-main-study` → 生成された `canary_frozen.json` のtrainingを `run-study` → `check-main-ready` を実行する。CLIの `--help` に引数がある。容量の参照55実行、開発窓72実行、D1 canary28実行が必要になる。別マシンのready判定をこのマシンへ流用しない。

容量は固定D0 optimizedの有限観測窓の参照値であり、物理上限ではない。λはこの参照値と入力由来の移動・処理時間比から計算し、heldoutの性能を見て調整しない。最終結論は合成地図生成分布・固定設定・予算の範囲に限定する。

### 준비 명령 예시

`CLOVER_PREP`은 기존 결과가 없는 새 절대 경로로 정한다. 아래는 본 실험 시작 전 개발 검수 명령이다. `CLOVER_PY`와환경 변수·작업 디렉터리는 위와 같다.

```sh
CLOVER_PREP=/absolute/new-preparation
"$CLOVER_PY" -B -m simulation.evaluation calibrate-reference --output "$CLOVER_PREP/capacity"
"$CLOVER_PY" -B -m simulation.evaluation prepare-main-study \
  --capacity "$CLOVER_PREP/capacity/receipt.json" --output "$CLOVER_PREP/bundle6"
"$CLOVER_PY" -B -m simulation.evaluation run-study \
  --frozen "$CLOVER_PREP/bundle6/canary_frozen.json" --phase training \
  --output "$CLOVER_PREP/canary" --run-wall-limit 900 --suite-wall-limit 10800
"$CLOVER_PY" -B -m simulation.evaluation check-main-ready \
  --bundle "$CLOVER_PREP/bundle6" --canary "$CLOVER_PREP/canary/manifest.json" \
  --output "$CLOVER_PREP/bundle6/readiness.json"
```

canary의256노드 지원 한계 때문에 `run-study`가 PARTIAL/종료코드1일 수 있다. 원시 실패 사유를 확인한 뒤 준비 검사에서 정상 조건·허용된 스트레스 범위를 구분한다. 임의 오류를 무시하거나 실패 파일을 삭제하지 않는다. READY가 없으면 본 실험 실행을 거부한다. 디스크 계획치만 초과한 경우에는 사전 정의한3지도 축소를 새 bundle에 적용할 수 있으며, 같은 소스의 개발 창은 `--development-from`으로 검증 후 재사용한다.
