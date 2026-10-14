#!/usr/bin/env python3
"""Render logo.svg to the dashboard's home-screen icons, deterministically.

    python3 assets/make_icons.py            # writes assets/icons/*.png
    python3 assets/make_icons.py --check    # exit 1 if the committed PNGs differ

Standard library only (there is no PIL, rsvg or ImageMagick on the mini), so
this reads the handful of SVG features logo.svg actually uses -- a rounded
rect with a radial gradient, circles, and stroked cubic/quadratic paths with
round caps and a dash pattern -- and refuses anything else rather than
silently drawing it wrong. Edges are antialiased analytically from each
shape's distance, so the same input gives the same bytes on every run.

Outputs:
  icon-180.png  apple-touch-icon. Flattened RGB, no alpha, square and full
                bleed: iOS rounds the corners itself and turns any
                transparency black.
  icon-192.png  manifest icons, RGBA, with logo.svg's own rounded corners.
  icon-512.png
"""
from __future__ import annotations

import math
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "logo.svg"
OUT = HERE / "icons"

#: name -> (pixel size, flattened RGB with square corners?)
ICONS = {
    "icon-180.png": (180, True),
    "icon-192.png": (192, False),
    "icon-512.png": (512, False),
}

NS = "{http://www.w3.org/2000/svg}"
#: The attributes each element may carry. Anything else (transform, style,
#: x/y, fx/fy, fill-opacity...) would be drawn wrong, so it is refused.
_STROKE = {"stroke", "stroke-width", "stroke-linecap", "stroke-dasharray"}
ALLOWED = {
    "svg": {"viewBox", "width", "height"},
    "defs": set(),
    "g": _STROKE | {"fill", "opacity"},
    "radialGradient": {"id", "cx", "cy", "r"},
    "stop": {"offset", "stop-color"},
    "rect": {"width", "height", "rx", "fill"},
    "circle": {"cx", "cy", "r", "fill", "opacity"} | _STROKE,
    "path": {"d", "fill"} | _STROKE,
}
FLATTEN_STEPS = 48          # line segments per curve; far below a pixel at 512


# --- reading the svg ------------------------------------------------------------

def hex_rgb(s: str) -> tuple[float, float, float]:
    s = s.strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", s):
        raise ValueError(f"unsupported colour {s!r}")
    return tuple(int(s[i:i + 2], 16) / 255 for i in (1, 3, 5))


def path_points(d: str) -> list[list[tuple[float, float]]]:
    """A path's subpaths as polylines. M, C/c, Q/q, L/l only."""
    toks = re.findall(r"[A-Za-z]|-?\d*\.?\d+(?:e-?\d+)?", d)
    subs, cur, pos, i, cmd = [], None, (0.0, 0.0), 0, None

    def nums(n):
        nonlocal i
        vals = [float(t) for t in toks[i:i + n]]
        if len(vals) != n or any(re.fullmatch(r"[A-Za-z]", t) for t in toks[i:i + n]):
            raise ValueError(f"bad path data near {toks[i - 1:i + n]}")
        i += n
        return vals

    while i < len(toks):
        if re.fullmatch(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
        rel = cmd.islower()
        ox, oy = pos if rel else (0.0, 0.0)
        c = cmd.upper()
        if c == "M":
            x, y = nums(2)
            pos = (ox + x, oy + y)
            cur = [pos]
            subs.append(cur)
            cmd = "l" if rel else "L"       # implicit lineto after a moveto
        elif c == "L":
            x, y = nums(2)
            pos = (ox + x, oy + y)
            cur.append(pos)
        elif c in ("C", "Q"):
            v = nums(6 if c == "C" else 4)
            pts = [pos] + [(ox + v[k], oy + v[k + 1]) for k in range(0, len(v), 2)]
            for s in range(1, FLATTEN_STEPS + 1):
                t = s / FLATTEN_STEPS
                cur.append(bezier(pts, t))
            pos = pts[-1]
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
    return subs


def bezier(pts, t):
    while len(pts) > 1:
        pts = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
               for a, b in zip(pts, pts[1:])]
    return pts[0]


