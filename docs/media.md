# 对话媒体输入

微信、Telegram、QQ 官方和飞书应用的入站图片、语音、视频进入现有对话队列。它们不是新的订阅源；只有已选定目标和可回复的消息才会处理。平台消息 ID 负责去重，会话隔离与文本相同。出站仍是文本回复，未接入图片生成、视频生成或语音合成。

| 平台 | 入站引用 | 下载与转换 |
| --- | --- | --- |
| 微信 | image_item / voice_item / video_item | 官方 CDN 下载，必要时 AES-128-ECB 解密；平台语音转写优先，未转写的音频进入本地转写，SILK 如本机 ffmpeg 不支持会明确失败 |
| Telegram | photo / voice / audio / video 的 file_id | getFile 获取路径，沿用机器人全局代理，按文件大小上限下载 |
| QQ 官方 | 消息附件 MIME 与 URL | URL 逐跳经过公网地址验证；仅被动回复，不主动推送 |
| 飞书应用 | 图片 key、音视频 file key | 官方消息资源 API；群消息仍须 @机器人 |

接收时仅将小型媒体引用加密入库，不把 CDN URL、密钥或文件内容返回管理接口。处理任务下载媒体后暂存于内存（视频解码使用进程临时目录并自动清理）；图片转为最多 1280 像素的 JPEG，语音由 ffmpeg 转 16 kHz 单声道 WAV，再由本地 faster-whisper-tiny 在 CPU 上转写为文字，视频最多取开头和第 3 秒两帧。模型只能据这些实际证据回答，不能推断未抽取的片段。图片 8 MB、语音 10 MB、视频 20 MB；单条最多三个媒体附件。下载、解密、解码、转写或模型格式不支持时记录原因并发出明确的文字回复，不假装识别成功。ffmpeg 是语音和视频处理的运行前提。

安装依赖后运行 `python scripts/install-stt-model.py`，下载固定版本的轻量转写权重到忽略的 `state/models/faster-whisper-tiny/`，约 75 MB。运行时不会自动下载模型；缺少权重时，未获平台转写的语音会明确失败。本机已安装并以合成英文口语识别出“42”；中文及噪声环境仍需实际样本验收。

现有 gpt-6-sol 对话模型能够处理合成图片和两帧视频画面；虽然音频请求也返回成功，但曾把“forty two”答成“7”，因此未转写语音不再直接交给对话模型。三个既有 bot 的配置在隔离数据库中分别完成图片、语音、视频处理和截获投递；Telegram 的合成 Ogg/Opus 口语经本地转写后，真实对话模型回答“42”。这不是实际平台的媒体消息验收。历史问答保留文本与回答，媒体二进制不长期保存。媒体链接过期时重新发送原消息；平台接受回复不证明用户已读。

参考：Telegram Bot API 的 [Message / getFile](https://core.telegram.org/bots/api)，腾讯 [openclaw-weixin 的媒体协议和 CDN 解码实现](https://github.com/Tencent/openclaw-weixin/tree/main/src)，飞书 [消息资源 API](https://open.feishu.cn/document/server-docs/im-v1/message-resource/get)。真实入站验收需要用户从对应平台发送图片、视频和语音；从服务端合成一个入站记录不是平台端到端验证。
