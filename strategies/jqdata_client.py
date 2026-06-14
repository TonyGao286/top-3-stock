#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JQData 本地 SDK 登录封装（jqdatasdk）。

运行环境：本地 Python 3 + pip install jqdatasdk
文档：https://www.joinquant.com/help/api/doc?name=JQDatadoc

凭据优先级：
  1. 函数参数 phone / password
  2. 环境变量 JQDATA_PHONE / JQDATA_PASSWORD
  3. 项目根目录 .env 文件（勿提交 Git）
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class JQDataDateRange:
    """JQData 账号可查询数据的日期闭区间 [start, end]。"""

    start: date
    end: date

    def __str__(self) -> str:
        return f"{self.start} ~ {self.end}"


def _to_date_safe(v: Any) -> date | None:
    try:
        from pandas import Timestamp

        return Timestamp(v).date()
    except Exception:
        if isinstance(v, str):
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", v.strip())
            if m:
                y, mo, d = map(int, m.group(1).split("-"))
                return date(y, mo, d)
        return None


def parse_permission_range_from_error(msg: str) -> JQDataDateRange | None:
    """从 JQData 异常文案解析权限区间，如「2025-02-19至2026-02-26」。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*至\s*(\d{4}-\d{2}-\d{2})", str(msg))
    if not m:
        return None
    start = _to_date_safe(m.group(1))
    end = _to_date_safe(m.group(2))
    if start and end:
        return JQDataDateRange(start=start, end=end)
    return None


def _parse_range_from_obj(obj: Any, depth: int = 0) -> JQDataDateRange | None:
    """递归从 get_privilege / get_account_info 返回结构中找 start/end。"""
    if depth > 12 or obj is None:
        return None

    if isinstance(obj, JQDataDateRange):
        return obj

    if isinstance(obj, dict):
        lower = {str(k).lower(): v for k, v in obj.items()}
        start_keys = ("start", "start_date", "date_start", "begin", "begin_date", "from")
        end_keys = ("end", "end_date", "date_end", "finish", "finish_date", "to")
        start = end = None
        for k in start_keys:
            if k in lower:
                start = _to_date_safe(lower[k])
                break
        for k in end_keys:
            if k in lower:
                end = _to_date_safe(lower[k])
                break
        if start and end:
            return JQDataDateRange(start=start, end=end)
        for v in obj.values():
            found = _parse_range_from_obj(v, depth + 1)
            if found:
                return found

    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _parse_range_from_obj(item, depth + 1)
            if found:
                return found

    return None


def get_jqdata_permission_range(*, probe_index: str = "000016.XSHG") -> JQDataDateRange | None:
    """
    获取当前账号的数据权限日期范围。
    依次尝试 get_privilege → get_account_info → 探测 get_index_stocks 报错信息。
    """
    from jqdatasdk import get_account_info, get_index_stocks, get_privilege

    for fn in (get_privilege, get_account_info):
        try:
            info = fn()
            parsed = _parse_range_from_obj(info)
            if parsed:
                return parsed
        except Exception:
            continue

    # 用「远超当前」的日期探测，从报错中解析权限上限
    try:
        get_index_stocks(probe_index, date="2099-01-01")
    except Exception as exc:
        parsed = parse_permission_range_from_error(str(exc))
        if parsed:
            return parsed

    return None


def resolve_screen_date(
    requested: date | datetime | str | None = None,
    *,
    auto_clamp: bool = True,
) -> tuple[date, JQDataDateRange | None, str | None]:
    """
    解析选股截面日，并在试用账号权限不足时自动落到权限内最近交易日。

    :return: (as_of, permission_range, adjustment_note)
    """
    from jqdatasdk import get_trade_days

    if isinstance(requested, datetime):
        req = requested.date()
    elif isinstance(requested, date):
        req = requested
    elif requested is None:
        req = None
    else:
        req = _to_date_safe(str(requested))

    perm = get_jqdata_permission_range()
    note: str | None = None

    def _latest_trade_on_or_before(d: date) -> date:
        days = get_trade_days(end_date=d, count=1)
        if len(days) == 0:
            raise RuntimeError(f"无法获取 ≤{d} 的交易日")
        return _to_date_safe(days[-1]) or d

    if req is None:
        cap = perm.end if perm else date.today()
        as_of = _latest_trade_on_or_before(cap)
        if perm and as_of > perm.end:
            as_of = _latest_trade_on_or_before(perm.end)
        if perm and as_of < perm.start:
            days = get_trade_days(start_date=perm.start, end_date=perm.end, count=1)
            if not len(days):
                raise RuntimeError(f"权限区间 {perm} 内无交易日")
            as_of = _to_date_safe(days[0]) or perm.start
        return as_of, perm, note

    as_of = req
    if perm is None:
        return as_of, None, note

    if as_of < perm.start:
        if not auto_clamp:
            raise RuntimeError(
                f"截面日 {as_of} 早于 JQData 权限起点 {perm.start}。"
                f"请使用 --date {perm.start} 或省略 --date 自动选取。"
            )
        as_of = _latest_trade_on_or_before(max(perm.start, as_of))
        if as_of < perm.start:
            days = get_trade_days(start_date=perm.start, end_date=perm.end, count=1)
            as_of = _to_date_safe(days[0]) if days else perm.start
        note = f"截面日 {req} 早于权限起点，已调整为 {as_of}（权限：{perm}）"

    elif as_of > perm.end:
        if not auto_clamp:
            raise RuntimeError(
                f"截面日 {as_of} 晚于 JQData 权限终点 {perm.end}。"
                f"试用账号请使用：python strategies/run_local_jqdata.py --date {perm.end}"
            )
        as_of = _latest_trade_on_or_before(perm.end)
        note = f"截面日 {req} 晚于权限终点，已调整为 {as_of}（权限：{perm}）"

    return as_of, perm, note


def load_dotenv(path: Path | None = None) -> None:
    """简易 .env 加载（不额外依赖 python-dotenv）。"""
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def get_jqdata_credentials(
    phone: Optional[str] = None,
    password: Optional[str] = None,
) -> Tuple[str, str]:
    load_dotenv()
    # 命令行显式传入时覆盖 .env；勿用占位符 138xxxx 覆盖已配置的真实账号
    p = (phone if phone is not None else os.environ.get("JQDATA_PHONE") or "").strip()
    pw = (password if password is not None else os.environ.get("JQDATA_PASSWORD") or "").strip()
    if not p or not pw:
        raise RuntimeError(
            "未配置 JQData 账号。\n"
            "请在 .env 中设置 JQDATA_PHONE（手机号）与 JQDATA_PASSWORD（聚宽登录密码），\n"
            "或执行：python strategies/run_local_jqdata.py --phone 手机号 --password 密码"
        )
    return p, pw


def ensure_jqdata_auth(
    phone: Optional[str] = None,
    password: Optional[str] = None,
    *,
    quiet: bool = False,
) -> bool:
    """
    登录 JQData；已登录则跳过。
    成功返回 True，失败抛异常。
    """
    from jqdatasdk import auth, is_auth

    if is_auth():
        if not quiet:
            print("JQData 已登录（跳过 auth）")
        return True

    p, pw = get_jqdata_credentials(phone, password)
    auth(p, pw)

    if not is_auth():
        raise RuntimeError("JQData 登录失败，请检查手机号与聚宽官网密码。")

    if not quiet:
        print("auth success（本地确认）")
    return True


def print_jqdata_account_summary() -> None:
    """打印账号权限区间与当日剩余调用量（登录后调用）。"""
    from jqdatasdk import get_query_count

    perm = get_jqdata_permission_range()
    if perm:
        print(f"JQData 数据权限区间：{perm}")
    else:
        print("JQData 数据权限区间：未能自动解析（选股时会尝试自动校正日期）")

    try:
        qc = get_query_count()
        if isinstance(qc, dict):
            print(f"今日调用额度：total={qc.get('total')} spare={qc.get('spare')}")
    except Exception as exc:
        print(f"今日调用额度：查询失败（{exc}）")
