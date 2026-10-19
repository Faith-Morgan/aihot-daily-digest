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
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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

HTTP_TIMEOUT = 20
RETRY_DELAYS = (1, 3)

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
STATE_DIR = Path(os.environ.get("AIHOT_STATE_DIR") or REPO_ROOT / "state")
STATE_FILE = STATE_DIR / "pushed_ids.json"
DELIVERY_FILE = STATE_DIR / "delivery.json"


class TransientFetchError(RuntimeError):
    """GET 可安全重试，但本次预算已耗尽。"""


class DeliveryUncertain(RuntimeError):
    """POST 可能已送达，禁止自动重发。"""


# ---------------------------------------------------------------- 工具


def log(msg: str) -> None:
    """输出到 stderr，避免污染 stdout（dry-run 时 stdout 是日报正文）。"""
    print(msg, file=sys.stderr, flush=True)


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f"抓取被拒绝: HTTP {exc.code}") from None
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc
        if attempt == 2:
            raise TransientFetchError("抓取瞬时故障，3 次尝试已耗尽") from None
        delay = retry_delay(error, attempt)
        log(f"[retry] 抓取第 {attempt + 1} 次失败，{delay}s 后重试")
        time.sleep(delay)


def retry_delay(error: Exception, attempt: int) -> float:
    delay = RETRY_DELAYS[attempt]
    if isinstance(error, urllib.error.HTTPError):
        value = error.headers.get("Retry-After", "") if error.headers else ""
        try:
            requested = float(value)
        except ValueError:
            try:
                requested = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                return delay
        if requested > 60:
            raise RuntimeError("服务端要求等待超过 60 秒，停止本次重试，请稍后再运行")
        return max(delay, requested)
    return delay


