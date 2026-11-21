# -*- coding: utf-8 -*-
"""知乎风格局部对比图：每行一对裁剪，红虚线=修复前的问题，绿虚线=修复后。"""
import fitz
from PIL import Image, ImageDraw, ImageFont

FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_R = r"C:\Windows\Fonts\msyh.ttc"
ZOOM = 3.0
SPLIT = 303.0
LEFT, RIGHT = (72.0, 301.0), (311.0, 540.0)
W = 620
PAD, GAP, HEAD = 18, 14, 64


def col_of(x):
    return LEFT if x < SPLIT else RIGHT




def line_at(page, pt):
    """包含给定点的文本行外框（用于取整行公式的真实宽度）。"""
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            r = fitz.Rect(l["bbox"])
            if r.contains(fitz.Point(pt)):
                return r
    return fitz.Rect(pt, pt)


def same_line_extent(page, mb):
    """沿 mb 所在基线把连续的 span 拼成整行外框（数学公式被拆成多个 span）。"""
    y = (mb.y0 + mb.y1) / 2
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln["spans"]:
                r = fitz.Rect(sp["bbox"])
                if r.y0 - 1 <= y <= r.y1 + 1 and r.x0 <= LEFT[1] + 90:
                    spans.append(r)
    spans.sort(key=lambda r: r.x0)
    cur = None
    for r in spans:
        if cur is None:
            cur = fitz.Rect(r)
        elif r.x0 - cur.x1 < 14:
            cur |= r
        elif r.x0 > cur.x1:
            break
    return cur

def block_of(page, text):
    """命中文本所在文本块的外框（表注通常两行，取整块）。"""
    hits = page.search_for(text)
    if not hits:
        raise SystemExit(f"p{page.number + 1}: 找不到 {text!r}")
    h = hits[0]
    for b in page.get_text("blocks"):
        r = fitz.Rect(b[:4])
        if r.contains(fitz.Point(h.x0, h.y0 + 0.5)):
            return r
    return h

def crop(page, rect, pad=2, pady=None):
    py = pad if pady is None else pady
    r = fitz.Rect(rect.x0 - pad, rect.y0 - py, rect.x1 + pad, rect.y1 + py)
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=r)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), r


