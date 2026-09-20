#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
篮球海外热榜采集
================
抓取 YouTube 上最近 24 小时内发布、播放量最高的篮球视频,输出每日榜单。

为什么用 yt-dlp 而不是 YouTube Data API:
  1. 不需要 API key(在大陆办 Google Cloud 账号本身就是个坎)
  2. 数据中心 IP(如 GitHub Actions)走 API 容易被判机器人;yt-dlp 久经考验

为什么自己在本地排序,而不是让 YouTube 按播放量排:
  YouTube 的 sp 过滤参数是 base64 编码的 protobuf,写死会随官方改动失效。
  这里改成:多关键词大范围捞取 → 本地按「发布时间窗口 + 播放量」过滤排序,
  不依赖任何魔法参数,官方改版也不会静默出错。

用法:
  # 正常采集(需要能访问 YouTube 的网络)
  python fetch_top.py

  # 自定义关键词与窗口
  python fetch_top.py --keywords "nba highlights" "streetball" --window 48 --top 10

  # 不联网,用内置样例数据跑一遍,验证渲染逻辑
  python fetch_top.py --selftest
"""
import argparse
import json
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 控制台默认 GBK,强制 UTF-8 输出,避免中文乱码
for _s in (sys.stdout, sys.stderr):
    try:
        if _s.encoding and _s.encoding.lower() not in ("utf-8", "utf8"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bball")

# ------------------------------- 默认配置 -------------------------------

DEFAULT_KEYWORDS = [
    "basketball highlights",
    "nba highlights",
    "nba best plays",
    "streetball",
    "basketball mixtape",
    "dunk compilation",
    "nba today",
    "basketball shorts",
]

# 频道名命中这些词 → 大概率是官方版权方,二次加工风险高
OFFICIAL_PATTERNS = [
    r"\bnba\b", r"national basketball", r"\bespn\b", r"turner sports", r"\btnt\b",
    r"\bnbatv\b", r"nba on", r"warriors", r"lakers", r"celtics", r"bulls\b",
    r"knicks", r"heat\b", r"nets\b", r"suns\b", r"bucks\b", r"nuggets",
    r"\bfiba\b", r"euroleague", r"olympics",
]
_OFFICIAL_RE = re.compile("|".join(OFFICIAL_PATTERNS), re.I)

# 明显跟篮球无关的标题 → 关键词搜索常带进来的噪音
NOISE_RE = re.compile(
    r"\b(fortnite|minecraft|roblox|gta|nba 2k|2k\d\d|篮球鞋|球鞋|sneaker|unboxing|"
    r"reaction to|podcast full|full episode|livestream|live stream|press conference)\b",
    re.I,
)


# ------------------------------- 工具函数 -------------------------------

def _parse_count(text):
    """把 '1.2M views' / '345K' / '1,234' 解析成整数。"""
    if not text:
        return None
    if isinstance(text, int):
        return text
    m = re.search(r"([\d.,]+)\s*([KMB]?)", str(text), re.I)
    if not m:
        return None
    num, unit = m.group(1).replace(",", ""), m.group(2).upper()
    try:
        val = float(num)
    except ValueError:
        return None
    return int(val * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[unit])


def _fmt_count(n):
    """整数 → 1,234,567 这种可读格式。"""
    return f"{n:,}" if isinstance(n, int) else "—"


def _fmt_duration(seconds):
    """秒 → 3:24 或 1:02:33。"""
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_age(hours):
    """小时数 → '6 小时前'。"""
    if hours is None:
        return "—"
    if hours < 1:
        return f"{int(hours * 60)} 分钟前"
    if hours < 48:
        return f"{hours:.1f} 小时前"
    return f"{hours / 24:.1f} 天前"


def _risk_of(channel):
    """按频道名粗判版权风险。官方版权方的素材二次加工风险最高。"""
    if channel and _OFFICIAL_RE.search(channel):
        return "高", "疑似官方版权方,转载/剪辑风险高,建议只做解说或数据二创"
    return "低", "非官方频道,仍需确认原始素材来源"


# ------------------------------- 诊断收集 -------------------------------
# GitHub Actions 的日志在网页上,排查时够不着。这里把关键信息攒起来写进
# 报告文件,报告会提交回仓库 —— 这样 git pull 就能看到失败原因,
# 不需要开网页、不需要 API、不需要截图。

DIAG = []


def diag(msg):
    """记录一条诊断信息(同时进日志和报告)。"""
    log.warning("[诊断] %s", msg)
    DIAG.append(str(msg))


# YouTube 对数据中心 IP(GitHub Actions 就是)有反爬。不同 player client
# 走的接口不一样,一个被挡换下一个。默认排最前,其余作为退路依次尝试。
CLIENT_VARIANTS = [
    None,
    "youtube:player_client=web_safari",
    "youtube:player_client=tv_embedded",
    "youtube:player_client=mweb",
    "youtube:player_client=android",
]


# ------------------------------- 采集核心 -------------------------------

def _ytdlp_jsonlines(extra_args, timeout=420, quiet=False):
    """跑一次 yt-dlp,把逐行 JSON 解析成 dict 列表。失败返回空列表。"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--dump-json", "--no-warnings", "--ignore-errors", "--no-progress",
        "--socket-timeout", "20", "--retries", "3",
    ] + extra_args
    log.debug("yt-dlp: %s", " ".join(extra_args))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        diag(f"yt-dlp 超时({timeout}s): {' '.join(extra_args[:2])}")
        return []
    if proc.returncode != 0 and not proc.stdout.strip():
        err = (proc.stderr or "").strip()
        if not quiet:
            diag(f"yt-dlp 失败 rc={proc.returncode}: {err[-400:]}")
        return []

    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def search_keyword(keyword, depth):
    """扁平搜索一个关键词。依次尝试各个 player client,直到有一个返回结果。

    为什么要循环试:YouTube 对数据中心 IP 会返回「确认你不是机器人」页面,
    而不同 client 走的后端接口不同,往往只有部分被挡。
    """
    for variant in CLIENT_VARIANTS:
        args = ["--flat-playlist"]
        if variant:
            args += ["--extractor-args", variant]
        args.append(f"ytsearch{depth}:{keyword}")

        items = _ytdlp_jsonlines(args, quiet=(variant is not None))
        if items:
            if variant:
                diag(f"关键词「{keyword}」用 {variant} 才成功(默认 client 被挡)")
            log.info("  关键词 %-28s → %d 条", keyword, len(items))
            return items
    diag(f"关键词「{keyword}」在所有 client 下都返回空")
    return []


