"""B站音频提取：playurl 取 DASH 音轨 → ffmpeg 转 16k 单声道 wav。

只在没有字幕、需要本地 ASR 时使用；音频不长期保存，任务结束即删。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
_AUDIO_HOSTS = (
    "bilivideo.com",
    "bilivideo.cn",
    "akamaized.net",
    "bilibili.com",
)
MAX_AUDIO_BYTES = 200 * 1024 * 1024


def _host_ok(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in _AUDIO_HOSTS)


def pick_audio_url(payload: dict) -> str:
    """从 playurl 返回里挑带宽最高的音频 URL。"""
    data = (payload or {}).get("data") or {}
    dash = data.get("dash") or {}
    audios = [a for a in (dash.get("audio") or []) if isinstance(a, dict)]
    if audios:
        best = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
        return str(best.get("baseUrl") or best.get("base_url") or "")
    durl = data.get("durl") or []
    if durl and isinstance(durl[0], dict):
        return str(durl[0].get("url") or "")
    return ""


async def fetch_audio_url(
    bvid: str,
    cid: int | str | None,
    sessdata: str = "",
    bili_jct: str = "",
    timeout: int = 20,
) -> str:
    if not bvid or not cid:
        return ""
    cookie = ""
    if sessdata:
        cookie = f"SESSDATA={sessdata}"
        if bili_jct:
            cookie += f"; bili_jct={bili_jct}"
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    params = {
        "bvid": str(bvid),
        "cid": str(cid),
        "fnval": "16",
        "fnver": "0",
        "fourk": "0",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(
            "https://api.bilibili.com/x/player/playurl", params=params, headers=headers
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    url = pick_audio_url(payload)
    host = (urlsplit(url).hostname or "").lower()
    if not url or not _host_ok(host):
        return ""
    return url


async def download_audio(
    url: str,
    dest: str,
    sessdata: str = "",
    bili_jct: str = "",
    timeout: int = 180,
) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if not _host_ok(host):
        raise ValueError(f"音频地址不在 B站域名内：{host or url[:60]}")
    headers = dict(HEADERS)
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}" + (
            f"; bili_jct={bili_jct}" if bili_jct else ""
        )
    total = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise ValueError("音频超过 200MB，放弃转写")
                    fh.write(chunk)
    return dest


def convert_to_wav(src: str, dest: str, max_seconds: int, ffmpeg: str | None = None) -> str:
    """转 16k 单声道 wav；max_seconds<=0 表示不截断。"""
    exe = ffmpeg or shutil.which("ffmpeg")
    if not exe:
        bundled = str(
            Path.home()
            / ".astrbot_launcher"
            / "components"
            / "ffmpeg"
            / "bin"
            / "ffmpeg.exe"
        )
        exe = bundled if os.path.isfile(bundled) else ""
    if not exe:
        raise RuntimeError("找不到 ffmpeg，无法抽取音频")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", str(int(max_seconds))]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-f", "wav", dest]
    subprocess.run(
        cmd,
        check=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not os.path.isfile(dest) or os.path.getsize(dest) < 1000:
        raise RuntimeError("音频转换后为空")
    return dest
