# 沪深300 多因子选股 — GitHub Actions + Server酱 部署指南

本文说明如何把本仓库部署到 GitHub，由 **Actions 在每个 A 股交易日** 自动跑选股，并通过 **Server酱** 推送到微信。

---

## 一、你会得到什么

| 项目 | 说明 |
|------|------|
| 运行时间 | 每天 **北京时间 00:00（午夜）** 触发（UTC `0 16 * * *`） |
| 是否每天都跑 | 仅当 **当天是 A 股交易日** 才真正执行；周末/节假日自动跳过 |
| 截面日 | 默认 **北京时间当日**（如 2026-06-02） |
| 推送内容 | 沪深300 多因子 Top3（聚宽教程算法） |
| 工作流文件 | `.github/workflows/hs300-daily-screen.yml` |
| 入口脚本 | `strategies/run_hs300_akshare.py` |

---

## 二、前置准备

### 1. GitHub 账号与仓库

- 注册 [GitHub](https://github.com/)
- 新建仓库（Public / Private 均可；Private 仓库 Actions 需在 Settings 里开启）

### 2. Server酱（推送到微信）

1. 打开 [https://sct.ftqq.com/](https://sct.ftqq.com/) 登录
2. 按页面提示用 **微信扫码** 绑定
3. 在控制台复制 **SendKey**（形如 `SCTxxxxxx`）
4. **切勿** 把 SendKey 写进代码或提交到 Git

### 3. 本机（可选，用于先本地试跑）

```bash
cd 单只股票分析
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

---

## 三、把代码推到 GitHub

### 方式 A：本机已有项目，用 Git 推送

```bash
cd c:\单只股票分析
git init
git add .
git commit -m "Add HS300 daily screen workflow"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

### 方式 B：GitHub 网页上传

1. 新建空仓库
2. **Upload files**，上传整个项目（至少包含）：
   - `.github/workflows/hs300-daily-screen.yml`
   - `strategies/`（含 `run_hs300_akshare.py`、`hs300_multi_factor_*.py`、`trade_calendar.py` 等）
   - `serverchan_push.py`
   - `deep_value_funnel/`（K 线拉取依赖）
   - `requirements.txt`

### 不要提交的文件

以下已在 `.gitignore` 中，**不要**强行加入仓库：

- `.env`（含 SendKey）
- `.venv/`
- 本地生成的 `hs300_akshare_*.csv`

---

## 四、配置 GitHub Secret（必做）

1. 打开仓库 → **Settings** → **Secrets and variables** → **Actions**
2. 点击 **New repository secret**
3. 填写：

| Name | Value |
|------|--------|
| `SERVERCHAN_SENDKEY` | 你的 Server酱 SendKey |

4. 保存

> Actions 里通过 `${{ secrets.SERVERCHAN_SENDKEY }}` 读取，不会出现在日志明文里。

---

## 五、启用 GitHub Actions

1. 仓库页 → **Actions**
2. 若提示 *Workflows aren’t being run on this fork* 等，点 **I understand my workflows will go to …** 启用
3. 左侧应出现 **「HS300 每日选股推送」**

---

## 六、第一次手动试跑（强烈建议）

在依赖定时任务之前，先手动跑通一次：

1. **Actions** → **HS300 每日选股推送** → **Run workflow**
2. `screen_date`：
   - **留空**：使用北京时间当天（须为交易日）
   - 或填 `2026-06-02` 等历史交易日做测试
3. 点绿色 **Run workflow**
4. 点开本次运行，查看 **Run HS300 screen and Server酱 push** 日志

### 成功标志

- 日志末尾有 `Server酱推送成功。`
- 微信收到标题类似：`HS300多因子 Top3 2026-06-02（A股交易日）`
- **Artifacts** 里可下载 `hs300_screen_*.csv`

### 非交易日

日志会出现类似：

```text
北京时间 2026-xx-xx 非 A 股交易日，跳过本次选股/推送。
```

任务显示绿色 **Success**（正常跳过，不是失败）。

---

## 七、定时任务说明

工作流 `on.schedule`：

```yaml
cron: "0 16 * * *"   # UTC 16:00 = 北京时间 00:00（午夜）
```

脚本参数：

```bash
python strategies/run_hs300_akshare.py \
  --screen \
  --push \
  --trading-day-only \
  --workers 1
```

- **`--trading-day-only`**：仅当「北京时间当天 = A 股交易日」才选股并推送
- **`GITHUB_ACTIONS=true`** 时也会自动启用该逻辑

### 修改推送时间

编辑 `.github/workflows/hs300-daily-screen.yml` 中 `cron`（**UTC 时间**）：

| 北京时间 | UTC cron |
|----------|----------|
| 00:00（午夜） | `0 16 * * *` |
| 18:00 | `0 10 * * *` |
| 19:00 | `0 11 * * *` |

公式：`UTC 小时 = 北京时间小时 - 8`（注意夏令时中国无夏令时，固定 +8 即可）。

---

## 八、本地对照命令

```bash
# 仅打印推送内容，不发送
python strategies/run_hs300_akshare.py --screen --push --dry-run --trading-day-only

# 真实推送（.env 中配置 SERVERCHAN_SENDKEY）
python strategies/run_hs300_akshare.py --screen --push --trading-day-only

# 指定截面日
python strategies/run_hs300_akshare.py --screen --date 2026-06-02 --push
```

`.env` 示例见项目根目录 `.env.example`。

---

## 九、常见问题

### 1. 没收到微信推送

- 检查 Secret 名称是否为 **`SERVERCHAN_SENDKEY`**（区分大小写）
- Server酱后台是否仍绑定微信、SendKey 是否过期
- Actions 日志是否 `Server酱推送成功`

### 2. 任务失败 / 超时

- 首次需拉取约 300 只成分股 K 线与估值，可能 **30～90 分钟**
- 工作流 `timeout-minutes: 180`，一般够用
- 东财限流时多试几次；已设 `AK_REQUEST_THROTTLE=3`

### 3. 推送日期不对

- 不要在工作流里写死 `--date`，留空即可用 **当日交易日**
- 手动 Run workflow 时若填了旧日期，会跑该历史日

### 4. Actions 在 fork 仓库不跑 schedule

- Fork 后默认禁用定时任务；到 Actions 页手动启用，或在自己账号下新建仓库推送代码

### 5. 私有仓库 Actions 分钟数

- GitHub 免费账户每月有 Actions 额度；本任务每个交易日约 1 次，通常足够

---

## 十、文件清单速查

```
.github/workflows/hs300-daily-screen.yml   # 定时 + 推送
strategies/run_hs300_akshare.py            # 命令行入口
strategies/hs300_multi_factor_akshare.py   # AkShare 数据
strategies/trade_calendar.py               # A 股交易日判断
strategies/hs300_screen_notify.py          # 推送文案
serverchan_push.py                         # Server酱 API
requirements.txt                           # Python 依赖
```

---

## 十一、安全提醒

- **SendKey = 密钥**，泄露后他人可向你的微信发消息
- 只用 GitHub **Secrets** 存储，不要写在 workflow 明文里
- 不要将 `.env` 提交到 Git

完成以上步骤后，每个 **A 股交易日 00:00（默认）** 会自动收到当日沪深300多因子 Top3 微信推送。

---

## 与单股推送一并部署

若同时启用 **单股指标每日推送**，见 [GITHUB_DEPLOY.md](GITHUB_DEPLOY.md)（一次配置 `SERVERCHAN_SENDKEY` 即可）。
