"""Tests for safe SVG text rendering."""

from custom_components.dji_romo.rendering import svg_room_legend, svg_text


def test_svg_text_escapes_room_markup() -> None:
    """A room name cannot inject tags or break text content."""
    rendered = svg_text('Kids & <script>alert("x")</script>')

    assert "<script>" not in rendered
    assert rendered == "Kids &amp; &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"


def test_room_legend_handles_non_numeric_area() -> None:
    """Malformed cloud area data cannot break the complete map image."""
    assert svg_room_legend("A&B", None, active=False) == "• A&amp;B: 0 m²"
