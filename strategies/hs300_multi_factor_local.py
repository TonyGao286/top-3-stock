#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 多因子选股 — JQData 本地数据层。

供 hs300_multi_factor_backtest.py 与单次截面选股调用。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable, List, Sequence

import pandas as pd
from jqdatasdk import get_fundamentals, get_index_stocks, get_price, get_trade_days, indicator, query, valuation

from strategies.hs300_multi_factor_core import (
    INDEX_CODE,
    SUSPENSION_LOOKBACK,
    score_factors,
    select_top_stocks,
)

logger = logging.getLogger(__name__)

_BATCH = 400


def _normalize_date(as_of: date | datetime | str) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return pd.Timestamp(as_of).date()


def _chunks(items: Sequence[str], size: int = _BATCH) -> Iterable[List[str]]:
    buf: List[str] = []
    for x in items:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def get_hs300_universe(as_of: date | datetime | str) -> List[str]:
    as_of = _normalize_date(as_of)
    members = get_index_stocks(INDEX_CODE, date=as_of)
    return sorted(members)


def filter_stock_pool(stocks: Sequence[str], as_of: date | datetime | str) -> List[str]:
    """剔除当日停牌 + 过去 SUSPENSION_LOOKBACK 个交易日内有过停牌的标的。"""
    as_of = _normalize_date(as_of)
    stock_list = list(stocks)
    if not stock_list:
        return []

    trade_days = get_trade_days(end_date=as_of, count=SUSPENSION_LOOKBACK)
    if len(trade_days) == 0:
        return []
    start = pd.Timestamp(trade_days[0]).date()

    paused_parts: list[pd.DataFrame] = []
    snap_parts: list[pd.DataFrame] = []
    for batch in _chunks(stock_list):
        hist = get_price(
            batch,
            start_date=start,
            end_date=as_of,
            frequency="daily",
            fields=["paused"],
            panel=False,
            skip_paused=False,
            fq="pre",
        )
        if hist is not None and not hist.empty:
            paused_parts.append(hist)

        snap = get_price(
            batch,
            end_date=as_of,
            count=1,
            frequency="daily",
            fields=["close", "paused"],
            panel=False,
            skip_paused=False,
            fq="pre",
        )
        if snap is not None and not snap.empty:
            snap_parts.append(snap)

    ever_paused: set[str] = set()
    if paused_parts:
        paused_df = pd.concat(paused_parts, ignore_index=True)
        for code, sub in paused_df.groupby("code"):
            if sub["paused"].fillna(False).astype(bool).any():
                ever_paused.add(code)

    snap = pd.concat(snap_parts, ignore_index=True) if snap_parts else pd.DataFrame()
    if not snap.empty:
        snap = snap.sort_values("time").groupby("code", as_index=False).tail(1).set_index("code")

    kept: List[str] = []
    for code in stock_list:
        if code in ever_paused:
            continue
        if snap.empty or code not in snap.index:
            continue
        row = snap.loc[code]
        if bool(row.get("paused", False)):
            continue
        close = pd.to_numeric(row.get("close"), errors="coerce")
        if pd.isna(close) or float(close) <= 0:
            continue
        kept.append(code)

    return kept


def fetch_factors(stocks: Sequence[str], as_of: date | datetime | str) -> pd.DataFrame:
    as_of = _normalize_date(as_of)
    if not stocks:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for batch in _chunks(list(stocks), size=800):
        q = query(
            valuation.code,
            valuation.market_cap,
            indicator.roe,
        ).filter(valuation.code.in_(batch))
        part = get_fundamentals(q, date=as_of)
        if part is not None and not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame(index=list(stocks), columns=["market_cap", "roe"])

    fund = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"], keep="last").set_index("code")
    out = pd.DataFrame(
        {
            "market_cap": pd.to_numeric(fund.get("market_cap"), errors="coerce"),
            "roe": pd.to_numeric(fund.get("roe"), errors="coerce"),
        }
    )
    return out.reindex(stocks)


def run_screen(as_of: date | datetime | str, *, top_n: int = 3) -> pd.DataFrame:
    """单次截面选股，返回打分明细（按综合得分升序，最优在前）。"""
    as_of = _normalize_date(as_of)
    universe = get_hs300_universe(as_of)
    logger.info("沪深300 成分：%d 只", len(universe))

    pool = filter_stock_pool(universe, as_of)
    logger.info("过滤后：%d 只", len(pool))

    raw = fetch_factors(pool, as_of)
    scored = score_factors(raw)
    target = select_top_stocks(scored, top_n)

    out = scored.copy()
    out["选中"] = out.index.isin(target)
    out = out.sort_values("composite_score", ascending=True)
    out.insert(0, "排名", range(1, len(out) + 1))
    return out


__all__ = [
    "get_hs300_universe",
    "filter_stock_pool",
    "fetch_factors",
    "run_screen",
]
