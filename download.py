#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
榜单视频下载
============
读 fetch_top.py 产出的 latest.json,把指定名次的视频下载下来。

存在的理由:你本地网络连不上 YouTube,所以「下载」这一步必须在 GitHub Actions
的海外服务器上完成,产物用 Artifact 带回本地。

用法:
  # 下载第 1~3 名(默认)
  python download.py --ranks 1 2 3

  # 下载全部 Top 5,压制到 720p 省体积
  python download.py --ranks 1 2 3 4 5 --max-height 720

  # 指定榜单文件
  python download.py --from-json reports/latest.json --out downloads
"""
import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        if _s.encoding and _s.encoding.lower() not in ("utf-8", "utf8"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bball.dl")


def safe_name(text, limit=70):
    """把标题清成安全的文件名(Windows 不允许 \\ / : * ? " < > |)。"""
    for ch in '\\/:*?"<>|\n\r\t':
        text = text.replace(ch, "_")
    text = " ".join(text.split())
    return (text[:limit] or "video").strip("._ ")


def download_one(rank, item, out_dir, fmt, max_height):
    """下载单条,返回产出文件路径(失败返回 None)。"""
    vid = item["id"]
    title = safe_name(item.get("title") or vid)
    stem = f"{rank:02d}_{title}"

    # 已存在就跳过,支持断点续传式的重复运行
    if list(out_dir.glob(f"{stem}.*")):
        log.info("[%d] 已存在,跳过: %s", rank, stem)
        return next(out_dir.glob(f"{stem}.*"))

    # 画质上限:用 height<=N 的格式串,拿不到就退到最佳
    vf = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings", "--no-progress", "--ignore-errors",
        "--socket-timeout", "30", "--retries", "5",
        "--merge-output-format", fmt,
        "-f", vf,
        "-o", str(out_dir / f"{stem}.%(ext)s"),
    ]
    if fmt == "mp4":
        # 保证在剪辑软件里能直接拖进去
        cmd += ["--remux-video", "mp4"]
    cmd.append(item["url"])

    log.info("[%d] 下载 %s", rank, item.get("title", "")[:56])
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        # 连不上是最常见的失败,单独给一句能照做的提示,别甩一堆 traceback
        if any(k in low for k in ("failed to establish a new connection", "10061",
                                  "getaddrinfo", "connection refused", "timed out",
                                  "unable to download webpage", "connection reset")):
            log.error("[%d] 连不上 YouTube —— 本地运行需要先开代理;"
                      "没代理就用 GitHub Actions 的「下载榜单视频」工作流", rank)
        else:
            log.error("[%d] 失败: %s", rank, err[-260:])
        return None

    hits = list(out_dir.glob(f"{stem}.*"))
    if hits:
        log.info("[%d] 完成: %s (%.1f MB)", rank, hits[0].name,
                 hits[0].stat().st_size / 1048576)
        return hits[0]
    log.warning("[%d] yt-dlp 返回成功但没找到输出文件", rank)
    return None


def main():
    ap = argparse.ArgumentParser(description="下载榜单视频")
    ap.add_argument("--from-json", default="reports/latest.json", help="榜单 JSON 路径")
    ap.add_argument("--ranks", nargs="+", type=int, default=[1, 2, 3],
                    help="要下载的名次,如 --ranks 1 2 3")
    ap.add_argument("--out", default="downloads", help="输出目录")
    ap.add_argument("--format", default="mp4", choices=["mp4", "mkv", "webm"])
    ap.add_argument("--max-height", type=int, default=1080,
                    help="画质上限(像素高),默认 1080。设 720 可省一半体积")
    args = ap.parse_args()

    src = Path(args.from_json)
    if not src.exists():
        log.error("找不到榜单文件 %s —— 先跑 python fetch_top.py", src)
        sys.exit(1)

    data = json.loads(src.read_text(encoding="utf-8"))
    items = {it["rank"]: it for it in data.get("top", [])}
    if not items:
        log.error("榜单是空的,没有可下载的视频")
        sys.exit(1)

    # 按榜单日期分目录,避免不同天的同名视频互相覆盖
    stamp = (data.get("generated_at") or "")[:10] or "undated"
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("榜单生成于 %s,共 %d 条", data.get("generated_at", "?"), len(items))
    ok = []
    for rank in args.ranks:
        item = items.get(rank)
        if not item:
            log.warning("榜单里没有第 %d 名,跳过", rank)
            continue
        if item.get("risk") == "高":
            log.warning("[%d] ⚠️ 该条疑似官方版权方(%s),仅供解说/二创参考",
                        rank, item.get("channel"))
        p = download_one(rank, item, out_dir, args.format, args.max_height)
        if p:
            ok.append(p)

    log.info("完成 %d/%d,输出目录:%s", len(ok), len(args.ranks), out_dir)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
