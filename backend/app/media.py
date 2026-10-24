"""Bounded, transient media decoding for inbound bot conversations."""

import asyncio
import base64
import io
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageOps, UnidentifiedImageError

from .channels import PlatformRejected

MAX_BYTES = {"image": 8_000_000, "voice": 10_000_000, "video": 20_000_000}
Image.MAX_IMAGE_PIXELS = 24_000_000


def media_types(attachments):
    if not isinstance(attachments, list) or len(attachments) > 3:
        raise ValueError("Invalid media attachment count")
    result = []
    for item in attachments:
        if not isinstance(item, dict) or item.get("type") not in MAX_BYTES:
            raise ValueError("Unsupported media attachment")
        if len(str(item)) > 4500:
            raise ValueError("Media reference too large")
        result.append(item["type"])
    return result


def _jpeg(data):
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 8_000_000:
                raise ValueError("Image exceeds pixel budget")
            image = ImageOps.exif_transpose(original)
            image.thumbnail((1280, 1280))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=78, optimize=True)
            value = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid or oversized image") from exc
    if len(value) > 2_000_000:
        raise ValueError("Image exceeds model budget")
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(value).decode()},
    }


def _ffmpeg(data, kind):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("Video/voice processing requires ffmpeg on the service host")
    if kind == "voice":
        proc = subprocess.run(
            [
                executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-t",
                "60",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            timeout=25,
            check=False,
        )
        if proc.returncode or not 44 < len(proc.stdout) <= 2_100_000:
            raise ValueError("Voice cannot be decoded as audio")
        return proc.stdout
    frames = []
    with tempfile.TemporaryDirectory(prefix="workbench-video-") as folder:
        path = Path(folder) / "input.mp4"
        path.write_bytes(data)
        for second in (0, 3):
            proc = subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(second),
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=960:-2",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0 and 0 < len(proc.stdout) <= 2_000_000:
                frames.append(_jpeg(proc.stdout))
    if not frames:
        raise ValueError("Video has no decodable frames")
    return frames


class MediaProcessor:
    def __init__(self, network, channels, root=None):
        self.net = network
        self.channels = channels
        self.stt_path = Path(root or Path.cwd()) / "state/models/faster-whisper-tiny"
        self._transcriber = None
        self._transcriber_lock = threading.Lock()

    def transcribe(self, data):
        with self._transcriber_lock:
            if self._transcriber is None:
                if not all(
                    (self.stt_path / name).is_file()
                    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
                ):
                    raise ValueError("Local speech model is not installed")
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise ValueError("Speech transcription dependency is missing") from exc
                self._transcriber = WhisperModel(
                    str(self.stt_path), device="cpu", compute_type="int8", num_workers=1
                )
            wav = _ffmpeg(data, "voice")
            segments, _ = self._transcriber.transcribe(
                io.BytesIO(wav),
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            if not text:
                raise ValueError("No speech detected in voice message")
            if len(text) > 4000:
                raise ValueError("Voice transcription exceeds conversation budget")
            return text

    async def fetch(self, kind, config, item):
        media_kind = item["type"]
        limit = MAX_BYTES[media_kind]
        if kind == "telegram":
            file_id = item.get("file_id", "")
            if not isinstance(file_id, str) or not 1 <= len(file_id) <= 256:
                raise ValueError("Invalid Telegram file ID")
            result = await self.channels.telegram_request(
                config, "GET", "getFile?" + urlencode({"file_id": file_id})
            )
            file = result.get("result") or {}
            path = file.get("file_path", "")
            if (
                not result.get("ok")
                or not isinstance(path, str)
                or not re.fullmatch(r"[A-Za-z0-9_./-]{1,250}", path)
                or ".." in path.split("/")
                or path.startswith("/")
            ):
                raise ValueError("Telegram did not return a valid file path")
            size = file.get("file_size") or 0
            if not isinstance(size, int) or size < 0 or size > limit:
                raise ValueError("Media exceeds download budget")
            url = "https://api.telegram.org/file/bot" + config["token"] + "/" + path
            proxy = self.channels.proxy_config(config) if self.channels.proxy_config else None
            data, _, _ = await self.net.request("GET", url, limit=limit, proxy=proxy)
        elif kind == "weixin":
            media = item.get("media") or {}
            if not isinstance(media, dict):
                raise ValueError("Invalid Weixin media reference")
            url = media.get("full_url")
            if not url:
                param = media.get("encrypt_query_param", "")
                if not isinstance(param, str) or not 1 <= len(param) <= 2048:
                    raise ValueError("Weixin CDN reference is missing")
                url = "https://novac2c.cdn.weixin.qq.com/c2c/download?" + urlencode(
                    {"encrypted_query_param": param}
                )
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "novac2c.cdn.weixin.qq.com"
                or parsed.port not in (None, 443)
                or not parsed.path.startswith("/c2c/")
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("Untrusted Weixin CDN URL")
            data, _, _ = await self.net.request("GET", url, limit=limit + 16)
            key = item.get("aeskey") or media.get("aes_key")
            if key:
                if item.get("aeskey"):
                    try:
                        raw = bytes.fromhex(key)
                    except ValueError as exc:
                        raise ValueError("Invalid Weixin media key") from exc
                else:
                    try:
                        raw = base64.b64decode(key, validate=True)
                        if len(raw) == 32:
                            raw = bytes.fromhex(raw.decode("ascii"))
                    except (ValueError, UnicodeError) as exc:
                        raise ValueError("Invalid Weixin media key") from exc
                if len(raw) != 16 or len(data) % 16:
                    raise ValueError("Invalid Weixin encrypted media")
                decryptor = Cipher(algorithms.AES(raw), modes.ECB()).decryptor()
                plaintext = decryptor.update(data) + decryptor.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                try:
                    data = unpadder.update(plaintext) + unpadder.finalize()
                except ValueError as exc:
                    raise ValueError("Weixin media decryption failed") from exc
        elif kind == "qq":
            url = item.get("url", "")
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError("Invalid QQ media URL")
            data, _, _ = await self.net.request("GET", url, limit=limit)
        elif kind == "feishu":
            key = item.get("file_key", "")
            message_id = item.get("message_id", "")
            if not all(
                isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", v)
                for v in (key, message_id)
            ):
                raise ValueError("Invalid Feishu media reference")
            token = await self.channels.feishu_token(config)
            resource_type = "image" if media_kind == "image" else "file"
            url = (
                "https://open.feishu.cn/open-apis/im/v1/messages/"
                + quote(message_id)
                + "/resources/"
                + quote(key)
                + "?"
                + urlencode({"type": resource_type})
            )
            data, _, _ = await self.net.request(
                "GET", url, headers={"Authorization": "Bearer " + token}, limit=limit
            )
        else:
            raise PlatformRejected("This bot cannot receive media")
        if not data or len(data) > limit:
            raise ValueError("Media is empty or exceeds download budget")
        return data

    async def content(self, kind, config, attachments, text):
        parts = [{"type": "text", "text": text or "请根据附件回答。"}]
        for item in attachments:
            media_kind = item["type"]
            if media_kind == "voice" and item.get("transcript"):
                parts.append(
                    {"type": "text", "text": "语音转写（平台提供）：" + item["transcript"][:4000]}
                )
                continue
            data = await self.fetch(kind, config, item)
            if media_kind == "image":
                parts.append(await asyncio.to_thread(_jpeg, data))
            elif media_kind == "video":
                parts.append(
                    {"type": "text", "text": "视频抽帧（最多开头与第 3 秒，并非整段视频）："}
                )
                parts.extend(await asyncio.to_thread(_ffmpeg, data, "video"))
            else:
                transcript = await asyncio.to_thread(self.transcribe, data)
                parts.append(
                    {
                        "type": "text",
                        "text": "语音转写（本地模型生成，可能有误）：" + transcript,
                    }
                )
        return parts
