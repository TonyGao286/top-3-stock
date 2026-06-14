#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股 — AkShare 本地版（无 JQData / 聚宽额度限制）。

运行环境：本地 Python + akshare（与 single_stock_scoring 相同数据源体系）
  - 指数成分：中证指数官网成分表（index_stock_cons_csindex）
  - 行情 K 线：腾讯 / 东财（deep_value_funnel.hist_fetch，东财含换手率）
  - 财务指标：东财财务分析指标 + 资产负债表 + 现金流量表

说明：
  - 全市场 ~1500 只成分股需逐只拉 K 线与财报，首次运行约 20~60 分钟（受 AK_REQUEST_THROTTLE 影响）。
  - 无「每日百万条」额度，但请控制并发，避免东财风控断连。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Dict, List, Sequence, Tuple

import akshare as ak
import numpy as np
import pandas as pd

from deep_value_funnel.http_utils import call_with_retry, df_nonempty
from deep_value_funnel.request_identity import ensure_request_identity
from deep_value_funnel.symbols import is_st_name, to_em_h10_code, to_em_sec_code
from strategies.jq_multi_factor_weekly import (
    FACTOR_DIRECTION,
    IDIO_VOL_WINDOW,
    INDEX_CODES,
    MIN_LISTING_TRADE_DAYS,
    MOM_WINDOW,
    TOP_N,
    TURNOVER_WINDOW,
    score_and_rank,
)

logger = logging.getLogger(__name__)

# 中证指数代码（AkShare 用 6 位，不带后缀）
_AK_INDEX_MAP: Dict[str, str] = {
    "000016.XSHG": "000016",
    "000905.XSHG": "000905",
    "000852.XSHG": "000852",
}
# 指数日 K 代码（东财）
_MKT_INDEX_EM = "sh000905"
_HIST_LOOKBACK_CALENDAR = 400  # 约 252+ 个交易日，用于次新股过滤与 20 日因子


def _normalize_date(as_of: date | datetime | str) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return pd.Timestamp(as_of).date()


