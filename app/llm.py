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


# --- reasoning extraction ----------------------------------------------------
# Popular OpenAI-compatible reasoning formats. Servers that parse reasoning
# themselves put it in delta.reasoning_content (DeepSeek API, vLLM, llama.cpp,
# SGLang) or delta.reasoning (OpenRouter, LM Studio); models served raw emit
# inline tag blocks instead. Both become {"type": "reasoning"} events.
_REASONING_TAGS: list[tuple[str, str]] = [
    ("<think>", "</think>"),           # DeepSeek R1 family, Qwen3, QwQ
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
    ("[THINK]", "[/THINK]"),           # Mistral Magistral
    ("<|channel|>analysis<|message|>", "<|end|>"),  # gpt-oss harmony, raw
]
# A close tag without an opener: R1-style templates pre-fill the opening
# <think>, so the completion STARTS inside the block and only closes it.
_ORPHAN_CLOSERS = ("</think>", "</thinking>", "</reasoning>", "[/THINK]")
# Raw harmony markers around the final answer: dropped from visible text.
_STRIP_MARKERS = (
    "<|start|>assistant<|channel|>final<|message|>",
    "<|channel|>final<|message|>",
    "<|return|>",
)


def _partial_suffix(low: str, tokens: list[str]) -> int:
    """Length of the longest buffer suffix that could still become one of
    `tokens` with more streamed text — held back instead of emitted."""
    keep = 0
    for token in tokens:
        t = token.lower()
        for k in range(min(len(t) - 1, len(low)), keep, -1):
            if low.endswith(t[:k]):
                keep = k
                break
    return keep


class ReasoningFilter:
    """Splits streamed assistant content into visible text and reasoning.

    Yields {"type": "delta"} for visible text, {"type": "reasoning"} for text
    inside a thinking block, and {"type": "reasoning_retro"} when an orphan
    close tag reveals that everything visible so far was actually reasoning.
    Tags may arrive split across any number of stream chunks.
    """

    def __init__(self) -> None:
        self.buf = ""
        self.closing: str | None = None  # close tag we are inside, if any
        self.saw_block = False           # a block opened/closed at some point

    def feed(self, text: str) -> list[dict]:
        self.buf += text
        out: list[dict] = []
        progress = True
        while progress:
            progress = (
                self._scan_reasoning(out) if self.closing
                else self._scan_visible(out)
            )
        return out

    def flush(self) -> list[dict]:
        """End of stream: remaining text keeps its current classification
        (models often never close the block before a tool call / stop)."""
        out: list[dict] = []
        if self.buf:
            kind = "reasoning" if self.closing else "delta"
            out.append({"type": kind, "text": self.buf})
        self.buf = ""
        self.closing = None
        return out

    def _tokens(self) -> list[tuple[str, str, str | None]]:
        toks: list[tuple[str, str, str | None]] = [
            (o, "open", c) for o, c in _REASONING_TAGS
        ]
        toks += [(t, "retro", None) for t in _ORPHAN_CLOSERS]
        toks += [(t, "strip", None) for t in _STRIP_MARKERS]
        return toks

    def _scan_visible(self, out: list[dict]) -> bool:
        low = self.buf.lower()
        best: tuple[int, str, str, str | None] | None = None
        for token, kind, close in self._tokens():
            pos = low.find(token.lower())
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, token, kind, close)
        if best is None:
            keep = _partial_suffix(low, [t for t, _, _ in self._tokens()])
            emit = self.buf[: len(self.buf) - keep]
            if emit:
                out.append({"type": "delta", "text": emit})
            self.buf = self.buf[len(self.buf) - keep:]
            return False
        pos, token, kind, close = best
        if pos:
            out.append({"type": "delta", "text": self.buf[:pos]})
        self.buf = self.buf[pos + len(token):]
        if kind == "open":
            self.closing = close
            self.saw_block = True
        elif kind == "retro":
            if not self.saw_block:  # stray closers after a real block: strip
                out.append({"type": "reasoning_retro"})
            self.saw_block = True
        return True

    def _scan_reasoning(self, out: list[dict]) -> bool:
        low = self.buf.lower()
        pos = low.find(self.closing.lower())
        if pos < 0:
            keep = _partial_suffix(low, [self.closing])
            emit = self.buf[: len(self.buf) - keep]
            if emit:
                out.append({"type": "reasoning", "text": emit})
            self.buf = self.buf[len(self.buf) - keep:]
            return False
        if pos:
            out.append({"type": "reasoning", "text": self.buf[:pos]})
        self.buf = self.buf[pos + len(self.closing):]
        self.closing = None
        return True


def strip_reasoning(text: str) -> str:
    """Visible text of a complete (non-streamed) reply, thinking removed."""
    f = ReasoningFilter()
    parts: list[str] = []
    for ev in f.feed(text or "") + f.flush():
        if ev["type"] == "delta":
            parts.append(ev["text"])
        elif ev["type"] == "reasoning_retro":
            parts.clear()
    return "".join(parts).strip()


def _slot_index(tc, pending: dict[int, dict]) -> int:
    """Accumulation slot for a streamed tool-call delta.

    OpenAI sets `index` on every delta; some local endpoints leave it None.
    In that case match by id, else treat a delta that carries an id/name as
    a new call and bare argument fragments as a continuation of the last one.
    """
    if tc.index is not None:
        return tc.index
    if tc.id:
        for i, slot in pending.items():
            if slot["id"] == tc.id:
                return i
    if tc.id or (tc.function and tc.function.name):
        return max(pending, default=-1) + 1
    return max(pending, default=0)


async def chat_stream(
    messages: list[dict], tools: list[dict] | None
) -> AsyncIterator[dict[str, Any]]:
    """Yield {"type": "delta"|"reasoning", "text"} events (visible text vs
    model thinking), possibly {"type": "reasoning_retro"} (see
    ReasoningFilter), then one final {"type": "end", "finish_reason",
    "tool_calls"} event.

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
    rfilter = ReasoningFilter()
    async for chunk in stream:
        if not chunk.choices:
            continue  # some providers emit usage-only chunks
        choice = chunk.choices[0]
        delta = choice.delta
        if delta:
            # Server-parsed reasoning fields, streamed token by token.
            reasoning = getattr(delta, "reasoning_content", None) \
                or getattr(delta, "reasoning", None)
            if isinstance(reasoning, str) and reasoning:
                yield {"type": "reasoning", "text": reasoning}
        if delta and delta.content:
            for ev in rfilter.feed(delta.content):
                yield ev
        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                idx = _slot_index(tc, pending)
                slot = pending.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    name = tc.function.name
                    # Some local endpoints re-send the FULL name on every
                    # chunk; blind += would garble it into "get_pageget_page"
                    # and the model would retry the "failed" call.
                    if name and name != slot["name"]:
                        slot["name"] += name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    for ev in rfilter.flush():
        yield ev
    tool_calls = [pending[i] for i in sorted(pending)]
    yield {"type": "end", "finish_reason": finish_reason, "tool_calls": tool_calls}


async def complete(messages: list[dict], temperature: float = 0.0) -> str:
    """One-shot non-streaming completion (used by the initiator). Inline
    thinking blocks are stripped so consumers see only the answer."""
    resp = await _client().chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=temperature,
    )
    return strip_reasoning(resp.choices[0].message.content or "")


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
