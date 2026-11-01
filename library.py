"""B站视频画面理解的纯逻辑：链接解析、视频信息、缩略图拼图（不依赖 AstrBot）。

B站公开的 videoshot 接口会返回视频的缩略图雪碧图（默认 10x10 帧），
无需下载视频即可抽取画面，再交给视觉模型理解。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)


def _get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _download(url: str, dest: str, timeout: int = 30) -> str:
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())
    return dest


def resolve_video_id(url: str, timeout: int = 15) -> tuple[str, str]:
    """把各种 B站链接解析为 (类型, ID)：('bvid', 'BV...') 或 ('aid', '123')。

    支持 b23.tv 短链（跟随跳转）、bilibili.com/video/BVxxx、av号。
    """
    text = str(url or "").strip()
    match = _BV_RE.search(text)
    if match:
        return "bvid", match.group(1)
    match = _AV_RE.search(text)
    if match:
        return "aid", match.group(1)
    if "b23.tv" in text:
        req = urllib.request.Request(text, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
        match = _BV_RE.search(final)
        if match:
            return "bvid", match.group(1)
        match = _AV_RE.search(final)
        if match:
            return "aid", match.group(1)
    raise ValueError("链接里没有识别到 BV 号或 av 号")


def fetch_video_info(kind: str, vid: str, timeout: int = 20) -> dict:
    query = urllib.parse.urlencode({kind: vid})
    data = _get_json(f"https://api.bilibili.com/x/web-interface/view?{query}", timeout)
    if data.get("code") != 0:
        raise ValueError(f"B站接口返回错误：{data.get('message') or data.get('code')}")
    info = data.get("data") or {}
    return {
        "bvid": info.get("bvid") or "",
        "aid": info.get("aid"),
        "cid": info.get("cid"),
        "title": info.get("title") or "",
        "desc": info.get("desc") or "",
        "duration": info.get("duration") or 0,
        "owner": (info.get("owner") or {}).get("name") or "",
        "pic": info.get("pic") or "",
    }


def fetch_videoshot(info: dict, timeout: int = 20) -> dict:
    if not info.get("bvid") or not info.get("cid"):
        return {}
    data = _get_json(
        "https://api.bilibili.com/x/player/videoshot?"
        + urllib.parse.urlencode({"bvid": info["bvid"], "cid": info["cid"], "index": 1}),
        timeout,
    )
    if data.get("code") != 0:
        return {}
    return data.get("data") or {}


def _time_label(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"t={total // 60:02d}:{total % 60:02d}"


def build_contact_sheet(
    sprite_paths: list[str],
    out_path: str,
    *,
    cols: int = 3,
    rows: int = 3,
    duration: float = 0,
    grid: tuple[int, int] = (10, 10),
    timestamps: list | None = None,
    min_brightness: float = 20.0,
) -> str:
    """把多张雪碧图里的帧抽成一张 3x3 拼图，带时间标签。

    B站雪碧图固定 10x10 网格，但视频帧数往往不足 100，空余格子是纯黑。
    因此先按亮度过滤黑格，再在有效帧里均匀抽样。
    """
    from PIL import Image, ImageDraw  # 延迟导入：测试环境没有 Pillow 时不炸

    total_cells = cols * rows
    x_len, y_len = max(1, int(grid[0])), max(1, int(grid[1]))
    valid: list[tuple[int, object]] = []  # (全局序号, 图像)
    for sheet_index, path in enumerate(sprite_paths):
        img = Image.open(path).convert("RGB")
        w, h = img.size
        fw, fh = w // x_len, h // y_len
        for cy in range(y_len):
            for cx in range(x_len):
                box = (cx * fw, cy * fh, (cx + 1) * fw, (cy + 1) * fh)
                frame = img.crop(box)
                # 用缩略图的平均亮度判断是否黑格
                small = frame.convert("L").resize((16, 9))
                pixels = list(small.getdata())
                brightness = sum(pixels) / max(1, len(pixels))
                if brightness < min_brightness:
                    continue
                valid.append((sheet_index * (x_len * y_len) + cy * x_len + cx, frame))

    if not valid:
        valid = [(0, Image.open(sprite_paths[0]).convert("RGB"))]

    if len(valid) <= total_cells:
        chosen = valid
    else:
        step = (len(valid) - 1) / max(1, total_cells - 1)
        chosen = [valid[round(i * step)] for i in range(total_cells)]

    total_valid = max(1, len(valid))
    stamp_list = [float(x) for x in (timestamps or []) if isinstance(x, (int, float))]
    picks: list[tuple[float, object]] = []
    for order, (global_index, frame) in enumerate(chosen):
        if len(stamp_list) > global_index:
            seconds = stamp_list[global_index]
        elif stamp_list:
            seconds = stamp_list[min(order, len(stamp_list) - 1)]
        else:
            seconds = duration * (order + 0.5) / total_valid
        picks.append((seconds, frame))
    cell_w, cell_h = 320, 180
    label_h = 20
    canvas = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (12, 16, 26))
    draw = ImageDraw.Draw(canvas)
    for index, (seconds, frame) in enumerate(picks):
        row, col = divmod(index, cols)
        x = col * cell_w
        y = row * (cell_h + label_h)
        canvas.paste(frame.resize((cell_w, cell_h)), (x, y))
        draw.text((x + 6, y + cell_h + 2), _time_label(seconds), fill=(200, 210, 225))
    canvas.save(out_path, "JPEG", quality=88)
    return out_path


def extract_frames(
    videoshot: dict,
    duration: float,
    workdir: str | None = None,
    *,
    cols: int = 3,
    rows: int = 3,
    min_brightness: float = 20.0,
) -> str:
    """下载雪碧图并生成拼图，返回拼图路径。"""
    images = videoshot.get("image") or []
    if not images:
        raise ValueError("这个视频没有可用的画面缩略图")
    workdir = workdir or tempfile.mkdtemp(prefix="bili_vision_")
    os.makedirs(workdir, exist_ok=True)
    sprite_paths = []
    for idx, item in enumerate(images[:3]):
        dest = os.path.join(workdir, f"sprite_{idx}.jpg")
        _download(str(item), dest)
        sprite_paths.append(dest)
    out = os.path.join(workdir, "contact_sheet.jpg")
    grid = (int(videoshot.get("img_x_len") or 10), int(videoshot.get("img_y_len") or 10))
    return build_contact_sheet(
        sprite_paths,
        out,
        cols=cols,
        rows=rows,
        duration=duration,
        grid=grid,
        timestamps=videoshot.get("index"),
        min_brightness=min_brightness,
    )
