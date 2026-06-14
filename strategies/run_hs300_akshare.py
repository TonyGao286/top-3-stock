#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 小市值 + 高 ROE 多因子轮动 — AkShare 本地入口（推荐，完全免费开源数据）。

示例：
  python strategies/run_hs300_akshare.py --screen
  python strategies/run_hs300_akshare.py --start 2024-01-01 --end 2025-12-31
  python strategies/run_hs300_akshare.py --start 2024-01-01 --end 2025-12-31 --workers 2
  python strategies/run_hs300_akshare.py --screen --push --trading-day-only
  # 指定历史截面日：python strategies/run_hs300_akshare.py --screen --date 2026-06-02
  python strategies/run_hs300_akshare.py --start 2024-01-01 --end 2025-12-31

GitHub Actions 每日推送：见 .github/workflows/hs300-daily-screen.yml
统一部署（HS300 + 单股）：见 docs/GITHUB_DEPLOY.md
  需在仓库 Secrets 配置 SERVERCHAN_SENDKEY
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.hs300_multi_factor_akshare import run_screen  # noqa: E402
from strategies.hs300_multi_factor_backtest_akshare import BacktestConfig, run_backtest  # noqa: E402
from strategies.hs300_multi_factor_core import TOP_N  # noqa: E402
from strategies.trade_calendar import china_today, resolve_screen_date  # noqa: E402


def _load_dotenv() -> None:
    path = ROOT / ".env"
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


def _push_screen_result(df, as_of: date, out: Path, *, dry_run: bool = False) -> None:
    from serverchan_push import send_serverchan
    from strategies.hs300_screen_notify import format_screen_push

    title, desp = format_screen_push(df, as_of, csv_path=out, top_n=TOP_N)
    if dry_run:
        print("\n=== Server酱 dry-run ===\n")
        print(f"Title: {title}\n")
        print(desp)
        return
    send_serverchan(title, desp)
    print("Server酱推送成功。")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _print_metrics(metrics: dict) -> None:
    print("\n=== 回测绩效（AkShare）===\n")
    rows = [
        ("数据源", metrics.get("data_source", "AkShare")),
        ("区间", f"{metrics['start_date']} ~ {metrics['end_date']}"),
        ("交易日数", metrics["trading_days"]),
        ("调仓次数", metrics["rebalance_count"]),
        ("初始资金", f"{metrics['initial_cash']:,.0f}"),
        ("期末资产", f"{metrics['final_value']:,.2f}"),
        ("策略总收益", f"{metrics['total_return']:.2%}"),
        ("基准总收益", f"{metrics['benchmark_total_return']:.2%}"),
        ("超额总收益", f"{metrics['excess_total_return']:.2%}"),
        ("策略年化", f"{metrics['annualized_return']:.2%}"),
        ("基准年化", f"{metrics['benchmark_annualized_return']:.2%}"),
        ("策略最大回撤", f"{metrics['max_drawdown']:.2%}"),
        ("基准最大回撤", f"{metrics['benchmark_max_drawdown']:.2%}"),
        ("夏普比率", f"{metrics['sharpe']:.2f}"),
    ]
    for k, v in rows:
        print(f"  {k:12s}: {v}")
    print(
        "\n说明：成分股为当前沪深300名单（非历史逐日成分），"
        "回测结果仅供策略逻辑验证，不代表真实可交易绩效。"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="沪深300 多因子轮动（AkShare 免费数据，15 日调仓持 3 只）")
    p.add_argument("--screen", action="store_true", help="单次截面选股")
    p.add_argument("--date", default=None, help="截面日 YYYY-MM-DD（默认=北京时间当日，仅交易日）")
    p.add_argument(
        "--trading-day-only",
        action="store_true",
        help="仅在北京时间「当天为 A 股交易日」时运行；非交易日直接退出（GitHub Actions 推荐）",
    )
    p.add_argument("--start", default=None, help="回测起始 YYYY-MM-DD")
    p.add_argument("--end", default=None, help="回测结束 YYYY-MM-DD（默认今天）")
    p.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金")
    p.add_argument("--workers", type=int, default=2, help="并行线程（建议 1~3）")
    p.add_argument("--limit", type=int, default=None, help="仅使用前 N 只成分股（调试）")
    p.add_argument(
        "--fast",
        action="store_true",
        help="快速模式：跳过 63 日停牌过滤（与聚宽结果可能不一致）",
    )
    p.add_argument("--output", default=None, help="输出 CSV 路径")
    p.add_argument("--push", action="store_true", help="选股完成后通过 Server酱 推送到微信")
    p.add_argument("--dry-run", action="store_true", help="仅打印推送内容，不调用 Server酱")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _load_dotenv()
    _setup_logging(args.verbose)
    print("数据源：AkShare（中证指数 / 东财 / 腾讯），无聚宽额度限制。\n")

    trading_day_only = args.trading_day_only or os.environ.get("GITHUB_ACTIONS") == "true"

    if args.screen:
        req = _parse_date(args.date) if args.date else None
        as_of, note = resolve_screen_date(req, trading_day_only=trading_day_only)
        if as_of is None:
            print(note or "非交易日，跳过。")
            return 0
        if note:
            print(f"注意：{note}")
        print(f"截面日（A 股交易日）：{as_of}（北京时间今日 {china_today()}）\n")
        df = run_screen(
            as_of=as_of,
            top_n=TOP_N,
            workers=args.workers,
            limit=args.limit,
            strict_jq=not args.fast,
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.output:
            out = Path(args.output)
        else:
            out = ROOT / f"hs300_akshare_screen_{as_of.strftime('%Y%m%d')}_{ts}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, encoding="utf-8-sig")
        print(f"\n=== Top {TOP_N} 选股结果 ===\n")
        picked = df[df["选中"]].head(TOP_N)
        if not picked.empty:
            print("【选中】（聚宽代码可直接对比）")
            print(
                picked[
                    ["聚宽代码", "名称", "market_cap_亿", "roe", "composite_score"]
                ].to_string()
            )
        print(f"\n【全池前 10】\n{df.head(10).to_string()}")
        print(f"\n已保存：{out.resolve()}")
        if args.push or args.dry_run:
            _push_screen_result(df, as_of, out, dry_run=args.dry_run)
        return 0

    if not args.start:
        raise SystemExit("回测请指定 --start YYYY-MM-DD；单次选股请加 --screen")

    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else date.today()
    if start > end:
        raise SystemExit("--start 不能晚于 --end")

    print(f"回测区间：{start} ~ {end}")
    print("首次运行需拉取 ~300 只成分股 K 线与估值，耗时较长，请耐心等待。\n")

    result = run_backtest(
        BacktestConfig(
            start_date=start,
            end_date=end,
            initial_cash=args.cash,
            workers=args.workers,
            limit=args.limit,
        )
    )
    _print_metrics(result["metrics"])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    eq_out = Path(args.output) if args.output else ROOT / f"hs300_akshare_backtest_{ts}.csv"
    eq_out.parent.mkdir(parents=True, exist_ok=True)
    result["equity_curve"].to_csv(eq_out, encoding="utf-8-sig")
    print(f"\n净值曲线已保存：{eq_out.resolve()}")

    if result["rebalance_log"]:
        print("\n最近 3 次调仓：")
        for row in result["rebalance_log"][-3:]:
            print(f"  {row['date']}: {row['targets']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
