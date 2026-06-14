#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沪深300 截面选股结果 → Server酱 推送文案。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Tuple

import pandas as pd

from strategies.hs300_multi_factor_core import TOP_N


def format_screen_push(
    df: pd.DataFrame,
    as_of: date,
    *,
    csv_path: Path | None = None,
    top_n: int = TOP_N,
) -> Tuple[str, str]:
    """生成 (title, desp) 供 Server酱 使用（desp 为 Markdown）。"""
    picked = df[df["选中"]].head(top_n) if "选中" in df.columns else df.head(top_n)
    title = f"HS300多因子 Top{top_n} {as_of}（A股交易日）"

    lines = [
        f"## 沪深300 多因子选股",
        "",
        f"- **截面日**：{as_of}",
        f"- **算法**：聚宽教程 post/1399（fillNan + getRank + bubble）",
        f"- **过滤后样本**：{len(df)} 只",
        "",
        f"### 选中 Top {top_n}",
        "",
    ]

    if picked.empty:
        lines.append("*（无选中标的，请检查数据源或日志）*")
    else:
        for i, (_, row) in enumerate(picked.iterrows(), 1):
            jq = row.get("聚宽代码", "")
            name = row.get("名称", "")
            mcap = row.get("market_cap_亿", row.get("market_cap"))
            roe = row.get("roe")
            score = row.get("composite_score")
            mcap_s = f"{float(mcap):.2f}亿" if pd.notna(mcap) else "N/A"
            roe_s = f"{float(roe):.2f}" if pd.notna(roe) else "N/A"
            score_s = f"{float(score):.2f}" if pd.notna(score) else "N/A"
            lines.append(f"{i}. **{name}** `{jq}`")
            lines.append(f"   - 市值 {mcap_s} | ROE {roe_s} | 得分 {score_s}")
            lines.append("")

    lines.extend(["### 全池前 5", ""])
    show_cols = [c for c in ("聚宽代码", "名称", "market_cap_亿", "roe", "composite_score") if c in df.columns]
    if show_cols:
        for _, row in df.head(5).iterrows():
            parts = [str(row.get(c, "")) for c in show_cols]
            lines.append("- " + " | ".join(parts))
    else:
        lines.append(df.head(5).to_string())

    if csv_path is not None:
        lines.extend(["", f"*CSV：{csv_path.name}*"])
    lines.extend(["", "*由 run_hs300_akshare.py + GitHub Actions 自动推送*"])

    return title, "\n".join(lines)
