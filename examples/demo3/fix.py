# -*- coding: utf-8 -*-
"""demo3：before → after。修法原则：寡行靠「删」把末行整行消掉，不靠加字填满。"""
import io

src = io.open("paper.tex", encoding="utf-8").read()

pairs = [
    # 1) 列表：去掉左缩进 + 每项缩短到单行
    (r"""\begin{itemize}
\item Quadratic attention matrix memory overhead across long video sequence lengths.
\item Redundant background patch representations degrade fine-grained local details.
\item Unstable gradient norms during end-to-end optimization with frozen encoders.
\end{itemize}""",
     r"""\begin{itemize}
\item Quadratic attention memory across long videos.
\item Redundant patches blur fine local details.
\item Unstable gradients with frozen encoders.
\end{itemize}"""),
    # 2) 子图：去掉顶部空带、间距 26pt → 4pt、宽度 0.42 → 0.49
    (r"""\vspace*{18pt}
\centering
\includegraphics[width=0.42\linewidth]{fig_l.png}\hspace*{26pt}\includegraphics[width=0.42\linewidth]{fig_r.png}""",
     r"""\centering
\includegraphics[width=0.49\linewidth]{fig_l.png}\hspace*{4pt}\includegraphics[width=0.49\linewidth]{fig_r.png}"""),
    # 3) 寡行：删词把「straint.」整行消掉
    (r"""Both rules are implemented as constraints in the solver rather than as post-hoc filters,
because a post-hoc filter can produce a map that is infeasible with respect to the
quorum constraint.""",
     r"""Both rules are enforced inside the solver, never as post-hoc filters."""),
    # 4) 另一处末行：删掉刻意铺陈的从句
    (r"""The remaining bottlenecks
are well understood and we list them explicitly, because each of them was measured on a
single GPU with the same batch size and the same random seed as the baseline, which makes
the comparison fair. This is why we believe the following three issues are the ones that
matter for long video.""",
     r"""The remaining bottlenecks are well understood; each was measured on a single GPU with
the same batch and seed as the baseline. Three issues matter for long video."""),
    # 5) 结果段：改写句尾，末行不再只剩一个词
    (r"""As shown in Figure 1, the trajectory-aligned sampler achieves lower FID across all step
budgets while maintaining near-linear latency scaling up to sixteen thousand tokens on a
single GPU.""",
     r"""In Figure 1 the trajectory-aligned sampler achieves lower FID across all step budgets
while maintaining near-linear latency scaling up to sixteen thousand tokens on a single GPU."""),
]

out = src
for a, b in pairs:
    assert a in out, a[:50]
    out = out.replace(a, b)

out = out.replace(r"\usepackage{amsmath}", "\\usepackage{amsmath}\n\\usepackage{enumitem}\n\\setlist[itemize]{leftmargin=*,topsep=2pt,itemsep=1pt,parsep=0pt}")
io.open("paper-fixed.tex", "w", encoding="utf-8").write(out)
print("wrote paper-fixed.tex", len(out))
