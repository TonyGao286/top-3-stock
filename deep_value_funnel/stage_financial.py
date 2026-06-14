"""
阶段 1（新顺序下）：财务「漏斗」——**近五年年报 ROE 均值（首道）**、毛利率、单期 ROE（加权）、
资产负债率（``ZCFZL``）、
五年经营现金流、经营现金流/净利润、**所有者盈余收益率**（格林沃尔德维护/扩张资本开支，可退回 FCFF）、
十年「留存利润 vs 市值增量」检验。

设计要点：
- 本阶段作为 **质量初筛** 在 **PE 分位** 之前执行（见 ``pipeline.run_screening``），用于先砍掉绝大部分标的；
  日 K 在 **分红** 之后执行。
- **五年 ROE 均值**：同表最近 5 个年报（12-31）``ROEJQ`` 算术平均须 **>** ``ROE_5Y_AVG_MIN``，
  在拉取指标表后 **最先** 校验；单期 ROE、毛利率、资产负债率（``ZCFZL``）与「经营现金流/净利润」亦来自该表（最新一期）；
  **现金流收益率**（默认）：三张年报（``stock_balance_sheet_by_yearly_em``、``stock_profit_sheet_by_yearly_em``、
  ``stock_cash_flow_sheet_by_yearly_em``）合并后，按格林沃尔德方法估算 **所有者盈余** ÷ 快照 ``总市值``，
  阈值仍为 ``FCF_YIELD_MIN``；可配置退回 ``FCFF_BACK``（见 ``owner_earnings``）。
  「连续 5 个完整年度经营现金流为正」使用现金流量表年报。
- 「一美元留存一美元市值」：见 ``retained_mcap_value``（利润表年报 + 现金流量表分红现金 +
  百度近十年总市值锚点 vs 东财快照总市值）。
- 上述 akshare 调用均经 ``call_with_retry`` + 请求后节流。
"""

from __future__ import annotations

import logging
import math

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty
from deep_value_funnel.owner_earnings import (
    fetch_balance_sheet_yearly_em,
    fetch_profit_sheet_yearly_em,
    resolve_owner_earnings_or_fcff_yield,
)
from deep_value_funnel.retained_mcap_value import compute_retained_mcap_metrics
from deep_value_funnel.symbols import to_em_h10_code

logger = logging.getLogger(__name__)


def _fetch_indicator(sec_code: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_financial_analysis_indicator_em(symbol=sec_code)

    return call_with_retry(f"{sec_code}:indicator_em", _go, validate=df_nonempty)


def _fetch_cashflow_yearly(em_h10: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_cash_flow_sheet_by_yearly_em(symbol=em_h10)

    return call_with_retry(f"{em_h10}:cashflow_yearly", _go, validate=df_nonempty)


def _latest_report_row(ind: pd.DataFrame) -> pd.Series | None:
    """按报告期降序取最新一期。"""
    if ind.empty:
        return None
    d = ind.copy()
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    d = d.sort_values("_rd", ascending=False)
    return d.iloc[0]


def _check_roe_annual_ny_avg_first(ind: pd.DataFrame) -> tuple[bool, float | None]:
    """
    财务漏斗 **第一层**：最近 ``ROE_5Y_AVG_LOOKBACK_YEARS`` 个年报的 ``ROEJQ`` 算术平均
    须 **严格大于** ``ROE_5Y_AVG_MIN``（%）。
    """
    n_y = int(getattr(config, "ROE_5Y_AVG_LOOKBACK_YEARS", 5))
    thr = float(getattr(config, "ROE_5Y_AVG_MIN", 20.0))
    if ind.empty or "ROEJQ" not in ind.columns or "REPORT_DATE" not in ind.columns:
        return False, None
    d = ind.copy()
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    d = d[d["_rd"].notna()]
    d = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)].copy()
    d = d.sort_values("_rd", ascending=False).head(n_y)
    if len(d) < n_y:
        return False, None
    vals = pd.to_numeric(d["ROEJQ"], errors="coerce")
    if vals.isna().any():
        return False, None
    avg = float(vals.mean())
    if not math.isfinite(avg):
        return False, None
    return avg > thr, avg


