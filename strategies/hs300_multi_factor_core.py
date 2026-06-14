#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 小市值 + 高 ROE 多因子选股 — 纯函数核心逻辑。

打分算法对齐聚宽官方教程《【量化课堂】多因子策略入门》：
  https://www.joinquant.com/post/1399

  fillNan → getRank（冒泡排序赋秩 1..N）→ 综合得分 = 市值秩×1 + ROE秩×(-1)
  → 冒泡排序取得分最小的前 N 只（与教程 bubble 升序一致）。

本地 AkShare：``python strategies/run_hs300_akshare.py``
"""

from __future__ import annotations

from typing import List, Sequence

import numpy as np
import pandas as pd

# --- 策略超参数（与聚宽教程 g.tc / g.yb / g.N 一致）---
BENCHMARK: str = "000300.XSHG"
INDEX_CODE: str = "000300.XSHG"
REBALANCE_EVERY_N: int = 15
SUSPENSION_LOOKBACK: int = 63
TOP_N: int = 3
FACTOR_COLS: tuple[str, ...] = ("market_cap", "roe")
FACTOR_WEIGHTS: tuple[float, ...] = (1.0, -1.0)  # g.weights=[[1],[-1]]
REBALANCE_TIME: str = "14:50"


def fill_nan_jq_article(matrix: np.ndarray) -> np.ndarray:
    """复刻教程 fillNan：列均值填充，整列 NaN 时均值取 0。"""
    m = np.asarray(matrix, dtype=float).copy()
    rows, cols = m.shape
    for j in range(cols):
        s = 0.0
        count = 0.0
        for i in range(rows):
            if np.isfinite(m[i, j]):
                s += float(m[i, j])
                count += 1.0
        avg = s / max(count, 1.0)
        for i in range(rows):
            if not np.isfinite(m[i, j]):
                m[i, j] = avg
    return m


def get_rank_jq_article(matrix: np.ndarray) -> np.ndarray:
    """复刻教程 getRank：逐列按因子值升序赋秩 1..N，再恢复原始行顺序。"""
    r = np.asarray(matrix, dtype=float).copy()
    rows = len(r)
    if rows == 0:
        return r
    cols = r.shape[1]
    indexes = list(range(rows))

    for k in range(cols):
        for i in range(rows):
            for j in range(i):
                if r[j, k] < r[i, k]:
                    indexes[j], indexes[i] = indexes[i], indexes[j]
                    r[j], r[i] = r[i].copy(), r[j].copy()
        for i in range(rows):
            r[i, k] = float(i + 1)
        for i in range(rows):
            for j in range(i):
                if indexes[j] > indexes[i]:
                    indexes[j], indexes[i] = indexes[i], indexes[j]
                    r[j], r[i] = r[i].copy(), r[j].copy()
    return r


def bubble_sort_jq(values: np.ndarray, labels: Sequence[str]) -> List[str]:
    """
    复刻教程 bubble（post/1399 原文）。

    该实现将综合得分 **较大** 的标的排在前面；``stock_sort[0:g.N]`` 取得分最大的前 N 只。
    """
    numbers = [[float(x)] for x in np.asarray(values, dtype=float).reshape(-1)]
    indexes = list(labels)
    n = len(numbers)
    for i in range(n):
        for j in range(i):
            if numbers[j][0] < numbers[i][0]:
                numbers[j][0], numbers[i][0] = numbers[i][0], numbers[j][0]
                indexes[j], indexes[i] = indexes[i], indexes[j]
    return indexes


bubble_sort_asc = bubble_sort_jq


def fill_nan_cross_section(df: pd.DataFrame, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """兼容旧接口；内部走教程 fillNan。"""
    cols = list(columns) if columns is not None else [c for c in FACTOR_COLS if c in df.columns]
    out = df.copy()
    if not cols or out.empty:
        return out
    mat = out[cols].apply(pd.to_numeric, errors="coerce").values
    filled = fill_nan_jq_article(mat)
    for i, col in enumerate(cols):
        out[col] = filled[:, i]
    return out


def score_factors(factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    聚宽教程 getRankedFactors 等价实现。

    - 市值：值越小 rank 越小（权重 +1）
    - ROE：值越大 rank 越大，乘以 -1 后更优（权重 -1）
    - 综合得分 = 市值秩×1 + ROE秩×(-1)
    - 选股：教程 bubble 排序后取前 N（得分较大者优先，与 post/1399 一致）
    """
    empty_cols = [*FACTOR_COLS, "rank_market_cap", "rank_roe", "composite_score"]
    if factor_df.empty:
        return pd.DataFrame(columns=empty_cols)

    df = factor_df.copy()
    for col in FACTOR_COLS:
        if col not in df.columns:
            df[col] = np.nan

    raw = df[list(FACTOR_COLS)].apply(pd.to_numeric, errors="coerce").values
    filled = fill_nan_jq_article(raw)
    ranked = get_rank_jq_article(filled)
    w = np.array(FACTOR_WEIGHTS, dtype=float)
    points = ranked @ w

    out = df.copy()
    out["market_cap"] = filled[:, 0]
    out["roe"] = filled[:, 1]
    out["rank_market_cap"] = ranked[:, 0]
    out["rank_roe"] = ranked[:, 1]
    out["composite_score"] = points
    return out


def select_top_stocks(scored: pd.DataFrame, top_n: int = TOP_N) -> List[str]:
    """教程 toBuy = stock_sort[0:g.N]。"""
    if scored.empty:
        return []
    ordered = bubble_sort_jq(scored["composite_score"].values, list(scored.index))
    return ordered[:top_n]


def is_rebalance_day(day_index: int, every_n: int = REBALANCE_EVERY_N) -> bool:
    """对应教程 g.t % g.tc == 0（g.t 从 0 计数）。"""
    if day_index < 0:
        return False
    return day_index % every_n == 0
