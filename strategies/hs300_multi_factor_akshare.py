#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 多因子选股 — AkShare 开源数据版（无聚宽 / JQData 依赖）。

数据源：
  - 指数成分：中证指数 ``index_stock_cons_csindex("000300")``（最新披露，非严格历史截面）
  - 日 K 线：腾讯 / 东财（``deep_value_funnel.hist_fetch``）
  - 总市值：东财 ``stock_value_em`` 历史估值序列（回测）；实时快照（截面选股）
  - ROE：东财 ``stock_financial_analysis_indicator_em``

局限（免费数据共性）：
  - 成分股为当前沪深300名单，历史回测存在 survivorship bias
  - 逐股拉取估值/财报，首次回测较慢；可用 ``--workers`` 与 ``--limit`` 控制
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
from deep_value_funnel.symbols import to_em_sec_code
from strategies.hs300_multi_factor_core import (
    SUSPENSION_LOOKBACK,
    TOP_N,
    score_factors,
    select_top_stocks,
)

logger = logging.getLogger(__name__)

HS300_INDEX_SYMBOL = "000300"
BENCHMARK_EM = "sh000300"
_HIST_BUFFER_CALENDAR = 120  # 63 交易日 + 缓冲


def to_jq_stock_code(code6: str) -> str:
    """6 位 A 股代码 → 聚宽格式（600519 → 600519.XSHG，000001 → 000001.XSHE）。"""
    c = str(code6).zfill(6)
    return f"{c}.XSHG" if c.startswith("6") else f"{c}.XSHE"


def from_jq_stock_code(code: str) -> str:
    """聚宽格式 → 6 位代码。"""
    return str(code).split(".")[0].zfill(6)


def _normalize_date(as_of: date | datetime | str) -> date:
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return pd.Timestamp(as_of).date()


def _date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def _index_cons() -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.index_stock_cons_csindex(symbol=HS300_INDEX_SYMBOL)

    return call_with_retry("hs300:index_cons", _go, validate=df_nonempty, max_retries=4)


def get_hs300_universe(as_of: date | datetime | str | None = None) -> Tuple[List[str], Dict[str, str]]:
    """返回 (6 位代码列表, {代码: 名称})。as_of 仅作日志，成分为最新披露。"""
    _ = _normalize_date(as_of) if as_of is not None else None
    df = _index_cons()
    code_col = "成分券代码" if "成分券代码" in df.columns else df.columns[0]
    name_col = "成分券名称" if "成分券名称" in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
    codes: List[str] = []
    names: Dict[str, str] = {}
    for _, row in df.iterrows():
        c = str(row[code_col]).zfill(6)
        codes.append(c)
        if name_col:
            names[c] = str(row[name_col])
    return sorted(set(codes)), names


def _spot_mcap(spot: pd.DataFrame | None, code: str) -> float:
    if spot is None or code not in spot.index:
        return np.nan
    row = spot.loc[code]
    for col in ("总市值", "市值", "流通市值"):
        v = pd.to_numeric(row.get(col), errors="coerce")
        if pd.notna(v) and float(v) > 0:
            return float(v)
    return np.nan


def _fetch_spot() -> pd.DataFrame | None:
    """A 股实时快照；优先东财（含总市值），失败再用新浪。"""

    def _sina() -> pd.DataFrame:
        df = ak.stock_zh_a_spot()
        out = df.copy()
        if "代码" in out.columns:
            out["代码"] = out["代码"].astype(str).str[-6:].str.zfill(6)
        return out

    def _em() -> pd.DataFrame:
        return ak.stock_zh_a_spot_em()

    for label, fn in (("stock_zh_a_spot_em", _em), ("stock_zh_a_spot", _sina)):
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
    return None


def _fetch_stock_hist(code: str, start: date, end: date) -> pd.DataFrame:
    from deep_value_funnel.hist_fetch import fetch_kline_qfq_normalized

    try:
        tx = fetch_kline_qfq_normalized(code, _date_str(start), _date_str(end))
        out = tx.copy()
        if "成交量" not in out.columns:
            out["成交量"] = np.nan
        return out
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
        out["成交量"] = pd.to_numeric(out.get("成交量"), errors="coerce")
        return out
    except Exception as exc:
        logger.warning("[%s] K 线拉取失败：%s", code, exc)
        return pd.DataFrame()