def rows_of(page):
    """页面上的文本行矩形（用于把裁剪边对齐到整行）。"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            r = fitz.Rect(l["bbox"])
            if r.height > 1 and r.width > 20:
                out.append(r)
    return out


def snap(page, rect):
    """把裁剪边扩到整行：上下各取相邻完整行，不留半截字。"""
    rs = [r for r in rows_of(page)
          if r.x1 > rect.x0 + 1 and r.x0 < rect.x1 - 1]
    top, bot = rect.y0, rect.y1
    inside = [r for r in rs if r.y0 <= rect.y0 <= r.y1]
    if inside:
        top = min(r.y0 for r in inside)
    else:
        up = [r.y0 for r in rs if r.y0 < rect.y0]
        top = max(up) if up else rect.y0
    inside = [r for r in rs if r.y0 <= rect.y1 <= r.y1]
    if inside:
        bot = max(r.y1 for r in inside)
    else:
        down = [r.y1 for r in rs if r.y1 > rect.y1]
        bot = min(down) if down else rect.y1
    return fitz.Rect(rect.x0, top, rect.x1, bot)


def mark(layer, r, origin, off, color, tint, scale, dashed_flag=True, w=3, dash=9):
    """在 RGBA 图层上画「底色 + 边框」；r 为 PDF 坐标。"""
    ax0 = (r.x0 - origin.x0) * scale + off[0]
    ay0 = (r.y0 - origin.y0) * scale + off[1]
    ax1 = (r.x1 - origin.x0) * scale + off[0]
    ay1 = (r.y1 - origin.y0) * scale + off[1]
    layer.rectangle([ax0, ay0, ax1, ay1], fill=tint)
    if not dashed_flag:
        layer.rectangle([ax0, ay0, ax1, ay1], outline=color, width=w)
        return
    for xa, ya, xb, yb in ((ax0, ay0, ax1, ay0), (ax1, ay0, ax1, ay1),
                           (ax1, ay1, ax0, ay1), (ax0, ay1, ax0, ay0)):
        if abs(xa - xb) < 0.1:
            y, step = min(ya, yb), dash * 2
            while y < max(ya, yb):
                layer.line([(xa, y), (xa, min(y + dash, max(ya, yb)))], fill=color, width=w)
                y += step
        else:
            x, step = min(xa, xb), dash * 2
            while x < max(xa, xb):
                layer.line([(x, ya), (min(x + dash, max(xa, xb)), ya)], fill=color, width=w)
                x += step


def main():
    bdoc, adoc = fitz.open("paper.pdf"), fitz.open("paper-final.pdf")

    def hit(doc, page, text, nth=-1):
        r = doc[page].search_for(text)
        if not r:
            raise SystemExit(f"missing {text!r} on p{page + 1}")
        return r[nth]

    def img_rect(doc, page, min_w=100):
        cands = [fitz.Rect(d["bbox"]) for d in doc[page].get_image_info()]
        cands = [c for c in cands if c.width >= min_w]
        return max(cands, key=lambda c: c.width)

    def lines_of(doc, page):
        out = []
        for d in doc[page].get_drawings():
            for it in d["items"]:
                if it[0] == "l":
                    p, q = it[1], it[2]
                elif it[0] == "re" and it[1].height <= 1.6:
                    p, q = it[1].bl, it[1].br
                else:
                    continue
                if abs(p.y - q.y) <= 1.2 and abs(p.x - q.x) >= 50:
                    out.append(fitz.Rect(min(p.x, q.x), p.y, max(p.x, q.x), p.y))
        return out

    def table_near(doc, page, cap):
        """取 caption 正下方、横跨度一致的那组框线，返回其并集矩形。"""
        ls = [r for r in lines_of(doc, page) if r.y0 >= cap.y0 - 2]
        if not ls:
            raise SystemExit(f"page {page + 1}: caption 下方没有横线")
        ls.sort(key=lambda r: r.y0)
        seed = ls[0]
        grp = [r for r in ls if abs(r.width - seed.width) <= 2 and r.y0 <= ls[0].y0 + 400]
        box = grp[0]
        for r in grp[1:]:
            box |= r
        return box

    rows = []

    # ① 寡行：同一句 "with respect to the quorum constraint." 33% → 66%
    r1 = hit(bdoc, 1, "quorum constraint.")
    b0, b1 = col_of(r1.x0)
    box1 = fitz.Rect(b0, r1.y0 - 2, r1.x1 + 2, r1.y1 + 2)
    rb = snap(bdoc[1], fitz.Rect(b0, r1.y0 - 52, b1, r1.y1 + 10))
    ib, ub = crop(bdoc[1], rb, pady=0)
    r2 = hit(adoc, 1, "quorum constraint.")
    a0, a1 = col_of(r2.x0)
    box2 = fitz.Rect(a0, r2.y0 - 2, r2.x1 + 2, r2.y1 + 2)
    ra = snap(adoc[1], fitz.Rect(a0, r2.y0 - 52, a1, r2.y1 + 10))
    ia, ua = crop(adoc[1], ra, pady=0)
    rows.append(("① 段落末行（寡行）", ib, ub, [box1], ia, ua, [box2],
                 "末行只占栏宽 33%", "末行撑到 66%"))

    # ② 表宽：Table 1（before 用 151pt 窄表，after 用整宽 tabular*）
    def tbl(doc, page, cap):
        """同一横跨度的 booktabs 框线取并集：得到表格整体的矩形。"""
        cand = [r for r in lines_of(doc, page) if cap.y0 < r.y0 < cap.y0 + 130]
        if not cand:
            raise SystemExit(f"p{page + 1}: 表框线没找到")
        best = max({round(r.width) for r in cand},
                   key=lambda w: sum(1 for r in cand if abs(r.width - w) <= 1.5))
        grp = sorted([r for r in cand if abs(r.width - best) <= 1.5], key=lambda r: r.y0)
        return fitz.Rect(min(r.x0 for r in grp), min(r.y0 for r in grp) - 1.0,
                         max(r.x1 for r in grp), max(r.y1 for r in grp) + 1.0)

    cb = block_of(bdoc[2], "Table 1:")
    tb = tbl(bdoc, 2, cb)
    rb = fitz.Rect(LEFT[0] - 8, cb.y0 - 6, LEFT[1] + 6, tb.y1 + 26)
    ib, ub = crop(bdoc[2], rb)
    ca = block_of(adoc[2], "Table 1:")
    ta = tbl(adoc, 2, ca)
    ra = fitz.Rect(LEFT[0] - 8, ca.y0 - 6, LEFT[1] + 6, ta.y1 + 26)
    ia, ua = crop(adoc[2], ra)
    rows.append(("③ 表格没撑满栏宽", ib, ub, [tb], ia, ua, [ta],
                 f"表宽 {tb.width:.0f}pt / 栏宽 229pt（{tb.width / 229 * 100:.0f}%），两侧各空 {(229 - tb.width) / 2:.0f}pt",
                 f"tabular* 拉满 {ta.width:.0f}pt（= 版心宽）"))

    # ③ 图：Figure 1
    fb_rect = img_rect(bdoc, 1)
    fb = hit(bdoc, 1, "Figure 1:")
    fa_rect = img_rect(adoc, 1)
    fa = hit(adoc, 1, "Figure 1:")
    y0 = min(fb_rect.y0, fa_rect.y0) - 18
    y1 = max(fb.y1, fa.y1) + 10
    rb = fitz.Rect(RIGHT[0] - 4, y0, RIGHT[1] + 4, y1)
    ib, ub = crop(bdoc[1], rb)
    ra = fitz.Rect(RIGHT[0] - 4, y0, RIGHT[1] + 4, y1)
    ia, ua = crop(adoc[1], ra)
    rows.append(("④ 图片两侧留白", ib, ub,
                 [fb_rect, fitz.Rect(fb_rect.x1, fb_rect.y0, RIGHT[1], fb_rect.y1)],
                 ia, ua, [fa_rect],
                 "图宽 156pt（68%），右侧空 73pt", "width=\\linewidth 顶到栏边"))

    # ④ 公式：越栏 + 编号掉行（红框用审计给的真实越界 bbox）
    import json

    issues = json.load(open("before.json", encoding="utf-8"))["issues"]
    ove = max((i for i in issues if i["category"] == "overflow"),
              key=lambda i: i.get("metric") or 0)
    def eq_anchor(page):
        hits = [h for h in page.search_for("min")
                if abs(h.x0 - LEFT[0]) < 8 and h.width < 40]
        return sorted(hits, key=lambda r: r.y0)[0]

    mb = eq_anchor(bdoc[1])
    eqb = hit(bdoc, 1, "(1)")
    obx = same_line_extent(bdoc[1], mb)
    obx.x1 = min(obx.x1, LEFT[1] + 74)
    eqline = fitz.Rect(obx.x0 - 2, obx.y0 - 2, obx.x1 + 2, obx.y1 + 2)
    rb = snap(bdoc[1], fitz.Rect(LEFT[0] - 6, mb.y0 - 26, 308.0, eqb.y1 + 10))
    ib, ub = crop(bdoc[1], rb, pady=0)
    ma = eq_anchor(adoc[1])
    oa = hit(adoc, 1, "(1)")
    blk = fitz.Rect(62, ma.y0 - 6, LEFT[1] + 10, oa.y1 + 2)
    ra = snap(adoc[1], fitz.Rect(56, ma.y0 - 16, LEFT[1] + 6, oa.y1 + 44))
    ia, ua = crop(adoc[1], ra, pady=0)
    rows.append(("⑤ 公式越栏、编号掉行", ib, ub, [eqline, fitz.Rect(eqb.x0 - 2, eqb.y0, eqb.x1 + 2, eqb.y1)],
                 ia, ua, [blk],
                 f"公式宽 {obx.width:.0f}pt = {obx.width / 229 * 100:.0f}% 栏宽，右侧溢出 {obx.x1 - LEFT[1]:.0f}pt，编号 (1) 被挤到下一行",
                 "aligned 三行，编号回到行内"))

    # ⑤ 章节层级：2.1.1 → 行内 paragraph
    hb = hit(bdoc, 1, "2.1.1")
    rb = snap(bdoc[1], fitz.Rect(LEFT[0], hb.y0 - 22, LEFT[1], hb.y1 + 40))
    ib, ub = crop(bdoc[1], rb, pady=0)
    ha = hit(adoc, 1, "Relaxation.")
    ra = snap(adoc[1], fitz.Rect(LEFT[0], ha.y0 - 22, LEFT[1], ha.y1 + 34))
    ia, ua = crop(adoc[1], ra, pady=0)
    rows.append(("⑥ 三级标题把正文切碎", ib, ub,
                 [fitz.Rect(LEFT[0], hb.y0 - 6, LEFT[0] + 150, hb.y1 + 2)],
                 ia, ua, [fitz.Rect(LEFT[0], ha.y0 - 4, LEFT[0] + 82, ha.y1 + 1)],
                 "2.1.1 Relaxation 独占一行＋上下空行", "Relaxation. 变成行内小标题"))

    f_t = ImageFont.truetype(FONT_B, 30)
    f_s = ImageFont.truetype(FONT_R, 21)
    names = ["01-widow", "03-table", "04-figure", "05-equation", "06-heading"]

    for name, (title, ib, ub, bb, ia, ua, ba, lb, la) in zip(names, rows):
        sb, sa = ib, ia
        w = max(sb.width, sa.width)
        img = Image.new("RGB", (PAD * 2 + w * 2 + GAP, 84 + max(sb.height, sa.height) + 78), "white")
        d = ImageDraw.Draw(img)
        d.text((PAD, 10), title, font=f_t, fill="black")
        d.text((PAD, 54), "BEFORE", font=f_s, fill="#b03030")
        d.text((PAD + w + GAP, 54), "AFTER", font=f_s, fill="#1f7a3a")
        top = 84
        img.paste(sb, (PAD, top))
        img.paste(sa, (PAD + w + GAP, top))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for r in bb:
            mark(ld, r, ub, (PAD, top), (198, 40, 60), (255, 205, 210, 120), ZOOM)
        for r in ba:
            mark(ld, r, ua, (PAD + w + GAP, top), (30, 122, 58), (205, 240, 215, 120),
                 ZOOM, dashed_flag=False)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(img)
        cy = top + max(sb.height, sa.height) + 16
        d.text((PAD, cy), lb, font=f_s, fill="#b03030")
        d.text((PAD + w + GAP, cy), la, font=f_s, fill="#1f7a3a")
        img.save(f"cmp-{name}.png")
        print("saved", f"cmp-{name}.png", img.size)


if __name__ == "__main__":
    main()
