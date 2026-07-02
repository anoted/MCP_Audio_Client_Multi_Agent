"""Per-WebSocket voice session: ASR -> LLM (with MCP tools) -> TTS pipeline.

Interruption model (barge-in):
- The whole respond pipeline runs as one cancellable asyncio task.
- All outbound traffic funnels through a single sender task so JSON control
  messages and binary TTS audio never interleave mid-send.
- Every response has a generation number; audio chunks are tagged with it and
  the sender drops chunks from stale generations, so audio produced by a
  TTS thread that hasn't noticed the cancellation yet can never reach the
  client after an interrupt.
"""
import asyncio
import contextlib
import json
import threading

from fastapi import WebSocket

from . import llm, speech
from .config import settings
from .llm import SentenceSplitter
from .mcp_manager import MCPManager

MAX_UTTERANCE_BYTES = settings.asr_sample_rate * 2 * 120  # 2 minutes of PCM16


class VoiceSession:
    def __init__(self, ws: WebSocket, mcp: MCPManager):
        self.ws = ws
        self.mcp = mcp
        self.loop = asyncio.get_running_loop()
        self.history: list[dict] = [
            {"role": "system", "content": settings.system_prompt}
        ]
        self.audio_buf = bytearray()
        self.capturing = False
        self.gen = 0  # bumped on every interrupt; stale audio is dropped
        self.response_task: asyncio.Task | None = None
        self.tts_stop = threading.Event()
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.sender_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.sender_task = asyncio.create_task(self._sender())
        self.send_json(
            {
                "type": "config",
                "tts_sample_rate": settings.tts_sample_rate,
                "model": settings.llm_model,
                "voice": settings.tts_voice,
                "speech_enabled": settings.speech_configured,
            }
        )
        self.send_state("listening")

    async def close(self) -> None:
        if self.response_task and not self.response_task.done():
            self.tts_stop.set()
            self.response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.response_task
        if self.sender_task:
            self.sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sender_task

    # -- outbound funnel -------------------------------------------------------

    async def _sender(self) -> None:
        while True:
            item = await self.out_q.get()
            if isinstance(item, tuple):  # ("audio", generation, chunk)
                _, gen, data = item
                if gen == self.gen:
                    await self.ws.send_bytes(data)
            else:
                await self.ws.send_text(json.dumps(item))

    def send_json(self, obj: dict) -> None:
        self.out_q.put_nowait(obj)

    def send_state(self, state: str) -> None:
        self.send_json({"type": "state", "state": state})

    def _send_audio_threadsafe(self, gen: int, chunk: bytes) -> None:
        self.loop.call_soon_threadsafe(self.out_q.put_nowait, ("audio", gen, chunk))

    # -- inbound events --------------------------------------------------------

    async def on_audio(self, data: bytes) -> None:
        if not self.capturing:
            return
        self.audio_buf.extend(data)
        if len(self.audio_buf) > MAX_UTTERANCE_BYTES:
            await self.on_speech_end()

    async def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "speech_start":
            await self.interrupt()
            self.capturing = True
            self.audio_buf.clear()
        elif kind == "speech_end":
            await self.on_speech_end()
        elif kind == "interrupt":
            await self.interrupt()
        elif kind == "text":
            text = (msg.get("text") or "").strip()
            if text:
                await self.interrupt()
                self.send_json({"type": "transcript", "text": text})
                self._start_response(text)
        elif kind == "reset":
            await self.interrupt()
            del self.history[1:]
            self.send_json({"type": "history_reset"})

    async def on_speech_end(self) -> None:
        if not self.capturing:
            return
        self.capturing = False
        pcm = bytes(self.audio_buf)
        self.audio_buf.clear()
        if len(pcm) < settings.asr_sample_rate // 5 * 2:  # < 200 ms — noise blip
            self.send_state("listening")
            return
        self.send_state("transcribing")
        try:
            text = await asyncio.to_thread(speech.transcribe, pcm)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"type": "error", "message": f"ASR failed: {exc}"})
            self.send_state("listening")
            return
        if not text:
            self.send_state("listening")
            return
        self.send_json({"type": "transcript", "text": text})
        self._start_response(text)

    # -- interruption ------------------------------------------------------------

    async def interrupt(self) -> None:
        task = self.response_task
        if task is None or task.done():
            return
        self.gen += 1  # stale audio chunks are dropped from here on
        self.tts_stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self.send_json({"type": "interrupted"})
        self.send_state("listening")

    # -- respond pipeline ----------------------------------------------------------

    def _start_response(self, user_text: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.response_task = asyncio.create_task(self._respond())

    async def _respond(self) -> None:
        gen = self.gen
        self.tts_stop = threading.Event()
        stop = self.tts_stop
        sentence_q: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(self._tts_worker(sentence_q, gen, stop))
        spoken_anything = False
        partial: list[str] = []

        try:
            self.send_state("thinking")
            self.send_json({"type": "assistant_start"})
            for round_no in range(settings.max_tool_rounds):
                splitter = SentenceSplitter()
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                tools = self.mcp.openai_tools()

                async for event in llm.chat_stream(self.history, tools):
                    if event["type"] == "delta":
                        text_parts.append(event["text"])
                        partial.append(event["text"])
                        self.send_json(
                            {"type": "assistant_delta", "text": event["text"]}
                        )
                        for sentence in splitter.feed(event["text"]):
                            spoken_anything = True
                            sentence_q.put_nowait(sentence)
                    else:
                        tool_calls = event["tool_calls"]

                for sentence in splitter.flush():
                    spoken_anything = True
                    sentence_q.put_nowait(sentence)

                text = "".join(text_parts)
                if not tool_calls:
                    self.history.append({"role": "assistant", "content": text})
                    break

                # Tool round: record the assistant turn, run each call via MCP,
                # then loop so the model can use the results.
                self.history.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": tc["id"] or f"call_{round_no}_{i}",
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"] or "{}",
                                },
                            }
                            for i, tc in enumerate(tool_calls)
                        ],
                    }
                )
                for i, tc in enumerate(tool_calls):
                    call_id = tc["id"] or f"call_{round_no}_{i}"
                    try:
                        arguments = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    target = self.mcp.resolve(tc["name"])
                    self.send_json(
                        {
                            "type": "tool_call",
                            "id": call_id,
                            "name": tc["name"],
                            "server": target[0] if target else "?",
                            "tool": target[1] if target else tc["name"],
                            "arguments": tc["arguments"] or "{}",
                        }
                    )
                    outcome = await self.mcp.call(tc["name"], arguments)
                    self.send_json(
                        {
                            "type": "tool_result",
                            "id": call_id,
                            "ok": outcome["ok"],
                            "result": outcome["result"],
                        }
                    )
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": outcome["result"],
                        }
                    )
            else:
                self.send_json(
                    {
                        "type": "error",
                        "message": "Stopped: too many consecutive tool rounds.",
                    }
                )

            self.send_json({"type": "assistant_done"})
            sentence_q.put_nowait(None)
            await tts_task
            self.send_state("listening")

        except asyncio.CancelledError:
            text = "".join(partial).strip()
            if text:
                self.history.append(
                    {"role": "assistant", "content": text + " [interrupted by user]"}
                )
            stop.set()
            tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tts_task
            raise
        except Exception as exc:  # noqa: BLE001
            self.send_json({"type": "error", "message": str(exc)})
            self.send_json({"type": "assistant_done"})
            sentence_q.put_nowait(None)
            stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tts_task
            self.send_state("listening")

    async def _tts_worker(
        self, sentence_q: asyncio.Queue, gen: int, stop: threading.Event
    ) -> None:
        """Synthesizes queued sentences in order, streaming PCM to the client."""
        if not settings.speech_configured:
            while await sentence_q.get() is not None:
                pass
            return
        started = False
        failed = False
        while True:
            sentence = await sentence_q.get()
            if sentence is None:
                break
            if stop.is_set() or failed:
                continue
            if not started:
                started = True
                self.send_state("speaking")
            try:
                await asyncio.to_thread(self._synth_blocking, sentence, gen, stop)
            except Exception as exc:  # noqa: BLE001
                failed = True
                self.send_json({"type": "error", "message": f"TTS failed: {exc}"})
        if started:
            self.send_json({"type": "tts_end"})

    def _synth_blocking(self, text: str, gen: int, stop: threading.Event) -> None:
        for chunk in speech.synthesize_stream(text, stop):
            if stop.is_set():
                return
            self._send_audio_threadsafe(gen, chunk)
