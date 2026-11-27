# -*- coding: utf-8 -*-
"""1 页样本：列表 / 多子图 / 寡行（删词）三张局部对比图。

左 = before（红虚线 + 浅红底），右 = after（绿实线 + 浅绿底），同一缩放。
"""
import io
import json
import os
import sys

import fitz
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ZOOM = 3.0
RED, RED_T = (198, 40, 60), (255, 205, 210, 60)
GREEN, GREEN_T = (30, 122, 58), (205, 240, 215, 60)
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_R = r"C:\Windows\Fonts\msyh.ttc"


def font(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT_R, size)


def crop(page, rect, pad=2, pady=None):
    py = pad if pady is None else pady
    r = fitz.Rect(rect.x0 - pad, rect.y0 - py, rect.x1 + pad, rect.y1 + py) & page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=r)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), r


def rows_of(page):
    out = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            r = fitz.Rect(ln["bbox"])
            if r.height > 1 and r.width > 20:
                out.append(r)
    return out


def snap(page, rect):
    """把裁剪边扩到整行，只算与裁剪框横向相交的文本行，避免露出半截字。"""
    rs = [r for r in rows_of(page) if r.x1 > rect.x0 + 1 and r.x0 < rect.x1 - 1]
    top, bot = rect.y0, rect.y1
    inside = [r for r in rs if r.y0 <= rect.y0 <= r.y1]
    top = min(r.y0 for r in inside) if inside else max(
        [r.y0 for r in rs if r.y0 < rect.y0] or [rect.y0])
    inside = [r for r in rs if r.y0 <= rect.y1 <= r.y1]
    bot = max(r.y1 for r in inside) if inside else min(
        [r.y1 for r in rs if r.y1 > rect.y1] or [rect.y1])
    return fitz.Rect(rect.x0, top, rect.x1, bot)


def search(page, text):
    hits = page.search_for(text)
    if not hits:
        raise SystemExit("not found: %r" % text)
    r = fitz.Rect(hits[0])
    for h in hits[1:]:
        r |= h
    return r


def line_of(page, text):
    """包含该文本的整行（把同一基线上的 span 并起来）。"""
    hit = search(page, text)
    rects = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln["spans"]:
                r = fitz.Rect(sp["bbox"])
                if r.intersects(hit) and sp["text"].strip():
                    rects.append(r)
    if not rects:
        return hit
    out = fitz.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out


def bullet_left(page, y0, y1):
    xs = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            t = "".join(sp["text"] for sp in ln["spans"]).strip()
            r = fitz.Rect(ln["bbox"])
            if t.startswith("•") and r.y0 >= y0 - 4 and r.y1 <= y1 + 4:
                xs.append(r.x0)
    return min(xs) if xs else None


def mark(layer, r, origin, off, color, tint, scale, dashed_flag=True, w=6, dash=15, pad=7):
    ax = (r.x0 - origin.x0) * scale + off[0]
    ay = (r.y0 - origin.y0) * scale + off[1]
    bx = (r.x1 - origin.x0) * scale + off[0]
    by = (r.y1 - origin.y0) * scale + off[1]
    if (by - ay) < 46:                        # 单行（寡行）：向下借白，不向上压到上一行
        x0, y0, x1, y1 = ax - 7, ay - 5, bx + 7, by + 12
    else:
        x0, y0, x1, y1 = ax - pad, ay - pad, bx + pad, by + pad
    layer.rectangle([x0, y0, x1, y1], fill=tint)
    if dashed_flag:
        for (ex0, ey0), (ex1, ey1) in (((x0, y0), (x1, y0)), ((x0, y1), (x1, y1)),
                                       ((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))):
            length = max(abs(ex1 - ex0), abs(ey1 - ey0))
            steps = max(1, int(length / dash))
            for i in range(steps):
                t0, t1 = i / steps, min(1.0, (i + 0.55) / steps)
                layer.line([(ex0 + (ex1 - ex0) * t0, ey0 + (ey1 - ey0) * t0),
                            (ex0 + (ex1 - ex0) * t1, ey0 + (ey1 - ey0) * t1)],
                           fill=color, width=w)
    else:
        layer.rectangle([x0, y0, x1, y1], outline=color, width=w)


