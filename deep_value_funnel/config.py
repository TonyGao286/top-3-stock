"""
全局阈值与网络节奏配置。

说明：
- **快照粗筛**：在 **质量初筛** 之前，仅用 ``stock_zh_a_spot_em`` 已有字段（PE/PB/市值等）
  压缩全市场，减少后续财报请求量。见 ``COARSE_*`` 与 ``universe.apply_coarse_prefilter``。
- **PE 分位（估值）**：在 **企业质量初筛（财务硬条件）通过之后**，以东财快照「市盈率-动态」
  作为当前 **PE(TTM)** 近似，与百度股市通「市盈率(TTM) / 近五年」序列对比，计算当前 PE
  在自身近五年分布中的 **分位百分数**（历史中 ``PE<=当前`` 的样本占比×100），仅保留分位不高于
  ``PE_TTM_5Y_PERCENTILE_MAX`` 的股票。PE 请求量取决于质量初筛后剩余股票数；可用
  ``AK_REQUEST_THROTTLE`` 放慢节奏。
- ``ROE_MIN``：单期净资产收益率-加权（``ROEJQ``，%）下限，与 **最新一期** 报告期对齐。
- ``ROE_5Y_AVG_MIN``：最近 ``ROE_5Y_AVG_LOOKBACK_YEARS`` 个 **年报** ``ROEJQ`` 算术平均须 **严格大于**
  该值（%）；在财务漏斗中 **最先** 判断（见 ``stage_financial``）。
- ``DEBT_ASSET_RATIO_MAX``：资产负债率（东财 ``ZCFZL``，%）须 **严格小于** 该值，与最新一期主要指标行对齐。
- ``RETAINED_SURPLUS_MCMP_RATIO_MIN``：十年「总市值增量 / 累计留存利润」下限（见
  ``retained_mcap_value``）；默认 1.0 对应「每留存 1 元至少创造 1 元市值增量」。
- ``FCF_YIELD_MIN``：现金流收益率下限（小数）。默认启用格林沃尔德 **所有者盈余**（经营现金流 − 维护性资本支出）
  ÷ 总市值；维护性资本支出 = 购建长期资产现金 − 扩张性资本支出（见 ``owner_earnings``）。关闭 ``OWNER_EARNINGS_GREENWALD_ENABLE``
  时退回 ``FCFF_BACK``/总市值。
- ``EXCLUDE_FINANCE_UTIL_INDUSTRY_EM``：基础池内按东财行业板块成份剔除金融、公用事业等（见 ``universe``）。
- ``DRAWDOWN_MIN``：近 ``HIST_WINDOW`` 日最大回撤下限（小数）；默认已放宽。可用环境变量 ``AK_DRAWDOWN_MIN``、
  ``run_screening(drawdown_min=...)`` 或 CLI ``--drawdown-min`` 覆盖。

防封 / 机房 IP（含 GitHub Actions）：
- 环境变量 ``AK_REQUEST_THROTTLE``：建议设为 ``2``～``4``，成倍拉长请求间隔与部分阶段休眠。
- 默认 **K 线仅用腾讯**（``stock_zh_a_hist_tx``），不再请求东财 ``push2his``；
  若腾讯不稳定需兜底：``set AK_KLINE_ALLOW_EASTMONEY=1``。
- 首次拉全市场快照 ``stock_zh_a_spot_em`` 前会冷却 ``AK_SPOT_COOLDOWN`` 秒（仍走东财域名）。
"""

from __future__ import annotations

import os


def clamp_drawdown_min(value: float) -> float:
    """将最大回撤阈值限制在 [0.05, 0.95]（小数口径），避免配置笔误。"""
    return max(0.05, min(0.95, float(value)))


