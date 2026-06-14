#!/usr/bin/env bash
# 注册 systemd 定时任务（每天 18:00，Asia/Shanghai 时区）
# 用法：bash setup_systemd.sh [项目目录]
set -euo pipefail

INSTALL_DIR="$(cd "${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}" && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
DAILY_SH="${INSTALL_DIR}/scripts/aliyun/daily_job.sh"

if [[ ! -f "${DAILY_SH}" ]]; then
  echo "错误：找不到 ${DAILY_SH}"
  exit 1
fi
chmod +x "${DAILY_SH}"

SERVICE_NAME="stock-daily-push"
UNIT_DIR="/etc/systemd/system"
SERVICE_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"
TIMER_FILE="${UNIT_DIR}/${SERVICE_NAME}.timer"

echo "==> 项目目录: ${INSTALL_DIR}"
echo "==> 运行用户: ${RUN_USER}"

sudo tee "${SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=单只股票每日评分并推送微信（Server酱）
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=TZ=Asia/Shanghai
EnvironmentFile=-${INSTALL_DIR}/.env
ExecStart=${DAILY_SH}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee "${TIMER_FILE}" >/dev/null <<EOF
[Unit]
Description=每天 18:00 执行单股推送

[Timer]
OnCalendar=*-*-* 18:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.timer"
sudo systemctl list-timers "${SERVICE_NAME}.timer" --no-pager

echo ""
echo "==> 已启用。常用命令："
echo "  立即试跑：sudo systemctl start ${SERVICE_NAME}.service"
echo "  查看状态：systemctl status ${SERVICE_NAME}.timer"
echo "  查看日志：journalctl -u ${SERVICE_NAME}.service -n 100 --no-pager"
echo "  业务日志：tail -f ${INSTALL_DIR}/logs/daily_*.log"