def panel(left, right, lb, rb, out):
    (limg, lrect), lbox = left, lb
    (rimg, rrect), rbox = right, rb
    gap, head, foot = 24, 44, 12
    W = limg.width + rimg.width + gap + 28
    H = head + max(limg.height, rimg.height) + foot
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    off_l = (14, head)
    off_r = (14 + limg.width + gap, head)
    canvas.paste(limg, off_l)
    canvas.paste(rimg, off_r)

    base = canvas.convert("RGBA")
    lay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    for img, rect, box, color, tint, off, dashed in (
        (limg, lrect, lbox, RED, RED_T, off_l, True),
        (rimg, rrect, rbox, GREEN, GREEN_T, off_r, False),
    ):
        scale = img.width / rect.width
        for b in box:
            mark(ld, b, rect, off, color, tint, scale, dashed_flag=dashed)
    canvas = Image.alpha_composite(base, lay).convert("RGB")

    d = ImageDraw.Draw(canvas)
    for off, img in ((off_l, limg), (off_r, rimg)):
        d.rectangle([off[0] - 1, off[1] - 1, off[0] + img.width, off[1] + img.height],
                    outline=(160, 160, 168), width=3)
        d.text((off[0], off[1] - 26), "修改前" if off == off_l else "修改后",
               font=font(20, True), fill=(90, 90, 96))
    canvas.save(os.path.join(HERE, out))
    print("wrote", out, canvas.size)


def main():
    before = fitz.open(os.path.join(HERE, "paper.pdf"))
    after = fitz.open(os.path.join(HERE, "paper-fixed.pdf"))
    b, a = before[0], after[0]

    # ① 寡行：删词后末行整行被吸收（12% → 95%）
    jb = json.load(io.open(os.path.join(HERE, "before.json"), encoding="utf-8"))
    tail = min((i for i in jb["issues"] if i["category"] == "widow"), key=lambda i: i["extra"]["fill"])
    lbox = [fitz.Rect(tail["bbox"])]
    rbox = [line_of(a, "post-hoc filters")]
    lrect = snap(b, fitz.Rect(60, lbox[0].y0 - 48, 312, lbox[0].y1 + 14))
    rrect = snap(a, fitz.Rect(60, rbox[0].y0 - 48, 312, rbox[0].y1 + 14))
    panel(crop(b, lrect, pady=0), crop(a, rrect, pady=0), lbox, rbox, "cmp-02-widow-delete.png")

    # ② 列表：左边距 + 条目末行不满
    first_b = b.search_for("Quadratic attention")[0]
    last_b = b.search_for("mization with frozen encoders")[0]
    first_a = a.search_for("Quadratic attention")[0]
    last_a = a.search_for("Unstable gradients with frozen encoders")[0]
    bl_b = bullet_left(b, first_b.y0, last_b.y1) or first_b.x0 - 12
    bl_a = bullet_left(a, first_a.y0, last_a.y1) or first_a.x0 - 12
    lb = fitz.Rect(bl_b - 4, first_b.y0 - 3, 301.0, last_b.y1 + 3)
    rb = fitz.Rect(bl_a - 4, first_a.y0 - 3, 301.0, last_a.y1 + 3)
    lrect = snap(b, fitz.Rect(66, lb.y0 - 42, 322, lb.y1 + 16))
    rrect = snap(a, fitz.Rect(66, rb.y0 - 42, 322, rb.y1 + 18))
    panel(crop(b, lrect, pady=0), crop(a, rrect, pady=0), [lb], [rb], "cmp-07-list.png")

    # ③ 多子图：子图间距 26pt + 上方空带 34pt
    imgs_b = [fitz.Rect(i["bbox"]) for i in b.get_image_info()]
    imgs_a = [fitz.Rect(i["bbox"]) for i in a.get_image_info()]
    band_b = fitz.Rect(imgs_b[0])
    for r in imgs_b[1:]:
        band_b |= r
    band_a = fitz.Rect(imgs_a[0])
    for r in imgs_a[1:]:
        band_a |= r
    lrect = snap(b, fitz.Rect(302, band_b.y0 - 34, 545, band_b.y1 + 44))
    rrect = snap(a, fitz.Rect(band_a.x0 - 8, band_a.y0 - 6, band_a.x1 + 8, band_a.y1 + 40))
    gapbox = fitz.Rect(imgs_b[0].x1, band_b.y0, imgs_b[1].x0, band_b.y1)
    above = [r for r in rows_of(b) if r.y1 <= band_b.y0 and r.x1 > band_b.x0 and r.x0 < band_b.x1]
    text_bot = max(r.y1 for r in above) if above else band_b.y0 - 30
    topbox = fitz.Rect(band_b.x0, text_bot + 1, band_b.x1, band_b.y0 - 1)
    panel(crop(b, lrect, pady=0), crop(a, rrect, pady=0), [gapbox, topbox], [band_a],
          "cmp-08-subfig.png")


if __name__ == "__main__":
    main()
