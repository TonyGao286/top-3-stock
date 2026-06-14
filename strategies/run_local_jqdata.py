#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 JQData 多因子选股 — 命令行入口。

安装：
  pip install jqdatasdk
  # 若 thriftpy2 编译失败：pip install thriftpy2==0.4.20

配置（任选其一）：
  1. 复制 .env.example 为 .env，填写 JQDATA_PHONE / JQDATA_PASSWORD
  2. 命令行 --phone / --password

示例：
  python strategies/run_local_jqdata.py
  python strategies/run_local_jqdata.py --date 2026-05-28
  python strategies/run_local_jqdata.py --test-auth
"""

from __future__ import annotations

import argparse
import logging
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# jqdatasdk 缓存与 NumPy 2.4 兼容告警，不影响功能
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"jqdatasdk.*")

from strategies.jqdata_client import (  # noqa: E402
    ensure_jqdata_auth,
    print_jqdata_account_summary,
    resolve_screen_date,
)
from strategies.multi_factor_local import run_screen  # noqa: E402
from strategies.jq_multi_factor_weekly import TOP_N  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="JQData 本地多因子选股（量价70% + 盈利30%）")
    p.add_argument("--phone", default=None, help="JQData 手机号（默认读 .env 的 JQDATA_PHONE）")
    p.add_argument("--password", default=None, help="聚宽官网登录密码")
    p.add_argument("--date", default=None, help="截面日期 YYYY-MM-DD（默认最近交易日）")
    p.add_argument("--top-n", type=int, default=TOP_N, help=f"输出前 N 名（默认 {TOP_N}）")
    p.add_argument(
        "--output",
        default=None,
        help="输出 CSV 路径（默认 multi_factor_top{N}_YYYYMMDD.csv）",
    )
    p.add_argument("--test-auth", action="store_true", help="仅测试登录，不跑选股")
    p.add_argument(
        "--no-clamp",
        action="store_true",
        help="禁止自动把截面日校正到 JQData 权限区间内（超出则报错）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    ensure_jqdata_auth(args.phone, args.password)
    if args.test_auth:
        print_jqdata_account_summary()
        print("JQData 登录测试通过。")
        return 0

    as_of, perm, note = resolve_screen_date(args.date, auto_clamp=not args.no_clamp)
    if perm:
        print(f"JQData 数据权限：{perm}")
    if note:
        print(f"注意：{note}")
    elif args.date is None:
        print(f"未指定 --date，使用权限内最近交易日：{as_of}")
    else:
        print(f"截面日：{as_of}")

    df = run_screen(as_of=as_of, top_n=args.top_n)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output) if args.output else ROOT / f"multi_factor_top{args.top_n}_{ts}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, encoding="utf-8-sig")

    print(f"\n=== Top {args.top_n}（截面日 {as_of}）===\n")
    show_cols = ["排名", "名称", "综合得分"] + [c for c in df.columns if c not in ("排名", "名称", "综合得分")]
    print(df[show_cols].head(args.top_n).to_string())
    print(f"\n已保存：{out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
