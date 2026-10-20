#!/usr/bin/env python3
"""Create a dependency-free bitmap source for the QuantWeave app icon."""
from __future__ import annotations

import sys
from pathlib import Path


SIZE = 1024


def main() -> None:
    target = Path(sys.argv[1])
    pixels = bytearray(SIZE * SIZE * 3)
    points = [(180, 690), (320, 570), (455, 625), (610, 400), (760, 470), (850, 280)]
    for y in range(SIZE):
        for x in range(SIZE):
            edge = min(x, y, SIZE - 1 - x, SIZE - 1 - y)
            rounded = edge < 80 and (x - (80 if x < SIZE / 2 else SIZE - 80)) ** 2 + (y - (80 if y < SIZE / 2 else SIZE - 80)) ** 2 > 80 ** 2
            base = (7, 13, 18) if not rounded else (0, 0, 0)
            grid = (x % 128 < 3 or y % 128 < 3) and 120 < x < 904 and 120 < y < 904
            color = (22, 47, 59) if grid else base
            offset = (y * SIZE + x) * 3
            pixels[offset:offset + 3] = bytes(color)
    for first, second in zip(points, points[1:]):
        x1, y1 = first; x2, y2 = second
        steps = max(abs(x2 - x1), abs(y2 - y1))
        for step in range(steps + 1):
            x = round(x1 + (x2 - x1) * step / steps); y = round(y1 + (y2 - y1) * step / steps)
            for dy in range(-14, 15):
                for dx in range(-14, 15):
                    if dx * dx + dy * dy > 14 * 14: continue
                    px, py = x + dx, y + dy
                    if 0 <= px < SIZE and 0 <= py < SIZE:
                        offset = (py * SIZE + px) * 3
                        pixels[offset:offset + 3] = bytes((91, 190, 225))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(f"P6\n{SIZE} {SIZE}\n255\n".encode("ascii") + pixels)


if __name__ == "__main__":
    main()
