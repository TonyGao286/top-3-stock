#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定股票（最多10只）关键数据收集 + 阈值评分 + 回撤评分，输出 Excel。

设计目标：
- 不再“全市场筛选”，而是对用户指定代码做深度分析与量化打分。
- 指标计算尽量复用 deep_value_funnel 现有逻辑（财务、分红、PE分位、回撤、节流重试）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty
from deep_value_funnel.owner_earnings import (
    fetch_balance_sheet_yearly_em,
    fetch_profit_sheet_yearly_em,
    resolve_owner_earnings_or_fcff_yield,
)
from deep_value_funnel.pe_hist_percentile import percentile_for_stock_baidu
from deep_value_funnel.retained_mcap_value import compute_retained_mcap_metrics
from deep_value_funnel.stage_dividend import (
    _calculate_ttm_dividend_yield,
    _fetch_fhps,
    _n_year_avg_payout,
    _pick_latest_dividend_yield,
)
from deep_value_funnel.stage_financial import (
    _check_roe_annual_ny_avg_first,
    _fetch_cashflow_yearly,
    _fetch_indicator,
    _latest_annual_indicator_row,
    _latest_report_row,
)
from deep_value_funnel.stage_market import compute_max_drawdown_250
from deep_value_funnel.symbols import to_em_h10_code, to_em_sec_code
from deep_value_funnel.hist_fetch import fetch_kline_qfq_normalized

MAX_CODES = 10


def _normalize_code(code: Any) -> Optional[str]:
    if code is None:
        return None
    s = str(code).strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        digits = digits[-6:]
    if len(digits) != 6:
        return None
    return digits.zfill(6)


def _try_fetch_spot_em(*, max_retries: int = 2) -> pd.DataFrame:
    """
    东财全市场快照易被风控；在“指定股票模式”里只做快速尝试，失败就走降级方案。
    """
    return call_with_retry(
        "stock_zh_a_spot_em",
        lambda: ak.stock_zh_a_spot_em(),
        validate=df_nonempty,
        max_retries=max_retries,
    )


def _fetch_listing_table() -> pd.DataFrame:
    return call_with_retry("stock_history_dividend", lambda: ak.stock_history_dividend(), validate=df_nonempty)


