#!/usr/bin/env python3
"""Regenerate PNG toolbar / store icons for the timeline export extension.

Deliberately mark-free: no white-X-on-black (that reads as the X Corp logo and
gets rejected by the Chrome Web Store for impersonation). Uses the same
Homebound navy/teal/amber palette as the activity-log exporter so the two
extensions look like siblings — a "timeline" spine with posts plus an export
arrow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw_icon(size: int) -> Image.Image:
    s = size / 128.0
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    rad = max(2, int(24 * s))
    dr.rounded_rectangle((0, 0, size - 1, size - 1), radius=rad, fill=(15, 23, 42, 255))

    def bar(x: float, y: float, w: float, h: float, fill: tuple[int, int, int], rpix: float = 3) -> None:
        x0, y0 = int(x * s), int(y * s)
        x1 = max(x0 + 1, int((x + w) * s) - 1)
        y1 = max(y0 + 1, int((y + h) * s) - 1)
        ry = max(1, int(rpix * s))
        dr.rounded_rectangle((x0, y0, x1, y1), radius=ry, fill=(*fill, 255))

    def dot(cx: float, cy: float, r: float, fill: tuple[int, int, int]) -> None:
        x0, y0 = int((cx - r) * s), int((cy - r) * s)
        x1, y1 = int((cx + r) * s), int((cy + r) * s)
        dr.ellipse((x0, y0, x1, y1), fill=(*fill, 255))

    # Vertical timeline spine with post markers (microblog timeline motif).
    bar(28, 30, 5, 56, (20, 184, 166), rpix=3)
    rows = [(36, (45, 212, 191), 56), (58, (94, 234, 212), 44), (80, (20, 184, 166), 50)]
    for cy, shade, width in rows:
        dot(30, cy, 7, shade)
        bar(44, cy - 3, width, 6, shade)

    # Export arrow (amber) — same accent the activity-log icon uses.
    pts = [(92, 84), (92, 100), (76, 100), (76, 92), (84, 92), (84, 84)]
    ip = [(int(px * s), int(py * s)) for px, py in pts]
    dr.polygon(ip, fill=(245, 158, 11, 255))

    return img


def main() -> None:
    here = Path(__file__).resolve().parent
    for sz in (16, 48, 128):
        out = here / f'icon{sz}.png'
        draw_icon(sz).save(out, format='PNG')
        print(out)


if __name__ == '__main__':
    main()