def _fetch_benchmark_closes(start: date, end: date) -> pd.Series:
    """沪深300 指数收盘价序列，index=date。"""
    cache_key = f"{_date_str(start)}_{_date_str(end)}"
    if not hasattr(_fetch_benchmark_closes, "_cache"):
        _fetch_benchmark_closes._cache = {}  # type: ignore[attr-defined]
    cached = _fetch_benchmark_closes._cache.get(cache_key)  # type: ignore[attr-defined]
    if cached is not None:
        return cached

    providers = [
        (
            "index_daily_em",
            lambda: ak.stock_zh_index_daily_em(
                symbol=BENCHMARK_EM,
                start_date=_date_str(start),
                end_date=_date_str(end),
            ),
        ),
        ("index_daily_sina", lambda: ak.stock_zh_index_daily(symbol=BENCHMARK_EM)),
        ("index_daily_tx", lambda: ak.stock_zh_index_daily_tx(symbol=BENCHMARK_EM)),
    ]

    for label, fn in providers:
        try:
            df = call_with_retry(label, fn, validate=df_nonempty, max_retries=2)
            d = df.copy()
            if "date" in d.columns:
                d["_d"] = pd.to_datetime(d["date"], errors="coerce")
                close_col = "close"
            elif "日期" in d.columns:
                d["_d"] = pd.to_datetime(d["日期"], errors="coerce")
                close_col = "收盘" if "收盘" in d.columns else "close"
            else:
                continue
            d[close_col] = pd.to_numeric(d[close_col], errors="coerce")
            d = d.dropna(subset=["_d", close_col])
            d = d[(d["_d"] >= pd.Timestamp(start)) & (d["_d"] <= pd.Timestamp(end))]
            if d.empty:
                continue
            s = d.sort_values("_d").set_index("_d")[close_col]
            s.index = s.index.date
            logger.info("沪深300 指数来源：%s（%d 根）", label, len(s))
            _fetch_benchmark_closes._cache[cache_key] = s  # type: ignore[attr-defined]
            return s
        except Exception as exc:
            logger.warning("沪深300 指数 %s 失败：%s", label, exc)

    raise RuntimeError("无法获取沪深300指数行情，请检查网络或稍后重试")


def get_trade_days_in_range(start: date, end: date) -> List[date]:
    """以沪深300指数有成交的交易日作为 A 股交易日历。"""
    closes = _fetch_benchmark_closes(start, end)
    return [d for d in closes.index if start <= d <= end]


def _klines_to_daily_map(hist: pd.DataFrame) -> Dict[date, dict]:
    if hist.empty or "日期" not in hist.columns:
        return {}
    d = hist.copy()
    d["日期"] = pd.to_datetime(d["日期"], errors="coerce").dt.date
    d["收盘"] = pd.to_numeric(d.get("收盘"), errors="coerce")
    d["成交量"] = pd.to_numeric(d.get("成交量"), errors="coerce")
    out: Dict[date, dict] = {}
    for _, row in d.iterrows():
        if pd.isna(row["日期"]):
            continue
        out[row["日期"]] = {"close": row["收盘"], "volume": row["成交量"]}
    return out


def _is_suspended_on_day(daily: Dict[date, dict], as_of: date) -> bool:
    """
    当日停牌：交易日无 K 线或收盘价无效。
    与聚宽 ``paused=True`` 对齐；不用成交量=0 判定（免费 K 线常有缺失/零量误杀）。
    """
    bar = daily.get(as_of)
    if bar is None:
        return True
    close = bar.get("close")
    if close is None or pd.isna(close) or float(close) <= 0:
        return True
    return False


def _had_suspension_in_window(
    daily: Dict[date, dict],
    trade_days: Sequence[date],
    as_of: date,
    lookback: int = SUSPENSION_LOOKBACK,
) -> bool:
    idx = [i for i, d in enumerate(trade_days) if d <= as_of]
    if not idx:
        return True
    end_i = idx[-1]
    start_i = max(0, end_i - lookback + 1)
    window = trade_days[start_i : end_i + 1]
    for d in window:
        if _is_suspended_on_day(daily, d):
            return True
    return False


