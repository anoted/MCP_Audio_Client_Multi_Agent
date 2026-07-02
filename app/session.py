"""Per-WebSocket voice session: ASR -> active agent (LLM + tools) -> TTS.

Multi-agent model:
- Four primary agents (assistant, explorer, planner, manager), each with its
  own chat history. The active agent handles every user turn; typed input can
  switch agents with a leading @mention, voice always goes to the active agent.
- planner saves a to-do list via the submit_plan virtual tool; manager works
  through it by spawning tool-restricted sub-agents (run_subagent) whose
  activity streams to the client as collapsible cards.
- Per-agent MCP tool access comes from the Initiator's read/modify
  classification (agents.initiator).

Interruption model (barge-in):
- The whole respond pipeline (sub-agents included) runs as one cancellable
  asyncio task.
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
import re
import threading

from fastapi import WebSocket

from . import conversations, llm, speech
from .agents import (
    AGENTS,
    DEFAULT_AGENT,
    SUBAGENT_PROMPT,
    describe_agents,
    dynamic_context,
    initiator,
    resolve_agent,
)
from .config import settings
from .llm import SentenceSplitter
from .mcp_manager import MCPManager

MAX_UTTERANCE_BYTES = settings.asr_sample_rate * 2 * 120  # 2 minutes of PCM16
MAX_SUBAGENT_REPORT = 4000
_MENTION = re.compile(r"^@([A-Za-z_-]+)[\s,:]*")


class VoiceSession:
    def __init__(self, ws: WebSocket, mcp: MCPManager):
        self.ws = ws
        self.mcp = mcp
        self.loop = asyncio.get_running_loop()
        self.agent = DEFAULT_AGENT
        self.histories: dict[str, list[dict]] = {
            name: [{"role": "system", "content": profile.system_prompt}]
            for name, profile in AGENTS.items()
        }
        self.todos: list[dict] = []  # [{"text", "status"}]
        self.transcript: list[dict] = []  # replayable UI log for save/load
        self.audio_buf = bytearray()
        self.capturing = False
        self.gen = 0  # bumped on every interrupt; stale audio is dropped
        self.response_task: asyncio.Task | None = None
        self.tts_stop = threading.Event()
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.sender_task: asyncio.Task | None = None
        self._sub_seq = 0

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
                "agent": self.agent,
                "agents": describe_agents(),
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

    def send_todos(self) -> None:
        self.send_json({"type": "todos", "todos": self.todos})

    def _send_audio_threadsafe(self, gen: int, chunk: bytes) -> None:
        self.loop.call_soon_threadsafe(self.out_q.put_nowait, ("audio", gen, chunk))

    # -- agent switching ---------------------------------------------------------

    def set_agent(self, name: str) -> None:
        if name == self.agent:
            return
        self.agent = name
        self.send_json({"type": "agent_changed", "agent": name})

    def _strip_mention(self, text: str) -> str:
        """Handle a leading @agent mention; returns the remaining message."""
        m = _MENTION.match(text)
        if not m:
            return text
        target = resolve_agent(m.group(1))
        if target is None:
            return text  # unknown mention, treat literally
        self.set_agent(target)
        return text[m.end():].strip()

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
            if not text:
                return
            await self.interrupt()
            text = self._strip_mention(text)
            if not text:
                return  # bare @mention: just an agent switch
            self.send_json({"type": "transcript", "text": text})
            self._start_response(text)
        elif kind == "set_agent":
            target = resolve_agent(msg.get("agent") or "")
            if target:
                await self.interrupt()
                self.set_agent(target)
        elif kind == "save":
            await self._save_conversation(msg.get("name") or "")
        elif kind == "load":
            await self._load_conversation(msg.get("name") or "")
        elif kind == "reset":
            await self.interrupt()
            for history in self.histories.values():
                del history[1:]
            self.todos = []
            self.transcript = []
            self.send_json({"type": "history_reset"})
            self.send_todos()

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

    # -- save / load ---------------------------------------------------------------

    async def _save_conversation(self, name: str) -> None:
        try:
            final = conversations.save(
                name,
                {
                    "version": 1,
                    "model": settings.llm_model,
                    "agent": self.agent,
                    "todos": self.todos,
                    "histories": self.histories,
                    "transcript": self.transcript,
                },
            )
        except OSError as exc:
            self.send_json({"type": "error", "message": f"Save failed: {exc}"})
            return
        self.send_json({"type": "saved", "name": final})

    async def _load_conversation(self, name: str) -> None:
        try:
            data = conversations.load(name)
        except FileNotFoundError:
            self.send_json({"type": "error", "message": f"No conversation '{name}'."})
            return
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json({"type": "error", "message": f"Load failed: {exc}"})
            return
        await self.interrupt()
        stored = data.get("histories") or {}
        for agent_name, profile in AGENTS.items():
            seed = [{"role": "system", "content": profile.system_prompt}]
            history = stored.get(agent_name)
            if isinstance(history, list) and history:
                if history[0].get("role") == "system":
                    history = history[1:]
                self.histories[agent_name] = seed + history
            else:
                self.histories[agent_name] = seed
        self.todos = data.get("todos") or []
        self.transcript = data.get("transcript") or []
        loaded_agent = resolve_agent(data.get("agent") or "") or DEFAULT_AGENT
        self.agent = loaded_agent
        self.send_json(
            {
                "type": "loaded",
                "name": data.get("name", name),
                "agent": self.agent,
                "todos": self.todos,
                "transcript": self.transcript,
            }
        )

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
        self.histories[self.agent].append({"role": "user", "content": user_text})
        self.transcript.append(
            {"kind": "user", "agent": self.agent, "text": user_text}
        )
        self.response_task = asyncio.create_task(self._respond())

    def _flush_segment(self, agent: str, seg: list[str], interrupted: bool = False) -> None:
        """Record the streamed-so-far assistant text as one transcript entry."""
        text = "".join(seg).strip()
        seg.clear()
        if text:
            self.transcript.append(
                {
                    "kind": "assistant",
                    "agent": agent,
                    "text": text,
                    "interrupted": interrupted,
                }
            )

    async def _respond(self) -> None:
        agent = self.agent
        profile = AGENTS[agent]
        history = self.histories[agent]
        allowed = initiator.allowed_for(agent)  # None = unrestricted
        gen = self.gen
        self.tts_stop = threading.Event()
        stop = self.tts_stop
        sentence_q: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(self._tts_worker(sentence_q, gen, stop))
        partial: list[str] = []  # current unflushed transcript segment

        try:
            self.send_state("thinking")
            self.send_json({"type": "assistant_start", "agent": agent})
            for round_no in range(settings.max_tool_rounds):
                splitter = SentenceSplitter()
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                tools = self.mcp.openai_tools(allowed) + list(profile.virtual_tools)
                messages = self._build_messages(agent)

                async for event in llm.chat_stream(messages, tools or None):
                    if event["type"] == "delta":
                        text_parts.append(event["text"])
                        partial.append(event["text"])
                        self.send_json(
                            {"type": "assistant_delta", "text": event["text"]}
                        )
                        for sentence in splitter.feed(event["text"]):
                            sentence_q.put_nowait(sentence)
                    else:
                        tool_calls = event["tool_calls"]

                for sentence in splitter.flush():
                    sentence_q.put_nowait(sentence)

                text = "".join(text_parts)
                if not tool_calls:
                    history.append({"role": "assistant", "content": text})
                    self._flush_segment(agent, partial)
                    break

                # Tool round: record the assistant turn, run each call (virtual
                # tools locally, the rest via MCP), then loop so the model can
                # use the results.
                self._flush_segment(agent, partial)
                history.append(
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
                    if tc["name"] in profile.virtual_tool_names:
                        result = await self._virtual_tool(tc["name"], arguments, gen)
                    else:
                        result = await self._mcp_tool(
                            agent, allowed, tc, call_id, arguments
                        )
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result,
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
                history.append(
                    {"role": "assistant", "content": text + " [interrupted by user]"}
                )
            self._flush_segment(agent, partial, interrupted=True)
            stop.set()
            tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tts_task
            raise
        except Exception as exc:  # noqa: BLE001
            self.send_json({"type": "error", "message": str(exc)})
            self.send_json({"type": "assistant_done"})
            self._flush_segment(agent, partial)
            sentence_q.put_nowait(None)
            stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tts_task
            self.send_state("listening")

    def _build_messages(self, agent: str) -> list[dict]:
        """Agent history plus per-turn dynamic context (tool inventory, plan)."""
        messages = list(self.histories[agent])
        extra = dynamic_context(agent, self.mcp, self.todos, initiator)
        if extra:
            messages.insert(1, {"role": "system", "content": extra})
        return messages

    # -- tool execution -------------------------------------------------------------

    async def _mcp_tool(
        self, agent: str, allowed: set[str] | None, tc: dict, call_id: str,
        arguments: dict,
    ) -> str:
        target = self.mcp.resolve(tc["name"])
        entry = {
            "kind": "tool",
            "name": tc["name"],
            "server": target[0] if target else "?",
            "tool": target[1] if target else tc["name"],
            "arguments": tc["arguments"] or "{}",
            "ok": False,
            "result": "",
        }
        self.transcript.append(entry)
        self.send_json(
            {
                "type": "tool_call",
                "id": call_id,
                "name": tc["name"],
                "server": entry["server"],
                "tool": entry["tool"],
                "arguments": entry["arguments"],
            }
        )
        if allowed is not None and tc["name"] not in allowed:
            outcome = {
                "ok": False,
                "result": (
                    f"Tool '{tc['name']}' is not available to the {agent} agent."
                    + (
                        " Delegate it to a sub-agent with run_subagent."
                        if agent == "manager"
                        else ""
                    )
                ),
            }
        else:
            outcome = await self.mcp.call(tc["name"], arguments)
        entry["ok"] = outcome["ok"]
        entry["result"] = outcome["result"]
        self.send_json(
            {
                "type": "tool_result",
                "id": call_id,
                "ok": outcome["ok"],
                "result": outcome["result"],
            }
        )
        return outcome["result"]

    async def _virtual_tool(self, name: str, args: dict, gen: int) -> str:
        if name == "submit_plan":
            raw = args.get("todos")
            steps = [str(t).strip() for t in raw if str(t).strip()] if isinstance(
                raw, list
            ) else []
            if not steps:
                return "submit_plan failed: 'todos' must be a non-empty list."
            self.todos = [{"text": t, "status": "pending"} for t in steps[:50]]
            self.send_todos()
            return f"Plan saved with {len(self.todos)} steps."
        if name == "set_todo_status":
            try:
                index = int(args.get("index", 0)) - 1
            except (TypeError, ValueError):
                return "set_todo_status failed: 'index' must be an integer."
            status = str(args.get("status", "")).strip()
            if status not in ("pending", "in_progress", "done"):
                return "set_todo_status failed: bad status."
            if not 0 <= index < len(self.todos):
                return (
                    f"set_todo_status failed: index out of range "
                    f"(plan has {len(self.todos)} items)."
                )
            self.todos[index]["status"] = status
            self.send_todos()
            return f"Step {index + 1} marked {status}."
        if name == "run_subagent":
            return await self._run_subagent(args, gen)
        return f"Unknown virtual tool '{name}'."

    # -- sub-agents --------------------------------------------------------------------

    async def _run_subagent(self, args: dict, gen: int) -> str:
        self._sub_seq += 1
        sub_id = f"sub-{gen}-{self._sub_seq}"
        name = str(args.get("name") or f"subagent-{self._sub_seq}").strip()[:40]
        instruction = str(args.get("instruction") or "").strip()
        requested = args.get("tools") if isinstance(args.get("tools"), list) else []
        if not instruction:
            return "run_subagent failed: 'instruction' is required."

        all_specs = self.mcp.openai_tools()
        available = {s["function"]["name"] for s in all_specs}
        granted = [str(t) for t in requested if str(t) in available]
        unknown = [str(t) for t in requested if str(t) not in available]
        specs = self.mcp.openai_tools(set(granted))

        entry = {
            "kind": "subagent",
            "id": sub_id,
            "name": name,
            "task": instruction,
            "tools": granted,
            "text": "",
            "events": [],
            "status": "running",
            "result": "",
        }
        self.transcript.append(entry)
        self.send_json(
            {
                "type": "subagent_start",
                "id": sub_id,
                "name": name,
                "task": instruction,
                "tools": granted,
            }
        )

        messages: list[dict] = [
            {"role": "system", "content": SUBAGENT_PROMPT.format(name=name)},
            {"role": "user", "content": instruction},
        ]
        final = ""
        try:
            for round_no in range(settings.max_tool_rounds):
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                async for event in llm.chat_stream(messages, specs or None):
                    if event["type"] == "delta":
                        text_parts.append(event["text"])
                        entry["text"] += event["text"]
                        self.send_json(
                            {"type": "subagent_delta", "id": sub_id,
                             "text": event["text"]}
                        )
                    else:
                        tool_calls = event["tool_calls"]
                text = "".join(text_parts)
                if not tool_calls:
                    final = text
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": tc["id"] or f"{sub_id}_call_{round_no}_{i}",
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
                    call_id = tc["id"] or f"{sub_id}_call_{round_no}_{i}"
                    try:
                        arguments = json.loads(tc["arguments"] or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    self.send_json(
                        {
                            "type": "subagent_tool_call",
                            "id": sub_id,
                            "call_id": call_id,
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                        }
                    )
                    if tc["name"] in set(granted):
                        outcome = await self.mcp.call(tc["name"], arguments)
                    else:
                        outcome = {
                            "ok": False,
                            "result": f"Tool '{tc['name']}' was not granted to "
                                      f"this sub-agent.",
                        }
                    entry["events"].append(
                        {
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                            "ok": outcome["ok"],
                            "result": outcome["result"],
                        }
                    )
                    self.send_json(
                        {
                            "type": "subagent_tool_result",
                            "id": sub_id,
                            "call_id": call_id,
                            "ok": outcome["ok"],
                            "result": outcome["result"],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": outcome["result"],
                        }
                    )
            else:
                final = entry["text"] or "(stopped: too many tool rounds)"
        except asyncio.CancelledError:
            entry["status"] = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["result"] = f"{type(exc).__name__}: {exc}"
            self.send_json(
                {"type": "subagent_done", "id": sub_id, "ok": False,
                 "result": entry["result"]}
            )
            return f"Sub-agent '{name}' failed: {entry['result']}"

        final = (final or "").strip() or "(sub-agent produced no output)"
        entry["status"] = "done"
        entry["result"] = final[:MAX_SUBAGENT_REPORT]
        self.send_json(
            {"type": "subagent_done", "id": sub_id, "ok": True,
             "result": entry["result"]}
        )
        note = f" (ignored unknown tools: {', '.join(unknown)})" if unknown else ""
        return f"Sub-agent '{name}' finished{note}:\n{final[:MAX_SUBAGENT_REPORT]}"

    # -- TTS ---------------------------------------------------------------------------

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
