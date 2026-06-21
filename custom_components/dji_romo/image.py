"""Live trajectory map image entity for DJI Romo."""

from __future__ import annotations

from base64 import b64encode
from datetime import datetime
from math import atan2, cos, degrees, radians, sin
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DjiRomoCoordinator, RomoSnapshot
from .entity import DjiRomoCoordinatorEntity
from .rooms import duplicate_label_ids, room_name

PARALLEL_UPDATES = 0
_ROBOT_TOP_IMAGE = Path(__file__).with_name("robot_top.png")
_ROBOT_MARKER_SIZE = 20
# The supplied top-view image points upward by default. Yaw is an angle from the
# map X axis, so this offset keeps the image aligned with the displayed heading.
_ROBOT_IMAGE_HEADING_OFFSET = 90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Romo map image entity."""
    coordinator = entry.runtime_data
    robot_image_uri = await hass.async_add_executor_job(_load_robot_image_data_uri)
    async_add_entities([DjiRomoMapImage(coordinator, robot_image_uri)])


class DjiRomoMapImage(DjiRomoCoordinatorEntity, ImageEntity):
    """SVG trajectory map showing robot path, robot position, and dock."""

    _attr_content_type = "image/svg+xml"

    def __init__(
        self,
        coordinator: DjiRomoCoordinator,
        robot_image_uri: str | None,
    ) -> None:
        # ImageEntity.__init__ wires up the HTTP client and the access-token
        # deque that state_attributes reads; it must run.
        ImageEntity.__init__(self, coordinator.hass)
        DjiRomoCoordinatorEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_map"
        self._attr_translation_key = "map"
        self._robot_image_uri = robot_image_uri

    @property
    def available(self) -> bool:
        """Keep the last-known map visible even while the robot is offline."""
        return self.coordinator.last_update_success

    @property
    def image_last_updated(self) -> datetime | None:
        """Tie image freshness to the most recent state or cloud refresh.

        Falls back to the REST refresh time so the entity has a state (and is
        not "unavailable") before the first MQTT osd message arrives.
        """
        data = self.coordinator.data
        stamps = [t for t in (data.last_updated, data.cloud_last_updated) if t]
        return max(stamps) if stamps else None

    async def async_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the SVG map as UTF-8 bytes."""
        return _generate_map_svg(
            self.coordinator.data,
            self._robot_image_uri,
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# SVG generation
# ---------------------------------------------------------------------------

_EMPTY_SVG = (
    '<svg viewBox="0 0 300 160" xmlns="http://www.w3.org/2000/svg">'
    '<rect width="300" height="160" fill="var(--card-background-color,#1c1c1e)" rx="6"/>'
    '<text x="150" y="84" text-anchor="middle" font-size="12" fill="#888" '
    'font-family="sans-serif">No position data yet</text>'
    '</svg>'
)


def _generate_map_svg(data: RomoSnapshot, robot_image_uri: str | None = None) -> str:
    """Build an SVG showing floor plan, trajectory, robot position, dock, and room list."""
    trajectory = data.trajectory
    robot_x, robot_y = data.robot_x, data.robot_y
    dock_x, dock_y = data.dock_x, data.dock_y
    polys = data.floor_plan_polys

    raw_pts: list[tuple[float, float]] = list(trajectory)
    if robot_x is not None and robot_y is not None:
        raw_pts.append((robot_x, robot_y))
    if dock_x is not None and dock_y is not None:
        raw_pts.append((dock_x, dock_y))
    for poly in polys:
        for v in poly.get("vertices", []):
            raw_pts.append((v["x"], v["y"]))

    if not raw_pts:
        return _EMPTY_SVG

    raw_xs = [p[0] for p in raw_pts]
    raw_ys = [p[1] for p in raw_pts]
    rotate_origin = (
        (min(raw_xs) + max(raw_xs)) / 2,
        (min(raw_ys) + max(raw_ys)) / 2,
    )
    map_rotation = _map_alignment_rotation(polys)

    def rotate_map_point(px: float, py: float) -> tuple[float, float]:
        if not map_rotation:
            return px, py
        ox, oy = rotate_origin
        angle = radians(map_rotation)
        dx = px - ox
        dy = py - oy
        return (
            ox + dx * cos(angle) - dy * sin(angle),
            oy + dx * sin(angle) + dy * cos(angle),
        )

    # Collect all known points to compute the bounding box.  The floor plan frames
    # the view; the sweep trace, robot and dock are included so nothing clips.
    all_pts = [rotate_map_point(x, y) for x, y in raw_pts]

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    # Roomier padding keeps the map readable in entity views and avoids clipping
    # the rendered robot image near edges.
    pad = 0.6
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    span_x = max(max_x - min_x, 0.5)
    span_y = max(max_y - min_y, 0.5)

    # Map canvas: 276 × 220 px inside 300 px wide SVG.
    canvas_w, canvas_h = 276, 220
    cx0, cy0 = 12.0, 8.0  # top-left corner of canvas in SVG space
    scale = min(canvas_w / span_x, canvas_h / span_y)
    # Centre the drawing inside the canvas.
    draw_x = cx0 + (canvas_w - span_x * scale) / 2
    draw_y = cy0 + (canvas_h - span_y * scale) / 2

    def to_svg(px: float, py: float) -> tuple[float, float]:
        tx, ty = rotate_map_point(px, py)
        sx = draw_x + (tx - min_x) * scale
        sy = draw_y + (max_y - ty) * scale  # flip Y (map N = SVG up)
        return round(sx, 1), round(sy, 1)

    # Dynamic height: fixed map block + legend rows.
    rooms = data.rooms
    n_rows = (len(rooms) + 1) // 2 if rooms else 0
    legend_h = 5 + n_rows * 14 if rooms else 0
    map_block_h = int(cy0 * 2) + canvas_h
    svg_h = map_block_h + legend_h

    parts: list[str] = [
        f'<svg viewBox="0 0 300 {svg_h}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="300" height="{svg_h}" '
        f'fill="var(--card-background-color,#1c1c1e)" rx="6"/>',
    ]

    # Floor plan: filled room polygons + outlines.
    if polys:
        room_polys = [p for p in polys if len(p.get("vertices", [])) >= 3]
        # Number duplicate room types ("Bathroom1"/"Bathroom2") like everywhere else.
        dup_labels = duplicate_label_ids(room_polys)
        for poly in room_polys:
            verts = poly.get("vertices", [])
            name = room_name(poly, dup_labels)
            is_active = bool(name) and name == data.current_room
            svg_verts = [to_svg(v["x"], v["y"]) for v in verts]
            pts_str = " ".join(f"{x},{y}" for x, y in svg_verts)
            fill = "#264666" if is_active else "#1e3a52"
            parts.append(
                f'<polygon points="{pts_str}" fill="{fill}" '
                f'stroke="#3a5f80" stroke-width="0.8" opacity="0.85"/>'
            )
            # Room label centroid
            if name:
                cx = round(sum(v[0] for v in svg_verts) / len(svg_verts), 1)
                cy = round(sum(v[1] for v in svg_verts) / len(svg_verts), 1)
                label_color = "#5bc8f5" if is_active else "#7ab5d0"
                parts.append(
                    f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
                    f'font-size="6.5" fill="{label_color}" font-family="sans-serif">'
                    f'{name[:12]}</text>'
                )

    # Cleaning trace: the path the robot swept this session, accumulated from its
    # position only while actually sweeping (and persisted across restarts). Drawn
    # as a wide translucent band (one robot width) plus a brighter centre line, so
    # the back-and-forth rows merge into the filled look the DJI app shows.
    if len(trajectory) > 1:
        svg_pts = [to_svg(x, y) for x, y in trajectory]
        pts_str = " ".join(f"{x},{y}" for x, y in svg_pts)
        band_w = max(4.0, round(0.33 * scale, 1))  # ~33 cm cleaning width
        parts.append(
            f'<polyline points="{pts_str}" fill="none" stroke="#3a9fd4" '
            f'stroke-width="{band_w}" stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="0.45"/>'
        )
        parts.append(
            f'<polyline points="{pts_str}" fill="none" stroke="#5b8def" '
            f'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="0.8"/>'
        )
    elif trajectory:
        sx, sy = to_svg(*trajectory[0])
        parts.append(f'<circle cx="{sx}" cy="{sy}" r="3" fill="#5b8def" opacity="0.85"/>')

    # Dock marker (orange triangle pointing up = "home").
    if dock_x is not None and dock_y is not None:
        sx, sy = to_svg(dock_x, dock_y)
        parts.append(
            f'<polygon points="{sx},{sy - 8} {sx - 6},{sy + 4} {sx + 6},{sy + 4}" '
            f'fill="#e67e22" opacity="0.9"/>'
            f'<rect x="{sx - 3}" y="{sy}" width="6" height="4" fill="#e67e22" opacity="0.9"/>'
        )

    # Detected obstacles (furniture legs, toys) — small warning diamonds.
    for ox, oy in data.obstacles:
        sx, sy = to_svg(ox, oy)
        parts.append(
            f'<polygon points="{sx},{sy - 4} {sx + 4},{sy} {sx},{sy + 4} {sx - 4},{sy}" '
            f'fill="#e74c3c" opacity="0.75"/>'
        )

    # Robot marker: compact top view of the white Romo body with its dark front
    # sensor. DJI yaw is an angle from the map X axis, so rotate the marker in
    # SVG space instead of treating it as a compass heading.
    if robot_x is not None and robot_y is not None:
        sx, sy = to_svg(robot_x, robot_y)
        parts.append(
            _robot_marker_svg(
                sx,
                sy,
                data.robot_yaw,
                map_rotation,
                robot_image_uri,
            )
        )

    # Scale bar (1 metre).
    bar_px = round(scale)
    bar_y = map_block_h - 10
    parts.append(
        f'<line x1="15" y1="{bar_y}" x2="{15 + bar_px}" y2="{bar_y}" '
        f'stroke="#888" stroke-width="1.5" stroke-linecap="round"/>'
        f'<text x="{15 + bar_px // 2}" y="{bar_y - 4}" text-anchor="middle" '
        f'fill="#888" font-size="8" font-family="sans-serif">1 m</text>'
    )

    # Room legend (2 columns).
    if rooms:
        sep_y = map_block_h
        parts.append(
            f'<line x1="10" y1="{sep_y}" x2="290" y2="{sep_y}" '
            f'stroke="#3a3a3a" stroke-width="1"/>'
        )
        for i, room in enumerate(rooms):
            col = i % 2
            row = i // 2
            name = room.get("name", f"Room {room.get('poly_index', '')}")
            area = room.get("area", 0)
            active = name == data.current_room
            fill = "#3498db" if active else "#999"
            prefix = "▶ " if active else "• "
            rx_text = 15 + col * 148
            ry_text = sep_y + 14 + row * 14
            parts.append(
                f'<text x="{rx_text}" y="{ry_text}" fill="{fill}" '
                f'font-size="9" font-family="sans-serif">'
                f'{prefix}{name}: {area:.0f} m²</text>'
            )

    parts.append("</svg>")
    return "\n".join(parts)


def _map_alignment_rotation(polys: list[dict]) -> float:
    """Return a small rotation that straightens the dominant room walls."""
    weighted_sum = 0.0
    total_length = 0.0
    for poly in polys:
        vertices = poly.get("vertices", [])
        if len(vertices) < 2:
            continue
        points = [(float(v["x"]), float(v["y"])) for v in vertices]
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
            dx = x2 - x1
            dy = y2 - y1
            length = (dx * dx + dy * dy) ** 0.5
            if length < 0.4:
                continue
            svg_angle = degrees(atan2(-dy, dx))
            residual = ((svg_angle + 45) % 90) - 45
            weighted_sum += length * residual
            total_length += length

    if not total_length:
        return 0.0

    rotation = weighted_sum / total_length
    # Only correct small systematic map skew. Larger angles may be real floorplan
    # orientation and should not be forced into a different layout.
    return round(rotation, 2) if abs(rotation) <= 15 else 0.0


def _robot_marker_svg(
    sx: float,
    sy: float,
    yaw: float | None,
    map_rotation: float = 0.0,
    image_uri: str | None = None,
) -> str:
    """Return the cropped Romo top-view marker centred on the robot position."""
    if image_uri is None:
        return _robot_marker_fallback_svg(sx, sy, yaw, map_rotation)

    rotation = (
        0
        if yaw is None
        else round(_ROBOT_IMAGE_HEADING_OFFSET - yaw - map_rotation, 1)
    )
    size = _ROBOT_MARKER_SIZE
    x = round(sx - size / 2, 1)
    y = round(sy - size / 2, 1)
    return (
        f'<g transform="rotate({rotation} {sx} {sy})">'
        f'<ellipse cx="{sx}" cy="{round(sy + 1.5, 1)}" rx="9.5" ry="7" '
        f'fill="#0f1720" opacity="0.22"/>'
        f'<image href="{image_uri}" x="{x}" y="{y}" width="{size}" height="{size}" '
        f'preserveAspectRatio="xMidYMid meet"/>'
        f'</g>'
    )


def _load_robot_image_data_uri() -> str | None:
    """Load the cropped marker image as an SVG data URI."""
    try:
        image = _ROBOT_TOP_IMAGE.read_bytes()
    except OSError:
        return None
    return f"data:image/png;base64,{b64encode(image).decode('ascii')}"


def _robot_marker_fallback_svg(
    sx: float,
    sy: float,
    yaw: float | None,
    map_rotation: float = 0.0,
) -> str:
    """Return a compact vector marker if the PNG asset is unavailable."""
    rotation = 0 if yaw is None else round(-yaw - map_rotation, 1)
    x = round(sx - 10, 1)
    y = round(sy - 8, 1)
    return (
        f'<g transform="rotate({rotation} {sx} {sy})">'
        f'<ellipse cx="{sx}" cy="{sy}" rx="12" ry="9.5" fill="#0f1720" opacity="0.22"/>'
        f'<rect x="{x}" y="{y}" width="20" height="16" rx="5" '
        f'fill="#f4f8f9" stroke="white" stroke-width="1.2"/>'
        f'<rect x="{round(sx + 3.6, 1)}" y="{round(sy - 4.2, 1)}" '
        f'width="5.8" height="8.4" rx="1.8" fill="#202a33"/>'
        f'<rect x="{round(sx - 5.2, 1)}" y="{round(sy - 5.3, 1)}" '
        f'width="7.2" height="10.6" rx="2.6" fill="#dfe7ea"/>'
        f'<circle cx="{round(sx - 1.6, 1)}" cy="{sy}" r="2.3" '
        f'fill="#eef4f5" stroke="#cfd9dd" stroke-width="0.7"/>'
        f'<line x1="{round(sx - 7, 1)}" y1="{round(sy + 5.1, 1)}" '
        f'x2="{round(sx + 1.5, 1)}" y2="{round(sy + 5.1, 1)}" '
        f'stroke="#c5d0d5" stroke-width="0.9" stroke-linecap="round"/>'
        f'<circle cx="{round(sx + 6.2, 1)}" cy="{round(sy - 1.8, 1)}" '
        f'r="1" fill="#5b6b75"/>'
        f'</g>'
    )