def _date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def _index_cons(symbol: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.index_stock_cons_csindex(symbol=symbol)

    return call_with_retry(f"index_cons:{symbol}", _go, validate=df_nonempty, max_retries=4)


def get_base_universe(as_of: date | datetime | str | None = None) -> Tuple[List[str], Dict[str, str]]:
    """
    合并上证50 / 中证500 / 中证1000 成分股。
    返回 (代码列表, {代码: 名称})。
    """
    _ = _normalize_date(as_of) if as_of is not None else None  # 中证 xls 为最新成分，无历史截面 API
    codes: set[str] = set()
    names: Dict[str, str] = {}

    for jq_code in INDEX_CODES:
        sym = _AK_INDEX_MAP.get(jq_code, jq_code.split(".")[0])
        try:
            df = _index_cons(sym)
        except Exception as exc:
            logger.warning("获取指数成分失败 %s: %s", sym, exc)
            continue
        code_col = "成分券代码" if "成分券代码" in df.columns else df.columns[0]
        name_col = "成分券名称" if "成分券名称" in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
        for _, row in df.iterrows():
            c = str(row[code_col]).zfill(6)
            codes.add(c)
            if name_col:
                names[c] = str(row[name_col])

    return sorted(codes), names


def _fetch_spot() -> pd.DataFrame | None:
    """A 股实时快照；优先新浪（东财易断连），失败再用东财。"""

    def _sina() -> pd.DataFrame:
        df = ak.stock_zh_a_spot()
        out = df.copy()
        if "代码" in out.columns:
            out["代码"] = out["代码"].astype(str).str[-6:].str.zfill(6)
        return out

    def _em() -> pd.DataFrame:
        return ak.stock_zh_a_spot_em()

    for label, fn in (("stock_zh_a_spot", _sina), ("stock_zh_a_spot_em", _em)):
        try:
            df = call_with_retry(label, fn, validate=df_nonempty, max_retries=2)
            out = df.copy()
            if "代码" not in out.columns:
                continue
            out["代码"] = out["代码"].astype(str).str.zfill(6)
            logger.info("行情快照来源：%s（%d 条）", label, len(out))
            return out.set_index("代码")
        except Exception as exc:
            logger.warning("行情快照 %s 失败：%s", label, exc)

    logger.warning("无法拉取全市场行情，将仅用成分股名称过滤 ST（不校验停牌）")
    return None


def filter_tradable_stocks(
    stocks: Sequence[str],
    names: Dict[str, str],
    spot: pd.DataFrame | None = None,
) -> List[str]:
    """
    剔除 ST、无成交（近似停牌）、名称缺失的标的。
    上市天数在后续 K 线长度校验中一并处理（≥ MIN_LISTING_TRADE_DAYS 根日 K）。
    """
    if spot is None:
        spot = _fetch_spot()

    kept: List[str] = []
    for code in stocks:
        nm = names.get(code) or ""
        if spot is not None and code in spot.index and not nm:
            nm = str(spot.loc[code].get("名称") or "")
        if is_st_name(nm):
            continue
        if spot is None:
            kept.append(code)
            continue
        if code not in spot.index:
            continue
        row = spot.loc[code]
        price = pd.to_numeric(row.get("最新价"), errors="coerce")
        vol = pd.to_numeric(row.get("成交量"), errors="coerce")
        if pd.isna(price) or float(price) <= 0:
            continue
        if pd.isna(vol) or float(vol) <= 0:
            continue
        kept.append(code)
    return kept


def _fetch_stock_hist(code: str, start: date, end: date) -> pd.DataFrame:
    """优先腾讯日 K（稳定）；东财仅作补充（含换手率，失败则用成交额代理）。"""
    from deep_value_funnel.hist_fetch import fetch_kline_qfq_normalized

    try:
        tx = fetch_kline_qfq_normalized(code, _date_str(start), _date_str(end))
        tx["换手率"] = np.nan
        if "成交额" in tx.columns:
            tx["_amt"] = pd.to_numeric(tx["成交额"], errors="coerce")
        return tx
    except Exception as exc:
        logger.debug("[%s] 腾讯 K 线失败，尝试东财：%s", code, exc)

    def _em() -> pd.DataFrame:
        return ak.stock_zh_a_hist(
            symbol=str(code).zfill(6),
            period="daily",
            start_date=_date_str(start),
            end_date=_date_str(end),
            adjust="qfq",
        )

    try:
        df = call_with_retry(f"{code}:hist_em", _em, validate=df_nonempty, max_retries=2)
        out = df.copy()
        out["日期"] = pd.to_datetime(out["日期"], errors="coerce")
        out = out.dropna(subset=["日期"]).sort_values("日期")
        out["收盘"] = pd.to_numeric(out["收盘"], errors="coerce")
        out["换手率"] = pd.to_numeric(out.get("换手率"), errors="coerce")
        return out
    except Exception as exc:
        logger.warning("[%s] K 线拉取失败：%s", code, exc)
        return pd.DataFrame()


def _index_close_to_returns(df: pd.DataFrame, start: date, end: date) -> pd.Series:
    """从指数日 K 表提取 close 并计算日收益，再裁剪到 [start, end]。"""
    d = df.copy()
    if "date" in d.columns:
        d["_d"] = pd.to_datetime(d["date"], errors="coerce")
    elif "日期" in d.columns:
        d["_d"] = pd.to_datetime(d["日期"], errors="coerce")
    else:
        return pd.Series(dtype=float)

    close_col = next((c for c in ("close", "收盘") if c in d.columns), None)
    if close_col is None:
        return pd.Series(dtype=float)

    d[close_col] = pd.to_numeric(d[close_col], errors="coerce")
    d = d.dropna(subset=["_d", close_col])
    d = d[(d["_d"] >= pd.Timestamp(start)) & (d["_d"] <= pd.Timestamp(end))]
    if d.empty:
        return pd.Series(dtype=float)
    return d.sort_values("_d").set_index("_d")[close_col].pct_change().dropna()


def _fetch_mkt_returns(start: date, end: date) -> pd.Series:
    """
    中证500（sh000905）日收益，用于特异性波动率回归。
    东财 → 新浪 → 腾讯，逐级兜底。
    """
    cache_key = f"{_date_str(start)}_{_date_str(end)}"

    if not hasattr(_fetch_mkt_returns, "_cache"):
        _fetch_mkt_returns._cache = {}  # type: ignore[attr-defined]
    cached = _fetch_mkt_returns._cache.get(cache_key)  # type: ignore[attr-defined]
    if cached is not None:
        return cached

    index_sym = _MKT_INDEX_EM  # sh000905

    providers: list[tuple[str, callable]] = [
        (
            "index_daily_em",
            lambda: ak.stock_zh_index_daily_em(
                symbol=index_sym,
                start_date=_date_str(start),
                end_date=_date_str(end),
            ),
        ),
        ("index_daily_sina", lambda: ak.stock_zh_index_daily(symbol=index_sym)),
        ("index_daily_tx", lambda: ak.stock_zh_index_daily_tx(symbol=index_sym)),
    ]

    for label, fn in providers:
        try:
            df = call_with_retry(label, fn, validate=df_nonempty, max_retries=2)
            ret = _index_close_to_returns(df, start, end)
            if not ret.empty:
                logger.info("指数日收益来源：%s（%d 根）", label, len(ret))
                _fetch_mkt_returns._cache[cache_key] = ret  # type: ignore[attr-defined]
                return ret
            logger.warning("指数日收益 %s 返回空（日期区间无数据）", label)
        except Exception as exc:
            logger.warning("指数日收益 %s 失败：%s", label, exc)

    logger.warning("无法获取中证500指数收益，特异性波动率因子将缺失（其余因子仍计算）")
    empty = pd.Series(dtype=float)
    _fetch_mkt_returns._cache[cache_key] = empty  # type: ignore[attr-defined]
    return empty


def _calc_idio_vol(stock_ret: pd.Series, mkt_ret: pd.Series) -> float:
    if mkt_ret is None or mkt_ret.empty:
        return np.nan
    aligned = pd.concat([stock_ret, mkt_ret], axis=1, join="inner").dropna()
    if len(aligned) < IDIO_VOL_WINDOW:
        return np.nan
    window = aligned.tail(IDIO_VOL_WINDOW)
    x, y = window.iloc[:, 1].values, window.iloc[:, 0].values
    if np.std(x) > 1e-12:
        beta = np.cov(y, x)[0, 1] / np.var(x)
        return -float(np.std(y - beta * x))
    return -float(np.std(y))


def _pv_factors_one(code: str, start: date, end: date, mkt_ret: pd.Series) -> dict[str, float] | None:
    hist = _fetch_stock_hist(code, start, end)
    if hist.empty or len(hist) < max(MOM_WINDOW + 1, MIN_LISTING_TRADE_DAYS):
        return None

    closes = hist.set_index("日期")["收盘"].astype(float)
    ret_20 = closes.iloc[-1] / closes.iloc[-MOM_WINDOW - 1] - 1.0
    rev_mom = -float(ret_20)

    if "换手率" in hist.columns and hist["换手率"].notna().any():
        turnover_20d = float(hist["换手率"].tail(TURNOVER_WINDOW).mean())
    elif "_amt" in hist.columns:
        turnover_20d = float(hist["_amt"].tail(TURNOVER_WINDOW).mean())
    else:
        turnover_20d = np.nan

    idio = _calc_idio_vol(closes.pct_change().dropna(), mkt_ret)
    return {"rev_mom_20d": rev_mom, "turnover_20d": turnover_20d, "idio_vol_20d": idio}


def _profit_factors_one(code: str) -> dict[str, float]:
    sec = to_em_sec_code(code)
    h10 = to_em_h10_code(code)
    out: dict[str, float] = {"roe": np.nan, "ocf_to_liability": np.nan}

    def _ind() -> pd.DataFrame:
        return ak.stock_financial_analysis_indicator_em(symbol=sec)

    try:
        ind = call_with_retry(f"{code}:indicator_em", _ind, validate=df_nonempty, max_retries=3)
        ind = ind.sort_values("REPORT_DATE", ascending=False)
        roe = pd.to_numeric(ind.iloc[0].get("ROEJQ"), errors="coerce")
        if pd.notna(roe):
            out["roe"] = float(roe)
    except Exception as exc:
        logger.debug("[%s] ROE 失败：%s", code, exc)

    def _bs() -> pd.DataFrame:
        return ak.stock_balance_sheet_by_report_em(symbol=h10)

    def _cf() -> pd.DataFrame:
        return ak.stock_cash_flow_sheet_by_report_em(symbol=h10)

    try:
        bs = call_with_retry(f"{code}:balance_em", _bs, validate=df_nonempty, max_retries=3)
        cf = call_with_retry(f"{code}:cashflow_em", _cf, validate=df_nonempty, max_retries=3)
        bs = bs.sort_values("REPORT_DATE", ascending=False)
        cf = cf.sort_values("REPORT_DATE", ascending=False)
        liability = pd.to_numeric(bs.iloc[0].get("TOTAL_LIABILITIES"), errors="coerce")
        ocf = pd.to_numeric(cf.iloc[0].get("NETCASH_OPERATE"), errors="coerce")
        if pd.notna(liability) and float(liability) > 0 and pd.notna(ocf):
            out["ocf_to_liability"] = float(ocf) / float(liability)
    except Exception as exc:
        logger.debug("[%s] OCF/负债 失败：%s", code, exc)

    return out


def _factors_one(
    code: str,
    start: date,
    end: date,
    mkt_ret: pd.Series,
) -> dict[str, float] | None:
    pv = _pv_factors_one(code, start, end, mkt_ret)
    if pv is None:
        return None
    prof = _profit_factors_one(code)
    return {**pv, **prof}


def compute_raw_factors(
    stocks: Sequence[str],
    as_of: date | datetime | str,
    *,
    workers: int = 2,
) -> pd.DataFrame:
    """并行计算原始因子（workers 建议 1~3，过大易触发东财风控）。"""
    as_of_d = _normalize_date(as_of)
    start = as_of_d - timedelta(days=_HIST_LOOKBACK_CALENDAR)
    mkt_ret = _fetch_mkt_returns(start, as_of_d)

    rows: dict[str, dict[str, float]] = {}
    workers = max(1, int(workers))
    total = len(stocks)

    if workers == 1:
        for i, code in enumerate(stocks, 1):
            if i % 50 == 0 or i == total:
                logger.info("因子进度 %d/%d", i, total)
            fac = _factors_one(code, start, as_of_d, mkt_ret)
            if fac:
                rows[code] = fac
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_factors_one, c, start, as_of_d, mkt_ret): c for c in stocks}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 50 == 0 or done == total:
                    logger.info("因子进度 %d/%d", done, total)
                code = futs[fut]
                try:
                    fac = fut.result()
                    if fac:
                        rows[code] = fac
                except Exception as exc:
                    logger.warning("[%s] 因子计算失败：%s", code, exc)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(rows, orient="index")
    for col, direction in FACTOR_DIRECTION.items():
        if col in df.columns and direction < 0:
            df[col] = -df[col]
    return df


