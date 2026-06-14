#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股 — JQData 本地版（jqdatasdk）。

与 strategies/jq_multi_factor_weekly.py 共用同一套因子与打分逻辑，
但运行在本地 Python，无需聚宽策略托管环境。

用法：由 run_local_jqdata.py 调用，或：
  from strategies.multi_factor_local import run_screen
  run_screen(as_of="2026-02-26")
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable, List, Sequence

import numpy as np
import pandas as pd
from jqdatasdk import (
    balance,
    cash_flow,
    get_all_securities,
    get_extras,
    get_fundamentals,
    get_index_stocks,
    get_price,
    get_query_count,
    get_trade_days,
    get_valuation,
    indicator,
    query,
    valuation,
)

from strategies.jq_multi_factor_weekly import (
    FACTOR_DIRECTION,
    IDIO_VOL_WINDOW,
    INDEX_CODES,
    MARKET_INDEX,
    MIN_LISTING_TRADE_DAYS,
    MOM_WINDOW,
    PROFIT_FACTOR_NAMES,
    TOP_N,
    TURNOVER_WINDOW,
    score_and_rank,
)

logger = logging.getLogger(__name__)

# 单次请求建议上限（过大易超时；过小则请求次数多）
_BATCH = 500
# 252 个交易日 ≈ 372 个自然日（避免对每只股票单独 get_trade_days）
_LISTING_CALENDAR_DAYS = int(MIN_LISTING_TRADE_DAYS * 365.25 / 252) + 10


def _chunks(items: Sequence[str], size: int = _BATCH) -> Iterable[List[str]]:
    buf: List[str] = []
    for x in items:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _normalize_date(as_of: date | datetime | str) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return pd.Timestamp(as_of).date()


def _log_query_quota(label: str = "") -> None:
    try:
        qc = get_query_count()
        if isinstance(qc, dict):
            logger.info(
                "JQData 调用额度%s：total=%s spare=%s",
                f" ({label})" if label else "",
                qc.get("total"),
                qc.get("spare"),
            )
    except Exception:
        pass


def _ensure_query_budget(min_spare: int = 80_000) -> None:
    """全市场截面预估消耗 ~5–10 万条，额度不足时提前报错。"""
    try:
        qc = get_query_count()
        spare = qc.get("spare") if isinstance(qc, dict) else None
        if spare is not None and int(spare) < min_spare:
            raise RuntimeError(
                f"JQData 今日剩余查询条数仅 {spare}，预计不足以跑完全市场截面（建议 ≥{min_spare}）。\n"
                "请明日再试，或联系聚宽升级流量：https://www.joinquant.com/help/api/doc?name=logon&id=9831"
            )
    except RuntimeError:
        raise
    except Exception:
        pass


def get_latest_trade_date(before: date | None = None) -> date:
    """取不晚于 before 的最近一个交易日（默认今天）。"""
    end = before or date.today()
    days = get_trade_days(end_date=end, count=1)
    if len(days) == 0:
        raise RuntimeError(f"无法获取交易日历（end_date={end}）")
    return pd.Timestamp(days[-1]).date()


def get_base_universe(as_of: date | datetime | str) -> List[str]:
    """合并三大指数成分股。"""
    as_of = _normalize_date(as_of)
    codes: set[str] = set()
    for idx in INDEX_CODES:
        try:
            codes.update(get_index_stocks(idx, date=as_of))
        except Exception as exc:
            logger.warning("获取指数成分失败 %s: %s", idx, exc)
    return sorted(codes)


