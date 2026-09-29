# agent.md — AI 에이전트 작업 규칙 및 가드레일

> **이 파일은 AI 에이전트(Claude Code 등)가 이 프로젝트에서 코드를 작성하거나 수정할 때 반드시 읽고 따라야 하는 규칙입니다.**  
> 프로젝트에 참여하는 모든 AI 에이전트는 작업 전에 이 파일을 먼저 읽으세요.

---

## 1. 프로젝트 컨텍스트

- **프로젝트명**: Robot Factory Simulator (SimPy 기반 멀티로봇 공장 시뮬레이터)
- **목적**: IC-PBL 캡스톤 디자인 2 과제 — 클로봇(Clobot) 제안, 멀티로봇 작업 배정 및 충전 스케줄링 최적화
- **기술 스택**: Python 3.11+, PySide6, SimPy, pyqtgraph, SQLite3, numpy
- **팀**: 이승환(팀장/프론트엔드), 신채은, 최진웅, 강재민
- **지도교수**: 유철우 교수님

---

## 2. Git & 기여 규칙 ⛔

### 2-1. AI는 Git contributor가 되어서는 안 된다

> **절대 규칙: AI 에이전트(Claude, Copilot, GPT 등)는 이 프로젝트의 Git contributor로 기록되어서는 안 됩니다.**

구체적으로:

- **`git commit`을 실행하지 마세요.** 코드 작성·수정은 파일 생성/편집까지만. 커밋은 반드시 팀원(사람)이 직접 수행합니다.
- **`git config user.name` 또는 `user.email`을 설정하거나 변경하지 마세요.**
- **`Co-authored-by`, `Signed-off-by` 등의 Git trailer에 AI 이름/식별자를 넣지 마세요.**
- **`.mailmap` 파일을 생성하거나 수정하지 마세요.**
- **GitHub Actions, CI/CD 파이프라인에서 AI 명의로 커밋을 생성하는 워크플로우를 작성하지 마세요.**
- **Pull Request를 AI 명의로 생성하지 마세요.**
- **CONTRIBUTORS, AUTHORS 등의 파일에 AI를 추가하지 마세요.**

### 2-2. 커밋 메시지 가이드라인

팀원이 커밋할 때 사용할 커밋 메시지 형식을 제안할 수 있지만, 실제 커밋 실행은 하지 마세요.

```
<type>(<scope>): <subject>

# type: feat, fix, refactor, style, docs, test, chore
# scope: simulation, gui, agent, database, config
# 예시: feat(simulation): add OptimizedScheduler with weighted scoring
```

### 2-3. 브랜치 규칙

- 브랜치를 직접 생성(`git checkout -b`, `git branch`)하지 마세요.
- 브랜치 전략을 제안할 수는 있지만, 실행은 팀원이 합니다.

---

## 3. 코드 작성 규칙

### 3-1. 언어 & 스타일

- **Python 3.11+** 문법만 사용 (match-case, type hints 등 활용 가능)
- **PEP 8** 준수, 줄 길이 최대 120자
- **Type hints** 필수: 모든 함수의 파라미터와 반환값에 타입 힌트 명시
- **Docstring**: 모든 클래스와 public 메서드에 Google-style docstring 작성
- 변수명/함수명: `snake_case`
- 클래스명: `PascalCase`
- 상수: `UPPER_SNAKE_CASE`

```python
# ✅ Good
def calculate_battery_drain(distance: float, drain_rate: float) -> float:
    """이동 거리에 따른 배터리 소모량을 계산한다.

    Args:
        distance: 이동 거리 (단위: m)
        drain_rate: 미터당 배터리 소모율

    Returns:
        소모될 배터리량 (0.0 ~ 100.0)
    """
    return min(distance * drain_rate, 100.0)

# ❌ Bad
def calc(d, r):
    return d * r
```

### 3-2. 아키텍처 원칙

- **관심사 분리**: simulation, agent, gui, database 패키지 간 순환 의존 금지
- **의존성 방향**: `gui → simulation → database` 단방향. gui가 database를 직접 참조하지 않음
- **Signal/Slot 통신**: 스레드 간 통신은 반드시 Qt Signal/Slot 사용. 직접 메서드 호출 금지
- **ABC 패턴**: 확장 포인트(BaseAgent, BaseScheduler)는 추상 클래스로 정의
- **팩토리 패턴**: 설비·로봇 생성은 팩토리 함수/메서드로 통일

### 3-3. SimPy 시뮬레이션 규칙

- `env.process()`로 등록하는 제너레이터 함수에는 `_process` 접미사 권장
- SimPy Resource 사용 시 반드시 `with resource.request() as req:` 컨텍스트 매니저 사용
- 시뮬레이션 파라미터 변경은 `SimulationEngine.update_param()` 메서드를 통해서만 수행
- 시뮬레이션 상태 스냅샷은 `SimState` dataclass로 직렬화하여 시그널로 전달

