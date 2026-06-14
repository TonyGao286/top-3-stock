"""
格林沃尔德式「扩张性资本支出」拆解，近似 **所有者盈余（Owner Earnings）** 收益率。

思路（与常见教材一致）：
- 用 **过去 N 个完整年报**（不含最新一年）的 ``固定资产 / 营业总收入`` 算术平均，作为资本密集度；
- **扩张性资本支出** ≈ 该均值 × ``max(0, 当年营业总收入 − 上年营业总收入)``；
- **维护性资本支出** ≈ ``购建固定资产…支付的现金``（东财 ``CONSTRUCT_LONG_ASSET``）− 扩张性部分，下限 0；
- **所有者盈余** ≈ 当年 **经营活动现金流净额**（``NETCASH_OPERATE``）− 维护性资本支出；
- **收益率** = 所有者盈余 ÷ 快照总市值，与 ``config.FCF_YIELD_MIN`` 比较（沿用原阈值名）。

无法合并三张年报表或历史不足时，可按 ``OWNER_EARNINGS_FALLBACK_FCFF`` 退回 ``indicator_em`` 的 ``FCFF_BACK``。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty

logger = logging.getLogger(__name__)


def fetch_balance_sheet_yearly_em(em_h10: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_balance_sheet_by_yearly_em(symbol=em_h10)

    return call_with_retry(f"{em_h10}:balance_yearly_em", _go, validate=df_nonempty)


def fetch_profit_sheet_yearly_em(em_h10: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_profit_sheet_by_yearly_em(symbol=em_h10)

    return call_with_retry(f"{em_h10}:profit_yearly_em", _go, validate=df_nonempty)


def merge_annual_statements(
    bs: pd.DataFrame,
    pl: pd.DataFrame,
    cfy: pd.DataFrame,
) -> pd.DataFrame:
    """按 ``REPORT_DATE`` 内连接资产负债表、利润表、现金流量表年报行。"""
    need_bs = {"REPORT_DATE", "FIXED_ASSET"}
    need_pl = {"REPORT_DATE", "TOTAL_OPERATE_INCOME"}
    need_cf = {"REPORT_DATE", "CONSTRUCT_LONG_ASSET", "NETCASH_OPERATE"}
    if not need_bs.issubset(bs.columns) or not need_pl.issubset(pl.columns) or not need_cf.issubset(cfy.columns):
        return pd.DataFrame()

    m = (
        cfy[list(need_cf)]
        .merge(bs[list(need_bs)], on="REPORT_DATE", how="inner")
        .merge(pl[list(need_pl)], on="REPORT_DATE", how="inner")
    )
    m["_rd"] = pd.to_datetime(m["REPORT_DATE"], errors="coerce")
    m = m[m["_rd"].notna()]
    m = m[(m["_rd"].dt.month == 12) & (m["_rd"].dt.day == 31)].copy()
    m = m.drop_duplicates(subset=["REPORT_DATE"], keep="first")
    m = m.sort_values("_rd", ascending=False).reset_index(drop=True)
    return m


def _fcff_yield_from_indicator(annual_row: pd.Series, mcap: float) -> tuple[float | None, dict[str, Any]]:
    meta: dict[str, Any] = {"_method": "FCFF_BACK"}
    if "FCFF_BACK" not in annual_row.index:
        return None, meta
    fcff = float(pd.to_numeric(annual_row["FCFF_BACK"], errors="coerce"))
    if not math.isfinite(fcff) or not math.isfinite(mcap) or mcap <= 0:
        return None, meta
    y = fcff / float(mcap)
    if not math.isfinite(y):
        return None, meta
    meta["FCFF_BACK_元"] = fcff
    return y, meta


def greenwald_owner_earnings_yield(
    merged: pd.DataFrame,
    mcap: float,
    *,
    hist_years: int | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """
    :returns: (``所有者盈余 / 总市值`` 或 ``None``, 审计字段)
    """
    meta: dict[str, Any] = {"_method": "greenwald_oe"}
    n_hist = int(hist_years if hist_years is not None else getattr(config, "OWNER_EARNINGS_PPE_SALES_HIST_YEARS", 5))
    n_hist = max(1, min(10, n_hist))
    need_rows = 1 + n_hist
    if merged is None or merged.empty or len(merged) < need_rows:
        meta["_fail_reason"] = f"合并年报不足 {need_rows} 期"
        return None, meta

    sales = pd.to_numeric(merged["TOTAL_OPERATE_INCOME"], errors="coerce")
    ppe = pd.to_numeric(merged["FIXED_ASSET"], errors="coerce")
    capex = pd.to_numeric(merged["CONSTRUCT_LONG_ASSET"], errors="coerce")
    ocf = pd.to_numeric(merged["NETCASH_OPERATE"], errors="coerce")

    d_sales = float(sales.iloc[0]) - float(sales.iloc[1])
    if not (math.isfinite(d_sales) and math.isfinite(float(sales.iloc[0])) and math.isfinite(float(sales.iloc[1]))):
        meta["_fail_reason"] = "营收变动无效"
        return None, meta
    delta_sales = max(0.0, d_sales)

    ratios: list[float] = []
    for i in range(1, need_rows):
        si = float(sales.iloc[i])
        pi = float(ppe.iloc[i])
        if not math.isfinite(si) or si <= 0:
            meta["_fail_reason"] = f"第{i}期营收无效"
            return None, meta
        if not math.isfinite(pi) or pi < 0:
            meta["_fail_reason"] = f"第{i}期固定资产无效"
            return None, meta
        ratios.append(pi / si)
    avg_ppe_sales = sum(ratios) / len(ratios)
    expansion_capex = avg_ppe_sales * delta_sales

    total_capex = float(capex.iloc[0])
    if not math.isfinite(total_capex) or total_capex < 0:
        meta["_fail_reason"] = "购建长期资产现金无效"
        return None, meta

    maintenance_capex = max(0.0, total_capex - expansion_capex)
    ocf0 = float(ocf.iloc[0])
    if not math.isfinite(ocf0):
        meta["_fail_reason"] = "经营现金流无效"
        return None, meta

    owner_earnings = ocf0 - maintenance_capex
    if not math.isfinite(mcap) or mcap <= 0:
        meta["_fail_reason"] = "总市值无效"
        return None, meta
    y = owner_earnings / float(mcap)
    if not math.isfinite(y):
        meta["_fail_reason"] = "收益率非有限"
        return None, meta

    meta.update(
        {
            "OE_用格林沃尔德": True,
            "OE_PPE销售额_历史N年均比": round(avg_ppe_sales, 6),
            "OE_历史均比年数": n_hist,
            "OE_营收变动_元": round(delta_sales, 2),
            "OE_扩张性资本支出_元": round(expansion_capex, 2),
            "OE_维护性资本支出_元": round(maintenance_capex, 2),
            "所有者盈余_元": round(owner_earnings, 2),
            "OE_经营现金流净额_元": round(ocf0, 2),
            "OE_购建长期资产支付_元": round(total_capex, 2),
        }
    )
    return y, meta


def resolve_owner_earnings_or_fcff_yield(
    *,
    mcap: float,
    annual_indicator: pd.Series,
    bs: pd.DataFrame,
    pl: pd.DataFrame,
    cfy: pd.DataFrame,
) -> tuple[bool, float | None, dict[str, Any]]:
    """
    按配置计算用于 **财务漏斗** 的收益率（所有者盈余优先，可退回 FCFF）。

    :returns: (是否通过 ``FCF_YIELD_MIN``, 收益率小数或 ``None``, 元数据)
    """
    thr = float(getattr(config, "FCF_YIELD_MIN", 0.10))
    use_gw = bool(getattr(config, "OWNER_EARNINGS_GREENWALD_ENABLE", True))
    fallback = bool(getattr(config, "OWNER_EARNINGS_FALLBACK_FCFF", True))

    if not use_gw:
        y_fc, meta_fc = _fcff_yield_from_indicator(annual_indicator, mcap)
        meta_fc["OE_用格林沃尔德"] = False
        meta_fc["_method"] = "FCFF_BACK"
        if y_fc is None:
            return False, None, meta_fc
        return y_fc >= thr, y_fc, meta_fc

    meta_gw: dict[str, Any] = {}

    merged = merge_annual_statements(bs, pl, cfy)
    if not merged.empty:
        y, meta_gw = greenwald_owner_earnings_yield(merged, mcap)
        if y is not None:
            meta_gw["_method"] = "greenwald_oe"
            return y >= thr, y, meta_gw
    if not fallback:
        meta_gw.setdefault("_fail_reason", "格林沃尔德路径失败或合并表为空")
        meta_gw["_method"] = "greenwald_oe_failed"
        meta_gw["OE_用格林沃尔德"] = False
        return False, None, meta_gw
    logger.debug("所有者盈余未算出，退回 FCFF_BACK：%s", meta_gw.get("_fail_reason", ""))

    y_fc, meta_fc = _fcff_yield_from_indicator(annual_indicator, mcap)
    out: dict[str, Any] = {**meta_gw, **meta_fc}
    out["OE_用格林沃尔德"] = False
    out["_method"] = "FCFF_BACK"
    for k in list(out.keys()):
        if k.startswith("OE_") and k != "OE_用格林沃尔德":
            del out[k]
    out["OE_用格林沃尔德"] = False
    if y_fc is None:
        return False, None, out
    return y_fc >= thr, y_fc, out
