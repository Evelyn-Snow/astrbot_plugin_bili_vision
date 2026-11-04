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
from urllib.parse import urlsplit

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
    # 雪碧图地址来自 B站接口，仍限制在 B站图片域名内（防接口被污染时读任意地址）
    host = (urlsplit(url).hostname or "").lower()
    if not (host.endswith("hdslb.com") or host.endswith("bilibili.com")):
        raise ValueError(f"拒绝下载非 B站域名资源：{host or url[:60]}")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())
    return dest


_BILI_HOSTS = ("bilibili.com", "b23.tv")


def _host_ok(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in _BILI_HOSTS)


def resolve_video_id(url: str, timeout: int = 15) -> tuple[str, str]:
    """把各种 B站链接解析为 (类型, ID)：('bvid', 'BV...') 或 ('aid', '123')。

    支持 b23.tv 短链（手动跟随跳转，逐跳校验域名）、bilibili.com/video/BVxxx、av号。
    只允许 B站域名，避免被诱导访问内网或任意站点。
    """
    text = str(url or "").strip()
    match = _BV_RE.search(text)
    if match:
        return "bvid", match.group(1)
    match = _AV_RE.search(text)
    if match:
        return "aid", match.group(1)
    if "b23.tv" in text:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("短链协议不被允许")
        if not _host_ok(parsed.hostname or ""):
            raise ValueError(f"短链域名不在允许范围（{parsed.hostname or '未知'}）")
        current = text if "://" in text else "https://" + text
        for _ in range(5):
            req = urllib.request.Request(current, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
            host = (urlsplit(final).hostname or "").lower()
            if not _host_ok(host):
                raise ValueError(f"短链跳转到了非 B站域名（{host or '未知'}），已停止")
            match = _BV_RE.search(final)
            if match:
                return "bvid", match.group(1)
            match = _AV_RE.search(final)
            if match:
                return "aid", match.group(1)
            if final == current:
                break
            current = final
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


# ---------------- 后台看视频的进度/失败文案 ----------------

PROGRESS_LINES = (
    "还没完呢……我眼睛都看花了，你挑的视频也太长了吧。",
    "这都多久了，进度条才爬了一半，急死我了。",
    "唔……好长啊，我快坐不住了，腿都麻了。",
    "看到一半我脑子有点转不动了，信息量也太大了。",
    "这人说话怎么慢悠悠的，我都替他着急。",
    "我还在看哦，别催别催，这么长总得给我点时间吧。",
    "哇，这个是真的长，我看得都快犯困了……不过快看到重点了。",
    "我跟你讲，这视频长得有点离谱，但我还是想知道结尾。",
    "哎这个有点意思，我再看会儿。",
    "等下等下，这里好像有点东西，我先看完。",
    "有点好看欸，我都有点舍不得快进了。",
)

FAIL_LINES_LONG = (
    "这个视频我实在看不下去了，有点长，先不看了。",
    "看着看着有点累了，回头再说吧。",
    "这个太长，我听到一半就走神了，先这样吧。",
)

FAIL_LINES_NO_SPEECH = (
    "这视频好像没什么人说话，我光看画面了。",
    "听了一圈基本是音乐和音效，没听到能讲的台词。",
    "这视频没多少对白，我只能看画面猜，先不瞎讲了。",
)

FAIL_LINES_TECH = (
    "这个我这边没打开，等会儿再试试。",
    "声音没取到，我暂时看不了，先不硬说了。",
    "这个我搞不定，没听清，先这样吧。",
)


def pick_fail_line(reason: str, duration: float, rng=None) -> str:
    """按失败原因选一句合适的话，别让短片也说“太长”。"""
    import random as _random

    rng = rng or _random
    reason = str(reason or "")
    if "转写内容太少" in reason or "没说话" in reason:
        return rng.choice(FAIL_LINES_NO_SPEECH)
    if "超时" in reason:
        return rng.choice(FAIL_LINES_LONG)
    return rng.choice(FAIL_LINES_TECH)


def plan_progress(duration: float, rng=None) -> list[float]:
    """规划随机进度消息：最多 2 次，多数 0/1 次，首次至少 180 秒。

    duration 小于 8 分钟基本不插话；返回的是相对开始观看的秒数。
    """
    import random as _random

    rng = rng or _random
    if float(duration or 0) < 480:
        return []
    count = rng.choices([0, 1, 2], weights=[0.5, 0.4, 0.1], k=1)[0]
    delays: list[float] = []
    elapsed = 0.0
    for index in range(count):
        elapsed += rng.uniform(180, 420) if index == 0 else rng.uniform(240, 600)
        delays.append(elapsed)
    return delays


# 本机 small/int8 实测：12 分钟视频约 5 分钟跑完，取 0.4 实时率 + 12 秒固定开销。
ASR_RTF_ESTIMATE = 0.4
ASR_FIXED_OVERHEAD = 12.0
LONG_WAIT_SECONDS = 60.0


def estimate_asr_seconds(duration: float) -> float:
    """按视频时长估算后台听音频要多久（秒）。"""
    return max(0.0, float(duration or 0)) * ASR_RTF_ESTIMATE + ASR_FIXED_OVERHEAD


def is_long_wait(duration: float) -> bool:
    """预计超过一分钟才听完时，才值得先打个“有点长”的招呼。"""
    return estimate_asr_seconds(duration) > LONG_WAIT_SECONDS
