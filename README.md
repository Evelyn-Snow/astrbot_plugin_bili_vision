# B站视频理解

用户分享 B站视频时，不下载完整视频，而是把「字幕 + 画面 + 标题简介」合在一起理解：

1. 解析 BV 号（支持 bilibili.com/video/BV... 和 b23.tv 短链）
2. 取视频标题、简介、时长、UP 主
3. **优先取 B站字幕**（官方 / CC / AI 字幕，需要 Cookie）：按时间轴整理成文字
4. 取**缩略图雪碧图**（默认 10x10 帧），抽 9 帧拼成带时间标签的 3x3 画面，交给视觉模型描述
5. 返回给主模型，用她自己的口吻把台词内容和画面结合起来回应
6. **没有字幕时**：先按画面回应；同时后台抽音频，用本地 faster-whisper 转写，完成后补一条听后感

没有字幕时先返回画面理解，音频在后台继续转写；没有画面时只返回字幕。两者都没有时只给标题简介，并明确不要编造。

后台听音频时，预计一分钟内能听完的只简单说「等我一下」；更长的才说「这个有点长，我先看看，等会儿跟你说」。
进度消息不按固定时间点发：8 分钟以上的视频随机插 0~2 次（多数 0 或 1 次），第一次至少在看 3 分钟后；
插话会结合已经听到的内容生成，好看就兴奋、拖沓就吐槽，并避开前面说过的话。
转写失败就说看不下去了/累了，不会硬编内容。

## 依赖

- **Pillow**（拼图）
- **bilibili-api-python**（处理 B站字幕接口的 WBI 签名和登录态）
- 一个能看图的模型：配置 `vision_provider_id`，留空回退到当前对话模型
- 可选：单独装好 **faster-whisper** 的 Python 环境 + ffmpeg（音频转写用，不往 AstrBot 主环境塞依赖）

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| vision_provider_id | 空 | 视觉模型 id，留空用当前对话模型 |
| grid_cols / grid_rows | 3 / 3 | 一次看多少帧画面，2~5 |
| min_brightness | 20 | 过滤雪碧图里的纯黑空格 |
| bilibili_cookie | 空 | `{sessdata, id(bili_jct)}`，用于取字幕；只存本机，不写日志 |
| max_subtitle_chars | 5000 | 字幕最多带多少字，超长保留开头 70% + 结尾 30% |
| asr_enabled | false | 没有字幕时是否后台听音频 |
| asr_python | 空 | 装了 faster-whisper 的解释器路径，留空用当前解释器 |
| asr_model | small | Whisper 模型；CPU 上 small int8 实时率约 0.3~0.45 |
| asr_threads | 2 | 转写线程数，留核给 AstrBot 和 QQ |
| asr_compute_type | int8 | CPU 推荐 int8 |
| asr_max_minutes | 15 | 超过就不转写，只根据画面理解 |
| asr_timeout_seconds | 900 | 转写子进程超时，超时回一句看不下去了 |
| asr_language | 空 | 留空自动识别；固定 zh 可能把英文翻成中文 |
| asr_initial_prompt | 空 | 人名、术语提示，帮助写对专有名词 |
| asr_download_root | 空 | Whisper 模型缓存目录 |
| asr_ffmpeg | 空 | ffmpeg 路径，留空自动找 |

## 说明

- 字幕是「视频讲了什么」最便宜、最准确的来源；画面用来补充演示、人物、PPT、画面文字。
- 没有字幕的视频才走本地 ASR：音频转成 16k 单声道 wav，转写完成即删除临时文件。
- 工具名仍然是 `bilibili_watch_video`；安装本插件后建议停用 `biliread`，避免两个 B站工具重复。
