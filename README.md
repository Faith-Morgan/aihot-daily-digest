# AI 热点日报 · 每日自动推送

目标为每天北京时间 **08:30** 自动抓取 [AI HOT](https://aihot.virxact.com) 过去 24 小时的精选热点 + 当前最热话题 Top3，汇总成日报推送到**企业微信群**。每条都带原始来源链接。GitHub Actions 的定时事件可能延迟；需要在 **08:25—08:35** 内完成时，使用[独立服务器部署](deploy/README.md)。两种方式均不依赖个人电脑开机。

## 特点

- **零依赖**：纯 Python 标准库，CI 里不用装任何包
- **免大模型**：AI HOT 的摘要已是成稿中文（实测 91–197 字，零缺失），直接用，不烧 token
- **进度恢复**：保存固定日报、内容哈希及逐片确认进度；重跑继续明确未成功的分片，送达结果不明则暂停核对
- **企业微信通道**：通过群机器人 Webhook 推送，无需个人微信开放接口

## 快速开始

### 1. 拿到企业微信群机器人 Webhook

企业微信 → 进入一个**企业内部群** → 右上角 `···` → 「群机器人」→ 添加机器人 → 复制 Webhook 地址。

形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-...`

> 这个地址等同密码，谁拿到都能往你群里发消息。只保存在 GitHub Secrets 或独立服务器受限的环境文件中，不要提交到仓库。

### 2. 建仓库并配置

1. 把本目录内容推到 GitHub 仓库（公开或私有均可）
2. 仓库 → Settings → Secrets and variables → Actions
   - **Secrets** → New repository secret：`WECOM_WEBHOOK` = 你的 Webhook 整条地址
   - **Variables** 不用动（workflow 默认就是 `wecom`）

### 3. 跑一次试试

仓库 → Actions → 「AI 热点日报」→ Run workflow。

确认企业微信群收到全部分片。之后 GitHub 会按 08:30 的目标时间调度，但不保证准时。脚本同时保存 `state/delivery.json` 作为发送进度和完成时间记录；每个业务日期最多完成一次。

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
4. **有限重试与不明结果处理**：GET 的瞬时故障最多尝试 3 次，间隔 1s/3s；HTTP 429 尊重 Retry-After（等待超过 60s 则停止本次重试）。永久错误立即失败。POST 只重试连接建立前的明确失败和 HTTP 429；超时、5xx 或无效响应可能已经送达，保存待核对状态并停止自动重发。
5. **状态文件只留最近 500 条 id**，防止仓库无限膨胀。
6. **每片前后持久化**：Actions 使用内置 `GITHUB_TOKEN` 回写进度，每次回写失败都会阻止后续发送；独立服务器使用持久磁盘和进程锁。进度或去重记录损坏时停止发送，避免把损坏状态当作从未推送。

### 发送中断后的处理

明确拒绝的分片可在排除原因后重跑，已确认分片会跳过。若提示“结果待核对”，先查看 `state/delivery.json` 的 `inflight`（从 0 开始）及对应 `chunks`，在目标群核对该片，再使用以下任一命令。命令只更新进度，不发送消息：

```bash
python3 scripts/aihot_daily.py --resolve-inflight sent   # 确认已收到
python3 scripts/aihot_daily.py --resolve-inflight retry  # 确认未收到
```

Actions 模式需要在可回写默认分支的受控运行环境中设置 `AIHOT_PERSIST_GIT=1` 和 `AIHOT_GIT_BRANCH`，确保人工确认也写回远程；独立服务器的处理方式见部署说明。无法判断是否收到时保持暂停，不要删除进度文件。

只核对今日完成时间，不发送日报：`python3 scripts/aihot_daily.py --check`。检查未通过时，命令返回失败，正常入口会尽力发送失败告警。GET 重试耗尽退出码为 1；永久错误、损坏状态或送达不明为 2，独立服务不自动重启这些情况。

## 费用

GitHub Actions 用量按当前账户与平台计费规则计算；逐片进度持久化会增加 Git 操作。独立服务器需要已有常在线主机，不由本仓库自动购买或开通。

## 开源协议

本项目以 [MIT License](./LICENSE) 发布。日报内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者所有；转载或二次分发请遵守各来源站点协议。

## 目录

```
scripts/aihot_daily.py        # 主脚本（企业微信单通道）
.github/workflows/daily.yml   # 定时任务
.github/workflows/verify.yml  # 无密钥的离线回归验证
state/pushed_ids.json         # 去重记录（自动维护）
state/delivery.json           # 固定日报、逐片进度及完成回执（首次运行创建）
deploy/                      # 独立运行及准时验收说明
```

## 数据来源

内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者。
