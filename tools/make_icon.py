#!/usr/bin/env python3
"""Render the water-drop brand icon (no dependencies beyond the standard library).

Writes custom_components/novafos_watts/brand/icon.png (256 px) and icon@2x.png (512 px).
"""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(__file__), "..", "custom_components", "novafos_watts", "brand")
TOP, BOTTOM = (96, 196, 245), (14, 94, 190)  # light to deep blue
SS = 3  # supersampling per axis for smooth edges


def inside_drop(x: float, y: float) -> bool:
    """Unit-square drop: a circle with a cone rising to an apex."""
    cx, cy, r, apex = 0.5, 0.62, 0.29, 0.10
    if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
        return True
    d = cy - apex
    half = math.asin(r / d)
    dy = y - apex
    return 0 <= dy <= d - r * math.sin(half) and abs(x - cx) <= dy * math.tan(half)


def inside_highlight(x: float, y: float) -> bool:
    return ((x - 0.40) / 0.045) ** 2 + ((y - 0.64) / 0.115) ** 2 <= 1


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        t = py / (size - 1)
        base = [round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)]
        for px in range(size):
            hits = glints = 0
            for sy in range(SS):
                for sx in range(SS):
                    x, y = (px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size
                    if inside_drop(x, y):
                        hits += 1
                        glints += inside_highlight(x, y)
            alpha = round(255 * hits / (SS * SS))
            mix = 0.55 * glints / max(hits, 1)  # white highlight blended into the body
            rgb = [round(c + (255 - c) * mix) for c in base]
            row += bytes((*rgb, alpha))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path: str, size: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(render(size), 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    write_png(os.path.join(OUT, "icon.png"), 256)
    write_png(os.path.join(OUT, "icon@2x.png"), 512)
    print("wrote", os.path.normpath(OUT))
