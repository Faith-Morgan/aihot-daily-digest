# AI 热点日报 · 每日自动推送

每天北京时间 **08:27** 自动抓取 [AI HOT](https://aihot.virxact.com) 过去 24 小时的精选热点 + 当前最热话题 Top3，汇总成日报推送到微信。每条都带原始来源链接。跑在 GitHub Actions 上，电脑关机照常推送。

## 特点

- **零依赖**：纯 Python 标准库，CI 里不用装任何包
- **免大模型**：AI HOT 的摘要已是成稿中文（实测 91–197 字，零缺失），直接用，不烧 token
- **自动去重**：记录已推 id，重跑不会重复轰炸
- **可换通道**：企业微信 / PushPlus / Server酱，改一个环境变量即可切换

## 快速开始

### 1. 拿到 Server酱 SendKey

打开 [sct.ftqq.com](https://sct.ftqq.com) → 微信扫码登录 → 复制页面上的 **SendKey**（形如 `SCTxxxx...`）。

> ⚠️ SendKey 等同密码。**只放进 GitHub Secrets，不要贴到任何公开地方。**

### 2. 建仓库并配置

1. 把本目录内容推到 GitHub 私有仓库
2. 仓库 → Settings → Secrets and variables → Actions
   - **Secrets** → New repository secret：`SERVERCHAN_SENDKEY` = 你的 SendKey
   - **Variables**（可选，已默认）→ `PUSH_CHANNEL` = `serverchan`（workflow 已内置默认，不填也行）

### 3. 跑一次试试

仓库 → Actions → 「AI 热点日报」→ Run workflow。

看到微信收到 3 条消息就成了。之后每天 08:27 自动推送。

## 本地调试

```bash
# 只渲染打印，不发送
python3 scripts/aihot_daily.py --dry-run

# 忽略去重，强制全量渲染
python3 scripts/aihot_daily.py --dry-run --no-dedup

# 本地实发（先设好环境变量）
export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
python3 scripts/aihot_daily.py
```

## 常见调整

**改推送时间** — 编辑 `.github/workflows/daily.yml` 的 cron（**UTC 时间**，北京时间减 8 小时）：

```yaml
- cron: "27 0 * * *"   # 北京 08:27（默认）
- cron: "27 4 * * *"   # 北京 12:27
- cron: "27 13 * * *"  # 北京 21:27
```

> 刻意用 27 分而非整点：GitHub 定时任务在整点是高峰，会延迟，极端负载下排队任务甚至被丢弃。

**换推送通道** — 仓库 Settings → Variables 新增 `PUSH_CHANNEL`：

| 值 | 通道 | 需要的 Secret | 说明 |
|---|---|---|---|
| `wecom` | 企业微信群机器人 | `WECOM_WEBHOOK` | 消息落在企业微信 App |
| `pushplus` | PushPlus | `PUSHPLUS_TOKEN` | **能推到个人微信**，免费 200 条/天 |
| `serverchan`（默认） | Server酱³ | `SERVERCHAN_SENDKEY` | 能推到个人微信，免费 5 条/天；长日报自动拆成多条消息 |

> 想真正推到**个人微信**就用 `pushplus`：去 [pushplus.plus](https://www.pushplus.plus) 微信扫码登录拿 token，配好 Secret 再把 `PUSH_CHANNEL` 设成 `pushplus` 即可，代码已经写好了。

**改内容条数/口径** — 编辑 `scripts/aihot_daily.py`：

- `fetch_items()` 里的 `window=24h` 可改 `7d`
- `fetch_hot_topics(3)` 改数字调整热榜条数
- `CATEGORY_ORDER` 调整分类展示顺序

## 实现上绕开的几个坑

1. **企微 markdown 上限是 4096 字节（不是字符）**，中文按 3 字节算。13 条内容约 7100 字节必然超限，所以按 3500 字节贪心分片（当前 3 片）。
2. **超长时企微返回 `errcode:0` 但消息不发出**（errmsg 带 `Warning: wrong json format.`），是个静默陷阱。代码显式校验 errmsg 才判定成功。
3. **上游返回 0 条时主动失败**并发告警，避免"每天绿灯但群里没消息"。而"抓到了但都推过"属正常，安静跳过不报错。
4. **只对网络异常重试**（1s/3s/9s 退避），业务错误立即抛出，不做无谓重试。
5. **状态文件只留最近 500 条 id**，防止仓库无限膨胀。
6. **用内置 `GITHUB_TOKEN` 而非 PAT** 回写状态：它产生的 push 不会再触发 workflow，天然防递归。
7. **Server酱免费版单条长消息在微信卡片里会被截断预览**（只显示标题、正文需点开），所以长日报拆成多条独立消息发送，每一"片"都独立到达、互不截断。这也是为什么你会收到 3 条带（1/3）（2/3）（3/3）的消息。

## 费用

私有仓库 GitHub Free 每月 2000 分钟。每天 1 次 × 约 1 分钟 ≈ 31 分钟/月，用掉 1.5%。

## 目录

```
scripts/aihot_daily.py        # 主脚本
.github/workflows/daily.yml   # 定时任务
state/pushed_ids.json         # 去重记录（自动维护）
```

## 数据来源

内容来自 [AI HOT](https://aihot.virxact.com)，第三方原文版权归原作者。