def enrich(video_id):
    """补全单条视频的精确元数据(播放量/发布时间/时长)。"""
    for variant in CLIENT_VARIANTS:
        args = []
        if variant:
            args += ["--extractor-args", variant]
        args.append(f"https://www.youtube.com/watch?v={video_id}")
        items = _ytdlp_jsonlines(args, timeout=120, quiet=(variant is not None))
        if items:
            return items[0]
    return None


def collect(cfg):
    """主流程:多关键词搜索 → 去重 → 补全 → 时间窗过滤 → 按播放量排序。"""
    DIAG.clear()
    # 1. 多关键词并行搜索
    log.info("搜索 %d 个关键词,每个取 %d 条 …", len(cfg["keywords"]), cfg["depth"])
    raw = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(search_keyword, kw, cfg["depth"]): kw for kw in cfg["keywords"]}
        for f in as_completed(futs):
            raw.extend(f.result())
    diag(f"阶段1 搜索:原始 {len(raw)} 条")

    # 2. 按 video id 去重,顺手做一次播放量粗排
    seen, candidates = set(), []
    for it in raw:
        vid = it.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        title = it.get("title") or ""
        if NOISE_RE.search(title):
            continue
        candidates.append(it)
    log.info("去重后 %d 条候选", len(candidates))
    diag(f"阶段2 去重去噪后:{len(candidates)} 条")

    # 3. 扁平搜索的播放量/时间不可靠,对头部候选补全精确元数据
    candidates.sort(key=lambda x: _parse_count(x.get("view_count")) or 0, reverse=True)
    head = candidates[: cfg["enrich"]]
    log.info("对播放量最高的 %d 条补全精确数据(约 %d 秒)…", len(head), len(head) * 3)

    detailed = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(enrich, it["id"]): it for it in head}
        for f in as_completed(futs):
            d = f.result()
            if d:
                detailed.append(d)
    log.info("补全成功 %d 条", len(detailed))
    diag(f"阶段3 补全精确数据:{len(detailed)}/{len(head)} 条")

    # 4. 发布时间窗口过滤
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg["window"])
    kept = []
    for d in detailed:
        ts = d.get("timestamp")
        if ts:
            pub = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            ud = d.get("upload_date")  # 'YYYYMMDD'
            if not ud:
                continue
            pub = datetime.strptime(ud, "%Y%m%d").replace(tzinfo=timezone.utc)
        if pub < cutoff:
            continue
        views = d.get("view_count")
        if not views:
            continue
        age_h = max((now - pub).total_seconds() / 3600, 0.05)
        d["_pub"] = pub
        d["_age_hours"] = age_h
        d["_velocity"] = views / age_h  # 播放量/小时,衡量"正在爆"的程度
        kept.append(d)

    log.info("时间窗内(%.0f 小时)有 %d 条", cfg["window"], len(kept))
    diag(f"阶段4 时间窗 {cfg['window']:.0f}h 内:{len(kept)} 条")

    # 5. 按播放量排序(用户要的是"播放量前五")
    kept.sort(key=lambda x: x["view_count"], reverse=True)
    return kept


