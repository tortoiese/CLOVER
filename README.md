# 🤖 Robot Factory Simulator

> **SimPy 기반 멀티로봇 공장 시뮬레이터 + AI 대화형 관제 시스템**  
> IC-PBL 캡스톤 디자인 2 — 클로봇(Clobot) 제안 주제

---

## 📌 프로젝트 개요

제조 현장의 멀티로봇 작업 배정 및 충전 스케줄링을 최적화하는 시뮬레이션 플랫폼입니다.  
납품 업체가 자사 공장 환경(설비 배치, 로봇 수, 충전기 등)을 설정하면 DB에 저장되고, 시뮬레이터에 **실시간 반영**됩니다.  
AI 관제 콘솔을 통해 자연어로 시뮬레이션을 제어하고, 긴급 생산 상황에 자율적으로 대응합니다.

### 핵심 가치
| | Baseline (기존) | 본 솔루션 (최적화) |
|---|---|---|
| 작업 배정 | 라운드로빈(순서대로) | 배터리·거리·큐·납기 가중합 최적 배정 |
| 충전 스케줄링 | 배터리 소진 시 충전 | 예측 기반 선제 충전 + 긴급 시 최소충전 복귀 |
| 설비 변경 대응 | 시뮬레이션 재시작 필요 | DB 핫리로드, 무중단 반영 |
| 긴급 주문 대응 | 수동 재배치 | AI 자율 판단 + 자동 로봇 재배치 |

---

## 🖥️ 화면 구성

```
┌─────────────────────────────────────────────────────────────────┐
│ [▶ 시작] [⏸] [⏹] [⏩ 배속] [리셋]  │ ⚙ 공장 설정 │ DB: ● 연결됨 │
├───────────────────┬──────────────────┬──────────────────────────┤
│                   │                  │   KPI 카드 (8개 지표)    │
│  공장 시각화       │  AI 관제 콘솔     ├──────────────────────────┤
│  (Factory Floor)  │  (Chat Console)  │   실시간 그래프 (7탭)     │
│                   │                  │                          │
├───────────────────┴──────────────────┴──────────────────────────┤
│  🔴 긴급 생산 알림 배너 (이벤트 발생 시에만 표시)                  │
└─────────────────────────────────────────────────────────────────┘
```

### 좌측 — 공장 시뮬레이터 시각화
- 2D 공장 바닥 뷰 (QGraphicsScene)
- 작업 스테이션, 충전소, 로봇, 벽/문/장애물을 DB 기반으로 동적 렌더링
- 로봇 상태별 색상: 이동(파랑), 작업(초록), 충전(노랑), 대기(회색), 고장(빨강), 긴급배정(주황 깜빡임)
- 로봇 배터리 잔량 바 + 이동 경로 점선 애니메이션

### 중앙 — AI 대화형 관제 콘솔
- 자연어 명령으로 시뮬레이션 제어 (로봇 추가/충전/배속 변경 등)
- 긴급 주문 투입, 설비 고장 처리, 병목 분석 등
- AI가 긴급 상황 자동 감지 → 자율 대응 → 결과 보고

### 우측 — 성능 대시보드
- 8개 KPI 카드 (Baseline 대비 증감률 표시)
- 7개 탭 차트: Throughput 비교, 로봇 상태 타임라인, 대기열 추이, 배터리 추이, 레이더 비교, 주문 진행률, 이벤트 타임라인

---

## 🗂️ 디렉토리 구조

