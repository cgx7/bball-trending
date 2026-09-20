#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
篮球海外热榜采集
================
抓取 YouTube 上最近 24 小时内发布、播放量最高的篮球视频,输出每日榜单。

为什么不用 YouTube Data API:
  1. 不需要 API key(在大陆办 Google Cloud 账号本身就是个坎)
  2. 数据中心 IP(如 GitHub Actions)走 API 容易被判机器人

为什么直接解析搜索结果页,而不是用 yt-dlp 逐条取视频详情:
  实测在 GitHub Actions 的机房 IP 上,搜索接口放行,但逐个请求
  watch?v= 会被 "Sign in to confirm you're not a bot" 全部挡死,
  换 player_client 也绕不过。而搜索结果页本身就带播放量/发布时间/时长,
  解析它等于一次请求拿全所有字段,既绕开反爬又比逐条请求快一个数量级。
  详见下面「采集核心」段的注释。

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
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

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


def _duration_text(rec):
    """时长显示。搜索页直接给 '3:24' 字符串,自检样例给的是秒数。"""
    return rec.get("duration_text") or _fmt_duration(rec.get("duration"))


def _published_at(rec, generated_at):
    """估算发布时间。搜索页只给「N 小时前」,从生成时刻倒推。"""
    if rec.get("_pub"):
        return rec["_pub"]
    return generated_at - timedelta(hours=rec.get("_age_hours") or 0)


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


# 抓搜索结果页用的请求头。带 Accept-Language 是为了让 YouTube 返回英文页面,
# 这样相对时间就是 "3 hours ago",好解析。
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


# ------------------------------- 采集核心 -------------------------------
#
# 这里为什么不用 yt-dlp 取视频详情(踩过的坑,别再改回去):
#
#   阶段1 搜索:原始 480 条          ← 搜索能过
#   阶段2 去重去噪后:421 条          ← 数据没问题
#   ERROR: [youtube] xxx: Sign in to confirm you're not a bot.   ← 全挂
#   阶段3 补全精确数据:0/40 条
#
# GitHub Actions 的机房 IP 上,YouTube 的搜索接口放行,但逐个取 watch?v= 详情
# 会被反爬全部挡死,换 player_client(web_safari/tv_embedded/mweb/android)也没用。
#
# 但搜索结果页本身就已经带着播放量、发布时间、时长 —— 直接解析它,
# 一次请求拿全所有字段,既绕开被封的接口,又比逐条请求快一个数量级。

def _extract_json_blob(html, marker):
    """从 HTML 里抠出 marker 后面那坨 JSON(如 ytInitialData)。

    用花括号配对而不是正则:ytInitialData 嵌套很深,且视频标题里可能出现
    '}' 或 '};',正则会提前截断。这里按字符串状态逐字符配对,才稳。
    """
    i = html.find(marker)
    if i < 0:
        return None
    i = html.find("{", i)
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _walk_find(node, target, out):
    """递归收集所有名为 target 的键对应的值。

    不写死 contents.twoColumnSearchResultsRenderer.… 这条路径:
    YouTube 改版频繁,路径会变,但 videoRenderer 这个键名很稳定。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == target:
                out.append(v)
            else:
                _walk_find(v, target, out)
    elif isinstance(node, list):
        for item in node:
            _walk_find(item, target, out)


def _text_of(node):
    """从 {'simpleText': ...} 或 {'runs': [{'text': ...}]} 里取纯文本。"""
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"] or ""
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


_REL_TIME_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago", re.I
)
_UNIT_HOURS = {
    "second": 1 / 3600, "minute": 1 / 60, "hour": 1,
    "day": 24, "week": 24 * 7, "month": 24 * 30, "year": 24 * 365,
}


def _parse_relative_age(text):
    """'3 hours ago' / 'Streamed 47 minutes ago' → 小时数(浮点)。

    YouTube 搜索结果页只给相对时间,不给时间戳。对「最近 24 小时」这个
    粒度来说完全够用 —— 没必要为了一个精确到秒的值去撞反爬。
    """
    if not text:
        return None
    m = _REL_TIME_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) * _UNIT_HOURS[m.group(2).lower()]


def _new_session():
    """带 CONSENT cookie 的会话,跳过欧盟那种同意跳转页。"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("CONSENT", "YES+cb.20210328-17-p0.en+FX+000", domain=".youtube.com")
    return s


