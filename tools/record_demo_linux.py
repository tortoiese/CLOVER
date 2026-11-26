"""CLOVER 데모 화면 녹화 — Linux/Xvfb 환경 전용.

실행: python tools/record_demo_linux.py  (working-dir: robot_factory_sim)
출력: robot_factory_sim/demo_raw.mp4
"""
from __future__ import annotations
import os, subprocess, sys, time
from pathlib import Path

# DISPLAY가 설정돼 있어야 함
DISPLAY = os.environ.get("DISPLAY", ":99")
W, H = 1920, 1080
FPS = 30
OUTPUT = Path("demo_raw.mp4")

PYTHON = sys.executable


def wait_window(title_part: str, timeout: float = 30.0) -> str | None:
    """xdotool로 창 ID 반환. 없으면 None."""
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


def click(x: int, y: int) -> None:
    subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"],
                   check=False, stderr=subprocess.DEVNULL)
    time.sleep(0.2)


def key(k: str) -> None:
    subprocess.run(["xdotool", "key", k],
                   check=False, stderr=subprocess.DEVNULL)
    time.sleep(0.2)


def sleep(s: float) -> None:
    time.sleep(s)


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
print("[1] ffmpeg 녹화 중...")

# ------------------------------------------------------------------
# 2. 앱 실행
# ------------------------------------------------------------------
print("[2] 앱 실행...")
env = {**os.environ, "PYTHONUTF8": "1",
       "QT_QPA_PLATFORM": "xcb",
       "CLOVER_RAG_ENABLED": os.environ.get("CLOVER_RAG_ENABLED", "0"),
       "CLOVER_RAG_PROVIDER": os.environ.get("CLOVER_RAG_PROVIDER", "local"),
       "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
       "DISPLAY": DISPLAY}
app = subprocess.Popen([PYTHON, "-B", "main.py"], env=env)
sleep(10)  # 앱 로드 대기

wid = wait_window("CLOVER")
if not wid:
    print("[ERROR] CLOVER 창을 찾지 못했습니다.")
    ffproc.terminate(); app.terminate(); sys.exit(1)
print(f"[2] 창 ID: {wid}")

# 창 최대화 + 활성화
subprocess.run(["wmctrl", "-ir", wid, "-b", "add,maximized_vert,maximized_horz"],
               check=False)
subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
sleep(1.5)

# 창 위치/크기 확인
geo = subprocess.check_output(["xdotool", "getwindowgeometry", wid]).decode()
print("[2]", geo.strip())

# 편의상 창이 (0,0) 에서 W x H 라고 가정 (최대화 후)
# 실제 위치를 파싱
import re
m_pos  = re.search(r"Position:\s*(\d+),(\d+)", geo)
m_size = re.search(r"Geometry:\s*(\d+)x(\d+)", geo)
rx = int(m_pos.group(1)) if m_pos else 0
ry = int(m_pos.group(2)) if m_pos else 0
rw = int(m_size.group(1)) if m_size else W
rh = int(m_size.group(2)) if m_size else H
print(f"[2] 창: pos=({rx},{ry}) size={rw}x{rh}")

# ------------------------------------------------------------------
# 버튼 절대 좌표 (창 기준)
# ------------------------------------------------------------------
TB = 28     # 툴바 y
TAB_Y = 72  # 탭바 y

# 툴바 버튼 (Qt 고정 크기)
bx = lambda lx: rx + lx
by = lambda ly: ry + ly

START  = (bx(45),  by(TB))
PAUSE  = (bx(118), by(TB))
STOP   = (bx(188), by(TB))
SPD    = (bx(320), by(TB))   # 배속 콤보박스
ORDER  = (bx(rw - 390), by(TB))

TAB_SIM = (bx(50),  by(TAB_Y))
TAB_CON = (bx(145), by(TAB_Y))

CON_CX = rx + rw // 2
BTN_STATUS = (CON_CX - 220, ry + rh - 65)
BTN_URGENT = (CON_CX + 195, ry + rh - 65)

DASH_X = rx + int(rw * 0.63)
TAB_THRU  = (DASH_X + 45,  ry + TAB_Y + 50)
TAB_RADAR = (DASH_X + 265, ry + TAB_Y + 50)

# ------------------------------------------------------------------
# 3. 시연
# ------------------------------------------------------------------
print("[3] 시연 시작...")
sleep(3)   # 초기 화면 보여주기

# 시작 버튼
click(*START)
sleep(0.5)

# 배속 10x
click(*SPD)
sleep(0.4)
key("End")
sleep(0.2)
key("Return")
sleep(0.5)

# 로봇 이동 확인
print("[3] 로봇 이동...")
sleep(8)

# KPI 처리량 탭
click(*TAB_THRU)
sleep(0.5)
print("[3] KPI 처리량 대시보드...")
sleep(8)

# KPI 레이더
click(*TAB_RADAR)
sleep(5)

# 주문 관리
print("[3] 주문 관리...")
click(*ORDER)
sleep(4)
key("Escape")
sleep(0.5)

# AI 관제 콘솔
print("[3] AI 관제 콘솔...")
click(*TAB_CON)
sleep(1)

click(*BTN_STATUS)
sleep(8)

# 긴급 주문
print("[3] 긴급 주문...")
click(*BTN_URGENT)
sleep(10)

# 최종 로봇 확인
print("[3] 로봇 최종 확인...")
click(*TAB_SIM)
sleep(8)

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
    ffproc.kill()
    ffproc.wait()

app.terminate()
app.wait(timeout=10)

if OUTPUT.exists() and OUTPUT.stat().st_size > 100_000:
    print(f"[OK] 녹화 완료: {OUTPUT} ({OUTPUT.stat().st_size // 1024} KB)")
else:
    print(f"[ERROR] 녹화 실패 또는 파일이 너무 작습니다.")
    print(open("ffmpeg.log").read()[-800:])
    sys.exit(1)
