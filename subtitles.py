"""B站字幕抓取：优先官方/CC/AI 字幕，处理 WBI 签名与登录态。

字幕本身不是视频画面，但它是「视频讲了什么」最便宜、最准确的来源；
没有字幕时再退回纯画面理解（后续可接音频 ASR）。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
_ZH_PRIORITY = ("zh-CN", "zh-Hans", "zh-Hant", "zh-HK")


def pick_subtitle(subtitles: list[dict]) -> dict | None:
    """优先简体/中文，其次任意 zh，最后取第一条。"""
    items = [s for s in (subtitles or []) if isinstance(s, dict) and s.get("subtitle_url")]
    if not items:
        return None
    for lan in _ZH_PRIORITY:
        for item in items:
            if str(item.get("lan") or "") == lan:
                return item
    for item in items:
        if str(item.get("lan") or "").lower().startswith("zh"):
            return item
    return items[0]


def _stamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_subtitles(body: list[dict], max_chars: int = 5000) -> str:
    """把字幕 body 压成带时间戳的文本；超长时保留开头和结尾。"""
    lines = []
    for item in body or []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not text:
            continue
        lines.append(f"[{_stamp(float(item.get('from') or 0))}] {text}")
    text = "\n".join(lines)
    max_chars = max(500, int(max_chars))
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head] + "\n…（中间省略）…\n" + text[-tail:]


def _subtitle_host_ok(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith("hdslb.com") or host.endswith("bilibili.com")


async def fetch_subtitles(
    bvid: str,
    cid: int | str | None,
    sessdata: str = "",
    bili_jct: str = "",
    max_chars: int = 5000,
    timeout: int = 20,
) -> dict:
    """返回 {"lan","lan_doc","text"}；没有字幕或失败时返回 {}。"""
    if not bvid:
        return {}
    import aiohttp
    from astrbot.api import logger

    try:
        from bilibili_api import Credential, video
    except Exception:
        logger.info("[bili_vision] 未安装 bilibili-api-python，跳过字幕")
        return {}
    credential = Credential(sessdata=str(sessdata or ""), bili_jct=str(bili_jct or ""))
    try:
        v = video.Video(str(bvid), credential=credential)
        if not cid:
            cid = await v.get_cid(0)
        data = await v.get_subtitle(cid)
    except Exception as exc:
        logger.info(f"[bili_vision] 取字幕失败：{exc}")
        return {}
    chosen = pick_subtitle((data or {}).get("subtitles") or [])
    if not chosen:
        return {}
    url = str(chosen.get("subtitle_url") or "")
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http") or not _subtitle_host_ok(url):
        logger.info(f"[bili_vision] 字幕地址不在 B站域名内，跳过：{url[:80]}")
        return {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=HEADERS) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
    except Exception as exc:
        logger.info(f"[bili_vision] 下载字幕失败：{exc}")
        return {}
    text = format_subtitles((payload or {}).get("body") or [], max_chars=max_chars)
    if not text:
        return {}
    return {
        "lan": str(chosen.get("lan") or ""),
        "lan_doc": str(chosen.get("lan_doc") or ""),
        "text": text,
    }
