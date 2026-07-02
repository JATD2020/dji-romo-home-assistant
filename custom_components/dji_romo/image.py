"""Live trajectory map image entity for DJI Romo."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby
from math import atan2, cos, degrees, radians, sin
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import decode_grid_cells
from .const import CLEAN_PASS_TYPES
from .coordinator import DjiRomoCoordinator, RomoSnapshot
from .entity import DjiRomoCoordinatorEntity
from .rooms import duplicate_label_ids, room_name

PARALLEL_UPDATES = 0

_ROBOT_MARKER_SIZE = 15
# Apparent diameter (metres) of the robot's live-position marker. Sized to the map
# scale like the trace/obstacles rather than as a fixed pixel blob. Deliberately a bit
# larger than the robot's true 33 cm footprint so the position icon stays legible (a
# 33 cm icon is too small to read), while still tracking the map zoom. Floored at
# _ROBOT_MARKER_MIN_PX only for extreme zoom-out.
_ROBOT_DIAM_M = 0.45
_ROBOT_MARKER_MIN_PX = 4
# Diameter (metres) of the circular drop-shadow under the robot, ~1.2x the marker so
# it reads as a soft halo just beyond the icon's edge. Drawn inside the marker's scaled
# group, so it tracks the map zoom and stays this physical size.
_ROBOT_SHADOW_DIAM_M = 0.54
# Height (metres) of the charging-station marker's taller dimension, kept slightly
# larger than the robot icon so the dock reads as a touch bigger. The nominal marker
# art is 10 wide x 12 tall; it's scaled to the map zoom like the robot, floored at
# _DOCK_MARKER_MIN_PX so it stays visible when zoomed out.
_DOCK_HEIGHT_M = 0.50
_DOCK_MARKER_MIN_PX = 5
_ROBOT_IMAGE_HEADING_OFFSET = 90.0
_ROBOT_TOP_IMAGE = Path(__file__).parent / "robot_top.png"
# Approx. floor width the robot sweeps in one pass (metres). Drawn as the light-blue
# "cleaned area" halo behind the dark centre-line trace, matching the DJI app where
# the swath = the robot's width and the dark line = the robot's centre path.
_CLEAN_SWATH_WIDTH_M = 0.33
# Width of the dark centre-line trace (metres of floor). Like the halo it scales
# with the map zoom, so the trace keeps the same look whatever the map size.
_CENTRE_LINE_WIDTH_M = 0.015
# Physical diameter (metres) used to draw a detected obstacle, matching the robot's
# own footprint (~33 cm). The circle scales with the map zoom like the trace, so an
# obstacle keeps a real-world size instead of a fixed pixel blob; floored at 2 px so
# it stays visible on small maps.
_OBSTACLE_DIAM_M = 0.33


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Romo map image entities."""
    coordinator = entry.runtime_data
    robot_image_uri = await hass.async_add_executor_job(_load_robot_image_data_uri)
    async_add_entities([DjiRomoMapImage(coordinator, robot_image_uri)])


