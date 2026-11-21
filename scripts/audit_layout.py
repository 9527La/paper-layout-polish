#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_layout.py — 论文版面「不均匀空白」量化审计（PyMuPDF）

设计原则：脚本只产出「候选证据 + 实测数值」，判定权交给 agent / 人。
每条 issue 都带 page / column / bbox / metric，便于用 --crop 复看局部。

覆盖（见 references/rubric.md）：
  widow     段落末行占比过低（矮行 / runt）            ← 核心
  orphan    段落首行掉到栏尾、末行孤悬栏首
  overflow  文本或公式越出栏宽（含公式编号掉行）
  bottom    栏底/页底大片空白（float 逃逸或分节断页）
  figure    图未填满栏宽 / 竖向图 / 全宽图越界
  table     表内字号过小或全文不统一 / 表未撑满栏宽
  source    源码级：\\subsubsection、\\texttt、代码式标识符、itemize 间距

用法：
  python audit_layout.py --pdf paper.pdf --tex . --md audit.md --json audit.json
  python audit_layout.py --pdf paper.pdf --render thumbs --zoom 0.5      # 50% 轮廓
  python audit_layout.py --pdf paper.pdf --crop 3:60,200,300,320 --crop-out c.png --crop-zoom 2

退出码：0 无 P0；1 存在 P0（便于批处理/CI）；2 参数或读取错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:  # Windows 控制台默认 GBK，遇到数学符号会直接崩
        if (_stream.encoding or "").lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 PyMuPDF：python -m pip install pymupdf\n")
    raise SystemExit(2)

BOLD_FLAG = 2 ** 4
ITALIC_FLAG = 2 ** 1

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "info": 4}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    page: int
    col: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: float
    text: str
    math: bool = False
    full: bool = False
    kind: str = "body"

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def nchars(self) -> int:
        return len(self.text.strip())


@dataclass
class Issue:
    cat: str
    severity: str
    page: int
    col: int
    metric: str
    detail: str
    suggest: str
    bbox: tuple | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "id": "",
            "category": self.cat,
            "severity": self.severity,
            "page": self.page,
            "column": self.col,
            "metric": self.metric,
            "detail": self.detail,
            "suggest": self.suggest,
        }
        if self.bbox:
            d["bbox"] = [round(v, 1) for v in self.bbox]
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class Column:
    index: int
    left: float
    right: float
    top: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left


# --------------------------------------------------------------------------- #
# 读取与几何推断
# --------------------------------------------------------------------------- #
def mode_value(values, ndigits=1, default=0.0):
    if not values:
        return default
    c = Counter(round(v, ndigits) for v in values)
    return c.most_common(1)[0][0]


def weighted_mode(values, weights, ndigits=1, default=0.0):
    if not values:
        return default
    c = Counter()
    for v, w in zip(values, weights):
        c[round(v, ndigits)] += max(1, int(w))
    return c.most_common(1)[0][0]


MATH_CHARS = set("∑∫∏∈ℝℕαβγδεζηθλμνξπρσϕφχψωΩℓ≤≥≠≈±×·÷∂∞→←↔∥⊥√∣⟨⟩")


def _looks_like_math(text: str, sizes) -> bool:
    """纯排版用途：判断这一行是不是公式/符号行，用于把它们排除在正文寡行统计之外。"""
    if any(ch in MATH_CHARS for ch in text):
        return True
    if sizes and max(sizes) - min(sizes) >= 1.5:  # 上下标共存
        return True
    stripped = text.strip()
    if not stripped:
        return True
    letters = [c for c in stripped if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) == len(letters) and len(letters) <= 3:
        return True  # N / X / i=1 这类符号碎片
    return False


def collect_lines(page, pno):
    """把 PyMuPDF 的 span 重组成「视觉行」：同基线的碎片合并，按间隙补回空格。"""
    runs = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            for s in ln.get("spans", []):
                text = s.get("text", "")
                if not text.strip():
                    continue
                x0, y0, x1, y1 = s["bbox"]
                runs.append(
                    {
                        "x0": x0,
                        "y0": y0,
                        "x1": x1,
                        "y1": y1,
                        "size": s["size"],
                        "text": text,
                        "bold": bool(int(s.get("flags", 0)) & BOLD_FLAG),
                    }
                )
    runs.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))

    clusters = []  # {'y0','y1','runs':[...],'sizes':[...],'x0':float,'xmax':float}
    for r in runs:
        target = None
        target = None
        best_score = None
        for cl in reversed(clusters[-8:]):
            # 字号差太多＝不同角色（节标题 vs 正文），即使基线接近也不能并成一行：
            # 曾把栏 2 的「3 Evaluation」并进栏 1 行尾，造出 17.2pt 的假越界。
            # 但上下标（$c_{s,r}$ 的下标 7pt）本来就比正文小且竖直内含在行框里，
            # 这类碎片必须允许并入，否则公式密集的段落会被拆出「where c」这种假末行。
            contained = (r["y0"] >= cl["y0"] - 0.8) and (r["y1"] <= cl["y1"] + 0.8)
            if abs(r["size"] - max(cl["sizes"])) > 1.5 and not contained:
                continue
            # 基线抖动只允许 1pt 量级；用 0.22*size 会跨过两个上下相邻行的间距。
            same_baseline = abs(r["y0"] - cl["y0"]) <= 0.6 + 0.06 * min(r["size"], max(cl["sizes"]))
            if not same_baseline:
                continue
            # 只允许「紧邻」的碎片合并，且阈值封顶 9pt（栏间中缝约 10pt，绝不能跨栏）。
            thr = min(9.0, max(3.5, 0.75 * max(r["size"], max(cl["sizes"]))))
            gap_right = r["x0"] - cl["xmax"]  # 本片接在簇右侧（同一行自左向右推进）
            gap_left = cl["x0"] - r["x1"]  # 本片接在簇左侧（补前面的碎片）
            ok_right = -2.0 < gap_right < thr
            ok_left = -2.0 < gap_left < thr
            if not (ok_right or ok_left):
                continue
            # 打分而不是 first-match-break：曾因左右两个簇的 gap 都是 0 而选中更晚创建的
            # 右侧簇，把「says whether shard s | is placed in …」劈成两行（假寡行 + 假右溢）。
            # 正常情形一行自左向右拼，所以向右接的代价更低；向左接要额外付 thr。
            score = max(0.0, gap_right) if ok_right else thr + max(0.0, gap_left)
            if best_score is None or score < best_score:
                best_score = score
                target = cl
        if target is None:
            clusters.append({"y0": r["y0"], "y1": r["y1"], "runs": [r], "sizes": [r["size"]], "x0": r["x0"], "xmax": r["x1"]})
        else:
            target["runs"].append(r)
            target["sizes"].append(r["size"])
            target["y0"] = min(target["y0"], r["y0"])
            target["y1"] = max(target["y1"], r["y1"])
            target["x0"] = min(target["x0"], r["x0"])
            target["xmax"] = max(target["xmax"], r["x1"])

    # 碎片可能乱序到达（公式/斜体 span 的 y0 与正文差 0.2pt 就会排到前面先成簇），
    # 单靠「运行中找邻居」补不回来。这里再走一遍簇合并：同基线、间隙足够小、字号相当，
    # 就把右簇并进左簇——否则同一行会被劈成「…shard s is placed in region」与
    # 「r becomes a frac-」两段，制造假寡行和假右溢。
    merged_clusters = True
    while merged_clusters:
        merged_clusters = False
        clusters.sort(key=lambda c: (c["y0"], c["x0"]))
        for i, a in enumerate(clusters):
            for j in range(i + 1, len(clusters)):
                b = clusters[j]
                b_in_a = (b["y0"] >= a["y0"] - 0.8) and (b["y1"] <= a["y1"] + 0.8)
                if abs(max(b["sizes"]) - max(a["sizes"])) > 1.5 and not b_in_a:
                    continue
                if abs(b["y0"] - a["y0"]) > 0.6 + 0.06 * min(max(a["sizes"]), max(b["sizes"])):
                    continue
                thr = min(9.0, max(3.5, 0.75 * max(max(a["sizes"]), max(b["sizes"]))))
                gap = b["x0"] - a["xmax"]
                if not (-2.0 <= gap < thr):
                    continue
                a["runs"].extend(b["runs"])
                a["sizes"].extend(b["sizes"])
                a["x0"] = min(a["x0"], b["x0"])
                a["xmax"] = max(a["xmax"], b["xmax"])
                a["y0"] = min(a["y0"], b["y0"])
                a["y1"] = max(a["y1"], b["y1"])
                clusters.pop(j)
                merged_clusters = True
                break
            if merged_clusters:
                break

    out = []
    for cl in clusters:
        parts = sorted(cl["runs"], key=lambda r: r["x0"])
        text = ""
        prev = None
        for r in parts:
            if prev is not None:
                gap = r["x0"] - prev["x1"]
                if gap > 0.18 * max(prev["size"], r["size"]) and not text.endswith(" "):
                    text += " "
            text += r["text"]
            prev = r
        text = text.strip()
        if not text:
            continue
        nchars = len(text)
        bold_chars = sum(len(r["text"]) for r in parts if r["bold"])
        sizes = [r["size"] for r in parts]
        out.append(
            Line(
                page=pno,
                col=0,
                x0=min(r["x0"] for r in parts),
                y0=cl["y0"],
                x1=max(r["x1"] for r in parts),
                y1=cl["y1"],
                size=round(max(sizes) * 2) / 2,
                bold=bold_chars / max(1, nchars),
                text=text,
                math=_looks_like_math(text, sizes),
            )
        )

    # 右对齐的公式编号与公式本体相距很远，按基线并回同一行（否则会被误判成「编号单独成行」）
    eqno = re.compile(r"^\(\d{1,3}[a-zA-Z]?\)$")
    merged = []
    for l in sorted(out, key=lambda x: (x.y0, x.x0)):
        if merged:
            p = merged[-1]
            if eqno.match(l.text.strip()) and p.math and abs(l.y0 - p.y0) <= 0.8 * max(p.size, l.size):
                p.x1 = max(p.x1, l.x1)
                p.y1 = max(p.y1, l.y1)
                p.text = p.text + " " + l.text.strip()
                p.math = True
                continue
        merged.append(l)
    return merged