```
robot_factory_sim/
├── main.py                        # 앱 진입점
├── README.md                      # 이 파일
├── agent.md                       # AI 에이전트(Claude 등) 작업 규칙
├── requirements.txt               # Python 의존성
├── config.py                      # 시뮬레이션 기본 파라미터 + DB 경로
├── database/
│   ├── schema.sql                 # DB 스키마 정의
│   ├── db_manager.py              # SQLite CRUD + changelog 기록
│   ├── config_watcher.py          # ConfigWatcher (DB 변경 폴링)
│   ├── order_manager.py           # 생산 주문 CRUD + EventWatcher
│   ├── seed_data.py               # 기본 공장 구성 시드 데이터
│   └── api_stub.py                # REST API 클라이언트 스텁
├── simulation/
│   ├── engine.py                  # SimPy 시뮬레이션 엔진 (GUI 없이 단독 실행 가능)
│   ├── sim_worker.py              # SimWorker(QThread) — Baseline·최적화 엔진 병렬 실행
│   ├── factory_service.py         # GUI용 공장 구성/주문 서비스 (gui → database 직접 참조 방지)
│   ├── state.py                   # SimState 스냅샷 + 에이전트 컨텍스트 변환
│   ├── robot.py                   # Robot 프로세스
│   ├── station.py                 # WorkStation, ChargingStation
│   ├── scheduler.py               # BaselineScheduler, OptimizedScheduler
│   ├── task_generator.py          # 작업 생성기 (주문 기반 하이브리드)
│   └── path_graph.py              # 경로 그래프 + Dijkstra
├── agent/
│   ├── base.py                    # BaseAgent ABC
│   ├── mock_agent.py              # MockAgent (규칙 기반)
│   └── models.py                  # 데이터 모델
├── gui/
│   ├── main_window.py             # 메인 윈도우
│   ├── factory_view.py            # 공장 시각화
│   ├── console_panel.py           # 관제 콘솔
│   ├── dashboard_panel.py         # KPI + 차트 대시보드
│   ├── config_editor.py           # 공장 설정 에디터
│   ├── order_dialog.py            # 주문 관리 다이얼로그
│   ├── alert_banner.py            # 긴급 알림 배너
│   ├── agent_bridge.py            # AgentBridge(QThread) — 에이전트 실행 스레드
│   ├── constants.py               # 색상/크기/문구 상수
│   ├── widgets/
│   │   ├── kpi_card.py
│   │   ├── chat_bubble.py
│   │   ├── robot_item.py
│   │   ├── station_item.py
│   │   └── obstacle_item.py
│   └── styles/
│       └── theme.qss              # 다크 테마
└── utils/
    ├── metrics.py                 # KPI 계산
    ├── event_types.py             # 이벤트/동작 타입 상수
    ├── config_models.py           # 공장 구성·주문 dataclass
    └── layout_tools.py            # 격자 경로망 생성 등 레이아웃 보조
```

### 스레드 구성
| 스레드 | 역할 |
|---|---|
| 메인 (Qt GUI) | 화면 렌더링, 사용자 입력 |
| SimWorker | SimPy 실행 — Baseline·최적화 엔진을 같은 시드로 나란히 돌려 KPI 동시 비교 |
| ConfigWatcher | `config_changelog` 1초 폴링 → 설정 핫리로드 |
| ProductionEventWatcher | `production_events` 1초 폴링 → AI 자율 대응 |
| AgentBridge | 관제 에이전트 실행 (LLM 에이전트로 교체해도 GUI가 멈추지 않음) |

스레드 간 통신은 모두 Qt Signal/Slot으로 한다. 툴바의 스케줄러 선택은 **화면 표시·AI 조치 대상 엔진**을 바꾸며, 두 엔진은 항상 함께 실행된다.

---

## ⚙️ 설치 & 실행

### 요구사항
- Python 3.11+
- 운영체제: Windows / macOS / Linux

### 설치
```bash
git clone <repo-url>
cd CLOVER/robot_factory_sim
pip install -r requirements.txt
```
> Windows에서 PySide6 설치가 긴 경로 문제(`OSError: [Errno 2] ...`)로 실패하면 Windows 긴 경로 지원을 켜거나, 경로가 짧은 가상환경(예: `C:\venv`)에 설치하세요.

### 실행
```bash
python main.py
```
- 최초 실행 시 `factory_config.db`가 자동 생성되고 기본 공장 구성(스테이션 5개, 충전소 2개, 로봇 5대)이 삽입됩니다.
- GUI 창이 열리면 **[▶ 시작]** 버튼으로 시뮬레이션과 DB 감시(ConfigWatcher / ProductionEventWatcher)를 시작하세요.
- 다른 DB 파일을 쓰려면 환경변수 `ROBOT_FACTORY_DB`에 경로를 지정합니다.

