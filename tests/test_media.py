"""Inbound media contracts and Agent handoff without external platform sends."""

import asyncio
import base64
import io
import json
import shutil
import subprocess
import wave
from types import SimpleNamespace as NS

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageDraw
from test_conversation import model
from test_workbench import bot, post

from backend.app.channels import Channels
from backend.app.media import MediaProcessor, _ffmpeg, _jpeg
from backend.app.platform_sockets import feishu_event, qq_event

pytest_plugins = ["test_workbench"]


def picture():
    image = Image.new("RGB", (192, 96), "white")
    ImageDraw.Draw(image).text((15, 20), "TEST 42", fill="black")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_telegram_and_weixin_media_only_messages_are_queued():
    class Network:
        async def json(self, _method, url, *_args, **_kwargs):
            if "getUpdates" in url:
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 9,
                            "message": {
                                "chat": {"id": 42, "type": "private"},
                                "from": {"id": 5},
                                "photo": [{"file_id": "small"}, {"file_id": "large"}],
                            },
                        },
                        {
                            "update_id": 10,
                            "message": {
                                "chat": {"id": 42, "type": "private"},
                                "from": {"id": 5},
                                "voice": {"file_id": "voice"},
                            },
                        },
                        {
                            "update_id": 11,
                            "message": {
                                "chat": {"id": -3, "type": "supergroup"},
                                "from": {"id": 5},
                                "video": {"file_id": "unaddressed"},
                            },
                        },
                    ],
                }
            return {
                "ret": 0,
                "msgs": [
                    {
                        "message_id": "wx1",
                        "from_user_id": "user",
                        "context_token": "ctx",
                        "item_list": [
                            {
                                "type": 2,
                                "image_item": {
                                    "media": {
                                        "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/download?file=1"
                                    },
                                    "aeskey": "11" * 16,
                                },
                            },
                            {"type": 3, "voice_item": {"text": "语音内容", "media": {}}},
                            {"type": 5, "video_item": {"media": {"encrypt_query_param": "opaque"}}},
                        ],
                    }
                ],
            }

    async def check():
        tg, messages = await Channels(Network()).receive(
            "telegram", {"token": "fake", "offset": 0}, True
        )
        assert tg["offset"] == 12
        assert [(m["id"], m["attachments"][0]["type"]) for m in messages] == [
            ("9", "image"),
            ("10", "voice"),
        ]
        assert messages[0]["attachments"][0]["file_id"] == "large"
        _, wx = await Channels(Network()).receive(
            "weixin", {"base_url": "https://ilinkai.weixin.qq.com", "token": "x"}, True
        )
        assert [v["type"] for v in wx[0]["attachments"]] == ["image", "voice", "video"]
        assert wx[0]["text"] == ""

    asyncio.run(check())


def test_weixin_decryption_and_image_conversion_rejects_untrusted_url():
    key = bytes.fromhex("11" * 16)
    padder = padding.PKCS7(128).padder()
    plaintext = padder.update(picture()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = cipher.update(plaintext) + cipher.finalize()

    class Network:
        async def request(self, *_args, **_kwargs):
            return encrypted, "", {}

    media = MediaProcessor(Network(), None)
    item = {"type": "image", "media": {"encrypt_query_param": "opaque"}, "aeskey": key.hex()}

    async def check():
        parts = await media.content("weixin", {}, [item], "图中有什么？")
        assert parts[0]["text"] == "图中有什么？"
        assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]).startswith(
            b"\xff\xd8"
        )
        item["media"]["full_url"] = "https://untrusted.example/c2c/download"
        with pytest.raises(ValueError, match="Untrusted"):
            await media.fetch("weixin", {}, item)

    asyncio.run(check())


def test_telegram_file_validation_and_size_budget():
    class Channel:
        proxy_config = None

        async def telegram_request(self, *_args):
            return {"ok": True, "result": {"file_path": "../private", "file_size": 10}}

    class Network:
        async def request(self, *_args, **_kwargs):
            raise AssertionError("Never request an invalid path")

    async def check():
        with pytest.raises(ValueError, match="file path"):
            await MediaProcessor(Network(), Channel()).fetch(
                "telegram", {"token": "fake"}, {"type": "voice", "file_id": "id"}
            )

    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_voice_and_video_decoding_have_model_parts():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    assert _ffmpeg(stream.getvalue(), "voice").startswith(b"RIFF")
    video = subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:r=1:d=4",
            "-c:v",
            "mpeg4",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        timeout=15,
        check=True,
    ).stdout
    assert len(_ffmpeg(video, "video")) == 2
    assert _jpeg(picture())["type"] == "image_url"