def search_page(keyword, session=None):
    """抓一个关键词的搜索结果页,从 ytInitialData 里解析出视频列表。"""
    sess = session or _new_session()
    try:
        r = sess.get(
            "https://www.youtube.com/results",
            params={"search_query": keyword, "hl": "en", "gl": "US"},
            timeout=25,
        )
        r.raise_for_status()
    except Exception as e:
        diag(f"关键词「{keyword}」请求失败:{type(e).__name__}: {e}")
        return []

    data = _extract_json_blob(r.text, "ytInitialData")
    if data is None:
        diag(f"关键词「{keyword}」:页面里找不到 ytInitialData"
             f"(HTML 长度 {len(r.text)},多半是被反爬挡了或官方改版)")
        return []

    renderers = []
    _walk_find(data, "videoRenderer", renderers)
    if not renderers:
        diag(f"关键词「{keyword}」:ytInitialData 里没有 videoRenderer(可能改版)")
        return []

    out, no_views, no_time = [], 0, 0
    for vr in renderers:
        vid = vr.get("videoId")
        if not vid:
            continue
        # viewCountText 是精确值("1,234,567 views"),
        # shortViewCountText 是缩写("1.2M views")。优先用精确的。
        views = (_parse_count(_text_of(vr.get("viewCountText")))
                 or _parse_count(_text_of(vr.get("shortViewCountText"))))
        age = _parse_relative_age(_text_of(vr.get("publishedTimeText")))
        if views is None:
            no_views += 1
        if age is None:
            no_time += 1
        out.append({
            "id": vid,
            "title": _text_of(vr.get("title")),
            "channel": (_text_of(vr.get("ownerText"))
                        or _text_of(vr.get("longBylineText"))),
            "view_count": views,
            "age_hours": age,
            "duration_text": _text_of(vr.get("lengthText")),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })

    log.info("  关键词 %-28s → %d 条(缺播放量 %d,缺时间 %d)",
             keyword, len(out), no_views, no_time)
    return out


def collect(cfg):
    """主流程:多关键词抓搜索页 → 去重去噪 → 时间窗过滤 → 按播放量排序。"""
    DIAG.clear()
    log.info("搜索 %d 个关键词 …", len(cfg["keywords"]))
    raw = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(search_page, kw): kw for kw in cfg["keywords"]}
        for f in as_completed(futs):
            raw.extend(f.result())
    diag(f"阶段1 搜索:原始 {len(raw)} 条")

    # 2. 按 video id 去重 + 标题去噪
    seen, candidates = set(), []
    for it in raw:
        vid = it.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        if NOISE_RE.search(it.get("title") or ""):
            continue
        candidates.append(it)
    diag(f"阶段2 去重去噪后:{len(candidates)} 条")

    # 3. 时间窗 + 播放量过滤
    no_time = no_views = 0
    kept = []
    for it in candidates:
        age = it.get("age_hours")
        if age is None:
            no_time += 1
            continue
        if age > cfg["window"]:
            continue
        views = it.get("view_count")
        if not views:
            no_views += 1
            continue
        it["_age_hours"] = age
        it["_velocity"] = views / max(age, 0.05)  # 播放量/小时,衡量"正在爆"
        kept.append(it)
    diag(f"阶段3 时间窗 {cfg['window']:.0f}h 内:{len(kept)} 条"
         f"(因无发布时间剔除 {no_time} 条、无播放量剔除 {no_views} 条)")

    # 4. 按播放量排序(用户要的是"播放量前五")
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
            "| 阶段1 = 0 条 | 搜索结果页请求失败/被反爬挡了,或网络不通 |",
            "| 阶段1 有数、阶段2 ≈ 0 | 标题去噪把结果全滤掉了(NOISE_RE 太激进) |",
            "| 阶段3 = 0 条 | 前面都正常,只是这个时间窗内确实没有新视频(放宽 `--window`) |",
            "| 大量「缺播放量/缺时间」 | YouTube 改版了搜索结果页的字段名,需要更新解析 |",
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
            f"- ⏱ **时长**:{_duration_text(r)}",
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
                f"{_fmt_count(int(r['_velocity']))} | {_duration_text(r)} | "
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
                "duration": _duration_text(r),
                "published": _published_at(r, generated_at).isoformat(),
                "age_hours": round(r["_age_hours"], 2),
                # 搜索页不给缩略图 URL,但 YouTube 的缩略图地址是有规律的,拼就是了
                "thumbnail": (r.get("thumbnail")
                              or f"https://i.ytimg.com/vi/{r['id']}/hqdefault.jpg"),
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