def filter_stock_pool(
    stocks: Sequence[str],
    as_of: date | datetime | str,
    *,
    kline_map: Dict[str, Dict[date, dict]] | None = None,
    trade_days: Sequence[date] | None = None,
    names: Dict[str, str] | None = None,
    spot: pd.DataFrame | None = None,
) -> List[str]:
    """
    剔除当日停牌 + 过去 SUSPENSION_LOOKBACK 交易日内有过停牌的标的。

    与聚宽 ``jq_hs300_multi_factor_15d.py`` 一致：**不过滤 ST**（原需求未要求）。
    截面模式若未提供 kline_map，仅做当日快照近似（无法做 63 日停牌过滤，结果可能与聚宽不一致）。
    """
    as_of_d = _normalize_date(as_of)
    stock_list = list(stocks)
    if not stock_list:
        return []

    if kline_map is None:
        # 降级：仅判断当日可交易（与聚宽 63 日规则不一致，会打印警告）
        logger.warning(
            "未加载历史 K 线，跳过 63 日停牌过滤；结果可能与聚宽不一致。"
            "请使用 run_screen(..., strict_jq=True) 或回测模式。"
        )
        if spot is None:
            spot = _fetch_spot()
        kept: List[str] = []
        for code in stock_list:
            if spot is None:
                kept.append(code)
                continue
            if code not in spot.index:
                continue
            price = pd.to_numeric(spot.loc[code].get("最新价"), errors="coerce")
            if pd.isna(price) or float(price) <= 0:
                continue
            kept.append(code)
        return kept

    if trade_days is None:
        raise ValueError("回测模式需提供 trade_days")

    kept = []
    for code in stock_list:
        daily = kline_map.get(code, {})
        if _is_suspended_on_day(daily, as_of_d):
            continue
        if _had_suspension_in_window(daily, trade_days, as_of_d):
            continue
        kept.append(code)
    return kept