def _latest_annual_indicator_row(ind: pd.DataFrame) -> pd.Series | None:
    """取最近一期年报（报告期末 12-31），用于 ``FCFF_BACK`` 等与年度口径匹配的字段。"""
    if ind.empty or "REPORT_DATE" not in ind.columns:
        return None
    d = ind.copy()
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    d = d[d["_rd"].notna()]
    d = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)]
    if d.empty:
        return None
    d = d.sort_values("_rd", ascending=False)
    return d.iloc[0]


def _check_gross_margin(latest: pd.Series) -> tuple[bool, float | None]:
    """销售毛利率（字段 ``XSMLL``，单位为 %）。"""
    if "XSMLL" not in latest.index:
        return False, None
    gm = float(pd.to_numeric(latest["XSMLL"], errors="coerce"))
    if pd.isna(gm):
        return False, None
    return gm >= config.GROSS_MARGIN_MIN, gm


def _check_roe_weighted(latest: pd.Series) -> tuple[bool, float | None]:
    """净资产收益率-加权（东财 ``ROEJQ``，单位为 %）。"""
    if "ROEJQ" not in latest.index:
        return False, None
    roe = float(pd.to_numeric(latest["ROEJQ"], errors="coerce"))
    if pd.isna(roe) or not math.isfinite(roe):
        return False, None
    return roe >= config.ROE_MIN, roe


def _check_debt_asset_ratio(latest: pd.Series) -> tuple[bool, float | None]:
    """资产负债率（东财 ``ZCFZL``，单位为 %）；须 **严格小于** ``config.DEBT_ASSET_RATIO_MAX``。"""
    if "ZCFZL" not in latest.index:
        return False, None
    dar = float(pd.to_numeric(latest["ZCFZL"], errors="coerce"))
    if pd.isna(dar) or not math.isfinite(dar):
        return False, None
    cap = float(getattr(config, "DEBT_ASSET_RATIO_MAX", 60.0))
    return dar < cap, dar


def _check_ocf_vs_np(latest: pd.Series) -> tuple[bool, float | None]:
    """
    最新一期：经营现金流净额 > 净利润。

    东财 ``NCO_NETPROFIT`` 为「经营活动产生的现金流量净额 / 归属母公司净利润」的比值，
    当其大于 1 时，等价于经营现金流高于净利润（同口径下的近似替代）。
    """
    if "NCO_NETPROFIT" not in latest.index:
        return False, None
    ratio = float(pd.to_numeric(latest["NCO_NETPROFIT"], errors="coerce"))
    if pd.isna(ratio):
        return False, None
    return ratio > 1.0, ratio


def _check_five_years_positive_ocf(cfy: pd.DataFrame) -> bool:
    """最近 5 个完整会计年度（年报 12-31）经营现金流净额均 > 0。"""
    if cfy.empty or "REPORT_DATE" not in cfy.columns or "NETCASH_OPERATE" not in cfy.columns:
        return False
    d = cfy.copy()
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    annual = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)].copy()
    annual = annual.sort_values("_rd", ascending=False).head(5)
    if len(annual) < 5:
        return False
    vals = pd.to_numeric(annual["NETCASH_OPERATE"], errors="coerce")
    return bool((vals > 0).all())


