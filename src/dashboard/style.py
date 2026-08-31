"""Shared color palette + small HTML-badge helpers for the dashboard.
Kept separate from app.py so the color logic isn't tangled through every
page function."""

CONVICTION_COLORS = {
    "High": "#22c55e",     # green
    "Medium": "#f59e0b",   # amber
    "Low": "#ef4444",      # red
}

DIRECTION_COLORS = {
    "bullish": "#22c55e",
    "bearish": "#ef4444",
}

STAGE_COLORS = {
    "Stage 1": "#94a3b8",   # slate (basing)
    "Stage 2": "#22c55e",   # green (advancing)
    "Stage 3": "#f59e0b",   # amber (topping)
    "Stage 4": "#ef4444",   # red (declining)
    "insufficient_data": "#64748b",
}

TRIGGER_COLORS = {
    "triggered": "#22c55e",   # green -- price has actually confirmed (close + volume)
    "forming": "#94a3b8",     # slate -- base still building, not yet confirmed
    "failed": "#ef4444",      # red -- pattern invalidated, exit or stand aside
}

ZONE_COLORS = {
    "demand": "rgba(34, 197, 94, 0.18)",
    "supply": "rgba(239, 68, 68, 0.18)",
}
ZONE_LINE_COLORS = {
    "demand": "rgba(34, 197, 94, 0.55)",
    "supply": "rgba(239, 68, 68, 0.55)",
}


def badge(text: str, color: str) -> str:
    return (
        f'<span style="background-color:{color}22; color:{color}; '
        f'border:1px solid {color}; padding:2px 10px; border-radius:12px; '
        f'font-weight:600; font-size:0.85em; white-space:nowrap;">{text}</span>'
    )


def conviction_badge(conviction: str) -> str:
    return badge(conviction, CONVICTION_COLORS.get(conviction, "#94a3b8"))


def direction_badge(direction: str) -> str:
    arrow = "▲" if direction == "bullish" else "▼"
    return badge(f"{arrow} {direction}", DIRECTION_COLORS.get(direction, "#94a3b8"))


def stage_badge(stage: str) -> str:
    return badge(stage, STAGE_COLORS.get(stage, "#94a3b8"))


def trigger_badge(status: str) -> str:
    label = {"triggered": "TRIGGERED", "failed": "FAILED"}.get(status, "forming")
    return badge(label, TRIGGER_COLORS.get(status, "#94a3b8"))
