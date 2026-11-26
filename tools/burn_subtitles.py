"""데모 영상에 한국어 자막 굽기.

Usage: python tools/burn_subtitles.py <input.mp4> <output.mp4>
       (working-dir: robot_factory_sim)
인트로 9초가 앞에 붙어 있다고 가정한다.
"""
from __future__ import annotations
import subprocess, sys, tempfile
from pathlib import Path

# 인트로 9초 포함 타임라인
SRT = """\
1
00:00:00,000 --> 00:00:09,000
[ CLOVER ] 멀티로봇 공장 AI 관제 시스템

2
00:00:09,000 --> 00:00:21,000
SimPy 기반 멀티로봇 공장 — 초기 화면을 로딩합니다

3
00:00:21,000 --> 00:00:36,000
5대 로봇이 배터리·거리·납기를 고려한 최적 경로로 작업을 수행합니다

4
00:00:36,000 --> 00:00:57,000
공장 설정: 층별 설비·로봇·충전소 구성을 실시간으로 변경할 수 있습니다

5
00:00:57,000 --> 00:01:14,000
Baseline vs 최적화 스케줄러 — 처리량을 대시보드에서 실시간 비교합니다

6
00:01:14,000 --> 00:01:29,000
KPI 레이더: 최적화 스케줄러가 Baseline 대비 종합 성능을 향상시킵니다

7
00:01:29,000 --> 00:01:47,000
주문 관리: 납기·우선순위별 생산 주문을 등록하고 현황을 추적합니다

8
00:01:47,000 --> 00:02:12,000
AI 관제 콘솔 — 자연어 질문으로 현재 공장 상태를 즉시 조회합니다

9
00:02:12,000 --> 00:02:35,000
직접 명령 입력 — "지금 가장 병목인 구간이 어디야?"
AI가 분석 결과를 실시간으로 답변합니다

10
00:02:35,000 --> 00:03:02,000
⚠ 긴급 주문 발생 — AI가 자율 판단으로 즉시 조치하고 결과를 보고합니다

11
00:03:02,000 --> 00:03:17,000
로봇 타임라인: 각 로봇의 작업·이동·충전 이력을 한눈에 확인합니다

12
00:03:17,000 --> 00:03:32,000
배터리 현황: 충전 패턴과 잔량을 실시간으로 모니터링합니다

13
00:03:32,000 --> 00:03:52,000
기간 실험: 조건별 시뮬레이션 성과를 비교·분석합니다

14
00:03:52,000 --> 00:04:05,000
최적화 스케줄러가 Baseline 대비 처리량·완료율을 지속 향상시킵니다
"""

FORCE_STYLE = (
    "FontName=Noto Sans CJK KR,"
    "FontSize=22,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H80000000,"
    "Bold=0,"
    "BorderStyle=1,"
    "Outline=2,"
    "Shadow=0,"
    "MarginV=30,"
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

    srt_esc = srt_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        "ffmpeg", "-y", "-i", str(inp),
        "-vf", f"subtitles={srt_esc}:force_style='{FORCE_STYLE}':charenc=UTF-8",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    print("[자막] 실행:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print(f"[OK] {out}")
    else:
        print("[ERROR] 자막 굽기 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