def _spot_row_for_codes(codes: List[str]) -> pd.DataFrame:
    wanted = list(codes)
    try:
        spot = _try_fetch_spot_em(max_retries=2).copy()
        spot["代码"] = spot["代码"].astype(str).str.extract(r"(\d{6})", expand=False).astype(str).str.zfill(6)
        out = spot[spot["代码"].isin(set(wanted))].copy()
        if not out.empty:
            return out
    except Exception:
        pass

    # ── 降级方案：逐股用百度估值 + 腾讯日K 兜底，避免依赖东财全量快照 ─────────
    rows: list[dict] = []

    def _baidu_series(code_6: str, indicator: str, period: str) -> pd.Series:
        df = call_with_retry(
            f"{code_6}:baidu:{indicator}:{period}",
            lambda: ak.stock_zh_valuation_baidu(symbol=str(code_6).zfill(6), indicator=indicator, period=period),
            validate=df_nonempty,
            max_retries=3,
        )
        s = pd.to_numeric(df.get("value"), errors="coerce")
        return s.dropna()

    def _baidu_latest_value(code_6: str, indicator: str, period: str) -> Optional[float]:
        try:
            s = _baidu_series(code_6, indicator, period)
            if s.empty:
                return None
            v = float(s.iloc[-1])
            return v if math.isfinite(v) else None
        except Exception:
            return None

    def _fetch_name(code_6: str) -> str:
        try:
            df = call_with_retry(
                f"{code_6}:individual_info",
                lambda: ak.stock_individual_info_em(symbol=str(code_6).zfill(6)),
                validate=df_nonempty,
                max_retries=2,
            )
            # 常见格式：item/value；尝试找“股票简称/名称”
            item_col = df.columns[0]
            val_col = df.columns[-1]
            hit = df[df[item_col].astype(str).str.contains("简称|名称", na=False)]
            if not hit.empty:
                return str(hit[val_col].iloc[0]).strip()
        except Exception:
            pass
        return ""

    for code in wanted:
        code6 = str(code).zfill(6)
        name = _fetch_name(code6)

        # 价格：用日K最新收盘兜底
        last_price = None
        try:
            end = pd.Timestamp.today()
            # 多取自然日，确保腾讯日K满足最少根数（交易日折算）
            start = end - pd.Timedelta(days=240)
            k = fetch_kline_qfq_normalized(code6, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
            if not k.empty:
                last_price = float(pd.to_numeric(k["收盘"], errors="coerce").dropna().iloc[-1])
        except Exception:
            last_price = None

        pe_ttm = _baidu_latest_value(code6, "市盈率(TTM)", "近五年")
        mcap_yi = _baidu_latest_value(code6, "总市值", "近十年")  # 单位：亿元（百度）
        mcap_yuan = (float(mcap_yi) * 1e8) if (mcap_yi is not None and math.isfinite(float(mcap_yi))) else None

        rows.append(
            {
                "代码": code6,
                "名称": name,
                "最新价": last_price,
                "市盈率-动态": pe_ttm,  # 在指定股模式下，用百度的 PE(TTM) 作为“当前 PE”近似
                "总市值": mcap_yuan,
            }
        )

    return pd.DataFrame(rows)


def _listing_date_map(codes: List[str]) -> Dict[str, Optional[date]]:
    listing = _fetch_listing_table()
    if "代码" not in listing.columns or "上市日期" not in listing.columns:
        return {c: None for c in codes}
    d = listing[["代码", "上市日期"]].copy()
    d["代码"] = d["代码"].astype(str).str.zfill(6)
    d["_ld"] = pd.to_datetime(d["上市日期"], errors="coerce").dt.date
    mp: Dict[str, Optional[date]] = {}
    for c in codes:
        hit = d[d["代码"] == c]
        mp[c] = (hit["_ld"].iloc[0] if not hit.empty else None)
    return mp


def _years_since(listing_date: Optional[date], as_of: Optional[date] = None) -> Optional[float]:
    if listing_date is None:
        return None
    as_of = as_of or date.today()
    days = (as_of - listing_date).days
    if days < 0:
        return None
    return float(days) / 365.25


def _score_min(value: Optional[float], threshold: float) -> Optional[float]:
    if value is None or (isinstance(value, float) and (math.isnan(value) or not math.isfinite(value))):
        return None
    if threshold <= 0:
        return None
    v = float(value)
    return float(min(100.0, max(0.0, 100.0 * v / float(threshold))))


def _score_max(value: Optional[float], threshold: float) -> Optional[float]:
    if value is None or (isinstance(value, float) and (math.isnan(value) or not math.isfinite(value))):
        return None
    if threshold <= 0:
        return None
    v = float(value)
    if v <= 0:
        return 0.0
    # <= threshold 满分；超过阈值按比例扣
    return float(min(100.0, max(0.0, 100.0 * float(threshold) / v)))


def _mean_ignore_none(vals: Iterable[Optional[float]]) -> Optional[float]:
    xs = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if not xs:
        return None
    return float(sum(xs) / len(xs))


def _row_num(row: Optional[pd.Series], key: str) -> Optional[float]:
    """安全读取 pandas Series 中的数值；NaN/缺失/非数都返回 None。"""
    if row is None:
        return None
    try:
        if key not in row.index:
            return None
    except Exception:
        return None
    v = pd.to_numeric(row.get(key), errors="coerce")
    if pd.isna(v):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _safe(callable_, default=None):
    """统一吞掉外部接口异常，便于让“某个失败”不影响其它指标。"""
    try:
        return callable_()
    except Exception:
        return default


@dataclass
class Collected:
    code: str
    name: str
    price: Optional[float]
    pe_dynamic: Optional[float]
    mcap_yuan: Optional[float]
    listing_date: Optional[date]
    listing_years: Optional[float]
    roe5_avg_pct: Optional[float]
    roe_latest_pct: Optional[float]
    gross_margin_pct: Optional[float]
    debt_asset_pct: Optional[float]
    ocf_np_ratio: Optional[float]
    fcf_yield_pct: Optional[float]
    fcf_method: str
    ocf_5y_all_positive: Optional[bool]
    retained_mcap_ratio: Optional[float]
    retained_sum_yuan_5y: Optional[float]
    mcap_anchor_yuan_5y: Optional[float]
    mcap_delta_yuan_5y: Optional[float]
    pe_5y_percentile: Optional[float]
    dividend_yield_pct: Optional[float]
    payout_5y_avg_pct: Optional[float]
    drawdown_250: Optional[float]


def collect_one(code_6: str, spot_row: pd.Series, listing_date: Optional[date]) -> Collected:
    code = str(code_6).zfill(6)
    name = str(spot_row.get("名称", "") or "").strip()

    price = pd.to_numeric(spot_row.get("最新价"), errors="coerce")
    pe_dyn = pd.to_numeric(spot_row.get("市盈率-动态"), errors="coerce")
    mcap = pd.to_numeric(spot_row.get("总市值"), errors="coerce")
    price_v = float(price) if pd.notna(price) else None
    pe_v = float(pe_dyn) if pd.notna(pe_dyn) else None
    mcap_v = float(mcap) if pd.notna(mcap) and float(mcap) > 0 else None

    sec_code = to_em_sec_code(code)
    em_h10 = to_em_h10_code(code)

    ind = _safe(lambda: _fetch_indicator(sec_code), default=pd.DataFrame())
    if ind is None:
        ind = pd.DataFrame()
    ok_roe5, roe5_avg = (False, None)
    if not ind.empty:
        ok_roe5, roe5_avg = _safe(lambda: _check_roe_annual_ny_avg_first(ind), default=(False, None))
    roe5_avg_v = float(roe5_avg) if (roe5_avg is not None and math.isfinite(float(roe5_avg))) else None

    latest = _safe(lambda: _latest_report_row(ind), default=None) if not ind.empty else None
    gross_margin_pct = _row_num(latest, "XSMLL")
    debt_asset_pct = _row_num(latest, "ZCFZL")
    ocf_np_ratio = _row_num(latest, "NCO_NETPROFIT")
    roe_latest_pct = _row_num(latest, "ROEJQ")

    annual = _safe(lambda: _latest_annual_indicator_row(ind), default=None) if not ind.empty else None
    cfy = _safe(lambda: _fetch_cashflow_yearly(em_h10), default=pd.DataFrame())
    if cfy is None:
        cfy = pd.DataFrame()

    ocf_5y_all_positive: Optional[bool] = None
    if not cfy.empty and "REPORT_DATE" in cfy.columns and "NETCASH_OPERATE" in cfy.columns:
        try:
            d = cfy.copy()
            d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
            annual_cf = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)].sort_values("_rd", ascending=False).head(5)
            if len(annual_cf) == 5:
                vals = pd.to_numeric(annual_cf["NETCASH_OPERATE"], errors="coerce")
                ocf_5y_all_positive = bool((vals > 0).all())
        except Exception:
            ocf_5y_all_positive = None

    fcf_yield_pct = None
    fcf_method = ""
    if annual is not None and mcap_v is not None:
        bs = _safe(lambda: fetch_balance_sheet_yearly_em(em_h10), default=pd.DataFrame())
        pl = _safe(lambda: fetch_profit_sheet_yearly_em(em_h10), default=pd.DataFrame())
        triple = _safe(
            lambda: resolve_owner_earnings_or_fcff_yield(mcap=mcap_v, annual_indicator=annual, bs=bs, pl=pl, cfy=cfy),
            default=(False, None, {}),
        )
        if triple is not None:
            _ok_y, y, meta = triple
            fcf_method = str((meta or {}).get("_method") or "")
            if y is not None and math.isfinite(float(y)):
                fcf_yield_pct = float(y) * 100.0

    retained_ratio = None
    retained_sum_yuan_5y = None
    mcap_anchor_yuan_5y = None
    mcap_delta_yuan_5y = None
    if mcap_v is not None:
        rm_pair = _safe(
            lambda: compute_retained_mcap_metrics(
                code_6=code,
                mcap_now_yuan=mcap_v,
                cfy=cfy,
                em_h10=em_h10,
                years_override=5,
            ),
            default=(False, {}),
        )
        if rm_pair is not None:
            _ok_r, rm = rm_pair
            rm = rm or {}
            r = rm.get("留存市值创造比_10年")
            if r is not None and math.isfinite(float(r)):
                retained_ratio = float(r)
            rs, ma, md = rm.get("十年累计留存_元"), rm.get("十年期初总市值锚点_元"), rm.get("十年总市值增量_元")
            retained_sum_yuan_5y = float(rs) if rs is not None and math.isfinite(float(rs)) else None
            mcap_anchor_yuan_5y = float(ma) if ma is not None and math.isfinite(float(ma)) else None
            mcap_delta_yuan_5y = float(md) if md is not None and math.isfinite(float(md)) else None

    pe_5y_pct = None
    if pe_v is not None and pe_v > 0:
        res = _safe(lambda: percentile_for_stock_baidu(code, pe_v), default=None)
        if res is not None:
            try:
                pe_5y_pct = float(res[0])
            except Exception:
                pe_5y_pct = None

    fh = _safe(lambda: _fetch_fhps(code), default=pd.DataFrame())
    if fh is None:
        fh = pd.DataFrame()
    dividend_yield_pct = None
    if not fh.empty:
        # 用 TTM 口径避免“一年多次分红”只取最新一笔导致偏小
        dv = None
        if price_v is not None:
            dv = _safe(lambda: _calculate_ttm_dividend_yield(fh, float(price_v)), default=None)
        if dv is None:
            dv = _safe(lambda: _pick_latest_dividend_yield(fh), default=None)
        if dv is not None and math.isfinite(float(dv)):
            dividend_yield_pct = float(dv) * 100.0
    payout_5y_avg_pct = _safe(lambda: _n_year_avg_payout(fh, ind, 5), default=None)

    drawdown_250 = None
    if price_v is not None:
        drawdown_250 = _safe(lambda: compute_max_drawdown_250(code, price_v), default=None)

    ly = _years_since(listing_date)

    return Collected(
        code=code,
        name=name,
        price=price_v,
        pe_dynamic=pe_v,
        mcap_yuan=mcap_v,
        listing_date=listing_date,
        listing_years=ly,
        roe5_avg_pct=roe5_avg_v,
        roe_latest_pct=roe_latest_pct,
        gross_margin_pct=gross_margin_pct,
        debt_asset_pct=debt_asset_pct,
        ocf_np_ratio=ocf_np_ratio,
        fcf_yield_pct=fcf_yield_pct,
        fcf_method=fcf_method,
        ocf_5y_all_positive=ocf_5y_all_positive,
        retained_mcap_ratio=retained_ratio,
        retained_sum_yuan_5y=retained_sum_yuan_5y,
        mcap_anchor_yuan_5y=mcap_anchor_yuan_5y,
        mcap_delta_yuan_5y=mcap_delta_yuan_5y,
        pe_5y_percentile=pe_5y_pct,
        dividend_yield_pct=dividend_yield_pct,
        payout_5y_avg_pct=payout_5y_avg_pct,
        drawdown_250=drawdown_250,
    )


