# LaTeX 修法手册

原则：**先改内容与构图，再动版式宏。** 每个修法都标注了副作用——版式手段几乎都会把问题推到别处，所以改完必须复跑审计。

---

## 1. 寡行（末行太短）

### 1.1 首选：把末行压回上一行（删词 / 换短表述）

英文学术写作里有一批"可压缩但不减信息"的替换，效果相当于 slider——需要短就往左滑，需要长就往右滑：

| 长表述 | 短表述 | 省下 |
|---|---|---|
| `in the way of` | `by` | 3 词 |
| `in the manner of` | `via` | 3 词 |
| `in order to` | `to` | 2 词 |
| `due to the fact that` | `because` | 4 词 |
| `a large number of` | `many` | 3 词 |
| `is able to` | `can` | 2 词 |
| `with respect to` | `for` | 2 词 |
| `as a consequence of` | `from` | 3 词 |
| `it is worth noting that` | （整句删） | 5 词 |
| `the proposed method` | `our method` / `we` | 2 词 |

同样适用于定语从句（`which is used to` → `to`）、名词（`utilization` → `use`）、动词（`demonstrates` → `shows`）。

**反过来也要会用**：末行只差半个词就够一行，加一个限定词（`significantly`、`in practice`）就能让末行变长——但只加**有信息量**的词，不要灌水。

### 1.2 `\looseness`

```latex
{\looseness=-1
This paragraph is currently one line too long...\par}
```

`\looseness=-1` 要求 TeX 把这段排短一行，`=1` 排长一行。副作用：整段词距会被拉伸，且可能把问题转移到下一段。**只对个别段落用，用完复跑。**

### 1.3 `\enlargethispage`

```latex
\enlargethispage{\baselineskip}   % 本页多排一行
\enlargethispage{-\baselineskip}  % 本页少排一行
```

适合"整段落差一行就能收进本栏/本页"的情况。副作用：改变本页文字基线，可能让页底与下一页的边距不齐——只在栏底本来就有余量时用。

### 1.4 结构手段

- 把段末一句并进上一句（用分号或 `and`），直接消灭这一行；
- 把长段落拆成两段（拆点选在语义转折处），两段的末行都比原来长；
- 段落最后一句整句掉到下一栏时：**不要**硬压，改为调整该栏上方图/表的高度（加/减 `\vspace`、改插图宽高比），让整句能落进本栏。

### 1.5 兜底（慎用）

```latex
\parfillskip=0pt plus 1fil   % 末行也两端对齐
```

视觉上"填满了"，但会把一行内的词距拉得很开，比原来更难看。仅在末行几乎占满（> 90%）时考虑。

---

## 2. 结果图

```latex
% 撑满栏宽（单栏图）
\includegraphics[width=\linewidth]{figs/result.pdf}

% 撑满版心（跨栏图）
\begin{figure*}[t]
  \includegraphics[width=\textwidth]{figs/result.pdf}
\end{figure*}
```

- **等比缩放**：只给 `width`，不要同时给 `height`（除非 `keepaspectratio`）。
- **长条构图**：matplotlib 里把 `figsize` 调成 `(7, 2.2)` 这类横向比例，而不是 `(6, 5)`，再导出 PDF/SVG（矢量，缩放不失真）。
- **legend 移出绘图区**：`ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left")` 或放到图下方一行；绝不半透明压在曲线上。
- **子图刻度统一**：`sharex`/`sharey`，或手动统一 `set_yticks` 的区间与步长；导出前检查两个子图的实际高度一致。
- **图注**：`\caption` 与图之间不留空行，图注字号用 `\small` 与表注保持一致。

---

## 3. 表格

```latex
% 撑满栏宽（列间距自动拉伸）
\begin{tabular*}{\linewidth}{@{\extracolsep{\fill}}lcccc@{}}
\toprule
...
\bottomrule
\end{tabular*}
```

- **宽度不够时**：先增加列内文字（`Method A (ours)` → 完整方法名），再调 `\tabcolsep`，最后才考虑 `\small`。
- **不要用 `\resizebox` 缩表**：它会把表内字号缩到与其它表不一致，违反「全文表字号统一」。
- **表头分组**：

```latex
\toprule
\multirow{2}{*}{Dataset} & \multicolumn{2}{c}{Top-1 (\%)} & \multicolumn{2}{c}{FLOPs (G)} \\
\cmidrule(lr){2-3}\cmidrule(lr){4-5}
 & Base & Ours & Base & Ours \\
\midrule
```

- **字号**：全文统一 `\small`（或统一 `\footnotesize`），不要一张表一个字号。
- **边距**：`\setlength{\tabcolsep}{4pt}` 全局设置一次，别在单表里改。

---

## 4. 公式

### 4.1 用 `align` 在算子处拆行

```latex
\begin{align}
\mathcal{L} &= \mathbb{E}_{x\sim p}\left[\log p_\theta(y\mid x)\right] \nonumber \\
            &\quad + \lambda \sum_{i=1}^{N} \|z_i - \mu\|^2
\end{align}
```

对齐点放在 `=`、`+`、`\leq` 这类算子处；`\nonumber` 去掉不需要的编号。

### 4.2 给编号留位

`equation` 编号默认贴在右边距上。公式太宽时它会被挤到下一行。修法优先级：

1. 用更短的等价写法（`\mathbb{E}` 换 `E`、定义符号 `p := ...`）；
2. 拆成两行（`align` / `split`）；
3. 把 `\quad`、`\hspace` 之类的装饰性间距删掉；
4. **不要**为了塞进去而压缩字号（`\footnotesize` 里的公式与正文正文不协调）。

```latex
\begin{equation}
\begin{split}
  \mathrm{Attn}(Q,K,V) &= \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V \\
  &\quad + \mathrm{residual}
\end{split}
\end{equation}
```

### 4.3 不许拆语义项

一个完整项（如 `\mathbb{E}_{x\sim p_\text{data}}[\log D(x)]`）被断成两行，读者会把上下标读错。断点只能落在算子处。

---

## 5. 结构级

```latex
% 三级标题改行内小标题
\paragraph{Token routing.} We apply ...
```

```latex
% 列表紧凑化（导言区）
\usepackage{enumitem}
\setlist[itemize]{leftmargin=*,topsep=2pt,itemsep=1pt,parsep=0pt}
\setlist[enumerate]{leftmargin=*,topsep=2pt,itemsep=1pt,parsep=0pt}
```

```latex
% 行内代码体 → 数学表达 / 可断行的路径
\texttt{CrossAttn()}      →  $\mathrm{CrossAttn}(\cdot)$
\texttt{routing/layer3}   →  \path{routing/layer3}  % 允许断行
```

`\texttt` 里的内容不参与断词，会把整行词距撑开；`\url`/`\path` 会在 `/`、`-` 处断行。

---

## 6. 编译与复查

```bash
latexmk -pdf -interaction=nonstopmode paper.tex
python "<skill>/scripts/audit_layout.py" --pdf paper.pdf --tex paper.tex \
  --md audit.md --json audit.json --crops crops
```

- 每改一簇问题就重编一次，别攒着改完再编（分不清是哪一改动生效）。
- 关注编译日志里的 `Overfull \hbox`——它和审计脚本的越界检查是同一件事的两种说法。
- 复跑要用同样的参数（`--min-fill`、`--target-fill`），否则 before/after 不可比。