def dashed(poly, pattern):
    """Split a polyline into its dashes (on-pieces), measured along its length."""
    if not pattern:
        return [poly]
    out, on, idx, left = [], True, 0, pattern[0]
    piece = [poly[0]]
    for a, b in zip(poly, poly[1:]):
        seg = math.dist(a, b)
        done = 0.0
        while seg - done > left:
            done += left
            t = done / seg
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if on:
                piece.append(p)
                out.append(piece)
            piece = [p]
            on = not on
            idx = (idx + 1) % len(pattern)
            left = pattern[idx]
        left -= seg - done
        piece.append(b)
    if on and len(piece) > 1:
        out.append(piece)
    return out


def shapes(svg_text: str):
    """logo.svg as a list of drawing ops, in paint order."""
    root = ET.fromstring(svg_text)
    vb = [float(v) for v in root.get("viewBox").split()]
    if vb[:2] != [0, 0] or vb[2] != vb[3]:
        raise ValueError("expected a square viewBox from 0,0")
    grads, ops = {}, []

    def walk(el, inherit):
        tag = el.tag.replace(NS, "")
        extra = set(el.attrib) - ALLOWED.get(tag, set())
        if extra:
            raise ValueError(f"<{tag}> uses unsupported attributes: {sorted(extra)}")
        attrs = {**inherit, **{k: v for k, v in el.attrib.items()
                               if k in ("stroke", "stroke-width", "fill", "opacity",
                                        "stroke-linecap", "stroke-dasharray")}}
        if tag in ("svg", "g", "defs"):
            for child in el:
                walk(child, attrs if tag == "g" else inherit)
        elif tag == "radialGradient":
            stops = [(float(s.get("offset")), hex_rgb(s.get("stop-color"))) for s in el]
            grads[el.get("id")] = (float(el.get("cx")), float(el.get("cy")),
                                   float(el.get("r")), stops)
        elif tag == "rect":
            fill = attrs.get("fill", "")
            m = re.fullmatch(r"url\(#(.+)\)", fill)
            ops.append(("rect", float(el.get("width")), float(el.get("height")),
                        float(el.get("rx", 0)), grads[m.group(1)] if m else hex_rgb(fill)))
        elif tag == "circle":
            ops.append(("circle", float(el.get("cx")), float(el.get("cy")), float(el.get("r")),
                        hex_rgb(attrs["fill"]), float(attrs.get("opacity", 1))))
            if attrs.get("stroke"):
                raise ValueError("stroked circles are not supported")
        elif tag == "path":
            if attrs.get("fill", "none") != "none":
                raise ValueError("filled paths are not supported")
            if attrs.get("stroke-linecap") != "round":
                raise ValueError("only round-capped strokes are supported")
            dash = [float(v) for v in attrs.get("stroke-dasharray", "").replace(",", " ").split()]
            polys = [p for sub in path_points(el.get("d")) for p in dashed(sub, dash)]
            ops.append(("stroke", polys, float(attrs["stroke-width"]) / 2,
                        hex_rgb(attrs["stroke"])))
        else:
            raise ValueError(f"unsupported element <{tag}>")

    walk(root, {})
    return vb[2], ops


# --- painting ---------------------------------------------------------------

