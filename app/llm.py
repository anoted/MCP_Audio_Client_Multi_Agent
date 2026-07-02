"""Streaming client for any OpenAI-compatible chat-completions endpoint."""
import re
from functools import lru_cache
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from .config import settings


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "not-set",
    )


async def chat_stream(
    messages: list[dict], tools: list[dict] | None
) -> AsyncIterator[dict[str, Any]]:
    """Yield {"type": "delta", "text"} events, then one final
    {"type": "end", "finish_reason", "tool_calls"} event.

    Tool-call deltas are accumulated across chunks and returned assembled.
    """
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    stream = await _client().chat.completions.create(**kwargs)
    pending: dict[int, dict] = {}
    finish_reason = None
    async for chunk in stream:
        if not chunk.choices:
            continue  # some providers emit usage-only chunks
        choice = chunk.choices[0]
        delta = choice.delta
        if delta and delta.content:
            yield {"type": "delta", "text": delta.content}
        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                slot = pending.setdefault(
                    tc.index, {"id": "", "name": "", "arguments": ""}
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    tool_calls = [pending[i] for i in sorted(pending)]
    yield {"type": "end", "finish_reason": finish_reason, "tool_calls": tool_calls}


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_NOISE = re.compile(r"[*_`#]+")


def clean_for_tts(text: str) -> str:
    """Strip markdown decoration so the TTS voice doesn't read symbols aloud."""
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Groups streamed LLM deltas into sentence-sized chunks for low-latency TTS."""

    BOUNDARY = re.compile(r"[.!?;:][\"'\)\]]?\s+|\n+")

    def __init__(self, min_len: int = 40):
        self.min_len = min_len
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        return self._extract()

    def flush(self) -> list[str]:
        out = self._extract()
        tail = clean_for_tts(self.buf)
        self.buf = ""
        if tail:
            out.append(tail)
        return out

    def _extract(self) -> list[str]:
        out = []
        while True:
            cut = None
            for m in self.BOUNDARY.finditer(self.buf):
                if m.end() >= self.min_len:
                    cut = m.end()
                    break
            if cut is None:
                break
            sentence = clean_for_tts(self.buf[:cut])
            self.buf = self.buf[cut:]
            if sentence:
                out.append(sentence)
        return out