def _fake_search_html():
    """伪造一份搜索结果页,结构照着 YouTube 真实的 ytInitialData 来。

    为什么要伪造:开发机在大陆连不上 YouTube,而解析代码是整条链路上最
    容易出错的地方(字段名、嵌套层级、转义)。这里用假数据把解析逻辑
    钉死,联网那部分交给 Actions 去验证。

    故意埋的坑:标题里带 '}' 和 '";' —— 正则抠 JSON 会在这里截断,
    花括号配对法必须扛住。
    """
    def vr(vid, title, chan, views, short_views, age, length):
        return {
            "videoRenderer": {
                "videoId": vid,
                "title": {"runs": [{"text": title}]},
                "ownerText": {"runs": [{"text": chan, "navigationEndpoint": {}}]},
                "viewCountText": {"simpleText": views} if views else {},
                "shortViewCountText": {"simpleText": short_views} if short_views else {},
                "publishedTimeText": {"simpleText": age} if age else {},
                "lengthText": {"simpleText": length},
                "thumbnail": {"thumbnails": [{"url": f"https://i.ytimg.com/vi/{vid}/hq.jpg"}]},
            }
        }

    data = {
        "contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
            "sectionListRenderer": {"contents": [
                {"itemSectionRenderer": {"contents": [
                    vr("vid001", "Lakers vs Celtics | FULL Highlights };\" weird",
                       "NBA", "1,820,000 views", "1.8M views", "4 hours ago", "9:12"),
                    vr("vid002", "INSANE Streetball Dunk Compilation",
                       "Ballislife", "940,000 views", "940K views", "7 hours ago", "7:01"),
                    vr("vid003", "Best NBA Plays of the Night",
                       "House of Highlights", "610,000 views", "610K views",
                       "Streamed 11 hours ago", "5:58"),
                    # 老视频 → 应被 24h 窗口挡掉
                    vr("vid004", "2016 Finals Game 7 Full Game",
                       "NBA", "48,000,000 views", "48M views", "3 years ago", "2:11:04"),
                    # 缺播放量 → 应被剔除且计入诊断
                    vr("vid005", "Unknown Upload", "Some Chan", "", "", "2 hours ago", "1:00"),
                    # 缺发布时间 → 应被剔除且计入诊断
                    vr("vid006", "No Timestamp Here", "Some Chan",
                       "5,000 views", "5K views", "", "0:44"),
                ]}},
                {"continuationItemRenderer": {"trigger": "CONTINUATION_TRIGGER_ON_ITEM_SHOWN"}},
            ]}
        }}}
    }
    return ("<html><script>var ytInitialData = "
            + json.dumps(data, ensure_ascii=False)
            + ";</script></html>")


