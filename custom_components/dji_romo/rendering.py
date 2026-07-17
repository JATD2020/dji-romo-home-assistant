"""Safe text formatting for DJI Romo map SVGs."""

from __future__ import annotations

from html import escape
from typing import Any


def svg_text(value: Any, max_chars: int | None = None) -> str:
    """Return escaped text that cannot add SVG markup."""
    text = str(value)
    if max_chars is not None:
        text = text[:max_chars]
    return escape(text)


def svg_room_legend(value: Any, area: Any, *, active: bool) -> str:
    """Return an escaped room legend label with a defensive numeric area."""
    try:
        numeric_area = float(area)
    except (TypeError, ValueError):
        numeric_area = 0.0
    prefix = "▶ " if active else "• "
    return f"{prefix}{svg_text(value)}: {numeric_area:.0f} m²"
