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
│   ├── engine.py                  # SimPy 시뮬레이션 엔진
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
    └── event_types.py             # 이벤트 타입 상수
```

---

## ⚙️ 설치 & 실행

### 요구사항
- Python 3.11+
- 운영체제: Windows / macOS / Linux

### 설치
```bash
git clone <repo-url>
cd robot_factory_sim
pip install -r requirements.txt
```

### 실행
```bash
python main.py
```
- 최초 실행 시 `factory_config.db`가 자동 생성되고 기본 공장 구성(스테이션 5개, 충전소 2개, 로봇 5대)이 삽입됩니다.
- GUI 창이 열리면 **[▶ 시작]** 버튼으로 시뮬레이션을 시작하세요.

---

## 🏭 업체 환경 설정 방법

### 방법 1: GUI 설정 에디터
1. 툴바의 **[⚙ 공장 설정]** 클릭
2. 팔레트에서 설비를 드래그 & 드롭으로 배치
3. 클릭하여 속성(좌표, 처리시간, 충전 슬롯 수 등) 편집
4. **[저장]** → 시뮬레이터에 즉시 반영

### 방법 2: DB 직접 수정
외부 시스템(MES/WMS)에서 `factory_config.db`의 테이블을 직접 INSERT/UPDATE하면, ConfigWatcher가 1초 내 자동 감지하여 시뮬레이터에 반영합니다.

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
- **DB 직접**: `production_orders` 테이블에 `priority='critical'` INSERT

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
