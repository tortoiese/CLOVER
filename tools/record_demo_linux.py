"""CLOVER 데모 화면 녹화 — Linux/Xvfb 환경 전용 (상세 시연, 약 3분 30초).

실행: python tools/record_demo_linux.py  (working-dir: robot_factory_sim)
출력: robot_factory_sim/demo_raw.mp4
"""
from __future__ import annotations
import os, re, subprocess, sys, time
from pathlib import Path

DISPLAY = os.environ.get("DISPLAY", ":99")
W, H = 1920, 1080
FPS = 30
OUTPUT = Path("demo_raw.mp4")
PYTHON = sys.executable


# ------------------------------------------------------------------
# 헬퍼
# ------------------------------------------------------------------
def xdo(*args: str) -> None:
    subprocess.run(["xdotool", *args], check=False, stderr=subprocess.DEVNULL)


def click(x: int, y: int) -> None:
    xdo("mousemove", str(x), str(y), "click", "1")
    time.sleep(0.25)


def type_text(text: str) -> None:
    xdo("type", "--clearmodifiers", "--delay", "30", text)
    time.sleep(0.3)


def key(k: str) -> None:
    xdo("key", k)
    time.sleep(0.2)


def sleep(s: float) -> None:
    time.sleep(s)


def activate_window(wid: str) -> None:
    xdo("windowactivate", "--sync", wid)
    sleep(0.3)


def clear_input_and_type(wid: str, text: str) -> None:
    """입력창 선택 후 기존 내용 지우고 새 텍스트 입력."""
    activate_window(wid)
    click(*INPUT_BOX)
    sleep(0.3)
    key("ctrl+a")
    sleep(0.1)
    key("Delete")
    sleep(0.2)
    type_text(text)
    sleep(0.3)
    key("Return")


# ------------------------------------------------------------------
# 1. ffmpeg 녹화 시작
# ------------------------------------------------------------------
print("[1] ffmpeg 녹화 시작...")
ffproc = subprocess.Popen([
    "ffmpeg", "-y",
    "-f", "x11grab", "-r", str(FPS),
    "-s", f"{W}x{H}",
    "-i", f"{DISPLAY}.0",
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
    "-pix_fmt", "yuv420p",
    str(OUTPUT),
], stdin=subprocess.PIPE, stderr=open("ffmpeg.log", "w"))
sleep(2)
if ffproc.poll() is not None:
    print("[ERROR] ffmpeg 시작 실패")
    print(open("ffmpeg.log").read()[-800:])
    sys.exit(1)
print("[1] 녹화 중...")

# ------------------------------------------------------------------
# 2. 앱 실행
# ------------------------------------------------------------------
print("[2] 앱 실행...")
env = {
    **os.environ,
    "PYTHONUTF8": "1",
    "QT_QPA_PLATFORM": "xcb",
    "CLOVER_RAG_ENABLED": os.environ.get("CLOVER_RAG_ENABLED", "0"),
    "CLOVER_RAG_PROVIDER": os.environ.get("CLOVER_RAG_PROVIDER", "local"),
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    "DISPLAY": DISPLAY,
}
app = subprocess.Popen([PYTHON, "-B", "main.py"], env=env)
sleep(12)   # 앱 + AI 에이전트 초기화 대기