def filter_tradable_stocks(stocks: Sequence[str], as_of: date | datetime | str) -> List[str]:
    """
    本地过滤：ST、停牌、次新股。
    全部使用批量接口，禁止逐股 get_security_info / get_trade_days（会瞬间打满百万条额度）。
    """
    as_of = _normalize_date(as_of)
    stock_list = list(stocks)
    if not stock_list:
        return []

    as_of_ts = pd.Timestamp(as_of)
    all_sec = get_all_securities(types=["stock"], date=as_of)
    meta = all_sec.reindex(stock_list).copy()
    meta["display_name"] = meta["display_name"].fillna("").astype(str)
    meta["start_date"] = pd.to_datetime(meta["start_date"], errors="coerce")
    meta["listed_days"] = (as_of_ts - meta["start_date"]).dt.days

    # 批量 ST 标记（1~4 次请求）
    st_flags: dict[str, bool] = {}
    for batch in _chunks(stock_list):
        flags = get_extras("is_st", batch, start_date=as_of, end_date=as_of, df=True)
        if flags is not None and not flags.empty:
            row = flags.iloc[0]
            for s in batch:
                st_flags[s] = bool(row.get(s, False))

    # 批量当日快照（1~4 次请求）
    snap_parts: list[pd.DataFrame] = []
    for batch in _chunks(stock_list):
        part = get_price(
            batch,
            end_date=as_of,
            count=1,
            frequency="daily",
            fields=["close", "paused"],
            panel=False,
            skip_paused=False,
            fq="pre",
        )
        if part is not None and not part.empty:
            snap_parts.append(part)
    snap = pd.concat(snap_parts, ignore_index=True) if snap_parts else pd.DataFrame()
    if not snap.empty:
        snap = snap.sort_values("time").groupby("code", as_index=False).tail(1)
        snap = snap.set_index("code")

    kept: List[str] = []
    for code in stock_list:
        if code not in meta.index or pd.isna(meta.at[code, "start_date"]):
            continue
        if "ST" in meta.at[code, "display_name"].upper() or st_flags.get(code, False):
            continue
        if meta.at[code, "listed_days"] < _LISTING_CALENDAR_DAYS:
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


def compute_raw_factors(stocks: Sequence[str], as_of: date | datetime | str) -> pd.DataFrame:
    """计算原始因子（量价 + 盈利，无成长因子）。"""
    as_of = _normalize_date(as_of)
    if not stocks:
        return pd.DataFrame()

    pv = _calc_price_volume_factors_local(list(stocks), as_of)
    prof = _calc_profitability_factors_local(list(stocks), as_of)

    df = pv.join(prof, how="outer")
    for col, direction in FACTOR_DIRECTION.items():
        if col in df.columns and direction < 0:
            df[col] = -df[col]
    return df


def _calc_rev_mom_from_closes(closes: pd.Series) -> float:
    c = closes.astype(float).dropna()
    if len(c) < MOM_WINDOW + 1:
        return np.nan
    return -float(c.iloc[-1] / c.iloc[-MOM_WINDOW - 1] - 1.0)


def _calc_idio_vol(stock_ret: pd.Series, mkt_ret: pd.Series) -> float:
    aligned = pd.concat([stock_ret, mkt_ret], axis=1, join="inner").dropna()
    if len(aligned) < IDIO_VOL_WINDOW:
        return np.nan
    window = aligned.tail(IDIO_VOL_WINDOW)
    x, y = window.iloc[:, 1].values, window.iloc[:, 0].values
    if np.std(x) > 1e-12:
        beta = np.cov(y, x)[0, 1] / np.var(x)
        return -float(np.std(y - beta * x))
    return -float(np.std(y))


def _calc_price_volume_factors_local(stocks: List[str], as_of: date) -> pd.DataFrame:
    lookback = max(MOM_WINDOW, TURNOVER_WINDOW, IDIO_VOL_WINDOW) + 5
    start = pd.Timestamp(get_trade_days(end_date=as_of, count=lookback)[0]).date()

    price_parts: list[pd.DataFrame] = []
    for batch in _chunks(stocks):
        part = get_price(
            batch,
            start_date=start,
            end_date=as_of,
            frequency="daily",
            fields=["close"],
            panel=False,
            skip_paused=True,
            fq="pre",
        )
        if part is not None and not part.empty:
            price_parts.append(part)
    price_panel = pd.concat(price_parts, ignore_index=True) if price_parts else pd.DataFrame()

    turnover_parts: list[pd.DataFrame] = []
    for batch in _chunks(stocks):
        part = get_valuation(batch, end_date=as_of, fields="turnover_ratio", count=TURNOVER_WINDOW)
        if part is not None and not part.empty:
            turnover_parts.append(part)
    turnover_panel = pd.concat(turnover_parts, ignore_index=True) if turnover_parts else pd.DataFrame()

    mkt = get_price(
        MARKET_INDEX,
        start_date=start,
        end_date=as_of,
        frequency="daily",
        fields=["close"],
        skip_paused=True,
        fq="pre",
    )
    mkt_ret = mkt["close"].pct_change().dropna()

    rev_mom: dict[str, float] = {}
    turnover_20d: dict[str, float] = {}
    idio_vol: dict[str, float] = {}

    if price_panel.empty:
        return pd.DataFrame()

    price_panel = price_panel.sort_values(["code", "time"])
    if not turnover_panel.empty:
        turnover_mean = (
            turnover_panel.groupby("code")["turnover_ratio"]
            .apply(lambda s: float(pd.to_numeric(s, errors="coerce").mean()))
        )
    else:
        turnover_mean = pd.Series(dtype=float)

    for code, sub in price_panel.groupby("code"):
        closes = sub.set_index("time")["close"]
        rev_mom[code] = _calc_rev_mom_from_closes(closes)
        turnover_20d[code] = float(turnover_mean.get(code, np.nan))
        idio_vol[code] = _calc_idio_vol(closes.pct_change().dropna(), mkt_ret)

    return pd.DataFrame(
        {
            "rev_mom_20d": pd.Series(rev_mom),
            "turnover_20d": pd.Series(turnover_20d),
            "idio_vol_20d": pd.Series(idio_vol),
        }
    )


