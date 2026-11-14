# 본 실험 준비 완료 계획

2026-11-14. 합성 연구환경으로 진행한다. 기존 기본 정책/가중치/수학 목적은 유지한다. 준비 완료는 **실행 가능한 동결 입력·분석·실행 예산·검수 PASS**가 있다는 뜻이며 본 실험 결과가 나왔다는 뜻이 아니다.

## 설계 결정

- 주 대조: optimized 대비 baseline, RH MILP, grid CP-SAT linear. native는 S01/S04/S08/S11에서 추가 대조한다. 표현 차이를 순수 backend 우열로 해석하지 않는다.
- 핵심6가족: S00 저부하1수준, S02 폭1.3/2.2/3.1m, S04 충전슬롯1/2/4, S06 smooth/burst2/burst4, S08 캐빈1/2, S10 지속부하1.1/1.3배. 총14조건. 같은 조건의 정책 입력과 물리처리 단위는 동일하며 폭/슬롯/캐빈 수준 사이에도 λ·trace·SOC를 고정한다.
- 보조6가족 S01/S03/S05/S07/S09/S11은 기전/확장 진단이다. 핵심과 보조의 추론 범위를 사전 구분한다.
- heldout: 핵심 조건별 독립 생성 지도3개×수요10seed. 보조는3지도×3seed의 기술통계. 지도는 기존 예제의 이름/ID/균일배율 변경이 아니라 독립 RNG로 x/y 격자 간격을 변형한다. 추론 모집단은 이 명시적 합성 지도 생성분포에 한정한다.
- training/validation의 지도·seed·이전 파일럿 지도와 heldout은 분리한다. 수요 seed는 지도별로 겹치지 않게 배정한다. 같은 지도 안의 정책/단일축 처리는 같은 seed/잡음/도착을 사용한다.
- 유한 관측: 기본 warmup300초 + 측정1800초. S09는 transient2100초/warmup0. 추가 긴 충전 창은 warmup600초+측정7200초. 이 창을 정상상태라고 주장하지 않는다. finite batch는 작업수·makespan을 보며 λ/ρ를 지속부하로 해석하지 않는다.

## 용량·부하 확정

단일 임시 μ=0.2를 제거한다. training 지도에서 고정 D0 optimized 참조 정책을 높은 수요로 포화시키고, 마지막 고정 구간들의 완료율을 반복 관측해 **유한 창의 참조 서비스율 μ_ref**를 얻는다. 물리 최대 용량/모든 정책의 용량으로 부르지 않는다. 원시 장부·fault/rescue·backlog·표본 seed와 추정 분산을 보존한다.

새 지도에는 입력만으로 계산한 자유주행+처리 시간 비율로 μ_ref를 예측한다. validation의 다른 지도/seed에서 예측을 검수한다. heldout 알고리즘 결과를 보며 λ를 맞추지 않는다. 낮음/근접/과부하는 이 참조 지표에 대한 사전 명칭이며 실제 대기·점유·층간 이동·WIP는 결과로 따로 확인한다.

S02는 작업 설비를 통로 양쪽에 배치하고 통로를 우회하는 연결을 제거한다. S08은 실제 캐빈 이용을 기록한다. 도착 목적지 비중으로 실제 자원 노출을 가정하지 않는다.

## 설정·분석·실행

- H40/step2/후보8/solver2초/native최대4행동을 a-priori 기준으로 고정한다. D1 v2 공통2초, admission0.1초·publication0.2초. 기존 파일럿이 최적 설정을 증명했다는 주장을 하지 않는다.
- 주 분석은 처리량의 후보−optimized 차이. 실용 차이는0.1 tasks/min. 납기·에너지·자원대기·미완료·계산/기한·fallback은 사전 정의한 보조/guardrail이다. 명백한 안전/에너지/입력 위반이나 불완전 비교는 우세 판정을 보류한다.
- 신뢰구간·다중비교·실패/검열/0분모·지도 clustering 규칙을 동결한다.10seed/3지도가 유의성/검정력을 보장한다는 표현은 하지 않는다. 정해진 표본에서 불확실하면 무결론을 허용한다. 결과를 보고 유리한 seed만 추가/삭제하지 않는다.
- source/Git blob·환경·전체 trace/초기상태/설정/분석을 동결한다. 큰 입력은 로컬 실행 bundle과 SHA/index로 관리한다. 재개는 사전 지정 shard를 새 폴더에 실행하고, 중복/누락을 검사한 뒤 병합한다. 실패는 보존하고 자동으로 유리한 재실행을 고르지 않는다.
- 본 실험 결과는 이번 작업에서 열지 않는다. 별도 개발/validation canary로 worker·장부·메모리·wall/storage 예산을 실측하고 전 실행량/중단·재개 명령을 만든다.

## 준비 완료 통과 기준

1. 용량 참조 근거와 validation 검수, 개발 입력별 관측창 보정 PASS.
2. geometry/seed/동일입력 중복·처리축·원시자료·source·설정·분석 잠금 검사 PASS.
3. shard/합치기/미실행 분모/중복·변조 거부와 실제 CLI PASS.
4. D1 canary·충전 반복·작업/에너지/자원/worker 해제 PASS, 실행 예산과 저장소 여유 확인.
5. confirmatory freeze 성공, main matrix/실행명령/분석명령 및 최종 readiness JSON `READY` 생성. 미완료 항목을 주석으로 숨기지 않는다.

설계 근거: [NIST 실험설계](https://www.itl.nist.gov/div898/handbook/pri/section3/pri3.htm), [blocking](https://www.itl.nist.gov/div898/handbook/pri/section3/pri332.htm), [표본수와 정밀도](https://www.itl.nist.gov/div898/handbook/prc/section2/prc222.htm). 계획 단계의 원칙을 참고하며 프로젝트의 실용 차이·분포·시간/자원 값은 연구 설계 선택이다.
