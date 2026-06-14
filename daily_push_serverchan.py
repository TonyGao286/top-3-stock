#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日将关键指标通过 Server酱 推送到微信。

推送字段（与可视化一致）：
- 总分：评分_总分(均分)
- 最新价
- PE近5年历史分位：PE(TTM)近5年历史分位_pct（当前 PE 在近5年序列中的分位）
- ROE加权近五年：ROE加权_近五年年报算术平均_pct

配置（环境变量或 .env）：
- SERVERCHAN_SENDKEY：Server酱 SendKey（必填）
- WATCH_CODES：默认监控代码，逗号/空格分隔（与 HOLDING_CODES + TARGET_CODES 二选一）
- HOLDING_CODES / TARGET_CODES：推送时分组展示（持仓 / 关注）

示例：
  python daily_push_serverchan.py --codes 600031 600519 603444
  python daily_push_serverchan.py --trading-day-only
  python daily_push_serverchan.py --from-xlsx   # 用最新评分表，仅补算 PE 历史分位后推送
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ[_k] = ""

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd

from serverchan_push import get_sendkey, send_serverchan
from single_stock_scoring import MAX_CODES, run_codes, save_report
from strategies.trade_calendar import resolve_screen_date
from visualize_result import _baidu_valuation_series, _percentile_rank

logger = logging.getLogger(__name__)

PE_HIST_COL = "PE(TTM)近5年历史分位_pct"
ROE5_COL = "ROE加权_近五年年报算术平均_pct"
SCORE_COL = "评分_总分(均分)"
PRICE_COL = "最新价"

# 推送分组默认（可被环境变量覆盖）
DEFAULT_HOLDING_CODES = "600519,600031"
DEFAULT_TARGET_CODES = "603444,300628,603369,000596"
DEFAULT_WATCH_CODES = "600519,600031,603444,300628,603369,000596"

# 常见简称兜底（东财/百度接口未返回名称时使用）
CODE_NAME_FALLBACK: dict[str, str] = {
    "600519": "贵州茅台",
    "600031": "三一重工",
    "603444": "吉比特",
    "300628": "亿联网络",
    "603369": "今世缘",
    "000596": "古井贡酒",
}


def _load_dotenv(path: Path | None = None) -> None:
    path = path or Path(__file__).resolve().parent / ".env"
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


def _parse_codes(raw: str | None) -> List[str]:
    if not raw:
        return []
    parts: List[str] = []
    for chunk in raw.replace(",", " ").split():
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if len(digits) >= 6:
            parts.append(digits[-6:].zfill(6))
    out: List[str] = []
    for c in parts:
        if c not in out:
            out.append(c)
    return out


def _resolve_watch_codes(cli_codes: List[str]) -> List[str]:
    if cli_codes:
        return cli_codes[:MAX_CODES]
    watch = _parse_codes(os.environ.get("WATCH_CODES", ""))
    if watch:
        return watch[:MAX_CODES]
    holdings = _parse_codes(os.environ.get("HOLDING_CODES", DEFAULT_HOLDING_CODES))
    targets = _parse_codes(os.environ.get("TARGET_CODES", DEFAULT_TARGET_CODES))
    merged: List[str] = []
    for c in holdings + targets:
        if c not in merged:
            merged.append(c)
    if not merged:
        merged = _parse_codes(DEFAULT_WATCH_CODES)
    return merged[:MAX_CODES]


def _code_group_map() -> dict[str, str]:
    """code -> 分组标题（持仓 / 关注）。"""
    groups: dict[str, str] = {}
    for c in _parse_codes(os.environ.get("HOLDING_CODES", DEFAULT_HOLDING_CODES)):
        groups[c] = "持仓"
    for c in _parse_codes(os.environ.get("TARGET_CODES", DEFAULT_TARGET_CODES)):
        if c not in groups:
            groups[c] = "关注"
    return groups


def _display_name(row: pd.Series) -> str:
    name = str(row.get("名称") or "").strip()
    if name and name.lower() != "nan":
        return name
    code = str(row.get("代码") or "").zfill(6)
    return CODE_NAME_FALLBACK.get(code, "")


def _fmt_num(v: Any, *, decimals: int = 2, suffix: str = "") -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or not math.isfinite(v))):
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "—"
    s = f"{f:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{s}{suffix}"


def _pick_latest_xlsx(directory: Path, pattern: str = "single_stock_score_*.xlsx") -> Path:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError(f"未找到 {pattern}（目录：{directory}）")
    return files[-1]


def _augment_pe_history_only(df: pd.DataFrame) -> pd.DataFrame:
    """仅补算 PE(TTM) 近5年历史分位，避免拉股息率等无关接口。"""
    if df is None or df.empty or "代码" not in df.columns:
        return df
    d = df.copy()
    if PE_HIST_COL not in d.columns:
        d[PE_HIST_COL] = None
    cache: dict[str, list[float]] = {}
    for i, r in d.iterrows():
        if pd.notna(r.get(PE_HIST_COL)):
            continue
        code = str(r.get("代码") or "").zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        try:
            vals = cache.get(code)
            if vals is None:
                _, vals = _baidu_valuation_series(code, indicator="市盈率(TTM)", period="近五年")
                cache[code] = vals
            if vals:
                d.at[i, PE_HIST_COL] = _percentile_rank(vals, vals[-1])
        except Exception:
            logger.debug("[%s] PE历史分位补算失败", code, exc_info=True)
    return d


def _ensure_pe_history_percentile(df: pd.DataFrame) -> pd.DataFrame:
    if PE_HIST_COL in df.columns and df[PE_HIST_COL].notna().any():
        return df
    logger.info("补算 %s …", PE_HIST_COL)
    return _augment_pe_history_only(df)


