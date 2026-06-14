#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 多因子轮动策略 — 本地事件驱动回测引擎（JQData 行情 + 财务数据）。

回测假设：
  - 调仓日收盘价成交（与聚宽 14:50 调仓近似）
  - 等权持有 TOP_N 只
  - 印花税 0.1%（卖），佣金万 3（买卖）
  - 最小佣金 5 元
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from jqdatasdk import get_price, get_trade_days

from strategies.hs300_multi_factor_core import (
    BENCHMARK,
    INDEX_CODE,
    REBALANCE_EVERY_N,
    TOP_N,
    is_rebalance_day,
    select_top_stocks,
    score_factors,
)
from strategies.hs300_multi_factor_local import fetch_factors, filter_stock_pool, get_hs300_universe

logger = logging.getLogger(__name__)

OPEN_COMMISSION = 0.0003
CLOSE_COMMISSION = 0.0003
CLOSE_TAX = 0.001
MIN_COMMISSION = 5.0


@dataclass
class BacktestConfig:
    start_date: date
    end_date: date
    initial_cash: float = 1_000_000.0
    top_n: int = TOP_N
    rebalance_every_n: int = REBALANCE_EVERY_N
    benchmark: str = BENCHMARK


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, float] = field(default_factory=dict)  # code -> shares

    def market_value(self, prices: Dict[str, float]) -> float:
        mv = 0.0
        for code, shares in self.positions.items():
            px = prices.get(code)
            if px is not None and np.isfinite(px) and px > 0:
                mv += shares * px
        return mv

    def total_value(self, prices: Dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


def _commission(amount: float, *, is_sell: bool) -> float:
    rate = CLOSE_COMMISSION + (CLOSE_TAX if is_sell else 0.0)
    fee = abs(amount) * rate
    return max(fee, MIN_COMMISSION) if amount > 0 else 0.0


def _fetch_close_prices(codes: List[str], as_of: date) -> Dict[str, float]:
    if not codes:
        return {}
    parts: list[pd.DataFrame] = []
    batch = 400
    for i in range(0, len(codes), batch):
        chunk = codes[i : i + batch]
        df = get_price(
            chunk,
            end_date=as_of,
            count=1,
            frequency="daily",
            fields=["close"],
            panel=False,
            skip_paused=False,
            fq="pre",
        )
        if df is not None and not df.empty:
            parts.append(df)
    if not parts:
        return {}
    snap = pd.concat(parts, ignore_index=True).sort_values("time").groupby("code", as_index=False).tail(1)
    return {row["code"]: float(row["close"]) for _, row in snap.iterrows() if pd.notna(row["close"])}


def _select_targets(as_of: date, top_n: int) -> List[str]:
    universe = get_hs300_universe(as_of)
    pool = filter_stock_pool(universe, as_of)
    if len(pool) < top_n:
        logger.warning("%s 可交易股票 %d 只，少于目标 %d", as_of, len(pool), top_n)
    factors = fetch_factors(pool, as_of)
    if factors.empty:
        return []
    scored = score_factors(factors)
    return select_top_stocks(scored, top_n)


def _rebalance_portfolio(portfolio: Portfolio, target: List[str], prices: Dict[str, float]) -> None:
    """先卖后买，等权目标市值。"""
    all_codes = set(portfolio.positions) | set(target)
    px_map = {c: prices.get(c) for c in all_codes}

    # 先卖
    for code in list(portfolio.positions.keys()):
        if code in target:
            continue
        shares = portfolio.positions.get(code, 0.0)
        if shares <= 0:
            portfolio.positions.pop(code, None)
            continue
        px = px_map.get(code)
        if px is None or px <= 0:
            continue
        proceeds = shares * px
        fee = _commission(proceeds, is_sell=True)
        portfolio.cash += proceeds - fee
        portfolio.positions.pop(code, None)

    if not target:
        return

    total = portfolio.total_value({c: px_map.get(c, 0.0) or 0.0 for c in target})
    per_value = total / len(target)

    # 再买 / 调仓
    for code in target:
        px = px_map.get(code)
        if px is None or px <= 0:
            continue
        target_shares = per_value / px
        current = portfolio.positions.get(code, 0.0)
        delta = target_shares - current
        if abs(delta) * px < 1e-6:
            continue
        trade_amount = abs(delta) * px
        fee = _commission(trade_amount, is_sell=delta < 0)
        if delta > 0:
            cost = trade_amount + _commission(trade_amount, is_sell=False)
            if cost > portfolio.cash:
                affordable = max(0.0, portfolio.cash - MIN_COMMISSION)
                if affordable <= 0:
                    continue
                buy_shares = affordable / (px * (1 + OPEN_COMMISSION))
                portfolio.positions[code] = current + buy_shares
                portfolio.cash -= buy_shares * px + _commission(buy_shares * px, is_sell=False)
            else:
                portfolio.positions[code] = target_shares
                portfolio.cash -= cost
        else:
            portfolio.cash += trade_amount - fee
            portfolio.positions[code] = target_shares


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else 0.0


def _annualized_return(equity: pd.Series, trading_days: int) -> float:
    if len(equity) < 2 or trading_days <= 0:
        return 0.0
    total = float(equity.iloc[-1] / equity.iloc[0])
    years = trading_days / 252.0
    if years <= 0 or total <= 0:
        return 0.0
    return total ** (1.0 / years) - 1.0


def _sharpe(daily_ret: pd.Series, rf: float = 0.0) -> float:
    excess = daily_ret - rf / 252.0
    std = excess.std(ddof=0)
    if std is None or std <= 1e-12:
        return 0.0
    return float(excess.mean() / std * np.sqrt(252))


def run_backtest(config: BacktestConfig) -> dict:
    """
    运行回测，返回：
      - equity_curve: 策略净值 DataFrame
      - benchmark_curve: 基准净值 DataFrame
      - metrics: 指标 dict
      - rebalance_log: 调仓记录 list
    """
    trade_days = get_trade_days(start_date=config.start_date, end_date=config.end_date)
    if len(trade_days) == 0:
        raise RuntimeError(f"区间 {config.start_date} ~ {config.end_date} 无交易日")

    trade_days = [pd.Timestamp(d).date() for d in trade_days]
    logger.info("回测区间：%s ~ %s，共 %d 个交易日", trade_days[0], trade_days[-1], len(trade_days))

    bench = get_price(
        config.benchmark,
        start_date=trade_days[0],
        end_date=trade_days[-1],
        frequency="daily",
        fields=["close"],
        skip_paused=True,
        fq="pre",
    )
    if bench is None or bench.empty:
        raise RuntimeError(f"无法获取基准 {config.benchmark} 行情")
    bench_close = bench["close"].astype(float)
    bench_close.index = pd.to_datetime(bench_close.index).date

    portfolio = Portfolio(cash=config.initial_cash)
    holdings: List[str] = []
    equity_rows: list[dict] = []
    rebalance_log: list[dict] = []

    for i, d in enumerate(trade_days):
        if is_rebalance_day(i, config.rebalance_every_n):
            target = _select_targets(d, config.top_n)
            prices = _fetch_close_prices(list(set(holdings) | set(target)), d)
            _rebalance_portfolio(portfolio, target, prices)
            holdings = target
            rebalance_log.append({"date": d, "targets": list(target)})

        mark_codes = list(portfolio.positions.keys())
        prices = _fetch_close_prices(mark_codes, d) if mark_codes else {}
        tv = portfolio.total_value(prices)
        bench_px = float(bench_close.get(d, np.nan))
        equity_rows.append(
            {
                "date": d,
                "total_value": tv,
                "cash": portfolio.cash,
                "holdings": len(portfolio.positions),
                "benchmark_close": bench_px,
            }
        )

    equity_df = pd.DataFrame(equity_rows).set_index("date")
    equity_df["strategy_nav"] = equity_df["total_value"] / config.initial_cash
    bench_start = equity_df["benchmark_close"].dropna().iloc[0]
    equity_df["benchmark_nav"] = equity_df["benchmark_close"] / bench_start

    strat_ret = equity_df["strategy_nav"].pct_change().dropna()
    bench_ret = equity_df["benchmark_nav"].pct_change().dropna()
    excess = strat_ret - bench_ret.reindex(strat_ret.index).fillna(0.0)

    metrics = {
        "start_date": str(trade_days[0]),
        "end_date": str(trade_days[-1]),
        "trading_days": len(trade_days),
        "initial_cash": config.initial_cash,
        "final_value": float(equity_df["total_value"].iloc[-1]),
        "total_return": float(equity_df["strategy_nav"].iloc[-1] - 1.0),
        "benchmark_total_return": float(equity_df["benchmark_nav"].iloc[-1] - 1.0),
        "annualized_return": _annualized_return(equity_df["strategy_nav"], len(trade_days)),
        "benchmark_annualized_return": _annualized_return(equity_df["benchmark_nav"], len(trade_days)),
        "max_drawdown": _max_drawdown(equity_df["strategy_nav"]),
        "benchmark_max_drawdown": _max_drawdown(equity_df["benchmark_nav"]),
        "sharpe": _sharpe(strat_ret),
        "excess_total_return": float(equity_df["strategy_nav"].iloc[-1] - equity_df["benchmark_nav"].iloc[-1]),
        "rebalance_count": len(rebalance_log),
    }

    return {
        "equity_curve": equity_df,
        "metrics": metrics,
        "rebalance_log": rebalance_log,
    }


__all__ = ["BacktestConfig", "run_backtest"]
