#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 single_stock_score_*.xlsx 可视化为一个可直接打开的 HTML。

特点：
- 不额外安装 Python 绘图库（内置 vendor/chart.umd.min.js，离线可打开）。
- 输出：single_stock_viz.html（默认），可用 --output 指定。
"""

from __future__ import annotations

import argparse
from datetime import datetime
import glob
import html
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd
import requests

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty
from deep_value_funnel.symbols import to_em_h10_code, to_em_sec_code

logger = logging.getLogger(__name__)

_CHART_VENDOR = Path(__file__).resolve().parent / "vendor" / "chart.umd.min.js"
_CHART_BUNDLE_NAME = "chart.umd.min.js"
_CHART_CDN_FALLBACK = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"


def _deploy_chartjs(out_html: Path) -> str:
    """将内置 Chart.js 复制到 HTML 同目录，返回相对 script src。"""
    if not _CHART_VENDOR.is_file():
        raise FileNotFoundError(
            f"缺少内置 Chart.js：{_CHART_VENDOR}。"
            "请从 Chart.js v4.4.3 发行包提取 dist/chart.umd.js 到该路径。"
        )
    dest = out_html.parent / _CHART_BUNDLE_NAME
    if not dest.exists() or dest.stat().st_mtime < _CHART_VENDOR.stat().st_mtime:
        shutil.copy2(_CHART_VENDOR, dest)
    return _CHART_BUNDLE_NAME


def _fetch_baidu_valuation_df(code_6: str, indicator: str, period: str) -> pd.DataFrame:
    """
    百度估值序列（兼容 akshare，但对空响应/不支持指标返回空表而非抛错）。
    """
    url = "https://gushitong.baidu.com/opendata"
    params = {
        "openapi": "1",
        "dspName": "iphone",
        "tn": "tangram",
        "client": "app",
        "query": indicator,
        "code": str(code_6).zfill(6),
        "word": "",
        "resource_id": "51171",
        "market": "ab",
        "tag": indicator,
        "chart_select": period,
        "industry_select": "",
        "skip_industry": "1",
        "finClientType": "pc",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data_json = r.json()
    try:
        chart_info = data_json["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"]
    except (KeyError, IndexError, TypeError):
        return pd.DataFrame()
    if not chart_info:
        return pd.DataFrame()
    body = chart_info[0].get("body") if isinstance(chart_info[0], dict) else None
    if not body:
        return pd.DataFrame()
    temp_df = pd.DataFrame(body)
    if temp_df.empty or temp_df.shape[1] < 2:
        return pd.DataFrame()
    temp_df = temp_df.iloc[:, :2].copy()
    temp_df.columns = ["date", "value"]
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.date
    temp_df["value"] = pd.to_numeric(temp_df["value"], errors="coerce")
    return temp_df.dropna(subset=["date", "value"])


def _pick_latest_xlsx(pattern: str = "single_stock_score_*.xlsx") -> Path:
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        raise FileNotFoundError(f"未找到评分结果文件：{pattern}")
    return Path(files[-1]).resolve()


def _to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    # pandas/numpy 类型转成 JSON 兼容类型
    recs: List[Dict[str, Any]] = []
    for r in df.to_dict(orient="records"):
        out: Dict[str, Any] = {}
        for k, v in r.items():
            if pd.isna(v):
                out[str(k)] = None
            elif hasattr(v, "item"):
                out[str(k)] = v.item()
            else:
                out[str(k)] = v
        recs.append(out)
    return recs


def _eval_time_str(ts: Optional[datetime] = None) -> str:
    ts = ts or datetime.now()
    return ts.strftime("%Y-%m-%d %H-%M")


def _unit_maps() -> tuple[dict[str, str], dict[str, str]]:
    """
    返回：
    - 单位映射（用于表格表头）
    - 数值后缀映射（用于前端渲染）
    """
    unit_label: dict[str, str] = {
        "最新价": "元",
        "市盈率-动态": "倍",
        "总市值_快照": "元",
        "ROE加权_近五年年报算术平均_pct": "%",
        "ROE加权_最近一期_pct": "%",
        "销售毛利率_最近一期pct": "%",
        "资产负债率_最近一期_pct": "%",
        "经营现金流净额_净利润比_最近一期": "倍",
        "现金流收益率_格林沃尔德OE_pct": "%",
        "留存市值创造比_近5年": "倍",
        "PE近5年分位_pct": "%",
        "PE(TTM)近5年历史分位_pct": "%",
        "股息率_TTM_pct": "%",
        "股息率_东财最近实施_pct": "%",
        "股息率近5年历史分位_pct": "%",
        "近五年平均分红率_pct": "%",
        "ROE加权近5年历史分位_pct": "%",
        "近250日最大回撤": "%",
        "评分_总分(均分)": "分",
        "评分_Step2均分": "分",
        "评分_回撤": "分",
    }
    suffix: dict[str, str] = {k: v for k, v in unit_label.items() if v in ("元", "倍", "%", "分")}
    return unit_label, suffix


def _fetch_indicator_em(code_6: str) -> pd.DataFrame:
    sec = to_em_sec_code(str(code_6).zfill(6))

    def _go() -> pd.DataFrame:
        return ak.stock_financial_analysis_indicator_em(symbol=sec)

    return call_with_retry(f"{code_6}:indicator_em", _go, validate=df_nonempty, max_retries=4)


def _annual_last_n(ind: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    if ind is None or ind.empty:
        return pd.DataFrame()
    d = ind.copy()
    if "REPORT_DATE" not in d.columns:
        return pd.DataFrame()
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    d = d[d["_rd"].notna()]
    d = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)].copy()
    d["year"] = d["_rd"].dt.year
    d = d.sort_values("year", ascending=False).drop_duplicates(subset=["year"], keep="first")
    d = d.head(int(n)).sort_values("year", ascending=True)
    return d


def _percentile_rank(values: List[float], current: Optional[float]) -> Optional[float]:
    """
    历史分位：在 values 的经验分布下，当前值所处的分位（0~100）。
    采用“<= current”的占比（包含自身），更直观且稳健。
    """
    if current is None or not pd.notna(current):
        return None
    s = pd.Series(values, dtype="float64")
    s = s[pd.notna(s)]
    if s.empty:
        return None
    cur = float(current)
    return float((s.le(cur).sum() / len(s)) * 100.0)


def _baidu_valuation_series(
    code_6: str,
    indicator: str,
    period: str = "近五年",
    *,
    max_retries: int = 4,
    silent_empty: bool = False,
) -> Tuple[list[pd.Timestamp], list[float]]:
    """
    百度估值序列。返回清洗后的 (dates, values)。

    silent_empty=True 时接口无数据不记 WARNING（用于股息率等多候选指标）。
    """

    def _go() -> pd.DataFrame:
        return _fetch_baidu_valuation_df(str(code_6).zfill(6), indicator, period)

    if silent_empty and int(max_retries) <= 1:
        try:
            df = _go()
        except Exception:
            return ([], [])
        if not df_nonempty(df):
            return ([], [])
    else:
        df = call_with_retry(
            f"{code_6}:baidu_valuation:{indicator}:{period}",
            _go,
            validate=df_nonempty,
            max_retries=int(max_retries),
        )
    d = df.copy()
    if "date" not in d.columns or "value" not in d.columns:
        return ([], [])
    d["_d"] = pd.to_datetime(d["date"], errors="coerce")
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    d = d.dropna(subset=["_d", "value"]).sort_values("_d", ascending=True)
    dates = [pd.Timestamp(x) for x in d["_d"].tolist()]
    vals = [float(x) for x in d["value"].tolist()]
    return dates, vals


def _div_yield_series_5y(code_6: str) -> list[float]:
    """
    股息率历史序列（尽量从百度估值取）。不同源的 indicator 命名可能变化，因此做多候选兜底。
    """
    candidates = [
        "股息率(%)",
        "股息率",
        "股息率(TTM)",
        "股息率(TTM,%)",
    ]
    for ind in candidates:
        try:
            _, vals = _baidu_valuation_series(
                code_6, indicator=ind, period="近五年", max_retries=1, silent_empty=True
            )
            if vals:
                return vals
        except Exception:
            continue
    return []


def _roe_yearly_last_5(code_6: str) -> list[float]:
    """
    近5个完整年报的 ROE加权（东财财务分析指标，ROEJQ）。
    """
    ind = _fetch_indicator_em(code_6)
    ann = _annual_last_n(ind, 5)
    if ann is None or ann.empty or "ROEJQ" not in ann.columns:
        return []
    vals = [float(v) for v in pd.to_numeric(ann["ROEJQ"], errors="coerce").dropna().tolist()]
    return vals


def _augment_history_percentiles(df: pd.DataFrame) -> pd.DataFrame:
    """
    为 xlsx Score 表补充：股息率/ROE/PE 的近5年历史分位（0~100）。

    - PE：百度估值“市盈率(TTM)”近五年序列。
    - 股息率：百度估值“股息率”近五年序列（多候选兜底）。
    - ROE：东财财务分析指标的年报 ROEJQ（近5个完整年报）。
    """
    if df is None or df.empty or "代码" not in df.columns:
        return df

    d = df.copy()

    pe_pct_col = "PE(TTM)近5年历史分位_pct"
    dy_pct_col = "股息率近5年历史分位_pct"
    roe_pct_col = "ROE加权近5年历史分位_pct"

    for c in (pe_pct_col, dy_pct_col, roe_pct_col):
        if c not in d.columns:
            d[c] = None

    cache_pe: dict[str, list[float]] = {}
    cache_dy: dict[str, list[float]] = {}
    cache_roe: dict[str, list[float]] = {}

    for i, r in d.iterrows():
        code = str(r.get("代码") or "").zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue

        # PE(TTM)
        try:
            vals_pe = cache_pe.get(code)
            if vals_pe is None:
                _, vals_pe = _baidu_valuation_series(code, indicator="市盈率(TTM)", period="近五年")
                cache_pe[code] = vals_pe
            if vals_pe:
                d.at[i, pe_pct_col] = _percentile_rank(vals_pe, vals_pe[-1])
        except Exception:
            pass

        # 股息率
        try:
            vals_dy = cache_dy.get(code)
            if vals_dy is None:
                vals_dy = _div_yield_series_5y(code)
                cache_dy[code] = vals_dy
            if vals_dy:
                d.at[i, dy_pct_col] = _percentile_rank(vals_dy, vals_dy[-1])
        except Exception:
            pass

        # ROE（年报）
        try:
            vals_roe = cache_roe.get(code)
            if vals_roe is None:
                vals_roe = _roe_yearly_last_5(code)
                cache_roe[code] = vals_roe
            if vals_roe:
                d.at[i, roe_pct_col] = _percentile_rank(vals_roe, vals_roe[-1])
        except Exception:
            pass

    return d


def _pe_ttm_yearly(code_6: str) -> Tuple[list[int], list[float]]:
    def _go() -> pd.DataFrame:
        return _fetch_baidu_valuation_df(str(code_6).zfill(6), "市盈率(TTM)", "近五年")

    df = call_with_retry(f"{code_6}:baidu_pe_ttm_5y", _go, validate=df_nonempty, max_retries=4)
    d = df.copy()
    if "date" not in d.columns or "value" not in d.columns:
        return ([], [])
    d["_d"] = pd.to_datetime(d["date"], errors="coerce")
    d = d[d["_d"].notna()].copy()
    d["year"] = d["_d"].dt.year
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    d = d.dropna(subset=["value"])
    grp = d.groupby("year")["value"].median().reset_index()
    grp = grp.sort_values("year", ascending=False).head(5).sort_values("year", ascending=True)
    years = [int(x) for x in grp["year"].tolist()]
    vals = [float(x) for x in grp["value"].tolist()]
    return years, vals


def _ocf_yearly(code_6: str) -> Tuple[list[int], list[float]]:
    """近5个完整年度的经营现金流净额（元）。"""
    em_h10 = to_em_h10_code(str(code_6).zfill(6))

    def _go() -> pd.DataFrame:
        return ak.stock_cash_flow_sheet_by_yearly_em(symbol=em_h10)

    df = call_with_retry(f"{code_6}:cashflow_yearly_em", _go, validate=df_nonempty, max_retries=4)
    d = df.copy()
    if "REPORT_DATE" not in d.columns or "NETCASH_OPERATE" not in d.columns:
        return ([], [])
    d["_rd"] = pd.to_datetime(d["REPORT_DATE"], errors="coerce")
    d = d[d["_rd"].notna()].copy()
    d = d[(d["_rd"].dt.month == 12) & (d["_rd"].dt.day == 31)]
    d["year"] = d["_rd"].dt.year
    d = d.sort_values("year", ascending=False).drop_duplicates(subset=["year"], keep="first")
    d = d.head(5).sort_values("year", ascending=True)
    yrs = [int(y) for y in d["year"].tolist()]
    vals = [float(v) for v in pd.to_numeric(d["NETCASH_OPERATE"], errors="coerce").tolist()]
    return yrs, vals


def _build_trend_payload(data: List[Dict[str, Any]]) -> dict[str, Any]:
    """
    为每只股票生成“低于80分项”的近5年趋势数据（尽量用可得的公开序列）。
    返回结构：
    { code6: { itemKey: {label, unit, years:[...], values:[...]} , ... }, ... }
    """
    out: dict[str, Any] = {}

    def low_items(rec: Dict[str, Any]) -> list[str]:
        keys: list[str] = []
        for k, v in rec.items():
            if not str(k).startswith("评分_"):
                continue
            if k in ("评分_总分(均分)", "评分_Step2均分", "评分_回撤"):
                continue
            try:
                if v is not None and float(v) < 80.0:
                    keys.append(str(k))
            except Exception:
                continue
        return keys

    for rec in data:
        code = str(rec.get("代码") or "").zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        items = low_items(rec)
        if not items:
            out[code] = {}
            continue

        trends: dict[str, Any] = {}
        ind: Optional[pd.DataFrame] = None
        ann: Optional[pd.DataFrame] = None

        need_indicator = any(
            k in items
            for k in (
                "评分_ROE5均值",
                "评分_ROE最近一期",
                "评分_毛利率",
                "评分_资产负债率",
                "评分_经营现金流净额/净利润",
            )
        )
        if need_indicator:
            try:
                ind = _fetch_indicator_em(code)
                ann = _annual_last_n(ind, 5)
            except Exception:
                ind = None
                ann = None

        if "评分_ROE5均值" in items or "评分_ROE最近一期" in items:
            if ann is not None and not ann.empty and "ROEJQ" in ann.columns:
                yrs = [int(y) for y in ann["year"].tolist()]
                vals = [float(v) for v in pd.to_numeric(ann["ROEJQ"], errors="coerce").tolist()]
                trends["评分_ROE5均值"] = {"label": "ROE加权（年报）", "unit": "%", "years": yrs, "values": vals}
                trends["评分_ROE最近一期"] = {"label": "ROE加权（年报）", "unit": "%", "years": yrs, "values": vals}

        if "评分_毛利率" in items:
            if ann is not None and not ann.empty and "XSMLL" in ann.columns:
                yrs = [int(y) for y in ann["year"].tolist()]
                vals = [float(v) for v in pd.to_numeric(ann["XSMLL"], errors="coerce").tolist()]
                trends["评分_毛利率"] = {"label": "销售毛利率（年报）", "unit": "%", "years": yrs, "values": vals}

        if "评分_资产负债率" in items:
            if ann is not None and not ann.empty and "ZCFZL" in ann.columns:
                yrs = [int(y) for y in ann["year"].tolist()]
                vals = [float(v) for v in pd.to_numeric(ann["ZCFZL"], errors="coerce").tolist()]
                trends["评分_资产负债率"] = {"label": "资产负债率（年报）", "unit": "%", "years": yrs, "values": vals}

        if "评分_经营现金流净额/净利润" in items:
            if ann is not None and not ann.empty and "NCO_NETPROFIT" in ann.columns:
                yrs = [int(y) for y in ann["year"].tolist()]
                vals = [float(v) for v in pd.to_numeric(ann["NCO_NETPROFIT"], errors="coerce").tolist()]
                trends["评分_经营现金流净额/净利润"] = {
                    "label": "经营现金流净额/净利润（年报口径指标）",
                    "unit": "倍",
                    "years": yrs,
                    "values": vals,
                }

        if "评分_PE近5年分位" in items:
            try:
                yrs, vals = _pe_ttm_yearly(code)
                if yrs:
                    trends["评分_PE近5年分位"] = {
                        "label": "PE(TTM) 年度中位（百度）",
                        "unit": "倍",
                        "years": yrs,
                        "values": vals,
                    }
            except Exception:
                pass

        if "评分_5年经营现金流为正" in items:
            try:
                yrs, vals = _ocf_yearly(code)
                if yrs:
                    trends["评分_5年经营现金流为正"] = {
                        "label": "经营现金流净额（年报，元）",
                        "unit": "元",
                        "years": yrs,
                        "values": vals,
                    }
            except Exception:
                pass

        out[code] = trends

    return out


def _score_meta() -> Dict[str, Dict[str, Any]]:
    """集中维护“分项→阈值/口径/单位/解读模板”的元信息，供前端解读使用。"""
    return {
        "评分_上市年限": {
            "label": "上市年限",
            "metricCol": "上市年限",
            "direction": "min",
            "threshold": float(config.LISTING_MIN_YEARS),
            "unit": "年",
            "thresholdText": f"≥{config.LISTING_MIN_YEARS}年",
            "description": "上市年限越长，历史财务可比性越强；不足时数据样本不足以验证长期质量。",
            "causes": ["上市时间较短", "重组/借壳上市使有效历史变短", "上市日期数据缺失"],
            "suggestions": ["人工核对历史财报年限", "把上市年限作为门槛而非分数项"],
        },
        "评分_ROE5均值": {
            "label": "ROE加权5年均值",
            "metricCol": "ROE加权_近五年年报算术平均_pct",
            "direction": "min",
            "threshold": float(config.ROE_5Y_AVG_MIN),
            "unit": "%",
            "thresholdText": f"≥{config.ROE_5Y_AVG_MIN}%（近5个年报算术平均）",
            "description": "5年平均ROE反映长期资本回报能力，是质量的“地基”。",
            "causes": ["近年盈利能力下行", "权益扩张稀释回报（增发/配股）", "行业景气下行", "一次性减值/坏账"],
            "suggestions": ["杜邦分解（销售净利率/资产周转率/权益乘数）", "对比同行业ROE中位数"],
        },
        "评分_ROE最近一期": {
            "label": "ROE加权最近一期",
            "metricCol": "ROE加权_最近一期_pct",
            "direction": "min",
            "threshold": float(config.ROE_MIN),
            "unit": "%",
            "thresholdText": f"≥{config.ROE_MIN}%（最新一期单期）",
            "description": "单期ROE提示当前阶段盈利状态；与5年均值对比可看趋势拐点。",
            "causes": ["半年报/三季报存在季节性（年化前会偏低）", "毛利下滑或费用上升", "再融资稀释净资产", "汇兑/投资性损益干扰"],
            "suggestions": ["关注最新季度净利润同比/环比", "确认报告期类型（年报/半年/季）"],
        },
        "评分_毛利率": {
            "label": "销售毛利率",
            "metricCol": "销售毛利率_最近一期pct",
            "direction": "min",
            "threshold": float(config.GROSS_MARGIN_MIN),
            "unit": "%",
            "thresholdText": f"≥{config.GROSS_MARGIN_MIN}%",
            "description": "毛利率反映产品/服务的定价权与成本结构。",
            "causes": ["原材料/人工成本上升", "降价/折扣/促销加大", "产品结构变化（低毛利占比上升）", "汇率/关税影响进出口业务"],
            "suggestions": ["看主业分部毛利率", "对比行业可比公司毛利率"],
        },
        "评分_资产负债率": {
            "label": "资产负债率",
            "metricCol": "资产负债率_最近一期_pct",
            "direction": "max",
            "threshold": float(config.DEBT_ASSET_RATIO_MAX),
            "unit": "%",
            "thresholdText": f"≤{config.DEBT_ASSET_RATIO_MAX}%",
            "description": "资产负债率越低意味着杠杆压力越小、抵御风险的余地越大。",
            "causes": ["有息负债扩张（短期/长期借款上升）", "应付款/合同负债增加", "资产端缩表（计提减值压低净资产）", "回购/分红压缩净资产"],
            "suggestions": ["核查有息负债占比", "用流动比率/现金比率验证短期偿债能力"],
        },
        "评分_经营现金流净额/净利润": {
            "label": "经营现金流/净利润",
            "metricCol": "经营现金流净额_净利润比_最近一期",
            "direction": "min",
            "threshold": 1.0,
            "unit": "倍",
            "thresholdText": "≥1.0倍",
            "description": "比值反映“账面利润是否落实为现金”，<1常意味着盈利质量打折。",
            "causes": ["应收账款/合同资产上升", "存货上升占用资金", "预收/合同负债下降", "投资性收益/公允价值变动等非现金利润"],
            "suggestions": ["跟踪应收账款周转天数（DSO）", "看现金流量表与利润表的差异科目"],
        },
        "评分_现金流收益率": {
            "label": "现金流收益率（OE/总市值）",
            "metricCol": "现金流收益率_格林沃尔德OE_pct",
            "direction": "min",
            "threshold": float(config.FCF_YIELD_MIN),
            "unit": "%",
            "thresholdText": f"≥{int(config.FCF_YIELD_MIN * 100)}%",
            "description": "用所有者盈余近似现金流收益率，越高表示对当前市值越“便宜”。",
            "causes": ["市值偏高（估值贵）", "经营现金流不足", "维护性资本支出过大", "公司处于扩产期"],
            "suggestions": ["核对FCFF/所有者盈余分子构成", "结合PE分位/PB综合判断估值"],
        },
        "评分_5年经营现金流为正": {
            "label": "5年经营现金流均为正",
            "metricCol": "最近5个完整年度经营现金流均为正",
            "direction": "bool",
            "threshold": None,
            "unit": "",
            "thresholdText": "近5个完整年度均>0",
            "description": "如果某一年OCF为负，提示业务“失血”；要重点核查具体年份的扰动。",
            "causes": ["某一年应收/存货异常上升", "行业下行使回款变差", "重大投资活动占用经营现金", "一次性诉讼/赔付/罚款"],
            "suggestions": ["定位异常年份，看现金流量表附注", "结合行业景气周期判断是否系统性"],
        },
        "评分_留存vs市值(5年)": {
            "label": "留存vs市值（近5年）",
            "metricCol": "留存市值创造比_近5年",
            "direction": "min",
            "threshold": float(config.RETAINED_SURPLUS_MCMP_RATIO_MIN),
            "unit": "倍",
            "thresholdText": f"≥{config.RETAINED_SURPLUS_MCMP_RATIO_MIN}倍",
            "description": "巴菲特“1美元留存创造1美元市值”的检验：市值增量/累计留存。",
            "causes": ["近5年市值增量为负（估值压缩、股价回落）", "累计留存被高分红抵消", "周期性行业景气切换", "行业Beta/利率上行使估值中枢下移"],
            "suggestions": ["看近5年PE(TTM)中枢变动", "对照同业市值增量与利润累计"],
            "special": "retained",
        },
        "评分_PE近5年分位": {
            "label": "PE近5年分位",
            "metricCol": "PE近5年分位_pct",
            "direction": "max",
            "threshold": float(config.PE_TTM_5Y_PERCENTILE_MAX),
            "unit": "%",
            "thresholdText": f"≤{config.PE_TTM_5Y_PERCENTILE_MAX}%（自身近5年分位）",
            "description": "当前PE在自身近5年分布中所处位置；越低代表相对历史越便宜。",
            "causes": ["市场情绪推升估值", "盈利下行被动抬PE", "行业整体扩张估值"],
            "suggestions": ["等待估值回落或寻找PE分位更低的同业", "对比PB分位辅证"],
        },
        "评分_股息率": {
            "label": "股息率（最近实施）",
            "metricCol": "股息率_东财最近实施_pct",
            "direction": "min",
            "threshold": float(config.DIV_YIELD_MIN),
            "unit": "%",
            "thresholdText": f"≥{int(config.DIV_YIELD_MIN * 100)}%",
            "description": "股息率体现“现价对应的现金回报”。",
            "causes": ["分红比例较低，倾向再投资", "股价上涨摊薄股息率", "近期未分红或方案未实施"],
            "suggestions": ["看历史分红连续性（5/10年）", "结合分红率与现金流匹配"],
        },
        "评分_5年平均分红率": {
            "label": "近5年平均分红率",
            "metricCol": "近五年平均分红率_pct",
            "direction": "min",
            "threshold": float(config.PAYOUT_RATIO_MIN),
            "unit": "%",
            "thresholdText": f"≥{config.PAYOUT_RATIO_MIN}%",
            "description": "公司近5年分红/净利润的平均比率，体现回报股东的意愿与可持续性。",
            "causes": ["扩张期留存利润再投资", "亏损年份无法分红", "现金流不足以支撑高分红"],
            "suggestions": ["看未来3年资本开支计划", "把股息率与分红率结合判断"],
        },
    }


def build_html(
    data: List[Dict[str, Any]],
    title: str,
    *,
    eval_time: str,
    trends: dict[str, Any],
    chart_js_src: str = _CHART_BUNDLE_NAME,
) -> str:
    safe_title = html.escape(title)
    js_data = json.dumps(data, ensure_ascii=False)
    js_trends = json.dumps(trends, ensure_ascii=False)
    js_score_meta = json.dumps(_score_meta(), ensure_ascii=False)

    # 选取最关键的分项得分字段（若未来列名变化，这里也尽量稳健）
    score_cols = [
        "评分_总分(均分)",
        "评分_Step2均分",
        "评分_回撤",
        "评分_上市年限",
        "评分_ROE5均值",
        "评分_ROE最近一期",
        "评分_毛利率",
        "评分_资产负债率",
        "评分_经营现金流净额/净利润",
        "评分_现金流收益率",
        "评分_5年经营现金流为正",
        "评分_留存vs市值(5年)",
        "评分_PE近5年分位",
        "评分_股息率",
        "评分_5年平均分红率",
    ]

    # 关键指标字段（用于表格展示）
    metric_cols = [
        "代码",
        "名称",
        "最新价",
        "市盈率-动态",
        "PE(TTM)近5年历史分位_pct",
        "ROE加权_近五年年报算术平均_pct",
        "ROE加权_最近一期_pct",
        "ROE加权近5年历史分位_pct",
        "销售毛利率_最近一期pct",
        "资产负债率_最近一期_pct",
        "经营现金流净额_净利润比_最近一期",
        "现金流收益率_格林沃尔德OE_pct",
        "留存市值创造比_近5年",
        "五年累计留存_元",
        "五年期初总市值锚点_元",
        "五年总市值增量_元",
        "PE近5年分位_pct",
        "股息率_TTM_pct",
        "股息率近5年历史分位_pct",
        "近五年平均分红率_pct",
        "近250日最大回撤",
    ]

    unit_label, suffix_map = _unit_maps()

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <script src="{html.escape(chart_js_src)}"
    onerror="this.onerror=null;this.src='{html.escape(_CHART_CDN_FALLBACK)}';"></script>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 16px; color: #111; }}
    h1 {{ font-size: 18px; margin: 0 0 10px; }}
    .row {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    @media (min-width: 1100px) {{ .row {{ grid-template-columns: 1.2fr 1fr; }} }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; background: #fff; }}
    .muted {{ color: #6b7280; font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #f1f5f9; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; position: sticky; top: 0; }}
    .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #f1f5f9; }}
    .grid2 {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
    @media (min-width: 900px) {{ .grid2 {{ grid-template-columns: 1fr 1fr; }} }}
  </style>
</head>
<body>
  <h1>{safe_title}</h1>
  <div class="muted">评估时间：{html.escape(eval_time)}</div>
  <div class="muted" style="margin-top:6px;">
    说明：分数为 0~100；满足阈值记 100 分，偏离阈值按比例扣分（如“至少 X”则用 实际/X×100，“至多 Y”则用 Y/实际×100，上限 100、下限 0）。
    “Step2均分”是除回撤外的各项基础条件得分的算术平均；“总分(均分)”是 Step2均分 与 回撤得分 的算术平均（忽略缺失项）。
  </div>

  <div class="row" style="margin-top:12px;">
    <div class="card">
      <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
        <div><span class="pill">总分/关键分项</span></div>
        <div class="muted" id="meta"></div>
      </div>
      <canvas id="scoreChart" height="140"></canvas>
    </div>

    <div class="card">
      <div><span class="pill">分项评分（选中股票）</span></div>
      <div class="muted" style="margin:6px 0;">点击左侧图例或表格行可切换股票。</div>
      <canvas id="detailChart" height="180"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:14px;">
    <div><span class="pill">关键指标明细</span></div>
    <div style="max-height: 520px; overflow:auto; margin-top:8px;">
      <table id="tbl"></table>
    </div>
  </div>

  <div class="card" style="margin-top:14px;">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;">
      <div><span class="pill">低于80分项：详细解读 + 近5年趋势</span></div>
      <div class="muted" id="lowSummary"></div>
    </div>
    <div class="muted" style="margin-top:6px;">
      对当前选中股票得分 &lt; 80 的每个分项，给出“阈值口径 / 实际值 / 可能原因 / 建议核查”，并在数据可得时绘制近5年趋势。
    </div>
    <div id="lowBox" class="grid2" style="margin-top:10px;"></div>
  </div>

  <script>
    if (typeof Chart === "undefined") {{
      const warn = document.createElement("div");
      warn.style.cssText = "margin:12px 0;padding:12px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#991b1b;";
      warn.textContent = "图表库未加载：请确认与 HTML 同目录存在 {html.escape(chart_js_src)}，或检查网络后刷新。";
      document.body.insertBefore(warn, document.body.firstChild.nextSibling);
      throw new Error("Chart.js not loaded");
    }}

    const DATA = {js_data};
    const TRENDS = {js_trends};
    const SCORE_META = {js_score_meta};
    const SCORE_COLS = {json.dumps(score_cols, ensure_ascii=False)};
    const METRIC_COLS = {json.dumps(metric_cols, ensure_ascii=False)};
    const UNIT_LABEL = {json.dumps(unit_label, ensure_ascii=False)};
    const SUFFIX = {json.dumps(suffix_map, ensure_ascii=False)};

    function fmtNumber(v, decimals=3) {{
      if (v === null || v === undefined) return "";
      if (typeof v !== "number") return String(v);
      if (!Number.isFinite(v)) return "";
      const abs = Math.abs(v);
      if (abs >= 1e8) return v.toFixed(0);
      if (abs >= 1000) return v.toFixed(0);
      return v.toFixed(decimals).replace(/\\.0+$/,"").replace(/(\\.\\d*[1-9])0+$/,"$1");
    }}

    function fmtYuanCN(v) {{
      if (v === null || v === undefined) return "";
      if (typeof v !== "number" || !Number.isFinite(v)) return "";
      const abs = Math.abs(v);
      const sign = v < 0 ? "-" : "";
      // 以更“人话”的数量级展示：万 / 亿 / 万亿（覆盖你提到的十亿/百亿等情形）
      if (abs >= 1e12) return sign + fmtNumber(abs / 1e12, 3) + "万亿";
      if (abs >= 1e8)  return sign + fmtNumber(abs / 1e8,  3) + "亿";
      if (abs >= 1e4)  return sign + fmtNumber(abs / 1e4,  3) + "万";
      return sign + fmtNumber(abs, 0) + "元";
    }}

    function fmtWithUnit(col, v) {{
      if (v === null || v === undefined) return "";
      if (typeof v !== "number") return String(v);
      let decimals = 3;
      if (String(col).includes("评分_")) decimals = 1;
      if (String(col).includes("最新价")) decimals = 3;
      if (String(col).includes("市盈率")) decimals = 2;
      // 大额“元”字段用中文计数法展示（避免一长串）
      if (String(col).includes("总市值") || String(col).endsWith("_元")) {{
        return fmtYuanCN(v);
      }}
      if (String(col).includes("分位") || String(col).endsWith("_pct") || String(col).includes("最大回撤")) decimals = 2;
      if (String(col).includes("近250日最大回撤")) {{
        return fmtNumber(v * 100.0, 2) + "%";
      }}
      const s = fmtNumber(v, decimals);
      const suf = SUFFIX[col] || "";
      return suf ? (s + suf) : s;
    }}

    const labels = DATA.map(r => `${{r["代码"]||""}} ${{r["名称"]||""}}`.trim());
    document.getElementById("meta").textContent = `股票数：${{DATA.length}}`;

    function getCol(col) {{
      return DATA.map(r => r[col] ?? null);
    }}

    const scoreCtx = document.getElementById("scoreChart");
    const scoreChart = new Chart(scoreCtx, {{
      type: "bar",
      data: {{
        labels,
        datasets: [
          {{ label: "总分", data: getCol("评分_总分(均分)"), backgroundColor: "#2563eb" }},
          {{ label: "Step2均分", data: getCol("评分_Step2均分"), backgroundColor: "#10b981" }},
          {{ label: "回撤", data: getCol("评分_回撤"), backgroundColor: "#f59e0b" }},
        ]
      }},
      options: {{
        responsive: true,
        scales: {{ y: {{ min: 0, max: 100, ticks: {{ stepSize: 20 }} }} }},
        plugins: {{
          tooltip: {{
            callbacks: {{
              label: (ctx) => `${{ctx.dataset.label}}: ${{fmtWithUnit("评分_总分(均分)", ctx.raw)}}`
            }}
          }}
        }},
        onClick: (evt, els) => {{
          if (!els || !els.length) return;
          const idx = els[0].index;
          selectRow(idx);
        }}
      }}
    }});

    const detailCtx = document.getElementById("detailChart");
    const DETAIL_KEYS = SCORE_COLS
      .filter(c => c.startsWith("评分_") && !["评分_总分(均分)","评分_Step2均分"].includes(c));
    const DETAIL_LABEL_MAP = {{
      "回撤": "回撤",
      "上市年限": "上市年限",
      "ROE5均值": "ROE5年均值",
      "ROE最近一期": "ROE最近一期",
      "毛利率": "毛利率",
      "资产负债率": "资产负债率",
      "经营现金流净额/净利润": "经营现金流/净利润",
      "现金流收益率": "现金流收益率",
      "5年经营现金流为正": "5年经营现金流为正",
      "留存vs市值(5年)": "留存vs市值(5年)",
      "PE近5年分位": "PE近5年分位",
      "股息率": "股息率",
      "5年平均分红率": "5年平均分红率"
    }};
    const DETAIL_LABELS = DETAIL_KEYS
      .map(k => k.replace(/^评分_/, ""))
      .map(k => DETAIL_LABEL_MAP[k] || k);
    const detailChart = new Chart(detailCtx, {{
      type: "bar",
      data: {{
        labels: DETAIL_LABELS,
        datasets: [{{ label: "得分", data: [] , backgroundColor: "#64748b"}}]
      }},
      options: {{
        responsive: true,
        scales: {{ y: {{ min: 0, max: 100, ticks: {{ stepSize: 20 }} }} }},
        plugins: {{
          tooltip: {{ callbacks: {{ label: (ctx) => `得分: ${{fmtWithUnit("评分_总分(均分)", ctx.raw)}}` }} }}
        }}
      }}
    }});

    function renderTable() {{
      const tbl = document.getElementById("tbl");
      const cols = METRIC_COLS;
      const thead = `<thead><tr>${{cols.map(c=>{{
        const u = UNIT_LABEL[c];
        const nm = String(c).replace(/_pct$/,"");
        return `<th>${{u ? (nm + "（" + u + "）") : nm}}</th>`;
      }}).join("")}}</tr></thead>`;
      const tbody = `<tbody>${{DATA.map((r,i)=>`<tr data-idx="${{i}}">${{cols.map(c=>`<td>${{fmtWithUnit(c, r[c])}}</td>`).join("")}}</tr>`).join("")}}</tbody>`;
      tbl.innerHTML = thead + tbody;
      tbl.querySelectorAll("tbody tr").forEach(tr => {{
        tr.style.cursor = "pointer";
        tr.addEventListener("click", () => selectRow(parseInt(tr.dataset.idx)));
      }});
    }}

    function clearLowBox() {{
      const box = document.getElementById("lowBox");
      while (box.firstChild) box.removeChild(box.firstChild);
    }}

    function lowScoreKeys(r) {{
      const keys = [];
      Object.keys(SCORE_META).forEach(k => {{
        const v = r[k];
        if (typeof v === "number" && Number.isFinite(v) && v < 80) keys.push(k);
      }});
      return keys;
    }}

    function nNum(v) {{
      return (typeof v === "number" && Number.isFinite(v)) ? v : null;
    }}

    function actualValueText(meta, r) {{
      if (meta.direction === "bool") {{
        const v = r[meta.metricCol];
        if (v === null || v === undefined || v === "") return "—";
        const truthy = (v === true || v === 1 || v === "True" || v === "true" || v === "是");
        return truthy ? "是" : "否";
      }}
      const v = nNum(r[meta.metricCol]);
      if (v === null) return "—";
      if (meta.unit === "%") return fmtNumber(v, 2) + "%";
      if (meta.unit === "倍") return fmtNumber(v, 3) + "倍";
      if (meta.unit === "年") return fmtNumber(v, 2) + "年";
      if (meta.unit === "元") return fmtYuanCN(v);
      return fmtNumber(v, 3) + (meta.unit || "");
    }}

    function buildRetainedExtra(r) {{
      const retained = nNum(r["五年累计留存_元"]);
      const anchor = nNum(r["五年期初总市值锚点_元"]);
      const delta = nNum(r["五年总市值增量_元"]);
      const now = nNum(r["总市值_快照"]);
      const lines = [];
      if (retained !== null) lines.push(`<div>近5年累计留存：${{fmtYuanCN(retained)}}</div>`);
      if (anchor !== null) lines.push(`<div>5年前市值锚点：${{fmtYuanCN(anchor)}}</div>`);
      if (now !== null) lines.push(`<div>当前总市值：${{fmtYuanCN(now)}}</div>`);
      if (delta !== null) lines.push(`<div>近5年市值增量：${{fmtYuanCN(delta)}}</div>`);
      let conclusion = "";
      if (delta !== null && delta <= 0) {{
        conclusion = "近5年市值增量≤0，创造比可能为负——通常对应估值压缩 / 行业景气切换：公司确实赚到并留存了利润，但市场给的“倍数”在下降。";
      }} else if (retained !== null && retained <= 0) {{
        conclusion = "累计留存非正（高分红或利润波动/亏损），此时创造比的参考意义有限。";
      }}
      return {{
        breakdown: lines.join(""),
        conclusion: conclusion
      }};
    }}

    function renderLowScoreCards(r) {{
      clearLowBox();
      const code = String(r["代码"]||"").padStart(6,"0");
      const td = TRENDS[code] || {{}};
      const lows = lowScoreKeys(r);
      const summary = document.getElementById("lowSummary");
      summary.textContent = lows.length ? `共 ${{lows.length}} 项 < 80` : "全部分项 ≥80";

      const box = document.getElementById("lowBox");
      if (!lows.length) {{
        const div = document.createElement("div");
        div.className = "muted";
        div.textContent = "该股票所有分项得分均 ≥80，无需展示解读。";
        box.appendChild(div);
        return;
      }}

      lows.forEach((k) => {{
        const meta = SCORE_META[k];
        if (!meta) return;
        const score = r[k];
        const card = document.createElement("div");
        card.className = "card";

        const title = document.createElement("div");
        title.innerHTML = `<span class="pill">${{meta.label}}</span> <span class="muted">得分 <b>${{fmtNumber(score,1)}}分</b>｜阈值 ${{meta.thresholdText}}</span>`;
        card.appendChild(title);

        const actual = document.createElement("div");
        actual.style.marginTop = "6px";
        actual.innerHTML = `实际值：<b>${{actualValueText(meta, r)}}</b>`;
        card.appendChild(actual);

        if (meta.description) {{
          const desc = document.createElement("div");
          desc.className = "muted";
          desc.style.marginTop = "4px";
          desc.textContent = meta.description;
          card.appendChild(desc);
        }}

        if (Array.isArray(meta.causes) && meta.causes.length) {{
          const causes = document.createElement("div");
          causes.style.marginTop = "6px";
          causes.innerHTML = `<b>可能原因</b><ul style="margin:4px 0 4px 18px; padding:0;">${{meta.causes.map(c => `<li>${{c}}</li>`).join("")}}</ul>`;
          card.appendChild(causes);
        }}

        if (Array.isArray(meta.suggestions) && meta.suggestions.length) {{
          const sugg = document.createElement("div");
          sugg.innerHTML = `<b>建议核查</b><ul style="margin:4px 0 4px 18px; padding:0;">${{meta.suggestions.map(c => `<li>${{c}}</li>`).join("")}}</ul>`;
          card.appendChild(sugg);
        }}

        if (meta.special === "retained") {{
          const extra = buildRetainedExtra(r);
          if (extra.breakdown) {{
            const bd = document.createElement("div");
            bd.className = "muted";
            bd.style.marginTop = "4px";
            bd.innerHTML = `<b>留存/市值拆解</b>${{extra.breakdown}}`;
            card.appendChild(bd);
          }}
          if (extra.conclusion) {{
            const cc = document.createElement("div");
            cc.style.marginTop = "4px";
            cc.innerHTML = `<b>结论</b>：${{extra.conclusion}}`;
            card.appendChild(cc);
          }}
        }}

        const one = td[k];
        if (one && Array.isArray(one.years) && one.years.length) {{
          const trendTitle = document.createElement("div");
          trendTitle.className = "muted";
          trendTitle.style.marginTop = "8px";
          trendTitle.textContent = `近5年趋势：${{one.label}}（${{one.unit||""}}）`;
          card.appendChild(trendTitle);

          const canvas = document.createElement("canvas");
          canvas.height = 140;
          card.appendChild(canvas);
          new Chart(canvas, {{
            type: "line",
            data: {{
              labels: one.years,
              datasets: [{{
                label: one.label,
                data: one.values,
                borderColor: "#2563eb",
                backgroundColor: "rgba(37,99,235,0.15)",
                tension: 0.25,
                fill: true,
                pointRadius: 3
              }}]
            }},
            options: {{
              responsive: true,
              plugins: {{
                legend: {{ display: false }},
                tooltip: {{
                  callbacks: {{
                    label: (ctx) => `${{one.label}}: ${{one.unit === "元" ? fmtYuanCN(ctx.raw) : (fmtNumber(ctx.raw, 3) + (one.unit||""))}}`
                  }}
                }}
              }},
              scales: {{
                x: {{ ticks: {{ maxRotation: 0 }} }},
                y: {{ beginAtZero: false }}
              }}
            }}
          }});
        }} else {{
          const noData = document.createElement("div");
          noData.className = "muted";
          noData.style.marginTop = "6px";
          noData.textContent = "（暂无可得的近5年公开序列，无法绘制趋势）";
          card.appendChild(noData);
        }}

        box.appendChild(card);
      }});
    }}

    function selectRow(idx) {{
      const r = DATA[idx];
      detailChart.data.datasets[0].data = DETAIL_KEYS.map(k => r[k] ?? null);
      detailChart.options.plugins.title = {{ display: true, text: `分项评分：${{r["代码"]||""}} ${{r["名称"]||""}}` }};
      detailChart.update();

      document.querySelectorAll("#tbl tbody tr").forEach(tr => {{
        tr.style.background = (parseInt(tr.dataset.idx)===idx) ? "#eef2ff" : "";
      }});

      renderLowScoreCards(r);
    }}

    renderTable();
    selectRow(0);
  </script>
</body>
</html>
"""


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把评分结果 xlsx 可视化为 HTML")
    p.add_argument("--input", default=None, help="输入 xlsx（默认自动选择最新的 single_stock_score_*.xlsx）")
    p.add_argument("--output", default="single_stock_viz.html", help="输出 HTML 文件名")
    p.add_argument("--no-trend", action="store_true", help="不拉取近5年趋势（更快生成）")
    p.add_argument("--no-history-percentile", action="store_true", help="不补算股息率/ROE/PE近5年历史分位（更快生成）")
    args = p.parse_args(argv)

    in_path = Path(args.input).resolve() if args.input else _pick_latest_xlsx()
    df = pd.read_excel(in_path, sheet_name="Score")
    if not args.no_history_percentile:
        df = _augment_history_percentiles(df)
    data = _to_records(df)
    eval_time = _eval_time_str()
    trends = {} if args.no_trend else _build_trend_payload(data)
    out_path = Path(args.output).resolve()
    chart_src = _deploy_chartjs(out_path)
    html_text = build_html(
        data,
        title=f"单只股票评分可视化：{in_path.name}",
        eval_time=eval_time,
        trends=trends,
        chart_js_src=chart_src,
    )
    out_path.write_text(html_text, encoding="utf-8")
    chart_path = out_path.parent / _CHART_BUNDLE_NAME
    print(f"已生成：{out_path}")
    print(f"图表库：{chart_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

