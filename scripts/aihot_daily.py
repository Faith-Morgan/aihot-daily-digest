#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI HOT 每日热点 -> 汇总 -> 推送到微信（企业微信群机器人 / PushPlus / Server酱）

设计要点：
- 只用 Python 标准库，零第三方依赖（CI 里无需 pip install）
- 数据源 AI HOT v1 公开 API，匿名只读
- 每条热点均携带原始来源链接（links.original）
- 企业微信 markdown 上限 4096 字节（不是字符），必须按字节分片
- 企微超长时会返回 errcode=0 但 errmsg 含 "Warning: wrong json format."，
  属于"伪成功"，消息实际没发出去，必须显式校验

用法：
    python3 aihot_daily.py --dry-run     # 只渲染打印，不发送
    python3 aihot_daily.py               # 渲染并推送
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 常量

BASE = "https://aihot.virxact.com"
UA = "aihot-skill/1.2.1 (+https://aihot.virxact.com/aihot-skill/)"

# 中国自 1991 年起不使用夏令时，UTC+8 恒定。
# 用固定偏移而非 zoneinfo，避免精简镜像缺 tzdata 的坑。
CST = timezone(timedelta(hours=8))

# 企微 markdown 硬上限 4096 字节。留足余量，避免踩"伪成功"静默失败。
CHUNK_LIMIT = 3500

# 去重状态最多保留的条目数，防止文件无限增长
MAX_STATE_IDS = 500

# 最大拆分份数（硬上限：无论怎么配置都绝不超出 5 份）。
# 可通过环境变量 MAX_CHUNKS 覆盖，但值会被强制夹取到 [1, 5] 区间，
# 防止误配置把日报拆成几十片、触发各通道限流或超长。
def _resolve_max_chunks() -> int:
    try:
        raw = int(os.environ.get("MAX_CHUNKS", "5"))
    except (TypeError, ValueError):
        raw = 5
    return max(1, min(raw, 5))


MAX_CHUNKS = _resolve_max_chunks()

HTTP_TIMEOUT = 20

CATEGORY_CN = {
    "ai-models": "模型进展",
    "ai-products": "产品动态",
    "industry": "行业观察",
    "paper": "论文研究",
    "tip": "技巧与观点",
}
# 日报里的分类展示顺序
CATEGORY_ORDER = ["ai-models", "ai-products", "industry", "paper", "tip"]

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "state" / "pushed_ids.json"


# ---------------------------------------------------------------- 工具


def log(msg: str) -> None:
    """输出到 stderr，避免污染 stdout（dry-run 时 stdout 是日报正文）。"""
    print(msg, file=sys.stderr, flush=True)


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bytelen(s: str) -> int:
    """企微按 UTF-8 字节计长度，中文 3 字节/字。"""
    return len(s.encode("utf-8"))


def to_cst(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        # API 返回形如 2026-08-08T14:58:57.000Z
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CST)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- 抓取


def fetch_items() -> list[dict]:
    """过去 24 小时的精选条目。"""
    url = f"{BASE}/api/v1/items?mode=selected&window=24h&limit=50"
    data = http_get_json(url)
    items = data.get("items") or []
    log(f"[fetch] 24h 精选 {len(items)} 条")
    return items


def fetch_hot_topics(top_n: int = 3) -> list[dict]:
    """当前最热话题。失败不致命——热榜是锦上添花，主体是精选条目。"""
    try:
        data = http_get_json(f"{BASE}/api/v1/hot-topics")
        topics = (data.get("items") or [])[:top_n]
        log(f"[fetch] 热榜 {len(topics)} 条")
        return topics
    except Exception as e:  # noqa: BLE001
        log(f"[warn] 热榜抓取失败，跳过该板块: {e}")
        return []


# ---------------------------------------------------------------- 去重状态


def load_pushed_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as e:
        log(f"[warn] 状态文件损坏，按空处理: {e}")
        return set()


def save_pushed_ids(old: set[str], new_ids: list[str]) -> None:
    """保留顺序、去重，只留最近 MAX_STATE_IDS 条。"""
    merged: list[str] = []
    seen: set[str] = set()
    for i in list(old) + new_ids:
        if i not in seen:
            seen.add(i)
            merged.append(i)
    merged = merged[-MAX_STATE_IDS:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    log(f"[state] 已记录 {len(merged)} 个 id")


# ---------------------------------------------------------------- 渲染

# 注意：企微 markdown 仅支持 标题/加粗/链接/行内代码/引用/font color
# 不支持：表格、图片、斜体、分割线 ---、有序列表


def render_item(it: dict) -> str:
    title = (it.get("title") or "无标题").strip()
    summary = (it.get("summary") or "").strip()
    source = ((it.get("source") or {}).get("name") or "未知来源").strip()
    links = it.get("links") or {}
    aihot = links.get("aihot") or ""
    original = links.get("original") or ""

    # publishedAt 是原文发布时间；为空时回退 discoveredAt 并如实标注口径
    dt = to_cst(it.get("publishedAt"))
    if dt:
        tlabel = dt.strftime("%m-%d %H:%M")
    else:
        dt2 = to_cst(it.get("discoveredAt"))
        tlabel = f"{dt2.strftime('%m-%d %H:%M')} 收录" if dt2 else "时间未知"

    head = f"**[{title}]({aihot})**" if aihot else f"**{title}**"
    parts = [head]
    if summary:
        # 摘要内部可能自带换行；引用块每行都要有 "> "，否则渲染会断开
        quoted = "\n".join(
            f"> {ln.strip()}" for ln in summary.splitlines() if ln.strip()
        )
        parts.append(quoted)

    meta = f"<font color=\"comment\">{source} · {tlabel}</font>"
    if original:
        meta += f" [原文]({original})"
    parts.append(meta)

    return "\n".join(parts)


def render_hot_topics(topics: list[dict]) -> str:
    if not topics:
        return ""
    lines = ["**🔥 当前最热**"]
    for t in topics:
        title = (t.get("title") or "").strip()
        links = t.get("links") or {}
        url = links.get("aihot") or links.get("original") or ""
        cnt = t.get("sourceCount")
        tail = f" <font color=\"comment\">{cnt} 家报道</font>" if cnt else ""
        lines.append(f"{t.get('rank', '')}. [{title}]({url}){tail}" if url else f"{t.get('rank','')}. {title}{tail}")
    return "\n".join(lines)


def build_blocks(items: list[dict], topics: list[dict]) -> tuple[str, list[str]]:
    """返回 (标题行, 内容块列表)。块是分片的最小单位，不会被切断。"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    header = f"# AI 热点日报 {today}"

    blocks: list[str] = []

    hot = render_hot_topics(topics)
    if hot:
        blocks.append(hot)

    # 按预设顺序分组，未知分类兜底到末尾
    grouped: dict[str, list[dict]] = {}
    for it in items:
        grouped.setdefault(it.get("category") or "other", []).append(it)

    ordered = [c for c in CATEGORY_ORDER if c in grouped]
    ordered += [c for c in grouped if c not in CATEGORY_ORDER]

    for cat in ordered:
        group = grouped[cat]
        cn = CATEGORY_CN.get(cat, "其他")
        section = f"**{cn}**（{len(group)} 条）"
        # 分组标题与首条绑成一个块，避免分片时标题独自留在上一片的末尾
        rendered = [render_item(it) for it in group]
        blocks.append(f"{section}\n\n{rendered[0]}")
        blocks.extend(rendered[1:])

    return header, blocks


def _padded_header_len(header: str) -> int:
    """标题行 + 页脚 "（x/y）" 占位的字节开销，供分片预算估算。"""
    return bytelen(header) + 60


def pack_chunks(header: str, blocks: list[str], limit: int = CHUNK_LIMIT,
                 max_chunks: int = MAX_CHUNKS) -> list[str]:
    """动态分片：份数由当日内容的数据量决定，但总份数不超过 max_chunks（默认 5）。

    规则：
    1. 按字节估算"最少需要几份"才能把每片压在平台单边上限 limit 内；
    2. 实际份数 = min(需要份数, max_chunks) —— 绝不写死为 1/2/3；
    3. 用均衡切分把块分配到这些份里，保持原顺序、绝不切断任何单条块；
    4. 兜底：若因内容过大被 max_chunks 上限夹取后段数仍偏多，尾部自动合并
       收敛到 max_chunks 份以内（此时单份可能超过 limit，属用户设定的硬上限场景）。
    """
    if not blocks:
        return []

    # 1) 估算需要的份数（含标题/页脚字节开销）
    overhead = _padded_header_len(header)
    content_bytes = sum(bytelen(b) + 2 for b in blocks)
    needed = max(1, -(-(overhead + content_bytes) // limit))  # ceil 除法
    count = min(needed, max_chunks)

    # 2) 将块均衡切成 count 段（按累积字节体积封口，保持原顺序）
    sizes = [bytelen(b) + 2 for b in blocks]
    total = sum(sizes)
    target = total / count  # 每段目标字节
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_size = 0
    remaining = count
    for i, b in enumerate(blocks):
        cur.append(b)
        cur_size += sizes[i]
        blocks_left = len(blocks) - (i + 1)
        # 当前段达到平均目标、且后续还有足够块填满剩余段 -> 封口；
        # remaining == 1 时（最后一段）不提前封口，由循环后的兜底收尾
        if remaining > 1 and cur_size >= target and blocks_left >= (remaining - 1):
            chunks.append(cur)
            cur, cur_size = [], 0
            remaining -= 1
    if cur:
        chunks.append(cur)

    # 3) 兜底合并，确保绝不超过 max_chunks 份
    while len(chunks) > max_chunks:
        last = chunks.pop()
        if chunks:
            chunks[-1] = chunks[-1] + last
        else:
            chunks.append(last)

    total_chunks = len(chunks)
    out = []
    for idx, blk in enumerate(chunks, 1):
        suffix = f"（{idx}/{total_chunks}）" if total_chunks > 1 else ""
        body = "\n\n".join(blk)
        out.append(f"{header}{suffix}\n\n{body}")
    return out


# ---------------------------------------------------------------- 推送


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_wecom(webhook: str, content: str, msgtype: str = "markdown") -> None:
    """企业微信群机器人。仅对网络异常重试，业务错误立即抛出。"""
    payload = (
        {"msgtype": "markdown", "markdown": {"content": content}}
        if msgtype == "markdown"
        else {"msgtype": "text", "text": {"content": content}}
    )

    last_err: Exception | None = None
    for attempt, delay in enumerate([1, 3, 9], 1):
        try:
            r = _post_json(webhook, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log(f"[retry] 网络异常，第 {attempt} 次退避 {delay}s: {e}")
            time.sleep(delay)
            continue

        errcode = r.get("errcode")
        errmsg = str(r.get("errmsg", ""))
        # 关键：errcode=0 但 errmsg 带 warning 是"伪成功"，消息并未送达
        if errcode == 0 and "warning" not in errmsg.lower():
            return
        raise RuntimeError(f"企微拒绝: errcode={errcode} errmsg={errmsg}")

    raise RuntimeError(f"网络重试耗尽: {last_err}")


def dispatch(chunks: list[str], title: str) -> None:
    """推送当日日报到企业微信群机器人（唯一通道）。"""
    channel = (os.environ.get("PUSH_CHANNEL") or "wecom").strip().lower()
    if channel != "wecom":
        log(f"[warn] 仅支持企业微信通道，忽略 PUSH_CHANNEL={channel!r}")

    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not hook:
        raise RuntimeError("缺少环境变量 WECOM_WEBHOOK")
    for i, c in enumerate(chunks, 1):
        send_wecom(hook, c)
        log(f"[push] 企微第 {i}/{len(chunks)} 片已发送（{bytelen(c)} 字节）")
        if i < len(chunks):
            time.sleep(1)  # 保证群内消息顺序，限流 20 条/分钟无压力


def alert_failure(err: str) -> None:
    """失败时尽力发一条短告警，避免任务静默挂掉。"""
    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not hook:
        return
    try:
        send_wecom(hook, f"AI 热点日报任务失败：{err[:300]}", msgtype="text")
        log("[alert] 已发送失败告警")
    except Exception as e:  # noqa: BLE001
        log(f"[alert] 告警也失败了: {e}")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="AI HOT 每日热点推送")
    ap.add_argument("--dry-run", action="store_true", help="只渲染打印，不发送")
    ap.add_argument("--no-dedup", action="store_true", help="忽略去重状态，全量渲染")
    args = ap.parse_args()

    items = fetch_items()
    raw_count = len(items)

    if not args.no_dedup:
        pushed = load_pushed_ids()
        before = len(items)
        items = [i for i in items if i.get("id") not in pushed]
        if before != len(items):
            log(f"[dedup] 过滤掉 {before - len(items)} 条已推送")

    if not items:
        # 区分两种情况：
        # - 上游真的返回空 => 抓取链路可能坏了，必须报错，否则每天绿灯但群里没消息
        # - 抓到了但都已推过 => 正常状态（例如手动重跑），安静退出即可
        if raw_count == 0:
            raise RuntimeError("上游返回 0 条，抓取链路可能异常")
        log("[skip] 本次无新增条目（均已推送过），跳过")
        return 0

    topics = fetch_hot_topics(3)
    header, blocks = build_blocks(items, topics)
    chunks = pack_chunks(header, blocks)

    log(f"[render] {len(items)} 条 -> {len(chunks)} 片")
    for i, c in enumerate(chunks, 1):
        size = bytelen(c)
        flag = "OK" if size < 4096 else "超限!"
        log(f"  片{i}: {size} 字节 [{flag}]")
        if size >= 4096:
            raise RuntimeError(f"第 {i} 片 {size} 字节，超出企微 4096 上限")

    if args.dry_run:
        print("\n\n" + ("=" * 60) + "\n\n".join(f"\n【片 {i}】\n{c}" for i, c in enumerate(chunks, 1)))
        return 0

    title = f"AI 热点日报 {datetime.now(CST).strftime('%Y-%m-%d')}"
    dispatch(chunks, title)

    if not args.no_dedup:
        save_pushed_ids(load_pushed_ids(), [i["id"] for i in items if i.get("id")])

    log("[done] 推送完成")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"[fatal] {exc}")
        if not any(a in sys.argv for a in ("--dry-run",)):
            alert_failure(str(exc))
        sys.exit(1)