def selftest():
    """不联网跑一遍解析 + 渲染,验证整条链路。"""
    now = datetime.now(timezone.utc)

    # ---- 1. 解析:伪造搜索结果页,走一遍真实的解析代码 ----
    html = _fake_search_html()
    assert _extract_json_blob(html, "ytInitialData") is not None, \
        "花括号配对法没能从 HTML 里抠出 ytInitialData"

    # 直接复用 search_page 的解析分支(不联网:传一个假的 session)
    class _FakeResp:
        text = html
        def raise_for_status(self):
            pass

    class _FakeSession:
        def get(self, *a, **kw):
            return _FakeResp()

    parsed = search_page("dummy", session=_FakeSession())
    by_id = {p["id"]: p for p in parsed}
    assert len(parsed) == 6, f"应解析出 6 条,实际 {len(parsed)}"
    assert by_id["vid001"]["title"].endswith('};" weird'), "标题里的 } 和 \"; 被截断了"
    assert by_id["vid001"]["view_count"] == 1_820_000, "精确播放量解析错"
    assert by_id["vid001"]["channel"] == "NBA", "频道名解析错"
    assert abs(by_id["vid002"]["age_hours"] - 7) < 0.01, "相对时间解析错"
    assert abs(by_id["vid003"]["age_hours"] - 11) < 0.01, "'Streamed 11 hours ago' 解析错"
    assert abs(by_id["vid004"]["age_hours"] - 24 * 365 * 3) < 1, "'3 years ago' 解析错"
    assert by_id["vid005"]["view_count"] is None and by_id["vid005"]["age_hours"] == 2
    assert by_id["vid006"]["view_count"] == 5_000 and by_id["vid006"]["age_hours"] is None

    # 只有短播放量时,退回到 shortViewCountText
    assert _parse_count("1.2M views") == 1_200_000
    assert _parse_count("2,345,678 views") == 2_345_678
    print(f"[selftest] 解析通过 ✅  6 条里 4 条字段完整、2 条各缺一项(符合预期)")

    # ---- 2. 过滤 + 排序:用解析结果跑一遍,应当只剩 3 条 ----
    cfg = {"keywords": DEFAULT_KEYWORDS, "window": 24, "top": 5, "table": 20}
    candidates = [p for p in parsed if not NOISE_RE.search(p["title"])]
    kept = []
    for it in candidates:
        if it["age_hours"] is None or it["age_hours"] > cfg["window"] or not it["view_count"]:
            continue
        it["_age_hours"] = it["age_hours"]
        it["_velocity"] = it["view_count"] / max(it["age_hours"], 0.05)
        kept.append(it)
    kept.sort(key=lambda x: x["view_count"], reverse=True)
    assert [k["id"] for k in kept] == ["vid001", "vid002", "vid003"], \
        f"过滤/排序错:拿到 {[k['id'] for k in kept]}"
    print("[selftest] 过滤排序通过 ✅  4 年前的、缺播放量的、缺时间的都被正确剔除")

    # ---- 3. 渲染 ----
    md = render_markdown(kept, cfg, now)
    print()
    print(md)
    print("=" * 60)
    js = render_json(kept, cfg, now)
    print(json.dumps(js, ensure_ascii=False, indent=2)[:700])
    assert "Top 3" in md, "渲染异常"
    assert js["top"][0]["rank"] == 1 and js["top"][0]["published"], "JSON 序列化异常"
    assert js["top"][1]["thumbnail"].startswith("https://i.ytimg.com/vi/vid002/"), "缩略图拼接错"
    print("\n[selftest] 渲染与序列化通过 ✅")
    print("\n全部自检通过 —— 解析、过滤、排序、渲染四段都正常。")


# ------------------------------- 入口 -------------------------------

def main():
    ap = argparse.ArgumentParser(description="篮球海外热榜采集(YouTube)")
    ap.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS,
                    help="搜索关键词,空格分隔")
    ap.add_argument("--window", type=float, default=24,
                    help="发布时间窗口(小时),默认 24")
    ap.add_argument("--top", type=int, default=5, help="Top N,默认 5")
    ap.add_argument("--table", type=int, default=20, help="完整榜单条数,默认 20")
    ap.add_argument("--out-dir", default="reports", help="输出目录")
    ap.add_argument("--selftest", action="store_true",
                    help="用伪造的搜索结果页自检解析+渲染,不联网")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    cfg = {
        "keywords": args.keywords, "window": args.window,
        "top": args.top, "table": args.table,
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