def screen_financials(row: pd.Series) -> dict | None:
    """
    对单行候选做财务深度过滤（**不要求**已计算 ``drawdown``）。

    必需列：``代码``、``名称``、``最新价``、``市盈率-动态``、``sec_code``（东财 ``600519.SH`` 形式）、
    ``总市值``（东财 ``stock_zh_a_spot_em``，用于所有者盈余或 FCFF 收益率分母）。
    资产负债率取 ``indicator_em`` 最新一期 ``ZCFZL``（%），须 **<** ``config.DEBT_ASSET_RATIO_MAX``。

    成功则返回字典（含 ``indicator_df``，**不含** ``drawdown``，由后续 K 线阶段写入）。
    """
    code = str(row["代码"]).zfill(6)
    sec = str(row["sec_code"])
    em_h10 = to_em_h10_code(code)

    try:
        ind = _fetch_indicator(sec)
    except Exception:
        logger.exception("[%s] 拉取主要财务指标失败", code)
        return None

    ok_roe5, roe5_avg = _check_roe_annual_ny_avg_first(ind)
    if not ok_roe5:
        logger.debug(
            "[%s] 五年 ROE 均值首道未通过：avg=%s 须 > %s",
            code,
            roe5_avg,
            getattr(config, "ROE_5Y_AVG_MIN", 20.0),
        )
        return None

    latest = _latest_report_row(ind)
    if latest is None:
        return None

    ok_gm, gm = _check_gross_margin(latest)
    ok_roe, roe = _check_roe_weighted(latest)
    ok_dar, dar = _check_debt_asset_ratio(latest)
    ok_ratio, nco_np = _check_ocf_vs_np(latest)
    if not (ok_gm and ok_roe and ok_dar and ok_ratio):
        return None

    mcap = float(pd.to_numeric(row.get("总市值"), errors="coerce"))
    if not math.isfinite(mcap) or mcap <= 0:
        return None
    annual = _latest_annual_indicator_row(ind)
    if annual is None:
        return None

    use_oe = bool(getattr(config, "OWNER_EARNINGS_GREENWALD_ENABLE", True))
    try:
        cfy = _fetch_cashflow_yearly(em_h10)
    except Exception:
        logger.exception("[%s] 拉取年度现金流量表失败", code)
        return None

    if use_oe:
        try:
            bs = fetch_balance_sheet_yearly_em(em_h10)
            pl = fetch_profit_sheet_yearly_em(em_h10)
        except Exception:
            logger.exception("[%s] 拉取资产负债表/利润表年报失败", code)
            return None
        ok_y, y_yield, oe_meta = resolve_owner_earnings_or_fcff_yield(
            mcap=mcap,
            annual_indicator=annual,
            bs=bs,
            pl=pl,
            cfy=cfy,
        )
    else:
        ok_y, y_yield, oe_meta = resolve_owner_earnings_or_fcff_yield(
            mcap=mcap,
            annual_indicator=annual,
            bs=pd.DataFrame(),
            pl=pd.DataFrame(),
            cfy=pd.DataFrame(),
        )

    if not ok_y:
        return None

    if not _check_five_years_positive_ocf(cfy):
        return None

    ok_rmv, rmv_meta = compute_retained_mcap_metrics(
        code_6=code,
        mcap_now_yuan=mcap,
        cfy=cfy,
        em_h10=em_h10,
    )
    if not ok_rmv:
        logger.debug("[%s] 留存市值创造：%s", code, rmv_meta.get("_fail_reason", ""))
        return None

    fcff_ann = float(pd.to_numeric(annual["FCFF_BACK"], errors="coerce"))
    rmv_out = {k: v for k, v in rmv_meta.items() if not str(k).startswith("_")}
    oe_export = {k: v for k, v in oe_meta.items() if not str(k).startswith("_")}
    return {
        "代码": code,
        "名称": row["名称"],
        "最新价": float(row["最新价"]),
        "市盈率-动态": float(row["市盈率-动态"]),
        "sec_code": sec,
        "总市值_快照": mcap,
        "ROE加权_五年平均_pct": round(float(roe5_avg), 4) if roe5_avg is not None else None,
        "销售毛利率_最近一期pct": gm,
        "ROE加权_最近一期_pct": roe,
        "资产负债率_最近一期_pct": dar,
        "经营现金流净额_净利润比_最近一期": nco_np,
        "自由现金流收益率_年报_pct": round(float(y_yield) * 100.0, 4) if y_yield is not None else None,
        "现金流收益率口径": oe_meta.get("_method"),
        "FCFF_BACK_最近年报元": fcff_ann if math.isfinite(fcff_ann) else None,
        **oe_export,
        **rmv_out,
        "indicator_df": ind,
    }
