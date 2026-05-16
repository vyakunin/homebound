#!/usr/bin/env python3
"""Regenerate PNG toolbar / store icons for X/Twitter export extension."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw_icon(size: int) -> Image.Image:
    s = size / 128.0
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    rad = max(2, int(24 * s))
    # Dark background (X brand)
    dr.rounded_rectangle((0, 0, size - 1, size - 1), radius=rad, fill=(0, 0, 0, 255))

    # Draw stylised "X" in white
    lw = max(2, int(10 * s))
    margin = int(30 * s)
    end = size - 1 - margin
    dr.line([(margin, margin), (end, end)], fill=(255, 255, 255, 255), width=lw)
    dr.line([(end, margin), (margin, end)], fill=(255, 255, 255, 255), width=lw)

    # Small export arrow in blue (bottom-right)
    ax, ay = int(88 * s), int(88 * s)
    aw = int(8 * s)
    ah = int(16 * s)
    dr.rectangle((ax, ay - ah, ax + aw, ay), fill=(29, 155, 240, 255))
    pts = [
        (ax - int(4 * s), ay - ah),
        (ax + aw + int(4 * s), ay - ah),
        (ax + aw // 2, ay - ah - int(8 * s)),
    ]
    dr.polygon(pts, fill=(29, 155, 240, 255))

    return img


def main() -> None:
    here = Path(__file__).resolve().parent
    for sz in (16, 48, 128):
        out = here / f'icon{sz}.png'
        draw_icon(sz).save(out, format='PNG')
        print(out)


if __name__ == '__main__':
    main()
