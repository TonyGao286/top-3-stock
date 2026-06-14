# 单股指标 — GitHub Actions + Server酱 部署指南

本文说明如何把 **指定股票深度评分 + 微信推送** 部署到 GitHub，在每个 **A 股交易日** 自动运行。

---

## 一、你会得到什么

| 项目 | 说明 |
|------|------|
| 运行时间 | 每天 **北京时间 20:00** 触发（UTC `0 12 * * *`；HS300 为 00:00，错开限流） |
| 是否每天都跑 | 仅当 **当天是 A 股交易日** 才真正执行；周末/节假日自动跳过 |
| 推送内容 | 每只股票的 **总分、最新价、PE近5年历史分位、ROE加权(近5年均)** |
| 推送分组 | **持仓**（贵州茅台、三一重工）与 **关注**（吉比特、亿联网络、今世缘、古井贡酒） |
| 工作流文件 | `.github/workflows/single-stock-daily-push.yml` |
| 入口脚本 | `daily_push_serverchan.py` |

---

## 二、监控股票清单（默认）

| 分组 | 代码 | 名称 |
|------|------|------|
| 持仓 | 600519 | 贵州茅台 |
| 持仓 | 600031 | 三一重工 |
| 关注 | 603444 | 吉比特 |
| 关注 | 300628 | 亿联网络 |
| 关注 | 603369 | 今世缘 |
| 关注 | 000596 | 古井贡酒 |

修改清单：编辑工作流中的 `HOLDING_CODES` / `TARGET_CODES` / `WATCH_CODES`，或本地 `.env` 同名变量。

---

## 三、前置准备

### 1. GitHub 仓库

- 注册 [GitHub](https://github.com/)
- 新建仓库（Public / Private 均可）

### 2. Server酱（推送到微信）

1. 打开 [https://sct.ftqq.com/](https://sct.ftqq.com/) 登录
2. 微信扫码绑定
3. 复制 **SendKey**（形如 `SCTxxxxxx`）
4. **切勿** 把 SendKey 写进代码或提交到 Git

---

## 四、把代码推到 GitHub

```bash
cd c:\单只股票分析
git init
git add .
git commit -m "Add single stock daily push workflow"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

至少需要上传：

```
.github/workflows/single-stock-daily-push.yml
daily_push_serverchan.py
single_stock_scoring.py
serverchan_push.py
visualize_result.py
deep_value_funnel/
requirements.txt
```

**不要提交**：`.env`、`.venv/`、本地 `single_stock_score_*.xlsx`

---

## 五、配置 GitHub Secret（必做）

1. 仓库 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**

| Name | Value |
|------|--------|
| `SERVERCHAN_SENDKEY` | 你的 Server酱 SendKey |

---

## 六、启用 Actions 并试跑

1. 仓库 → **Actions** → 启用 workflows
2. 左侧选 **「单股指标每日推送」** → **Run workflow** → **Run workflow**
3. 查看日志，成功时应出现 `已推送到微信（Server酱）`
4. 微信收到标题类似：`单股指标日报 2026-06-14`

### 非交易日

日志会显示：

```text
北京时间 2026-xx-xx 非 A 股交易日，跳过本次选股/推送。
```

任务仍为绿色 Success（正常跳过）。

---

## 七、修改推送时间

编辑 `.github/workflows/single-stock-daily-push.yml` 中 `cron`（**UTC 时间**）：

| 北京时间 | UTC cron |
|----------|----------|
| 18:00 | `0 10 * * *` |
| 18:30 | `30 10 * * *` |
| 20:00 | `0 12 * * *` |
| 19:00 | `0 11 * * *` |

公式：`UTC 小时 = 北京时间小时 - 8`

---

## 八、本地对照命令

```bash
# 复制配置
cp .env.example .env
# 编辑 .env 填入 SERVERCHAN_SENDKEY

# 仅打印 Markdown，不推送
python daily_push_serverchan.py --dry-run

# 真实推送（6 只默认清单）
python daily_push_serverchan.py

# 仅交易日推送（与 GitHub 行为一致）
python daily_push_serverchan.py --trading-day-only
```

---

## 九、与 HS300 选股工作流的关系

本仓库可同时保留两个定时任务（**统一部署见 [GITHUB_DEPLOY.md](GITHUB_DEPLOY.md)**）：

| 工作流 | 时间 | 用途 |
|--------|------|------|
| `hs300-daily-screen.yml` | 00:00 | 沪深300 多因子 Top3 |
| `single-stock-daily-push.yml` | 20:00 | 你的持仓 + 关注股深度评分 |

若只需单股推送，可在 Actions 里禁用 HS300 工作流。

---

## 十、常见问题

### 1. 没收到微信

- Secret 名称必须为 **`SERVERCHAN_SENDKEY`**
- Server酱 后台是否仍绑定微信
- Actions 日志是否有 `已推送到微信`

### 2. 任务超时 / 失败

- 6 只股票约需 **15～25 分钟**（拉财报 + PE 分位 + 回撤）
- 工作流 `timeout-minutes: 90`
- 东财限流时可重试；已设 `AK_REQUEST_THROTTLE=3`

### 3. 修改监控股票

编辑 workflow 中环境变量，或本地 `.env`：

```env
HOLDING_CODES=600519,600031
TARGET_CODES=603444,300628,603369,000596
WATCH_CODES=600519,600031,603444,300628,603369,000596
```

最多 **10 只**（`single_stock_scoring.MAX_CODES`）。

---

## 十一、安全提醒

- SendKey 等同密钥，只用 GitHub Secrets 存储
- 不要将 `.env` 提交到 Git

完成以上步骤后，每个 **A 股交易日** 会自动收到持仓与关注股的指标推送。