# ------------------------------- 报告渲染 -------------------------------

def render_markdown(rows, cfg, generated_at):
    """渲染成给人和下游脚本用的 markdown。"""
    top = rows[: cfg["top"]]
    table = rows[: cfg["table"]]

    lines = [
        f"# 🏀 篮球海外热榜 · {generated_at:%Y-%m-%d}",
        "",
        f"> 数据源:YouTube 全站 | 统计窗口:过去 {cfg['window']:.0f} 小时 | "
        f"关键词 {len(cfg['keywords'])} 组 | 生成于 {generated_at:%Y-%m-%d %H:%M} 北京时间",
        "",
    ]

    if not top:
        lines += [
            "## ⚠️ 本次没有采到数据",
            "",
            "---",
            "",
            "### 🔍 诊断(排查用,定位死在哪一环)",
            "",
            "```text",
            *(DIAG or ["(没有诊断信息 —— 说明脚本根本没跑到采集阶段)"]),
            "```",
            "",
            "**怎么读这几行:**",
            "",
            "| 停在哪 | 说明 |",
            "|---|---|",
            "| 阶段1 = 0 条 | YouTube 把 GitHub 的服务器 IP 挡了(反爬),或网络不通 |",
            "| 阶段1 有数、阶段3 = 0 | 搜索能通但取视频详情被挡 |",
            "| 阶段4 = 0 | 前面都正常,只是这个时间窗内确实没有新视频(放宽 `--window`) |",
            "| 一条诊断都没有 | 脚本没执行到采集,是环境/依赖问题 |",
            "",
        ]
        return "\n".join(lines)

    lines += [f"## Top {len(top)}", ""]
    for i, r in enumerate(top, 1):
        risk, risk_note = _risk_of(r.get("channel") or r.get("uploader"))
        vid = r["id"]
        lines += [
            f"### {i}. {r.get('title', '(无标题)')}",
            "",
            f"- 📺 **频道**:{r.get('channel') or r.get('uploader') or '—'}",
            f"- 👁 **播放量**:{_fmt_count(r['view_count'])}",
            f"- ⚡ **热度**:{_fmt_count(int(r['_velocity']))} 次/小时",
            f"- ⏱ **时长**:{_fmt_duration(r.get('duration'))}",
            f"- 🕐 **发布**:{_fmt_age(r['_age_hours'])}",
            f"- 🔗 **链接**:https://youtu.be/{vid}",
            f"- ⚖️ **版权风险**:{risk} —— {risk_note}",
            "",
        ]

    if len(table) > len(top):
        lines += [
            f"## 完整榜单(Top {len(table)})",
            "",
            "| # | 标题 | 频道 | 播放量 | 热度/小时 | 时长 | 发布 | 链接 |",
            "|---|------|------|--------|-----------|------|------|------|",
        ]
        for i, r in enumerate(table, 1):
            title = (r.get("title") or "").replace("|", "丨")[:52]
            chan = (r.get("channel") or r.get("uploader") or "—").replace("|", "丨")[:20]
            lines.append(
                f"| {i} | {title} | {chan} | {_fmt_count(r['view_count'])} | "
                f"{_fmt_count(int(r['_velocity']))} | {_fmt_duration(r.get('duration'))} | "
                f"{_fmt_age(r['_age_hours'])} | [看](https://youtu.be/{r['id']}) |"
            )
        lines.append("")

    # 官方版权方统计,直接告诉用户这期有几条不能碰
    high = [r for r in top if _risk_of(r.get("channel") or r.get("uploader"))[0] == "高"]
    lines += ["## ⚖️ 选片提示", ""]
    if high:
        lines += [
            f"本期 Top {len(top)} 里有 **{len(high)} 条**来自疑似官方版权方:",
            "",
        ]
        lines += [f"- {r.get('channel')} —— {r.get('title', '')[:40]}" for r in high]
        lines += [
            "",
            "NBA 在中国的独家数字版权在腾讯,官方素材直接搬运是维权重点。"
            "这几条建议只做**解说、数据可视化或评论**,不要原片重传。",
            "",
        ]
    else:
        lines += ["本期 Top 榜没有命中官方版权方关键词,但仍需自行确认素材来源。", ""]

    return "\n".join(lines)


