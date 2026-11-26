"""데모 영상에 한국어 자막 굽기.

Usage: python tools/burn_subtitles.py <input.mp4> <output.mp4>
       (working-dir: robot_factory_sim)
"""
from __future__ import annotations
import subprocess, sys, tempfile
from pathlib import Path

SRT = """\
1
00:00:00,000 --> 00:00:10,000
[ CLOVER ] 멀티로봇 공장 AI 관제 시스템

2
00:00:10,000 --> 00:00:26,000
5대 로봇이 배터리·거리·납기를 고려한 최적 경로로 작업을 수행합니다

3
00:00:26,000 --> 00:00:50,000
Baseline vs 최적화 스케줄러 동시 실행
처리량·KPI를 대시보드에서 실시간 비교합니다

4
00:00:50,000 --> 00:01:04,000
주문 관리: 납기·우선순위별 생산 주문을 등록하고 현황을 추적합니다

5
00:01:04,000 --> 00:01:20,000
AI 관제 콘솔 — 자연어 명령으로 로봇 추가·충전·배속 조절 등을 직접 제어합니다

6
00:01:20,000 --> 00:01:40,000
⚠ 긴급 이벤트 감지 — AI가 자율 판단으로 즉시 조치하고 결과를 보고합니다

7
00:01:40,000 --> 00:02:00,000
최적화 스케줄러가 Baseline 대비 처리량·완료율을 향상시킵니다
"""

# NanumGothic이나 DejaVu Sans를 fallback으로 사용 (CI 환경)
FORCE_STYLE = (
    "FontName=Noto Sans CJK KR,"
    "FontSize=20,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H80000000,"
    "Bold=0,"
    "BorderStyle=1,"
    "Outline=2,"
    "Shadow=0,"
    "MarginV=28,"
    "Alignment=2"
)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: burn_subtitles.py <input> <output>")
        sys.exit(1)

    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])

    with tempfile.NamedTemporaryFile(suffix=".srt", mode="w",
                                    encoding="utf-8", delete=False) as f:
        f.write(SRT)
        srt_path = f.name

    # 경로 이스케이프 (ffmpeg 필터에서 콜론 이스케이프)
    srt_esc = srt_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        "ffmpeg", "-y", "-i", str(inp),
        "-vf", f"subtitles={srt_esc}:force_style='{FORCE_STYLE}':charenc=UTF-8",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    print("[자막] ffmpeg 실행:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print(f"[OK] 자막 완료: {out}")
    else:
        print("[ERROR] 자막 굽기 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