def infer_split(lines, page_w):
    """双栏分界：在页面中部找一条「穿过它的行」最少的竖线。"""
    if not lines:
        return None
    lo, hi = page_w * 0.33, page_w * 0.67
    best = None
    for i in range(40):
        x = lo + (hi - lo) * i / 39.0
        crossing = sum(1 for l in lines if l.x0 < x - 2 and l.x1 > x + 2)
        left = sum(1 for l in lines if l.cx < x)
        right = len(lines) - left
        if right < len(lines) * 0.08 or left < len(lines) * 0.08:
            continue
        score = (crossing, abs(left - right), abs(x - page_w / 2))
        if best is None or score < best[0]:
            best = (score, x)
    if best is None:
        return None
    # 单栏版式的自救：单栏页面上几乎每一行都跨过页中线，找不到「干净」的竖线。
    # 若不在这里退出，后面会把 468pt 的版心劈成两个 230pt 的假栏，
    # 于是全文每一行都成了「整宽行」而被寡行检查整体跳过。
    if best[0][0] > max(3, 0.3 * len(lines)):
        return None
    return best[1]


def infer_columns(lines, page_rect):
    page_w = page_rect[2] - page_rect[0]
    split = infer_split(lines, page_w)
    cols = []
    if split is not None:
        # 两道自洽检查：两栏必须有中缝（左栏右边 < 右栏左边），
        # 且左右两侧都真的有自己的左边界。否则视为单栏。
        left_lines = [l for l in lines if l.cx < split and l.nchars > 8]
        right_lines = [l for l in lines if l.cx >= split and l.nchars > 8]
        if left_lines and right_lines:
            l_right = weighted_mode([l.x1 for l in left_lines], [l.nchars for l in left_lines])
            r_left = weighted_mode([l.x0 for l in right_lines], [l.nchars for l in right_lines])
            if l_right >= r_left - 4:
                split = None
        else:
            split = None
    if split is None:
        body = [l for l in lines if l.nchars > 8]
        left = weighted_mode([l.x0 for l in body], [l.nchars for l in body])
        right = weighted_mode([l.x1 for l in body], [l.nchars for l in body])
        cols.append(Column(0, left, max(right, left + 50), 0, 0))
    else:
        for idx, (sel, lo, hi) in enumerate(
            (
                (lambda l: l.cx < split, page_rect[0], split),
                (lambda l: l.cx >= split, split, page_rect[2]),
            )
        ):
            sub = [l for l in lines if sel(l) and l.nchars > 8]
            if not sub:
                cols.append(Column(idx, lo + 10, hi - 10, 0, 0))
                continue
            left = weighted_mode([l.x0 for l in sub], [l.nchars for l in sub])
            right = weighted_mode([l.x1 for l in sub], [l.nchars for l in sub])
            cols.append(Column(idx, left, max(right, left + 40), 0, 0))
    for c in cols:
        c.top = mode_value([l.y0 for l in lines if l.col == c.index], 0, 0.0)
    return cols, split


# --------------------------------------------------------------------------- #
# 浮动体区域（图 / 表）识别：把表格内部的短行排除在寡行检查之外
# --------------------------------------------------------------------------- #
def find_table_regions(page):
    """由横线聚类出表格框。

    booktabs 的 toprule/midrule/bottomrule 跨度几乎一致，所以按「x 跨度」聚：
    同一张表的框线 x0/x1 相同，行高再大也不会把一张表拆成几块。
    返回 [{'x0','x1','y0','y1','n'}]。
    """
    hlines = []
    for d in page.get_drawings():
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.8 and abs(p1.x - p2.x) > 25:
                    hlines.append((min(p1.x, p2.x), p1.y, max(p1.x, p2.x)))
            elif item[0] == "re":
                r = item[1]
                if r.height <= 1.6 and r.width > 25:
                    hlines.append((r.x0, (r.y0 + r.y1) / 2, r.x1))
    groups = []
    for x0, y, x1 in sorted(hlines, key=lambda h: (round(h[0], 0), h[1])):
        for g in groups:
            if abs(x0 - g["x0"]) <= 4 and abs(x1 - g["x1"]) <= 4:
                g["ys"].append(y)
                g["x0"] = min(g["x0"], x0)
                g["x1"] = max(g["x1"], x1)
                break
        else:
            groups.append({"x0": x0, "x1": x1, "ys": [y]})
    out = []
    for g in groups:
        ys = sorted(g["ys"])
        if len(ys) < 2:
            continue
        height = ys[-1] - ys[0]
        if height < 6 or height > 520:
            continue
        out.append({"x0": g["x0"], "x1": g["x1"], "y0": ys[0], "y1": ys[-1], "n": len(ys)})
    return out


