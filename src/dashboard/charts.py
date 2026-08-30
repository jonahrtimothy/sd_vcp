"""
Builds the Page 2 candlestick chart: OHLCV candles + shaded supply/demand
zones + VCP base region and trigger level. Kept independent of Streamlit
(returns a plain plotly Figure) so it's testable without running the app.
"""

from typing import List, Optional

import pandas as pd
import plotly.graph_objects as go

from style import ZONE_COLORS, ZONE_LINE_COLORS


def build_price_chart(
    df: pd.DataFrame,
    zones: List,
    vcp_setup: Optional[object] = None,
    title: str = "",
) -> go.Figure:
    """
    df: OHLCV with columns [date, open, high, low, close, volume], ascending.
    zones: list of zones.Zone (or anything with kind/zone_low/zone_high/
        start_idx/fresh attributes).
    vcp_setup: a vcp.VCPSetup, or None.
    """
    df = df.reset_index(drop=True)
    dates = pd.to_datetime(df["date"])

    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=dates, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="price",
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
    ))

    for z in zones:
        start_date = dates.iloc[z.start_idx] if z.start_idx < len(dates) else dates.iloc[0]
        fig.add_shape(
            type="rect",
            x0=start_date, x1=dates.iloc[-1],
            y0=z.zone_low, y1=z.zone_high,
            fillcolor=ZONE_COLORS.get(z.kind, "rgba(148,163,184,0.15)"),
            line=dict(
                color=ZONE_LINE_COLORS.get(z.kind, "rgba(148,163,184,0.5)"),
                width=1,
                dash="solid" if z.fresh else "dot",
            ),
            layer="below",
        )
        fig.add_annotation(
            x=start_date, y=z.zone_high,
            text=f"{z.kind} {'(fresh)' if z.fresh else f'(tested x{z.tests})'}",
            showarrow=False, yshift=10, font=dict(size=10, color="#94a3b8"),
            xanchor="left",
        )

    if vcp_setup is not None:
        base_start = dates.iloc[vcp_setup.base_start_idx] if vcp_setup.base_start_idx < len(dates) else dates.iloc[0]
        base_end = dates.iloc[vcp_setup.base_end_idx] if vcp_setup.base_end_idx < len(dates) else dates.iloc[-1]
        fig.add_shape(
            type="rect", x0=base_start, x1=base_end,
            y0=df["low"].iloc[vcp_setup.base_start_idx:vcp_setup.base_end_idx + 1].min(),
            y1=df["high"].iloc[vcp_setup.base_start_idx:vcp_setup.base_end_idx + 1].max(),
            fillcolor="rgba(245, 158, 11, 0.12)",
            line=dict(color="rgba(245, 158, 11, 0.6)", width=1),
            layer="below",
        )
        fig.add_hline(
            y=vcp_setup.trigger_level,
            line=dict(color="#f59e0b", width=1.5, dash="dash"),
            annotation_text=f"VCP trigger {vcp_setup.trigger_level:.2f}",
            annotation_position="top left",
            annotation_font=dict(color="#f59e0b", size=11),
        )
        for c in vcp_setup.contractions:
            c_start = dates.iloc[c.start_idx] if c.start_idx < len(dates) else dates.iloc[0]
            c_end = dates.iloc[c.end_idx] if c.end_idx < len(dates) else dates.iloc[-1]
            fig.add_trace(go.Scatter(
                x=[c_start, c_end],
                y=[df["high"].iloc[c.start_idx], df["low"].iloc[c.end_idx]],
                mode="markers",
                marker=dict(size=7, color="#f59e0b", symbol="diamond"),
                showlegend=False,
                hovertext=f"contraction depth={c.depth_pct}%",
                hoverinfo="text",
            ))

    fig.update_layout(
        title=title,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=40, b=10),
        height=520,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig
