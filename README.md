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

1. 把本目录内容推到 GitHub 仓库（公开或私有均可）
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

**自动分片** — 日报按 UTF-8 字节数打包，每片连同标题、页码和空行不超过 **3500 字节**，保留全部条目、摘要和链接，片数由实际内容决定，允许超过 5 片。旧的 `MAX_CHUNKS` 环境变量已不再使用。

发送片与片之间至少间隔 3 秒，遵守企微机器人每分钟 20 条的限流。单个完整内容块（包括分类标题或热榜）若已超出正文预算，脚本会在发送前明确报错，不截断内容，也不记录为已推送。

离线回归测试：`python3 -m unittest discover -s tests -v`。

**改内容条数/口径** — 编辑 `scripts/aihot_daily.py`：

- `fetch_items()` 里的 `window=24h` 可改 `7d`
- `fetch_hot_topics(3)` 改数字调整热榜条数
- `CATEGORY_ORDER` 调整分类展示顺序

## 实现上绕开的几个坑

1. **企微 markdown 上限是 4096 字节（不是字符）**，中文按 3 字节算。按完整消息 3500 字节的预算贪心分片；加入下一条前检查容量，片数可超过 5，避免内容多时合并出超限消息。
2. **超长时企微返回 `errcode:0` 但消息不发出**（errmsg 带 `Warning: wrong json format.`），是个静默陷阱。代码显式校验 errmsg 才判定成功。
3. **上游返回 0 条时主动失败**并发告警，避免"每天绿灯但群里没消息"。而"抓到了但都推过"属正常，安静跳过不报错。
4. **只对网络异常重试**（1s/3s/9s 退避），业务错误立即抛出，不做无谓重试。
5. **状态文件只留最近 500 条 id**，防止仓库无限膨胀。
6. **用内置 `GITHUB_TOKEN` 而非 PAT** 回写状态：它产生的 push 不会再触发 workflow，天然防递归。

## 费用

GitHub Actions 对**公开仓库免费且不限时长**；私有仓库 Free 套餐每月 2000 分钟。本任务每天 1 次 × 约 1 分钟，无论公私都几乎无成本。

## 开源协议

本项目以 [MIT License](./LICENSE) 发布。日报内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者所有；转载或二次分发请遵守各来源站点协议。

## 目录

```
scripts/aihot_daily.py        # 主脚本（企业微信单通道）
.github/workflows/daily.yml   # 定时任务
state/pushed_ids.json         # 去重记录（自动维护）
```

## 数据来源

内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者。
