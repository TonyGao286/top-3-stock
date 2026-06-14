#!/usr/bin/env bash
# 每日任务：重新拉数评分 + 补算 PE 历史分位 + Server酱推送微信
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

export TZ="${TZ:-Asia/Shanghai}"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

# 云机房 IP 易被东财限流，默认放慢（可在 .env 覆盖）
export AK_REQUEST_THROTTLE="${AK_REQUEST_THROTTLE:-3}"

VENV_PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  echo "错误：未找到 ${VENV_PY}，请先运行 scripts/aliyun/install.sh"
  exit 1
fi

mkdir -p "${ROOT}/logs"
LOG="${ROOT}/logs/daily_$(date +%Y%m%d).log"

{
  echo "========================================"
  echo "开始: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "目录: ${ROOT}"
  echo "AK_REQUEST_THROTTLE=${AK_REQUEST_THROTTLE}"
  echo "WATCH_CODES=${WATCH_CODES:-（未设置，需 --codes 或 .env）}"
  echo "----------------------------------------"

  # 重新评分并推送（会保存 single_stock_score_*.xlsx）
  "${VENV_PY}" daily_push_serverchan.py --trading-day-only --output-dir "${ROOT}/outputs"

  echo "----------------------------------------"
  echo "结束: $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >>"${LOG}" 2>&1

echo "任务完成，日志: ${LOG}"