CAPTION_RE = re.compile(r"^\s*(Table|Tab\.|TABLE|表)\s*([0-9]+|[IVX]+)\s*[.:：]", re.UNICODE)
FIGCAP_RE = re.compile(r"^\s*(Figure|Fig\.|FIGURE|图)\s*([0-9]+|[IVX]+)\s*[.:：]?", re.UNICODE)


def find_captions(page, lines):
    """返回 [(kind, rect, text)]：表注/图注的锚点行，用于把 issue 和对象对上。"""
    out = []
    for l in lines:
        if CAPTION_RE.match(l.text):
            out.append(("table", l, l.text.strip()))
        elif l.text.strip().startswith(("Figure ", "Fig. ")) and re.match(r"^\s*(Figure|Fig\.)\s*\d", l.text):
            out.append(("figure", l, l.text.strip()))
    return out


def table_region_rects(page):
    return [fitz.Rect(r["x0"], r["y0"] - 2, r["x1"], r["y1"] + 2) for r in find_table_regions(page)]


def figure_rects(page):
    rects = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"])
        if r.width > 40 and r.height > 30:
            rects.append(r)
    return rects


def subfigure_groups(page):
    """把同一 y 带上的多张图归为一组（≥2 张视为多子图）。"""
    imgs = sorted(figure_rects(page), key=lambda r: (r.y0, r.x0))
    groups, cur = [], []
    for r in imgs:
        if not cur:
            cur = [r]
            continue
        union = fitz.Rect(cur[0])
        for q in cur[1:]:
            union |= q
        if r.y0 < union.y1 - 6 and r.y1 > union.y0 + 6:
            cur.append(r)
        else:
            groups.append(cur)
            cur = [r]
    if cur:
        groups.append(cur)
    return groups


def inside(rects, x, y):
    for r in rects:
        if r.x0 - 2 <= x <= r.x1 + 2 and r.y0 - 2 <= y <= r.y1 + 2:
            return True
    return False