def build_push_markdown(df: pd.DataFrame, *, eval_time: str | None = None) -> tuple[str, str]:
    """返回 (title, desp_markdown)。"""
    eval_time = eval_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"单股指标日报 {eval_time[:10]}"

    lines: List[str] = [
        f"**评估时间**：{eval_time}",
        "",
    ]

    if df is None or df.empty:
        lines.append("（无数据）")
        return title, "\n".join(lines)

    groups = _code_group_map()

    def _row_block(row: pd.Series) -> List[str]:
        code = str(row.get("代码") or "").zfill(6)
        name = _display_name(row)
        err = row.get("错误")
        if err and pd.notna(err):
            return [f"### {code} {name}".strip(), f"- **错误**：{err}", ""]

        header = f"### {code} {name}".strip()
        score = row.get(SCORE_COL)
        price = row.get(PRICE_COL)
        pe_hist = row.get(PE_HIST_COL)
        if pe_hist is None or (isinstance(pe_hist, float) and pd.isna(pe_hist)):
            pe_hist = row.get("PE近5年分位_pct")
        roe5 = row.get(ROE5_COL)

        return [
            header,
            f"- **总分**：{_fmt_num(score, decimals=1, suffix=' 分')}",
            f"- **最新价**：{_fmt_num(price, decimals=2, suffix=' 元')}",
            f"- **PE近5年历史分位**：{_fmt_num(pe_hist, decimals=1, suffix='%')}",
            f"- **ROE加权(近5年均)**：{_fmt_num(roe5, decimals=2, suffix='%')}",
            "",
        ]

    if groups:
        section_order = ["持仓", "关注"]
        for section in section_order:
            section_codes = [c for c, g in groups.items() if g == section]
            if not section_codes:
                continue
            subset = df[df["代码"].astype(str).str.zfill(6).isin(section_codes)]
            if subset.empty:
                continue
            lines.append(f"## {section}")
            lines.append("")
            for _, row in subset.iterrows():
                lines.extend(_row_block(row))
        listed = set(df["代码"].astype(str).str.zfill(6))
        grouped = set(groups.keys())
        extra = df[df["代码"].astype(str).str.zfill(6).isin(listed - grouped)]
        if not extra.empty:
            lines.append("## 其他")
            lines.append("")
            for _, row in extra.iterrows():
                lines.extend(_row_block(row))
    else:
        for _, row in df.iterrows():
            lines.extend(_row_block(row))

    lines.append("---")
    lines.append("*由 daily_push_serverchan.py 自动推送*")
    return title, "\n".join(lines)


def collect_dataframe(
    codes: List[str],
    *,
    from_xlsx: Path | None,
    output_dir: Path,
    save_xlsx: bool,
    augment_pe: bool,
) -> pd.DataFrame:
    if from_xlsx is not None:
        logger.info("读取评分表：%s", from_xlsx)
        df = pd.read_excel(from_xlsx, sheet_name="Score")
    else:
        if not codes:
            raise ValueError("未指定股票代码（--codes 或环境变量 WATCH_CODES）")
        logger.info("拉取并评分：%s", ", ".join(codes))
        df = run_codes(codes)
        if save_xlsx:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = output_dir / f"single_stock_score_{ts}.xlsx"
            save_report(df, out)
            logger.info("已保存：%s", out)

    if augment_pe:
        df = _ensure_pe_history_percentile(df)
    return df


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Server酱每日推送关键指标到微信")
    p.add_argument(
        "--codes",
        nargs="+",
        default=None,
        help=f"股票代码（最多 {MAX_CODES} 只）；不填则用 WATCH_CODES 或 HOLDING_CODES+TARGET_CODES",
    )
    p.add_argument(
        "--from-xlsx",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="从已有评分 xlsx 读取（省略 PATH 则用最新 single_stock_score_*.xlsx）",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="保存新评分 xlsx 的目录（非 --from-xlsx 时）",
    )
    p.add_argument(
        "--no-save-xlsx",
        action="store_true",
        help="重新评分时不写 xlsx（仅推送）",
    )
    p.add_argument(
        "--no-augment-pe",
        action="store_true",
        help="不补算 PE近5年历史分位（推送可能为 —）",
    )
    p.add_argument(
        "--trading-day-only",
        action="store_true",
        help="仅 A 股交易日执行；非交易日跳过（GitHub Actions 默认启用）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 Markdown，不调用 Server酱",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    trading_day_only = args.trading_day_only or os.environ.get("GITHUB_ACTIONS") == "true"
    if trading_day_only:
        as_of, note = resolve_screen_date(None, trading_day_only=True)
        if as_of is None:
            print(note or "非 A 股交易日，跳过推送。")
            return 0

    codes = _resolve_watch_codes(_parse_codes(" ".join(args.codes) if args.codes else None))

    from_path: Path | None = None
    if args.from_xlsx is not None:
        if str(args.from_xlsx).strip():
            from_path = Path(args.from_xlsx).resolve()
        else:
            from_path = _pick_latest_xlsx(args.output_dir.resolve())

    try:
        df = collect_dataframe(
            codes,
            from_xlsx=from_path,
            output_dir=args.output_dir.resolve(),
            save_xlsx=not args.no_save_xlsx and from_path is None,
            augment_pe=not args.no_augment_pe,
        )
    except Exception as exc:
        logger.exception("准备推送数据失败")
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    title, desp = build_push_markdown(df)
    if args.dry_run:
        print("=== TITLE ===")
        print(title)
        print("=== MARKDOWN ===")
        print(desp)
        return 0

    try:
        get_sendkey()
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        send_serverchan(title, desp)
    except Exception as exc:
        logger.exception("Server酱推送失败")
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print("已推送到微信（Server酱）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
