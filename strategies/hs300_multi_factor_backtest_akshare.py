#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 多因子轮动 — AkShare 开源数据回测引擎。

无 jqdatasdk / 聚宽依赖；数据见 ``hs300_multi_factor_akshare.py``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List

import numpy as np
import pandas as pd

from strategies.hs300_multi_factor_akshare import HS300AkShareStore
from strategies.hs300_multi_factor_core import REBALANCE_EVERY_N, TOP_N, is_rebalance_day

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
    workers: int = 2
    limit: int | None = None  # 调试：仅预加载前 N 只成分股


@dataclass
class Portfolio:
    cash: float
    positions: Dict[str, float] = field(default_factory=dict)

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


def _rebalance_portfolio(portfolio: Portfolio, target: List[str], prices: Dict[str, float]) -> None:
    all_codes = set(portfolio.positions) | set(target)
    px_map = {c: prices.get(c) for c in all_codes}

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
        portfolio.cash += proceeds - _commission(proceeds, is_sell=True)
        portfolio.positions.pop(code, None)

    if not target:
        return

    total = portfolio.total_value({c: px_map.get(c, 0.0) or 0.0 for c in target})
    per_value = total / len(target)

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
            portfolio.cash += trade_amount - _commission(trade_amount, is_sell=True)
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
    store = HS300AkShareStore(config.start_date, config.end_date)
    store.bootstrap(limit=config.limit)
    logger.info("预加载成分股 K 线…")
    store.preload_klines(workers=config.workers)

    trade_days = store.trade_days
    portfolio = Portfolio(cash=config.initial_cash)
    holdings: List[str] = []
    equity_rows: list[dict] = []
    rebalance_log: list[dict] = []

    rebalance_total = sum(1 for i in range(len(trade_days)) if is_rebalance_day(i, config.rebalance_every_n))
    rebalance_done = 0

    for i, d in enumerate(trade_days):
        if is_rebalance_day(i, config.rebalance_every_n):
            rebalance_done += 1
            logger.info("调仓 %d/%d：%s", rebalance_done, rebalance_total, d)
            target = store.select_targets(d, config.top_n)
            prices = store.close_prices(list(set(holdings) | set(target)), d)
            _rebalance_portfolio(portfolio, target, prices)
            holdings = target
            rebalance_log.append({"date": d, "targets": list(target)})

        prices = store.close_prices(list(portfolio.positions.keys()), d)
        tv = portfolio.total_value(prices)
        bench_px = float(store.bench_close.get(d, np.nan))
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
        "data_source": "AkShare",
    }

    return {
        "equity_curve": equity_df,
        "metrics": metrics,
        "rebalance_log": rebalance_log,
        "store": store,
    }


__all__ = ["BacktestConfig", "run_backtest"]