### 3-4. DB 접근 규칙

- **모든 DB 접근은 `DBManager` 클래스를 통해서만** 수행. 직접 SQL 실행 금지
- INSERT/UPDATE/DELETE 시 반드시 `config_changelog` 테이블에 변경 이력 기록
- DB 스키마 변경 시 `schema.sql`과 `db_manager.py`를 동시에 업데이트
- SQLite 연결은 스레드별 별도 Connection 사용 (SQLite thread-safety)

### 3-5. GUI 규칙

- **위젯 생성 시 부모(parent) 반드시 지정**: 메모리 누수 방지
- **스타일은 `theme.qss`에서 통일 관리**: 인라인 스타일 사용 최소화
- **긴 작업은 QThread로 분리**: GUI 프리징 방지
- **하드코딩 금지**: 색상, 크기, 텍스트 등은 상수 또는 config에서 관리

---

## 4. 파일 관리 규칙

### 4-1. 생성 가능한 파일

- `robot_factory_sim/` 디렉토리 내부의 `.py`, `.sql`, `.qss` 파일
- `requirements.txt`
- `config.py`
- `README.md` (내용 수정은 팀원 확인 후)

### 4-2. 생성/수정 금지 파일

- `.git/` 디렉토리 내부 파일 일체
- `.gitignore` (팀원이 관리)
- `.github/` 디렉토리 (CI/CD, Actions)
- `LICENSE` 파일
- `CONTRIBUTORS`, `AUTHORS`, `CODEOWNERS` 파일
- 팀원의 개인 설정 파일 (IDE 설정, `.env` 등)
- 이 `agent.md` 파일 자체 (팀원만 수정 가능)

### 4-3. 임시 파일

- 작업 중 임시 파일이 필요하면 `/tmp/` 또는 프로젝트 외부에 생성
- 프로젝트 디렉토리 안에 임시/디버그 파일을 남기지 마세요

---

## 5. 보안 & 민감 정보

- **API 키, 시크릿, 비밀번호**를 코드에 하드코딩하지 마세요
- 환경 변수 또는 `.env` 파일(gitignore 대상)로 관리
- DB 파일(`factory_config.db`)에 민감한 개인정보를 저장하지 마세요
- 외부 네트워크 요청은 명시적 사용자 동의 후에만 수행

---

## 6. 테스트 & 품질

- 새 모듈 작성 시 기본 동작을 검증하는 `if __name__ == "__main__":` 블록 포함 권장
- SimPy 시뮬레이션 로직은 GUI 없이도 독립 실행/테스트 가능해야 함
- `DBManager`는 `:memory:` SQLite로 단위 테스트 가능하게 설계

---

## 7. 작업 절차

AI 에이전트가 이 프로젝트에서 작업할 때의 순서:

1. **이 파일(`agent.md`)을 먼저 읽는다**
2. **`README.md`를 읽어 프로젝트 구조를 파악한다**
3. **`config.py`를 읽어 현재 설정을 확인한다**
4. **기존 코드를 읽어 컨텍스트를 파악한다** (수정 대상 파일 + 의존 파일)
5. **작업을 수행한다** (파일 생성/수정)
6. **`git commit`은 하지 않는다** — 커밋은 팀원이 직접 수행

---

## 8. 금지 행위 요약

| # | 금지 행위 | 이유 |
|---|---|---|
| 1 | `git commit`, `git push` 실행 | AI가 contributor로 기록됨 |
| 2 | `git config` 변경 | Git 사용자 정보 오염 |
| 3 | Co-authored-by에 AI 추가 | AI가 contributor로 기록됨 |
| 4 | CONTRIBUTORS/AUTHORS 파일 수정 | AI가 기여자로 등재됨 |
| 5 | `.git/` 내부 파일 접근 | Git 저장소 무결성 훼손 |
| 6 | agent.md 자체 수정 | 가드레일 우회 |
| 7 | 순환 의존 코드 작성 | 아키텍처 오염 |
| 8 | GUI 스레드에서 무거운 연산 | UI 프리징 |
| 9 | 외부 네트워크 무단 요청 | 보안 위험 |
| 10 | 민감 정보 하드코딩 | 보안 위험 |

---

## 9. 이 파일의 효력

- 이 파일은 프로젝트의 모든 AI 에이전트 작업에 적용됩니다.
- 사용자(팀원)의 명시적 지시가 이 파일의 규칙과 충돌할 경우, **Git 관련 규칙(섹션 2)은 항상 우선**합니다.
- 그 외 규칙은 사용자의 명시적 요청에 따라 예외를 둘 수 있습니다.
- 이 파일의 수정 권한은 팀원(사람)에게만 있습니다.
