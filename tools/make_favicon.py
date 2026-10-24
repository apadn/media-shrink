"""生成 favicon.ico / apple-touch-icon.png（纯标准库，无第三方依赖）。

图形：深色圆角底 + 左右相对的蓝色箭头 + 中间白色竖条（“压缩”意象），
与页面配色一致（--accent #5b8cff / --bg #0c0e12）。所有尺寸都用同一份几何
按比例缩放后 8x 超采样，小尺寸下不会糊。

用法：python3 tools/make_favicon.py
输出：app/static/favicon.ico（16/32/48 三档）、app/static/apple-touch-icon.png（180）
"""
import struct
import zlib

BASE = 128.0          # 设计稿边长
SS = 8                # 超采样倍数

BG_TOP = (27, 33, 45)
BG_BOTTOM = (14, 18, 24)
ACCENT = (91, 140, 255)
BAR = (231, 234, 240)


def _in_rounded_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_triangle(x, y, ax, ay, bx, by, cx, cy):
    def sign(px, py, qx, qy, rx, ry):
        return (px - rx) * (qy - ry) - (qx - rx) * (py - ry)

    d1 = sign(x, y, ax, ay, bx, by)
    d2 = sign(x, y, bx, by, cx, cy)
    d3 = sign(x, y, cx, cy, ax, ay)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def shade(u, v):
    """u, v 是 0..128 设计稿坐标，返回 (r, g, b, a)。"""
    if not _in_rounded_rect(u, v, 1, 1, BASE - 1, BASE - 1, 27):
        return (0, 0, 0, 0)

    t = v / BASE
    bg = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)) + (255,)

    cy = BASE / 2
    # 左箭头：底在 x=18，尖端指向中心 x=52
    if _in_triangle(u, v, 18, cy - 26, 18, cy + 26, 52, cy):
        return ACCENT + (255,)
    # 右箭头：底在 x=110，尖端指向中心 x=76
    if _in_triangle(u, v, 110, cy - 26, 110, cy + 26, 76, cy):
        return ACCENT + (255,)
    # 中间竖条
    if 60 <= u <= 68 and abs(v - cy) <= 26:
        return BAR + (255,)

    return bg


def render(size):
    """返回 size×size 的 RGBA 行列表。"""
    scale = BASE / size / SS
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    pr, pg, pb, pa = shade(x * BASE / size + (sx + 0.5) * scale,
                                           y * BASE / size + (sy + 0.5) * scale)
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            n = SS * SS
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
        rows.append(bytes(row))
    return rows


def png_bytes(rows):
    size = len(rows)
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(images):
    """images: [(size, png_bytes)]，写成多尺寸 ICO（内嵌 PNG，浏览器全支持）。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, payload = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(payload)
        )
        payload += data
    return header + entries + payload


if __name__ == "__main__":
    # favicon.ico：16/32/48 三档打包在一起
    imgs = [(s, png_bytes(render(s))) for s in (16, 32, 48)]
    with open("app/static/favicon.ico", "wb") as f:
        f.write(ico_bytes(imgs))
    print(f"ok  app/static/favicon.ico        {len(ico_bytes(imgs))} bytes (16/32/48)")

    apple = png_bytes(render(180))
    with open("app/static/apple-touch-icon.png", "wb") as f:
        f.write(apple)
    print(f"ok  app/static/apple-touch-icon.png {len(apple)} bytes (180)")