def _fetch_mcap_history(code: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_value_em(symbol=str(code).zfill(6))

    try:
        df = call_with_retry(f"{code}:value_em", _go, validate=df_nonempty, max_retries=3)
        out = df.copy()
        out["数据日期"] = pd.to_datetime(out["数据日期"], errors="coerce").dt.date
        out["总市值"] = pd.to_numeric(out["总市值"], errors="coerce")
        return out.dropna(subset=["数据日期"]).sort_values("数据日期")
    except Exception as exc:
        logger.debug("[%s] 市值历史失败：%s", code, exc)
        return pd.DataFrame()


def _fetch_roe_history(code: str) -> pd.DataFrame:
    sec = to_em_sec_code(code)

    def _go() -> pd.DataFrame:
        return ak.stock_financial_analysis_indicator_em(symbol=sec)

    try:
        df = call_with_retry(f"{code}:indicator_em", _go, validate=df_nonempty, max_retries=3)
        out = df.copy()
        out["REPORT_DATE"] = pd.to_datetime(out["REPORT_DATE"], errors="coerce").dt.date
        out["ROEJQ"] = pd.to_numeric(out.get("ROEJQ"), errors="coerce")
        return out.dropna(subset=["REPORT_DATE"]).sort_values("REPORT_DATE")
    except Exception as exc:
        logger.debug("[%s] ROE 历史失败：%s", code, exc)
        return pd.DataFrame()


def _mcap_as_of(mcap_df: pd.DataFrame, as_of: date, daily: Dict[date, dict] | None = None) -> float:
    """截面日总市值；优先东财估值序列中 <= as_of 最近一条（与聚宽 valuation 时点一致）。"""
    if not mcap_df.empty:
        sub = mcap_df[mcap_df["数据日期"] <= as_of]
        if not sub.empty:
            v = float(sub.iloc[-1]["总市值"])
            if np.isfinite(v) and v > 0:
                return v
    if daily:
        bar = daily.get(as_of)
        if bar and bar.get("close") is not None and pd.notna(bar["close"]):
            return float(bar["close"])
    return np.nan


def _roe_as_of(roe_df: pd.DataFrame, as_of: date) -> float:
    if roe_df.empty:
        return np.nan
    sub = roe_df[roe_df["REPORT_DATE"] <= as_of]
    if sub.empty:
        return np.nan
    v = float(sub.iloc[-1]["ROEJQ"])
    return v if np.isfinite(v) else np.nan


def fetch_factors(
    stocks: Sequence[str],
    as_of: date | datetime | str,
    *,
    kline_map: Dict[str, Dict[date, dict]] | None = None,
    mcap_cache: Dict[str, pd.DataFrame] | None = None,
    roe_cache: Dict[str, pd.DataFrame] | None = None,
    spot: pd.DataFrame | None = None,
) -> pd.DataFrame:
    as_of_d = _normalize_date(as_of)
    rows: dict[str, dict[str, float]] = {}

    if kline_map is None:
        for code in stocks:
            mcap_df = _fetch_mcap_history(code)
            prof = _fetch_roe_history(code)
            rows[code] = {
                "market_cap": _mcap_as_of(mcap_df, as_of_d),
                "roe": _roe_as_of(prof, as_of_d),
            }
        return pd.DataFrame.from_dict(rows, orient="index")

    mcap_cache = mcap_cache or {}
    roe_cache = roe_cache or {}
    for code in stocks:
        daily = kline_map.get(code, {})
        mcap_df = mcap_cache.get(code)
        if mcap_df is None:
            mcap_df = _fetch_mcap_history(code)
            mcap_cache[code] = mcap_df
        roe_df = roe_cache.get(code)
        if roe_df is None:
            roe_df = _fetch_roe_history(code)
            roe_cache[code] = roe_df
        rows[code] = {
            "market_cap": _mcap_as_of(mcap_df, as_of_d, daily),
            "roe": _roe_as_of(roe_df, as_of_d),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


class HS300AkShareStore:
    """回测用数据缓存：K 线、估值、ROE、交易日历。"""

    def __init__(self, start: date, end: date) -> None:
        self.start = start
        self.end = end
        self.load_start = start - timedelta(days=_HIST_BUFFER_CALENDAR)
        self.trade_days: List[date] = []
        self.bench_close: pd.Series = pd.Series(dtype=float)
        self.universe: List[str] = []
        self.names: Dict[str, str] = {}
        self.kline_map: Dict[str, Dict[date, dict]] = {}
        self.mcap_cache: Dict[str, pd.DataFrame] = {}
        self.roe_cache: Dict[str, pd.DataFrame] = {}

    def bootstrap(self, *, limit: int | None = None) -> None:
        ensure_request_identity()
        self.trade_days = get_trade_days_in_range(self.start, self.end)
        if not self.trade_days:
            raise RuntimeError(f"区间 {self.start}~{self.end} 无交易日")
        self.bench_close = _fetch_benchmark_closes(self.start, self.end)
        self.universe, self.names = get_hs300_universe()
        if limit is not None and limit > 0:
            self.universe = self.universe[: int(limit)]
        logger.info("沪深300 成分 %d 只，交易日 %d 天", len(self.universe), len(self.trade_days))

    def preload_klines(self, *, workers: int = 2, codes: Sequence[str] | None = None) -> None:
        workers = max(1, int(workers))
        codes = list(codes) if codes is not None else self.universe
        total = len(codes)

        def _one(code: str) -> tuple[str, Dict[date, dict]]:
            hist = _fetch_stock_hist(code, self.load_start, self.end)
            return code, _klines_to_daily_map(hist)

        if workers == 1:
            for i, code in enumerate(codes, 1):
                if i % 30 == 0 or i == total:
                    logger.info("K 线进度 %d/%d", i, total)
                c, daily = _one(code)
                self.kline_map[c] = daily
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_one, c): c for c in codes}
                done = 0
                for fut in as_completed(futs):
                    done += 1
                    if done % 30 == 0 or done == total:
                        logger.info("K 线进度 %d/%d", done, total)
                    try:
                        c, daily = fut.result()
                        self.kline_map[c] = daily
                    except Exception as exc:
                        code = futs[fut]
                        logger.warning("[%s] K 线预加载失败：%s", code, exc)

    def preload_klines_for_screen(self, as_of: date, *, workers: int = 2, codes: Sequence[str] | None = None) -> None:
        """截面选股：仅加载 as_of 前 lookback 窗口 K 线。"""
        self.end = as_of
        self.load_start = as_of - timedelta(days=_HIST_BUFFER_CALENDAR)
        self.trade_days = get_trade_days_in_range(self.load_start, as_of)
        self.preload_klines(workers=workers, codes=codes)

    def filter_pool(self, as_of: date) -> List[str]:
        return filter_stock_pool(
            self.universe,
            as_of,
            kline_map=self.kline_map,
            trade_days=self.trade_days,
            names=self.names,
        )

    def factors(self, stocks: Sequence[str], as_of: date) -> pd.DataFrame:
        return fetch_factors(
            stocks,
            as_of,
            kline_map=self.kline_map,
            mcap_cache=self.mcap_cache,
            roe_cache=self.roe_cache,
        )

    def close_prices(self, codes: Sequence[str], as_of: date) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for code in codes:
            bar = self.kline_map.get(code, {}).get(as_of)
            if bar and bar.get("close") is not None and pd.notna(bar["close"]):
                px = float(bar["close"])
                if px > 0:
                    out[code] = px
        return out

    def select_targets(self, as_of: date, top_n: int = TOP_N) -> List[str]:
        pool = self.filter_pool(as_of)
        factors = self.factors(pool, as_of)
        if factors.empty:
            return []
        scored = score_factors(factors)
        return select_top_stocks(scored, top_n)