def _calc_profitability_factors_local(stocks: List[str], as_of: date) -> pd.DataFrame:
    """财务因子尽量单次 query（JQData 按返回条数计额度）。"""
    frames: list[pd.DataFrame] = []
    for batch in _chunks(stocks, size=800):
        q = query(
            valuation.code,
            indicator.roe,
            balance.total_liability,
            cash_flow.net_operate_cash_flow,
        ).filter(valuation.code.in_(batch))
        part = get_fundamentals(q, date=as_of)
        if part is not None and not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame(index=stocks, columns=list(PROFIT_FACTOR_NAMES))

    fund = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"], keep="last").set_index("code")
    roe = pd.to_numeric(fund.get("roe"), errors="coerce")
    liability = pd.to_numeric(fund.get("total_liability"), errors="coerce")
    ocf = pd.to_numeric(fund.get("net_operate_cash_flow"), errors="coerce")
    ocf_ratio = ocf / liability.replace(0, np.nan)

    out = pd.DataFrame({"roe": roe, "ocf_to_liability": ocf_ratio})
    return out.reindex(stocks)


def run_screen(
    as_of: date | datetime | str | None = None,
    *,
    top_n: int = TOP_N,
    all_sec: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """执行一次截面选股，返回含因子与综合得分的 DataFrame（按得分降序）。"""
    _ensure_query_budget()
    _log_query_quota("选股开始前")

    as_of_d = _normalize_date(as_of) if as_of is not None else get_latest_trade_date()
    logger.info("选股截面日：%s", as_of_d)

    universe = get_base_universe(as_of_d)
    logger.info("指数成分合并：%d 只", len(universe))
    if not universe:
        from strategies.jqdata_client import get_jqdata_permission_range

        perm = get_jqdata_permission_range()
        hint = (
            f"当前截面日 {as_of_d} 可能超出 JQData 试用权限"
            + (f"（{perm}）" if perm else "")
            + "。请省略 --date 让程序自动选取，或显式指定如 --date 2026-02-26"
        )
        raise RuntimeError(f"基础股票池为空。{hint}")

    if all_sec is None:
        all_sec = get_all_securities(types=["stock"], date=as_of_d)

    logger.info("基础过滤中…")
    tradable = filter_tradable_stocks(universe, as_of_d)
    logger.info("基础过滤后：%d 只", len(tradable))
    _log_query_quota("过滤完成后")

    logger.info("计算因子中（约 %d 只 × 25 日行情 + 财务）…", len(tradable))
    raw = compute_raw_factors(tradable, as_of_d)
    if raw.empty:
        raise RuntimeError("因子矩阵为空，请检查 JQData 权限或日期是否为交易日。")
    _log_query_quota("因子计算完成后")

    scores = score_and_rank(raw)
    top = scores.head(top_n)

    name_map = all_sec["display_name"].to_dict() if not all_sec.empty else {}

    out = raw.loc[top.index].copy()
    out.insert(0, "名称", [name_map.get(c, "") for c in out.index])
    out["综合得分"] = top
    out["排名"] = range(1, len(out) + 1)
    return out.sort_values("综合得分", ascending=False)


__all__ = [
    "run_screen",
    "get_base_universe",
    "filter_tradable_stocks",
    "compute_raw_factors",
    "get_latest_trade_date",
]
