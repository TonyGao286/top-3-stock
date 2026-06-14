"""
阶段 2（当前顺序）：分红「漏斗」——股息率（东财口径）与近三年平均分红率。

上游需已通过质量（财务）与 PE 分位初筛（尚未拉日 K）；``stock_fhps_detail_em`` 经 ``call_with_retry`` 拉取。

数据来源 ``ak.stock_fhps_detail_em``：
- ``现金分红-股息率``：东财在对应分配方案下给出的股息率（小数形式，如 0.05 表示 5%）。
- ``现金分红-现金分红比例``：与「每 10 股派息（元）」数值一致，可结合 ``总股本`` 与
  ``stock_financial_analysis_indicator_em`` 中的 ``PARENTNETPROFIT`` 还原分红占净利润比。
"""

from __future__ import annotations

import logging

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty

logger = logging.getLogger(__name__)


def _fetch_fhps(code: str) -> pd.DataFrame:
    def _go() -> pd.DataFrame:
        return ak.stock_fhps_detail_em(symbol=str(code).zfill(6))

    return call_with_retry(f"{code}:fhps_detail", _go, validate=df_nonempty)


def _pick_latest_dividend_yield(fh: pd.DataFrame) -> float | None:
    """取「已实施」方案中、报告期最新的一条股息率（小数）。"""
    if "方案进度" not in fh.columns:
        return None
    done = fh[fh["方案进度"].astype(str).str.contains("实施", na=False)].copy()
    if done.empty:
        return None
    done["_rd"] = pd.to_datetime(done["报告期"], errors="coerce")
    done = done.sort_values("_rd", ascending=False)
    for _, r in done.iterrows():
        dv = pd.to_numeric(r.get("现金分红-股息率"), errors="coerce")
        if pd.notna(dv):
            return float(dv)
    return None


def _resolve_cash_div_cols(fh: pd.DataFrame) -> tuple[str | None, str | None, str | None]:
    """
    解析分红明细表中可能用于计算的关键列：
    - 事件日期列（优先除权除息日，其次股权登记日，其次实施公告日/公告日）
    - 报告期列（用于分红率按年度归属）
    - 每10股派息列（东财接口一般为「现金分红-现金分红比例」）
    """
    cols = list(fh.columns)
    date_candidates = [
        "除权除息日",
        "股权登记日",
        "实施公告日",
        "公告日",
        "实施日期",
    ]
    dt_col = next((c for c in date_candidates if c in cols), None)
    report_col = "报告期" if "报告期" in cols else None
    cash_per10_col = "现金分红-现金分红比例" if "现金分红-现金分红比例" in cols else None
    return dt_col, report_col, cash_per10_col


def _calculate_ttm_dividend_yield(fh: pd.DataFrame, current_price: float) -> float | None:
    """
    计算过去 365 天 TTM 股息率（小数）：过去一年内所有“已实施”的现金分红（每股）加总 / 当前股价。

    说明：
    - 使用「每10股派息」字段（现金分红-现金分红比例）累加，避免“一年多次分红”被只取最新一笔误伤。
    - 时间锚点优先使用除权除息日，其次股权登记日/公告日等；若完全缺失则无法计算。
    """
    if fh is None or fh.empty or not current_price or float(current_price) <= 0:
        return None
    if "方案进度" not in fh.columns:
        return None

    done = fh[fh["方案进度"].astype(str).str.contains("实施", na=False)].copy()
    if done.empty:
        return None

    dt_col, _, cash_per10_col = _resolve_cash_div_cols(done)
    if dt_col is None or cash_per10_col is None:
        return None

    done["_dt"] = pd.to_datetime(done[dt_col], errors="coerce")
    done[cash_per10_col] = pd.to_numeric(done[cash_per10_col], errors="coerce")
    done = done.dropna(subset=["_dt", cash_per10_col])
    if done.empty:
        return None

    one_year_ago = pd.Timestamp.today().normalize() - pd.Timedelta(days=365)
    recent = done[done["_dt"] >= one_year_ago]
    if recent.empty:
        return None

    total_cash_per10 = float(recent[cash_per10_col].sum())
    if not total_cash_per10 or total_cash_per10 <= 0:
        return None

    ttm_cash_per_share = total_cash_per10 / 10.0
    return float(ttm_cash_per_share / float(current_price))


def _three_year_avg_payout(fh: pd.DataFrame, ind: pd.DataFrame) -> float | None:
    """
    计算最近三个「年报」分配方案（已实施）的分红率平均值（净利润口径，百分数）。

    单年分红率 = (每 10 股派息元数 / 10 * 总股本) / 归属母公司净利润 * 100。
    """
    return _n_year_avg_payout(fh, ind, 3)


