#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定股票分析入口（最多 10 只）：

- 收集关键数据（财务/估值/分红/回撤）
- 按 `deep_value_funnel/config.py` 阈值打分
- 输出 `single_stock_score_*.xlsx`
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ[_k] = ""

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from single_stock_scoring import run_codes as run_single_codes, save_report as save_single_report

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="指定股票（≤5）关键数据收集 + 阈值评分 + 回撤评分",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--codes",
        nargs="+",
        default=None,
        metavar="CODE",
        help="指定最多 10 只股票代码（如 600519 000001）",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="产出文件目录",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)
    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.codes:
        print("错误：必须指定 --codes（最多 10 只股票）", file=sys.stderr)
        return 2
    try:
        df = run_single_codes(list(args.codes))
    except Exception as exc:
        print(f"错误：指定股票分析失败：{exc}", file=sys.stderr)
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_xlsx = out_dir / f"single_stock_score_{ts}.xlsx"
    save_single_report(df, out_xlsx)
    print(f"已生成：{out_xlsx.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
