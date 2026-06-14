#!/usr/bin/env bash
# 阿里云 ECS（Ubuntu 22.04/24.04）一次性环境安装
# 用法：bash install.sh [安装目录，默认 $HOME/stock-analysis]
set -euo pipefail

INSTALL_DIR="${1:-$HOME/stock-analysis}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

echo "==> 安装目录: ${INSTALL_DIR}"

if command -v apt-get >/dev/null 2>&1; then
  echo "==> 安装系统依赖 (apt)…"
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates tzdata
fi

if [[ ! -d "${INSTALL_DIR}" ]]; then
  echo "错误：目录不存在 ${INSTALL_DIR}"
  echo "请先把项目上传到该目录，例如："
  echo "  scp -r ./单只股票分析/* root@你的ECSIP:${INSTALL_DIR}/"
  exit 1
fi

cd "${INSTALL_DIR}"

if [[ ! -f requirements.txt ]]; then
  echo "错误：${INSTALL_DIR} 下缺少 requirements.txt，请确认上传的是项目根目录"
  exit 1
fi

echo "==> 设置时区为 Asia/Shanghai"
sudo timedatectl set-timezone Asia/Shanghai 2>/dev/null || true

echo "==> 创建 Python 虚拟环境"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip wheel
pip install -r requirements.txt -i "${PYPI_MIRROR}"

mkdir -p logs

if [[ ! -f .env ]]; then
  cp -n .env.example .env 2>/dev/null || true
  echo ""
  echo "!! 请编辑 ${INSTALL_DIR}/.env ，填写 SERVERCHAN_SENDKEY 和 WATCH_CODES"
fi

chmod +x scripts/aliyun/daily_job.sh 2>/dev/null || true

echo ""
echo "==> 安装完成。下一步："
echo "  1) nano ${INSTALL_DIR}/.env"
echo "  2) 测试：cd ${INSTALL_DIR} && bash scripts/aliyun/daily_job.sh"
echo "  3) 启用每日定时：bash scripts/aliyun/setup_systemd.sh ${INSTALL_DIR}"
