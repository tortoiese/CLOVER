"""CLOVER 시연영상 아웃트로 클립 생성.

Usage: python tools/make_outro.py  (working-dir: robot_factory_sim)
Output: robot_factory_sim/outro.mp4  (7초, 1920x1080)
"""
from __future__ import annotations
import subprocess, sys
from pathlib import Path

FPS = 30
DURATION = 7
W, H = 1920, 1080

BG    = (21,  26,  30)
ACC   = (115, 184, 163)
WHITE = (238, 242, 241)
MUTED = (172, 185, 184)


def find_cjk_font() -> str:
    try:
        lines = subprocess.check_output(
            ["fc-list", ":lang=ko", "--format=%{file}\n"],
            stderr=subprocess.DEVNULL,
        ).decode().strip().split("\n")
    except Exception:
        return ""
    for line in lines:
        l = line.lower()
        if ("noto" in l or "malgun" in l) and ("bold" in l or "cjk" in l):
            return line.strip()
    return lines[0].strip() if lines else ""


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def alpha_at(t: float) -> float:
    """페이드인 0→1초, 페이드아웃 5.5→7초."""
    if t < 1.0:
        return t
    if t > 5.5:
        return max(0.0, 1.0 - (t - 5.5) / 1.5)
    return 1.0


def tint(color: tuple[int, int, int], a: float) -> tuple[int, int, int]:
    return tuple(int(c * a) for c in color)  # type: ignore[return-value]


def render_frame(draw, font_msg, font_title, font_mini, t: float) -> None:
    base_a = alpha_at(t)
    cx, cy = W // 2, H // 2

    # 감사 메시지 (0.3초 후 등장)
    if t > 0.3:
        a = lerp(0, 1, (t - 0.3) / 0.7) * base_a
        draw.text((cx, cy - 60), "시청해주셔서 감사합니다",
                  font=font_msg, fill=tint(WHITE, a), anchor="mm")

    # CLOVER (0.9초 후)
    if t > 0.9:
        a = lerp(0, 1, (t - 0.9) / 0.6) * base_a
        draw.text((cx, cy + 60), "CLOVER",
                  font=font_title, fill=tint(ACC, a), anchor="mm")

    # 소속 (1.4초 후)
    if t > 1.4:
        a = lerp(0, 1, (t - 1.4) / 0.6) * base_a
        draw.text((cx, cy + 150), "명지대학교 캡스톤 디자인 2",
                  font=font_mini, fill=tint(MUTED, a), anchor="mm")


def main() -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow", "-q"])
        from PIL import Image, ImageDraw, ImageFont  # type: ignore[no-redef]

    font_path = find_cjk_font()
    print(f"[outro] 폰트: {font_path or '(기본 폰트 사용)'}")

    def load_font(size: int):
        if font_path:
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    fm_msg   = load_font(58)   # 시청해주셔서 감사합니다
    fm_title = load_font(90)   # CLOVER
    fm_mini  = load_font(34)   # 소속

    out_dir = Path("outro_frames")
    out_dir.mkdir(exist_ok=True)

    total = FPS * DURATION
    for i in range(total):
        t = i / FPS
        img = Image.new("RGB", (W, H), color=BG)
        draw = ImageDraw.Draw(img)
        render_frame(draw, fm_msg, fm_title, fm_mini, t)
        img.save(str(out_dir / f"frame_{i:05d}.png"))
        if i % 60 == 0:
            print(f"[outro] {i}/{total} 프레임 완료")

    subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(out_dir / "frame_%05d.png"),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "outro.mp4",
    ], check=True)

    print("[outro] outro.mp4 완료")


if __name__ == "__main__":
    main()
