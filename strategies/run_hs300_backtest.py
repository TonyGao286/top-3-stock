#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 多因子轮动 — 本地回测 CLI（已弃用 JQData 路径）。

请改用 AkShare 免费数据入口：
  python strategies/run_hs300_akshare.py --start 2024-01-01 --end 2025-12-31
  python strategies/run_hs300_akshare.py --screen
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "本脚本依赖 JQData，已不再推荐。\n"
        "请使用 AkShare 开源数据版本：\n\n"
        "  python strategies/run_hs300_akshare.py --screen\n"
        "  python strategies/run_hs300_akshare.py --start 2024-01-01 --end 2025-12-31\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
