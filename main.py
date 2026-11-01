"""B站视频画面理解：不下载视频，用官方缩略图让她“看”到画面。

用户分享 B站视频时，调用 bilibili_watch_video：解析 BV 号 → 取视频信息和
缩略图雪碧图 → 拼成 3x3 画面 → 交给视觉模型描述 → 返回给主模型自然回应。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .library import extract_frames, fetch_video_info, fetch_videoshot, resolve_video_id

NAME = "astrbot_plugin_bili_vision"


class BiliVision(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

    def _vision_provider(self):
        pid = str(self.conf.get("vision_provider_id") or "").strip()
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
                if provider is not None:
                    return provider
            except Exception:
                pass
        # 未配置或配置的 id 不存在时，回退到当前使用中的聊天提供商。
        try:
            provider = self.context.get_using_provider()
            if provider is not None:
                return provider
        except Exception:
            pass
        return None

    async def _describe(self, info: dict, sheet: str) -> str:
        provider = self._vision_provider()
        if provider is None:
            return "（没有可用的视觉模型，无法描述画面）"
        prompt = (
            "这是一个 B站视频按时间顺序抽取的 9 个画面（左上到右下，带时间标签）。\n"
            f"视频标题：{info.get('title') or '未知'}\n"
            f"视频简介：{(info.get('desc') or '无')[:300]}\n"
            "请描述这个视频大概讲了什么：画面场景、人物或物体、画面里的文字、发生的事和情绪。\n"
            "要求：用中文，4-8 句；只描述看得见的内容，看不清的不要编造；"
            "不要提到“拼图”“截图”“视觉模型”这些词。"
        )
        result = await provider.text_chat(prompt=prompt, image_urls=[sheet])
        return str(getattr(result, "completion_text", "") or "").strip()

    @filter.on_llm_request()
    async def hint_bili_video(self, event: AstrMessageEvent, req):
        text = str(getattr(event, "message_str", "") or "")
        lowered = text.lower()
        if not any(k in lowered for k in ("bilibili.com", "b23.tv", "bv1", "bv2")):
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n[视频说明] 这条消息包含 B站视频链接。用户问视频内容、讲了什么、好不好看时，"
            "调用 bilibili_watch_video 看画面（不需要 Cookie，直接可用）。"
            "不要调用需要 Cookie 的 bilibili_read，也不要用 analyze_webpage 分析视频页，除非用户明确要字幕原文。"
            "看完画面后用你自己的口吻自然回应，不要罗列分镜或提到工具。"
        )

    @filter.llm_tool(name="bilibili_watch_video")
    async def bilibili_watch_video(
        self,
        event: AstrMessageEvent,
        url: str,
        question: str = "",
    ) -> str:
        """“看”一个 B站视频的画面：抽取视频多个画面并理解内容，用于回答视频讲了什么。
        用户分享 B站视频链接（bilibili.com/video/BV... 或 b23.tv 短链）并问内容、
        问好不好看、让你评价时调用。只能看画面和标题简介，没有字幕细节。

        Args:
            url(string): B站视频链接或 BV 号
            question(string): 用户关于这个视频的具体问题，可留空
        """
        workdir = tempfile.mkdtemp(prefix="bili_vision_")
        try:
            kind, vid = await asyncio.to_thread(resolve_video_id, url)
            info = await asyncio.to_thread(fetch_video_info, kind, vid)
            shot = await asyncio.to_thread(fetch_videoshot, info)
            if not shot:
                return (
                    f"视频《{info.get('title') or vid}》没有可用的画面缩略图，"
                    f"只能看到标题和简介：{(info.get('desc') or '无')[:300]}。不要编造画面内容。"
                )
            sheet = await asyncio.to_thread(
                extract_frames,
                shot,
                float(info.get("duration") or 0),
                workdir,
                cols=max(2, min(5, int(self.conf.get("grid_cols") or 3))),
                rows=max(2, min(5, int(self.conf.get("grid_rows") or 3))),
                min_brightness=max(
                    0.0, min(100.0, float(self.conf.get("min_brightness") or 20.0))
                ),
            )
            description = await self._describe(info, sheet)
            if not description:
                return f"视频《{info.get('title') or vid}》的画面没能识别出来，不要编造。"
            header = (
                f"视频：{info.get('title') or vid}\n"
                f"UP主：{info.get('owner') or '未知'}　时长：{int(info.get('duration') or 0)} 秒\n"
            )
            if question:
                header += f"用户问题：{question}\n"
            return header + "画面内容：\n" + description
        except Exception as exc:
            logger.warning(f"[{NAME}] 视频画面理解失败: {exc}")
            return f"这个视频暂时看不了（{str(exc)[:80]}），不要编造视频内容。"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
