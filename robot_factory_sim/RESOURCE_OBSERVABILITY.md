# 자원 대기 검열·캐빈 실제 이동·원시 로그 재분석

최신 실패 계측·동일 문제 구성 개선·profile CLI는 `NATIVE_PROFILE_AND_BUDGETS.md`를 참고한다.

최신 자유 시작시각 CP-SAT의 해상도·전체/제한 domain은 `NATIVE_SCHEDULING.md`를 참고한다.

최신 진행 중 운행·완료 구간의 달력은 `INFLIGHT_CALENDAR.md`를 참고한다.

2026-11-11 도입 · 현재 `common-model-v1.9-native-profile`에서도 같은 관측 정의를 사용한다.
최신 자원 달력·캐빈 계획 변경은 `CALENDAR_AND_CABIN_FLOW.md`를 참고한다.
기본 `optimized`, 기존 가중치·GUI·물리 자원 확보/해제 규칙은 유지한다.

## 이번 변경의 목적

줄을 빠져나온 사람의 대기시간만 평균 내면 아직 오래 기다리는 사람을 놓칠 수 있다.
기존 queue_samples는 대기가 끝난 뒤 남기는 표본이 중심이다.
이를 보존하면서, 대기 시작·확보·취소/실패를 원시 로그에 추가하고 종료 시 남은 대기를 별도로 집계한다.
아직 끝나지 않은 대기 나이는 실제 최종 대기시간의 하한이며, 완료 대기시간처럼 평균에 넣지 않는다.

`simulation/telemetry.py`는 원래 자원 Event를 그대로 한 번 yield한다.
새 SimPy timeout/process나 추가 물리 지연을 만들지 않는다.
대상은 통로·교차로의 원자적 bundle, 작업 설비, 충전 슬롯, 캐빈 슬롯이다.
계획된 선행 대기나 작업 배정 전의 backlog는 자원 큐와 구분하며 기존 작업 지연·미완료 지표로 본다.

## resource_waits

cohort는 측정 구간 `[warmup, observed_end)` 안에 대기를 **시작한** episode다.

- `cohort_episodes = cohort_granted + cohort_withdrawn + cohort_right_censored`.
- `granted_wait`: 실제 자원을 확보한 cohort의 조건부 분포. 대기 0도 포함한다.
- `withdrawn_elapsed`: 취소·퇴역·실패 등으로 줄을 떠난 cohort의 관측 경과시간. 확보 표본과 섞지 않는다.
- `right_censored_ages`: 아직 줄에 있는 cohort의 현재 나이. 최종 대기시간의 추정 분포가 아니다.
- `carry_in_episodes`: warmup 이전부터 시작돼 측정 구간까지 이어진 대기.
- `queue_robot_seconds`: 모든 대기와 측정 구간이 겹친 시간의 합. carry-in과 진행 중 대기도 포함한다.
- `mean_queue_length`: 위 노출량을 실제 측정 기간으로 나눈 시간 평균 대기 로봇 수.
- `open_episodes`: 종료 시 남은 요청별 로봇 instance·자원·시작·전체 나이·구간 내 나이·bundle 수요.

`by_channel`은 자원 종류, `by_queue`는 종류와 기록된 queue/초기 blocker의 조합이다.
통로 bundle은 자원을 여러 개 요구해도 요청 하나로 한 번만 센다.
특정 자원이 전체 대기의 유일한 원인이라는 인과적 attribution이나 점유율 통계로 해석하지 않는다.
로봇 ID가 재사용돼도 `robot_instance`로 구분한다.

120초 초과의 `active_over_threshold`는 오래 기다린 관측값이다. 순환 교착의 증명이 아니다.
Kaplan–Meier, 전체 대기의 무검열 분위수 추정, 교착 검출기는 이번 구현에 포함하지 않는다.
측정 시작 전에 run이 실패하면 노출량 0, 평균 null의 빈 구간으로 기록한다.
실패 도중 이미 처리된 경계 시각의 이벤트는 관측한 상태로 남기며, 처리하지 않은 미래 이벤트는 읽지 않는다.

## cabin_phases

같은 캐빈 instance의 단계별 시작·완료·중단을 기록한다.

| kind | 의미 |
|---|---|
| empty_move | arrive가 호출한 승차층까지의 빈 회송 |
| boarding | 승차 측 문 개폐 절반 |
| loaded_move | carry가 호출한 탑승 목적층까지 이동 |
| alighting | 하차 측 문 개폐 절반 |
| other_move | 직접 호출된 별도 이동. 승차 요청 회송으로 임의 분류하지 않음 |

시간·실제로 이동한 층 간격을 측정 구간에 맞춰 자른다.
중단된 이동은 도달한 소수 층까지의 거리만 더한다.
종료 시 이동 중이면 기존 floor_position과 같은 선형 보간으로 부분 이동량을 포함하고 active로 남긴다.
활성/완료/중단 단계 수와 완료 ride 수는 다른 지표다. 중단 후 재시도로 단계 수가 더 많아질 수 있다.
캐빈 ID가 재생성되면 새 instance를 사용한다. 같은 instance의 겹친 phase 로그는 분석에서 거부한다.

거리 단위는 **floor intervals**다. 층고 자료가 없으므로 m·전력·에너지로 환산하지 않는다.
`empty_distance_fraction`의 분모는 empty_move+loaded_move의 관측 이동량이다.
other_move와 문 시간은 분모에 넣지 않으며 이동량이 0이면 null이다.
로봇의 캐빈 슬롯 대기는 resource_waits에, 슬롯 확보 후 빈 회송·문·운송은 cabin_phases에 있다.

## 원시 로그 재분석

결과 schema는 `clover-headless-run-v4`다. 최신 관측은 v6·계획/모델은 v8다. 자원 관측 지표의 정의는 유지한다.
이전 결과는 이전 소스에서 replay한다. v4의 원시 관측 분석에는 native solver가 필요하지 않다.

```bash
python -m simulation.evaluation analyze \
  --run /tmp/clover-run/charging__optimized__1001.json \
  --output /tmp/clover-analysis/charging.json
```

기본 이벤트 경로는 같은 stem의 `.events.jsonl`이다. `--events`로 명시할 수도 있다.
분석은 sequence·시간 순서·교통 승인 용량·원시 event digest·물리 projection digest를 확인한다.
그 뒤 대기·캐빈 통계를 다시 만들어 저장된 값과 비교한다.
결과와 원시 파일 SHA256도 기록한다. 로그 절단/손상은 거부하며, 저장 지표 불일치는 METRICS_MISMATCH로 반환한다.
실패한 run도 관측한 구간까지 분석한다. 이 명령은 정책 추천이나 본격적인 통계적 우열 분석을 수행하지 않는다.

## 물리 동작 보존 검사

`event_digest`는 새 계측 로그까지 포함해 replay한다.
`physical_event_digest`는 새 observation 이벤트 네 종류만 제외하고 sequence를 다시 매긴다.
기존 사건의 payload와 순서는 그대로 검사한다. baseline/optimized의 이전 물리 digest와 대조할 수 있다.
새 관측 로그의 내용은 전체 event digest와 재분석 검사가 담당한다.
engine.close의 정리 전에 최종 통계를 저장하므로, 종료 후 generator 정리로 검열 표본을 완료로 바꾸지 않는다.

다음 작업은 더 정밀한 자원 달력·여러 운행의 캐빈 공동 일정·완전한 D1·본 시나리오 확대 실험이다.