def score_one(c: Collected) -> Dict[str, Any]:
    # Step1: 数据收集（直接落表）
    # Step2: 阈值评分：满足阈值=100，低于/高于阈值按比例扣分
    score_listing = None
    if c.listing_years is not None:
        score_listing = float(min(100.0, max(0.0, 100.0 * c.listing_years / float(config.LISTING_MIN_YEARS))))

    score_roe5 = _score_min(c.roe5_avg_pct, float(config.ROE_5Y_AVG_MIN))
    score_roe_latest = _score_min(c.roe_latest_pct, float(config.ROE_MIN))
    score_gm = _score_min(c.gross_margin_pct, float(config.GROSS_MARGIN_MIN))
    score_dar = _score_max(c.debt_asset_pct, float(config.DEBT_ASSET_RATIO_MAX))
    score_ocf_np = _score_min(c.ocf_np_ratio, 1.0)
    score_fcf = _score_min((c.fcf_yield_pct / 100.0) if c.fcf_yield_pct is not None else None, float(config.FCF_YIELD_MIN))
    score_retained = _score_min(c.retained_mcap_ratio, float(config.RETAINED_SURPLUS_MCMP_RATIO_MIN))
    score_pe_pct = _score_max(c.pe_5y_percentile, float(config.PE_TTM_5Y_PERCENTILE_MAX))
    score_div_yield = _score_min((c.dividend_yield_pct / 100.0) if c.dividend_yield_pct is not None else None, float(config.DIV_YIELD_MIN))
    score_payout5 = _score_min(c.payout_5y_avg_pct, float(config.PAYOUT_RATIO_MIN))

    # 经营现金流 5 年全为正：满足=100，否则 0（按需求可改成比例；这里保持清晰）
    score_ocf5 = None
    if c.ocf_5y_all_positive is not None:
        score_ocf5 = 100.0 if c.ocf_5y_all_positive else 0.0

    step2_scores = [
        score_listing,
        score_roe5,
        score_roe_latest,
        score_gm,
        score_dar,
        score_ocf_np,
        score_fcf,
        score_ocf5,
        score_retained,
        score_pe_pct,
        score_div_yield,
        score_payout5,
    ]
    score_step2 = _mean_ignore_none(step2_scores)

    # Step3: 回撤评分（阈值为满分，差距按比例扣分）
    dd_score = None
    if c.drawdown_250 is not None and math.isfinite(float(c.drawdown_250)):
        dd_score = _score_min(float(c.drawdown_250), float(config.DRAWDOWN_MIN))

    score_total = _mean_ignore_none([score_step2, dd_score])

    return {
        "代码": c.code,
        "名称": c.name,
        "最新价": c.price,
        "市盈率-动态": c.pe_dynamic,
        "总市值_快照": c.mcap_yuan,
        "上市日期": c.listing_date.isoformat() if c.listing_date else None,
        "上市年限": round(c.listing_years, 3) if c.listing_years is not None else None,
        "ROE加权_近五年年报算术平均_pct": c.roe5_avg_pct,
        "ROE加权_最近一期_pct": c.roe_latest_pct,
        "销售毛利率_最近一期pct": c.gross_margin_pct,
        "资产负债率_最近一期_pct": c.debt_asset_pct,
        "经营现金流净额_净利润比_最近一期": c.ocf_np_ratio,
        "现金流收益率_格林沃尔德OE_pct": c.fcf_yield_pct,
        "现金流收益率口径": c.fcf_method,
        "最近5个完整年度经营现金流均为正": c.ocf_5y_all_positive,
        "留存市值创造比_近5年": c.retained_mcap_ratio,
        "五年累计留存_元": c.retained_sum_yuan_5y,
        "五年期初总市值锚点_元": c.mcap_anchor_yuan_5y,
        "五年总市值增量_元": c.mcap_delta_yuan_5y,
        "PE近5年分位_pct": c.pe_5y_percentile,
        # 口径：优先 TTM（过去一年已实施现金分红 / 当前股价）
        "股息率_TTM_pct": c.dividend_yield_pct,
        # 兼容旧列名（避免下游可视化/历史报表断裂）
        "股息率_东财最近实施_pct": c.dividend_yield_pct,
        "近五年平均分红率_pct": c.payout_5y_avg_pct,
        "近250日最大回撤": c.drawdown_250,
        # 分项得分
        "评分_上市年限": score_listing,
        "评分_ROE5均值": score_roe5,
        "评分_ROE最近一期": score_roe_latest,
        "评分_毛利率": score_gm,
        "评分_资产负债率": score_dar,
        "评分_经营现金流净额/净利润": score_ocf_np,
        "评分_现金流收益率": score_fcf,
        "评分_5年经营现金流为正": score_ocf5,
        "评分_留存vs市值(5年)": score_retained,
        "评分_PE近5年分位": score_pe_pct,
        "评分_股息率": score_div_yield,
        "评分_5年平均分红率": score_payout5,
        "评分_Step2均分": score_step2,
        "评分_回撤": dd_score,
        "评分_总分(均分)": score_total,
    }