def test_voice_uses_platform_transcript_without_downloading():
    media = MediaProcessor(None, None)

    async def forbidden(*_args):
        raise AssertionError("Platform transcript must not fetch the audio")

    media.fetch = forbidden
    parts = asyncio.run(
        media.content("weixin", {}, [{"type": "voice", "transcript": "原话"}], "这段说了什么")
    )
    assert parts[-1]["text"].endswith("原话")


def test_voice_requires_installed_local_model(tmp_path):
    media = MediaProcessor(None, None, tmp_path)
    with pytest.raises(ValueError, match="Local speech model is not installed"):
        media.transcribe(b"unread")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_voice_uses_local_transcription_not_untested_model_audio(tmp_path):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    media = MediaProcessor(None, None, tmp_path)
    media._transcriber = NS(transcribe=lambda *_args, **_kwargs: ([NS(text="forty two")], None))

    async def fetch(*_args):
        return stream.getvalue()

    media.fetch = fetch
    parts = asyncio.run(media.content("telegram", {}, [{"type": "voice"}], ""))
    assert len(parts) == 2
    assert parts[1]["type"] == "text"
    assert "forty two" in parts[1]["text"]
    media._transcriber = NS(transcribe=lambda *_args, **_kwargs: ([], None))
    with pytest.raises(ValueError, match="No speech detected"):
        media.transcribe(stream.getvalue())


def test_socket_media_refs_only_on_addressed_messages():
    qq = NS(
        id="q1",
        group_openid="g1",
        group_name="Group",
        content="",
        author=NS(member_openid="u1"),
        mentions=[],
        attachments=[NS(content_type="image/png", url="https://cdn.example/a")],
    )
    assert qq_event(qq, "group")["attachments"][0]["type"] == "image"
    assert qq_event(qq, "group", addressed=False)["attachments"] == []
    feishu = NS(
        event=NS(
            message=NS(
                message_id="om_1",
                chat_id="oc_1",
                chat_type="p2p",
                message_type="image",
                content=json.dumps({"image_key": "img_1"}),
                mentions=[],
            ),
            sender=NS(sender_type="user", sender_id=NS(open_id="ou_user")),
        )
    )
    assert feishu_event(feishu, "ou_bot")["attachments"] == [
        {"type": "image", "message_id": "om_1", "file_key": "img_1"}
    ]


def test_existing_bot_queue_keeps_media_private_and_sends_model_reply(wb, server):
    c, w = wb
    b = bot(c, server)
    conversation_model = model(c, server, "dialogue")
    post(c, "models/roles", {"conversation_model_id": conversation_model["id"]})
    ref = {"type": "image", "file_id": "opaque-test-file-id"}
    config = w.store.open(w.store.get("bot", b["id"])["secret"])
    assert w.accept_inbound(
        w.store.get("bot", b["id"]),
        config,
        {
            "id": "media-question",
            "target": "endpoint",
            "sender": "user",
            "text": "",
            "attachments": [ref],
        },
    )
    row = w.store.db.execute("SELECT * FROM inbound_messages").fetchone()
    assert "opaque-test-file-id" not in str(dict(row))
    assert w.store.open(row["attachments"]) == [ref]
    assert not w.accept_inbound(
        w.store.get("bot", b["id"]),
        config,
        {
            "id": "media-question",
            "target": "endpoint",
            "sender": "user",
            "text": "",
            "attachments": [ref],
        },
    )

    async def content(*_args):
        return [{"type": "text", "text": "描述图片"}, _jpeg(picture())]

    seen = []

    async def completion(_config, messages, _tools):
        seen.append(messages[-1]["content"])
        return {"choices": [{"message": {"content": "图片里写着 TEST 42。"}}]}

    w.media.content = content
    w.editor.completion = completion
    assert asyncio.run(w.chat_one())
    assert seen[0][1]["type"] == "image_url"
    assert asyncio.run(w.send_one())
    assert (
        w.store.db.execute("SELECT state FROM inbound_messages").fetchone()[0] == "ADAPTER_ACCEPTED"
    )
    history = c.get("/api/history?kind=inbound_messages").json()["rows"][0]
    assert history["media_types"] == ["image"] and "attachments" not in history