def _n_year_avg_payout(fh: pd.DataFrame, ind: pd.DataFrame, n: int) -> float | None:
    """
    计算最近 N 个「年报」分配方案（已实施）的分红率平均值（净利润口径，百分数）。

    单年分红率 = (每 10 股派息元数 / 10 * 总股本) / 归属母公司净利润 * 100。
    """
    n = int(n)
    if n <= 0:
        return None
    if "方案进度" not in fh.columns:
        return None
    done = fh[fh["方案进度"].astype(str).str.contains("实施", na=False)].copy()
    if done.empty:
        return None

    # 报告期用于“分红归属年度”的口径：同一年内（年报/中报/季报等）所有已实施分红累加，避免一年多次分红漏算。
    _, report_col, cash_per10_col = _resolve_cash_div_cols(done)
    if report_col is None or cash_per10_col is None:
        return None

    done["_rd"] = pd.to_datetime(done[report_col], errors="coerce")
    done[cash_per10_col] = pd.to_numeric(done[cash_per10_col], errors="coerce")
    done["总股本"] = pd.to_numeric(done.get("总股本"), errors="coerce")
    done = done.dropna(subset=["_rd", cash_per10_col, "总股本"])
    if done.empty:
        return None

    done["_year"] = done["_rd"].dt.year
    done["_cash_total"] = (done[cash_per10_col] / 10.0) * done["总股本"]
    by_year = (
        done.groupby("_year", as_index=False)["_cash_total"]
        .sum()
        .sort_values("_year", ascending=False)
    )

    ind_local = ind.copy()
    ind_local["_rd"] = pd.to_datetime(ind_local["REPORT_DATE"], errors="coerce")

    payouts: list[float] = []
    for _, fr in by_year.iterrows():
        if len(payouts) >= n:
            break
        y = fr["_year"]
        cash_total = float(fr["_cash_total"])
        if not cash_total or cash_total <= 0:
            continue

        # 利润按年报口径：同自然年的 12 月报告期取最新一条（兼容 12-30/12-31 披露差异）
        hit = ind_local[(ind_local["_rd"].dt.year == y) & (ind_local["_rd"].dt.month == 12)].sort_values(
            "_rd", ascending=False
        )
        if hit.empty or "PARENTNETPROFIT" not in hit.columns:
            continue
        np_val = float(pd.to_numeric(hit.iloc[0]["PARENTNETPROFIT"], errors="coerce"))
        if np_val <= 0:
            continue
        payouts.append(cash_total / np_val * 100.0)

    if len(payouts) < n:
        return None
    return float(sum(payouts[:n]) / float(n))


def get_dividend_metrics_for_export(code: str, ind: pd.DataFrame) -> dict:
    """
    仅用于中间态 CSV：拉取分红送配并计算股息率 / 三年平均分红率，**不参与**分红硬条件过滤。

    与正式漏斗中 ``screen_dividend`` 使用同一套 ``call_with_retry`` 与计算公式，便于人工核对。
    """
    c = str(code).zfill(6)
    try:
        fh = _fetch_fhps(c)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] step2 分红展示数据拉取失败：%s", c, exc)
        return {
            "股息率_东财最近实施_小数": None,
            "股息率_最近实施_pct": None,
            "近三年平均分红率_pct": None,
            "分红数据备注": f"拉取失败: {exc!s}",
        }

    # 展示口径：优先给出 TTM（避免一年多次分红误判），并保留“最近实施单笔”用于排查。
    price = None
    try:
        # 指定股模式/流水线一般都会把最新价放在上游；这里导出函数无法保证有，故仅在能取到时算 TTM。
        price = None
    except Exception:
        price = None

    dv_latest = _pick_latest_dividend_yield(fh)
    dv_ttm = None
    if price is not None:
        dv_ttm = _calculate_ttm_dividend_yield(fh, float(price))
    payout = _three_year_avg_payout(fh, ind)
    dv_latest_pct = round(float(dv_latest) * 100, 4) if dv_latest is not None else None
    dv_ttm_pct = round(float(dv_ttm) * 100, 4) if dv_ttm is not None else None
    return {
        "股息率_东财最近实施_小数": dv_latest,
        "股息率_东财最近实施_pct": dv_latest_pct,
        "股息率_TTM_小数": dv_ttm,
        "股息率_TTM_pct": dv_ttm_pct,
        "近三年平均分红率_pct": payout,
        "分红数据备注": "",
    }


def screen_dividend(fin_result: dict) -> dict | None:
    """
    在已通过财务条件的 ``fin_result`` 上验证分红条件。

    ``fin_result`` 必须包含 ``indicator_df``（财务阶段缓存的主要指标表）。
    """
    code = str(fin_result["代码"]).zfill(6)
    ind = fin_result.get("indicator_df")
    if ind is None or ind.empty:
        return None
    price = fin_result.get("最新价")
    try:
        price_f = float(price) if price is not None else None
    except Exception:
        price_f = None

    try:
        fh = _fetch_fhps(code)
    except Exception:
        logger.exception("[%s] 拉取分红送配详情失败", code)
        return None

    # 股息率：使用 TTM（过去一年所有已实施现金分红 / 当前股价），避免一年多次分红导致“只取最新一笔”偏小。
    dv = _calculate_ttm_dividend_yield(fh, price_f) if price_f is not None else None
    avg_payout = _three_year_avg_payout(fh, ind)

    ok_yield = dv is not None and dv >= config.DIV_YIELD_MIN
    ok_payout = avg_payout is not None and avg_payout >= config.PAYOUT_RATIO_MIN
    if not (ok_yield or ok_payout):
        return None

    out = {k: v for k, v in fin_result.items() if k != "indicator_df"}
    out["股息率_TTM_小数"] = dv
    out["近三年平均分红率_pct"] = avg_payout
    out["分红条件说明"] = (
        "股息率达标" if ok_yield else ""
    ) + ("；" if ok_yield and ok_payout else "") + ("三年分红率达标" if ok_payout else "")
    return out
