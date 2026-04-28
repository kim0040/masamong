"""Seaborn/Matplotlib 기반 활동 랭킹 차트 생성 유틸."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import tempfile

KOREAN_FONT_CANDIDATES = [
    "Pretendard",
    "Apple SD Gothic Neo",
    "AppleGothic",
    "NanumGothic",
    "Nanum Gothic",
    "Noto Sans CJK KR",
    "Noto Sans KR",
    "Malgun Gothic",
]


def _prepare_matplotlib_env() -> None:
    """샌드박스/저권한 환경에서도 캐시 경로를 확보합니다."""
    base_tmp = Path(tempfile.gettempdir()) / "masamong_mpl"
    mpl_dir = base_tmp / "mplconfig"
    cache_dir = base_tmp / "cache"
    mpl_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLCONFIGDIR", str(mpl_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))


def _resolve_korean_font_name() -> str:
    try:
        from matplotlib import font_manager
    except Exception:
        return "sans-serif"

    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in KOREAN_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return "sans-serif"


def build_activity_ranking_chart_bytes(
    *,
    channel_name: str,
    period_label: str,
    ranking_rows: list[dict],
    total_messages: int,
    total_users: int,
    generated_at_kst: datetime | None = None,
) -> bytes:
    """활동 랭킹 데이터를 PNG 바이트로 렌더링합니다."""
    if not ranking_rows:
        raise ValueError("ranking_rows is empty")
    ranking_rows = [row for row in ranking_rows if int(row.get("count", 0)) > 0]
    if not ranking_rows:
        raise ValueError("no positive activity rows")

    try:
        _prepare_matplotlib_env()
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import seaborn as sns
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("seaborn and matplotlib are required for ranking chart generation") from exc

    generated_at = generated_at_kst or datetime.now()
    top_rows = ranking_rows[:10]

    labels = [f"{row['rank']}위 {str(row['user_name'])}" for row in top_rows]
    counts = [int(row["count"]) for row in top_rows]
    shares = [float(row.get("share", 0.0)) for row in top_rows]
    grade_map = {
        "🔥 채널 지배자": "채널 지배자",
        "⚡ 폭주 기관차": "폭주 기관차",
        "🎯 핵심 멤버": "핵심 멤버",
        "🧃 꾸준 멤버": "꾸준 멤버",
        "🌱 워밍업 중": "워밍업 중",
    }
    grades = [grade_map.get(str(row.get("grade", "정보 없음")), str(row.get("grade", "정보 없음"))) for row in top_rows]
    max_count = max(counts)

    font_name = _resolve_korean_font_name()
    sns.set_theme(
        style="whitegrid",
        rc={
            "font.family": font_name,
            "font.sans-serif": [font_name, "DejaVu Sans", "Arial"],
            "axes.unicode_minus": False,
        },
    )

    row_count = len(top_rows)
    max_name_len = max(len(str(row["user_name"])) for row in top_rows)
    fig_width = 8.3
    fig_height = max(6.0, 2.8 + row_count * 0.72)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=170)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    palette = sns.color_palette("blend:#FB7185,#F59E0B,#10B981,#3B82F6,#8B5CF6", n_colors=len(top_rows))
    bars = ax.barh(labels, counts, color=palette, edgecolor="none", height=0.62)
    ax.invert_yaxis()

    right_margin_ratio = 1.22
    for bar, count, share, grade in zip(bars, counts, shares, grades):
        y_center = bar.get_y() + bar.get_height() / 2
        ax.text(
            count + max_count * 0.015,
            y_center,
            f"{count}회 | {share:.1f}% | {grade}",
            va="center",
            ha="left",
            fontsize=10.4,
            fontweight="bold",
            color="#2E3440",
        )

    ax.set_xlim(0, max_count * right_margin_ratio)
    ax.set_xlabel("메시지 수", fontsize=12.8, color="#1F2937", labelpad=8, fontweight="bold")
    ax.set_ylabel("")
    ax.tick_params(axis="x", labelsize=10.2, colors="#4B5563")
    ax.tick_params(axis="y", labelsize=12.0, colors="#111827")
    for tick in ax.get_yticklabels():
        tick.set_fontweight(800)
    ax.grid(axis="x", color="#E5E7EB", linewidth=1.0, alpha=0.95)
    ax.grid(axis="y", visible=False)
    for spine in ax.spines.values():
        spine.set_visible(False)

    left_margin = min(0.36, 0.18 + max_name_len * 0.009)
    fig.subplots_adjust(left=left_margin, right=0.96, top=0.86, bottom=0.11)

    fig.text(
        0.5,
        0.95,
        "마사몽 활동 랭킹 브리핑",
        fontsize=21,
        fontweight=900,
        color="#111827",
        ha="center",
    )
    fig.text(
        0.5,
        0.921,
        f"#{channel_name} · {period_label}",
        fontsize=11.2,
        color="#4B5563",
        ha="center",
    )
    fig.text(
        0.08,
        0.882,
        (f"총 메시지 {int(total_messages):,}개 · 참여 인원 {int(total_users):,}명 · "
         f"1위 {top_rows[0]['user_name']} ({counts[0]}회)"),
        fontsize=10.6,
        color="#374151",
    )

    top3 = " / ".join([f"{row['rank']}위 {row['user_name']}" for row in top_rows[:3]])
    fig.text(
        0.08,
        0.04,
        f"TOP3: {top3}",
        fontsize=9.8,
        color="#475569",
    )
    fig.text(
        0.96,
        0.04,
        f"생성 시각(KST): {generated_at.strftime('%Y-%m-%d %H:%M')}",
        fontsize=9.0,
        color="#64748B",
        ha="right",
    )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=170, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()
