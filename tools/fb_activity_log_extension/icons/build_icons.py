#!/usr/bin/env python3
"""Regenerate PNG toolbar / store icons (Chrome does not use SVG in manifest)."""

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

    bar(28, 34, 72, 6, (20, 184, 166))
    bar(28, 50, 56, 6, (45, 212, 191))
    bar(28, 66, 64, 6, (94, 234, 212))

    pts = [(88, 82), (88, 98), (72, 98), (72, 90), (80, 90), (80, 82)]
    ip = [(int(px * s), int(py * s)) for px, py in pts]
    dr.polygon(ip, fill=(245, 158, 11, 255))

    x0, y0 = int(28 * s), int(82 * s)
    x1, y1 = int(68 * s) - 1, int(104 * s) - 1
    sw = max(1, int(3 * s))
    rbox = max(1, int(4 * s))
    dr.rounded_rectangle((x0, y0, x1, y1), radius=rbox, outline=(148, 163, 184, 255), width=sw)
    return img


def main() -> None:
    here = Path(__file__).resolve().parent
    for sz in (16, 48, 128):
        out = here / f'icon{sz}.png'
        draw_icon(sz).save(out, format='PNG')
        print(out)


if __name__ == '__main__':
    main()
