from __future__ import annotations

import base64
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI


class ElevenLabsQuotaError(Exception):
    pass


OPENAI_VALID_VOICES = {
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
}


@dataclass
class TtsChunk:
    audio: str
    provider: str
    text: str


def strip_urls_for_tts(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://[^\s)\]>\"\']+", "링크 참조", text)
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    text = re.sub(r"(?m)^[-*]\s+", "", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    if not text or not text.strip():
        return []

    urls = re.findall(r"https?://[^\s)\]>\"\']+", text)
    placeholder_map = {}
    protected = text
    for i, url in enumerate(urls):
        placeholder = f"__URL{i}__"
        placeholder_map[placeholder] = url
        protected = protected.replace(url, placeholder, 1)

    parts = re.split(r"(?<=[.!?。])\s*", protected.strip())
    merged: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if merged and len(part) < 10:
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)

    result = []
    for sentence in merged if merged else [protected.strip()]:
        for placeholder, url in placeholder_map.items():
            sentence = sentence.replace(placeholder, url)
        result.append(sentence)
    return result


class VoiceService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.openai_client = (
            AsyncOpenAI(api_key=settings.openai_api_key)
            if settings.openai_api_key
            else None
        )

    async def speech_to_text(
        self,
        audio_bytes: bytes,
        file_ext: str = "m4a",
        mime_type: str = "audio/mp4",
        language: str | None = "ko",
        prompt: str | None = None,
    ) -> str:
        if self.openai_client is None:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다")

        suffix = f".{file_ext.lstrip('.')}" if file_ext else ".m4a"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        try:
            with open(tmp_path, "rb") as audio_file:
                req: dict[str, Any] = {
                    "file": (f"audio{suffix}", audio_file, mime_type),
                    "model": "whisper-1",
                }
                if language:
                    req["language"] = language
                if prompt:
                    req["prompt"] = prompt
                result = await self.openai_client.audio.transcriptions.create(**req)
            return result.text
        finally:
            os.unlink(tmp_path)

    async def text_to_speech_elevenlabs(
        self, text: str, voice_id: str | None = None
    ) -> str:
        api_key = self.settings.elevenlabs_api_key
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY가 설정되지 않았습니다")

        vid = (
            (voice_id or self.settings.elevenlabs_voice_id or "").strip()
            or self.settings.elevenlabs_voice_id
        )
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)

        if response.status_code in (401, 402, 429):
            raise ElevenLabsQuotaError(
                f"ElevenLabs quota/auth 제한(status={response.status_code})"
            )
        if not response.is_success:
            raise RuntimeError(
                f"ElevenLabs TTS 오류(status={response.status_code}): "
                f"{response.text[:300]}"
            )
        return base64.b64encode(response.content).decode()

    async def text_to_speech_openai(
        self, text: str, hd: bool = True, voice: str = "alloy"
    ) -> str:
        if self.openai_client is None:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다")
        safe_voice = voice if voice in OPENAI_VALID_VOICES else "alloy"
        resp = await self.openai_client.audio.speech.create(
            model="tts-1-hd" if hd else "tts-1",
            voice=safe_voice,
            input=text,
            response_format="mp3",
        )
        return base64.b64encode(resp.content).decode()

    async def text_to_speech(
        self,
        text: str,
        preferred_provider: str | None = None,
        voice: str | None = None,
    ) -> TtsChunk:
        provider = (
            preferred_provider or self.settings.default_tts_provider or "openai-hd"
        ).strip().lower()
        chains: dict[str, list[str]] = {
            "elevenlabs": ["elevenlabs", "openai-hd", "openai"],
            "openai": ["openai", "openai-hd", "elevenlabs"],
            "openai-hd": ["openai-hd", "openai", "elevenlabs"],
        }
        chain = chains.get(provider, chains["openai-hd"])

        last_error: Exception | None = None
        for item in chain:
            try:
                if item == "elevenlabs":
                    return TtsChunk(
                        audio=await self.text_to_speech_elevenlabs(
                            text, voice_id=voice
                        ),
                        provider="elevenlabs",
                        text=text,
                    )
                if item == "openai":
                    return TtsChunk(
                        audio=await self.text_to_speech_openai(
                            text, hd=False, voice=(voice or "alloy")
                        ),
                        provider="openai",
                        text=text,
                    )
                return TtsChunk(
                    audio=await self.text_to_speech_openai(
                        text, hd=True, voice=(voice or "alloy")
                    ),
                    provider="openai-hd",
                    text=text,
                )
            except ElevenLabsQuotaError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"TTS failed: {last_error}")

    async def synthesize_chunks(
        self,
        text: str,
        preferred_provider: str | None,
        voice: str | None,
    ) -> list[TtsChunk]:
        tts_text = strip_urls_for_tts(text)
        return [
            await self.text_to_speech(sentence, preferred_provider, voice)
            for sentence in split_sentences(tts_text)
        ]
