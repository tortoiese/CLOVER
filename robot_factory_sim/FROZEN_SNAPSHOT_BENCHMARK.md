# 같은 관측에서 MILP와 CP-SAT 비교하기

이 경로는 저장된 관측 하나의 공통 grid 모델을 비교한다. 공장의 처리량·충전 결과를 평가하는 `run-prepared`와 별개다. `rh_milp_grid`와 `rh_cpsat_linear`만 같은 정수 제약·두 목적·시작해를 사용한다. native/interval의 표현 차이는 이 실험에 섞지 않는다.

`robot_factory_sim`에서 프로젝트 Python 환경을 사용한다. worker spawn을 쓰므로 Python API를 직접 호출하는 script는 `if __name__ == '__main__':` 아래에서 실행한다. 명령행 경로는 이를 처리한다.

```sh
export QT_QPA_PLATFORM=offscreen CLOVER_RAG_ENABLED=0 CLOVER_RAG_ALLOW_NETWORK=0 PYTHONDONTWRITEBYTECODE=1
python -B -m simulation.evaluation freeze-snapshot \
  --run /absolute/path/to/saved-run.json --decision 1 \
  --output /tmp/clover-frozen-grid.json
python -B -m simulation.evaluation benchmark-snapshot \
  --snapshot /tmp/clover-frozen-grid.json --output /tmp/clover-frozen-results \
  --budgets 0.25 2 --repeats 3 --order-seed 20261114 \
  --startup-limit 10 --request-allowance 3 --memory-limit-mib 1024 \
  --suite-wall-limit 300
```

`--decision`은0부터 시작하는 명시적 index다. 기존 기록은 worker 반환 계획이 있어야 한다. [D1 v2](STUDY_PROTOCOL.md)처럼 RPC 전 CAS 입력이 남은 기록은 worker의 계획 반환이 없어도 사용할 수 있다. 이때 route/모델 준비는 export 시 새로 수행하며 원래 worker가 준비를 완료했다는 뜻이 아니다. 반환 계획이 존재하면 CAS 관측으로 재생성한 입력과도 대조한다. 기한 때문에 거절된 반환 계획 역시 사용할 수 있으며, 입력 출처·반환 여부·거절 이유와 원래 accepted 값을 provenance에 남긴다. 원래 실행 소스는 변경하지 않고 새 artifact의 생성 소스를 따로 기록한다.

동결 파일은 PreparedInput·모델/입력/시작해 해시·checked 시작해·공통 준비 비용을 포함한다. 파일을 다시 읽을 때 route를 실제 기하와 대조하고 공통 sparse Model을 한 번 재구성해 해시/시작해를 확인한다. 각 backend에 같은 객체의 격리된 사본을 보내며 저장용 pickle은 사용하지 않는다. source가 달라졌다면 원래 기록에서 새 artifact를 만들어야 한다. 과거 artifact의 source hash를 고치지 않는다.

계산 예산은 worker 안의 prebuilt solve, 즉 시작해 검사·compile·두 단계 search·결과 decode 비용이다. 공통 route/model/hint 생성·선택 backend 기동·IPC·독립 계획 검사는 별도 비용이다. `summary.csv`의 `backend_seconds`, `compile_seconds`, `search_seconds`, `backend_cpu_seconds`, `startup_wall_seconds`, `request_wall_seconds`, `validation_seconds`를 구분해서 읽는다. 일부 하위 시간은 중첩되므로 전부 합하지 않는다. RSS는 worker 샘플 peak이며 부모/전체 장비 peak나 순간 peak 보장이 아니다.

`backend_seconds`는 평가 worker에서 `Backend.solve` 호출 직전부터 반환 직후까지 직접 측정한다. `request_wall_seconds`는 IPC뿐 아니라 계산·검사·worker/parent 처리도 포함한 전체 요청 시간이며 IPC 단독 측정값이 아니다. 첫 계측 revision의 decode 누락과 수정 후 별도 전체 검수는 저장소의 R2 검수 보고서에 기록한다.

native time limit은 soft limit이다. 구조적으로 유효한 해라도 worker의 solve 시간이 예산을 넘으면 `SOFT_BUDGET_EXCEEDED`, `timed_eligible=false`로 남긴다. parent watchdog은 예산+명시적 요청 여유까지 프로세스를 보호한다. 요청 여유를 solver에 추가로 주는 예산으로 해석하지 않는다. 이 결과는 D1 전체 결정2초 준수를 입증하지 않는다.

반복은 같은 관측의 **기술 반복**이다. 독립 수요 seed나 새 공장으로 세지 않으며 구간·p값·운영 정책 추천을 생성하지 않는다. 목적값은 두 단계의 사전식 쌍으로 읽는다. 두 유효한 OPTIMAL 쌍이 다르면 `PARITY_MISMATCH`다. FEASIBLE·timeout·메모리/기동 실패·source/입력 변경·미실행을 보존하며 누락을0으로 채우지 않는다. 전체 manifest와 arm별 plan/worker/해제 증거를 확인한다. SUCCESS는 모든 예정 arm이 유효한 해를 기한 안에 반환했고 증명된 최적 목적 불일치가 없다는 뜻이다.

출력 덮어쓰기를 거부한다. 실행 중 앱 Python과 동결 파일을 수정하지 않고 같은 장비에서 직렬 실행한다. CLI는 SUCCESS에0, 실패/부분 결과에1, 입력 오류에2를 반환한다. 원시 plan은 클 수 있으므로 Git에는 프로토콜·해시·요약·감사 판정을, 전체 원본은 별도 저장소에 보존한다.