### GUI 없이 테스트
```bash
python -m simulation.engine          # Baseline vs 최적화 KPI 비교 (headless)
python -m agent.mock_agent           # 자연어 명령 해석 확인
python -m simulation.factory_service # :memory: DB로 서비스 동작 확인
```

---

## 🏭 업체 환경 설정 방법

### 방법 1: GUI 설정 에디터
1. 툴바의 **[⚙ 공장 설정]** 클릭
2. 팔레트에서 설비를 드래그 & 드롭으로 배치
3. 클릭하여 속성(좌표, 처리시간, 충전 슬롯 수 등) 편집
4. **[🧭 경로 편집]** 모드: 빈 곳 클릭 = 노드 추가, 노드 두 개 연속 클릭 = 엣지 연결, 우클릭 = 삭제 (벽에 막힌 엣지는 빨간 점선)
5. **[🤖 로봇 관리]** 탭에서 로봇 추가/삭제·파라미터 설정, 상단에서 업체별 프리셋 저장/불러오기
6. **[저장]** → 시뮬레이터에 즉시 반영 (무중단 핫리로드)

### 방법 2: DB 직접 수정
외부 시스템(MES/WMS)에서 `factory_config.db`의 테이블을 INSERT/UPDATE하고 **`config_changelog`에 변경 이력 행을 함께 남기면**, ConfigWatcher가 1초 내 감지하여 시뮬레이터에 반영합니다. (`DBManager` / `FactoryService`를 통해 쓰면 이력은 자동으로 기록됩니다.)

### 방법 3: AI 관제 콘솔
```
> 충전소 하나 더 추가해 위치 (400, 300)
> WS-03 위치를 (200, 150)으로 옮겨줘
> 로봇 2대 더 투입해
```

---

## 🚨 긴급 생산 대응

### 긴급 주문 투입
- **관제 콘솔**: `"긴급 주문 넣어줘: 제품A 300개, 2시간 내"`
- **주문 관리 다이얼로그**: [📋 주문 관리] 버튼 → 주문 추가
- **DB 직접**: `production_orders` 테이블에 `priority='critical'` INSERT + `config_changelog` 기록 (긴급 이벤트는 시뮬레이터가 감지해 `production_events`에 자동 기록)

### AI 자율 대응 시나리오
| 상황 | AI 자동 조치 |
|---|---|
| 🔴 긴급 주문 | 유휴 로봇 즉시 재배치, 저우선순위 작업 선점 |
| 🟠 납기 임박 | 로봇 추가 투입, 스테이션 우선순위 조정 |
| 🟡 생산량 급증 | 유휴 로봇 전수 가동, 최소충전 복귀 |
| ⚫ 설비 고장 | 대체 스테이션 재배정, 납기 재계산 |

---

## 📊 성능 지표 (KPI)

| KPI | 설명 |
|---|---|
| Throughput | 단위 시간당 완료 작업 수 |
| Avg Wait Time | 작업 큐 평균 대기시간 |
| Utilization | 로봇 가동률 |
| Charge Wait | 충전 대기 평균시간 |
| Idle Rate | 로봇 유휴율 |
| Battery Efficiency | 충전 1회당 작업 완료 수 |
| Order Fulfillment | 납기 내 완료 주문 비율 |
| Response Time | 긴급 이벤트 대응 시간 |

---

## 🔧 기술 스택

| 카테고리 | 기술 |
|---|---|
| 언어 | Python 3.11+ |
| GUI | PySide6 (Qt6) |
| 시뮬레이션 | SimPy |
| 차트 | pyqtgraph |
| DB | SQLite3 (내장) |
| 수치 계산 | numpy |

---

## 👥 팀

| 역할 | 이름 |
|---|---|
| 팀장 / 프론트엔드 | 이승환 |
| 팀원 | 신채은 |
| 팀원 | 최진웅 |
| 팀원 | 강재민 |

**지도교수**: 유철우 교수님  
**제안 기업**: 클로봇(Clobot)

---

## 📄 라이선스

이 프로젝트는 명지대학교 캡스톤 디자인 2 과제로 개발되었습니다.
