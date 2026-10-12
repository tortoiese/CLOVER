"""ROI 자동 그래프.

ROI 결과(베이스라인 vs 최적화안)를 자동으로 비교 그래프로 그린다.
matplotlib Figure를 반환하거나 PNG로 저장한다.
GUI(Qt)에서는 FigureCanvasQTAgg로 임베드할 수 있다.

compute_roi()로 구한 두 ROIResult를 넘기면, 전력 소비량·총투입비용·총순이익·ROI를
한 번에 비교하는 2x2 막대 그래프를 자동으로 그린다.
"""

from __future__ import annotations

from utils.roi import ROIResult

# (키, 제목, 단위, 높을수록 좋은지)
_PANELS: tuple[tuple[str, str, str, bool], ...] = (
    ("energy_wh", "전력 소비량", "Wh", False),
    ("total_cost", "총투입비용", "원", False),
    ("net_profit", "총순이익", "원", True),
    ("roi_pct", "ROI", "%", True),
)

_COLOR_BASE = "#9aa7b0"   # 베이스라인(회색)
_COLOR_OPT = "#0c8f82"    # 최적화안(청록)
_COLOR_GOOD = "#1f8f4e"
_COLOR_BAD = "#c23b3b"


def _setup_korean_font() -> None:
    """사용 가능한 한글 폰트를 matplotlib 기본으로 설정(없으면 조용히 통과)."""
    try:
        import matplotlib
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in ("AppleGothic", "Malgun Gothic", "NanumGothic", "NanumBarunGothic", "Noto Sans CJK KR"):
            if name in available:
                matplotlib.rcParams["font.family"] = name
                break
        matplotlib.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def make_comparison_figure(baseline: ROIResult, optimized: ROIResult):
    """베이스라인 vs 최적화안 비교 그래프(Figure)를 만든다.

    Args:
        baseline: 현행(baseline) ROI 결과
        optimized: 최적화안 ROI 결과

    Returns:
        matplotlib Figure (GUI 임베드 또는 저장에 사용)
    """
    import matplotlib
    matplotlib.use("Agg", force=False)  # GUI 없이도 안전
    import matplotlib.pyplot as plt

    _setup_korean_font()

    fig, axes = plt.subplots(2, 2, figsize=(9, 6))
    fig.suptitle("운영안 비교: 베이스라인 vs 최적화안", fontsize=13, fontweight="bold")

    for ax, (key, title, unit, higher_better) in zip(axes.flat, _PANELS):
        b = getattr(baseline, key)
        o = getattr(optimized, key)
        bars = ax.bar(["베이스라인", "최적화안"], [b, o], color=[_COLOR_BASE, _COLOR_OPT], width=0.55)
        # 변화율과 좋은/나쁜 방향 표시
        if b not in (0, 0.0):
            pct = (o - b) / b * 100.0
            improved = (o > b) if higher_better else (o < b)
            ax.set_title(
                f"{title}  ({pct:+.1f}%)",
                fontsize=11,
                color=_COLOR_GOOD if improved else _COLOR_BAD,
            )
        else:
            ax.set_title(title, fontsize=11)
        ax.set_ylabel(unit, fontsize=9)
        ax.margins(y=0.18)
        for bar, val in zip(bars, [b, o]):
            ax.annotate(
                f"{val:,.0f}",
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
            )
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig


def save_comparison_chart(baseline: ROIResult, optimized: ROIResult, path: str, dpi: int = 130) -> str:
    """비교 그래프를 PNG로 저장한다. (시뮬 종료 시 자동 호출용)

    Args:
        baseline: 현행(baseline) ROI 결과
        optimized: 최적화안 ROI 결과
        path: 저장 경로 (.png)
        dpi: 해상도

    Returns:
        저장된 파일 경로
    """
    fig = make_comparison_figure(baseline, optimized)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    return path


if __name__ == "__main__":
    from utils.roi import compute_roi

    base = compute_roi(completed_units=500, total_distance_m=20000)
    opt = compute_roi(completed_units=510, total_distance_m=16500)
    out = save_comparison_chart(base, opt, "roi_comparison.png")
    print("saved:", out)
