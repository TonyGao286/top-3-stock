#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子选股 — AkShare 本地入口（推荐，无 JQData 日额度限制）。

示例：
  python strategies/run_local_akshare.py
  python strategies/run_local_akshare.py --workers 2
  python strategies/run_local_akshare.py --limit 100   # 调试：只跑前 100 只
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.jq_multi_factor_weekly import TOP_N  # noqa: E402
from strategies.multi_factor_akshare import run_screen  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="AkShare 本地多因子选股（量价70% + 盈利30%，无聚宽额度限制）"
    )
    p.add_argument("--date", default=None, help="截面日期 YYYY-MM-DD（默认今天；成分股为最新披露）")
    p.add_argument("--top-n", type=int, default=TOP_N, help=f"输出前 N 名（默认 {TOP_N}）")
    p.add_argument("--workers", type=int, default=2, help="并行线程数（建议 1~3，过大易触发东财风控）")
    p.add_argument("--limit", type=int, default=None, help="仅计算前 N 只可交易股票（调试用）")
    p.add_argument("--output", default=None, help="输出 CSV 路径")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    print("数据源：AkShare（东财/腾讯/中证指数），无 JQData 每日条数限制。")
    print("提示：全市场约 1500 只，首次运行较久；可在 .env 设置 AK_REQUEST_THROTTLE=1~2 降低风控概率。\n")

    df = run_screen(
        as_of=args.date,
        top_n=args.top_n,
        workers=args.workers,
        limit=args.limit,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output) if args.output else ROOT / f"multi_factor_akshare_top{args.top_n}_{ts}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, encoding="utf-8-sig")

    print(f"\n=== Top {args.top_n} ===\n")
    show_cols = ["排名", "名称", "综合得分"] + [
        c for c in df.columns if c not in ("排名", "名称", "综合得分")
    ]
    print(df[show_cols].head(args.top_n).to_string())
    print(f"\n已保存：{out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
