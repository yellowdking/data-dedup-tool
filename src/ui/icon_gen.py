"""Programmatic multi-size application icon generator for 数据查重程序.

Generates a clean, modern geometric vector-style multi-size Windows ICO file
at ``src/resources/app.ico`` with zero external assets required.

Run directly via:
    python -m src.ui.icon_gen
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional

from src.core.config import get_resource_path, is_frozen

ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _render_with_pillow(output_path: Path) -> None:
    """Render high-resolution supersampled icon via Pillow and save multi-size ICO."""
    from PIL import Image, ImageDraw

    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Smooth diagonal gradient: deep indigo (#3B5BDB) -> blue (#2563EB) -> cyan (#06B6D4)
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x * 0.55 + y * 0.45) / size
            if t < 0.55:
                u = t / 0.55
                r = int(59 + (37 - 59) * u)
                g = int(91 + (99 - 91) * u)
                b = int(219 + (235 - 219) * u)
            else:
                u = (t - 0.55) / 0.45
                r = int(37 + (6 - 37) * u)
                g = int(99 + (182 - 99) * u)
                b = int(235 + (212 - 235) * u)
            px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    pad = 24
    radius = 110
    mdraw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img, "RGBA")

    # Subtle glossy inner rim
    draw.rounded_rectangle(
        [pad + 3, pad + 3, size - pad - 3, size - pad - 3],
        radius=radius - 3,
        outline=(255, 255, 255, 58),
        width=5,
    )

    # Back geometric plate (compared layer)
    draw.rounded_rectangle(
        [105, 100, 325, 340],
        radius=28,
        fill=(255, 255, 255, 118),
        outline=(255, 255, 255, 185),
        width=5,
    )

    # Front crisp white plate (deduplicated canonical record)
    draw.rounded_rectangle(
        [160, 145, 385, 390],
        radius=28,
        fill=(255, 255, 255, 246),
    )

    # Minimalist data bars inside front card
    draw.rounded_rectangle([198, 198, 348, 218], radius=10, fill=(59, 91, 219, 225))
    draw.rounded_rectangle([198, 242, 318, 260], radius=9, fill=(148, 163, 184, 215))
    draw.rounded_rectangle([198, 282, 288, 300], radius=9, fill=(148, 163, 184, 215))

    # Emerald deduplication checkmark badge
    cx, cy, br = 360, 360, 74
    draw.ellipse(
        [cx - br - 7, cy - br - 7, cx + br + 7, cy + br + 7],
        fill=(255, 255, 255, 255),
    )
    draw.ellipse(
        [cx - br, cy - br, cx + br, cy + br],
        fill=(16, 185, 129, 255),
    )

    pts = [(cx - 32, cy + 2), (cx - 9, cy + 26), (cx + 36, cy - 24)]
    draw.line(pts, fill=(255, 255, 255, 255), width=18, joint="curve")
    for x0, y0 in pts:
        draw.ellipse([x0 - 9, y0 - 9, x0 + 9, y0 + 9], fill=(255, 255, 255, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="ICO", sizes=ICON_SIZES)


def _encode_png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    """Minimal pure-Python RGBA PNG encoder for fallback ICO generation."""
    raw = b"".join(b"\x00" + row for row in rgba_rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _render_fallback_ico(output_path: Path) -> None:
    """Pure-Python PNG-in-ICO fallback if Pillow is unavailable."""
    size = 64
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            if 4 <= x < size - 4 and 4 <= y < size - 4:
                if 20 <= x <= 48 and 18 <= y <= 48:
                    row.extend((255, 255, 255, 245))
                elif 14 <= x <= 38 and 12 <= y <= 40:
                    row.extend((219, 234, 254, 180))
                else:
                    t = (x + y) / (2 * size)
                    row.extend((int(37 + 10 * t), int(99 + 70 * t), 235, 255))
            else:
                row.extend((0, 0, 0, 0))
        rows.append(bytes(row))

    png_bytes = _encode_png(size, size, rows)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png_bytes), 22)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(header + entry + png_bytes)


def get_default_icon_path() -> Path:
    """Return the canonical path of ``src/resources/app.ico``."""
    return get_resource_path(Path("src") / "resources" / "app.ico")


def ensure_app_icon(output_path: Optional[Path] = None, force: bool = False) -> Path:
    """Ensure ``src/resources/app.ico`` exists, generating it idempotently if needed."""
    target = Path(output_path) if output_path is not None else get_default_icon_path()
    if target.is_file() and target.stat().st_size > 0 and not force:
        return target
    if is_frozen() and output_path is None:
        return target
    try:
        _render_with_pillow(target)
    except Exception:
        _render_fallback_ico(target)
    return target


if __name__ == "__main__":
    generated = ensure_app_icon(force=True)
    print(f"Generated app icon: {generated} ({generated.stat().st_size} bytes)")
