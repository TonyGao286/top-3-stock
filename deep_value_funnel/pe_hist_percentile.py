"""
当前 PE(TTM) 在其近 5 年可比历史中的「分位百分比」。

分位定义： historical 中满足 ``PE <= 当前快照 PE`` 的样本占比 ×100
（数值越高表示相对自身历史越贵；策略上保留分位不高于阈值的股票）。

AKShare 路径：百度股市通 ``stock_zh_valuation_baidu``（``市盈率(TTM)`` + ``近五年``）。
Tushare 路径：``daily_basic`` 按 ``ts_code`` + 起止日期拉取 ``pe_ttm`` 序列。
"""

from __future__ import annotations

from datetime import date, timedelta

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty

_BAIDU_PERIOD = "近五年"
_BAIDU_INDICATOR = "市盈率(TTM)"


def five_year_date_bounds() -> tuple[date, date]:
    """回溯窗口：约 5 个自然年（另加缓冲），用于 Tushare 起止日。"""
    end = date.today()
    start = end - timedelta(days=365 * 5 + 45)
    return start, end


def five_year_trade_range_str() -> tuple[str, str]:
    s, e = five_year_date_bounds()
    return s.strftime("%Y%m%d"), e.strftime("%Y%m%d")


def _percentile_leq(current_pe: float, hist: pd.Series) -> tuple[float, int] | None:
    s = pd.to_numeric(hist, errors="coerce").dropna()
    s = s[(s > 0) & (s < 1e7)]
    n = int(len(s))
    need = int(getattr(config, "PE_TTM_HIST_MIN_SAMPLES", 80))
    if n < need:
        return None
    cur = float(current_pe)
    pct = 100.0 * float((s <= cur).sum()) / float(n)
    return (pct, n)


def fetch_pe_ttm_hist_baidu(code_6: str) -> pd.Series:
    """百度近五年 PE(TTM) 日（或近似频次）序列。"""

    def _go() -> pd.DataFrame:
        return ak.stock_zh_valuation_baidu(
            symbol=str(code_6).zfill(6),
            indicator=_BAIDU_INDICATOR,
            period=_BAIDU_PERIOD,
        )

    df = call_with_retry(f"{code_6}:baidu_pe_ttm_5y", _go, validate=df_nonempty)
    return pd.to_numeric(df["value"], errors="coerce")


def fetch_pe_ttm_hist_tushare(pro, ts_code: str, start_yyyymmdd: str, end_yyyymmdd: str) -> pd.Series:
    """单只股票约 5 年的 ``pe_ttm``（日频）。"""

    def _go() -> pd.DataFrame:
        df = pro.daily_basic(
            ts_code=ts_code,
            start_date=start_yyyymmdd,
            end_date=end_yyyymmdd,
            fields="trade_date,pe_ttm",
        )
        return df if df is not None else pd.DataFrame()

    df = call_with_retry(
        f"{ts_code}:ts_pe_ttm_hist",
        _go,
        validate=df_nonempty,
    )
    return pd.to_numeric(df["pe_ttm"], errors="coerce")


def percentile_for_stock_baidu(code_6: str, current_pe: float) -> tuple[float, int] | None:
    hist = fetch_pe_ttm_hist_baidu(code_6)
    return _percentile_leq(current_pe, hist)


def percentile_for_stock_tushare(
    pro,
    ts_code: str,
    current_pe: float,
    start_yyyymmdd: str,
    end_yyyymmdd: str,
) -> tuple[float, int] | None:
    hist = fetch_pe_ttm_hist_tushare(pro, ts_code, start_yyyymmdd, end_yyyymmdd)
    return _percentile_leq(current_pe, hist)
