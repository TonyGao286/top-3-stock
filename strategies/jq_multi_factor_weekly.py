#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股策略 — 量价(70%) + 盈利(30%)，周频等权调仓。

================================================================================
运行环境 A：聚宽 JoinQuant 云端策略（https://www.joinquant.com）
  → 整文件粘贴到聚宽策略编辑器回测/模拟盘。

运行环境 B：本地 Python + AkShare（推荐，无 JQData 日额度限制）
  → python strategies/run_local_akshare.py
  → 实现见 strategies/multi_factor_akshare.py

运行环境 C：本地 Python + JQData SDK（试用额度少，仅适合轻量验证）
  → python strategies/run_local_jqdata.py
  → 实现见 strategies/multi_factor_local.py
================================================================================

其他平台迁移提示（仅注释说明，本文件未直接调用其 SDK）：
  - BigQuant：将 jqdata 函数映射为 D.history / D.instruments；调仓钩子改为 handle_data。
  - QMT（xtquant）：用 xtdata.get_index_weight 取成分股，xtdata.get_market_data 取行情，
    定时任务在 on_bar / 自定义 scheduler 中调用 rebalance()。

因子约束（策略设计红线）：
  ✓ 仅使用「量价因子」与「盈利因子」
  ✗ 禁止引入任何成长类因子（净利润/营收同比增速等）
================================================================================
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd

# =============================================================================
# 全局超参数（部署到云服务器后可在此集中调参）
# =============================================================================

# --- 股票池 ---
INDEX_CODES: Tuple[str, ...] = (
    "000016.XSHG",  # 上证50
    "000905.XSHG",  # 中证500
    "000852.XSHG",  # 中证1000
)
MIN_LISTING_TRADE_DAYS: int = 252  # 剔除上市不足 N 个交易日的次新股

# --- 因子与权重 ---
PV_WEIGHT: float = 0.70  # 量价因子组权重
PROFIT_WEIGHT: float = 0.30  # 盈利因子组权重

# 量价组内各因子等权（合计 PV_WEIGHT）
PV_FACTOR_NAMES: Tuple[str, ...] = (
    "rev_mom_20d",       # 20日动量反转（-20日收益率，A股短期反转）
    "turnover_20d",      # 20日日均换手率（流动性/资金博弈）
    "idio_vol_20d",      # 20日特异性波动率（取负向：低波动得分更高）
)
# 盈利组内各因子等权（合计 PROFIT_WEIGHT）
PROFIT_FACTOR_NAMES: Tuple[str, ...] = (
    "roe",               # ROE 净资产收益率
    "ocf_to_liability",  # 经营活动现金流净额 / 负债合计
)

# 因子方向：+1 表示原始值越大越好，-1 表示原始值越小越好（会在标准化前取反）
FACTOR_DIRECTION: Dict[str, int] = {
    "rev_mom_20d": +1,       # 已构造为 -return，越大越好
    "turnover_20d": +1,
    "idio_vol_20d": +1,      # 已构造为 -idio_vol，越大越好
    "roe": +1,
    "ocf_to_liability": +1,
}

# --- 预处理 ---
MAD_N: float = 3.0  # MAD 去极值倍数

# --- 组合 ---
TOP_N: int = 30  # 目标持仓数量
REBALANCE_WEEKDAY: int = 5  # 聚宽 run_weekly 的 weekday（5=周五，近似「周末调仓」）
REBALANCE_TIME: str = "14:50"  # 尾盘调仓，降低盘中冲击

# --- 量价因子计算窗口 ---
MOM_WINDOW: int = 20
TURNOVER_WINDOW: int = 20
IDIO_VOL_WINDOW: int = 20
MARKET_INDEX: str = "000905.XSHG"  # 特异性波动率回归用市场代理（中证500）

# --- 交易 ---
MAX_POSITION_PCT: float = 0.05  # 单票上限（可选风控，30 等权约 3.3%）
ENABLE_ORDER_COST: bool = True


# =============================================================================
# 聚宽策略入口
# =============================================================================