class DjiRomoMapImage(DjiRomoCoordinatorEntity, ImageEntity):
    """SVG trajectory map showing robot path, robot position, and dock.

    Dynamically switches between the live map (during cleaning) and the
    historical last cleaning report (when idle/docked).
    """

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
        # Rendered-SVG cache. Generation decodes the whole occupancy grid and
        # rebuilds the trace polylines, so repeated frontend fetches of the same
        # snapshot must not re-render.
        self._svg_cache: tuple[tuple[Any, ...], bytes] | None = None

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
        if not data:
            return None

        is_active = data.activity in {"cleaning", "paused", "returning", "error"}

        # mission_bid from MQTT is instantly updated. last_job from REST lags.
        last_job_uuid = data.last_job.get("uuid") if data.last_job else None
        latest_known_uuid = data.mission_bid or last_job_uuid

        # When docked/idle, show the end time of the last cleaning job, but ONLY
        # if the REST API has actually fetched this newly finished job.
        if not is_active and data.last_job and last_job_uuid == latest_known_uuid:
            end_time = data.last_job.get("end_time")
            if end_time and isinstance(end_time, (int, float)) and end_time > 0:
                dt = datetime.fromtimestamp(end_time, tz=UTC)

                # If the final report map is available, append a microsecond to bust
                # the browser cache and load the new SVG without changing the UI display time.
                has_latest_report = bool(
                    data.last_clean_map_uuid and latest_known_uuid and data.last_clean_map_uuid == latest_known_uuid
                )
                if has_latest_report:
                    dt += timedelta(microseconds=1)

                return dt

        stamps = [t for t in (data.last_updated, data.cloud_last_updated) if t]
        return max(stamps) if stamps else None

    async def async_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the SVG map as UTF-8 bytes (cached, rendered off the loop)."""
        data = self.coordinator.data
        if not data:
            return None

        is_active = data.activity in {"cleaning", "paused", "returning", "error"}

        last_job_uuid = data.last_job.get("uuid") if data.last_job else None
        latest_known_uuid = data.mission_bid or last_job_uuid

        has_latest_report = bool(
            data.last_clean_map_uuid and latest_known_uuid and data.last_clean_map_uuid == latest_known_uuid
        )
        use_report = bool(not is_active and data.last_clean_map and has_latest_report)

        # The snapshot is replaced (never mutated) on every update, so these
        # fields identify the rendered picture; the trajectory length covers
        # /paths appends that don't bump last_updated.
        cache_key = (
            use_report,
            data.last_updated,
            data.cloud_last_updated,
            len(data.trajectory),
            data.last_clean_map_uuid,
        )
        if self._svg_cache is not None and self._svg_cache[0] == cache_key:
            return self._svg_cache[1]

        generator = _generate_report_svg if use_report else _generate_map_svg
        svg = await self.hass.async_add_executor_job(
            generator, data, self._robot_image_uri
        )
        image = svg.encode("utf-8")
        self._svg_cache = (cache_key, image)
        return image


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

type _ToSvg = Callable[[float, float], tuple[float, float]]


@dataclass(slots=True)
class _Projection:
    """World->SVG mapping and canvas geometry shared by both map styles."""

    to_svg: _ToSvg
    scale: float
    svg_w: float
    map_block_h: float
    svg_h: float
    map_rotation: float


def _project(
    pts: list[tuple[float, float]],
    polys: list[dict[str, Any]],
    rooms: list[dict[str, Any]],
) -> _Projection | None:
    """Fit the world points into the fixed canvas, shared by both map styles.

    Applies the dominant-wall alignment rotation, pads the bounding box of
    ``pts`` and centres it on the canvas, and reserves rows under the map for
    the room legend. Returns None when there is nothing to draw.
    """
    if not pts:
        return None

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

    canvas_w, canvas_h = 276.0, 220.0
    margin = 12.0
    scale = min(canvas_w / span_x, canvas_h / span_y)
    svg_w = round(canvas_w + 2 * margin, 1)
    map_block_h = round(canvas_h + 2 * margin, 1)
    draw_x = margin + (canvas_w - span_x * scale) / 2
    draw_y = margin + (canvas_h - span_y * scale) / 2

    # Dynamic height: map block + legend rows.
    n_rows = (len(rooms) + 1) // 2 if rooms else 0
    legend_h = 5 + n_rows * 14 if rooms else 0
    svg_h = round(map_block_h + legend_h, 1)

    def to_svg(px: float, py: float) -> tuple[float, float]:
        tx, ty = rotate_map_point(px, py)
        return (
            round(draw_x + (tx - min_x) * scale, 1),
            round(draw_y + (max_y - ty) * scale, 1),
        )

    return _Projection(to_svg, scale, svg_w, map_block_h, svg_h, map_rotation)


def _svg_prelude(proj: _Projection) -> list[str]:
    """Opening tag, the carpet/no-go patterns and the page background."""
    return [
        f'<svg viewBox="0 0 {proj.svg_w} {proj.svg_h}" xmlns="http://www.w3.org/2000/svg">',
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
        f'<rect width="{proj.svg_w}" height="{proj.svg_h}" fill="#e8eaee" rx="6"/>',
    ]


def _render_room_fills(
    parts: list[str],
    polys: list[dict[str, Any]],
    to_svg: _ToSvg,
) -> None:
    """The two room fill passes shared by both map styles.

    Each room carries two outlines: ``border_vertices`` (the simplified nominal
    room) and ``vertices`` (the actual scanned floor, which carves out
    furniture/obstacles standing against the walls). Filling the nominal room
    grey first, then the accessible floor light on top, leaves the blocked spots
    grey — reproducing the DJI app's "grey zones" inside/at the edges of rooms.
    """
    for poly in polys:
        verts = poly.get("border_vertices", poly.get("vertices", []))
        pts_str = " ".join(
            f"{x},{y}" for x, y in (to_svg(v["x"], v["y"]) for v in verts)
        )
        parts.append(f'<polygon points="{pts_str}" fill="#d6d8db"/>')
    for poly in polys:
        verts = poly.get("vertices", [])
        if len(verts) >= 3:
            pts_str = " ".join(
                f"{x},{y}" for x, y in (to_svg(v["x"], v["y"]) for v in verts)
            )
            parts.append(f'<polygon points="{pts_str}" fill="#f3f4f5"/>')


def _render_grid(
    parts: list[str],
    grid: dict[str, Any],
    polys: list[dict[str, Any]],
    to_svg: _ToSvg,
    scale: float,
    clip_id: str,
) -> None:
    """Draw the occupancy grid (the scanned floor detail), clipped to the rooms.

    Cells are merged into horizontal runs to keep the SVG small. Category 0 (the
    SLAM wall layer) is skipped by decode_grid_cells' default; pass
    ``categories=(0,)`` there to draw the walls instead.
    """
    clip_polys = []
    for p in polys:
        vs = p.get("border_vertices") or []
        if vs:
            pts = " ".join(
                f"{x},{y}" for x, y in (to_svg(v["x"], v["y"]) for v in vs)
            )
            clip_polys.append(f'<polygon points="{pts}"/>')

    if clip_polys:
        parts.append(f'<defs><clipPath id="{clip_id}">')
        parts.extend(clip_polys)
        parts.append('</clipPath></defs>')
        parts.append(f'<g clip-path="url(#{clip_id})">')

    info = grid.get("map_info", {})
    g_res = info.get("resolution", 0.05)
    g_ox = info.get("origin_x", 0.0)
    g_oy = info.get("origin_y", 0.0)
    px_sz = max(scale * g_res, 0.4)

    def _emit_run(gx: int, gy: int, length: int) -> None:
        sx, sy = to_svg(g_ox + gx * g_res, g_oy + gy * g_res)
        parts.append(
            f'<rect x="{sx}" y="{round(sy - px_sz, 1)}" '
            f'width="{round(length * px_sz + 0.1, 1)}" '
            f'height="{round(px_sz + 0.1, 1)}" fill="#78a0d2" opacity="0.4"/>'
        )

    cells = decode_grid_cells(grid)
    cells.sort(key=lambda c: (c[1], c[0]))
    for gy, row in groupby(cells, key=lambda c: c[1]):
        xs = [c[0] for c in row]
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
            else:
                _emit_run(start, gy, prev - start + 1)
                start = prev = x
        _emit_run(start, gy, prev - start + 1)

    if clip_polys:
        parts.append('</g>')


def _render_zones(
    parts: list[str],
    carpets: list[dict[str, Any]],
    restricted: list[dict[str, Any]],
    walls: list[dict[str, Any]],
    to_svg: _ToSvg,
) -> None:
    """Carpet (dotted), no-go (hatched) and virtual-wall (red line) overlays."""
    for c in carpets:
        verts = c.get("vertices", [])
        if len(verts) >= 3:
            pts_str = " ".join(
                f"{x},{y}" for x, y in (to_svg(v["x"], v["y"]) for v in verts)
            )
            parts.append(
                f'<polygon points="{pts_str}" fill="url(#rc)" stroke="none"/>'
            )
    for r in restricted:
        verts = r.get("vertices", [])
        if len(verts) >= 3:
            pts_str = " ".join(
                f"{x},{y}" for x, y in (to_svg(v["x"], v["y"]) for v in verts)
            )
            parts.append(
                f'<polygon points="{pts_str}" fill="url(#ng)" stroke="none" opacity="0.5"/>'
            )
    for vw in walls:
        verts = vw.get("vertices", [])
        if len(verts) == 2:
            x1, y1 = to_svg(verts[0]["x"], verts[0]["y"])
            x2, y2 = to_svg(verts[1]["x"], verts[1]["y"])
            parts.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="#dc3c32" stroke-width="2" stroke-linecap="round"/>'
            )


def _render_obstacles(
    parts: list[str],
    points: list[tuple[float, float]],
    to_svg: _ToSvg,
    scale: float,
) -> None:
    """Detected obstacles as orange circles sized like the robot footprint."""
    obstacle_r = round(max(_OBSTACLE_DIAM_M / 2 * scale, 2.0), 1)
    for ox, oy in points:
        sx, sy = to_svg(ox, oy)
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="{obstacle_r}" fill="#ff9614" '
            f'stroke="#3c2800" stroke-width="0.5"/>'
        )


def _render_legend(
    parts: list[str],
    rooms: list[dict[str, Any]],
    current_room: str | None,
    svg_w: float,
    map_block_h: float,
) -> None:
    """Two-column room list under the map, highlighting the active room."""
    if not rooms:
        return
    sep_y = map_block_h
    parts.append(
        f'<line x1="10" y1="{sep_y}" x2="{svg_w - 10}" y2="{sep_y}" '
        f'stroke="#c0c0c0" stroke-width="1"/>'
    )
    col_width = svg_w / 2
    for i, room in enumerate(rooms):
        col = i % 2
        row = i // 2
        name = room.get("name", f"Room {room.get('poly_index', '')}")
        area = room.get("area", 0)
        active = name == current_room
        fill = "#3498db" if active else "#282d37"
        prefix = "▶ " if active else "• "
        rx_text = round(15 + col * col_width, 1)
        ry_text = sep_y + 14 + row * 14
        parts.append(
            # \u202f: narrow no-break space between the value and its unit.
            f'<text x="{rx_text}" y="{ry_text}" fill="{fill}" '
            f'font-size="9" font-family="sans-serif">'
            f'{prefix}{escape(name)}: {area:.0f}\u202fm²</text>'
        )


def _band_trajectory(
    trajectory: list[tuple[float, float]],
    to_svg,
) -> list[list[tuple[float, float]]]:
    """Split the live (x, y) trajectory into svg-point bands.

    A jump > 0.6 m breaks the polyline (transit moves leave a gap, matching the
    app). Returned bands have >= 2 points; single-point runs are dropped.
    """
    segs: list[list[tuple[float, float]]] = []
    band: list[tuple[float, float]] = []
    prev_pt: tuple[float, float] | None = None
    for x, y in trajectory:
        if prev_pt is not None and ((x - prev_pt[0]) ** 2 + (y - prev_pt[1]) ** 2) ** 0.5 <= 0.6:
            band.append(to_svg(x, y))
        else:
            if len(band) > 1:
                segs.append(band)
            band = [to_svg(x, y)]
        prev_pt = (x, y)
    if len(band) > 1:
        segs.append(band)
    return segs


def _emit_trace(
    parts: list[str],
    segs: list[list[tuple[float, float]]],
    halo_width: float,
    line_width: float,
) -> None:
    """Draw the cleaning trace the way the DJI app does.

    A wide light-blue halo (``halo_width`` ≈ the robot's width at the current map
    scale) represents the floor swept under the robot, with a thin dark-blue
    centre-line (``line_width``) on top for the robot's path. Both scale with the
    map zoom. All halos are drawn first, then all lines, so a centre-line is never
    covered by a neighbouring band's halo.
    """
    for band in segs:
        pstr = " ".join(f"{x},{y}" for x, y in band)
        parts.append(
            f'<polyline points="{pstr}" fill="none" stroke="#aacef4" stroke-width="{halo_width}" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="0.5"/>'
        )
    for band in segs:
        pstr = " ".join(f"{x},{y}" for x, y in band)
        parts.append(
            f'<polyline points="{pstr}" fill="none" stroke="#1a5fc4" stroke-width="{line_width}" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="1"/>'
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
        for v in poly.get("border_vertices", poly.get("vertices", [])):
            all_pts.append((v["x"], v["y"]))

    proj = _project(all_pts, polys, data.rooms)
    if proj is None:
        return _EMPTY_SVG
    to_svg, scale = proj.to_svg, proj.scale

    parts = _svg_prelude(proj)

    # Floor plan: the shared grey/white fills, then outline + label per room
    # (the room currently being cleaned is highlighted).
    if polys:
        room_polys = [p for p in polys if len(p.get("border_vertices", p.get("vertices", []))) >= 3]
        dup_labels = duplicate_label_ids(room_polys)
        _render_room_fills(parts, room_polys, to_svg)
        for poly in room_polys:
            verts = poly.get("border_vertices", poly.get("vertices", []))
            name = room_name(poly, dup_labels)
            is_active = bool(name) and name == data.current_room
            svg_verts = [to_svg(v["x"], v["y"]) for v in verts]
            pts_str = " ".join(f"{x},{y}" for x, y in svg_verts)
            parts.append(
                f'<polygon points="{pts_str}" fill="none" stroke="#96a0af" stroke-width="0.8"/>'
            )
            if name:
                cx, cy = _polygon_centroid(svg_verts)
                label_color = "#3498db" if is_active else "#282d37"
                parts.append(
                    f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
                    f'font-size="6.5" fill="{label_color}" font-family="sans-serif">'
                    f'{escape(name[:12])}</text>'
                )

    # Occupancy grid (the scanned floor detail).
    if data.grid_map_data:
        _render_grid(parts, data.grid_map_data, polys, to_svg, scale, "live_floor_clip")

    # Carpets, no-go zones and virtual walls.
    _render_zones(
        parts, data.carpet_polys, data.restricted_polys, data.virtual_walls, to_svg
    )

    # Cleaning trace: light-blue swept-area halo (robot width) + dark centre-line.
    halo_w = round(max(_CLEAN_SWATH_WIDTH_M * scale, 2.0), 1)
    line_w = round(_CENTRE_LINE_WIDTH_M * scale, 2)
    segs = _band_trajectory(trajectory, to_svg)
    if not segs and trajectory:
        sx, sy = to_svg(*trajectory[-1])
        parts.append(f'<circle cx="{sx}" cy="{sy}" r="{round(halo_w / 2, 1)}" fill="#aacef4" opacity="0.5"/>')
        parts.append(f'<circle cx="{sx}" cy="{sy}" r="1.3" fill="#1a5fc4"/>')
    _emit_trace(parts, segs, halo_w, line_w)

    # Dock marker
    if dock_x is not None and dock_y is not None:
        sx, sy = to_svg(dock_x, dock_y)
        parts.append(_dock_marker_svg(sx, sy, scale))

    # Detected obstacles (orange circles), sized like the robot footprint.
    _render_obstacles(parts, data.obstacles, to_svg, scale)

    # Robot marker
    if robot_x is not None and robot_y is not None:
        sx, sy = to_svg(robot_x, robot_y)
        parts.append(
            _robot_marker_svg(
                sx,
                sy,
                data.robot_yaw,
                proj.map_rotation,
                robot_image_uri,
                scale,
            )
        )

    # Scale bar (1 metre).
    bar_px = round(scale)
    bar_y = proj.map_block_h - 10
    parts.append(
        f'<line x1="15" y1="{bar_y}" x2="{15 + bar_px}" y2="{bar_y}" '
        f'stroke="#888" stroke-width="1.5" stroke-linecap="round"/>'
        f'<text x="{15 + bar_px // 2}" y="{bar_y - 4}" text-anchor="middle" '
        f'fill="#888" font-size="8" font-family="sans-serif">1 m</text>'
    )

    # Room legend (2 columns).
    _render_legend(parts, data.rooms, data.current_room, proj.svg_w, proj.map_block_h)

    parts.append("</svg>")
    return "\n".join(parts)


def _generate_report_svg(data: RomoSnapshot, robot_image_uri: str | None = None) -> str:
    """Render a completed job's ``room_map`` snapshot as the cleaning-report SVG.

    Light theme matching the DJI app report: white rooms + labels, the occupancy
    grid as scan detail, carpets and no-go zones hatched, the dense ``history_path``
    sweep trace, detected obstacles, and the robot/station markers.
    """
    report_map = data.last_clean_map or {}
    seg = report_map.get("seg_map", {}) or {}
    polys = [
        p for p in seg.get("poly_info", []) if len(p.get("border_vertices", [])) >= 3
    ]
    history = (report_map.get("history_path") or {}).get("history_path") or []

    pts: list[tuple[float, float]] = [
        (v["x"], v["y"]) for p in polys for v in p["border_vertices"]
    ]
    pts.extend((q[0], q[1]) for q in history)

    proj = _project(pts, polys, data.rooms)
    if proj is None:
        return _EMPTY_SVG
    to_svg, scale = proj.to_svg, proj.scale

    parts = _svg_prelude(proj)

    dup = duplicate_label_ids(polys)

    # Rooms: the shared grey/white fills, then the outlines (labels are drawn
    # last so nothing covers them).
    _render_room_fills(parts, polys, to_svg)
    for p in polys:
        sv = [to_svg(v["x"], v["y"]) for v in p["border_vertices"]]
        pstr = " ".join(f"{x},{y}" for x, y in sv)
        parts.append(
            f'<polygon points="{pstr}" fill="none" stroke="#96a0af" stroke-width="0.8"/>'
        )

    # Occupancy grid: the scanned detail, merged into horizontal runs.
    grid = report_map.get("grid_map")
    if grid:
        _render_grid(parts, grid, polys, to_svg, scale, "report_floor_clip")

    # Carpets + no-go zones (hatched) + virtual walls.
    _render_zones(
        parts,
        (report_map.get("carpet_layer") or {}).get("data", []),
        (report_map.get("restricted_layer") or {}).get("data", []),
        (report_map.get("virtual_wall") or {}).get("data", []),
        to_svg,
    )

    # Sweep trace (history_path). Each point is [x, y, yaw, flag, type, width].
    # Only actual *cleaning* passes (CLEAN_PASS_TYPES, see const.py) are drawn —
    # the other types are transit/maneuvering the DJI app does not trace either.
    # Those points — and any jump > 0.6 m — break the polyline (so transit moves
    # leave a gap, matching the app).
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
    # Fall back to the accumulated live trace (persisted/restored across reloads)
    # when the report blob has no usable cleaning pass, so the last cleaning's
    # trace still shows at end-of-session and after a Home Assistant reload.
    if not segs and data.trajectory:
        segs = _band_trajectory(data.trajectory, to_svg)
    _emit_trace(
        parts,
        segs,
        round(max(_CLEAN_SWATH_WIDTH_M * scale, 2.0), 1),
        round(_CENTRE_LINE_WIDTH_M * scale, 2),
    )

    # Obstacles (orange), sized like the robot footprint.
    _render_obstacles(
        parts,
        [
            (vs[0]["x"], vs[0]["y"])
            for o in (report_map.get("obstacle_layer") or {}).get("data", [])
            if (vs := o.get("vertices", []))
        ],
        to_svg,
        scale,
    )

    # Station marker only. The robot marker is deliberately omitted on the report:
    # this is a frozen end-of-job snapshot, so the job's robot_pos reflects where the
    # robot stopped (often offset from the dock by its body, in whatever direction it
    # was facing) and would render misaligned with the dock. The live map shows the
    # current robot position instead.
    st = report_map.get("station_pos") or {}
    if st.get("station_position_x") is not None:
        sx, sy = to_svg(st["station_position_x"], st["station_position_y"])
        parts.append(_dock_marker_svg(sx, sy, scale))

    # Room labels.
    for p in polys:
        name = room_name(p, dup)
        if not name:
            continue
        sv = [to_svg(v["x"], v["y"]) for v in p["border_vertices"]]
        cx, cy = _polygon_centroid(sv)
        parts.append(
            f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
            f'font-size="7.5" fill="#282d37" font-family="sans-serif">{escape(name[:14])}</text>'
        )

    # Room legend (2 columns).
    _render_legend(parts, data.rooms, data.current_room, proj.svg_w, proj.map_block_h)

    parts.append("</svg>")
    return "\n".join(parts)


def _polygon_centroid(vertices: list[tuple[float, float]]) -> tuple[float, float]:
    """Calculate the geometric centroid of a non-intersecting polygon."""
    area = 0.0
    cx = 0.0
    cy = 0.0
    n = len(vertices)
    if n == 0:
        return 0.0, 0.0
    for i in range(n):
        x0, y0 = vertices[i]
        x1, y1 = vertices[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area /= 2.0
    if area == 0:
        return vertices[0]
    return round(cx / (6.0 * area), 1), round(cy / (6.0 * area), 1)


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


def _robot_marker_scale(map_scale: float) -> float:
    """Multiplier mapping the nominal _ROBOT_MARKER_SIZE marker to the map scale.

    Targets the robot's real footprint (_ROBOT_DIAM_M) at the current map zoom,
    floored at _ROBOT_MARKER_MIN_PX so the icon stays legible. Returns 1.0 when the
    scale is unknown (<= 0), keeping the original fixed size.
    """
    if map_scale <= 0:
        return 1.0
    target_px = max(_ROBOT_DIAM_M * map_scale, _ROBOT_MARKER_MIN_PX)
    return round(target_px / _ROBOT_MARKER_SIZE, 3)


def _dock_marker_svg(sx: float, sy: float, map_scale: float = 0.0) -> str:
    """Return the charging-station marker, sized a touch larger than the robot.

    The nominal art is 10 wide x 12 tall; it's scaled so its height tracks the map
    zoom at _DOCK_HEIGHT_M (just above the robot's diameter), floored at
    _DOCK_MARKER_MIN_PX. Falls back to the original fixed 0.6 when the scale is
    unknown (<= 0).
    """
    if map_scale <= 0:
        k = 0.6
    else:
        k = round(max(_DOCK_HEIGHT_M * map_scale, _DOCK_MARKER_MIN_PX) / 12.0, 3)
    return (
        f'<g transform="translate({sx} {sy}) scale({k})">'
        f'<rect x="-5" y="-6" width="10" height="12" rx="2" fill="#151515"/>'
        f'<path d="M 1,-3.5 L -2,0.5 H -0.5 L -1.5,4 L 2,-0.5 H 0.5 Z" fill="white"/>'
        f'</g>'
    )


def _robot_marker_svg(
    sx: float,
    sy: float,
    yaw: float | None,
    map_rotation: float = 0.0,
    image_uri: str | None = None,
    map_scale: float = 0.0,
) -> str:
    """Return the cropped Romo top-view marker centred on the robot position."""
    if image_uri is None:
        return _robot_marker_fallback_svg(sx, sy, yaw, map_rotation, map_scale)

    rotation = (
        0
        if yaw is None
        else round(_ROBOT_IMAGE_HEADING_OFFSET - yaw - map_rotation, 1)
    )
    size = _ROBOT_MARKER_SIZE
    x = round(sx - size / 2, 1)
    y = round(sy - size / 2, 1)
    # Scale the whole marker (image + shadow) uniformly about the robot centre so it
    # tracks the map zoom while keeping its tuned proportions.
    k = _robot_marker_scale(map_scale)
    # Circular drop-shadow ~40 cm across (slightly larger than the robot). In nominal
    # marker units the robot image spans _ROBOT_MARKER_SIZE for _ROBOT_DIAM_M, so the
    # shadow radius scales from that ratio; centred on the robot so rotation is a no-op.
    shadow_r = round(_ROBOT_MARKER_SIZE / 2 * (_ROBOT_SHADOW_DIAM_M / _ROBOT_DIAM_M), 1)
    return (
        f'<g transform="translate({sx} {sy}) scale({k}) translate({-sx} {-sy})">'
        f'<circle cx="{sx}" cy="{sy}" r="{shadow_r}" fill="#0f1720" opacity="0.22"/>'
        f'<g transform="rotate({rotation} {sx} {sy})">'
        f'<image href="{image_uri}" x="{x}" y="{y}" width="{size}" height="{size}" '
        f'preserveAspectRatio="xMidYMid meet"/>'
        f'</g>'
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
    map_scale: float = 0.0,
) -> str:
    """Return a compact vector marker if the PNG asset is unavailable."""
    rotation = 0 if yaw is None else round(-yaw - map_rotation, 1)
    x = round(sx - 10, 1)
    y = round(sy - 8, 1)
    # Same map-scale sizing as the image marker, folded into this marker's own
    # base scale so the vector glyph tracks the map zoom too.
    scale = round(_ROBOT_MARKER_SIZE / 26.0 * _robot_marker_scale(map_scale), 3)
    return (
        f'<g transform="translate({sx} {sy}) rotate({rotation}) scale({scale}) translate({-sx} {-sy})">'
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
