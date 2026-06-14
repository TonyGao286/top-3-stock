"""
「一美元留存、一美元市值」近似检验（十年窗口）。

**累计留存（元）**：最近 ``N`` 个完整年报年度，每年
``归母净利润(PARENT_NETPROFIT) − 分配股利利润现金(ASSIGN_DIVIDEND_PORFIT)`` 之和
（与现金流量表年报按 ``REPORT_DATE`` 内连接；缺分红列按 0）。

**市值增量（元）**：东财快照 ``总市值``（元）− 百度股市通「总市值 / 近十年」序列中、
不晚于「今日 − N 年」的最近一条市值（亿元 × 1e8 → 元）。

通过条件：``总市值增量 / 累计留存 >= config.RETAINED_SURPLUS_MCMP_RATIO_MIN``（默认 1.0），
且分母 > 0、分子 > 0。
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty

logger = logging.getLogger(__name__)

_BAIDU_MCAP_PERIOD = "近十年"


def _fetch_profit_yearly(em_h10: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_profit_sheet_by_yearly_em(symbol=em_h10)

    return call_with_retry(f"{em_h10}:profit_yearly_em", _go, validate=df_nonempty)


def _fetch_baidu_mcap_series(code_6: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_zh_valuation_baidu(
            symbol=str(code_6).zfill(6),
            indicator="总市值",
            period=_BAIDU_MCAP_PERIOD,
        )

    return call_with_retry(f"{code_6}:baidu_mcap_10y", _go, validate=df_nonempty)


def _annual_mcap_yuan_at_or_before(mcap_hist: pd.DataFrame, cutoff: date) -> float | None:
    """
    取十年锚点总市值（元）：优先 ``date <= cutoff`` 的最后一条（亿元×1e8）；
    若无（例如锚点落在两条样本之间），则取 ``date >= cutoff`` 的最早一条；
    再不行则取近十年序列中最早一条。
    """
    if mcap_hist.empty or "date" not in mcap_hist.columns or "value" not in mcap_hist.columns:
        return None
    d = mcap_hist.copy()
    d["_d"] = pd.to_datetime(d["date"], errors="coerce").dt.date
    d = d[d["_d"].notna()].sort_values("_d")
    if d.empty:
        return None
    le = d[d["_d"] <= cutoff]
    pick = le.iloc[-1] if not le.empty else None
    if pick is None:
        ge = d[d["_d"] >= cutoff]
        pick = ge.iloc[0] if not ge.empty else d.iloc[0]
    v = float(pd.to_numeric(pick["value"], errors="coerce"))
    if not (v == v) or v <= 0:  # nan check
        return None
    return v * 1e8


def compute_retained_mcap_metrics(
    *,
    code_6: str,
    mcap_now_yuan: float,
    cfy: pd.DataFrame,
    em_h10: str,
    years_override: int | None = None,
) -> tuple[bool, dict]:
    """
    :returns: (是否通过硬条件, 审计用字段字典；未通过时 ok=False 且含 ``_fail_reason``)
    """
    years = int(years_override if years_override is not None else getattr(config, "RETAINED_VALUE_LOOKBACK_YEARS", 10))
    ratio_min = float(getattr(config, "RETAINED_SURPLUS_MCMP_RATIO_MIN", 1.0))

    out: dict = {
        "十年累计留存_元": None,
        "十年期初总市值锚点_元": None,
        "十年总市值增量_元": None,
        "留存市值创造比_10年": None,
    }

    if not math.isfinite(mcap_now_yuan) or mcap_now_yuan <= 0:
        out["_fail_reason"] = "当前总市值无效"
        return False, out

    try:
        profit = _fetch_profit_yearly(em_h10)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] 利润表年报拉取失败：%s", code_6, exc)
        out["_fail_reason"] = "利润表年报拉取失败"
        return False, out

    if (
        cfy.empty
        or "REPORT_DATE" not in cfy.columns
        or "ASSIGN_DIVIDEND_PORFIT" not in cfy.columns
    ):
        out["_fail_reason"] = "现金流量表缺分红字段"
        return False, out
    if "PARENT_NETPROFIT" not in profit.columns:
        out["_fail_reason"] = "利润表缺归母净利润"
        return False, out

    c_sub = cfy[["REPORT_DATE", "ASSIGN_DIVIDEND_PORFIT"]].copy()
    p_sub = profit[["REPORT_DATE", "PARENT_NETPROFIT"]].copy()
    c_sub["REPORT_DATE"] = pd.to_datetime(c_sub["REPORT_DATE"], errors="coerce")
    p_sub["REPORT_DATE"] = pd.to_datetime(p_sub["REPORT_DATE"], errors="coerce")
    merged = p_sub.merge(c_sub, on="REPORT_DATE", how="inner")
    merged = merged[merged["REPORT_DATE"].notna()]
    merged["_rd"] = merged["REPORT_DATE"]
    merged = merged[(merged["_rd"].dt.month == 12) & (merged["_rd"].dt.day == 31)].copy()
    merged = merged.sort_values("_rd", ascending=False).head(years)

    if len(merged) < years:
        out["_fail_reason"] = f"完整年报不足{years}期（仅{len(merged)}期）"
        return False, out

    npv = pd.to_numeric(merged["PARENT_NETPROFIT"], errors="coerce")
    div = pd.to_numeric(merged["ASSIGN_DIVIDEND_PORFIT"], errors="coerce").fillna(0.0)
    retained_series = npv - div
    sum_retained = float(retained_series.sum())
    if not math.isfinite(sum_retained) or sum_retained <= 0:
        out["_fail_reason"] = "十年累计留存非正，无法比较"
        out["十年累计留存_元"] = sum_retained if math.isfinite(sum_retained) else None
        return False, out

    try:
        mhist = _fetch_baidu_mcap_series(code_6)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] 百度市值序列拉取失败：%s", code_6, exc)
        out["_fail_reason"] = "百度市值序列拉取失败"
        return False, out

    cutoff = date.today() - timedelta(days=int(365.25 * years))
    mcap_past = _annual_mcap_yuan_at_or_before(mhist, cutoff)
    if mcap_past is None or mcap_past <= 0:
        out["_fail_reason"] = "无法取得十年锚点总市值"
        return False, out

    delta = float(mcap_now_yuan) - float(mcap_past)
    out["十年期初总市值锚点_元"] = round(mcap_past, 2)
    out["十年累计留存_元"] = round(sum_retained, 2)
    out["十年总市值增量_元"] = round(delta, 2)

    if delta <= 0:
        out["_fail_reason"] = "十年总市值增量非正"
        out["留存市值创造比_10年"] = round(delta / sum_retained, 6) if sum_retained else None
        return False, out

    ratio = delta / sum_retained
    out["留存市值创造比_10年"] = round(ratio, 6)
    ok = ratio >= ratio_min - 1e-12
    if not ok:
        out["_fail_reason"] = f"市值增量/留存 {ratio:.4f} < 阈值 {ratio_min}"
    return ok, out
