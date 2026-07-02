"""NVIDIA NIM speech services (Riva gRPC): Parakeet ASR + Magpie TTS.

All functions here are blocking gRPC calls; callers run them in a thread
(asyncio.to_thread) so the event loop stays responsive and interruptions work.
"""
import threading
from functools import lru_cache
from typing import Iterator

import riva.client

from .config import settings


def _auth(server: str, function_id: str) -> "riva.client.Auth":
    metadata = []
    if function_id:
        metadata.append(["function-id", function_id])
    if settings.nvidia_api_key:
        metadata.append(["authorization", f"Bearer {settings.nvidia_api_key}"])
    return riva.client.Auth(
        uri=server,
        use_ssl=settings.riva_use_ssl,
        metadata_args=metadata,
    )


@lru_cache(maxsize=1)
def _asr_service() -> "riva.client.ASRService":
    return riva.client.ASRService(_auth(settings.asr_server, settings.asr_function_id))


@lru_cache(maxsize=1)
def _tts_service() -> "riva.client.SpeechSynthesisService":
    return riva.client.SpeechSynthesisService(
        _auth(settings.tts_server, settings.tts_function_id)
    )


def transcribe(pcm16: bytes) -> str:
    """Offline-recognize one utterance of 16 kHz mono 16-bit PCM."""
    if not settings.speech_configured:
        raise RuntimeError(
            "Speech is not configured: set NVIDIA_API_KEY in .env "
            "(or point RIVA_ASR_SERVER at a local NIM container)."
        )
    config = riva.client.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=settings.asr_sample_rate,
        language_code=settings.asr_language,
        max_alternatives=1,
        enable_automatic_punctuation=True,
        audio_channel_count=1,
    )
    response = _asr_service().offline_recognize(pcm16, config)
    parts = [
        result.alternatives[0].transcript
        for result in response.results
        if result.alternatives
    ]
    return " ".join(p.strip() for p in parts).strip()


def synthesize_stream(text: str, stop: threading.Event) -> Iterator[bytes]:
    """Stream raw PCM16 chunks for `text`; bails out quickly when `stop` is set."""
    if not settings.speech_configured:
        raise RuntimeError("Speech is not configured: set NVIDIA_API_KEY in .env.")
    responses = _tts_service().synthesize_online(
        text,
        voice_name=settings.tts_voice,
        language_code=settings.tts_language,
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hz=settings.tts_sample_rate,
    )
    for resp in responses:
        if stop.is_set():
            break
        if resp.audio:
            yield resp.audio
