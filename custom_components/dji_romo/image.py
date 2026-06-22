"""Live trajectory map image entity for DJI Romo."""

from __future__ import annotations

from base64 import b64encode
from datetime import datetime
from itertools import groupby
from math import atan2, cos, degrees, radians, sin
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import decode_grid_cells
from .coordinator import DjiRomoCoordinator, RomoSnapshot
from .entity import DjiRomoCoordinatorEntity
from .rooms import duplicate_label_ids, room_name
PARALLEL_UPDATES = 0

_ROBOT_MARKER_SIZE = 26
_ROBOT_IMAGE_HEADING_OFFSET = -90.0
_ROBOT_TOP_IMAGE = Path(__file__).parent / "robot_top.png"

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Romo map image entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [DjiRomoMapImage(coordinator), DjiRomoLastCleanImage(coordinator)]
    )


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
        self._robot_image_uri = _load_robot_image_data_uri()

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
        return _generate_map_svg(self.coordinator.data, self._robot_image_uri).encode("utf-8")


class DjiRomoLastCleanImage(DjiRomoCoordinatorEntity, ImageEntity):
    """Frozen "cleaning report" map for the last completed job.

    Renders the per-job ``room_map`` snapshot (rooms, occupancy grid, obstacles,
    carpets, no-go zones) plus that job's dense ``history_path`` sweep trace and
    robot/station positions — the same picture the DJI app shows in its report.
    """

    _attr_content_type = "image/svg+xml"

    def __init__(self, coordinator: DjiRomoCoordinator) -> None:
        ImageEntity.__init__(self, coordinator.hass)
        DjiRomoCoordinatorEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.device_sn}_last_clean_map"
        self._attr_translation_key = "last_clean_map"
        self._robot_image_uri = _load_robot_image_data_uri()

    @property
    def available(self) -> bool:
        """Available once a completed-cleaning report map has been fetched."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.last_clean_map is not None
        )

    @property
    def image_last_updated(self) -> datetime | None:
        """Tie freshness to the cloud refresh that fetched the report map."""
        data = self.coordinator.data
        return data.cloud_last_updated if data else None

    async def async_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the cleaning-report SVG as UTF-8 bytes."""
        data = self.coordinator.data
        if not data or not data.last_clean_map:
            return None
        return _generate_report_svg(
            data.last_clean_map, data.floor_plan_polys, self._robot_image_uri
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

    map_rotation = _map_alignment_rotation(polys)

    def rotate_map_point(px: float, py: float) -> tuple[float, float]:
        if not map_rotation:
            return px, py
        angle = radians(map_rotation)
        c, s = cos(angle), sin(angle)
        return px * c - py * s, px * s + py * c

    rotated_pts = [rotate_map_point(x, y) for x, y in all_pts]
    xs = [p[0] for p in rotated_pts]
    ys = [p[1] for p in rotated_pts]
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
        tx, ty = rotate_map_point(px, py)
        sx = draw_x + (tx - min_x) * scale
        sy = draw_y + (max_y - ty) * scale  # flip Y (map N = SVG up)
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

    # Occupancy grid (the scanned floor detail). Decoded with the block-offset
    # scheme (categories 1+; the category-0 wall layer is skipped — the room
    # polygons already outline the walls). Cells in the same row are merged into
    # horizontal runs so the SVG stays compact even with thousands of cells.
    if data.grid_map_data:
        info = data.grid_map_data.get("map_info", {})
        g_res = info.get("resolution", 0.05)
        g_ox = info.get("origin_x", 0.0)
        g_oy = info.get("origin_y", 0.0)
        pixel_size = max(scale * g_res, 0.5)

        cells = decode_grid_cells(data.grid_map_data)
        cells.sort(key=lambda c: (c[1], c[0]))
        runs: list[tuple[int, int, int]] = []  # (gx_start, gy, length)
        for gy, row in groupby(cells, key=lambda c: c[1]):
            xs = [c[0] for c in row]
            start = prev = xs[0]
            for x in xs[1:]:
                if x == prev + 1:
                    prev = x
                else:
                    runs.append((start, gy, prev - start + 1))
                    start = prev = x
            runs.append((start, gy, prev - start + 1))

        for gx, gy, length in runs:
            sx, sy = to_svg(g_ox + gx * g_res, g_oy + gy * g_res)
            rect_w = round(length * pixel_size + 0.5, 1)
            parts.append(
                f'<rect x="{sx}" y="{round(sy - pixel_size, 1)}" width="{rect_w}" '
                f'height="{round(pixel_size + 0.5, 1)}" fill="#3a9fd4" opacity="0.4"/>'
            )

    # Carpet zones (darker texture)
    for c in data.carpet_polys:
        verts = c.get("vertices", [])
        if len(verts) >= 3:
            svg_verts = [to_svg(v["x"], v["y"]) for v in verts]
            pts_str = " ".join(f"{x},{y}" for x, y in svg_verts)
            parts.append(
                f'<polygon points="{pts_str}" fill="#2e2e2e" stroke="#555" stroke-width="0.8" opacity="0.65"/>'
            )

    # Restricted zones (red hatched or semi-transparent red)
    for r in data.restricted_polys:
        verts = r.get("vertices", [])
        if len(verts) >= 3:
            svg_verts = [to_svg(v["x"], v["y"]) for v in verts]
            pts_str = " ".join(f"{x},{y}" for x, y in svg_verts)
            parts.append(
                f'<polygon points="{pts_str}" fill="#e74c3c" stroke="#c0392b" stroke-width="1.2" opacity="0.4"/>'
            )

    # Virtual walls (red lines)
    for vw in data.virtual_walls:
        verts = vw.get("vertices", [])
        if len(verts) == 2:
            x1, y1 = to_svg(verts[0]["x"], verts[0]["y"])
            x2, y2 = to_svg(verts[1]["x"], verts[1]["y"])
            parts.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="#e74c3c" stroke-width="2" stroke-linecap="round" opacity="0.8"/>'
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

    # Detected obstacles (blue circles). The user prefers "rond bleu".
    for ox, oy in data.obstacles:
        sx, sy = to_svg(ox, oy)
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="3.5" fill="#3498db" stroke="white" stroke-width="0.5" opacity="0.85"/>'
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


def _generate_report_svg(
    report_map: dict, polys: list[dict], robot_image_uri: str | None = None
) -> str:
    """Render a completed job's ``room_map`` snapshot as the cleaning-report SVG.

    Light theme matching the DJI app report: white rooms + labels, the occupancy
    grid as scan detail, carpets and no-go zones hatched, the dense ``history_path``
    sweep trace, detected obstacles, and the robot/station markers.
    """
    seg = report_map.get("seg_map", {}) or {}
    polys = [
        p for p in seg.get("poly_info", []) if len(p.get("border_vertices", [])) >= 3
    ]
    history = (report_map.get("history_path") or {}).get("history_path") or []

    pts: list[tuple[float, float]] = [
        (v["x"], v["y"]) for p in polys for v in p["border_vertices"]
    ]
    pts.extend((q[0], q[1]) for q in history)
    if not pts:
        return _EMPTY_SVG

    map_rotation = _map_alignment_rotation(polys)

    def rotate_map_point(px: float, py: float) -> tuple[float, float]:
        if not map_rotation:
            return px, py
        angle = radians(map_rotation)
        c, s = cos(angle), sin(angle)
        return px * c - py * s, px * s + py * c

    rotated_pts = [rotate_map_point(x, y) for x, y in pts]
    xs = [p[0] for p in rotated_pts]
    ys = [p[1] for p in rotated_pts]
    pad = 0.3
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    span_x = max(max_x - min_x, 0.5)
    span_y = max(max_y - min_y, 0.5)

    canvas_w = 288.0
    scale = canvas_w / span_x
    canvas_h = span_y * scale
    margin = 6.0
    svg_w = round(canvas_w + 2 * margin, 1)
    svg_h = round(canvas_h + 2 * margin, 1)

    def to_svg(px: float, py: float) -> tuple[float, float]:
        tx, ty = rotate_map_point(px, py)
        return (
            round(margin + (tx - min_x) * scale, 1),
            round(margin + (max_y - ty) * scale, 1),
        )

    parts: list[str] = [
        f'<svg viewBox="0 0 {svg_w} {svg_h}" xmlns="http://www.w3.org/2000/svg">',
        '<defs>'
        # Carpet: light-grey dotted texture matching the DJI app — a square dot
        # grid rotated 45° (two dots on the tile diagonal), so successive rows are
        # offset by half the horizontal period for the app's staggered "damier" look.
        '<pattern id="rc" width="5" height="5" patternUnits="userSpaceOnUse">'
        '<rect x="0.35" y="0.35" width="1.8" height="1.8" fill="#cfd0d0"/>'
        '<rect x="2.85" y="2.85" width="1.8" height="1.8" fill="#cfd0d0"/></pattern>'
        # No-go zone: thick diagonal salmon stripes (≈50/50 stripe/gap) matching the
        # DJI app. Base colours are pre-multiplied so the polygon's opacity="0.5"
        # composites to the app's measured colours over a white room (#f0c7c0 gap /
        # #f1a69a stripe) while staying translucent over carpet/grid underneath.
        '<pattern id="ng" width="7.5" height="7.5" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="7.5" height="7.5" fill="#e18f81"/>'
        '<rect width="3.8" height="7.5" fill="#e34d35"/></pattern>'
        '</defs>',
        f'<rect width="{svg_w}" height="{svg_h}" fill="#e8eaee" rx="6"/>',
    ]

    dup = duplicate_label_ids(polys)

    # Rooms. Each room carries two outlines: ``border_vertices`` (the simplified
    # nominal room) and ``vertices`` (the actual scanned floor, which carves out
    # furniture/obstacles standing against the walls). Filling the nominal room
    # grey first, then the accessible floor light on top, leaves the blocked spots
    # grey — reproducing the DJI app's "grey zones" inside/at the edges of rooms.
    for p in polys:
        sv = [to_svg(v["x"], v["y"]) for v in p["border_vertices"]]
        pstr = " ".join(f"{x},{y}" for x, y in sv)
        parts.append(f'<polygon points="{pstr}" fill="#d6d8db"/>')
    for p in polys:
        vv = p.get("vertices", [])
        if len(vv) >= 3:
            sv = [to_svg(v["x"], v["y"]) for v in vv]
            pstr = " ".join(f"{x},{y}" for x, y in sv)
            parts.append(f'<polygon points="{pstr}" fill="#f3f4f5"/>')
    for p in polys:
        sv = [to_svg(v["x"], v["y"]) for v in p["border_vertices"]]
        pstr = " ".join(f"{x},{y}" for x, y in sv)
        parts.append(
            f'<polygon points="{pstr}" fill="none" stroke="#96a0af" stroke-width="0.8"/>'
        )

    # Occupancy grid: category-0 walls (grey) under the scanned detail (1+, blue),
    # each merged into horizontal runs. Matches the validated report look.
    grid = report_map.get("grid_map")
    if grid:
        info = grid.get("map_info", {})
        g_res = info.get("resolution", 0.05)
        g_ox = info.get("origin_x", 0.0)
        g_oy = info.get("origin_y", 0.0)
        px_sz = max(scale * g_res, 0.4)

        def _emit_run(gx: int, gy: int, length: int, color: str, opacity: str) -> None:
            sx, sy = to_svg(g_ox + gx * g_res, g_oy + gy * g_res)
            parts.append(
                f'<rect x="{sx}" y="{round(sy - px_sz, 1)}" '
                f'width="{round(length * px_sz + 0.1, 1)}" '
                f'height="{round(px_sz + 0.1, 1)}" fill="{color}" opacity="{opacity}"/>'
            )

        def _draw_grid(cells: list[tuple[int, int]], color: str, opacity: str) -> None:
            cells.sort(key=lambda c: (c[1], c[0]))
            for gy, row in groupby(cells, key=lambda c: c[1]):
                xs2 = [c[0] for c in row]
                start = prev = xs2[0]
                for x in xs2[1:]:
                    if x == prev + 1:
                        prev = x
                    else:
                        _emit_run(start, gy, prev - start + 1, color, opacity)
                        start = prev = x
                _emit_run(start, gy, prev - start + 1, color, opacity)

        _draw_grid(decode_grid_cells(grid, categories=(0,)), "#5a6473", "0.95")
        _draw_grid(decode_grid_cells(grid), "#78a0d2", "0.5")

    # Carpets + no-go zones (hatched).
    for c in (report_map.get("carpet_layer") or {}).get("data", []):
        vs = c.get("vertices", [])
        if len(vs) >= 3:
            pstr = " ".join("{},{}".format(*to_svg(v["x"], v["y"])) for v in vs)
            parts.append(
                f'<polygon points="{pstr}" fill="url(#rc)" stroke="none"/>'
            )
    for r in (report_map.get("restricted_layer") or {}).get("data", []):
        vs = r.get("vertices", [])
        if len(vs) >= 3:
            pstr = " ".join("{},{}".format(*to_svg(v["x"], v["y"])) for v in vs)
            parts.append(
                f'<polygon points="{pstr}" fill="url(#ng)" stroke="none" opacity="0.5"/>'
            )
    for vw in (report_map.get("virtual_wall") or {}).get("data", []):
        vs = vw.get("vertices", [])
        if len(vs) == 2:
            x1, y1 = to_svg(vs[0]["x"], vs[0]["y"])
            x2, y2 = to_svg(vs[1]["x"], vs[1]["y"])
            parts.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="#dc3c32" stroke-width="2" stroke-linecap="round"/>'
            )

    # Sweep trace (history_path). Each point is [x, y, yaw, flag, type, width].
    # Only the actual *cleaning* passes are drawn — col4 `type` in CLEAN_PASS_TYPES:
    # 80 (main boustrophedon sweep), 48 (room-perimeter pass) and 112 (edge detail).
    # The other types are inter-room transit (32/128) or in-room navigation / obstacle
    # maneuvering (96/64) that the DJI app does NOT trace; those points — and any jump
    # > 0.6 m — break the polyline (so transit moves leave a gap, matching the app).
    CLEAN_PASS_TYPES = (48, 80, 112)
    segs: list[list[tuple[float, float]]] = []
    band: list[tuple[float, float]] = []
    prev_pt: tuple[float, float] | None = None
    for q in history:
        x, y = q[0], q[1]
        is_clean = len(q) > 4 and q[4] in CLEAN_PASS_TYPES
        if (
            is_clean
            and prev_pt is not None
            and ((x - prev_pt[0]) ** 2 + (y - prev_pt[1]) ** 2) ** 0.5 <= 0.6
        ):
            band.append(to_svg(x, y))
        else:
            if len(band) > 1:
                segs.append(band)
            band = [to_svg(x, y)] if is_clean else []
        prev_pt = (x, y) if is_clean else None
    if len(band) > 1:
        segs.append(band)
    for band in segs:
        pstr = " ".join(f"{x},{y}" for x, y in band)
        parts.append(
            f'<polyline points="{pstr}" fill="none" stroke="#2d78e1" stroke-width="0.5" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
        )

    # Obstacles (orange).
    for o in (report_map.get("obstacle_layer") or {}).get("data", []):
        vs = o.get("vertices", [])
        if vs:
            sx, sy = to_svg(vs[0]["x"], vs[0]["y"])
            parts.append(
                f'<circle cx="{sx}" cy="{sy}" r="3.6" fill="#ff9614" '
                f'stroke="#3c2800" stroke-width="0.5"/>'
            )

    # Station (orange ring) + robot (green dot).
    st = report_map.get("station_pos") or {}
    if st.get("station_position_x") is not None:
        sx, sy = to_svg(st["station_position_x"], st["station_position_y"])
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="4.2" fill="#e68c1e" stroke="white" stroke-width="1"/>'
        )
    rb = report_map.get("robot_pos") or {}
    if rb.get("crobot_position_x") is not None:
        sx, sy = to_svg(rb["crobot_position_x"], rb["crobot_position_y"])
        yaw = None
        if "crobot_direction" in rb:
            yaw = degrees(rb["crobot_direction"])
        parts.append(
            _robot_marker_svg(
                sx,
                sy,
                yaw,
                map_rotation,
                robot_image_uri,
            )
        )

    # Room labels.
    for p in polys:
        name = room_name(p, dup)
        if not name:
            continue
        sv = [to_svg(v["x"], v["y"]) for v in p["border_vertices"]]
        cx = round(sum(x for x, _ in sv) / len(sv), 1)
        cy = round(sum(y for _, y in sv) / len(sv), 1)
        parts.append(
            f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="7.5" fill="#282d37" font-family="sans-serif">{name[:14]}</text>'
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