def run_screen(
    as_of: date | datetime | str | None = None,
    *,
    top_n: int = TOP_N,
    workers: int = 2,
    limit: int | None = None,
) -> pd.DataFrame:
    """执行截面选股（AkShare 数据源）。"""
    ensure_request_identity()
    as_of_d = _normalize_date(as_of) if as_of is not None else date.today()
    logger.info("选股截面日：%s（AkShare 成分为最新披露，非严格历史截面）", as_of_d)

    universe, names = get_base_universe(as_of_d)
    logger.info("指数成分合并：%d 只", len(universe))

    logger.info("拉取 A 股实时行情用于 ST/停牌过滤…")
    spot = _fetch_spot()
    tradable = filter_tradable_stocks(universe, names, spot=spot)
    if limit is not None and limit > 0:
        tradable = tradable[: int(limit)]
        logger.info("调试模式：仅计算前 %d 只", len(tradable))
    logger.info("基础过滤后：%d 只", len(tradable))

    raw = compute_raw_factors(tradable, as_of_d, workers=workers)
    if raw.empty:
        raise RuntimeError("因子矩阵为空，请检查网络或稍后重试（东财接口可能限流）。")

    scores = score_and_rank(raw)
    top = scores.head(top_n)

    out = raw.loc[top.index].copy()
    out.insert(0, "名称", [names.get(c, "") for c in out.index])
    out["综合得分"] = top
    out["排名"] = range(1, len(out) + 1)
    return out.sort_values("综合得分", ascending=False)


__all__ = ["run_screen", "get_base_universe", "filter_tradable_stocks", "compute_raw_factors"]