def initialize(context) -> None:
    """聚宽初始化：设置基准、定时任务、全局变量容器。"""
    set_benchmark("000905.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    if ENABLE_ORDER_COST:
        set_order_cost(
            OrderCost(
                open_tax=0,
                close_tax=0.001,
                open_commission=0.0003,
                close_commission=0.0003,
                min_commission=5,
            ),
            type="stock",
        )

    # 缓存上一期目标名单，便于日志对比
    context.target_list: List[str] = []
    context.last_scores: pd.Series = pd.Series(dtype=float)

    # 方式一：每周五定时（节假日当周会自动跳过，聚宽会顺延到下一触发日）
    run_weekly(rebalance, weekday=REBALANCE_WEEKDAY, time=REBALANCE_TIME)

    # 方式二（可选）：若严格要求「本周最后一个交易日」，可注释上一行，改用：
    # run_daily(check_week_end_and_rebalance, time=REBALANCE_TIME)


def check_week_end_and_rebalance(context) -> None:
    """
    判断「今日是否为本周最后交易日」，若是则调仓。
    比固定周五更贴近需求，但多一次日频判断。
    """
    today = context.current_dt.date()
    trade_days = get_trade_days(start_date=today, count=2)
    if len(trade_days) < 2:
        return
    # 若下一交易日已跨入下一自然周，则 today 为本周最后交易日
    next_day = trade_days[1]
    if next_day.isocalendar()[1] != today.isocalendar()[1]:
        rebalance(context)


def rebalance(context) -> None:
    """调仓主流程：选股 → 打分 → 生成目标持仓 → 下单。"""
    as_of = context.current_dt.date()

    universe = get_base_universe(as_of)
    if not universe:
        log.warn("基础股票池为空，跳过调仓")
        return

    tradable = filter_tradable_stocks(universe, as_of)
    if len(tradable) < TOP_N:
        log.warn(f"可交易股票仅 {len(tradable)} 只，少于目标 {TOP_N}")

    raw_factors = compute_raw_factors(tradable, as_of)
    if raw_factors.empty:
        log.warn("因子矩阵为空，跳过调仓")
        return

    scores = score_and_rank(raw_factors)
    target = scores.head(TOP_N).index.tolist()

    context.target_list = target
    context.last_scores = scores

    execute_equal_weight(context, target)
    log.info(f"[{as_of}] 调仓完成，目标 {len(target)} 只：{target[:5]}...")


# =============================================================================
# 模块 1：股票池与基础过滤
# =============================================================================


def get_base_universe(as_of) -> List[str]:
    """
    合并上证50、中证500、中证1000 最新成分股（去重）。
    聚宽 API：get_index_stocks(index, date)
    """
    codes: set[str] = set()
    for idx in INDEX_CODES:
        try:
            members = get_index_stocks(idx, date=as_of)
            codes.update(members)
        except Exception as exc:  # noqa: BLE001 — 聚宽环境异常类型不固定
            log.warn(f"获取指数成分失败 {idx}: {exc}")
    return sorted(codes)


def filter_tradable_stocks(stocks: Sequence[str], as_of) -> List[str]:
    """
    剔除 ST/*ST、停牌、上市不足 MIN_LISTING_TRADE_DAYS 的次新股。
    """
    if not stocks:
        return []

    current = get_current_data()
    kept: List[str] = []

    # 批量取 ST 标记（比逐只查 current 更稳）
    st_flags = get_extras("is_st", stocks, start_date=as_of, end_date=as_of, df=True)
    st_today = set()
    if st_flags is not None and not st_flags.empty:
        row = st_flags.iloc[0]
        st_today = {s for s in stocks if bool(row.get(s, False))}

    for code in stocks:
        info = get_security_info(code)
        if info is None:
            continue

        # 上市天数：按交易日计数
        listed_days = get_trade_days(start_date=info.start_date, end_date=as_of)
        if len(listed_days) < MIN_LISTING_TRADE_DAYS:
            continue

        if code in st_today:
            continue

        cd = current[code]
        if cd.paused:
            continue
        if cd.is_st or ("ST" in (cd.name or "")):
            continue
        if cd.last_price is None or cd.last_price <= 0:
            continue

        kept.append(code)

    return kept


# =============================================================================
# 模块 2：因子计算（仅量价 + 盈利，无成长因子）
# =============================================================================


def compute_raw_factors(stocks: Sequence[str], as_of) -> pd.DataFrame:
    """
    计算全部原始因子，返回 index=股票代码、columns=因子名的 DataFrame。
    缺失值保留 NaN，后续截面打分时会剔除无效样本。
    """
    if not stocks:
        return pd.DataFrame()

    pv = _calc_price_volume_factors(stocks, as_of)
    prof = _calc_profitability_factors(stocks, as_of)

    df = pv.join(prof, how="outer")
    # 统一因子方向（低越好类因子已在构造时取反，此处按 FACTOR_DIRECTION 再保险）
    for col, direction in FACTOR_DIRECTION.items():
        if col in df.columns and direction < 0:
            df[col] = -df[col]
    return df


def _calc_price_volume_factors(stocks: Sequence[str], as_of) -> pd.DataFrame:
    """量价因子组：20日反转、20日换手、20日特异性波动率（取负）。"""
    lookback = max(MOM_WINDOW, TURNOVER_WINDOW, IDIO_VOL_WINDOW) + 5
    start = get_trade_days(end_date=as_of, count=lookback)[0]

    price_panel = get_price(
        list(stocks),
        start_date=start,
        end_date=as_of,
        frequency="daily",
        fields=["close"],
        panel=False,
        skip_paused=True,
        fq="pre",
    )

    # 20 日日均换手率（valuation.turnover_ratio 为小数，如 0.03 = 3%）
    turnover_panel = get_valuation(
        list(stocks),
        end_date=as_of,
        fields="turnover_ratio",
        count=TURNOVER_WINDOW,
    )

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

    rev_mom: Dict[str, float] = {}
    turnover_20d: Dict[str, float] = {}
    idio_vol: Dict[str, float] = {}

    if price_panel is None or price_panel.empty:
        return pd.DataFrame()

    for code in stocks:
        sub = price_panel[price_panel["code"] == code].sort_values("time")
        if len(sub) < MOM_WINDOW + 1:
            continue
        closes = sub["close"].astype(float)
        ret_20 = closes.iloc[-1] / closes.iloc[-MOM_WINDOW - 1] - 1.0
        rev_mom[code] = -float(ret_20)

        if turnover_panel is not None and not turnover_panel.empty:
            tsub = turnover_panel[turnover_panel["code"] == code]["turnover_ratio"]
            if len(tsub) > 0:
                turnover_20d[code] = float(pd.to_numeric(tsub, errors="coerce").mean())
            else:
                turnover_20d[code] = np.nan
        else:
            turnover_20d[code] = np.nan

        daily_ret = closes.pct_change().dropna()
        aligned = pd.concat([daily_ret, mkt_ret], axis=1, join="inner").dropna()
        if len(aligned) >= IDIO_VOL_WINDOW:
            aligned.columns = ["stock", "mkt"]
            window = aligned.tail(IDIO_VOL_WINDOW)
            x = window["mkt"].values
            y = window["stock"].values
            if np.std(x) > 1e-12:
                beta = np.cov(y, x)[0, 1] / np.var(x)
                resid = y - beta * x
                idio_vol[code] = -float(np.std(resid))
            else:
                idio_vol[code] = -float(np.std(y))
        else:
            idio_vol[code] = np.nan

    return pd.DataFrame(
        {
            "rev_mom_20d": pd.Series(rev_mom),
            "turnover_20d": pd.Series(turnover_20d),
            "idio_vol_20d": pd.Series(idio_vol),
        }
    )


def _calc_profitability_factors(stocks: Sequence[str], as_of) -> pd.DataFrame:
    """
    盈利因子组：ROE、经营现金流/负债合计。
    使用聚宽财务表 query + get_fundamentals（时点 as_of，避免未来函数）。
    """
    if not stocks:
        return pd.DataFrame()

    q = query(
        valuation.code,
        indicator.roe,
        balance.total_liability,
        cash_flow.net_operate_cash_flow,
    ).filter(valuation.code.in_(list(stocks)))

    fund = get_fundamentals(q, date=as_of)
    if fund is None or fund.empty:
        return pd.DataFrame(index=list(stocks), columns=list(PROFIT_FACTOR_NAMES))

    fund = fund.set_index("code")
    roe = pd.to_numeric(fund.get("roe"), errors="coerce")
    liability = pd.to_numeric(fund.get("total_liability"), errors="coerce")
    ocf = pd.to_numeric(fund.get("net_operate_cash_flow"), errors="coerce")

    # 负债为 0 或缺失时比值无效
    ocf_ratio = ocf / liability.replace(0, np.nan)

    out = pd.DataFrame(
        {
            "roe": roe,
            "ocf_to_liability": ocf_ratio,
        }
    )
    return out.reindex(stocks)


# =============================================================================
# 模块 3：因子预处理（MAD 去极值 + Z-Score）与综合打分
# =============================================================================


def mad_winsorize(series: pd.Series, n: float = MAD_N) -> pd.Series:
    """
    MAD 法去极值（Median Absolute Deviation）。
    边界：median ± n * 1.4826 * MAD
    """
    s = series.astype(float).copy()
    valid = s.dropna()
    if valid.empty:
        return s

    med = valid.median()
    mad = (valid - med).abs().median()
    if mad <= 0 or not math.isfinite(mad):
        return s

    upper = med + n * 1.4826 * mad
    lower = med - n * 1.4826 * mad
    return s.clip(lower=lower, upper=upper)


def zscore_standardize(series: pd.Series) -> pd.Series:
    """截面 Z-Score 标准化。"""
    s = series.astype(float)
    mu = s.mean(skipna=True)
    sigma = s.std(skipna=True, ddof=0)
    if sigma is None or sigma <= 1e-12 or not math.isfinite(sigma):
        return s * 0.0
    return (s - mu) / sigma


def preprocess_factor_cross_section(raw: pd.Series) -> pd.Series:
    """单因子截面预处理流水线：MAD → Z-Score。"""
    return zscore_standardize(mad_winsorize(raw))


def score_and_rank(raw_factors: pd.DataFrame) -> pd.Series:
    """
    对截面因子做预处理，按组加权合成综合得分并降序排名。

    综合得分 = PV_WEIGHT * mean(量价因子 Z 分) + PROFIT_WEIGHT * mean(盈利因子 Z 分)
    组内因子等权。
    """
    df = raw_factors.copy()

    # 要求至少有一类因子非空
    all_factor_cols = list(PV_FACTOR_NAMES) + list(PROFIT_FACTOR_NAMES)
    for col in all_factor_cols:
        if col not in df.columns:
            df[col] = np.nan

    z_df = pd.DataFrame(index=df.index)
    for col in all_factor_cols:
        z_df[col] = preprocess_factor_cross_section(df[col])

    # 组内等权：对缺失因子做 mean(skipna)，要求每组至少 1 个有效因子
    pv_z = z_df[list(PV_FACTOR_NAMES)].mean(axis=1, skipna=True)
    prof_z = z_df[list(PROFIT_FACTOR_NAMES)].mean(axis=1, skipna=True)

    # 盈利因子缺失的股票仍可能参与（仅量价），但盈利全缺则降权处理
    composite = PV_WEIGHT * pv_z + PROFIT_WEIGHT * prof_z.fillna(0.0)

    # 剔除关键因子大面积缺失的标的（量价组至少 2/3 有效）
    pv_valid_cnt = z_df[list(PV_FACTOR_NAMES)].notna().sum(axis=1)
    composite = composite[pv_valid_cnt >= 2]

    return composite.sort_values(ascending=False)


# =============================================================================
# 模块 4：交易信号与等权调仓
# =============================================================================


def generate_target_portfolio(scores: pd.Series, universe: Iterable[str], top_n: int = TOP_N) -> List[str]:
    """
    根据综合得分选取前 top_n，且必须在 universe 内。
    """
    universe_set = set(universe)
    ranked = scores[scores.index.isin(universe_set)]
    return ranked.head(top_n).index.tolist()


def execute_equal_weight(context, target: List[str]) -> None:
    """
    等权重调仓：
      - 不在目标名单 / 不在三大指数成分内的持仓 → 清仓
      - 目标名单内 → 等权买入
    """
    as_of = context.current_dt.date()
    index_pool = set(get_base_universe(as_of))
    current_holdings = list(context.portfolio.positions.keys())

    # 先卖：不在目标池，或已不在指数成分内
    for code in current_holdings:
        if code not in target or code not in index_pool:
            order_target_value(code, 0)
            log.info(f"卖出 {code}（不在目标池或已非指数成分）")

    if not target:
        return

    total_value = context.portfolio.total_value
    weight = 1.0 / len(target)
    target_value = total_value * weight

    for code in target:
        if code not in index_pool:
            continue
        # 可选单票上限
        cap_value = total_value * MAX_POSITION_PCT
        order_target_value(code, min(target_value, cap_value))


# =============================================================================
# 本地调试辅助（聚宽环境外不执行；便于将来抽离为纯函数单测）
# =============================================================================

if __name__ == "__main__":
    print(
        "本策略需在聚宽 JoinQuant 环境中运行。\n"
        f"超参数摘要：TOP_N={TOP_N}, PV_WEIGHT={PV_WEIGHT}, PROFIT_WEIGHT={PROFIT_WEIGHT}, "
        f"调仓=weekly weekday={REBALANCE_WEEKDAY} {REBALANCE_TIME}"
    )