# 창 찾기
def wait_window(title_part: str, timeout: float = 30.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                ["xdotool", "search", "--name", title_part],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if out:
                return out.split()[0]
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    return None

wid = wait_window("CLOVER")
if not wid:
    print("[ERROR] CLOVER 창을 찾지 못했습니다.")
    ffproc.terminate(); app.terminate(); sys.exit(1)
print(f"[2] 창 ID: {wid}")

subprocess.run(["wmctrl", "-ir", wid, "-b", "add,maximized_vert,maximized_horz"], check=False)
activate_window(wid)
sleep(1.5)

geo = subprocess.check_output(["xdotool", "getwindowgeometry", wid]).decode()
print("[2]", geo.strip())
m_pos  = re.search(r"Position:\s*(\d+),(\d+)", geo)
m_size = re.search(r"Geometry:\s*(\d+)x(\d+)", geo)
rx = int(m_pos.group(1)) if m_pos else 0
ry = int(m_pos.group(2)) if m_pos else 0
rw = int(m_size.group(1)) if m_size else W
rh = int(m_size.group(2)) if m_size else H
print(f"[2] pos=({rx},{ry}) size={rw}x{rh}")

# ------------------------------------------------------------------
# 절대 좌표 계산
# ------------------------------------------------------------------
TB   = 28    # 툴바 y
TABy = 72    # 메인 탭바 y

def bx(lx: int) -> int: return rx + lx
def by(ly: int) -> int: return ry + ly

# 툴바 버튼
BTN_START  = (bx(45),  by(TB))
BTN_PAUSE  = (bx(118), by(TB))
BTN_STOP   = (bx(188), by(TB))
SPD_COMBO  = (bx(315), by(TB))
BTN_CONFIG = (bx(rw - 490), by(TB))
BTN_ORDER  = (bx(rw - 400), by(TB))

# 메인 탭
TAB_SIM  = (bx(50),  by(TABy))
TAB_CON  = (bx(145), by(TABy))
TAB_EXP  = (bx(240), by(TABy))

# 대시보드 패널
DASH_X = rx + int(rw * 0.63)
DT = ry + TABy + 50
TAB_THRU     = (DASH_X + 45,  DT)
TAB_TIMELINE = (DASH_X + 135, DT)
TAB_QUEUE    = (DASH_X + 210, DT)
TAB_BATT     = (DASH_X + 275, DT)
TAB_RADAR    = (DASH_X + 345, DT)

# 관제 콘솔 패널
CON_CX     = rx + rw // 2
BTN_STATUS = (CON_CX - 220, ry + rh - 65)   # 현재 상태 조회
BTN_EFF    = (CON_CX - 130, ry + rh - 65)   # 효율 분석
BTN_BOTTLE = (CON_CX - 40,  ry + rh - 65)   # 병목 분석
BTN_URGENT = (CON_CX + 195, ry + rh - 65)   # 긴급 주문 발생
INPUT_BOX  = (CON_CX,       ry + rh - 33)   # 텍스트 입력창

# ------------------------------------------------------------------
# 3. 시연 (약 3분 20초)
# ------------------------------------------------------------------
print("[3] 시연 시작...")

# ── 장면 1: 초기 화면 (10초) ──
sleep(10)

# ── 장면 2: 시뮬레이션 시작 + 10x 배속 ──
print("[3] 시뮬레이션 시작...")
activate_window(wid)
click(*BTN_START)
sleep(0.5)
click(*SPD_COMBO)
sleep(0.4)
key("End")
sleep(0.2)
key("Return")
sleep(0.5)
sleep(15)   # 로봇 이동 확인

# ── 장면 3: 공장 설정 다이얼로그 ──
print("[3] 공장 설정...")
activate_window(wid)
click(*BTN_CONFIG)
sleep(10)
key("Escape")
sleep(1)

# ── 장면 4: KPI 처리량 대시보드 ──
print("[3] KPI 처리량...")
activate_window(wid)
click(*TAB_THRU)
sleep(12)

# ── 장면 5: KPI 레이더 ──
print("[3] KPI 레이더...")
activate_window(wid)
click(*TAB_RADAR)
sleep(10)

# ── 장면 6: 주문 관리 다이얼로그 (상세) ──
print("[3] 주문 관리...")
activate_window(wid)
click(*BTN_ORDER)
sleep(14)   # 주문 목록 상세 확인
key("Escape")
sleep(1)

# ── 장면 7a: AI 관제 콘솔 — 현재 상태 조회 ──
print("[3] AI 관제 콘솔 — 현재 상태 조회...")
activate_window(wid)
click(*TAB_CON)
sleep(1)
click(*BTN_STATUS)
sleep(15)   # AI 응답 대기

# ── 장면 7b: AI 효율 분석 버튼 ──
print("[3] AI 효율 분석...")
activate_window(wid)
click(*BTN_EFF)
sleep(12)   # AI 응답 대기

# ── 장면 8a: 직접 명령 — 병목 질의 ──
print("[3] AI 직접 질의 — 병목 위치...")
clear_input_and_type(wid, "지금 가장 병목인 구간이 어디야?")
sleep(15)   # AI 분석 응답 대기

# ── 장면 8b: 직접 명령 — 로봇 작업 재배치 오더 ──
print("[3] AI 직접 오더 — 로봇 재배치 명령...")
clear_input_and_type(wid, "5번 로봇 충전소로 귀환시키고 작업 재배치해줘")
sleep(20)   # AI 명령 실행 및 결과 대기

# ── 장면 8c: 직접 명령 — 처리량 최대화 오더 ──
print("[3] AI 직접 오더 — 처리량 최대화 명령...")
clear_input_and_type(wid, "처리량 최대화 방향으로 스케줄러 재조정 명령 내려줘")
sleep(20)   # AI 명령 실행 및 결과 대기

# ── 장면 9: 긴급 주문 발생 (자율 AI 대응) ──
print("[3] 긴급 주문 발생...")
activate_window(wid)
click(*BTN_URGENT)
sleep(22)   # AI 긴급 대응 대기

# ── 장면 9b: 주문 관리 — 긴급 주문 처리 결과 확인 ──
print("[3] 주문 처리 결과 확인...")
activate_window(wid)
click(*BTN_ORDER)
sleep(12)   # 긴급 주문이 목록에 반영된 것 확인
key("Escape")
sleep(1)

# ── 장면 10: 로봇 타임라인 탭 ──
print("[3] 로봇 타임라인...")
activate_window(wid)
click(*TAB_SIM)
sleep(0.5)
click(*TAB_TIMELINE)
sleep(10)

# ── 장면 11: 배터리 탭 ──
print("[3] 배터리 현황...")
activate_window(wid)
click(*TAB_BATT)
sleep(10)

# ── 장면 12: 기간 실험 탭 ──
print("[3] 기간 실험...")
activate_window(wid)
click(*TAB_EXP)
sleep(12)

# ── 장면 13: 시뮬레이션 최종 복귀 ──
print("[3] 최종 화면...")
activate_window(wid)
click(*TAB_SIM)
click(*TAB_THRU)
sleep(10)

# ------------------------------------------------------------------
# 4. 종료
# ------------------------------------------------------------------
print("[4] 녹화 종료...")
ffproc.stdin.write(b"q\n")
ffproc.stdin.flush()
ffproc.stdin.close()
try:
    ffproc.wait(timeout=20)
    print("[4] ffmpeg 정상 종료")
except subprocess.TimeoutExpired:
    ffproc.kill(); ffproc.wait()

app.terminate()
app.wait(timeout=10)

if OUTPUT.exists() and OUTPUT.stat().st_size > 500_000:
    print(f"[OK] {OUTPUT}  ({OUTPUT.stat().st_size // 1024} KB)")
else:
    print("[ERROR] 녹화 파일 없거나 너무 작음")
    if Path("ffmpeg.log").exists():
        print(open("ffmpeg.log").read()[-800:])
    sys.exit(1)
