#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股交易日历（北京时间）与截面日解析。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Tuple
from zoneinfo import ZoneInfo

import akshare as ak

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")


def china_today() -> date:
    """当前北京时间日期。"""
    return datetime.now(CN_TZ).date()


@lru_cache(maxsize=1)
def all_trade_dates() -> frozenset[date]:
    """A 股交易日集合（新浪历，缓存进程内一次）。"""

    def _go():
        return ak.tool_trade_date_hist_sina()

    try:
        df = _go()
        return frozenset(df["trade_date"].tolist())
    except Exception as exc:
        logger.warning("新浪交易日历拉取失败：%s，回退沪深300指数日 K", exc)
        from strategies.hs300_multi_factor_akshare import get_trade_days_in_range

        start = china_today() - timedelta(days=400)
        end = china_today() + timedelta(days=30)
        return frozenset(get_trade_days_in_range(start, end))


def is_trading_day(d: date) -> bool:
    return d in all_trade_dates()


def latest_trade_date_on_or_before(d: date) -> date | None:
    cal = all_trade_dates()
    candidates = [x for x in cal if x <= d]
    return max(candidates) if candidates else None


def resolve_screen_date(
    requested: date | None = None,
    *,
    trading_day_only: bool = False,
) -> Tuple[date | None, str | None]:
    """
    解析选股截面日。

    - 未指定 ``requested``：取 **北京时间当天**；若当天为交易日则用它，否则：
      - ``trading_day_only=True`` → 返回 (None, 说明)，表示应跳过
      - 否则 → 回退为 <= 当天最近一个交易日
    - 指定 ``requested``：原样使用（不强制校验是否为交易日，便于补跑历史）
    """
    if requested is not None:
        return requested, None

    today = china_today()
    if is_trading_day(today):
        return today, None

    if trading_day_only:
        note = f"北京时间 {today} 非 A 股交易日，跳过本次选股/推送。"
        logger.info(note)
        return None, note

    last = latest_trade_date_on_or_before(today)
    if last is None:
        return None, "无法解析最近交易日。"
    note = f"北京时间 {today} 非交易日，截面日回退为最近交易日 {last}。"
    logger.info(note)
    return last, note
