# AI 热点日报 · 每日自动推送

每天北京时间 **08:30** 自动抓取 [AI HOT](https://aihot.virxact.com) 过去 24 小时的精选热点 + 当前最热话题 Top3，汇总成日报推送到**企业微信群**。每条都带原始来源链接。跑在 GitHub Actions 上，电脑关机照常推送。

## 特点

- **零依赖**：纯 Python 标准库，CI 里不用装任何包
- **免大模型**：AI HOT 的摘要已是成稿中文（实测 91–197 字，零缺失），直接用，不烧 token
- **自动去重**：记录已推 id，重跑不会重复轰炸
- **企业微信通道**：通过群机器人 Webhook 推送，无需个人微信开放接口

## 快速开始

### 1. 拿到企业微信群机器人 Webhook

企业微信 → 进入一个**企业内部群** → 右上角 `···` → 「群机器人」→ 添加机器人 → 复制 Webhook 地址。

形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-...`

> ⚠️ 这个地址等同密码，谁拿到都能往你群里发消息。**只放进 GitHub Secrets，不要贴到任何公开地方。**

### 2. 建仓库并配置

1. 把本目录内容推到 GitHub 私有仓库
2. 仓库 → Settings → Secrets and variables → Actions
   - **Secrets** → New repository secret：`WECOM_WEBHOOK` = 你的 Webhook 整条地址
   - **Variables** 不用动（workflow 默认就是 `wecom`）

### 3. 跑一次试试

仓库 → Actions → 「AI 热点日报」→ Run workflow。

看到企业微信群里收到消息就成了。之后每天 08:30 自动推送。

## 本地调试

```bash
# 只渲染打印，不发送
python3 scripts/aihot_daily.py --dry-run

# 忽略去重，强制全量渲染（不会写入去重状态）
python3 scripts/aihot_daily.py --dry-run --no-dedup

# 本地实发（先设好环境变量）
export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
python3 scripts/aihot_daily.py
```

## 常见调整

**改推送时间** — 编辑 `.github/workflows/daily.yml` 的 cron（**UTC 时间**，北京时间减 8 小时）：

```yaml
- cron: "30 0 * * *"   # 北京 08:30（默认）
- cron: "30 4 * * *"   # 北京 12:30
- cron: "30 13 * * *"  # 北京 21:30
```

> 刻意用 30 分而非整点：GitHub 定时任务在整点是高峰，会延迟，极端负载下排队任务甚至被丢弃。

**控制拆分份数上限** — 日报按当日内容数据量**动态**决定拆成几片发送（不写死），但总份数有硬上限。通过环境变量 `MAX_CHUNKS` 调整，取值会被强制夹取到 **[1, 5]**，无论设多大都不会超过 5 片：

```bash
MAX_CHUNKS=5   # 默认，最多 5 片（也是硬上限）
MAX_CHUNKS=3   # 最多 3 片
MAX_CHUNKS=9   # 会被夹取到 5，不会真的拆成 9 片
```

份数由内容体积自动推算（每片尽量压在 3500 字节以内），所以"内容少 → 1~2 片，内容多 → 3~5 片"，你只设上限、不用管具体几片。

**改内容条数/口径** — 编辑 `scripts/aihot_daily.py`：

- `fetch_items()` 里的 `window=24h` 可改 `7d`
- `fetch_hot_topics(3)` 改数字调整热榜条数
- `CATEGORY_ORDER` 调整分类展示顺序

## 实现上绕开的几个坑

1. **企微 markdown 上限是 4096 字节（不是字符）**，中文按 3 字节算。13 条内容约 7100 字节必然超限，所以按 3500 字节贪心分片（动态 1~5 片）。
2. **超长时企微返回 `errcode:0` 但消息不发出**（errmsg 带 `Warning: wrong json format.`），是个静默陷阱。代码显式校验 errmsg 才判定成功。
3. **上游返回 0 条时主动失败**并发告警，避免"每天绿灯但群里没消息"。而"抓到了但都推过"属正常，安静跳过不报错。
4. **只对网络异常重试**（1s/3s/9s 退避），业务错误立即抛出，不做无谓重试。
5. **状态文件只留最近 500 条 id**，防止仓库无限膨胀。
6. **用内置 `GITHUB_TOKEN` 而非 PAT** 回写状态：它产生的 push 不会再触发 workflow，天然防递归。

## 费用

私有仓库 GitHub Free 每月 2000 分钟。每天 1 次 × 约 1 分钟 ≈ 31 分钟/月，用掉 1.5%。

## 目录

```
scripts/aihot_daily.py        # 主脚本（企业微信单通道）
.github/workflows/daily.yml   # 定时任务
state/pushed_ids.json         # 去重记录（自动维护）
```

## 数据来源

内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者。