# --- 选股硬条件（与用户策略一致，可在此调参） ---
# 当前 PE(TTM) 在近 5 年可比历史中的分位百分数上限（0~100）。分位定义见文件头说明。
PE_TTM_5Y_PERCENTILE_MAX: float = 35.0
# 近五年序列中，有效 PE 样本数低于该值则不参与分位筛选（剔除，避免短历史误判）
PE_TTM_HIST_MIN_SAMPLES: int = 80
# 逐股估值请求完成后的额外休眠（秒）；0 表示仅依赖 http_utils 的全局节流
PE_HIST_INTER_STOCK_SLEEP: float = 0.0

GROSS_MARGIN_MIN: float = 40.0  # 销售毛利率下限（%）
ROE_MIN: float = 20.0  # 单期 ROE（加权 ``ROEJQ``，%）下限；与「最新一期」行对齐
# 近五年（年报）ROEJQ 算术平均须 **>** 该值（严格大于，非 ≥）
ROE_5Y_AVG_MIN: float = 20.0
ROE_5Y_AVG_LOOKBACK_YEARS: int = 5
# 资产负债率（``ZCFZL``，%）上限：须 **严格小于** 该值（与最新一期 ``indicator_em`` 行对齐）
DEBT_ASSET_RATIO_MAX: float = 60.0
# 十年留存 vs 市值：总市值增量（元）/ 累计留存（元）≥ 该值；>1 可将阈值设为 1.01 等
RETAINED_SURPLUS_MCMP_RATIO_MIN: float = 1.0
RETAINED_VALUE_LOOKBACK_YEARS: int = 10  # 回溯完整年报年数

FCF_YIELD_MIN: float = 0.10  # 现金流收益率下限（小数）：默认「所有者盈余/总市值」；见 ``OWNER_EARNINGS_*``
# 格林沃尔德扩张/维护资本开支拆解（资产负债表 ``FIXED_ASSET`` + 利润表 ``TOTAL_OPERATE_INCOME`` + 现金流量表）
OWNER_EARNINGS_GREENWALD_ENABLE: bool = True
# 计算「固定资产/营业总收入」均值的完整年报期数（不含最新一年，与最新年营收变动相乘得扩张性资本支出）
OWNER_EARNINGS_PPE_SALES_HIST_YEARS: int = 5
# 合并年报不足或字段缺失时，是否退回 ``indicator_em`` 的 ``FCFF_BACK`` 收益率
OWNER_EARNINGS_FALLBACK_FCFF: bool = True
# 近 250 日最大回撤下限（小数）：dd = 1 - 现价/区间最高价；0.28 ≈ 至少 28% 回撤。原为 0.50，已适度放宽。
# 运行时覆盖：环境变量 ``AK_DRAWDOWN_MIN``（如 0.25、0.30）、``run_screening(drawdown_min=...)``、CLI ``--drawdown-min``。
DRAWDOWN_MIN: float = 0.28
_dd_raw = (os.environ.get("AK_DRAWDOWN_MIN") or "").strip()
if _dd_raw:
    try:
        DRAWDOWN_MIN = clamp_drawdown_min(float(_dd_raw))
    except ValueError:
        pass
DIV_YIELD_MIN: float = 0.05  # 股息率下限：东财分红详情里为小数（0.05 = 5%）
PAYOUT_RATIO_MIN: float = 50.0  # 近三年平均分红率（净利润口径）下限（%）
LISTING_MIN_YEARS: int = 5  # 上市满多少年才纳入（自然年近似）
HIST_WINDOW: int = 250  # 计算回撤的行情窗口（交易日）

# --- 基础池：剔除东财行业板块成份（金融、公用事业等；每板块一次 ``stock_board_industry_cons_em``）---
EXCLUDE_FINANCE_UTIL_INDUSTRY_EM: bool = True
# 须与 ``ak.stock_board_industry_name_em()`` 的「板块名称」一致；表中不存在的名称会打 warning 并跳过
EXCLUDE_INDUSTRY_BOARDS_EM: tuple[str, ...] = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "公用事业",
)

