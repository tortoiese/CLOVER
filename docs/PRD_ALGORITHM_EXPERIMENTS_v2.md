# PRD v2.0 — 공장 로봇 공유자원 최적화와 환경별 알고리즘 선택

작성일: 2026-11-10 · 상태: 실행 명세 + 2026-11-13 구현 진척·잔여 작업 반영 · 초점: GUI 없는 기능 구현, 모델 정확성, 최적화, 재현 실험.

## 0. 이 작업의 최종 목적

최종 산출물은 “MILP를 붙였다”가 아니라 다음 질문에 근거를 갖고 답하는 것이다.

> 어떤 부하·통로·충전·설비·다층·계산 예산에서 가중치 휴리스틱, 배정 MILP,
> RH MILP, RH CP-SAT 중 어느 정책이 적합하며, 어떤 조건에서는 추천을 보류해야 하는가?

그 결론을 내리려면 **동일하고 올바른 공장 모델**, **같은 외부 수요와 초기 상태**,
**실제 행동까지 이어지는 계획**, **계산 비용을 포함한 공정한 비교**가 먼저 필요하다.
MILP나 CP-SAT의 우세를 완료 조건으로 미리 정하지 않는다. 특정 조건의 개선과 한계를 함께 남긴다.

이 문서는 미래 작업의 요구사항이다. 아래 숫자형 실험 수준·SLA는 제안값이며 검증된 공장 기준이 아니다.
아래 초기 점검은 2026-11-10 설계 당시 기록이다. 현재 기능·검수 완료 범위와 남은 작업은 [§20 최신 진척](#progress-20261113)을 따른다.

동반 문서:

- main 기능·알고리즘 점검 (선행 검토 문서; 본 저장소 밖의 원본): 재현 결과, 소스 근거, 수정 우선순위.
- 변수·시나리오 목록 (선행 검토 문서; 본 저장소 밖의 원본): 환경 58개, 알고리즘 설정 16개, 12개 필수 시나리오와 부정 검사.

## 1. 2026-11-10 기준 코드와 초기 점검 상태 (보존)

| 구분 | 확인한 상태 |
|---|---|
| main | [`a28522d9dd9aee238c27fdc2404cab2b1b10c60d`](https://github.com/tortoiese/CLOVER/commit/a28522d9dd9aee238c27fdc2404cab2b1b10c60d), 107개 파일 원격 hash 일치 |
| 기존 회귀 | macOS/Python 3.14에서 154개 통과. 이 결과와 새로운 모델 정확성 검사를 구분 |
| main 실시간 정책 | baseline, optimized(가중치 휴리스틱) |
| 별도 기존 MILP | SciPy/HiGHS의 유휴 로봇–작업–설비 배정. 충전·통행 시간표를 공동 최적화하지 않음 |
| 통행 기반 | PR #9의 `traffic-mm-bundle-v1` 반영: mm·slot, 원자적 묶음 확보, 안전 경계 해제, drain 후 구조 변경 |
| PR #10 | [`d63276d…`](https://github.com/tortoiese/CLOVER/pull/10), 열림·미병합·main과 충돌. 공통 정수 모델·두 backend·실행/실험 기능의 재사용 후보 |

구버전 PRD의 “통로 폭 검사·점유 보존을 처음 구현해야 한다”는 항목은 현재 상태로 갱신한다.
이미 해결한 통행 계약을 보존하고, 미통합 RH와 남은 모델 문제를 구분한다.

### 1.1 이번 점검에서 발견한 선행 문제

| 문제 | 이번 확인 결과 | 성능 결론에 미치는 영향 |
|---|---|---|
| 작업 중 에너지 부족 | 필요한 1.0 unit 중 배터리 0.1 unit만 쓰고 20초 작업 완료 | 불가능한 행동이 좋은 처리량으로 보임 |
| 데모 수요 상한 | 동일 RNG 수요 65건 중 42건만 생성, 23건 미기록 | 느린 정책의 누적 수요가 사라져 비교 왜곡 |
| λ=0 | 1000초 동안 적어도 9건 발생 | 빈 수요·외부 trace 실험 오염 가능 |
| 거리와 경로 비용 혼용 | 기본 공장 예에서 수평 155m를 가상 비용으로 195m 환산 | 다층 에너지·이동시간·후보 판단 오류 |
| 최근 표본으로 전체 평균 | 전체 600건 평균 16.67초를 최근 500건으로 0초 표시 | 장기 실험의 대기 지표 왜곡 |
| 캐빈 capacity=2 | 두 이동 중 `_motion`이 덮여 아직 이동 중인데 moving=False | 합승 실험의 물리 모델 불일치 |

세부 반례는 동반 점검 문서의 A01~A06이다. **기존 테스트가 통과해도 이 문제를 수정하기 전에는 우열 결론을 내리지 않는다.**

## 2. 범위와 공장 모델의 의미

### 2.1 V1 연구 모델

현재 엔진은 로봇이 설비로 이동한 뒤 설비와 함께 작업시간 내내 점유되는 **로봇–설비 협업 작업 모델**이다.
V1은 이 의미를 명시적으로 유지한다. 작업 1건의 완료를 완제품 1개나 AMR 운송 1건으로 바꿔 부르지 않는다.

V1에 포함한다:

- 단일 작업의 적합 설비 선택, 로봇 배정, 이동·대기·작업·부분 충전의 공동 계획.
- 폭이 다른 로봇, 방향성 경로, 통제 구간 묶음, 교차로, 충전기, 캐빈 상태.
- 캐빈당 승객 capacity=1, 서로 다른 독립 캐빈 여러 대.
- 작업·예약 수명, stale 계획, 계산 실패·지연, 복구·취소·설정 변경의 공통 안전 처리.
- headless 실행·평가·재현 CLI, 원시 로그, 환경별 추천 규칙과 검증.

### 2.2 이번 범위 밖 또는 별도 버전

GUI 확장, LLM·RAG에 의한 정책 조정, 실제 로봇 명령 송신, 산업 안전 인증, 연속 충돌 판정은 범위 밖이다.
AMR 운송형 pickup/drop와 설비 독립 가공, 다단계 공정·BOM, 실제 공간 버퍼,
합승 캐빈, payload 동역학은 V2로 분리한다. 필요한 경우 V1의 범위를 먼저 변경하고 plant·계획·검증기를 함께 구현한다.

폭·교차로 자원 근사는 차선별 좌표·회전 궤적·제동거리를 증명하지 않는다.
결과는 검증한 추상 공장 모델과 환경 범위에 대한 알고리즘 선택 근거다.

### 2.3 유지할 운영 원칙

- 기본 정책 `optimized`와 사용자 공장 DB를 임의로 변경하지 않는다.
- 정확성 수정은 모든 비교 정책의 공통 plant에 적용한다.
- 휴리스틱을 약하게 만들어 RH를 돋보이게 하지 않는다. 버그 수정 전후 정책 버전과 가중치를 기록한다.
- 기존 GUI·데모는 회귀 대상으로 유지하되 연구 runner의 초기화·데이터·지표와 분리한다.

## 3. 성공 조건

| 목표 | 완료 증거 |
|---|---|
| G1 정확한 실행 모델 | 작업·에너지·자원 불변식과 재현 반례 회귀 통과 |
| G2 RH 통합 | 동일 snapshot에서 계획 생성→검증→prefix 실행→관측→재계획 |
| G3 두 알고리즘의 공정한 구현 | 공통 모델의 MILP/CP-SAT 작은 최적값 일치, native CP 표현의 의미 동등성 |
| G4 실험 재현성 | 외부 trace·초기 상태·환경·source·solver·설정 hash와 replay |
| G5 환경별 선택 | 독립 환경·seed의 held-out 시험에서 정책 선택 효과·비용·불확실성 공개 |
| G6 실패의 투명성 | timeout/fallback/stale/unsupported/미완료를 누락 없이 결과에 포함 |

추천 불확실, 휴리스틱 우세, 두 RH 정책의 실질적 동등성도 유효한 연구 결론이다.
모든 결과가 중립이면 차이를 만들지 못한 이유와 검증된 선택 가능 범위를 설명한다.

## 4. 선행 수정 요구사항 — G0 게이트

| 요구 ID | 요구사항 | 우선순위·수용 기준 |
|---|---|---|
| FR-01 | 작업·이동 중 에너지의 시간 정산과 고갈 처리 | P0. A01 실패 완료가 사라지고 정상 완료·중단·복구의 장부 일치 |
| FR-02 | demand source 분리와 외부 trace | P0. λ=0/빈 trace 작업 0, 제안 수요의 무기록 손실 0 |
| FR-03 | 공정/설비 적합성 | P0. 고장 우회가 다른 공정으로 변하지 않음, 부적합 배정 0 |
| FR-04 | 경로 비용·수평 거리·시간 분리 | P0. 수직 비용이 주행 에너지에 들어가지 않음 |
| FR-05 | 전체 이벤트 장부와 측정 구간 | P0. bounded GUI 표본 대신 전체 합계·분포·검열을 연구 결과에 사용 |
| FR-06 | 캐빈 단일 운행 상태 | P0. capacity>1 명시적 거부, 빈 회송·승하차·목적층 상태 일치 |
| FR-07 | 명시적 작업 상태·소유권 | P0. 중복 배정/완료·미기록 유실·취소 작업 재실행·고아 예약 0 |
| FR-08 | long_wait와 교착 구분 | P0. 시간 임계치만으로 교착 판정하지 않음 |
| FR-09 | 초기 상태·plant profile | P1. SOC·위치·설비·캐빈 층을 같은 입력으로 재현 |
| FR-10 | 데모/실험 설정 분리 | P1. 데모 상수·예시 ROI·QThread·GUI 없이 runner 동작 |
| FR-11 | run별 예산·실험 전체 예산 분리 | P0. 같은 run 한도, 무작위 순서, 미시작 표본을 정책 실패로 오분류하지 않음 |

작업 상태는 최소 `offered → admitted/ rejected → queued → reserved → assigned → traveling → waiting → processing → completed`를 구분한다.
취소·실패·재큐잉은 별도 전이이며 재큐잉으로 새 외부 작업을 만든 것으로 세지 않는다.
실행 attempt 번호와 logical job uid를 분리한다.

보존식은 항상 검사한다:

```text
offered = rejected + admitted
admitted = completed + cancelled + permanently_failed + queued + reserved + active
B_now = B_initial + charged + rescue_injected - consumed
```

각 작업은 우변의 한 상태에만 속한다. 미래 미도래 trace는 offered에 넣지 않는다.
취소 전에 쓴 에너지·이동거리는 사라지지 않는다. 초기 SOC와 측정 구간 경계의 누적값도 보존한다.
로봇 추가·퇴역이 있으면 투입 SOC와 반출 SOC를 별도 inventory 항으로 기록하며, 같은 표시 ID의 재투입도 instance uid로 구분한다.

## 5. 기능 구조

권장 책임 경계는 다음과 같다. 파일명은 제안이며 기존 PR #10 모듈과 중복 생성하지 않는다.

```text
simulation/
  engine.py, robot.py, station.py, elevator.py   # 공통 plant
  resource_pool.py, traffic_rules.py, corridor.py# PR9 자원 계약
  planning/
    contracts.py, snapshot.py                   # 관측값만 담은 불변 입력
    semantic_model.py                          # 자원·시간·에너지·목적 의미
    model.py, backend.py                        # 공통 grid MILP/CP-SAT
    cp_interval.py                             # native CP scheduling
    validation.py, execution.py, controller.py  # 검증·prefix·재계획
    process.py, replay.py                       # 격리·중단·재현
  evaluation/
    scenario.py, trace.py, runner.py            # GUI/DB 없는 실행
    ledger.py, metrics.py, statistics.py        # 전체 계측·paired 분석
    selection.py, manifest.py                   # 선택 규칙·근거·버전
```

plant→evaluation/planning의 순환 import를 만들지 않는다. 정책은 공통 관측/행동 계약으로 plant에 접근한다.
연구 runner는 `seed_data` 데모 초기화나 현재 GUI 상태에 의존하지 않는다.
Qt와 RAG 패키지가 없어도 핵심 평가 CLI를 실행할 수 있도록 핵심/선택 의존성을 분리한다.

## 6. 공통 계약과 자원 수명

### 6.1 입력·출력

`ScenarioSpec`은 geometry, capability, plant profile, InitialState, demand/failure trace, measurement protocol을 가진다.
InitialState는 첫 dispatch 이벤트 이전에 적용한다. 테스트 편의를 위한 시작 후 위치·SOC 덮어쓰기를 정식 runner의 초기화 방식으로 사용하지 않는다.
`PlanningSnapshot`은 관측 시각, layout version, resource contract, 작업 uid·상태, 로봇 위치·SOC,
진행 중 행동의 남은 점유, 현재 소유자, 확정 prefix, 관측된 가용성과 nominal 처리시간을 담는다.
실제 미래 처리 표본과 아직 도착하지 않은 작업·고장 이벤트는 포함하지 않는다.

`PlanResult`는 후보 행동·예약, 목적별 incumbent/bound, solver 상태, model/semantic hash,
모델 크기, 각 단계 시간, 후보 포함률, 적용 범위, unsupported 사유를 포함한다.
`ExecutionReceipt`는 계획 수락·거부·지연·fallback과 실제 행동 token을 기록한다.

### 6.2 자원·단위

| 대상 | 단위·규칙 |
|---|---|
| 수평 길이 | m, 경로 탐색의 가상 비용과 분리 |
| 실행·예약 시간 | 절대 sim-sec, `[start,end)` |
| 계획 시간 | 정수 tick, duration·occupied end는 보수적으로 올림 |
| 통로 | PR9 정수 mm, `C=floor(1000(W-E))`, `q=ceil(1000(w+S))` |
| 교차로·설비·충전기·캐빈 | 명시적 slot; 캐빈 V1 capacity=1 |
| 배터리 | 기본 unit, 계획에서는 공통 quantum(초기안 .001 unit)으로 정수화 |

배터리 초기값·용량·충전 가능량은 안전 방향으로 내림, 소비량·요구 예비량은 올림한다.
반복 반올림의 누적 오차와 CP-SAT 정수 overflow, MILP 정수성·부동소수 허용오차를 따로 검사한다.
단위·contract version 불일치는 요청을 거부하며 적당한 값으로 clamp하지 않는다.

### 6.3 통제 구간과 계획 예약

PR9의 `section_end`, `section_demands`, `planned_reservations`를 공통 경계로 사용한다.
한 묶음의 모든 통로·교차로는 첫 진입부터 출구와 후미 여유까지 **동일한 전체 점유 구간**을 사용한다.
구간 내부 대기와 교차로 통과 시간도 포함한다. 개별 엣지의 주행 시간만 예약하는 기존 PR10 방식은 수정한다.

현재 claim은 `robot_id/amount/unit/contract`로 읽는다. 예전 `user.proc` 기반 소유자 추론을 이식하지 않는다.
미래 계획 예약과 실제 확보된 물리 소유권을 구분한다. 미래 예약을 이유로 H 전체의 자원을 지금 잡지 않는다.
V1 계획 예약은 검증된 실행 지침이며, 실제 진입은 PR9 arbiter가 다시 승인한다.
확률적 지연으로 시작이 밀리면 점유를 연장하고 영향받은 미실행 계획을 무효화·재계획한다.
계획 수락을 “예정 시각 진입 보장”으로 표시하지 않는다.

취소는 미승인 요청과 미실행 예약을 제거한다. 이미 진입·작업 중인 물리 점유는 명세된 안전 경계나 고장 복구까지 유지한다.
reset은 새 simulation epoch이며 이전 계산 결과를 수락하지 않는다.

## 7. Rolling Horizon 동작

한 주기는 관측 → 후보 생성 → 시간표 계산 → 독립 검증 → 실행 prefix 확정 → 실제 실행 → 재관측 순서다.
MILP와 CP-SAT은 이 컨트롤러에 연결하는 대체 계산기다. CP-SAT을 MILP의 필수 후처리로 두지 않는다.

필수 동작:

1. 주기 Δ와 중대 이벤트(긴급 도착·고장·취소·구조 변경·계획 이탈)로 재계획한다.
2. 진행 중 행동은 고정하고 미래의 미실행 suffix만 바꾼다.
3. 같은 로봇·작업에 서로 다른 요청이 동시에 commit되지 않도록 token과 상태 버전을 검사한다.
4. 첫 안전 행동 또는 작은 freeze 구간만 확정한다. 작업 중 로봇은 남은 점유를 고정 예약으로 반영한다.
5. 후보 수 제한은 두 backend에 동일 적용한다. 총 대기·후보 수·제외 이유·최대 제외 나이를 기록한다.
6. aging으로 낮은 우선순위·오래된 작업의 영구 제외를 막는다. 동점은 안정적인 uid 순서로 해소한다.
7. H보다 긴 작업·이동을 영구 탈락시키지 않는다. H 안에 시작한 서비스 행동은 H 이후 완료까지 tail/carry-over로 보존하고 자원·에너지를 끝까지 모델링한다.
8. solver timeout은 무해 증명이 아니다. 검증된 incumbent가 있으면 정책에 따라 사용하고, 없으면 공통 안전 휴리스틱 또는 hold를 사용한다.

이 문서에서 H는 **새 서비스 행동을 시작할 수 있는 구간**이다. 서비스 행동은 현재 위치에서 작업장으로의 이동과 그 작업의 처리까지 연결한 단위다.
`u[j]=1`은 이번 계획에서 그 서비스 행동의 시작을 H 안에 배치하지 않았다는 뜻이다. H 안에 실제 작업이 완료됐다는 지표가 아니다.
이동이 H보다 길어도 이동만 시작하고 작업은 영구 defer되는 일을 막도록, 선택한 첫 서비스의 이동–작업 연결을 끝까지 보장한다.
MILP는 복합 서비스 arc 또는 동등한 연결 제약으로, native CP는 연결된 이동·작업 interval로 같은 의미를 구현한다.
tail 상한은 `H + 최대 허용 서비스 길이`처럼 입력에서 산출하고, 새 서비스의 시작을 tail로 미루지 않는다.
이미 시작한 장기 행동은 다음 계획에서 남은 점유로 고정한다.
양 backend는 같은 시작 허용 구간·최종 완료 상한을 사용해야 한다.
꼬리 구간에 새 작업을 연속 배정해 사실상 H를 몰래 늘리지 않는다. 모델 크기 상한에 걸리면 명시적으로 실패·대체 실행을 기록한다.

### 7.1 시간 의미를 분리하는 두 평가 모드

**D0: 계산 품질 모드.** 관측 시각의 가상 시간을 고정한 채 계산한다. 동일 입력의 해 품질·모델 크기·계산 비용을 분석한다.
결정 계산 시간이 공장 성능에 영향을 주지 않으므로 이 결과만으로 실시간 운영 적합성을 결론내리지 않는다.

**D1: 결정 지연 반영 모드.** snapshot에서 결과 검증·commit까지의 지연 L을 측정하고,
실제 제어 기준 `1 wall-sec = 1 sim-sec`로 결과 적용을 늦춘다.
headless에서는 관측 시각 τ의 입력으로만 계산한 결과를 `τ+L` 적용 이벤트로 예약하고 그 사이 도착·진행을 plant가 처리하게 할 수 있다.
이는 가상 시간이 CPU 실행 속도나 GUI 배속에 따라 달라지는 것을 막는다.
결과 도착 시 현재 상태로 다시 검증하며, 허용 범위의 shift도 자원·SOC·납기를 재검사한다.

L에는 snapshot, route/candidate 생성, process 전달·대기, 모델 생성·컴파일, 두 목적 단계,
decode, validator, commit 비용을 포함한다. cold start와 warm call을 구분한다.
controller가 계산 중이라고 로봇·외부 수요의 가상 시계를 멈춘 결과를 D1으로 부르지 않는다.
최종 운영 추천의 기본 근거는 D1이며 D0은 원인 분석용이다.

## 8. MILP·CP-SAT 구현 명세

### 8.1 비교 정책과 구현 순서

| 정책 ID | 역할 |
|---|---|
| `baseline_rr` | 약한 기준선. 라운드로빈/FIFO+반응형 충전이라는 정확한 이름 사용 |
| `weighted_heuristic` | 주 대조군. 거리·에너지 단위를 수정하고 기존 우선순위·가중치·충전 규칙 보존 |
| `assignment_milp_legacy` | 현재 단일 배정 MILP. 원래 후보·충전 범위를 명시 |
| `rh_milp_grid` | 공통 정수 시간 확장 모델을 MILP backend로 계산 |
| `rh_cpsat_linear` | 같은 정수 변수·선형식·목적을 CP-SAT으로 계산 |
| `rh_cpsat_interval` | 같은 문제 의미를 native interval/NoOverlap/Cumulative로 표현한 확장 후보 |

첫 5개가 기본 비교다. 마지막 후보는 구현·동등성 검증 후 최종 비교에 추가한다.
MILP는 PR10의 OR-Tools/SCIP adapter를 우선 재사용한다. legacy는 SciPy/HiGHS를 유지한다.
라이브러리·solver 버전을 고정하고 실제 설치 버전과 설정을 manifest에 남긴다.
기존 PR10의 `baseline_fifo`, `rh_milp`, `rh_cpsat` 식별자는 각각 위 역할의 호환 alias로 읽을 수 있게 한다.
새 결과는 정확한 canonical ID와 모델 버전을 기록한다. 이전 JSON 계획을 새 자원 계약의 실행 계획으로 조용히 재해석하지 않는다.
계획·실험 schema 변경은 버전을 올리고, 지원하지 않는 이전 schema는 명시적 변환 또는 읽기 전용 분석으로 처리한다.

### 8.2 공통 수학적 의미

같은 입력에서 두 backend가 다른 문제를 풀지 않도록 `SemanticSpec`을 먼저 고정한다.

결정: 어떤 로봇이 어떤 적합 설비에서 어떤 작업을 언제 시작할지,
어떤 경로 후보로 이동할지, 어느 충전기에서 얼마 동안 충전할지, 언제 기다릴지를 선택한다.

최소 변수·제약:

| 요소 | 공통 요구 |
|---|---|
| 작업 선택 | 작업별 실행 선택 + defer가 1. 중복 작업·비호환 설비 선택 금지 |
| 로봇 흐름 | 현재 위치·가용 시각에서 시작하는 연속 행동 경로, 동시 행동 금지 |
| 시간 선후 | 이동 완료 뒤 작업/충전 시작, 선행 행동·not-before·기존 prefix 준수 |
| 설비 | 작업 시간 동안 로봇과 적합 설비 슬롯 동시 점유 |
| 충전 | 슬롯 수, 가용성, 충전 속도·에너지 증가·용량 상한 준수 |
| 통행 | 통제 구간 전체의 mm/slot 수요와 실제 고정 점유 합계가 용량 이하 |
| 배터리 | 초기/소비/충전/예비량의 일관성, 작업 종료·terminal에서 충전소 복귀 가능 에너지 |
| 캐빈 | 층·문·빈 회송·승차·하차를 연결한 단일 상태 경로, 순간이동 금지 |
| tail | H 안에서 시작한 장기 행동의 H 이후 점유와 에너지 보존 |
| 제외·불능 | 단절 경로·적합 설비 없음·지원 불가를 각각 설명하고 숨기지 않음 |

충전소가 없거나 도달 불가이면 복귀 에너지를 0으로 두지 않는다.
terminal 제약이 모든 행동을 부당하게 막는 경우와 실제 에너지 불가능을 구분해 로그·작은 예제로 점검한다.
V1은 K=1 경로다. K>1을 추가하면 같은 후보를 모든 RH 표현에 제공한다.

### 8.3 MILP formulation

기본 구현은 PR10의 시간 확장 행동 arc를 사용한다. 각 arc는 로봇·출발·도착·시작·끝·행동·에너지·자원 점유를 갖는다.
이진 `x[r,a]`, defer `u[j]`, 정수 배터리 `B[r,t]`, 충전 증가량 `g[r,t]`를 둔다.

```text
로봇 흐름: 각 상태에서 outgoing(x) - incoming(x) = 초기 공급 또는 terminal 조건
작업:      Σ work_arc(r,j,a) x[r,a] + u[j] = 1
자원:      fixed_use[k,t] + Σ q[r,a,k,t] x[r,a] <= C[k,t]
배터리:    B[r,t+1] = B[r,t] - consumption[r,t] + g[r,t]
충전:      0 <= g[r,t] <= conservative_rate[t] * selected_charge[r,t]
범위:      0 <= B <= capacity, 안전 예비·복귀 제약은 해당 행동 종료 시점에 적용
```

자원 계수는 엣지 주행 시간 대신 공통 통제 구간 interval에서 생성한다.
밀집 행렬 대신 sparse 행렬을 사용하고, 불가능한 arc는 근거를 남기고 생성 전 제거한다.
Big-M이 필요한 추가 표현에는 변수의 실제 상한으로 M을 계산하며 임의의 거대한 값을 쓰지 않는다.

### 8.4 CP-SAT formulation

1차로 같은 grid 모델의 정수식·목적을 그대로 풀어 backend 차이를 분리한다.
이 단계에서 MILP와 같은 모델 hash, 단위, 후보, terminal, two-phase 예산을 사용한다.

그 다음 native scheduling을 구현한다:

- 선택 가능한 작업·이동·충전은 presence를 가진 optional interval로 표현한다.
- 로봇·단일 설비·교차로·캐빈은 해당 의미의 NoOverlap 또는 상태 전이 제약을 둔다.
- 충전 슬롯과 통로 폭은 Cumulative의 정수 수요/용량으로 표현한다.
- 묶음 구간은 자원마다 같은 `[entry, tail_release)` interval을 사용한다.
- 캐빈 NoOverlap만으로 층 연속성이 생기는 것은 아니다. 빈 회송과 출발층·도착층 전이도 제약한다.
- 단위·시간 domain·충전 반올림·tail·목적이 canonical 모델과 같아야 한다.

native와 grid의 모델 hash는 달라도 된다. 대신 같은 `SemanticSpecHash`와 독립 validator로 행동 의미를 검증한다.
작은 문제에서 가능한 일정과 최적 목적이 달라지면 표현 동등성을 해결하기 전 성능 비교에 넣지 않는다.
native 구현 없이 측정했다면 결론은 “공통 선형 표현에서의 backend 비교”로 제한한다.

### 8.5 목적함수와 평가 목표

안전은 가중 비용으로 타협하지 않고 hard constraint로 둔다.
초기 공통 목적은 두 단계다.

1. 가중 defer를 줄인다. 후보 작업의 중요도·대기 aging을 반영한다. `u[j]`는 §7의 서비스 행동 시작 기준이며 모든 표현에서 동일하다.
2. 1단계가 증명된 최적값이면 그 값을 고정하고, 대기·예상 지각·이동·충전 점유·불필요한 계획 변경 비용을 줄인다.

1단계가 시간 내 미증명 feasible이면 2단계가 이를 악화시키지 않도록 하되 전체 lexicographic optimal이라고 부르지 않는다.
1단계 incumbent를 먼저 보관하고 2단계 timeout/예외 때도 검증된 해를 유지한다.
충전·대기만 반복하며 중요한 작업을 영구 미루는 계획을 작은 starvation 검사로 배제한다.

계수는 단위를 정규화한 `objective_profile`로 버전 관리한다. validation에서 계수를 바꾸지 않는다.
스케줄러 내부 목적값이 좋아졌다는 사실만으로 공장 처리량·에너지·납기가 개선됐다고 판단하지 않는다.

## 9. 실행 검증·실패 처리

반드시 독립 validator가 다음을 검사한다.

- 정수성·변수 범위·중복 작업·설비 적합성·로봇 행동 연속성.
- 모든 실제 자원 수요와 예약의 단위·버전·용량, 고정 점유와의 중첩.
- 현재 SOC·행동 에너지·복귀 가능성·충전량, 노후 snapshot의 위치·가용성 차이.
- 캐빈 층 경로와 실제 객체 일치, 통제 구간 후미 여유, tail 범위.
- 이미 취소·퇴역·재배정된 작업이나 오래된 simulation epoch의 결과 여부.

실패 상태는 `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `NO_INCUMBENT_TIMEOUT`, `MODEL_INVALID`,
`BUILD_LIMIT`, `MEMORY_LIMIT`, `HARD_TIMEOUT`, `STALE`, `UNSUPPORTED`, `CANCELLED`, `ERROR`를 구분한다.
solver bound/gap은 그 snapshot·후보·격자·horizon 모델의 값이다. 전체 공장 기간의 전역 최적성으로 설명하지 않는다.

공통 fallback은 물리 제약과 적합성·충전 복귀 가능성을 확인하는 안전 휴리스틱이다.
없는 경로·에너지 문제를 무리한 배정으로 돌파하지 않고 hold한다.
fallback의 행동·시간·완료 작업도 해당 정책의 최종 성과에 포함한다. 정상 solver 결과만 따로 골라 보고하지 않는다.
solver process의 강제 종료·재시작·메모리 상한·자식 프로세스 정리를 Windows/macOS/Linux에서 검사한다.
의존성 부재는 별도 사유이며, 실험 예산 때문에 시작하지 않은 run은 `NOT_RUN_BUDGET`으로 보존한다.
각 run watchdog과 실험 전체 중단 예산을 구분하여 고정 순서의 뒤 정책만 불리해지지 않게 한다.

## 10. 실험 입력과 공정성

모든 정책에 같은 `ScenarioSpec + InitialState + WorkloadTrace + InterventionTrace`를 제공한다.
외부 trace는 실행 전에 만들되 planner는 도착 시각 이전 항목과 실제 noise를 보지 못한다.
arrival/service/failure/initial/solver 난수 stream을 분리하고, 서비스 표본은 logical job uid와 공정에 묶는다.
정책별 실행 순서가 달라도 같은 작업의 공통 난수가 유지되게 한다.

설비 선택에 따라 분포가 다르면 같은 공통 uniform 표본을 해당 설비 분포의 역누적함수에 적용하는 등 coupling 규칙을 고정한다.
미래 표본을 nominal duration으로 넣지 않는다.

비교는 두 종류로 나눈다.

- **Backend 비교:** 같은 후보·grid·목적·hint·단일 thread·전체 wall 예산의 RH MILP vs RH CP-SAT(linear).
- **운영 정책 비교:** 베이스라인·휴리스틱·배정 MILP·RH·native CP의 최종 공장 성과. 모델 표현·기능 차이와 계산 비용을 공개한다.

모든 정책에 같은 평가 규칙을 적용하지만 legacy와 RH를 동일 수학 문제를 푸는 solver라고 주장하지 않는다.
FIFO만 이기고 강한 휴리스틱에 지는 결과를 RH 효과 입증으로 채택하지 않는다.

## 11. 실험 설계

### 11.1 필수 축

주요 환경 축은 대수·부하, 통로 폭·길이·역방향 흐름, 교차로 수,
설비 처리시간·변동성·적합 설비 수, 충전 슬롯·속도·초기 SOC,
층간 작업·캐빈 시간, 긴급·취소·고장, 계산 예산·규모다.
자세한 값과 현재/추가 구현 구분은 동반 목록의 E01~E58, A01~A16을 따른다.

S00~S11은 여유 환경, 공동 배차, 좁은 통로, 교차로, 충전 부족, 설비 병목,
긴급 burst, 이종 로봇, 다층, 외란, 과부하, 규모·계산 제한을 포함한다.
유리한 배치를 골라 seed만 바꾸는 실험과 토폴로지·거리·흐름 자체를 바꾸는 일반화 시험을 구분한다.

### 11.2 단계와 표본

기본안은 E0 180회, E1 1200회, E2 1200회, E3 120회, 합계 2700회다.
native CP를 같은 E2 조건에 추가하면 240회가 더 필요하다. 세부 계산은 동반 목록에 있다.
전수조합을 피하고 12개 family를 층화하여 표본화한다. 알려진 상호작용은 별도 2차 설계로 확인한다.[R5]

E0는 기능 확인, E1은 모델·설정 튜닝과 선택 규칙 학습, **E2는 공개 전 잠근 최종 held-out 평가**다.
E2는 E1과 다른 seed뿐 아니라 다른 배치·거리·방향 조합을 사용한다. E3 stress도 튜닝에 쓰지 않는다.
의사결정 규칙의 최소 검증 지지집합 초안은 leaf당 서로 다른 환경 3개와 paired run 30개다.
E2가 이 조건을 충족하지 못하는 세부 규칙은 일반화 추천 대신 해당 cell의 결과만 제시한다.
native CP의 코드·파라미터도 E2 공개 전에 고정한다. 그렇지 못하면 native의 최종 검증용 새 환경·seed를 확보한다.

초기 seed 이름 공간 제안은 discovery 1000대, tuning 10000대, held-out 20000대, stress 30000대다.
실제 정수 목록은 generator 버전·환경 hash와 함께 `protocol.json`에 저장한다. 같은 seed 숫자만으로 재현성을 주장하지 않는다.
정책 실행 순서는 seed block 안에서 무작위로 배치하고 같은 기계에서 solver들을 동시 경쟁시키지 않는다.

### 11.3 기간·초기 상태

- 기전 smoke는 약 600초로 시작하되 최종 성능 근거로 쓰지 않는다.
- 정상 부하 stream은 pilot에서 warm-up W와 측정 T를 결정한다. 초기안 W≥300초 및 대표 작업 사이클의 5배, T≥1800초 및 대표 사이클의 20배를 검토한다.
- 충전 시나리오는 충전·방전의 여러 순환이 포함되는지 확인한다. 만충전 짧은 실행만으로 충전 최적화 효과를 판단하지 않는다.
- 유한 batch는 별도로 makespan·완료율을 측정한다. stream warm-up 결과와 같은 평균에 섞지 않는다.
- 과부하·급변 상황은 정상상태 가정을 하지 않고 유한 구간의 backlog 증가와 서비스 실패를 측정한다.

W·T는 결과를 본 뒤 유리하게 늘리거나 줄이지 않는다. 대표 사이클이 너무 길어 정한 T가 부족하면 해당 조건의 장기 효과 결론을 보류한다.
측정창 `[W,W+T)`의 event-rate와 그 안에 도착한 cohort의 지연을 구분한다.
측정 이후에 추적하는 drain 기간이 있으면 도착 중단 여부·상한·추가 관측 완료를 별도 필드로 기록한다.

### 11.4 계산 자원 예산

실험 수보다 solver 호출 수가 비용을 지배할 수 있다. pilot에서 다음 값을 예측한다.

```text
추정 solver wall 비용 ≈ Σ RH run {ceil((W+T)/Δ) × 1회 전체 결정 지연}
```

event trigger·빈 큐 skip·warm start의 실제 호출 수로 보정하고 CPU-hour·메모리·동시 worker 한도를 정한다.
E2의 검증 기간을 정한 후 계산이 비싸다는 이유로 일부 정책만 짧게 실행하거나 불리한 seed를 삭제하지 않는다.
예산이 부족하면 E2 공개 전 환경 수·반복·기간을 재설계하여 protocol 버전을 바꾼다.
도중 예산 종료는 partial로 남기며 완료된 결과만으로 원래 검증 전체를 통과했다고 쓰지 않는다.

## 12. 계측과 지표

| 범주 | 필수 지표·분모 |
|---|---|
| 정확성 | 작업·에너지 보존 오차, 중복·부적합 배정, 자원 용량 위반, 고아 예약 |
| 처리량 | 측정창의 실제 완료 건수/T. 최근 120초 화면 throughput과 분리 |
| 서비스 | offered/admitted/rejected, 완료·미완료·취소·영구실패, due≤평가시각인 전체 대상의 납기 위반 |
| 시간 | 생성→배정, 배정→작업 시작, 생성→완료, 긴급 응답; 평균·P50·P95·최대·표본 수 |
| 검열 | 종료 시 미시작·미완료 나이·건수, 완결 관찰 비율, P95 산출 가능 여부 |
| backlog | WIP(t) 적분, 구간말 backlog·증가율, 우선순위별 최대 나이·기아 |
| 자원 | 통로 mm·교차로·설비·충전·캐빈 이용률과 대기 episode, 빈 회송 시간 |
| 에너지 | 주행/작업/유휴/대기 소비, 충전 증가·구조 주입, 완료 1건당 소비 unit |
| 복구 | 배터리 고갈·기계 정지·복구 횟수, 중단 작업·재시작 비용, 구조 에너지 |
| 계산 | capture/path/build/compile/solve/validate/commit/전체 지연 P50·P95·P99, deadline miss |
| 모델·실패 | 변수·제약·arc 수, 후보 포함률, incumbent·bound·gap, fallback/stale/unsupported·메모리 |

미완료 작업을 제거한 지연 P95만으로 승자를 정하지 않는다.
완료 관측 P95는 조건부 통계로 표시하고, 검열이 있으면 backlog·due cohort 위반과 함께 판단한다.
생존분석을 도입할 경우 식별 불가한 P95는 null로 남긴다. 관측되지 않은 tail을 0으로 만들지 않는다.

가동률은 생산 작업·이동·자원 대기·충전·유휴·고장을 분리한다.
대기가 늘어 가동률이 높아진 것을 성능 개선으로 해석하지 않는다.
묶음 요청의 대기를 여러 자원에 중복 합산하지 않는다. 최초 blocker 분류와 blocker별 시간 분해는 다른 지표다.

에너지 unit→Wh와 원가·매출은 보정 자료가 있을 때만 계산한다. 예시 ROI는 기본 선택 목표에서 제외한다.
중단·미완료·취소 작업에 쓴 에너지와 초기/최종 SOC 차이를 누락하지 않는다.
에너지/건의 기본 분자는 측정창 전체 소비, 분모는 같은 창의 실제 완료 건수다. cohort별 추정은 별도 필드로 분리한다.

## 13. 통계 분석과 우세 판정

기본 관측 단위는 **환경·seed별 한 run**이다. 한 run의 수천 작업을 독립 반복처럼 취급하지 않는다.
같은 환경·seed의 정책 쌍으로 차이를 계산하고, 강한 휴리스틱 기준과 MILP–CP-SAT 직접 비교를 모두 제공한다.

필수 분석:

1. 원시 run별 값·실패율·효과 크기와 기본 95% paired 신뢰구간.
2. 사전 지정한 평균 차이 분석과 seed-pair bootstrap 민감도 분석. 결과를 보고 더 유리한 방법을 고르지 않음.
3. 다중 정책/주요 지표의 사전 지정 비교에 Holm 등 보정. 보정하지 않은 탐색 결과는 탐색으로 표시.
4. seed뿐 아니라 layout family가 다른 일반화 분석. 집계 가중치는 실제 배포 환경 비율 또는 균등 family로 미리 고정.
5. 실패·fallback·시간 제한·미완료 표본 보존. 실패한 run을 빼고 성공 run 평균만으로 추천하지 않음.

통계적 비유의는 동등성 증거가 아니다. 효과가 불확실한 경우와 사전 정한 실질적 동등 구간 안에 충분히 들어온 경우를 분리한다.
초기 실질적 효과 기준안은 처리량 +5%, 납기 위반율 -5 percentage point, 에너지/건 -5%, 처리량 비열등 한계 -2%다.
이는 프로젝트의 제안 기준이다. pilot 이후 held-out 공개 전에 profile별 한 개의 주 목표와 보조 제한을 고정한다.
기준선 0건·납기 대상 없음·완료 부족에서는 비율을 나누지 말고 절대값·판정 불가를 사용한다.
유의성만으로 개선이라 하지 않고 사전 효과 기준을 함께 충족해야 한다.
동등성은 미리 정한 margin 안에 양측 구간이 충분히 들어오는지 또는 사전 지정한 동등성 검정으로 판단한다.
보정 없는 95% 구간을 여러 환경의 동시 95% 보장으로 해석하지 않는다.

10개 seed로 구간이 넓으면 사전 규칙에 따라 같은 cell의 모든 정책을 30개 seed까지 확대한다.
반복 중간 결과를 보고 유리할 때 멈추지 않으며, 중간 검정이 필요하면 별도 순차 추론 설계를 쓴다.

## 14. 환경별 알고리즘 선택 규칙

### 14.1 단계적 판정

1. **적용 가능성:** 입력·모델 지원 범위, 공통 안전 불변식, 메모리·결정 기한 충족 여부 확인.
2. **운영 목표:** 처리량 중심 / 납기 중심 / 에너지 중심 중 배포 목표를 선택.
3. **비열등·개선:** 강한 휴리스틱 대비 성과 악화 허용 범위와 개선 기준을 paired 근거로 확인.
4. **계산·안정성:** 동등한 성과면 전체 결정 지연·CPU·메모리·fallback가 작은 정책 우선.
5. **불확실성:** 표본·환경 범위가 부족하거나 상충하면 추천 보류 또는 검증된 기본 휴리스틱 유지.

초기 운영 한도안은 D1 결정 기한 미준수 관측 비율≤1%, 정상 범위 fallback≤5%다.
실제 허용값은 protocol에서 확정하고 구간·분모를 함께 보고한다. 이 비율을 만족해도 안전 위반은 허용하지 않는다.
성능은 solver가 낸 정상 해만이 아니라 fallback을 포함한 전체 정책의 성과다.

목표별 예:

| profile | 주 목표 | 보조 제한 |
|---|---|---|
| throughput | 전체 측정창 완료율 | 납기·기아·미완료가 사전 한도를 넘지 않음 |
| deadline | 전체 due cohort의 위반율·긴급 대응 | 처리량 비열등·일반 작업 기아 한도 |
| energy | 완료 1건당 공통 에너지 | 처리량 비열등·납기 SLA; 구조 에너지 별도 포함 |

### 14.2 도출할 가설과 결론의 구분

현재의 합리적인 가설은 저부하·짧은 결정 예산에서는 휴리스틱이 충분할 수 있고,
배정·충전·시간 결합이 강하면 RH 공동 계획의 가치가 커질 수 있으며,
복잡한 이산 자원 충돌에서는 native CP 표현이 유리할 가능성이 있다는 것이다.
MILP/CP-SAT의 상대 성능은 크기·격자·표현·예산에 따라 달라지므로 이를 결론으로 선기입하지 않는다.

최종 표는 아래 형식을 따른다.

| 환경의 검증된 범위 | 권장 정책·정확한 모델/backend 버전 | 성과 차이·CI·표본 수 | 결정 예산·fallback | 예외·판정 수준 |
|---|---|---|---|---|
| 실험 이후 작성 | 실험 이후 작성 | 원시 결과에서 생성 | D1 기준 | 추천 / 동등 / 불확실 / 지원 불가 |

### 14.3 해석 가능한 선택기

E1에서 관측 가능한 특징으로 깊이≤3의 규칙표/결정트리를 만들 수 있다.
후보 특징은 대기 작업/대수, 최근 도착 추정, 통로·충전 압력, 로봇 이질성,
층간 비율, 모델 크기 예상, 허용 계산 예산이다.
미래 전체 trace에서 계산한 진짜 부하나 최종 결과를 입력 특징으로 쓰지 않는다.

규칙과 임계값을 E1에서 고정하고 E2에서 선택 정책을 실제 closed-loop로 평가한다.
“각 환경의 사후 최고 결과를 골랐을 때”의 oracle 성과와 실제 선택기 성과를 구분한다.
선택기가 기본 휴리스틱 또는 최고 고정 정책 대비 얼마나 이득/손실인지, 검증된 oracle 대비 regret를 보고한다.
검증되지 않은 범위에서는 abstain한다. 온라인 정책 전환은 현재 prefix·점유·예약을 보존하는 별도 기능이며 이번 최종 보고서만으로 자동 활성화하지 않는다.

## 15. 최적화 작업의 우선순위

| 단계 | 개선 후보 | 완료 판단 |
|---|---|---|
| 정확성 이후 1차 | 경로·width·거리/시간 캐시, 후보 전처리, sparse 행렬 | 같은 semantic hash·해 의미, build 비용 감소 |
| 2차 | feasible hint, incumbent 재사용, 불가능 arc 제거 | 작은 최적값 유지, 실패·시간 초과 감소 |
| 3차 | CP interval 표현, horizon·격자·후보수의 훈련 기반 설정 | 독립 hold-out에서 품질·지연 trade-off 개선 |
| 규모 확장 | 영역 분해·계층 계획·근사 배정, 선택적 재계획 | 별도 policy ID, 근사·지원 한도 공개 |

프로파일링 없이 최적화를 시작하지 않는다. 단계별 build/solve/validate 시간을 먼저 측정한다.
후보 축소·느슨한 모델·scope 변경으로 빨라진 결과를 동일 문제의 solver 속도 개선으로 부르지 않는다.
후보 aging·starvation·hard constraint를 제거하는 속도 개선은 허용하지 않는다.

## 16. 테스트·검수 기준

| 검사군 | 필수 예시 | 수용 기준 |
|---|---|---|
| 단위·경계 | mm 반올림, 시간 반개구간, 에너지 quantum, 충전 0/만충전 | 양 표현·실행 일치 |
| 상태·에너지 | 작업 고갈·취소·고장·구조 주입·부분 작업 | 보존 오차 허용 quantum 이내, 무료 완료 0 |
| 자원 수명 | 대기 취소·진입 후 퇴역·layout drain·reset | 부분 확보·고아 소유·이전 epoch 실행 0 |
| 작은 최적값 | ≤2로봇·≤4작업·2설비의 손계산/독립 완전탐색 | 두 목적 최적값과 feasible 행동 일치 |
| 표현 동등성 | grid MILP/CP linear/native CP의 작은 동일 문제 | 같은 semantic 가능 일정·목적 |
| 계획–실행 | 결정적 처리·외란 없음 | 계획·실제 점유/시각 오차가 정한 격자·보수화 범위 이내 |
| 확률·외란 | 지연·고장·취소·stale·solver crash | 안전 유지, 재계획·대체 원인 추적 |
| 실험 공정성 | trace·noise·initial hash, 순서 변경 | 입력 일치, 미래 정보 노출 0 |
| 장기·과부하 | 로그 상한·미완료·버퍼 손실·고갈 반복 | 누락 없는 계측·지원 한도·정상 종료 |
| 플랫폼 | Python 3.11/3.14, Linux/macOS/Windows | 핵심 headless·process 종료·계약 검사 통과 |

공통 모델을 그대로 읽어 만든 검사기만으로 모델을 검증하지 않는다.
실제 실행기의 불변식과 독립 손계산 예제를 별도로 둔다.
FIFO 동점 순서·같은 timestamp의 이벤트 순서는 SimPy 실행 순서에 영향을 받으므로 명시적으로 고정하고 순서 민감도도 검사한다.[R4]

## 17. 개발 단계와 완료 게이트

| 단계 | 작업 | 다음 단계 진입 조건 |
|---|---|---|
| P0 기준 확정 | 공장 작업 의미·지원 범위·단위·에너지·장부 결정 | FR-01~08·11 반례 회귀·불변식 통과 |
| P1 평가 기반 | Scenario/InitialState/trace/headless CLI/전체 계측 | 동일 입력 replay·빈 수요·수요 보존 통과 |
| P2 RH MILP 통합 | PR10 충돌 해소·새 자원 adapter·grid model·prefix | 작은 최적값·자원 수명·장기 작업 통과 |
| P3 CP-SAT 통합 | linear parity→native interval→의미 동등성 | 동일 문제 검증·독립 validator 통과 |
| P4 프로파일링·파일럿 | D0/D1·예산·기간·기전 확인·training 설정 | E2 전에 protocol·코드·설정 freeze |
| P5 실험·분석 | E1 탐색, E2 held-out, E3 stress, native 추가 시험 | 누락 없는 원시 결과·실패·paired 분석 |
| P6 결론 | 환경별 규칙·선택기·한계·재현 package | 주장별 근거·미검증 범위·기본 정책 회귀 확인 |

P0~P3는 기능 완료, P4~P6는 효과와 적용 조건의 검증이다.
기존 PRD 구현물은 재사용할 수 있지만 “파일이 존재한다”만으로 이번 게이트를 완료 처리하지 않는다.
시간 추정은 pilot의 실제 solver/실험 비용으로 산정한다. 지금 확정된 개발일 수나 성능 향상률은 제시하지 않는다.

## 18. CLI·산출물 요구

아래는 **구현할 명령 형태**이며 현재 main에서 이미 동작한다는 뜻이 아니다.

```bash
python -m simulation.evaluation validate --scenario cases/s04.yaml
python -m simulation.evaluation run --protocol protocol.json --output results/
python -m simulation.evaluation replay --run results/run.json
python -m simulation.evaluation analyze --manifest results/manifest.json
python -m simulation.evaluation recommend --features observed.json --rules selection_rules.json
```

최종 산출물:

1. source/환경/설정/trace/initial/semantic/model hash와 seed·solver·thread manifest.
2. 모든 환경·정책·반복의 상태·실패·원시 이벤트와 전체 계측 결과.
3. 실제 적용 계획·실행·fallback·계획 이탈 및 재현 snapshot.
4. 공통 모델 수식·제약·목적·양 backend 차이와 native 표현 설명.
5. 환경별 효과·불확실성·계산 비용 표와 알고리즘 선택 규칙.
6. 휴리스틱·MILP·CP-SAT 각각의 유리·불리·불확실 조건을 포함한 보고서.

작업 완료는 GUI 시연 영상이나 throughput 그래프 한 장으로 대신하지 않는다.
유효하지 않은 물리 모델·수요 누락·완료 건만의 통계·solver 기한 미반영이 남으면 최종 선택 결론 단계로 가지 않는다.

## 19. 참고 근거

프로젝트 상태는 위의 commit에 고정했다. 아래 외부 자료는 모델링/API·실험 설계의 근거이며 CLOVER 성능의 실증 근거가 아니다.

- [R1 SciPy milp 공식 문서](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html): 선형 정수 제약, 시간·gap 제한과 반환 상태. 기존 배정 MILP 범위를 해석하는 기준.
- [R2 OR-Tools CP-SAT 공식 문서](https://developers.google.com/optimization/cp/cp_solver): 정수 모델과 solver 상태. 정수화와 feasible/optimal 구분에 사용.
- [R3 OR-Tools scheduling recipes](https://github.com/google/or-tools/blob/stable/ortools/sat/docs/scheduling.md), [Job Shop 공식 예제](https://developers.google.com/optimization/scheduling/job_shop): interval·선후·자원 충돌 표현. native CP 요구의 근거.
- [R4 SimPy Time and Scheduling](https://simpy.readthedocs.io/en/stable/topical_guides/time_and_scheduling.html): 가상 시간·순차 이벤트·동시 timestamp의 처리 순서. D0/D1 분리와 순서 검사 근거.
- [R5 NIST 실험 설계 안내](https://itl.nist.gov/div898/handbook/pri/section3/pri3.htm): 목표·변수·수준·설계·blocking과 부분 요인 실험의 구분.

어떤 solver가 특정 공장 조건에서 더 좋을지는 이 자료만으로 확정하지 않는다. 그 질문에 답하는 것이 본 PRD의 실험 작업이다.

<a id="progress-20261113"></a>

## 20. 2026-11-13 구현 진척·보완 계획·병합 현황

이 절은 설계 당시의 §1과 구분한 실제 진행 기록이다. **main에 반영된 상태가 아니라 기능 브랜치의 상태**다.
검수한 기능 코드는 `b618506`, 실험·문서 정리 기준은 `7aa12dc`이며, 이번 변경은 PRD와 병합 검사 기록만 갱신한다.
기존 검수의 소스·원시 결과·코드 hash를 새 실행 결과로 바꾸지 않는다.

### 20.1 어디까지 구현됐는가

| 단계 | 현재 상태 | 구현·검수 근거 | 남은 경계 |
|---|---|---|---|
| P0 공통 모델 | V1 범위 기능 완료 | mm/slot 계약, 묶음 확보·후미 해제, 작업·에너지 장부, 단일 승객 캐빈, 취소/중단 불변식 | 실제 로봇·연속 충돌·합승·pickup/drop 모델은 범위 밖 |
| P1 headless 기반 | 기능 완료 | 외부 trace·초기 상태, run/suite/replay/analyze/profile, 전체 사건·검열·캐빈 단계 | suite 통계·별도 drain 관찰·최종 추천 CLI는 남음 |
| P2 RH MILP | headless 기능 완료, 저장소 통합 미완료 | 공통 snapshot→SCIP 두 목적→독립 검사→첫 prefix→재계획, 충전·tail·자원 달력 | PR #10 충돌 해소/GUI 결합/실제 main 병합은 수행하지 않음 |
| P3 CP-SAT | 지원 범위 기능 완료 | 공통 선형, 고정 격자 interval, 자유 시작 native, 작은 동일 문제 의미/최적값 검사 | 큰 팀 지도·전체 domain의 계산량/효과는 미검증 |
| P4 profiling·pilot | 작은 진단 환경 완료 | 전체 7정책 D0/D1, 준비/재시작/실패, 예산·기간·범위 비교, training 후보/절차 동결 | 팀의 최종 64/256-node 지도·측정창·최종 E2 설정 동결은 남음 |
| P5 본 실험·분석 | 미완료 | 진단용 입력 10개와 실행 기반 준비 | E1 튜닝/validation, 새 E2 지도·seed, E3 stress, paired 통계 |
| P6 환경별 결론 | 미완료 | 유리·동률·불리한 작은 사례 정리 | 검증된 선택 규칙·불확실성·자동 선택기·최종 재현 package |

P0~P4는 **지원한 추상 모델과 작은 headless 진단 범위의 구현/파일럿 진척**이다.
P2의 저장소 통합이나 P4의 최종 본 실험 준비까지 모두 끝났다는 의미로 사용하지 않는다.
단계 수만 세어 전체 진행률을 백분율로 표시하지 않는다.

- 실제 Git checkout에서 Python 3.11·3.14 각각 **430 passed**. 이 수치는 직전 기능 검수이며 이번 문서 수정으로 재실행한 수치가 아니다.
- 물리 실행 총 **591회**: A 기능 134 + A 파일럿/범위 비교 380 + B release 21 + B 수정 교차로 대조 56.
- 591회 모두 저장 결정 기반 replay·원시 로그 지표 대조 일치. **590 SUCCESS + 시간 여유 초과 1회**이며 실패를 성공으로 세지 않는다.
- 기존 D0 물리 사건 digest 36/36 보존. 기본 `optimized`·기본 D0 유지.
- 시나리오 생성기의 `kind`/`node_type` 오류를 수정하고 교차로 유무만 바꾼 56회를 별도 검수했다. 기존 A의 잘못된 교차로 대조는 효과 근거에서 제외한다.
- 공개 여유 0.1s 초과 1회는 보존했다. 같은 입력의 7정책 모두 0.2s로 다시 비교해 통과했고, 새 실험용 JSON은 0.2s다. 기본 설정을 변경한 것은 아니다.

근거: [P4 상세 검수](https://github.com/tortoiese/CLOVER/blob/feat/prd-p4-d1-startup/docs/P4_REVIEW.md), [판정 JSON](https://github.com/tortoiese/CLOVER/blob/feat/prd-p4-d1-startup/docs/D1_VALIDATION.json), [training 후보 protocol](https://github.com/tortoiese/CLOVER/blob/feat/prd-p4-d1-startup/docs/P4_TRAINING_PROTOCOL.json).
공통 D1은 `d1-staged-budget-v1`의 측정 구간+선반영 공개 여유다. 실제 총 비용과 정확히 같은 지연이라는 의미는 아니다.

### 20.2 무엇을 더 보완해야 하는가

아래는 아직 완료하지 않은 실행 계획이다. 특정 solver의 승리가 아니라 같은 조건의 효과·실패·비용 확인이 완료 조건이다.

| 우선순위 / ID | 남은 작업 | 구체적인 완료 조건 |
|---|---|---|
| 1 / R1 실제 시나리오 입력 | 팀 6개 시나리오의 지도·방향·거리·폭·설비·SOC·캐빈 층·외부 trace를 JSON으로 확정 | 4/8로봇·64/256노드 등의 실제 배치, 대응 대조군, generator/initial/trace hash를 고정. 실제 선택 경로·병목 통과 흐름·추가 자원 사용을 감사. 현재 10개 진단 입력으로 대체하지 않음 |
| 2 / R2 알고리즘·비용 튜닝 | H·격자·후보 작업 수·native 시간 단위/방문 상한·예산·cold/eager를 E1에서 비교 | 공통 선형/격자 parity와 native 범위 차이를 분리. 같은 seed/입력/제한에서 build/solve/검사/공개·stale/fallback·RSS와 실행 품질을 함께 기록. 휴리스틱 가중치는 고정 |
| 2 / R3 D1 규약 보정 | 큰 지도에서도 공개 여유와 반복 충전 판단의 비용을 점검 | 0.2s는 새 실험 시작값이며 보장이 아님. 초과는 무효로 보존하고 동일 block 전 정책을 공통 새 여유로 재실행. 고정 여유 합과 실제 측정 합을 별도로 보고해 solver 자체 성능과 혼동하지 않음 |
| 3 / R4 기간·관측 설계 | warm-up·측정창·유한 batch·과부하·drain을 구분 | 시작안 W=600s, 측정 T=7200s를 E1에서 확인한 뒤 환경별 공통 창 고정. 로봇별 여러 충전 순환·구간별 rate/WIP를 확인. 전체 충전량/배터리 비만으로 안정 상태를 주장하지 않음. drain 사용 시 도착 중단/상한을 명시하고 runner 지원도 검증 |
| 3 / R5 suite 통계 | paired 차이·신뢰구간·실패·미완료/검열을 분석하는 suite 기반 추가 | 독립 지도/seed block을 보존하고 실패 포함 분모·지연의 관측 완결률을 공개. 결정적 trace의 seed 이름 변경을 독립 반복으로 세지 않음. 단일 run analyze를 통계 분석 완료로 표시하지 않음 |
| 4 / R6 E2·E3 본 실험 | E1 tuning/validation 뒤 최종 코드·설정·지도·창·주 지표를 동결하고 새 환경으로 실행 | 현재 protocol의 E2는 disabled, 실제 E2 입력은 미정. seed뿐 아니라 지도/거리/방향도 분리. 모든 정책의 같은 실행창·계산 예산·원시 결과와 실패/부분 실행을 보존 |
| 5 / R7 환경별 선택 결론 | 선택 규칙·추천 보류 조건·추천 CLI를 구현 | held-out 개선·계산 비용·안전/납기 조건과 불확실성을 제시. 초안인 규칙 leaf당 다른 환경 3개·paired 30개 미충족이면 일반화 추천을 보류. 기본 정책 변경은 별도 결정 |
| 별도 / R8 통합·품질 정리 | PR #10의 필요한 GUI/API만 최신 계약으로 재연결하고 환경별 검수 | 아래 충돌 감사에 따라 통합 계획 확정. main 통행 계약과 최신 planning/decision 계약 보존. 통합 후 전체 회귀·GUI 정책 전환/저장/재생·CI 검증. 이번 P4의 새 D1 경로는 macOS 검수이며 새 경로의 Linux/Windows·대규모 지원은 미검증 |

전체 workspace lint의 검토 잔여 59건도 품질 정리 대상으로 남긴다.
실행 오류 규칙 E9/F63/F7/F82와 새 생성기 lint는 통과했지만 전체 lint가 깨끗하다고 기록하지 않는다.
먼저 시나리오 담당자는 R1을, 알고리즘 담당자는 R2~R5를 진행하고, 그 결과로 R6의 최종 실험 protocol을 확정한다.

### 20.3 현재 말할 수 있는 알고리즘 차이

공동 배정 작은 D0 배치에서 휴리스틱→MILP 완료 시각은 15.1→12.5s였다. 모두 2건 완료이므로 이를 처리량 증가율로 부르지 않는다.
반대로 긴 충전 D1 파일럿 1800s·seed 1001에서는 optimized 128/130, MILP 126/130, native 128/130이었다.
따라서 “MILP를 구현했으므로 더 좋다”는 결론은 내리지 않는다.
계획 품질, 해상도/방문 범위, 준비·반복 제어·fallback, 관측창을 분리해 조건별로 판단해야 한다.

### 20.4 병합 충돌: 8770b66/d63276d 당시 검사 기록

2026-11-13 원격 fetch 및 GitHub 조회 후 `git merge-tree --write-tree --name-only --no-messages`로 확인했다.
실제 checkout merge·rebase·main 변경은 하지 않았다. 명령의 가상 병합 tree만 검사했다.
현재 main은 `a28522d`, P4 문서 갱신 전 head는 `7aa12dc`, 기존 PR #10 head는 `d63276d`다.

| 비교 | 결과 | 의미 |
|---|---|---|
| 최신 main ↔ `feat/prd-p4-d1-startup` | 충돌 0파일 | main을 기준으로 만든 현재 작업 브랜치. 아직 main 병합/새 PR 생성은 하지 않음 |
| 최신 main ↔ `feat/rh-milp-cpsat` (PR #10) | 충돌 12파일 | GitHub에서도 open·mergeable=false·dirty. 기존 PR이 충돌 중 |
| 현재 P4 ↔ 기존 PR #10 브랜치 | 충돌 24파일 | 기존 planning 구현과 새 planning/decision 통합을 함께 바꿔 두 브랜치를 그대로 합치기 어려움 |

**main과 PR #10 사이의 실제 충돌 파일**은 다음과 같다. 표의 구간 수는 가상 병합 결과의 conflict marker 묶음 수다.

| 파일 (저장소 상대 경로) | 충돌 구간 수 |
|---|---:|
| `.github/workflows/tests.yml` | 1 |
| `robot_factory_sim/gui/factory_panel.py` | 1 |
| `robot_factory_sim/requirements.txt` | 1 |
| `robot_factory_sim/simulation/corridor.py` | 6 |
| `robot_factory_sim/simulation/engine.py` | 4 |
| `robot_factory_sim/simulation/factory_service.py` | 1 |
| `robot_factory_sim/simulation/path_graph.py` | 3 |
| `robot_factory_sim/simulation/resource_pool.py` | 1 |
| `robot_factory_sim/simulation/robot.py` | 10 |
| `robot_factory_sim/simulation/scheduler.py` | 3 |
| `robot_factory_sim/simulation/sim_worker.py` | 1 |
| `robot_factory_sim/simulation/state.py` | 1 |

통행 계층에서 main은 `traffic-mm-bundle-v1`의 mm/slot·원자적 확보·후미 해제를 쓰고,
기존 PR #10은 그 이전 자원 처리와 RH 실행 연결을 함께 수정했다.
특히 `resource_pool.py`와 `corridor.py`를 단순히 한쪽 파일로 선택하면 공통 계약을 잃을 수 있다.
GUI는 통행 대기 표시와 계획 상태 표시가 겹치고, CI는 통행 검사 목록과 계획/평가 검사 목록이 겹친다.
requirements에서는 OR-Tools/psutil 추가 위치가 충돌한다.

권장 통합 기준은 **현재 P4의 공통 실행/계획 계약을 기준으로 PR #10에서 필요한 GUI/API를 선별 재연결**하는 것이다.
이는 앞으로의 통합 계획이며 이번 문서 갱신에서 충돌 코드를 고치거나 PR을 병합한 것은 아니다.
정확한 세 비교의 SHA·전체 파일 목록은 [병합 검사 JSON](https://github.com/tortoiese/CLOVER/blob/feat/prd-p4-d1-startup/docs/P4_EVIDENCE/MERGE_STATUS_20261113.json)에 기록한다.
텍스트 충돌 0건은 기능 인수·통합 회귀 통과와 구분한다.


## 21. PR #10 충돌 해결·통합 검수 (2026-11-13)

§20.4의 12/24파일 충돌은 과거 두 head의 검사 기록이다. 기존 PR #10에 P4를 merge하고 필요한 GUI를 현재 모델에 이식했다.
최신 통합 기준은 [PR10_INTEGRATION.md](PR10_INTEGRATION.md)와 [통합 검증 JSON](PR10_INTEGRATION_VALIDATION.json)이다.
실제 main 반영 여부와 병합 commit은 [PR #10](https://github.com/tortoiese/CLOVER/pull/10)의 merged 상태를 따른다.
이 절은 병합 직전 인수 기록이며 merge를 수행했다고 미리 표시하지 않는다.

- P2의 저장소 통합 보완: main/P4의 mm·slot·에너지·작업 장부를 유지하고, 라이브 정책 선택·설정·비동기 계산·진행률 보존을 재연결했다.
- 구 planner/experiment/warm-start/자원 관측 구현은 현재 planning/decisions/evaluation로 대체했다. 과거 증거는 해당 source commit에 고정한다.
- 기본 optimized/D0, 기본 두 비교 정책, config.py와 DB 스키마를 유지한다. 라이브 규약은 headless D1과 구분한다.
- §20.2 R8의 GUI/API 충돌 정리는 이번 통합 대상으로 수행했다. 실제 팀 지도·대규모 튜닝·통계·E2/E3·선택 규칙(R1~R7)은 계속 남는다.
- 이번 CI의 OS별 결과는 현재 통합본 인수 근거이며, 이전 591회 결과나 다른 환경의 solver 우세를 새로 증명하지 않는다.

Windows Python3.11 첫 CI에서 발견한 미세 비용 0초/공개 초과 누락은 고해상도 perf_counter로 전체 시간 경계를 통일하고 회귀 2개를 추가하여 보완한다. 구현·실패 보존·최종 CI는 통합 검수 문서를 따른다.

## 22. 시나리오 최종 검수와 평가 매핑 (2026-11-13)

최신 상세 설계는 [시나리오 v3](SCENARIO_DESIGN_v3.md), 알고리즘→시나리오→결론 매핑은 [최종 평가 요약](SCENARIO_EVALUATION_FINAL_REVIEW.md)을 따른다. §20.2 R1의 과거 “팀 6개” 계획은 S00~S11의 12가족·기본46조건·복합16조건으로 구체화했다. 실제 지도·trace·절대 λ·deadline·판단 설정은 여전히 확정 전이며 62조건의 본 실험을 완료한 것은 아니다.

- 12가족의 핵심 운영 비교는 baseline/optimized/RH MILP/grid CP-SAT linear 4종이다. native는 우선 S01/S04/S08/S11에서 추가한다. legacy와 grid interval은 기능 및 기전 대조이며, 평가하지 않은 환경에 대한 정책 추천은 하지 않는다.
- E0 기능·기전, E1 training/validation, E2 held-out, E3 stress를 유지한다. 환경별 결론은 P6/R7이다. 이전 시나리오 v3의 “E3 선택 기준” 표현은 본 PRD와 맞춰 정정했다.
- 휴리스틱 가중치는 고정한다. MILP/CP-SAT의 순수 backend 비교는 같은 snapshot·후보·목적·예산에서 하며, closed-loop 운영 비교와 구별한다. 동일 snapshot 실험의 입력·분석 묶음은 별도 준비가 필요하다.
- v3의 E0 126회·E1 screening 735회는 구체화된 파일럿 예시다. §11.2의 2,700회는 초기 전체 예산 초안이며 두 수치를 합산하지 않는다. E1 설정 튜닝·여러 지도·E2/E3까지 포함한 최종 실행 수는 새 protocol에서 동결한다.
- §11.2의 leaf당3환경/paired30은 최소 지지집합 초안이다. v3의 지도당30seed 제안도 확정된 검정력이나 유의성 보장이 아니다. 파일럿 변동·실용 차이·지도 다양성을 반영해 실제 배분을 동결한다.
- 자원 존재/사용 가능성을 확인하는 기전 검사를 모든 정책의 양수 대기·동일 캐빈 사용을 요구하는 필터로 쓰지 않는다. S05는 설비 고정 용량 대조, S09는 복수 적합 설비를 가진 교란 대조로 구분한다.
- 기존 검수 receipt는 당시 commit의 증거로 보존한다. 최신 문서의 정합성 검수는 별도 최종 receipt에 기록하며, 새 시뮬레이션 성능 결과로 표시하지 않는다.

<a id="execution-inputs-20261113"></a>

## 23. 실행 입력·공통 seed 묶음 후속 구현 (2026-11-13)

§22의 과정을 확인하고 R1/R4의 첫 실행 작업을 시작했다. 사용법은 [입력 준비·paired 실행](../robot_factory_sim/PREPARED_EXPERIMENTS.md)을 따른다. 본 절의 합성 참조 입력은 팀의 실제 공장 지도나 보정 완료 수요를 대체하지 않는다.

| 항목 | 구현 범위 | 남은 완료 조건 |
|---|---|---|
| R1 입력 변환 | `prepare-design`: 기본46+복합16조건 → 실행용 Scenario JSON. E0 62개·E1 후보162개 생성·무결성 검증. 외부 참조 또는 명시적 합성 지도, 전체 설정·초기 상태·trace·해시 저장 | 실제 팀 지도/참조·부하·납기 확정, 실제 고정 trace import/설계 대응 |
| 공통 입력 실행 | `run-prepared`: 셀별 고정 수요 seed와 정책 묶음 사용. 결정적12셀의 seed 이름만 바꾼 반복 방지, 설정/입력 변조·소스 변경·미실행 예산 보존. 합성 E0 185회 실행·원시 지표·replay 일치 | E1/E2의 실제 지도 반복 배분, 최종 동결 protocol |
| R2/R3 설정 | 수학·D1 설정 파일을 실험마다 고정. E0 smoke용30초 horizon/0.25초 solver와 E1 후보40초/2초를 구분 | 성능/메모리 튜닝, 실제 장비의 공통 공개 여유 검증. 기본 휴리스틱 가중치 유지 |
| R4 시간창 | E0 180초/warmup0, S09 사건60/복구90. 충전 미노출을 보존하고 동일 trace prefix의900초 추가 대조 및 혼합SOC D1 검수. E1 후보 S09 1200초/warmup0·사건300/420, S10 warmup0 | 일반 stream600+7200초의 안정성·로봇별 충전 반복 확인, 구조 에너지/고장과 자립 운용 구분, 환경별 관측창 동결 |
| R5 분석 | 실행된 정책의 scenario/trace/initial 해시 비교와 전체 상태 보존. 185회 개별 replay/analyze·수요/에너지/자원 점유 감사 및 판정·CSV 저장 | paired 통계·신뢰구간·지도 clustering·최종 선택 규칙은 미완료 |

S09의 300~420초 사건을 일반 warmup600초 이전에 놓으면 교란을 관측하지 못한다. 이 문제를 회귀 검사하고 과도 응답 창을 분리했다. 취소용 probe 작업1개는 무교란을 포함한 S09 전 조건에 공통으로 넣으며 정책별로 대상을 바꾸지 않는다.

합성 참조에서 E0 입력은62개, E1 3seed 후보는162개(결정적12+무작위50×3)다. E1 후보는 생성만 했으며 아직 성능 실험하지 않았다. 최초 E0의12가족×7정책 D0 84회·6가족×7정책 D1 42회, 나머지50조건 baseline D0 50회에 충전 추가 관측2+7회를 더해185회다. 모두 SUCCESS·원시 지표 재구성·저장 결정 replay가 일치했다. D1 49회는 제어 coverage와 소유 자원 해제도 확인했다. 이를185개의 독립 반복이나 전62조건×7정책 본 실험으로 세지 않는다.

통로1/2/3대 점유, 실제 교차로 slot, 두 캐빈의 실제 loaded 이동과 교란 적용을 감사했다. 짧은 S04는 충전0이었으므로 원래 결과를 남기고 같은 seed·λ의900초 관측을 추가했다. 슬롯1/4에서 충전 대기541.6/0 robot·s였지만 완료 수는 모두21/27이고 구조 에너지24unit·fault8/9회가 있었다. 병목 노출 확인이며 지속 가능한 운용/처리량 우수성 근거가 아니다. λ·충전 반복·기간은 E1 보정 대상으로 남는다.

실행 기준 소스는 `4f6dc5e748735318cdb4fa068b86f4e8db9b894c`다. [E0 검수 보고서](SCENARIO_EXECUTION_E0_REVIEW.md)와 [185회 판정](scenarios/e0_execution_20261113/receipt.json)에 정확한 소스/원시 파일 해시·프로토콜·제한을 보존한다. Linux CI에서 발견한 IPC 종료 경합은 `ddc94f4`에서 보완했다. 수정 후 Python3.11/3.14 각각487검사와 통로·충전·다층3조건×7정책×D0/D1의 별도42회 실행·원시 지표·replay를 통과했다. [수정 후 판정](scenarios/e0_execution_20261113/ipc_canary_receipt.json)은 별도 소스 기준이며 과거185회 결과의 소스 해시를 바꾸지 않았다. E0 결과로 일반적 알고리즘 우열·통계적 유의성·E2 완료를 주장하지 않는다.

### 23.1 최신 main 통합·최종 기능 검수

원격 PR #11·#12 병합 후 main `69bb83c`를 충돌 없이 기능 브랜치에 통합했고 PR #13의 base를 main으로 바꿨다. PR #13의 main 병합은 수행하지 않았다. 통합 코드 `aec9b53`는 Python3.11/3.14 전체 각각499검사, GitHub Ubuntu/macOS/Windows×두 버전의6개 조합 각각499검사를 통과했다. 최초 localhost 차단으로 실패한3검사도 보존하고 로컬 HTTP 모의 서버를 허용한 환경에서 전체 검사를 통과했다.

통합 소스로 다시 준비한3조건×7정책×D0/D1의 추가42회도 모두 SUCCESS·원시 지표 재구성·replay·수요/에너지/자원 불변식·D1 coverage/해제를 통과했다. [통합42회 판정](scenarios/e0_execution_20261113/main_canary_receipt.json)과 [통합 CI](scenarios/e0_execution_20261113/ci_main_integration.json)를 분리해 보존했다. 총185+42+42=269회는 소스별 기능 검수 횟수이며 독립 반복 수가 아니다. R1의 실제 팀 입력 확정과 R2~R7의 보정·튜닝·통계·held-out 결론은 여전히 남는다.

### 23.2 추가 main 3dfb4ba 통합·GUI 순회 분리

작업 종료 확인 중 추가 병합된 PR #14의 main `3dfb4ba`도 충돌 없이 통합했다. GUI의 `roam=True`는 유휴 순회·초기 층 분산을 추가하므로, 고정 입력 실험 runner에는 `roam=False`를 명시했다. 무수요 다층 baseline/optimized의 이동·소비 에너지·캐빈 이동0과 replay를 검증하는2개 회귀를 추가했다. GUI의 순회 실행 및 ROI 환산 가정은 공식 headless 실험의 입력/주 지표와 구분한다.

코드 `77cbac9`에서 Python3.11 전체502검사·시나리오 모듈33검사와 다층1조건×7정책×D0/D1의 추가14회 실행·원시 지표·replay·불변식 검수를 통과했다. [순회 경계14회 판정](scenarios/e0_execution_20261113/roam_canary_receipt.json)과 [로컬 검사](scenarios/e0_execution_20261113/local_pr14_integration.json)를 보존하며 교차 플랫폼 최신 판정은 PR #13 checks에서 확인한다. 총283회는 소스별 기능 검수다. E1 후보162개를 성능 실험한 것으로 세거나 R1 실제 지도·λ 확정 및 R2~R7을 완료 처리하지 않는다.

이후 Windows3.11 CI의501통과·GUI1실패도 보존했다. 고정250ms 대기 때문에 첫 layout 도착 전에 층 선택을 하던 검사에 worker/초기 상태/애니메이션 완료의 조건 대기를 적용했다. 화면의 층·크기·배율 assertion과 제한 시간은 유지하며 제품/물리 런타임은 변경하지 않는다. [E0 보고서의 CI 동기화 기록](SCENARIO_EXECUTION_E0_REVIEW.md#windows-gui-검사-동기화)과 최종 PR checks를 함께 확인한다.


## 24. R5 paired 분석 기반 후속 구현

개별 run의 재생·원시 지표 감사 이후, [R5 상세 계획](PRD_PAIRED_ANALYSIS_R5.md)에 따라 실행 묶음의 탐색적 paired 분석 v1을 구현·기초 검수했다. PR #13의 실험 입력 구현은 그대로 보존하고 별도 후속 브랜치에서 진행했다. 실제 팀 입력 확정 전의 분석 기반 검수이며 본 실험 완료를 뜻하지 않는다.

manifest/result/raw 무결성과 정책별 같은 입력·시간/설정 확인, 실패·미실행/미완료 분모 보존, 명시적 지도·수요 seed 단위, 평균 차이·pointwise t 구간·고정 paired bootstrap·Holm p값, 반복 부족/중복/검열/0분모의 추론 보류, JSON/CSV/해석 노트를 구현했다. 유한 batch는 전체 완료 시에만 makespan을 계산하며, 납기 대상 UID가 정책마다 다르면 비율 비교를 보류한다. t 구간의 가정이나 독립성은 최소 반복 수만으로 보장되지 않는다.

구현 기준 `691458e`에서 Python3.11 전체553검사, Python3.14 신규51검사를 통과했다. 기존283건의 원본 해시를 대조했고9개 소스/모드 묶음의231건을 paired 분석, baseline만 있는52건을 별도 자원 대조로 유지했다.1584개 효과 행은 지표별 기술 통계이며 독립 실험 수가 아니다. 새로운 성능 시뮬레이션은0회이고 E0 구간·p값·추천도0개다. [R5 검수 보고서](R5_ANALYSIS_REVIEW_20261114.md), [재분석 판정](scenarios/r5_analysis_20261114/receipt.json), [효과 CSV](scenarios/r5_analysis_20261114/effect_summary.csv)에 정확한 범위·원래 소스·해시를 보존했다.

실제 지도/λ·기간/충전 순환 보정, 교차 군집·검열 생존 분석, 사전 등록 E2/E3와 최종 추천 규칙은 여전히 후속 작업이다.

## 25. R2/R3 E1 설정 파일럿 착수 (2026-11-14)

[설정 파일럿 PRD](PRD_E1_SETTINGS_PILOT_20261114.md)에 따라 합성4조건×5정책×3설정의60회 실행 가능성 검사를 사전 동결한다. 실제 팀 지도·수요 입력이 없으므로 E1 본 실험이나 R1 완료로 표시하지 않는다. horizon/solver timeout을 한 축씩 바꾸고 공통 D1 예산·고정 입력을 유지한다. 실행 순서·최대 예산·실패 보존·publication 초과 시의 조건 전체 추가 검수 규칙을 JSON으로 고정한다. 이 절은 실행 전 착수 기록이며 결과·검수는 후속 기록으로 추가한다.

### 25.1 실행·감사 완료 경계

입력 동결 `e827424`에서60회 모두 SUCCESS, 원시 지표·저장 결정 replay·에너지/작업/자원·D1 coverage/해제를 통과했다. [설정 파일럿 검수 보고서](E1_SETTINGS_PILOT_REVIEW_20261114.md)와 [60회 판정](scenarios/e1_settings_pilot_20261114/receipt.json)에 입력·소스·원본 해시를 보존했다. publication 최대0.11871초로 공통0.2초를 충족했고 추가 재실행은0회다. 세 설정별 분석의384비교·768효과 행은 독립 반복 수가 아니며 CI/p값/최종 추천은0개다.

기준 h40_t2에서 S01 전체 batch는 optimized57.84초/MILP57.33초/linear27.24초, S08은106.93/105.30/85.04초였다. 반면 S04는 optimized의 충전 대기가 RH보다 짧았고, S11은 optimized16/16·RH15/16 완료였다. horizon 확대·계산 제한 증가가 항상 개선되지는 않았다. 단일 seed·미보정 합성 입력·계산 지연/fallback 포함 결과를 일반 우열로 승격하지 않는다.

R2/R3는 설정 스크린 한 묶음까지 진행했으며 완료가 아니다. 같은 snapshot의 backend 대조, 실제 장비에서2초 운영 예산의 단계별 배분, native build/timeout·충전 stale/auxiliary 기전, 복수 지도·seed의 training/validation이 남는다. R1 실제 입력·R4 기간/충전 반복 보정·R6/R7 held-out/stress/최종 추천도 미완료다. 기본 optimized·가중치·MILP/CP-SAT 모델·기본 D0는 유지한다.

## 26. R2 공통 snapshot backend 비교 기반

[R2 상세 계획](PRD_FROZEN_SNAPSHOT_R2.md)에 따라 같은 관측·후보·경로·정수 제약·목적·시작해의 MILP/CP-SAT linear 비교를 추가한다. native/interval 및 closed-loop 운영 비교와 구분한다. [사용법](../robot_factory_sim/FROZEN_SNAPSHOT_BENCHMARK.md)은 공통 준비/기동/IPC/독립 검사 비용과 backend의 compile/search 예산을 구분하고, 늦게 반환된 해·실패·미실행을 보존한다. 이전60회 원본을 변경하지 않으며 동결 소스에서 별도48arm 기능·비용 검수를 수행할 계획이다. 이 절은 착수 기록이며 결과·완료 경계는 검수 후 추가한다.