def render(svg_text: str, size: int, full_bleed: bool) -> list[list[float]]:
    """RGBA rows, floats 0..1, premultiplied."""
    units, ops = shapes(svg_text)
    k = size / units                        # pixels per svg unit
    px = [[0.0, 0.0, 0.0, 0.0] for _ in range(size * size)]

    def centre(i):                          # pixel index -> svg units
        return (i + 0.5) / k

    def paint(x, y, cov, rgb, alpha=1.0):
        a = cov * alpha
        if a <= 0:
            return
        p = px[y * size + x]
        inv = 1 - a
        p[0] = rgb[0] * a + p[0] * inv
        p[1] = rgb[1] * a + p[1] * inv
        p[2] = rgb[2] * a + p[2] * inv
        p[3] = a + p[3] * inv

    def span(lo, hi):                       # svg-unit range -> pixel range
        return max(0, int(math.floor(lo * k)) - 1), min(size, int(math.ceil(hi * k)) + 1)

    for op in ops:
        if op[0] == "rect":
            _, w, h, rx, fill = op
            if full_bleed:
                rx = 0.0
            for y in range(size):
                cy = centre(y)
                for x in range(size):
                    cx = centre(x)
                    # rounded-rect distance: negative inside
                    qx = abs(cx - w / 2) - (w / 2 - rx)
                    qy = abs(cy - h / 2) - (h / 2 - rx)
                    d = (math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - rx)
                    cov = min(1.0, max(0.0, 0.5 - d * k))
                    if full_bleed:
                        cov = 1.0
                    if isinstance(fill, tuple) and len(fill) == 4:
                        gx, gy, gr, stops = fill
                        t = math.hypot(cx - gx * w, cy - gy * h) / (gr * w)
                        rgb = gradient(stops, t)
                    else:
                        rgb = fill
                    paint(x, y, cov, rgb)
        elif op[0] == "circle":
            _, ccx, ccy, r, rgb, alpha = op
            x0, x1 = span(ccx - r, ccx + r)
            y0, y1 = span(ccy - r, ccy + r)
            for y in range(y0, y1):
                for x in range(x0, x1):
                    d = math.hypot(centre(x) - ccx, centre(y) - ccy) - r
                    paint(x, y, min(1.0, max(0.0, 0.5 - d * k)), rgb, alpha)
        else:
            _, polys, hw, rgb = op
            # Distance to the whole stroke first, then one paint per pixel:
            # painting per segment would darken where neighbours overlap.
            near: dict[int, float] = {}
            for poly in polys:
                for a, b in zip(poly, poly[1:]):
                    x0, x1 = span(min(a[0], b[0]) - hw, max(a[0], b[0]) + hw)
                    y0, y1 = span(min(a[1], b[1]) - hw, max(a[1], b[1]) + hw)
                    for y in range(y0, y1):
                        for x in range(x0, x1):
                            d = seg_dist((centre(x), centre(y)), a, b) - hw
                            i = y * size + x
                            if d < near.get(i, math.inf):
                                near[i] = d
            for i in sorted(near):
                paint(i % size, i // size, min(1.0, max(0.0, 0.5 - near[i] * k)), rgb)
    return px


def gradient(stops, t):
    t = min(1.0, max(0.0, t))
    for (o0, c0), (o1, c1) in zip(stops, stops[1:]):
        if t <= o1:
            f = 0.0 if o1 == o0 else (t - o0) / (o1 - o0)
            return tuple(a + (b - a) * f for a, b in zip(c0, c1))
    return stops[-1][1]


def seg_dist(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L = ax * ax + ay * ay
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
    return math.hypot(p[0] - a[0] - ax * t, p[1] - a[1] - ay * t)


# --- writing png -----------------------------------------------------------

def png(px, size: int, rgb_only: bool) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)                      # filter: none
        for x in range(size):
            r, g, b, a = px[y * size + x]
            if rgb_only:
                # Flatten: there is nothing behind a full-bleed icon, but be
                # explicit -- composite onto the gradient's own dark end.
                bg = (0x3D / 255, 0x11 / 255, 0x40 / 255)
                rows += bytes(round(255 * (c + bc * (1 - a))) for c, bc in zip((r, g, b), bg))
            else:
                if a > 0:
                    rows += bytes(round(255 * min(1.0, c / a)) for c in (r, g, b))
                else:
                    rows += b"\0\0\0"
                rows.append(round(255 * a))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2 if rgb_only else 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def pixels(data: bytes):
    """A PNG's header and decoded scanlines, for comparing two encodings."""
    pos, ihdr, idat = 8, b"", b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    return ihdr, zlib.decompress(idat)


def build() -> dict[str, bytes]:
    svg = SOURCE.read_text()
    out = {}
    for name, (size, flat) in ICONS.items():
        out[name] = png(render(svg, size, flat), size, flat)
    return out


def main(argv) -> int:
    built = build()
    if "--check" in argv:
        # Pixels, not bytes: another zlib may compress the same image differently.
        stale = [n for n, data in built.items()
                 if not (OUT / n).exists() or pixels((OUT / n).read_bytes()) != pixels(data)]
        if stale:
            print("stale: " + ", ".join(stale) + " -- run python3 assets/make_icons.py")
            return 1
        print("icons match logo.svg")
        return 0
    OUT.mkdir(exist_ok=True)
    for name, data in built.items():
        (OUT / name).write_bytes(data)
        print(f"wrote {OUT / name} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