# --------------------------------------------------------------------------- #
# 主审计
# --------------------------------------------------------------------------- #
class Auditor:
    def __init__(self, doc, args):
        self.doc = doc
        self.args = args
        self.pages = list(range(len(doc)))
        self.lines_by_page = {}
        self.issues: list[Issue] = []
        self.geometry = {}
        self.body_size = 10.0
        self.text_width = 0.0
        self.body_top = 0.0
        self.body_bottom = 0.0

    # -- 采样 ------------------------------------------------------------- #
    def sample(self):
        all_lines = []
        for pno in self.pages:
            page = self.doc[pno]
            lines = collect_lines(page, pno)
            self.lines_by_page[pno] = lines
            all_lines.extend(lines)
        if not all_lines:
            raise SystemExit("PDF 中没有可提取的文本（可能是扫描件）。")
        rect = self.doc[0].rect
        self.page_rect = (rect.x0, rect.y0, rect.x1, rect.y1)

        sizes = [l.size for l in all_lines if l.nchars > 40]
        self.body_size = mode_value(sizes, 1, 10.0)

        cols, split = infer_columns(all_lines, self.page_rect)
        # 第二遍：按分栏归属重新统计列几何
        if split is not None:
            newcols = []
            for idx, c in enumerate(cols):
                lo = self.page_rect[0] if idx == 0 else split
                hi = split if idx == 0 else self.page_rect[2]
                sub = [l for l in all_lines if lo <= l.cx < hi and l.nchars > 8]
                left = weighted_mode([l.x0 for l in sub], [l.nchars for l in sub], default=c.left)
                right = weighted_mode([l.x1 for l in sub], [l.nchars for l in sub], default=c.right)
                newcols.append(Column(idx, left, right, 0, 0))
            cols = newcols
        else:
            sub = [l for l in all_lines if l.nchars > 8]
            cols = [
                Column(
                    0,
                    weighted_mode([l.x0 for l in sub], [l.nchars for l in sub]),
                    weighted_mode([l.x1 for l in sub], [l.nchars for l in sub]),
                    0,
                    0,
                )
            ]
            split = cols[0].right + 5
        self.cols = cols
        self.split = split
        self.text_width = cols[-1].right - cols[0].left

        # 正文上下边界：逐页用「真正的正文行」（字号=正文字号、够长）的包络，
        # 这样页脚页码、running head 不会把边界撑到纸张边缘；页码用的是「短行 + 落在包络外」的双重判据。
        tops, bots = [], []
        dropped = 0
        for pno, lines in self.lines_by_page.items():
            bodyish = [l for l in lines if abs(l.size - self.body_size) < 0.4 and l.nchars > 20 and not l.math]
            top = min(l.y0 for l in bodyish) if bodyish else self.page_rect[1] + 36
            bot = max(l.y1 for l in bodyish) if bodyish else self.page_rect[3] - 36
            tops.append(top)
            bots.append(bot)
            keep = []
            for l in lines:
                furniture = (l.nchars <= 4 and (l.y0 < top - 2 or l.y1 > bot + 2)) or l.y0 < top - 40 or l.y1 > bot + 40
                if furniture:
                    dropped += 1
                    continue
                keep.append(l)
            self.lines_by_page[pno] = keep
        self.body_top = min(tops) if tops else 0.0
        self.body_bottom = max(bots) if bots else 0.0
        self.furniture_dropped = dropped

        # 行归属分栏
        for lines in self.lines_by_page.values():
            for l in lines:
                l.col = 0 if l.cx < split else min(1, len(cols) - 1)

        self.leading = self._estimate_leading()
        self.char_w = self._estimate_char_width()
        self.tol = max(6.0, 2.4 * self.char_w)  # 判定「短行」的容差

        # 整宽行（标题、单栏摘要、running head、页脚声明）不属于任何一栏，跳过栏内检查
        if len(cols) > 1:
            for lines in self.lines_by_page.values():
                for l in lines:
                    l.full = (l.x1 - l.x0) >= 0.85 * self.text_width

        self.geometry = {
            "pages": len(self.doc),
            "page_size": [round(self.page_rect[2] - self.page_rect[0], 1), round(self.page_rect[3] - self.page_rect[1], 1)],
            "columns": len(cols),
            "column_geom": [
                {"index": c.index, "left": round(c.left, 1), "right": round(c.right, 1), "width": round(c.width, 1)}
                for c in cols
            ],
            "text_width": round(self.text_width, 1),
            "body_font_size": self.body_size,
            "leading": round(self.leading, 2),
            "body_top": round(self.body_top, 1),
            "body_bottom": round(self.body_bottom, 1),
        }

    def _estimate_leading(self) -> float:
        dys = []
        for pno, lines in self.lines_by_page.items():
            for c in range(len(self.cols)):
                seq = sorted([l for l in lines if l.col == c and l.nchars > 12], key=lambda l: l.y0)
                for a, b in zip(seq, seq[1:]):
                    d = b.y0 - a.y0
                    if 4 < d < 30:
                        dys.append(d)
        return mode_value(dys, 1, 12.0)

    def _estimate_char_width(self) -> float:
        ws = [l.w / l.nchars for l in self.lines_by_page.get(0, []) if l.nchars > 30]
        for pno in self.pages:
            ws = [l.w / l.nchars for l in self.lines_by_page[pno] if l.nchars > 30]
            if len(ws) > 20:
                break
        ws.sort()
        return ws[len(ws) // 2] if ws else 4.5

    # -- 判定工具 --------------------------------------------------------- #
    def is_heading(self, l: Line) -> bool:
        if l.size > self.body_size * 1.06:
            return True
        if l.bold > 0.55 and l.nchars < 60 and l.size >= self.body_size - 0.1:
            return True
        return False

    def is_small(self, l: Line) -> bool:
        return l.size < self.body_size - 0.4

    def is_centered(self, l: Line, col: Column) -> bool:
        left_gap = l.x0 - col.left
        right_gap = col.right - l.x1
        return left_gap > col.width * 0.08 and right_gap > col.width * 0.08 and abs(left_gap - right_gap) < col.width * 0.06

    def eligible(self, l: Line, col: Column, excl) -> bool:
        if l.nchars < 1 or l.full:
            return False
        if l.math:
            return False
        if self.is_heading(l) or self.is_small(l):
            return False
        if inside(excl, l.cx, (l.y0 + l.y1) / 2):
            return False
        if self.is_centered(l, col):
            return False
        if l.text.strip().startswith("\\"):
            return False
        return True

    # -- 检查 1/2：寡行与孤行 --------------------------------------------- #
    def check_widows(self):
        self.widow_stats = {"paragraph_ends": 0, "under_min": 0, "under_target": 0, "reported": 0}
        cands = []
        orphans = []
        prev_full_before_col = True  # 上一栏最后一行是否排满：全文第一行没有上文，视为已结束
        for pno in self.pages:
            page = self.doc[pno]
            excl = table_region_rects(page) + figure_rects(page)
            cap_spans = []
            for kind, cl, _t in find_captions(page, self.lines_by_page[pno]):
                cap_spans.append((cl.col, cl.y0 - 2, cl.y0 + 1.8 * self.leading, kind))
            for c in self.cols:
                seq = sorted([l for l in self.lines_by_page[pno] if l.col == c.index], key=lambda l: (l.y0, l.x0))
                seq = [l for l in seq if self.eligible(l, c, excl)]
                if len(seq) < 3:
                    prev_full_before_col = True
                    continue

                for i, l in enumerate(seq):
                    gap = c.right - l.x1
                    is_short = gap > self.tol
                    # 段落末行必须从栏左起排：居中行、缩进行、公式都不是「末行」
                    at_left = (l.x0 - c.left) <= self.tol
                    if not (is_short and at_left):
                        continue
                    # 结构判据（这是把「碎片行」和「真末行」区分开的关键）：
                    # 1) 上一行必须排满 —— 否则本行只是被拆开的碎片（标题里的「2.1」、
                    #    正文里「We」后面接公式的残句、图注的「Figure 1:」标签）。
                    prev = seq[i - 1] if i > 0 else None
                    if prev is not None and (c.right - prev.x1) > self.tol:
                        continue
                    # 2) 同一栏的下一行必须开启新段（首行缩进或标题）——否则本行只是续排。
                    nxt = seq[i + 1] if i + 1 < len(seq) else None
                    if nxt is not None:
                        para_indent = max(6.0, 0.8 * self.body_size)
                        starts_para = (nxt.x0 - c.left) >= para_indent or self.is_heading(nxt)
                        if not starts_para:
                            continue
                    # 3) 末行要以句末标点收尾：公式/缩写折行留下的「where $c_{s,r}$」
                    # 这类续行虽然短且顶格，但不是段落末行。
                    if not re.search(r"[.?!)\]]$", l.text.strip()):
                        continue
                    fill = (l.x1 - c.left) / c.width
                    self.widow_stats["paragraph_ends"] += 1
                    if fill < self.args.min_fill - 0.005:
                        self.widow_stats["under_min"] += 1
                    elif fill < self.args.target_fill - 0.005:
                        self.widow_stats["under_target"] += 1
                    if fill < self.args.target_fill - 0.005:
                        lonely = i == 0 and prev_full_before_col is False
                        in_cap = [k for cc, y0, y1, k in cap_spans if cc == l.col and y0 <= l.y0 <= y1]
                        tag = "（图注末行）" if in_cap and in_cap[0] == "figure" else ("（表注末行）" if in_cap else "")
                        if lonely:
                            detail = "段落末行孤悬在栏/页首：上一栏还在续排，读者先看到半句话的尾巴"
                        else:
                            detail = f"段落末行只占栏宽 {fill*100:.0f}%（硬阈值 {self.args.min_fill*100:.0f}%，目标 {self.args.target_fill*100:.0f}%）"
                        cands.append(
                            Issue(
                                cat="widow",
                                severity="P0" if (lonely or fill < self.args.min_fill) else "P2",
                                page=pno + 1,
                                col=c.index + 1,
                                metric=f"末行占比 {fill*100:.1f}%",
                                detail=detail + tag,
                                suggest="首选删词 / 长短表述替换（in the way of→by 这类），把这条末行整行消掉；其次 {\\looseness=-1}、\\enlargethispage；把它填长只算最后手段",
                                bbox=(l.x0, l.y0, l.x1, l.y1),
                                extra={"fill": round(fill, 3), "text": l.text.strip()[:70], "lonely": lonely, "caption": bool(in_cap)},
                            )
                        )

                # 本栏最后一行的「排满」状态，供下一栏判断「末行孤悬在栏首」
                prev_full_before_col = (c.right - seq[-1].x1) <= self.tol

                # 孤行：段落首行落在栏尾（有未来、没有过去）
                last, second_last = seq[-1], seq[-2]
                if (c.right - second_last.x1) > self.tol and (c.right - last.x1) <= self.tol:
                    if not self.is_heading(last):
                        orphans.append(
                            Issue(
                                cat="orphan",
                                severity="P1",
                                page=pno + 1,
                                col=c.index + 1,
                                metric="段落首行落在栏尾",
                                detail="栏底只留下段落的第一行，剩下整段跑到下一栏",
                                suggest="\\needspace{2\\baselineskip} / \\enlargethispage{\\baselineskip} / 微调上一段措辞把这一行挪下去",
                                bbox=(last.x0, last.y0, last.x1, last.y1),
                                extra={"text": last.text.strip()[:70]},
                            )
                        )

        # 按「末行越短越靠前」排，超过上限只保留最狠的若干条，其余进汇总
        cands.sort(key=lambda i: (i.extra["fill"], i.page, i.col))
        limit = self.args.max_widows
        self.widow_stats["reported"] = min(len(cands), limit)
        self.widow_stats["total"] = len(cands)
        self.widow_stats["median_fill"] = (
            round(sorted(c.extra["fill"] for c in cands)[len(cands) // 2], 3) if cands else None
        )
        self.issues.extend(cands[:limit])
        self.issues.extend(orphans[: max(3, limit // 5)])

    # -- 检查 3：越界与公式编号掉行 --------------------------------------- #
    def check_overflow(self):
        for pno in self.pages:
            prev = None
            block = []  # 竖直相邻的越界行：一个公式被拆成多段时只报一条

            def flush():
                if not block:
                    return
                worst = max(block, key=lambda b: b[1])
                x0 = min(b[0].x0 for b in block)
                x1 = max(b[0].x1 for b in block)
                y0 = min(b[0].y0 for b in block)
                y1 = max(b[0].y1 for b in block)
                c = self.cols[worst[0].col]
                formulas = any(b[0].math for b in block)
                if len(block) > 1 and formulas:
                    what = f"公式（{len(block)} 行）"
                    detail = f"公式越出栏右边界，最远 {worst[1]:.1f}pt（含上下标碎片）"
                    metric = f"右溢 {worst[1]:.1f}pt"
                else:
                    what = "行内容"
                    detail = f"{what}越过栏右边界（{worst[0].text.strip()[:60]}）"
                    metric = f"右溢 {worst[1]:.1f}pt"
                self.issues.append(
                    Issue(
                        cat="overflow",
                        severity="P0" if worst[1] > 4 else "P1",
                        page=pno + 1,
                        col=c.index + 1,
                        metric=metric,
                        detail=detail,
                        suggest="公式用 align 拆分 / 图缩到 \\linewidth / 长 URL 用 \\url 允许断行",
                        bbox=(x0, y0, x1, y1),
                    )
                )
                del block[:]

            prev_math = None  # 最近一条公式行，用于判断「编号是否被公式挤下来」
            text_left = self.cols[0].left
            text_right = self.cols[-1].right
            page_mid = (text_left + text_right) / 2
            for l in sorted(self.lines_by_page[pno], key=lambda x: (x.y0, x.x0)):
                if l.nchars == 0 or l.full:
                    continue
                c = self.cols[l.col]
                centered = abs((l.x0 + l.x1) / 2 - page_mid) < 0.04 * self.text_width
                starts_left = (l.x0 - c.left) <= max(self.tol, 0.1 * c.width)
                if centered:
                    # 居中的整宽元素（标题、running head、版权声明）：只和版心宽度比，
                    # 越出版心才是真问题；拿来跟单栏比全是假阳性。
                    over = l.x1 - text_right
                elif l.math or starts_left:
                    # 公式通常居中、不从栏左起排，所以 math 行不看 starts_left
                    over = l.x1 - c.right
                else:
                    # 既不是栏内正文，也不是居中元素（如跨栏浮动块里的碎片）：跳过
                    over = -1.0
                if over > 2.0 and l.nchars > 2 and l.nchars < 400:
                    if block and (l.y0 - max(b[0].y1 for b in block)) > 2.5 * self.leading:
                        flush()
                    block.append((l, over))
                elif not l.math:
                    # 公式中间夹着的非越界碎片（如孤立下标 i、j）不切断同一个公式块
                    flush()
                # 编号单独成行 = 公式吃满栏宽，编号被挤到下一行（比越界更隐蔽）。
                # 只有当「上一行确实是公式、且宽到几乎没有余量」时才成立，否则只是版式噪音。
                lone_eqno = re.fullmatch(r"\(\d{1,3}[a-zA-Z]?\)", l.text.strip())
                if lone_eqno and prev_math is not None:
                    gap = l.y0 - prev_math.y1
                    wide = (prev_math.x1 - c.left) > 0.88 * c.width
                    if -2.0 <= gap <= 3.0 * self.leading and wide:
                        self.issues.append(
                            Issue(
                                cat="overflow",
                                severity="P0",
                                page=pno + 1,
                                col=c.index + 1,
                                metric="公式编号单独占行",
                                detail=f"编号 {l.text.strip()} 被挤到公式下方独立一行："
                                f"公式宽度已占栏宽 {(prev_math.x1-c.left)/c.width*100:.0f}%，没有给编号留位",
                                suggest="公式整体宽度要略小于栏宽（给编号留位）：\\begin{align} + & 在 = 或 + 处拆行",
                                bbox=(l.x0, l.y0, l.x1, l.y1),
                            )
                        )
                if not l.math:
                    prev = l
                elif not centered:
                    prev_math = l
            flush()

    # -- 检查 4：栏底空白 ------------------------------------------------- #
    def check_bottom(self):
        last_page = self.pages[-1]
        for pno in self.pages:
            if pno == last_page:
                continue
            page = self.doc[pno]
            excl = table_region_rects(page) + figure_rects(page)
            for c in self.cols:
                seq = [
                    l
                    for l in self.lines_by_page[pno]
                    if l.col == c.index and not l.full and l.y1 <= self.body_bottom + 3
                ]
                seq = [l for l in seq if not inside(excl, l.cx, l.y0)]
                if len(seq) < 6:
                    continue
                y_last = max(l.y1 for l in seq)
                # 该栏下半部分若被整宽块（标题/摘要/跨栏图）占据，栏底空白是版式使然
                fulls = [l for l in self.lines_by_page[pno] if l.full]
                if fulls and max(f.y1 for f in fulls) > y_last:
                    continue
                hole = self.body_bottom - y_last
                if hole > 0.12 * (self.body_bottom - self.body_top):
                    self.issues.append(
                        Issue(
                            cat="bottom",
                            severity="P1" if hole > 0.2 * (self.body_bottom - self.body_top) else "P2",
                            page=pno + 1,
                            col=c.index + 1,
                            metric=f"栏底空白 {hole:.0f}pt（{hole/(self.body_bottom-self.body_top)*100:.0f}% 栏高）",
                            detail="栏底留下一条整块空白，破坏「矩形色块」轮廓",
                            suggest="调整 float 位置 / \\enlargethispage / 图文重排填满，而不是留白",
                            bbox=(c.left, y_last, c.right, self.body_bottom),
                        )
                    )

    # -- 检查 5：图 ------------------------------------------------------- #
    def check_figures(self):
        for pno in self.pages:
            page = self.doc[pno]
            # 多子图交给 check_subfigures 统一判（否则「子图各占 49%」会被逐张报一遍）
            grouped = set()
            for g in subfigure_groups(page):
                if len(g) >= 2:
                    for q in g:
                        grouped.add((round(q.x0, 1), round(q.y0, 1), round(q.x1, 1), round(q.y1, 1)))
            for r in figure_rects(page):
                if (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)) in grouped:
                    continue

                full = r.width > self.cols[-1].right - self.cols[0].left + 2
                ref_w = self.text_width if full else min(c.width for c in self.cols)
                ratio = r.width / ref_w
                if full and r.width > self.text_width + 1:
                    sev, note = "P0", f"全宽图越出版心宽 {r.width - self.text_width:.1f}pt"
                elif 0.45 < ratio < 0.9:
                    sev, note = "P2", f"图未填满{'版心' if full else '栏'}宽：占 {ratio*100:.0f}%"
                elif ratio <= 0.45:
                    sev, note = "info", f"小图：占 {ratio*100:.0f}% 宽（若属装饰性/示意性可忽略）"
                else:
                    sev, note = None, ""
                if sev:
                    self.issues.append(
                        Issue(
                            cat="figure",
                            severity=sev,
                            page=pno + 1,
                            col=1 if r.x0 < self.split else 2,
                            metric=f"宽 {r.width:.0f}pt / 可用 {ref_w:.0f}pt",
                            detail=note,
                            suggest="\\includegraphics[width=\\linewidth] 撑满；或把多子图拼成一条长图",
                            bbox=(r.x0, r.y0, r.x1, r.y1),
                            extra={"aspect_h_over_w": round(r.height / r.width, 2)},
                        )
                    )
                if r.height > r.width * 1.05 and r.width > 0.5 * min(c.width for c in self.cols):
                    self.issues.append(
                        Issue(
                            cat="figure",
                            severity="P2",
                            page=pno + 1,
                            col=1 if r.x0 < self.split else 2,
                            metric=f"竖向图 {r.width:.0f}×{r.height:.0f}",
                            detail="高大于宽，版面里会留下左右两条空白",
                            suggest="重排成横向长条（≥4:3，越长越好），或拆成并排多子图",
                            bbox=(r.x0, r.y0, r.x1, r.y1),
                        )
                    )

    # -- 检查 5b：多子图 -------------------------------------------------- #

    def check_subfigures(self):
        """一图多子图：子图之间的空白、子图上方的空带、子图整体没撑满。

        原文的判据是「图要像一块矩形色块」：子图之间只留一条细缝，上方不留空带。
        """
        for pno in self.pages:
            page = self.doc[pno]
            for grp in subfigure_groups(page):
                if len(grp) < 2:
                    continue
                rects = sorted(grp, key=lambda r: r.x0)
                band = fitz.Rect(rects[0])
                for r in rects[1:]:
                    band |= r
                gaps = [round(rects[i + 1].x0 - rects[i].x1, 1) for i in range(len(rects) - 1)]
                col_idx = 1 if band.x0 < self.split else 2
                col = self.cols[col_idx - 1]
                if band.width < 0.9 * col.width:
                    self.issues.append(
                        Issue(
                            cat="figure",
                            severity="P2",
                            page=pno + 1,
                            col=col_idx,
                            metric=f"{len(rects)} 子图整体占栏宽 {band.width / col.width * 100:.0f}%",
                            detail="多子图加起来没撑满栏宽，两侧留白不匀",
                            suggest="每张子图用 width=0.49\\linewidth + 4pt 间距，整体吃掉整栏",
                            bbox=(band.x0, band.y0, band.x1, band.y1),
                            extra={"gaps": gaps, "subfigures": len(rects)},
                        )
                    )
                if gaps and (max(gaps) > 12 or max(gaps) - min(gaps) > 6):
                    self.issues.append(
                        Issue(
                            cat="figure",
                            severity="P2",
                            page=pno + 1,
                            col=col_idx,
                            metric=f"{len(rects)} 子图，间距 {min(gaps):.0f}–{max(gaps):.0f}pt",
                            detail="子图之间留白过宽或宽窄不一，整块图不再是一块连续的色块",
                            suggest="子图用同一 \\includegraphics 宽度 + 固定间距（或直接拼成一张长图），间距 ≤ 8pt",
                            bbox=(band.x0, band.y0, band.x1, band.y1),
                            extra={"gaps": gaps, "subfigures": len(rects)},
                        )
                    )
                # 子图上方的空带：往上找同一栏的正文行
                prev_bottom = None
                for l in self.lines_by_page[pno]:
                    if l.col != col.index or l.y1 > band.y0 - 1:
                        continue
                    if prev_bottom is None or l.y1 > prev_bottom:
                        prev_bottom = l.y1
                if prev_bottom is not None and band.y0 - prev_bottom > 2.2 * self.leading:
                    self.issues.append(
                        Issue(
                            cat="figure",
                            severity="P2",
                            page=pno + 1,
                            col=col_idx,
                            metric=f"子图上空 {band.y0 - prev_bottom:.0f}pt",
                            detail="多子图整体上方留了一条空带（排版空白 + 图片自带留白）",
                            suggest="裁掉图片自身的白边（bbox_inches='tight'），并让子图整体贴住上一段正文",
                            bbox=(band.x0, prev_bottom, band.x1, band.y0),
                            extra={"subfigures": len(rects)},
                        )
                    )

    # -- 检查 5c：列表 ---------------------------------------------------- #

    def check_lists(self):
        """itemize/enumerate：左侧缩进 + 每个条目末行不满，是最像「凑篇幅」的一类空白。"""
        markers = ("•", "·", "◦", "‣", "▪", "–", "-")
        for pno in self.pages:
            for c in self.cols:
                seq = sorted([l for l in self.lines_by_page[pno] if l.col == c.index],
                             key=lambda l: (l.y0, l.x0))
                bullets = [l for l in seq if l.text.lstrip()[:1] in markers and l.nchars > 1]
                if len(bullets) < 2:
                    continue
                # 同一列表：bullet 起点 x 相同、相邻 bullet 之间纵向连续（间隔离谱就断开）
                lists, cur = [], [bullets[0]]
                for b in bullets[1:]:
                    same_x = abs(b.x0 - cur[0].x0) <= 3.0
                    gap = b.y0 - cur[-1].y0
                    if same_x and gap < 25 * self.leading:
                        cur.append(b)
                    else:
                        lists.append(cur)
                        cur = [b]
                lists.append(cur)

                for grp in lists:
                    if len(grp) < 2:
                        continue
                    indent = grp[0].x0 - c.left
                    # 每个条目的末行：bullet 之后、下一个 bullet（或正文新段）之前的最后一个行
                    fills = []
                    for k, first in enumerate(grp):
                        stop = grp[k + 1].y0 - 1 if k + 1 < len(grp) else first.y0 + 12 * self.leading
                        body = [l for l in seq
                                if first.y0 - 0.5 <= l.y0 < stop and l.x0 >= first.x0 - 1
                                and not self.is_heading(l)]
                        if not body:
                            continue
                        last = max(body, key=lambda l: l.y0)
                        fills.append((last.x1 - c.left) / c.width)
                    if not fills:
                        continue
                    under = sum(1 for f in fills if f < self.args.min_fill - 0.005)
                    med = sorted(fills)[len(fills) // 2]
                    if under == 0 and indent <= 8:
                        continue
                    sev = "P1" if under else "P2"
                    detail = (f"列表共 {len(grp)} 项，左边距 {indent:.0f}pt"
                              f"（正文顶格在 {c.left:.0f}pt）")
                    if under:
                        detail += f"；其中 {under} 项的末行不足 {self.args.min_fill*100:.0f}%"
                    self.issues.append(
                        Issue(
                            cat="list",
                            severity=sev,
                            page=pno + 1,
                            col=c.index + 1,
                            metric=f"末行占比中位 {med*100:.0f}%，左边距 {indent:.0f}pt",
                            detail=detail,
                            suggest=("\\usepackage{enumitem} + \\setlist[itemize]{leftmargin=*,itemsep=1pt} 去左缩进；"
                                     "每条缩短到末行 ≥50%（删词优先，不要加字），条目少时直接改 \\paragraph 或并成一段"),
                            bbox=(grp[0].x0, grp[0].y0, c.right, max(l.y1 for l in grp)),
                            extra={"items": len(grp), "under_min": under,
                                   "fills": [round(f, 3) for f in fills], "indent": round(indent, 1)},
                        )
                    )

    # -- 检查 6：表 ------------------------------------------------------- #
    def check_tables(self):
        per_table = []
        for pno in self.pages:
            page = self.doc[pno]
            regions = find_table_regions(page)
            lines = self.lines_by_page[pno]
            caps = [c for c in find_captions(page, lines) if c[0] == "table"]
            for reg in regions:
                rect = fitz.Rect(reg["x0"], reg["y0"], reg["x1"], reg["y1"])
                inside_lines = [l for l in lines if rect.x0 <= l.cx <= rect.x1 and rect.y0 - 12 <= l.y0 <= rect.y1 + 3]
                if not inside_lines:
                    continue
                # 表格实际宽度 = 文字与横线的并集
                w = max(rect.width, max(l.x1 for l in inside_lines) - min(l.x0 for l in inside_lines))
                c_assign = 1 if rect.x0 < self.split else 2
                col_w = self.cols[c_assign - 1].width
                ratio = w / col_w
                if ratio > 1.02:
                    continue  # 跨栏表，交给全宽图检查
                cap = None
                for _k, cl, text in caps:
                    # 表注可在表上方（\caption 在 tabular 前）或下方；取纵向最近的一条
                    if cl.col == c_assign - 1 and -70 <= rect.y0 - cl.y0 <= 45:
                        if cap is None or abs(rect.y0 - cl.y0) < abs(rect.y0 - cap[0].y0):
                            cap = (cl, text)
                capnote = f"（{cap[1][:28]}…）" if cap and len(cap[1]) > 28 else (f"（{cap[1]}）" if cap else "")
                if ratio < 0.92:
                    self.issues.append(
                        Issue(
                            cat="table",
                            severity="P1" if ratio < 0.75 else "P2",
                            page=pno + 1,
                            col=c_assign,
                            metric=f"表宽 {w:.0f}pt / 栏宽 {col_w:.0f}pt（{ratio*100:.0f}%）",
                            detail=f"表格没撑到页边距，两侧各留约 {(col_w-w)/2:.0f}pt 空白{capnote}",
                            suggest="tabular* + @{\\extracolsep{\\fill}}；或增加列内文字/调整 \\tabcolsep 撑满栏宽",
                            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                        )
                    )
                sizes = [l.size for l in inside_lines if l.nchars > 1]
                if sizes:
                    per_table.append((pno + 1, round(mode_value(sizes, 1), 1), rect))
        if per_table:
            values = [v for _, v, _ in per_table]
            lo, hi = min(values), max(values)
            if hi - lo > 0.6:
                worst_page, worst_size, worst_rect = max(per_table, key=lambda t: t[1])
                listing = "、".join(f"p{p} 表 {s}pt" for p, s, _r in per_table)
                self.issues.append(
                    Issue(
                        cat="table",
                        severity="P2",
                        page=worst_page,
                        col=1 if worst_rect.x0 < self.split else 2,
                        metric=f"表内字号 {lo:g}–{hi:g}pt 不统一",
                        detail=f"全文 {len(per_table)} 张表字号不一致（{listing}），最容易被读成「准备草率」",
                        suggest="统一 \\small / \\footnotesize，全表用同一个字号宏",
                        bbox=(worst_rect.x0, worst_rect.y0, worst_rect.x1, worst_rect.y1),
                    )
                )
            for page_no, size, rect in per_table:
                if size >= self.body_size - 0.1:
                    self.issues.append(
                        Issue(
                            cat="table",
                            severity="P3",
                            page=page_no,
                            col=1 if rect.x0 < self.split else 2,
                            metric=f"表内字号 {size}pt ≈ 正文 {self.body_size}pt",
                            detail="表内字号未略小于正文",
                            suggest="\\small 或 \\footnotesize（全文统一）",
                            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                        )
                    )

    # -- 检查 7：源码 ----------------------------------------------------- #
    def check_source(self, tex_root: Path):
        files = []
        if tex_root.is_file():
            files = [tex_root]
        else:
            files = sorted(tex_root.rglob("*.tex"))
        if not files:
            return
        subsubsection, texttt, camel, itemize = [], [], [], []
        for f in files:
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            in_verbatim = False
            for no, raw in enumerate(src.splitlines(), 1):
                line = re.sub(r"(?<!\\)%.*$", "", raw)
                if re.search(r"\\begin\{(verbatim|lstlisting|minted)\}", line):
                    in_verbatim = True
                if re.search(r"\\end\{(verbatim|lstlisting|minted)\}", line):
                    in_verbatim = False
                    continue
                if in_verbatim:
                    continue
                if "\\subsubsection" in line:
                    subsubsection.append(f"{f.name}:{no}")
                for m in re.finditer(r"\\texttt\{([^}]*)\}", line):
                    texttt.append(f"{f.name}:{no} {m.group(1)[:40]}")
                for m in re.finditer(r"(?<![\w{$\\)])([A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)\(\)", line):
                    camel.append(f"{f.name}:{no} {m.group(1)}()")
                if "\\begin{itemize}" in line:
                    itemize.append(f"{f.name}:{no}")
        if subsubsection:
            for loc in subsubsection:
                self.issues.append(
                    Issue(
                        cat="source",
                        severity="P2",
                        page=0,
                        col=0,
                        metric="\\subsubsection",
                        detail=f"{loc}：三级以下细分，渲染时容易在标题之间拉出大段空白",
                        suggest="改用 \\paragraph{...} 或 \\noindent\\textbf{...} 行内小标题",
                    )
                )
        if texttt:
            self.issues.append(
                Issue(
                    cat="source",
                    severity="P3",
                    page=0,
                    col=0,
                    metric=f"\\texttt × {len(texttt)}",
                    detail="\\texttt 内的文字不参与断词，会把整行词距撑开（示例：" + "; ".join(texttt[:3]) + "）",
                    suggest="方法名改用数学符号；确需代码体时用 \\url/\\path 允许断行",
                )
            )
        if camel:
            self.issues.append(
                Issue(
                    cat="source",
                    severity="P3",
                    page=0,
                    col=0,
                    metric=f"代码式标识符 × {len(camel)}",
                    detail="正文出现 CrossAttn() 这类 repo 式写法：" + "; ".join(camel[:4]),
                    suggest="Method 里给数学表达（如 $f_\\theta(\\cdot)$），不要给函数名",
                )
            )
        if itemize:
            compact = 0
            for f in files:
                try:
                    s = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "enumitem" in s or "itemsep" in s or "\\setlist" in s:
                    compact += 1
            # 版面层已经报过列表问题就不再重复（同一条 itemize 不报两遍）
            if compact == 0 and not any(i.cat == "list" for i in self.issues):
                self.issues.append(
                    Issue(
                        cat="source",
                        severity="P3",
                        page=0,
                        col=0,
                        metric=f"itemize × {len(itemize)}，无紧凑设置",
                        detail="默认列表有左缩进与条目间距，视觉上像在凑篇幅",
                        suggest="\\usepackage{enumitem} + \\setlist[itemize]{leftmargin=*,topsep=2pt,itemsep=1pt,parsep=0pt}",
                    )
                )

    # -- 汇总 ------------------------------------------------------------- #
    def run(self, tex_root=None):
        self.sample()
        self.check_widows()
        self.check_overflow()
        self.check_bottom()
        self.check_figures()
        self.check_subfigures()
        self.check_lists()
        self.check_tables()
        if tex_root:
            self.check_source(Path(tex_root))
        self.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.page, i.col))
        for n, issue in enumerate(self.issues, 1):
            issue.extra.setdefault("id", f"{issue.cat[:3].upper()}-{n:02d}")
        return self.issues


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def to_markdown(aud: Auditor, issues, pdf_path) -> str:
    g = aud.geometry
    counts = Counter(i.severity for i in issues)
    lines = []
    lines.append(f"# 版面审计：{Path(pdf_path).name}")
    lines.append("")
    lines.append(
        f"- 页数 {g['pages']}｜版面 {g['page_size'][0]:.0f}×{g['page_size'][1]:.0f}pt｜"
        f"{g['columns']} 栏｜栏宽 {g['column_geom'][0]['width']:.1f}pt｜版心宽 {g['text_width']:.1f}pt"
    )
    lines.append(f"- 正文字号 {g['body_font_size']}pt｜行距 {g['leading']:.1f}pt｜判「短行」容差 {aud.tol:.1f}pt")
    ws = getattr(aud, "widow_stats", None)
    if ws and ws.get("paragraph_ends"):
        med = ws.get("median_fill")
        lines.append(
            f"- 段落末行：共检出 {ws['paragraph_ends']} 个段落结尾"
            f"｜<{aud.args.min_fill*100:.0f}% 的 {ws['under_min']} 条"
            f"｜{aud.args.min_fill*100:.0f}–{aud.args.target_fill*100:.0f}% 的 {ws['under_target']} 条"
            + (f"｜中位占比 {med*100:.0f}%" if med is not None else "")
        )
        if ws.get("total", 0) > ws.get("reported", 0):
            lines.append(
                f"- 末行问题共 {ws['total']} 条，本报告按「越短越靠前」只列前 {ws['reported']} 条（--max-widows 可调）"
            )
    lines.append(
        f"- 候选问题 P0 {counts.get('P0', 0)}｜P1 {counts.get('P1', 0)}｜P2 {counts.get('P2', 0)}｜P3 {counts.get('P3', 0)}"
    )
    lines.append("")
    lines.append("> 脚本只给候选证据，判定权在你：先看裁剪图，再决定哪条是真问题。")
    lines.append("")
    by_cat = {}
    for i in issues:
        by_cat.setdefault(i.cat, []).append(i)
    if "widow" in by_cat:
        by_cat["widow"].sort(key=lambda i: i.extra.get("fill", 1.0))
    names = {
        "widow": "寡行 / 矮行（段落末行）",
        "orphan": "孤行（段落首行掉栏尾）",
        "overflow": "越界与公式编号",
        "bottom": "栏底空白",
        "figure": "图",
        "list": "列表（itemize/enumerate）",
        "table": "表",
        "source": "源码级",
    }
    for cat, items in by_cat.items():
        lines.append(f"## {names.get(cat, cat)}（{len(items)}）")
        lines.append("")
        lines.append("| ID | 严重度 | 页 | 栏 | 实测 | 说明 | 建议 |")
        lines.append("|---|---|---|---|---|---|---|")
        for i in items:
            page = "-" if i.page == 0 else str(i.page)
            col = "-" if i.col == 0 else str(i.col)
            detail = i.detail.replace("|", "\\|")
            lines.append(
                f"| {i.extra.get('id','')} | {i.severity} | {page} | {col} | {i.metric} | {detail} | {i.suggest} |"
            )
        lines.append("")
        crops = [i for i in items if i.extra.get("crop")]
        if crops:
            lines.append(f"### {names.get(cat, cat)}·裁剪预览")
            lines.append("")
            for i in crops:
                lines.append(f"**{i.extra.get('id','')}**（P{i.page} 栏 {i.col}）{i.metric}")
                lines.append("")
                lines.append(f"![{i.extra['id']}](crops/{i.extra['crop']})")
                lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="论文版面「不均匀空白」量化审计")
    ap.add_argument("--pdf", required=True, help="编译好的 PDF")
    ap.add_argument("--tex", help="LaTeX 源（目录或单个 .tex），用于源码级检查")
    ap.add_argument("--json", help="写出 JSON 结果")
    ap.add_argument("--md", help="写出 Markdown 报告")
    ap.add_argument("--pages", help="只审计指定页，如 1-4,7")
    ap.add_argument("--max-widows", type=int, default=25, help="最多列出多少条寡行候选（默认 25，其余只汇总）")
    ap.add_argument("--crops", help="把图/表的裁剪图输出到该目录（报告里会引用 crops/xxx.png）")
    ap.add_argument("--crop-zoom", type=float, default=2.0)
    ap.add_argument("--min-fill", type=float, default=0.5, help="末行占比硬阈值（默认 0.5）")
    ap.add_argument("--target-fill", type=float, default=0.7, help="末行占比目标阈值（默认 0.7）")
    ap.add_argument("--render", help="逐页渲染缩略图到该目录（配合 --zoom 0.5 做轮廓检查）")
    ap.add_argument("--zoom", type=float, default=0.5, help="--render 的缩放比")
    ap.add_argument("--crop", help="裁剪局部：PAGE:x0,y0,x1,y1（页号从 1 起）")
    ap.add_argument("--crop-out", help="裁剪输出 PNG")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        doc = fitz.open(args.pdf)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"打不开 PDF：{exc}\n")
        return 2

    if args.crop:
        if not args.crop_out:
            sys.stderr.write("--crop 需要 --crop-out\n")
            return 2
        try:
            page_s, coords = args.crop.split(":", 1)
            page = doc[int(page_s) - 1]
            x0, y0, x1, y1 = [float(v) for v in coords.split(",")]
        except Exception:  # noqa: BLE001
            sys.stderr.write("--crop 格式应为 PAGE:x0,y0,x1,y1\n")
            return 2
        pix = page.get_pixmap(matrix=fitz.Matrix(args.crop_zoom, args.crop_zoom), clip=fitz.Rect(x0, y0, x1, y1))
        Path(args.crop_out).parent.mkdir(parents=True, exist_ok=True)
        pix.save(args.crop_out)
        if not args.quiet:
            print(f"裁剪已保存：{args.crop_out}（{pix.width}×{pix.height}px）")
        return 0

    aud = Auditor(doc, args)
    if args.pages:
        sel = set()
        for part in args.pages.split(","):
            if "-" in part:
                a, b = part.split("-")
                sel.update(range(int(a) - 1, int(b)))
            else:
                sel.add(int(part) - 1)
        aud.pages = [p for p in aud.pages if p in sel]
    issues = aud.run(args.tex)

    if args.crops:
        outdir = Path(args.crops)
        outdir.mkdir(parents=True, exist_ok=True)
        n = 0
        for i in issues:
            if i.cat not in ("figure", "table") or not i.bbox:
                continue
            pad = 6
            clip = fitz.Rect(i.bbox[0] - pad, i.bbox[1] - pad, i.bbox[2] + pad, i.bbox[3] + pad)
            pix = doc[i.page - 1].get_pixmap(matrix=fitz.Matrix(args.crop_zoom, args.crop_zoom), clip=clip)
            name = f"{i.extra.get('id','obj').lower()}-p{i.page}.png"
            pix.save(outdir / name)
            i.extra["crop"] = name
            n += 1
        if not args.quiet:
            print(f"裁剪图：{n} 张 → {outdir}")

    if args.render:
        out = Path(args.render)
        out.mkdir(parents=True, exist_ok=True)
        for pno in aud.pages:
            pix = doc[pno].get_pixmap(matrix=fitz.Matrix(args.zoom, args.zoom))
            pix.save(out / f"page-{pno+1:03d}.png")
        if not args.quiet:
            print(f"缩略图：{out}（{len(aud.pages)} 页 @ {args.zoom:g}x）")

    md = to_markdown(aud, issues, args.pdf)
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(md, encoding="utf-8")
    if args.json:
        payload = {
            "pdf": str(Path(args.pdf).resolve()),
            "geometry": aud.geometry,
            "short_line_tolerance": round(aud.tol, 2),
            "issues": [i.as_dict() for i in issues],
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(md)
    return 1 if any(i.severity == "P0" for i in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
