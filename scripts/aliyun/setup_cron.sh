#!/usr/bin/env bash
# 使用 crontab 每天 18:00 执行（不依赖 systemd）
# 用法：bash setup_cron.sh [项目目录]
set -euo pipefail

INSTALL_DIR="$(cd "${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}" && pwd)"
DAILY_SH="${INSTALL_DIR}/scripts/aliyun/daily_job.sh"
chmod +x "${DAILY_SH}"

CRON_LINE="0 18 * * * TZ=Asia/Shanghai ${DAILY_SH} >> ${INSTALL_DIR}/logs/cron.log 2>&1"

( crontab -l 2>/dev/null | grep -v "daily_job.sh" || true
  echo "${CRON_LINE}"
) | crontab -

echo "==> 已写入 crontab："
crontab -l | grep daily_job || true
echo ""
echo "每天 18:00（上海时区）执行。试跑：${DAILY_SH}"