def run_screen(
    as_of: date | datetime | str | None = None,
    *,
    top_n: int = TOP_N,
    workers: int = 2,
    limit: int | None = None,
    strict_jq: bool = True,
) -> pd.DataFrame:
    """
    单次截面选股。

    strict_jq=True（默认）：加载 K 线并做 63 日停牌过滤 + 截面日估值/ROE，尽量对齐聚宽逻辑。
    strict_jq=False：快速模式，跳过 63 日停牌过滤（与聚宽可能不一致）。
    """
    ensure_request_identity()
    from strategies.trade_calendar import resolve_screen_date

    if as_of is not None:
        as_of_d = _normalize_date(as_of)
        note = None
    else:
        as_of_d, note = resolve_screen_date(None, trading_day_only=False)
        if as_of_d is None:
            raise RuntimeError(note or "无法解析截面日")
    if note:
        logger.info(note)
    logger.info("截面日：%s（AkShare 成分为最新沪深300，聚宽为历史时点成分）", as_of_d)

    universe, names = get_hs300_universe(as_of_d)
    if limit is not None and limit > 0:
        universe = universe[: int(limit)]
        logger.info("调试模式：仅计算前 %d 只成分", len(universe))

    store = HS300AkShareStore(as_of_d - timedelta(days=_HIST_BUFFER_CALENDAR), as_of_d)
    store.universe = universe
    store.names = names

    if strict_jq:
        logger.info("聚宽对齐模式：加载 K 线并执行 63 日停牌过滤…")
        store.preload_klines_for_screen(as_of_d, workers=workers, codes=universe)
        pool = filter_stock_pool(
            universe,
            as_of_d,
            kline_map=store.kline_map,
            trade_days=store.trade_days,
        )
    else:
        pool = filter_stock_pool(universe, as_of_d)

    logger.info("过滤后：%d 只（聚宽同日请核对过滤后数量）", len(pool))

    workers = max(1, int(workers))
    rows: dict[str, dict[str, float]] = {}

    def _one(code: str) -> tuple[str, dict[str, float]]:
        daily = store.kline_map.get(code, {}) if strict_jq else {}
        mcap_df = store.mcap_cache.get(code)
        if mcap_df is None:
            mcap_df = _fetch_mcap_history(code)
            store.mcap_cache[code] = mcap_df
        roe_df = store.roe_cache.get(code)
        if roe_df is None:
            roe_df = _fetch_roe_history(code)
            store.roe_cache[code] = roe_df
        return code, {
            "market_cap": _mcap_as_of(mcap_df, as_of_d, daily if daily else None),
            "roe": _roe_as_of(roe_df, as_of_d),
        }

    if workers == 1:
        for i, code in enumerate(pool, 1):
            if i % 30 == 0 or i == len(pool):
                logger.info("因子进度 %d/%d", i, len(pool))
            c, fac = _one(code)
            rows[c] = fac
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            futs = {pool_exec.submit(_one, c): c for c in pool}
            done = 0
            total = len(pool)
            for fut in as_completed(futs):
                done += 1
                if done % 30 == 0 or done == total:
                    logger.info("因子进度 %d/%d", done, total)
                try:
                    c, fac = fut.result()
                    rows[c] = fac
                except Exception as exc:
                    logger.warning("[%s] 因子失败：%s", futs[fut], exc)

    raw = pd.DataFrame.from_dict(rows, orient="index")
    if raw.empty:
        raise RuntimeError("因子矩阵为空")

    scored = score_factors(raw)
    target = select_top_stocks(scored, top_n)

    out = scored.copy()
    out.insert(0, "聚宽代码", [to_jq_stock_code(c) for c in out.index])
    out.insert(1, "名称", [names.get(c, "") for c in out.index])
    out["market_cap_亿"] = out["market_cap"] / 1e8
    out["选中"] = out.index.isin(target)
    out = out.sort_values("composite_score", ascending=False)
    out.insert(0, "排名", range(1, len(out) + 1))
    return out


__all__ = [
    "HS300AkShareStore",
    "get_hs300_universe",
    "get_trade_days_in_range",
    "filter_stock_pool",
    "fetch_factors",
    "run_screen",
    "to_jq_stock_code",
    "from_jq_stock_code",
]