def write_json(path: Path, data: object) -> None:
    """同目录原子替换；崩溃时保留旧文件，避免读到半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def load_delivery() -> dict | None:
    if not DELIVERY_FILE.exists():
        return None
    try:
        plan = json.loads(DELIVERY_FILE.read_text(encoding="utf-8"))
        chunks = plan["chunks"]
        hashes = [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in chunks]
        valid = (
            plan["version"] == 1 and isinstance(chunks, list)
            and all(isinstance(c, str) for c in chunks)
            and hashes == plan["sha256"]
            and type(plan["confirmed"]) is int
            and 0 <= plan["confirmed"] <= len(chunks)
            and (plan["inflight"] is None or (
                type(plan["inflight"]) is int
                and plan["inflight"] == plan["confirmed"] < len(chunks)))
            and isinstance(plan["header"], str)
            and isinstance(plan["ids"], list)
            and all(isinstance(i, str) for i in plan["ids"])
            and datetime.strptime(plan["date"], "%Y-%m-%d")
            and (plan["completed_at"] is None or (
                plan["confirmed"] == len(chunks) and plan["inflight"] is None
                and to_cst(plan["completed_at"]) is not None))
        )
        if not valid:
            raise ValueError("invalid delivery state")
        return plan
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        raise RuntimeError("发送进度文件损坏，停止发送；请核对并恢复状态") from None


def persist_git_state() -> None:
    """Actions 中在每次 POST 前后远程确认状态；独立服务器使用本地持久盘。"""
    if os.environ.get("AIHOT_PERSIST_GIT") != "1":
        return
    branch = os.environ.get("AIHOT_GIT_BRANCH", "")
    if not branch or STATE_DIR.resolve() != (REPO_ROOT / "state").resolve():
        raise RuntimeError("Git 状态回写配置无效")

    def git(*args: str) -> int:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, check=False,
        ).returncode

    if git("add", "--", "state/pushed_ids.json", "state/delivery.json"):
        raise RuntimeError("无法暂存发送进度，停止发送")
    changed = git("diff", "--staged", "--quiet")
    if changed not in (0, 1) or (changed == 1 and git(
        "commit", "-m", "chore: 保存日报发送进度 [skip ci]",
    )):
        raise RuntimeError("无法提交发送进度，停止发送")
    for attempt in range(3):
        if git("push", "origin", f"HEAD:refs/heads/{branch}") == 0:
            return
        if attempt < 2:
            time.sleep(RETRY_DELAYS[attempt])
    raise RuntimeError("发送进度远程回写失败，停止发送；不得强制推送或跳过检查点")


def save_delivery(plan: dict) -> None:
    write_json(DELIVERY_FILE, plan)
    persist_git_state()


def new_delivery(header: str, chunks: list[str], ids: list[str]) -> dict:
    return {
        "version": 1, "date": datetime.now(CST).strftime("%Y-%m-%d"),
        "header": header, "chunks": chunks, "ids": ids,
        "sha256": [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in chunks],
        "confirmed": 0, "inflight": None, "completed_at": None,
        "run_url": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                    f"{os.environ['GITHUB_RUN_ID']}" if os.environ.get("GITHUB_RUN_ID")
                    and os.environ.get("GITHUB_REPOSITORY") else None),
    }


def finish_delivery(plan: dict) -> None:
    if plan["confirmed"] != len(plan["chunks"]) or plan["inflight"] is not None:
        raise RuntimeError("仍有未确认分片，不能标记推送完成")
    if plan["ids"]:
        save_pushed_ids(load_pushed_ids(), plan["ids"])
    plan["completed_at"] = datetime.now(CST).isoformat(timespec="seconds")
    save_delivery(plan)
    log(f"[done] {plan['date']} 推送完成，共 {len(plan['chunks'])} 片")


def check_delivery(now: datetime | None = None) -> None:
    now = now or datetime.now(CST)
    plan = load_delivery()
    if not plan or plan["date"] != now.strftime("%Y-%m-%d") or not plan["completed_at"]:
        raise RuntimeError("今日日报尚未完成，请检查运行与发送进度")
    completed = to_cst(plan["completed_at"])
    start = now.replace(hour=8, minute=25, second=0, microsecond=0)
    end = now.replace(hour=8, minute=35, second=0, microsecond=0)
    if not start <= completed <= end:
        raise RuntimeError(f"今日日报完成时间 {completed:%H:%M:%S} 不在 08:25—08:35 窗口")
    log(f"[check] 今日完成时间 {completed:%H:%M:%S}，符合时间窗口")


def ensure_delivery_window() -> None:
    if os.environ.get("AIHOT_ENFORCE_WINDOW") != "1":
        return
    now = datetime.now(CST)
    if not now.replace(hour=8, minute=25, second=0, microsecond=0) <= now <= now.replace(
        hour=8, minute=35, second=0, microsecond=0,
    ):
        raise RuntimeError("已超出 08:25—08:35 发送窗口，停止迟到补发，请核对运行情况")


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
        ids = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise TypeError("invalid pushed ids")
        return set(ids)
    except (json.JSONDecodeError, OSError, TypeError):
        raise RuntimeError("去重记录损坏，停止发送；请恢复状态文件") from None


def save_pushed_ids(old: set[str], new_ids: list[str]) -> None:
    """保留顺序、去重，只留最近 MAX_STATE_IDS 条。"""
    merged: list[str] = []
    seen: set[str] = set()
    # 从持久化数组恢复历史顺序，不能用 set 的任意顺序淘汰“最近”记录。
    history = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else list(old)
    for i in [i for i in history if i in old] + new_ids:
        if i not in seen:
            seen.add(i)
            merged.append(i)
    merged = merged[-MAX_STATE_IDS:]
    write_json(STATE_FILE, merged)
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


def pack_chunks(header: str, blocks: list[str], limit: int = CHUNK_LIMIT) -> list[str]:
    """按完整消息的 UTF-8 字节预算贪心分片，保留全部块、顺序和单条内容。"""
    if not blocks:
        return []

    # 片数最多等于块数，用它预留最长页码（包括两位数以上），不限制片数。
    suffix = f"（{len(blocks)}/{len(blocks)}）" if len(blocks) > 1 else ""
    budget = limit - bytelen(f"{header}{suffix}\n\n")
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_size = 0
    for i, b in enumerate(blocks, 1):
        size = bytelen(b)
        if size > budget:
            raise RuntimeError(
                f"第 {i} 个内容块 {size} 字节，超出单片正文预算 {budget}；"
                "请缩短该条内容"
            )
        separator = 2 if cur else 0
        if cur and cur_size + separator + size > budget:
            chunks.append(cur)
            cur, cur_size = [], 0
            separator = 0
        cur.append(b)
        cur_size += separator + size
    if cur:
        chunks.append(cur)

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
    """仅重试连接前失败及明确的 HTTP 429；结果不明时停止重发。"""
    payload = (
        {"msgtype": "markdown", "markdown": {"content": content}}
        if msgtype == "markdown"
        else {"msgtype": "text", "text": {"content": content}}
    )

    for attempt in range(3):
        ensure_delivery_window()
        try:
            r = _post_json(webhook, payload)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code == 408:
                raise DeliveryUncertain("企微响应不明，请核对目标群后处理当前分片") from None
            if exc.code != 429:
                raise RuntimeError(f"企微拒绝: HTTP {exc.code}") from None
            error = exc
        except urllib.error.URLError as exc:
            if not isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError)):
                raise DeliveryUncertain("企微连接中断，送达结果不明，请核对当前分片") from None
            error = exc
        except (TimeoutError, OSError, ValueError):
            raise DeliveryUncertain("企微响应超时或无效，送达结果不明，请核对当前分片") from None
        else:
            if not isinstance(r, dict) or type(r.get("errcode")) is not int:
                raise DeliveryUncertain("企微响应格式不明，请核对当前分片")
            errcode = r.get("errcode")
            errmsg = str(r.get("errmsg", ""))
            # errcode=0 但带 warning 也不能记为成功。
            if errcode == 0 and "warning" not in errmsg.lower():
                return
            raise RuntimeError(f"企微拒绝: errcode={errcode}")

        if attempt < 2:
            delay = retry_delay(error, attempt)
            log(f"[retry] 企微连接前失败或限流，第 {attempt + 1} 次等待 {delay}s")
            time.sleep(delay)
    raise RuntimeError("企微连接前失败或限流，3 次尝试已耗尽")


def dispatch(chunks: list[str], title: str, plan: dict | None = None) -> None:
    """推送当日日报到企业微信群机器人（唯一通道）。"""
    channel = (os.environ.get("PUSH_CHANNEL") or "wecom").strip().lower()
    if channel != "wecom":
        log(f"[warn] 仅支持企业微信通道，忽略 PUSH_CHANNEL={channel!r}")

    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if not hook:
        raise RuntimeError("缺少环境变量 WECOM_WEBHOOK")
    if plan and plan["inflight"] is not None:
        raise DeliveryUncertain(
            f"第 {plan['inflight'] + 1} 片结果待核对；确认后使用 --resolve-inflight sent 或 retry"
        )
    start = plan["confirmed"] if plan else 0
    for i, c in enumerate(chunks[start:], start + 1):
        ensure_delivery_window()
        if plan is not None:
            plan["inflight"] = i - 1
            save_delivery(plan)  # 必须先持久化，才允许 POST。
        try:
            send_wecom(hook, c)
        except DeliveryUncertain:
            raise  # 保留 inflight；重启后也不得盲目重发。
        except RuntimeError:
            if plan is not None:
                plan["inflight"] = None
                save_delivery(plan)  # 明确拒绝，允许下次继续此分片。
            raise
        if plan is not None:
            plan["confirmed"] = i
            plan["inflight"] = None
            save_delivery(plan)
        log(f"[push] 企微第 {i}/{len(chunks)} 片已发送（{bytelen(c)} 字节）")
        if i < len(chunks):
            time.sleep(3)  # 片数可超过 20，间隔至少 3 秒以遵守 20 条/分钟限流


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
    ap.add_argument("--check", action="store_true", help="核对今日是否在 08:25—08:35 完成")
    ap.add_argument("--resolve-inflight", choices=["sent", "retry"], help="人工核对目标群后处理结果不明分片")
    args = ap.parse_args()

    if args.no_dedup and not args.dry_run:
        raise RuntimeError("--no-dedup 只允许与 --dry-run 一起使用")
    if args.check and (args.dry_run or args.resolve_inflight):
        raise RuntimeError("--check 不能与渲染或人工处理选项混用")
    if args.resolve_inflight and args.dry_run:
        raise RuntimeError("人工处理进度不能与 --dry-run 混用")
    if args.check:
        check_delivery()
        return 0

    if not args.dry_run:
        plan = load_delivery()
        if args.resolve_inflight:
            if not plan or plan["inflight"] is None:
                raise RuntimeError("没有结果待核对的分片")
            if args.resolve_inflight == "sent":
                plan["confirmed"] += 1
            plan["inflight"] = None
            save_delivery(plan)
            log("[resolve] 已保存人工确认；下次正常运行将从此进度继续")
            return 0
        ensure_delivery_window()
        today = datetime.now(CST).strftime("%Y-%m-%d")
        if plan and plan["date"] == today and plan["completed_at"]:
            log("[skip] 今日日报已完成，跳过")
            return 0
        if plan and not plan["completed_at"]:
            log(f"[resume] 继续 {plan['date']} 已确认 {plan['confirmed']}/{len(plan['chunks'])} 片")
            dispatch(plan["chunks"], plan["header"], plan)
            finish_delivery(plan)
            if plan["date"] == today:
                return 0

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
        if not args.dry_run:
            finish_delivery(new_delivery(f"AI 热点日报 {today}", [], []))
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
    plan = new_delivery(title, chunks, [i["id"] for i in items if i.get("id")])
    save_delivery(plan)
    dispatch(chunks, title, plan)
    finish_delivery(plan)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"[fatal] {exc}")
        if not any(a in sys.argv for a in ("--dry-run",)):
            alert_failure(str(exc))
        # systemd 仅重启 GET 瞬时故障；拒绝/状态损坏/结果不明须先处理原因。
        sys.exit(1 if isinstance(exc, TransientFetchError) else 2)