def run_codes(codes: List[str]) -> pd.DataFrame:
    codes_n = []
    for c in codes:
        n = _normalize_code(c)
        if n and n not in codes_n:
            codes_n.append(n)
    if not codes_n:
        return pd.DataFrame()
    if len(codes_n) > MAX_CODES:
        raise ValueError(f"最多支持指定 {MAX_CODES} 只股票")

    spot = _spot_row_for_codes(codes_n)
    listing_map = _listing_date_map(codes_n)

    rows: List[Dict[str, Any]] = []
    for code in codes_n:
        hit = spot[spot["代码"] == code]
        if hit.empty:
            rows.append({"代码": code, "名称": "", "错误": "未在行情快照中找到该代码（可能非A股/停牌/接口波动）"})
            continue
        c = collect_one(code, hit.iloc[0], listing_map.get(code))
        rows.append(score_one(c))

    df = pd.DataFrame(rows)
    # 以总分降序方便查看
    if "评分_总分(均分)" in df.columns:
        df = df.sort_values("评分_总分(均分)", ascending=False, na_position="last").reset_index(drop=True)
    return df


def save_report(df: pd.DataFrame, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Score", index=False)
        # 简单可读性：冻结首行
        ws = writer.sheets["Score"]
        ws.freeze_panes = "A2"
    return out_path