# --- 全市场快照粗筛（仅东财 spot 列，无财报/百度；基础池之后、质量初筛之前）---
COARSE_PREFILTER_ENABLE: bool = True
# 保留 市盈率-动态 > 该值（默认 0 即剔除亏损与无效 PE）
COARSE_PE_MIN_EXCL: float = 0.0
# 剔除 PE 高于该值的标的；None 表示不设上限
COARSE_PE_MAX: float | None = 150.0
# 剔除 市净率 高于该值或缺失/非正；None 表示不筛市净率
COARSE_PB_MAX: float | None = 40.0
# 总市值下限（元）；None 表示不筛（东财 spot 与后续 FCF 分母口径一致）
COARSE_MIN_TOTAL_MV_YUAN: float | None = 2e9
# 流通市值下限（元）；None 表示不筛
COARSE_MIN_FLOAT_MV_YUAN: float | None = None
# 最新价下限（元）；None 表示不筛（可剔除异常低价噪声）
COARSE_MIN_PRICE: float | None = None

# --- 防封禁 / 节流 ---
# 机房、GitHub Actions 等出口 IP 易被东财重点风控：设置 AK_REQUEST_THROTTLE=3 等成倍放慢（≥0.25）
REQUEST_THROTTLE_MULTIPLIER: float = max(0.25, float(os.environ.get("AK_REQUEST_THROTTLE", "1.0")))

REQUEST_BASE_SLEEP: float = 0.55  # 每次远程请求成功后的基础休眠（秒），原 0.35
REQUEST_JITTER: float = 0.40  # 额外随机抖动上限（秒）
MAX_RETRIES: int = 6  # 单接口默认最大重试次数（不含首次请求）
RETRY_BACKOFF_BASE: float = 1.8  # 指数退避基数（秒）
# 东财日 K 易出现 RemoteDisconnected / Connection aborted，对连接类错误额外加长的等待（秒）
CONNECTION_RETRY_EXTRA_BASE: float = 2.5
CONNECTION_RETRY_EXTRA_STEP: float = 2.0

# 日 K：默认仅腾讯，避免访问 push2his.eastmoney.com。需要东财兜底时：AK_KLINE_ALLOW_EASTMONEY=1
HIST_ALLOW_EASTMONEY_FALLBACK: bool = os.environ.get("AK_KLINE_ALLOW_EASTMONEY", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HIST_PROVIDER_ORDER: tuple[str, ...] = (
    ("tencent", "eastmoney") if HIST_ALLOW_EASTMONEY_FALLBACK else ("tencent",)
)
HIST_PROVIDER_RETRIES: int = 4
HIST_FALLBACK_PAUSE: float = 1.5
HIST_MAX_RETRIES: int = 8
HIST_INTER_STOCK_SLEEP: float = 1.15  # 每处理完一只 K 线后的间隔（秒）
# 分红阶段逐股间隔（东财 fhps 接口）
DIV_INTER_STOCK_SLEEP: float = 0.55

# 首次请求「东财全市场行情」stock_zh_a_spot_em 前额外冷却（秒），可与 AK_REQUEST_THROTTLE 相乘
SPOT_EM_COOLDOWN_SEC: float = max(0.0, float(os.environ.get("AK_SPOT_COOLDOWN", "1.5")))

# --- 运行控制（便于本机调试） ---
MAX_DEEP_CANDIDATES: int | None = None
MAX_HIST_CANDIDATES: int | None = None

# ---------------------------------------------------------------------------
# Tushare 副源配置（Token 仅来自环境变量；勿写入仓库）
# ---------------------------------------------------------------------------
TUSHARE_TOKEN: str = os.environ.get("TUSHARE_TOKEN", "").strip()

_env_ts_cmp = os.environ.get("ENABLE_TUSHARE_COMPARE", "").strip()
if _env_ts_cmp:
    ENABLE_TUSHARE_COMPARE = _env_ts_cmp.lower() not in ("0", "false", "no", "off")
else:
    ENABLE_TUSHARE_COMPARE = bool(TUSHARE_TOKEN)