def render_json(rows, cfg, generated_at):
    """结构化输出,给下载脚本和后续自动化消费。"""
    top = rows[: cfg["top"]]
    return {
        "generated_at": generated_at.isoformat(),
        "window_hours": cfg["window"],
        "keywords": cfg["keywords"],
        "source": "youtube",
        "top": [
            {
                "rank": i,
                "id": r["id"],
                "title": r.get("title"),
                "channel": r.get("channel") or r.get("uploader"),
                "url": f"https://youtu.be/{r['id']}",
                "view_count": r["view_count"],
                "views_per_hour": int(r["_velocity"]),
                "duration": r.get("duration"),
                "published": r["_pub"].isoformat(),
                "age_hours": round(r["_age_hours"], 2),
                "thumbnail": r.get("thumbnail"),
                "risk": _risk_of(r.get("channel") or r.get("uploader"))[0],
            }
            for i, r in enumerate(top, 1)
        ],
    }


# ------------------------------- 自检样例 -------------------------------

SELFTEST_ROWS = [
    ("dQw4w9WgXcQ", "Lakers vs Celtics FULL Highlights", "NBA", 1_820_000, 4.0, 512),
    ("abc12345678", "INSANE Streetball Dunk Compilation", "Ballislife", 940_000, 7.5, 421),
    ("def12345678", "Best NBA Plays of the Night", "House of Highlights", 610_000, 11.0, 358),
    ("ghi12345678", "1v1 to 21 — The Rematch", "Overtime", 402_000, 19.0, 733),
    ("jkl12345678", "Why Nobody Can Guard This Rookie", "Thinking Basketball", 88_000, 2.0, 601),
]


def selftest():
    """不联网跑一遍渲染,验证 markdown/json 输出与风险判定是否正确。"""
    now = datetime.now(timezone.utc)
    rows = []
    for vid, title, chan, views, age, dur in SELFTEST_ROWS:
        rows.append({
            "id": vid, "title": title, "channel": chan, "view_count": views,
            "duration": dur, "_age_hours": age, "_velocity": views / age,
            "_pub": now - timedelta(hours=age),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    rows.sort(key=lambda x: x["view_count"], reverse=True)
    cfg = {"keywords": DEFAULT_KEYWORDS, "window": 24, "top": 5, "table": 20, "depth": 0, "enrich": 0}
    md = render_markdown(rows, cfg, now)
    print(md)
    print("=" * 60)
    print(json.dumps(render_json(rows, cfg, now), ensure_ascii=False, indent=2)[:900])
    assert "NBA" in md and "Top 5" in md, "渲染异常"
    assert json.loads(json.dumps(render_json(rows, cfg, now)))["top"][0]["rank"] == 1
    print("\n[selftest] 渲染与序列化检查通过 ✅")


# ------------------------------- 入口 -------------------------------

def main():
    ap = argparse.ArgumentParser(description="篮球海外热榜采集(YouTube)")
    ap.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS,
                    help="搜索关键词,空格分隔")
    ap.add_argument("--window", type=float, default=24,
                    help="发布时间窗口(小时),默认 24")
    ap.add_argument("--top", type=int, default=5, help="Top N,默认 5")
    ap.add_argument("--table", type=int, default=20, help="完整榜单条数,默认 20")
    ap.add_argument("--depth", type=int, default=60, help="每个关键词搜索条数,默认 60")
    ap.add_argument("--enrich", type=int, default=40, help="补全精确数据的条数,默认 40")
    ap.add_argument("--out-dir", default="reports", help="输出目录")
    ap.add_argument("--selftest", action="store_true", help="用样例数据自检渲染,不联网")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    cfg = {
        "keywords": args.keywords, "window": args.window, "top": args.top,
        "table": args.table, "depth": args.depth, "enrich": args.enrich,
    }
    # 用北京时间做报告日期,更符合使用习惯
    generated_at = datetime.now(timezone(timedelta(hours=8)))

    rows = collect(cfg)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = render_markdown(rows, cfg, generated_at)
    md_path = out_dir / f"{generated_at:%Y-%m-%d}.md"
    md_path.write_text(md, encoding="utf-8")

    data = render_json(rows, cfg, generated_at)
    (out_dir / "latest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("已写出 %s 与 reports/latest.json", md_path)
    if rows:
        log.info("第 1 名:%s —— %s 次播放", rows[0].get("title", "")[:50],
                 _fmt_count(rows[0]["view_count"]))


if __name__ == "__main__":
    main()
