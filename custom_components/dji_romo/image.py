"""Live trajectory map image entity for DJI Romo."""

from __future__ import annotations

from datetime import datetime
from math import cos, radians, sin

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DjiRomoCoordinator, RomoSnapshot
from .entity import DjiRomoCoordinatorEntity
from .rooms import duplicate_label_ids, room_name

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Romo map image entity."""
    coordinator = entry.runtime_data
    async_add_entities([DjiRomoMapImage(coordinator)])


class DjiRomoMapImage(DjiRomoCoordinatorEntity, ImageEntity):
    """SVG trajectory map showing robot path, robot position, and dock."""

    _attr_content_type = "image/svg+xml"

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        # ImageEntity.__init__ wires up the HTTP client and the access-token
        # deque that state_attributes reads; it must run.
        ImageEntity.__init__(self, coordinator.hass)
        DjiRomoCoordinatorEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_map"
        self._attr_translation_key = "map"

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
        return _generate_map_svg(self.coordinator.data).encode("utf-8")


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


def _generate_map_svg(data: RomoSnapshot) -> str:
    """Build an SVG showing floor plan, trajectory, robot position, dock, and room list."""
    trajectory = data.trajectory
    robot_x, robot_y = data.robot_x, data.robot_y
    dock_x, dock_y = data.dock_x, data.dock_y
    polys = data.floor_plan_polys

    # Collect all known points to compute the bounding box.  The floor plan frames
    # the view; the sweep trace, robot and dock are included so nothing clips.
    all_pts: list[tuple[float, float]] = list(trajectory)
    if robot_x is not None and robot_y is not None:
        all_pts.append((robot_x, robot_y))
    if dock_x is not None and dock_y is not None:
        all_pts.append((dock_x, dock_y))
    for poly in polys:
        for v in poly.get("vertices", []):
            all_pts.append((v["x"], v["y"]))

    if not all_pts:
        return _EMPTY_SVG

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    # 30 cm padding around the data extent so markers near the edge stay visible.
    pad = 0.3
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    span_x = max(max_x - min_x, 0.5)
    span_y = max(max_y - min_y, 0.5)

    # Map canvas: 276 × 240 px inside 300 px wide SVG.
    canvas_w, canvas_h = 276, 240
    cx0, cy0 = 12.0, 8.0  # top-left corner of canvas in SVG space
    scale = min(canvas_w / span_x, canvas_h / span_y)
    # Centre the drawing inside the canvas.
    draw_x = cx0 + (canvas_w - span_x * scale) / 2
    draw_y = cy0 + (canvas_h - span_y * scale) / 2

    def to_svg(px: float, py: float) -> tuple[float, float]:
        sx = draw_x + (px - min_x) * scale
        sy = draw_y + (max_y - py) * scale  # flip Y (map N = SVG up)
        return round(sx, 1), round(sy, 1)

    # Dynamic height: fixed map block + legend rows.
    rooms = data.rooms
    n_rows = (len(rooms) + 1) // 2 if rooms else 0
    legend_h = 5 + n_rows * 14 if rooms else 0
    map_block_h = int(cy0 * 2) + canvas_h  # 256
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

    # Robot marker: white-outlined dot with heading arrow so it stands out on
    # both dark (floor) and light (room label) backgrounds.
    if robot_x is not None and robot_y is not None:
        sx, sy = to_svg(robot_x, robot_y)
        # White outline ring.
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="8" fill="white" opacity="0.35"/>'
        )
        # Solid blue dot.
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="6" fill="#3498db" opacity="0.98"/>'
        )
        if data.robot_yaw is not None:
            angle = radians(data.robot_yaw)
            ex = round(sx + 14 * sin(angle), 1)
            ey = round(sy - 14 * cos(angle), 1)  # SVG Y is inverted
            parts.append(
                f'<line x1="{sx}" y1="{sy}" x2="{ex}" y2="{ey}" '
                f'stroke="white" stroke-width="3" stroke-linecap="round" opacity="0.9"/>'
            )
            parts.append(
                f'<line x1="{sx}" y1="{sy}" x2="{ex}" y2="{ey}" '
                f'stroke="#3498db" stroke-width="2" stroke-linecap="round"/>'
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
