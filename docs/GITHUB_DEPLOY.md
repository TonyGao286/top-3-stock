# GitHub Actions 统一部署指南

一次配置，同时启用 **两个** 每日自动任务，均通过 **Server酱** 推送到微信。

| 工作流 | 推送时间（北京时间） | 内容 |
|--------|----------------------|------|
| **HS300 每日选股推送** | 00:00（午夜） | 沪深300 小市值 + 高 ROE 多因子 Top3 |
| **单股指标每日推送** | 20:00 | 持仓 2 只 + 关注 4 只深度评分指标 |

两个任务均 **仅在 A 股交易日** 执行；周末/节假日自动跳过（绿色 Success）。

详细说明见：

- [HS300 多因子选股](GITHUB_HS300_DEPLOY.md)
- [单股指标推送](GITHUB_SINGLE_STOCK_DEPLOY.md)

---

## 一、一次性准备

### 1. GitHub 仓库

1. 注册 [GitHub](https://github.com/)
2. 新建仓库（Public / Private 均可；Private 需在 Settings 开启 Actions）

### 2. Server酱

1. 打开 [https://sct.ftqq.com/](https://sct.ftqq.com/) 登录并绑定微信
2. 复制 **SendKey**（形如 `SCTxxxxxx`）

### 3. 配置 Secret（两个工作流共用）

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|------|--------|
| `SERVERCHAN_SENDKEY` | 你的 SendKey |

> 只需配置 **一个** Secret，HS300 与单股推送共用。

---

## 二、推送代码到 GitHub

在项目根目录（`c:\单只股票分析`）：

```powershell
git init
git add .
git commit -m "Add HS300 and single stock daily push workflows"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

### 必须包含的文件/目录

```
.github/workflows/hs300-daily-screen.yml      # HS300 定时任务
.github/workflows/single-stock-daily-push.yml # 单股定时任务
strategies/                                   # HS300 选股逻辑
deep_value_funnel/                            # 数据拉取与评分
daily_push_serverchan.py                      # 单股推送入口
single_stock_scoring.py
serverchan_push.py
visualize_result.py
run_pipeline.py
requirements.txt
docs/
```

### 不要提交

- `.env`（含 SendKey）
- `.venv/`
- `outputs/`、`single_stock_score_*.xlsx`、`hs300_akshare_*.csv`（已在 `.gitignore`）

---

## 三、启用并试跑

1. 仓库 → **Actions** → 若提示启用 workflows，点确认
2. 左侧应看到两个工作流：
   - **HS300 每日选股推送**
   - **单股指标每日推送**
3. **分别手动 Run workflow 各一次**，确认微信收到两条不同推送

### 成功标志

| 工作流 | 微信标题示例 | 日志关键字 |
|--------|--------------|------------|
| HS300 | `HS300多因子 Top3 2026-06-14（A股交易日）` | `Server酱推送成功。` |
| 单股 | `单股指标日报 2026-06-14` | `已推送到微信（Server酱）` |

### Artifacts

每次成功运行可在 Actions 详情页下载：

- HS300：`hs300_screen_*.csv`
- 单股：`single_stock_score_*.xlsx`

---

## 四、定时说明

| 工作流 | cron (UTC) | 北京时间 | 预计耗时 |
|--------|------------|----------|----------|
| HS300 | `0 16 * * *` | 00:00 | 30～90 分钟 |
| 单股 | `0 12 * * *` | 20:00 | 15～25 分钟 |

单股任务安排在 **20:00**，与 HS300（00:00）错开，避免同时大量请求东财/百度接口导致限流。

修改时间：编辑对应 `.github/workflows/*.yml` 中的 `cron`（UTC = 北京时间 − 8）。

---

## 五、本地对照

`.env` 见 `.env.example`（填 `SERVERCHAN_SENDKEY`）。

```powershell
# HS300：仅打印推送，不发送
python strategies/run_hs300_akshare.py --screen --push --dry-run --trading-day-only

# HS300：真实推送
python strategies/run_hs300_akshare.py --screen --push --trading-day-only

# 单股：仅打印
python daily_push_serverchan.py --dry-run

# 单股：真实推送
python daily_push_serverchan.py --trading-day-only
```

---

## 六、常见问题

### 没收到推送

- Secret 名称必须为 **`SERVERCHAN_SENDKEY`**（区分大小写）
- Server酱 是否仍绑定微信
- Actions 日志是否报错；失败时会另发一条「Actions 失败」通知（若 Secret 已配置）

### Fork 仓库 schedule 不跑

Fork 后默认定时任务禁用；在自己账号下 **新建仓库** 并 push，或在 Actions 页手动启用。

### Actions 额度

免费账户每月有分钟数限制；两个任务各约 1 次/交易日，一般足够。

### 只想保留其中一个

Actions → 选中不需要的工作流 → 右上角 **⋯** → **Disable workflow**

---

## 七、安全提醒

- **SendKey 是密钥**，只用 GitHub Secrets，勿写入代码或 `.env` 并提交
- 仓库 Public 时，代码公开但 Secret 不会泄露

完成以上步骤后，每个交易日你会收到 **两条** 微信推送：**00:00** HS300 Top3 + **20:00** 持仓/关注股指标日报。
