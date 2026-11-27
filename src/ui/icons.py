"""Zero-DLL pure QPainter/QPainterPath vector icon engine (Lucide / Fluent 24x24).

Renders crisp @2x DPR monochrome icons and the programmatic sidebar brand mark
with zero QtSvg dependencies and zero external assets. Exports:
- ``paint_icon(painter, name, rect_f, color, stroke_width=1.5)``
- ``render_icon_pixmap(name, size=16, color=..., stroke_width=1.5, dpr=2.0)``
- ``get_icon(name, size=16, color=None, theme="light", ...)``
- ``get_brand_pixmap(size=28, theme="light", dpr=2.0)``
- ``get_brand_icon(size=28, theme="light", dpr=2.0)``
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from src.ui.theme import get_palette

_TOKEN_RE = re.compile(r"[MLHVCQZmlhvcqz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")

# Cached compiled icon geometry: (stroke_path, fill_path) on a canonical 24x24 grid
_COMPILED_CACHE: dict[str, tuple[QPainterPath, QPainterPath]] = {}
_ICON_CACHE: dict[tuple[Any, ...], QIcon] = {}
_BRAND_CACHE: dict[tuple[int, str, float], QPixmap] = {}


def _make_gear_outline() -> str:
    """Generate a mathematically symmetric 8-tooth Lucide-style gear path (24x24)."""
    cx, cy = 12.0, 12.0
    teeth = 8
    r_outer = 9.8
    r_inner = 7.4
    pts: list[tuple[float, float]] = []
    step = (2.0 * math.pi) / teeth
    for i in range(teeth):
        a0 = i * step - math.pi / 2.0
        # 4 angles per tooth: valley-end, tip-start, tip-end, valley-start
        offsets = (
            (-0.27, r_inner),
            (-0.15, r_outer),
            (0.15, r_outer),
            (0.27, r_inner),
        )
        for frac, rad in offsets:
            ang = a0 + frac * step
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    parts = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    for x, y in pts[1:]:
        parts.append(f"L {x:.2f} {y:.2f}")
    parts.append("Z")
    return " ".join(parts)


# Primitive specs on canonical 24x24 grid:
#   ("L", x1, y1, x2, y2)                   -> line
#   ("P", [(x, y), ...], closed)            -> polyline / polygon
#   ("R", x, y, w, h, rx)                   -> rounded rectangle
#   ("C", cx, cy, r)                        -> stroked circle
#   ("F", cx, cy, r)                        -> filled dot/circle
#   ("E", x, y, w, h)                       -> ellipse
#   ("A", x, y, w, h, start_deg, span_deg)  -> arc
#   ("D", path_str)                         -> mini SVG path (M/L/H/V/C/Q/Z)
_ICON_SPECS: dict[str, list[tuple[Any, ...]]] = {
    "database": [
        ("E", 3.5, 2.5, 17, 5),
        ("L", 3.5, 5, 3.5, 19),
        ("L", 20.5, 5, 20.5, 19),
        ("D", "M 3.5 12 C 3.5 13.8 7.3 15 12 15 C 16.7 15 20.5 13.8 20.5 12"),
        ("D", "M 3.5 19 C 3.5 20.8 7.3 22 12 22 C 16.7 22 20.5 20.8 20.5 19"),
    ],
    "upload": [
        ("D", "M 21 15 L 21 19 C 21 20.1 20.1 21 19 21 L 5 21 C 3.9 21 3 20.1 3 19 L 3 15"),
        ("P", [(17, 8), (12, 3), (7, 8)], False),
        ("L", 12, 3, 12, 15),
    ],
    "download": [
        ("D", "M 21 15 L 21 19 C 21 20.1 20.1 21 19 21 L 5 21 C 3.9 21 3 20.1 3 19 L 3 15"),
        ("P", [(7, 10), (12, 15), (17, 10)], False),
        ("L", 12, 15, 12, 3),
    ],
    "table": [
        ("R", 3, 3.5, 18, 17, 2.2),
        ("L", 3, 9, 21, 9),
        ("L", 3, 14.5, 21, 14.5),
        ("L", 9, 9, 9, 20.5),
    ],
    "git-compare": [
        ("C", 6, 6, 2.6),
        ("C", 18, 18, 2.6),
        ("D", "M 13 6 L 16 6 C 17.1 6 18 6.9 18 8 L 18 15.2"),
        ("P", [(15.5, 3.5), (13, 6), (15.5, 8.5)], False),
        ("D", "M 11 18 L 8 18 C 6.9 18 6 17.1 6 16 L 6 8.8"),
        ("P", [(8.5, 15.5), (11, 18), (8.5, 20.5)], False),
    ],
    "layers": [
        ("P", [(12, 2.5), (21, 7.5), (12, 12.5), (3, 7.5)], True),
        ("P", [(3, 12.5), (12, 17.5), (21, 12.5)], False),
        ("P", [(3, 16.8), (12, 21.5), (21, 16.8)], False),
    ],
    "settings": [
        ("D", _make_gear_outline()),
        ("C", 12, 12, 3.1),
    ],
    "sun": [
        ("C", 12, 12, 4),
        ("L", 12, 2.2, 12, 4.2),
        ("L", 12, 19.8, 12, 21.8),
        ("L", 2.2, 12, 4.2, 12),
        ("L", 19.8, 12, 21.8, 12),
        ("L", 5.1, 5.1, 6.5, 6.5),
        ("L", 17.5, 17.5, 18.9, 18.9),
        ("L", 18.9, 5.1, 17.5, 6.5),
        ("L", 6.5, 17.5, 5.1, 18.9),
    ],
    "moon": [
        (
            "D",
            "M 20.4 14.3 C 19.2 14.8 17.9 15.1 16.5 15.1 "
            "C 11.7 15.1 7.9 11.3 7.9 6.5 C 7.9 5.1 8.2 3.8 8.7 2.6 "
            "C 5.0 3.8 2.5 7.3 2.5 11.5 C 2.5 16.8 6.8 21.1 12.1 21.1 "
            "C 16.3 21.1 19.7 18.2 20.4 14.3 Z",
        ),
    ],
    "copy": [
        ("R", 8.5, 8.5, 12.5, 12.5, 2.2),
        (
            "D",
            "M 5.2 15.5 L 4.5 15.5 C 3.4 15.5 2.5 14.6 2.5 13.5 "
            "L 2.5 4.5 C 2.5 3.4 3.4 2.5 4.5 2.5 L 13.5 2.5 "
            "C 14.6 2.5 15.5 3.4 15.5 4.5 L 15.5 5.2",
        ),
    ],
    "check": [
        ("P", [(4.5, 12.6), (9.5, 17.6), (19.5, 6.6)], False),
    ],
    "minus": [
        ("L", 5, 12, 19, 12),
    ],
    "search": [
        ("C", 10.8, 10.8, 6.8),
        ("L", 20.5, 20.5, 15.8, 15.8),
    ],
    "plus": [
        ("L", 12, 5, 12, 19),
        ("L", 5, 12, 19, 12),
    ],
    "folder": [
        (
            "D",
            "M 3 6 C 3 4.9 3.9 4 5 4 L 9.2 4 C 9.9 4 10.5 4.3 10.9 4.9 "
            "L 12 6.5 C 12.4 7.1 13 7.5 13.7 7.5 L 19 7.5 "
            "C 20.1 7.5 21 8.4 21 9.5 L 21 18 C 21 19.1 20.1 20 19 20 "
            "L 5 20 C 3.9 20 3 19.1 3 18 Z",
        ),
    ],
    "folder-open": [
        (
            "D",
            "M 3 18.6 L 3 6 C 3 4.9 3.9 4 5 4 L 9.1 4 C 9.8 4 10.4 4.3 10.8 4.9 "
            "L 11.9 6.5 C 12.3 7.1 12.9 7.5 13.6 7.5 L 18.8 7.5 "
            "C 19.9 7.5 20.8 8.4 20.8 9.5 L 20.8 11",
        ),
        (
            "D",
            "M 3.2 19.2 L 5.3 12.3 C 5.6 11.5 6.3 11 7.2 11 L 20.2 11 "
            "C 21.1 11 21.8 11.9 21.5 12.8 L 19.7 18.7 "
            "C 19.4 19.5 18.7 20 17.8 20 L 4.5 20 C 3.7 20 3.0 19.6 3.2 19.2 Z",
        ),
    ],
    "file-text": [
        (
            "D",
            "M 14.5 2.5 L 6 2.5 C 4.9 2.5 4 3.4 4 4.5 L 4 19.5 "
            "C 4 20.6 4.9 21.5 6 21.5 L 18 21.5 C 19.1 21.5 20 20.6 20 19.5 "
            "L 20 8 Z",
        ),
        ("P", [(14.5, 2.5), (14.5, 8), (20, 8)], False),
        ("L", 8, 9, 10.5, 9),
        ("L", 8, 13, 16, 13),
        ("L", 8, 17, 16, 17),
    ],
    "file-spreadsheet": [
        (
            "D",
            "M 14.5 2.5 L 6 2.5 C 4.9 2.5 4 3.4 4 4.5 L 4 19.5 "
            "C 4 20.6 4.9 21.5 6 21.5 L 18 21.5 C 19.1 21.5 20 20.6 20 19.5 "
            "L 20 8 Z",
        ),
        ("P", [(14.5, 2.5), (14.5, 8), (20, 8)], False),
        ("L", 8, 12.5, 16, 12.5),
        ("L", 8, 16.5, 16, 16.5),
        ("L", 12, 10.5, 12, 18.5),
    ],
    "trash": [
        ("L", 3.5, 6, 20.5, 6),
        ("D", "M 8.5 6 L 8.5 4.2 C 8.5 3.3 9.3 2.5 10.2 2.5 L 13.8 2.5 C 14.7 2.5 15.5 3.3 15.5 4.2 L 15.5 6"),
        (
            "D",
            "M 18.5 6 L 17.6 19.5 C 17.5 20.6 16.6 21.5 15.5 21.5 "
            "L 8.5 21.5 C 7.4 21.5 6.5 20.6 6.4 19.5 L 5.5 6",
        ),
        ("L", 10, 10.5, 10, 17),
        ("L", 14, 10.5, 14, 17),
    ],
    "refresh-cw": [
        ("A", 4, 4, 16, 16, 30, 295),
        ("P", [(19.8, 3.6), (19.3, 8.3), (14.6, 7.8)], False),
    ],
    "arrow-left-right": [
        ("L", 4, 8, 20, 8),
        ("P", [(16, 4), (20, 8), (16, 12)], False),
        ("L", 20, 16, 4, 16),
        ("P", [(8, 12), (4, 16), (8, 20)], False),
    ],
    "play": [
        (
            "D",
            "M 6.8 4.8 C 6.8 3.9 7.8 3.4 8.5 3.9 L 19.0 11.1 "
            "C 19.7 11.5 19.7 12.5 19.0 12.9 L 8.5 20.1 "
            "C 7.8 20.6 6.8 20.1 6.8 19.2 Z",
        ),
    ],
    "more-horizontal": [
        ("F", 5.5, 12, 1.4),
        ("F", 12, 12, 1.4),
        ("F", 18.5, 12, 1.4),
    ],
    "chevron-down": [
        ("P", [(6, 9), (12, 15), (18, 9)], False),
    ],
    "chevron-up": [
        ("P", [(18, 15), (12, 9), (6, 15)], False),
    ],
    "chevron-right": [
        ("P", [(9, 6), (15, 12), (9, 18)], False),
    ],
    "chevron-left": [
        ("P", [(15, 6), (9, 12), (15, 18)], False),
    ],
    "chevrons-up-down": [
        ("P", [(7.5, 9), (12, 4.5), (16.5, 9)], False),
        ("P", [(7.5, 15), (12, 19.5), (16.5, 15)], False),
    ],
    "check-circle": [
        ("C", 12, 12, 9.2),
        ("P", [(8.2, 12.2), (10.8, 14.8), (16, 9.4)], False),
    ],
    "x-circle": [
        ("C", 12, 12, 9.2),
        ("L", 9, 9, 15, 15),
        ("L", 15, 9, 9, 15),
    ],
    "alert-triangle": [
        (
            "D",
            "M 10.6 3.9 L 2.7 17.6 C 2.1 18.7 2.9 20 4.1 20 L 19.9 20 "
            "C 21.1 20 21.9 18.7 21.3 17.6 L 13.4 3.9 C 12.8 2.8 11.2 2.8 10.6 3.9 Z",
        ),
        ("L", 12, 9, 12, 13.5),
        ("F", 12, 16.7, 1.05),
    ],
    "info": [
        ("C", 12, 12, 9.2),
        ("L", 12, 11.2, 12, 16.2),
        ("F", 12, 7.8, 1.05),
    ],
    "sparkles": [
        (
            "D",
            "M 10.5 3 C 11 7.2 12.8 9 17 9.5 C 12.8 10 11 11.8 10.5 16 "
            "C 10 11.8 8.2 10 4 9.5 C 8.2 9 10 7.2 10.5 3 Z",
        ),
        ("L", 18.5, 14.5, 18.5, 20.5),
        ("L", 15.5, 17.5, 21.5, 17.5),
    ],
    "filter": [
        ("P", [(3, 4.5), (21, 4.5), (14, 12.5), (14, 19), (10, 21), (10, 12.5)], True),
    ],
    "columns": [
        ("R", 3, 3.5, 18, 17, 2.2),
        ("L", 9.5, 3.5, 9.5, 20.5),
        ("L", 15, 3.5, 15, 20.5),
    ],
    "package": [
        ("P", [(12, 2.5), (20.5, 7.2), (20.5, 16.8), (12, 21.5), (3.5, 16.8), (3.5, 7.2)], True),
        ("P", [(3.5, 7.2), (12, 12), (20.5, 7.2)], False),
        ("L", 12, 12, 12, 21.5),
        ("L", 7.8, 4.8, 16.2, 9.6),
    ],
    "x": [
        ("L", 6, 6, 18, 18),
        ("L", 18, 6, 6, 18),
    ],
    "edit": [
        (
            "D",
            "M 11 4 L 5 4 C 3.9 4 3 4.9 3 6 L 3 19 C 3 20.1 3.9 21 5 21 "
            "L 18 21 C 19.1 21 20 20.1 20 19 L 20 13",
        ),
        (
            "D",
            "M 18.3 2.7 C 19.1 1.9 20.4 1.9 21.2 2.7 C 22 3.5 22 4.8 21.2 5.6 "
            "L 12 14.8 L 8.5 15.5 L 9.2 12 Z",
        ),
    ],
    "external-link": [
        (
            "D",
            "M 18 13 L 18 19 C 18 20.1 17.1 21 16 21 L 5 21 "
            "C 3.9 21 3 20.1 3 19 L 3 8 C 3 6.9 3.9 6 5 6 L 11 6",
        ),
        ("P", [(15, 3), (21, 3), (21, 9)], False),
        ("L", 10, 14, 21, 3),
    ],
    "archive": [
        ("R", 2.5, 3.5, 19, 4.5, 1.5),
        ("D", "M 4 8 L 4 18.5 C 4 19.6 4.9 20.5 6 20.5 L 18 20.5 C 19.1 20.5 20 19.6 20 18.5 L 20 8"),
        ("L", 10, 12.5, 14, 12.5),
    ],
}

ICON_NAMES: tuple[str, ...] = tuple(sorted(_ICON_SPECS.keys()))


def has_icon(name: str) -> bool:
    """Return True if ``name`` is registered in the vector icon catalog."""
    return name in _ICON_SPECS


def _append_svg_path(path: QPainterPath, d_str: str) -> None:
    """Parse a lightweight subset of SVG path commands (M/L/H/V/C/Q/Z) into ``path``."""
    tokens = _TOKEN_RE.findall(d_str)
    i = 0
    cmd = ""
    cx, cy = 0.0, 0.0

    def _next_float() -> float:
        nonlocal i
        val = float(tokens[i])
        i += 1
        return val

    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in ("Z", "z"):
                path.closeSubpath()
                continue
        if cmd == "M":
            cx, cy = _next_float(), _next_float()
            path.moveTo(cx, cy)
            cmd = "L"
        elif cmd == "L":
            cx, cy = _next_float(), _next_float()
            path.lineTo(cx, cy)
        elif cmd == "H":
            cx = _next_float()
            path.lineTo(cx, cy)
        elif cmd == "V":
            cy = _next_float()
            path.lineTo(cx, cy)
        elif cmd == "C":
            x1, y1 = _next_float(), _next_float()
            x2, y2 = _next_float(), _next_float()
            cx, cy = _next_float(), _next_float()
            path.cubicTo(x1, y1, x2, y2, cx, cy)
        elif cmd == "Q":
            x1, y1 = _next_float(), _next_float()
            cx, cy = _next_float(), _next_float()
            path.quadTo(x1, y1, cx, cy)
        else:
            break


def _compile_icon(name: str) -> tuple[QPainterPath, QPainterPath]:
    cached = _COMPILED_CACHE.get(name)
    if cached is not None:
        return cached

    specs = _ICON_SPECS.get(name)
    stroke_path = QPainterPath()
    fill_path = QPainterPath()
    if specs is None:
        # Subtle fallback circle so unknown names never crash rendering
        stroke_path.addEllipse(QRectF(4, 4, 16, 16))
        return stroke_path, fill_path

    for item in specs:
        kind = item[0]
        if kind == "L":
            _, x1, y1, x2, y2 = item
            stroke_path.moveTo(x1, y1)
            stroke_path.lineTo(x2, y2)
        elif kind == "P":
            _, pts, closed = item
            if pts:
                stroke_path.moveTo(pts[0][0], pts[0][1])
                for px, py in pts[1:]:
                    stroke_path.lineTo(px, py)
                if closed:
                    stroke_path.closeSubpath()
        elif kind == "R":
            _, x, y, w, h, rx = item
            stroke_path.addRoundedRect(QRectF(x, y, w, h), rx, rx)
        elif kind == "C":
            _, cx, cy, r = item
            stroke_path.addEllipse(QPointF(cx, cy), r, r)
        elif kind == "F":
            _, cx, cy, r = item
            fill_path.addEllipse(QPointF(cx, cy), r, r)
        elif kind == "E":
            _, x, y, w, h = item
            stroke_path.addEllipse(QRectF(x, y, w, h))
        elif kind == "A":
            _, x, y, w, h, start_deg, span_deg = item
            rect = QRectF(x, y, w, h)
            stroke_path.arcMoveTo(rect, start_deg)
            stroke_path.arcTo(rect, start_deg, span_deg)
        elif kind == "D":
            _append_svg_path(stroke_path, item[1])

    result = (stroke_path, fill_path)
    _COMPILED_CACHE[name] = result
    return result


def _to_qcolor(color: str | QColor) -> QColor:
    return color if isinstance(color, QColor) else QColor(color)


def paint_icon(
    painter: QPainter,
    name: str,
    rect_f: QRectF,
    color: str | QColor,
    stroke_width: float = 1.5,
) -> None:
    """Paint a crisp vector icon directly onto ``painter`` within ``rect_f``."""
    side = min(rect_f.width(), rect_f.height())
    if side <= 0.5:
        return

    stroke_path, fill_path = _compile_icon(name)
    qcolor = _to_qcolor(color)

    dx = rect_f.x() + (rect_f.width() - side) * 0.5
    dy = rect_f.y() + (rect_f.height() - side) * 0.5
    scale = side / 24.0

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(dx, dy)
    painter.scale(scale, scale)

    if not stroke_path.isEmpty():
        # Convert target logical stroke width (e.g. 1.5px at 16-24px) into 24-unit space
        pen_w = max(1.1, stroke_width * (24.0 / max(side, 12.0)))
        pen = QPen(
            qcolor,
            pen_w,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(stroke_path)

    if not fill_path.isEmpty():
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(qcolor))
        painter.drawPath(fill_path)

    painter.restore()


def render_icon_pixmap(
    name: str,
    size: int = 16,
    color: str | QColor = "#52525B",
    *,
    stroke_width: float = 1.5,
    dpr: float = 2.0,
) -> QPixmap:
    """Render an antialiased vector icon into a high-DPI ``QPixmap``."""
    eff_size = max(8, int(size))
    eff_dpr = max(1.0, float(dpr))
    phys = max(8, int(round(eff_size * eff_dpr)))

    pm = QPixmap(phys, phys)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(eff_dpr)

    painter = QPainter(pm)
    try:
        paint_icon(
            painter,
            name,
            QRectF(0.0, 0.0, float(eff_size), float(eff_size)),
            color,
            stroke_width=stroke_width,
        )
    finally:
        painter.end()
    return pm


def get_icon(
    name: str,
    size: int = 16,
    color: Optional[str | QColor] = None,
    *,
    theme: str = "light",
    active_color: Optional[str | QColor] = None,
    disabled_color: Optional[str | QColor] = None,
    stroke_width: float = 1.5,
    dpr: float = 2.0,
) -> QIcon:
    """Return a cached multi-state ``@2x`` DPR ``QIcon`` for ``name``."""
    p = get_palette(theme)
    if color is None:
        c_normal = _to_qcolor(p["icon_default"])
        c_active = _to_qcolor(active_color if active_color is not None else p["icon_hover"])
        c_disabled = _to_qcolor(disabled_color if disabled_color is not None else p["icon_muted"])
    else:
        c_normal = _to_qcolor(color)
        c_active = _to_qcolor(active_color if active_color is not None else c_normal)
        if disabled_color is not None:
            c_disabled = _to_qcolor(disabled_color)
        else:
            c_disabled = QColor(c_normal)
            c_disabled.setAlphaF(max(0.35, c_normal.alphaF() * 0.42))

    key = (
        name,
        int(size),
        c_normal.rgba(),
        c_active.rgba(),
        c_disabled.rgba(),
        round(float(stroke_width), 2),
        round(float(dpr), 2),
    )
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    pm_normal = render_icon_pixmap(name, size, c_normal, stroke_width=stroke_width, dpr=dpr)
    pm_active = (
        pm_normal
        if c_active.rgba() == c_normal.rgba()
        else render_icon_pixmap(name, size, c_active, stroke_width=stroke_width, dpr=dpr)
    )
    pm_disabled = render_icon_pixmap(name, size, c_disabled, stroke_width=stroke_width, dpr=dpr)

    icon = QIcon()
    icon.addPixmap(pm_normal, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(pm_active, QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(pm_active, QIcon.Mode.Active, QIcon.State.Off)
    icon.addPixmap(pm_active, QIcon.Mode.Active, QIcon.State.On)
    icon.addPixmap(pm_active, QIcon.Mode.Selected, QIcon.State.Off)
    icon.addPixmap(pm_active, QIcon.Mode.Selected, QIcon.State.On)
    icon.addPixmap(pm_disabled, QIcon.Mode.Disabled, QIcon.State.Off)
    icon.addPixmap(pm_disabled, QIcon.Mode.Disabled, QIcon.State.On)

    _ICON_CACHE[key] = icon
    return icon


def get_brand_pixmap(
    size: int = 28,
    theme: str = "light",
    dpr: float = 2.0,
) -> QPixmap:
    """Render a razor-sharp Option B programmatic vector brand mark for the sidebar."""
    eff_size = max(16, int(size))
    norm_theme = "dark" if theme == "dark" else "light"
    eff_dpr = max(1.0, float(dpr))
    cache_key = (eff_size, norm_theme, round(eff_dpr, 2))
    cached = _BRAND_CACHE.get(cache_key)
    if cached is not None:
        return cached

    p = get_palette(norm_theme)
    phys = max(16, int(round(eff_size * eff_dpr)))
    pm = QPixmap(phys, phys)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(eff_dpr)

    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = eff_size / 28.0
        painter.scale(scale, scale)

        # Rounded monochrome container
        bg = QColor("#18181B") if norm_theme == "light" else QColor("#222531")
        rim = QColor("#27272A") if norm_theme == "light" else QColor("#363A4D")
        painter.setPen(QPen(rim, 1.0))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(QRectF(0.75, 0.75, 26.5, 26.5), 7.0, 7.0)

        # Back translucent geometric layer (compared dataset)
        painter.setPen(QPen(QColor(255, 255, 255, 110), 1.15))
        painter.setBrush(QBrush(QColor(255, 255, 255, 34)))
        painter.drawRoundedRect(QRectF(6.0, 5.8, 11.2, 12.2), 2.4, 2.4)

        # Front crisp canonical record plate
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#FAFAFA")))
        painter.drawRoundedRect(QRectF(9.6, 8.8, 12.2, 13.0), 2.5, 2.5)

        # Two ultra-clean data lines on front plate
        painter.setBrush(QBrush(QColor(p["accent"])))
        painter.drawRoundedRect(QRectF(12.0, 11.8, 7.4, 1.9), 0.9, 0.9)
        painter.setBrush(QBrush(QColor("#A1A1AA")))
        painter.drawRoundedRect(QRectF(12.0, 15.2, 5.2, 1.6), 0.8, 0.8)

        # Crisp emerald/accent verification badge at bottom-right
        painter.setPen(QPen(bg, 1.4))
        painter.setBrush(QBrush(QColor(p["accent"])))
        painter.drawEllipse(QPointF(20.6, 20.6), 4.1, 4.1)

        # Tiny white check inside badge
        ck_pen = QPen(
            QColor("#FFFFFF"),
            1.45,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(ck_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        ck = QPainterPath()
        ck.moveTo(18.9, 20.7)
        ck.lineTo(20.1, 21.9)
        ck.lineTo(22.4, 19.3)
        painter.drawPath(ck)
    finally:
        painter.end()

    _BRAND_CACHE[cache_key] = pm
    return pm


def get_brand_icon(
    size: int = 28,
    theme: str = "light",
    dpr: float = 2.0,
) -> QIcon:
    """Return a ``QIcon`` wrapping ``get_brand_pixmap``."""
    return QIcon(get_brand_pixmap(size=size, theme=theme, dpr=dpr))


def clear_icon_cache() -> None:
    """Clear rendered pixmap/icon caches (useful for tests or memory resets)."""
    _ICON_CACHE.clear()
    _BRAND_CACHE.clear()
