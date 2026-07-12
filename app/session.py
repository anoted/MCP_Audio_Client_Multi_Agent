"""Per-WebSocket voice session: ASR -> active agent (LLM + tools) -> TTS.

Multi-agent workflow model:
- Workflow agents (manager, planner, explorer, reviewer, verifier) plus the
  out-of-workflow assistant. Each keeps its own chat history. Typed input can
  switch agents with a leading @mention; voice always goes to the active agent.
- The manager orchestrates the task→review→verification pipeline: run_planner
  produces a plan that PAUSES for human approval; workers (run_subagent) get
  tool grants expanded by the Initiator and routed to the active skill's
  servers; steps that modified state need reviewer + verifier PASS before
  set_todo_status(done) is accepted (enforced here, not just prompted).
- Human approval checkpoints: modifying tool calls (mode-dependent: all /
  high-risk / off) suspend on an ApprovalGate until the user approves or
  denies from the UI. Denials return a tool error, never an exception.
- Privacy: tool results pass the Pseudonymizer (people data → stable tokens)
  and the injection guard before entering any LLM context; outgoing tool
  arguments are de-pseudonymized so real content reaches the MCP server.
- MCP apps: tool results carrying {"mcp_app": {...}} render as interactive
  iframes in the client; the iframe can browse further data through a
  read-only tool bridge and push items into the workflow context.
- Audit: every consequential event lands in a per-session JSONL log and
  streams to the UI activity panel.

Interruption model (barge-in):
- The whole respond pipeline (sub-agents included) runs as one cancellable
  asyncio task; approval gates are resolved as denials on interrupt.
- All outbound traffic funnels through a single sender task; every response
  has a generation number and stale audio chunks are dropped after interrupt.
"""
import asyncio
import contextlib
import json
import re
import threading
import uuid

from fastapi import WebSocket

from . import conversations, llm, privacy, speech
from .agents import (
    AGENTS,
    DEFAULT_AGENT,
    PLANNER_SUBAGENT_PROMPT,
    REVIEWER_SUBAGENT_PROMPT,
    SUBAGENT_PROMPT,
    SUBMIT_PLAN_SPEC,
    VERIFIER_SUBAGENT_PROMPT,
    describe_agents,
    dynamic_context,
    initiator,
    resolve_agent,
)
from .audit import AuditLog
from .config import settings
from .llm import SentenceSplitter
from .mcp_manager import MCPManager
from .privacy import Pseudonymizer
from .skills import registry as skill_registry
from .workflow import ApprovalGate, Workflow, approval_required, risk_of

MAX_UTTERANCE_BYTES = settings.asr_sample_rate * 2 * 120  # 2 minutes of PCM16
MAX_SUBAGENT_REPORT = 4000
MAX_APP_HTML = 512 * 1024
MAX_LLM_RESULT = 8000        # tool-result chars entering an LLM context
MAX_BRIDGE_RESULT = 300_000  # tool-result chars for MCP-app bridge consumers
_MENTION = re.compile(r"^@([A-Za-z_-]+)[\s,:]*")

# Short spoken/typed phrases that resolve a pending plan approval directly.
_PLAN_APPROVE = re.compile(
    r"^(yes|yep|ok(ay)?|sure|approve[d]?|approve (the )?plan|go ahead|proceed|"
    r"looks good( to me)?|lgtm|sounds good|do it)[.! ]*$",
    re.IGNORECASE,
)
_PLAN_REJECT = re.compile(
    r"^(no|nope|reject(ed)?|reject (the )?plan|request changes|revise( the plan)?|"
    r"change (the )?plan)[.! ]*$",
    re.IGNORECASE,
)


def _plan_phrase(text: str) -> bool | None:
    """True = approve, False = reject, None = not a plan decision."""
    stripped = text.strip()
    if _PLAN_APPROVE.match(stripped):
        return True
    if _PLAN_REJECT.match(stripped):
        return False
    return None


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
        self.workflow = Workflow()
        self.approvals = ApprovalGate()
        self.pseudo = Pseudonymizer()
        self.audit = AuditLog(listener=self._on_audit_entry)
        self.transcript: list[dict] = []  # replayable UI log for save/load
        self.audio_buf = bytearray()
        self.capturing = False
        self.gen = 0  # bumped on every interrupt; stale audio is dropped
        self.response_task: asyncio.Task | None = None
        self.tts_stop = threading.Event()
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.sender_task: asyncio.Task | None = None
        self._sub_seq = 0
        self._sentence_q: asyncio.Queue | None = None  # live TTS queue, if any

    @property
    def todos(self) -> list[dict]:
        return self.workflow.todos

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.sender_task = asyncio.create_task(self._sender())
        self.send_json(
            {
                "type": "config",
                **settings.public(),
                "agent": self.agent,
                "agents": describe_agents(),
                "skills": skill_registry.describe(),
            }
        )
        self.send_workflow()
        self.send_state("listening")
        self.audit.event("session_start", session=self.audit.session_id)

    async def close(self) -> None:
        self.approvals.cancel_all()
        if self.response_task and not self.response_task.done():
            self.tts_stop.set()
            self.response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.response_task
        if self.sender_task:
            self.sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sender_task
        self.audit.event("session_end")

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
        self.send_json({"type": "todos", "todos": self.workflow.todos})

    def send_workflow(self) -> None:
        self.send_json({"type": "workflow", **self.workflow.to_dict()})

    def _on_audit_entry(self, entry: dict) -> None:
        self.send_json({"type": "log", "entry": entry})

    def _send_audio_threadsafe(self, gen: int, chunk: bytes) -> None:
        self.loop.call_soon_threadsafe(self.out_q.put_nowait, ("audio", gen, chunk))

    def _speak(self, text: str) -> None:
        """Queue a short spoken sentence if a TTS stream is currently open."""
        if self._sentence_q is not None and settings.speech_configured:
            self._sentence_q.put_nowait(text)

    # -- agent switching ---------------------------------------------------------

    def set_agent(self, name: str) -> None:
        if name == self.agent:
            return
        self.agent = name
        self.audit.event("agent_switch", agent=name)
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
        elif kind == "approval":
            self._on_approval(msg)
        elif kind == "plan_decision":
            self._on_plan_decision(msg)
        elif kind == "set_skill":
            self._on_set_skill(msg)
        elif kind == "app_tool_call":
            await self._on_app_tool_call(msg)
        elif kind == "open_app":
            await self._on_open_app(msg)
        elif kind == "app_workflow_add":
            text = str(msg.get("text") or "")[:2000]
            if text:
                self.audit.event("app_workflow_add", text=text[:300])
        elif kind == "save":
            await self._save_conversation(msg.get("name") or "")
        elif kind == "load":
            await self._load_conversation(msg.get("name") or "")
        elif kind == "reset":
            await self.interrupt()
            self.approvals.cancel_all()
            for history in self.histories.values():
                del history[1:]
            self.workflow = Workflow()
            self.pseudo = Pseudonymizer()
            self.transcript = []
            self.audit.event("reset")
            self.send_json({"type": "history_reset"})
            self.send_todos()
            self.send_workflow()

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

    # -- approvals / workflow control -------------------------------------------

    def _on_approval(self, msg: dict) -> None:
        approval_id = str(msg.get("id") or "")
        approved = bool(msg.get("approved"))
        note = str(msg.get("note") or "")[:500]
        if self.approvals.resolve(approval_id, approved, note):
            self.audit.event(
                "approval_decision", id=approval_id, approved=approved, note=note
            )

    def _on_plan_decision(self, msg: dict) -> None:
        approved = bool(msg.get("approved"))
        note = str(msg.get("note") or "")[:500]
        if approved:
            if not self.workflow.approve_plan():
                return
            self.audit.event("plan_approved")
            resume = "Plan approved — proceed with the plan."
        else:
            self.workflow.reject_plan()
            self.audit.event("plan_rejected", note=note)
            resume = (
                "Plan rejected"
                + (f": {note}" if note else "")
                + ". Revise the plan with run_planner before executing."
            )
        self.send_workflow()
        self.send_todos()
        # Resume the manager hands-free if it is idle right now.
        if self.response_task is None or self.response_task.done():
            self.set_agent("manager")
            self.send_json({"type": "transcript", "text": resume})
            self._start_response(resume)

    def _on_set_skill(self, msg: dict) -> None:
        name = (msg.get("name") or "").strip().lower()
        if name and skill_registry.get(name) is None:
            return
        self.workflow.skill = name or None
        self.audit.event("skill_set", skill=self.workflow.skill, by="user")
        self.send_workflow()

    async def _on_app_tool_call(self, msg: dict) -> None:
        """Read-only tool bridge for MCP app iframes. Never allows writes."""
        req_id = str(msg.get("req_id") or "")
        server = str(msg.get("server") or "")
        tool = str(msg.get("tool") or "")
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        api_name = self._find_api_name(server, tool)
        deny = None
        if api_name is None:
            deny = f"Unknown tool '{tool}' on server '{server}'."
        elif initiator.classes.get(api_name) != "read":
            deny = "App bridge only permits read-only tools."
        elif len(json.dumps(args)) > 4000:
            deny = "Arguments too large."
        self.audit.event(
            "app_tool_call", server=server, tool=tool, denied=deny or False
        )
        if deny:
            self.send_json(
                {"type": "app_tool_result", "req_id": req_id, "ok": False,
                 "result": deny}
            )
            return
        outcome = await self.mcp.call(api_name, args, max_chars=MAX_BRIDGE_RESULT)
        self.send_json(
            {
                "type": "app_tool_result",
                "req_id": req_id,
                "ok": outcome["ok"],
                "result": outcome["result"][:MAX_BRIDGE_RESULT],
            }
        )

    def _find_api_name(self, server: str, tool: str) -> str | None:
        for spec in self.mcp.openai_tools():
            target = self.mcp.resolve(spec["function"]["name"])
            if target and target[0] == server and target[1] == tool:
                return spec["function"]["name"]
        return None

    async def _on_open_app(self, msg: dict) -> None:
        """Open an MCP app directly from the Apps panel (read-only tools only)."""
        server = str(msg.get("server") or "")
        tool = str(msg.get("tool") or "")
        api_name = self._find_api_name(server, tool)
        if api_name is None or initiator.classes.get(api_name) != "read":
            self.send_json(
                {"type": "error", "message": f"Cannot open '{tool}' on '{server}'."}
            )
            return
        self.audit.event("app_opened", server=server, tool=tool)
        outcome = await self.mcp.call(api_name, {}, max_chars=MAX_BRIDGE_RESULT)
        app = self._extract_app(outcome["result"]) if outcome["ok"] else None
        if app:
            await self._send_app(server, app)
        else:
            self.send_json(
                {"type": "error",
                 "message": f"'{tool}' did not return an app: "
                            f"{outcome['result'][:200]}"}
            )

    # -- save / load ---------------------------------------------------------------

    async def _save_conversation(self, name: str) -> None:
        try:
            final = conversations.save(
                name,
                {
                    "version": 2,
                    "model": settings.llm_model,
                    "agent": self.agent,
                    "workflow": self.workflow.to_dict(),
                    "histories": self.histories,
                    "transcript": self.transcript,
                },
            )
        except OSError as exc:
            self.send_json({"type": "error", "message": f"Save failed: {exc}"})
            return
        self.audit.event("conversation_saved", name=final)
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
        self.workflow = Workflow()
        if data.get("workflow"):
            self.workflow.load(data["workflow"])
        elif data.get("todos"):  # legacy v1 files
            self.workflow.load({"stage": "executing", "todos": data["todos"]})
        self.transcript = data.get("transcript") or []
        loaded_agent = resolve_agent(data.get("agent") or "") or DEFAULT_AGENT
        self.agent = loaded_agent
        self.audit.event("conversation_loaded", name=data.get("name", name))
        self.send_json(
            {
                "type": "loaded",
                "name": data.get("name", name),
                "agent": self.agent,
                "todos": self.workflow.todos,
                "workflow": self.workflow.to_dict(),
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
        self.approvals.cancel_all()  # pending approvals resolve as denied
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self.audit.event("interrupted")
        self.send_json({"type": "interrupted"})
        self.send_state("listening")

    # -- respond pipeline ----------------------------------------------------------

    def _start_response(self, user_text: str) -> None:
        # A bare "approve" / "reject" while the plan waits is the decision
        # itself (works by voice too), not a new manager turn.
        if self.workflow.plan_pending:
            decision = _plan_phrase(user_text)
            if decision is not None:
                self._on_plan_decision({"approved": decision, "note": ""})
                return
        # A fresh task arriving at the manager (or planner) opens a workflow:
        # select the skill and record the task before the LLM turn starts.
        if self.agent in ("manager", "planner") and self.workflow.stage in (
            "idle", "complete",
        ):
            skill = skill_registry.select(user_text)
            self.workflow.begin(user_text, skill.name if skill else None)
            self.audit.event(
                "workflow_started",
                task=user_text[:300],
                skill=self.workflow.skill,
            )
            self.send_workflow()
        self.audit.event("user_input", agent=self.agent, text=user_text[:300])
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

    def _active_skill(self):
        return skill_registry.get(self.workflow.skill) if self.workflow.skill else None

    async def _respond(self) -> None:
        agent = self.agent
        profile = AGENTS[agent]
        history = self.histories[agent]
        allowed = initiator.allowed_for(agent)  # None = unrestricted
        gen = self.gen
        self.tts_stop = threading.Event()
        stop = self.tts_stop
        sentence_q: asyncio.Queue = asyncio.Queue()
        self._sentence_q = sentence_q
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
            self._sentence_q = None
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
            self.audit.event("error", message=str(exc)[:300])
            self.send_json({"type": "error", "message": str(exc)})
            self.send_json({"type": "assistant_done"})
            self._flush_segment(agent, partial)
            sentence_q.put_nowait(None)
            stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tts_task
            self.send_state("listening")
        finally:
            self._sentence_q = None

    def _build_messages(self, agent: str) -> list[dict]:
        """Agent history plus per-turn dynamic context (tool inventory, plan)."""
        messages = list(self.histories[agent])
        extra = dynamic_context(
            agent,
            self.mcp,
            self.workflow.todos,
            initiator,
            skill=self._active_skill(),
            workflow_stage=self.workflow.stage,
        )
        if extra:
            messages.insert(1, {"role": "system", "content": extra})
        return messages

    # -- guarded tool execution ---------------------------------------------------

    def _reveal_arguments(self, arguments: dict) -> dict:
        """De-pseudonymize outgoing arguments so real content reaches MCP.

        The LLM only ever saw tokens like 'Student-3'; anything it writes
        back (feedback comments, page bodies) must carry the real values when
        it leaves for the tool server.
        """
        if not settings.privacy_enabled or not self.pseudo.mapping_size:
            return arguments
        try:
            return json.loads(self.pseudo.reveal(json.dumps(arguments)))
        except (TypeError, ValueError):
            return arguments

    def _extract_app(self, raw_text: str) -> dict | None:
        """Detect an MCP-app payload ({"mcp_app": {...}}) in a tool result."""
        if '"mcp_app"' not in raw_text:
            return None
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            return None
        app = data.get("mcp_app") if isinstance(data, dict) else None
        if not isinstance(app, dict):
            return None
        resource = str(app.get("resource") or "")
        if not resource.startswith("ui://"):
            return None
        return {
            "resource": resource,
            "title": str(app.get("title") or resource),
            "data": app.get("data"),
            "summary": str(
                data.get("summary")
                or f"Rendered interactive app {resource} for the user."
            ),
        }

    async def _send_app(self, server: str, app: dict) -> None:
        html = await self.mcp.read_resource(server, app["resource"])
        if not html or len(html) > MAX_APP_HTML:
            return
        entry = {
            "kind": "app",
            "id": f"app-{uuid.uuid4().hex[:8]}",
            "server": server,
            "uri": app["resource"],
            "title": app["title"],
            "html": html,
            "data": app["data"],
        }
        self.transcript.append(entry)
        self.audit.event("app_rendered", server=server, uri=app["resource"])
        self.send_json({"type": "app", **entry})

    async def _guarded_call(
        self, caller: str, api_name: str, arguments: dict
    ) -> dict:
        """MCP call wrapped with approval checkpoint, privacy and injection
        guard. Returns the mcp outcome dict extended with 'flags'."""
        access = initiator.classes.get(api_name, "modify")
        risk = risk_of(api_name, access)
        target = self.mcp.resolve(api_name)
        server = target[0] if target else "?"
        if approval_required(risk):
            approval_id = self.approvals.new_id()
            args_json = json.dumps(arguments, ensure_ascii=False, indent=1)[:2000]
            display = self.pseudo.reveal(args_json)
            entry = {
                "kind": "approval",
                "id": approval_id,
                "tool": api_name,
                "server": server,
                "risk": risk,
                "caller": caller,
                "arguments": display,
                "approved": None,
                "note": "",
            }
            self.transcript.append(entry)
            self.audit.event(
                "approval_requested", id=approval_id, tool=api_name,
                risk=risk, caller=caller,
            )
            self.send_json(
                {
                    "type": "approval_request",
                    "id": approval_id,
                    "tool": api_name,
                    "server": server,
                    "risk": risk,
                    "caller": caller,
                    "arguments": display,
                }
            )
            self._speak("I need your approval to continue.")
            approved, note = await self.approvals.wait(approval_id)
            entry["approved"] = approved
            entry["note"] = note
            self.send_json(
                {
                    "type": "approval_resolved",
                    "id": approval_id,
                    "approved": approved,
                    "note": note,
                }
            )
            self.audit.event(
                "approval_result", id=approval_id, approved=approved, note=note
            )
            if not approved:
                return {
                    "ok": False,
                    "server": server,
                    "tool": api_name,
                    "result": (
                        "Denied at the human approval checkpoint"
                        + (f": {note}" if note else ".")
                        + " Do not retry this call unless the user asks."
                    ),
                    "flags": [],
                    "denied": True,
                }
        # Fetch with a generous cap so an app payload is never truncated
        # mid-JSON; the text that continues toward the LLM is capped below.
        outcome = await self.mcp.call(
            api_name, self._reveal_arguments(arguments),
            max_chars=MAX_BRIDGE_RESULT,
        )
        raw = outcome["result"]
        app = self._extract_app(raw) if outcome["ok"] else None
        if app:
            await self._send_app(server, app)
            raw = app["summary"]
        text = raw[:MAX_LLM_RESULT]
        if settings.privacy_enabled:
            text = self.pseudo.scrub(text, initiator.category.get(api_name))
        flags: list[str] = []
        if settings.injection_guard_enabled:
            text, flags = privacy.guard(text)
        if flags:
            self.audit.event(
                "injection_flagged", tool=api_name, caller=caller, flags=flags
            )
        outcome["result"] = text
        outcome["flags"] = flags
        return outcome

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
            "flags": [],
        }
        self.transcript.append(entry)
        self.audit.event(
            "tool_call", agent=agent, tool=tc["name"], server=entry["server"]
        )
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
                "flags": [],
            }
            self.audit.event(
                "tool_blocked", agent=agent, tool=tc["name"], reason="not-granted"
            )
        else:
            outcome = await self._guarded_call(f"@{agent}", tc["name"], arguments)
        entry["ok"] = outcome["ok"]
        entry["result"] = outcome["result"]
        entry["flags"] = outcome.get("flags", [])
        self.audit.event(
            "tool_result", tool=tc["name"], ok=outcome["ok"],
            chars=len(outcome["result"]),
        )
        self.send_json(
            {
                "type": "tool_result",
                "id": call_id,
                "ok": outcome["ok"],
                "result": outcome["result"],
                "flags": entry["flags"],
            }
        )
        return outcome["result"]

    async def _virtual_tool(self, name: str, args: dict, gen: int) -> str:
        if name == "submit_plan":
            return self._submit_plan(args)
        if name == "set_todo_status":
            return self._set_todo_status(args)
        if name == "run_subagent":
            return await self._run_subagent(args, gen, role="worker")
        if name == "run_planner":
            return await self._run_subagent(args, gen, role="planner")
        if name == "run_reviewer":
            return await self._run_subagent(args, gen, role="reviewer")
        if name == "run_verifier":
            return await self._run_subagent(args, gen, role="verifier")
        return f"Unknown virtual tool '{name}'."

    def _submit_plan(self, args: dict) -> str:
        raw = args.get("todos")
        steps = [str(t).strip() for t in raw if str(t).strip()] if isinstance(
            raw, list
        ) else []
        if not steps:
            return "submit_plan failed: 'todos' must be a non-empty list."
        self.workflow.set_plan(steps[:50])
        self.audit.event("plan_submitted", steps=len(self.workflow.todos))
        self.send_todos()
        self.send_workflow()
        if self.workflow.plan_pending:
            self.send_json({"type": "plan_review", **self.workflow.to_dict()})
            return (
                f"Plan saved with {len(self.workflow.todos)} steps. It is now "
                "awaiting the user's approval — execution is blocked until "
                "they approve it."
            )
        return f"Plan saved with {len(self.workflow.todos)} steps."

    def _set_todo_status(self, args: dict) -> str:
        try:
            index = int(args.get("index", 0)) - 1
        except (TypeError, ValueError):
            return "set_todo_status failed: 'index' must be an integer."
        status = str(args.get("status", "")).strip()
        if status not in ("pending", "in_progress", "done"):
            return "set_todo_status failed: bad status."
        if not 0 <= index < len(self.workflow.todos):
            return (
                f"set_todo_status failed: index out of range "
                f"(plan has {len(self.workflow.todos)} items)."
            )
        if status == "done":
            block = self.workflow.completion_block(index)
            if block:
                self.audit.event("step_close_blocked", step=index + 1)
                return f"set_todo_status refused: {block}"
        self.workflow.todos[index]["status"] = status
        self.audit.event("step_status", step=index + 1, status=status)
        self.send_todos()
        if self.workflow.maybe_complete():
            self.audit.event("workflow_complete")
            self.send_workflow()
            return (
                f"Step {index + 1} marked done. All steps are complete — the "
                "workflow is finished; give the user a short closing summary."
            )
        return f"Step {index + 1} marked {status}."

    # -- sub-agents (workers, planner, reviewer, verifier) -------------------------

    def _role_setup(
        self, role: str, args: dict, available: set[str]
    ) -> tuple[str, str, set[str], list[str], int | None] | str:
        """Resolve (name, instruction, granted, unmatched, step_index) for a
        sub-agent run; returns an error string on bad arguments."""
        skill = self._active_skill()
        servers = skill.servers if skill else None
        if role == "planner":
            instruction = str(args.get("task") or "").strip()
            if not instruction:
                return "run_planner failed: 'task' is required."
            if skill and skill.plan_guidance:
                instruction += "\n\nSkill plan guidance:\n" + skill.plan_guidance
            granted, _ = initiator.expand(["read"], available, servers)
            return "planner", instruction, granted, [], None
        if role in ("reviewer", "verifier"):
            key = "summary" if role == "reviewer" else "instruction"
            brief = str(args.get(key) or "").strip()
            if not brief:
                return f"run_{role} failed: '{key}' is required."
            try:
                step_index = int(args.get("step_index", 0)) - 1
            except (TypeError, ValueError):
                return f"run_{role} failed: 'step_index' must be an integer."
            if not 0 <= step_index < len(self.workflow.todos):
                return (
                    f"run_{role} failed: step_index out of range "
                    f"(plan has {len(self.workflow.todos)} items)."
                )
            step = self.workflow.todos[step_index]
            parts = [
                f"Step {step_index + 1}: {step['text']}",
                brief,
            ]
            if role == "reviewer" and skill and skill.review_checklist:
                parts.append("Review checklist:\n" + skill.review_checklist)
            if role == "verifier" and skill and skill.verification:
                parts.append("Verification guidance:\n" + skill.verification)
            instruction = "\n\n".join(parts)
            if role == "reviewer":
                granted: set[str] = set()  # artifact QA is tool-free by design
            else:
                granted, _ = initiator.expand(["read"], available, servers)
            return role, instruction, granted, [], step_index
        # worker
        name = str(args.get("name") or f"subagent-{self._sub_seq}").strip()[:40]
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return "run_subagent failed: 'instruction' is required."
        requested = args.get("tools") if isinstance(args.get("tools"), list) else []
        granted, unmatched = initiator.expand(requested, available, servers)
        if requested and not granted:
            return (
                "run_subagent failed: no tools matched "
                f"{', '.join(repr(u) for u in unmatched)}"
                + (
                    f" within the active skill's servers ({', '.join(servers)})"
                    if servers
                    else ""
                )
                + ". Grant access classes ('read', 'write', 'all'), "
                "categories/keywords from the tool inventory, or exact tool "
                "names."
            )
        return name, instruction, granted, unmatched, None

    async def _run_subagent(self, args: dict, gen: int, role: str = "worker") -> str:
        if role == "worker" and self.workflow.plan_pending:
            return (
                "Blocked: the plan is awaiting the user's approval. Do not "
                "run sub-agents until the user approves it; tell them it is "
                "ready instead."
            )
        self._sub_seq += 1
        sub_id = f"sub-{gen}-{self._sub_seq}"
        available = {s["function"]["name"] for s in self.mcp.openai_tools()}
        setup = self._role_setup(role, args, available)
        if isinstance(setup, str):
            return setup
        name, instruction, granted_set, unmatched, step_index = setup
        granted = sorted(granted_set)
        specs = self.mcp.openai_tools(granted_set)
        if role == "planner":
            specs = specs + [SUBMIT_PLAN_SPEC]
        system_prompt = {
            "planner": PLANNER_SUBAGENT_PROMPT,
            "reviewer": REVIEWER_SUBAGENT_PROMPT,
            "verifier": VERIFIER_SUBAGENT_PROMPT,
        }.get(role) or SUBAGENT_PROMPT.format(name=name)

        entry = {
            "kind": "subagent",
            "id": sub_id,
            "name": name,
            "role": role,
            "task": instruction,
            "tools": granted,
            "text": "",
            "events": [],
            "status": "running",
            "result": "",
        }
        self.transcript.append(entry)
        self.audit.event(
            "subagent_start", id=sub_id, role=role, name=name, tools=len(granted)
        )
        self.send_json(
            {
                "type": "subagent_start",
                "id": sub_id,
                "name": name,
                "role": role,
                "task": instruction,
                "tools": granted,
            }
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction},
        ]
        final = ""
        wrote = False
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
                    if role == "planner" and tc["name"] == "submit_plan":
                        plan_result = self._submit_plan(arguments)
                        outcome = {
                            "ok": not plan_result.startswith("submit_plan failed"),
                            "result": plan_result,
                        }
                    elif tc["name"] in granted_set:
                        self.audit.event(
                            "tool_call", agent=f"{role}:{name}", tool=tc["name"],
                            server=(self.mcp.resolve(tc["name"]) or ["?"])[0],
                        )
                        outcome = await self._guarded_call(
                            f"{role} '{name}'", tc["name"], arguments
                        )
                        if (
                            outcome["ok"]
                            and initiator.classes.get(tc["name"]) == "modify"
                        ):
                            wrote = True
                    else:
                        outcome = {
                            "ok": False,
                            "result": f"Tool '{tc['name']}' was not granted to "
                                      f"this sub-agent.",
                        }
                        self.audit.event(
                            "tool_blocked", agent=f"{role}:{name}",
                            tool=tc["name"], reason="not-granted",
                        )
                    entry["events"].append(
                        {
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                            "ok": outcome["ok"],
                            "result": outcome["result"],
                            "flags": outcome.get("flags", []),
                        }
                    )
                    self.send_json(
                        {
                            "type": "subagent_tool_result",
                            "id": sub_id,
                            "call_id": call_id,
                            "ok": outcome["ok"],
                            "result": outcome["result"],
                            "flags": outcome.get("flags", []),
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
            self.audit.event("subagent_failed", id=sub_id, error=entry["result"][:200])
            self.send_json(
                {"type": "subagent_done", "id": sub_id, "ok": False,
                 "result": entry["result"]}
            )
            return f"Sub-agent '{name}' failed: {entry['result']}"

        final = (final or "").strip() or "(sub-agent produced no output)"
        entry["status"] = "done"
        entry["result"] = final[:MAX_SUBAGENT_REPORT]
        self.audit.event("subagent_done", id=sub_id, role=role, wrote=wrote)
        self.send_json(
            {"type": "subagent_done", "id": sub_id, "ok": True,
             "result": entry["result"]}
        )
        return self._finish_role_run(
            role, name, final, wrote, step_index, unmatched
        )

    def _finish_role_run(
        self,
        role: str,
        name: str,
        final: str,
        wrote: bool,
        step_index: int | None,
        unmatched: list[str],
    ) -> str:
        report = final[:MAX_SUBAGENT_REPORT]
        if role == "planner":
            plan = "\n".join(
                f"{i}. [{t['status']}] {t['text']}"
                for i, t in enumerate(self.workflow.todos, 1)
            ) or "(no plan was submitted)"
            pending = (
                "\n\nThe plan is awaiting the user's approval — do not start "
                "executing until it is approved."
                if self.workflow.plan_pending
                else ""
            )
            return f"Planner finished:\n{report}\n\nCurrent plan:\n{plan}{pending}"
        if role in ("reviewer", "verifier"):
            verdict = "fail"
            if re.match(r"\s*PASS\b", final, re.IGNORECASE):
                verdict = "pass"
            elif not re.match(r"\s*FAIL\b", final, re.IGNORECASE):
                report = "(no clear PASS/FAIL verdict — treated as FAIL)\n" + report
            kind = "review" if role == "reviewer" else "verify"
            if step_index is not None:
                self.workflow.record_review(step_index, verdict, kind)
                self.audit.event(
                    f"{kind}_recorded", step=step_index + 1, verdict=verdict
                )
                self.send_todos()
            return f"{role.capitalize()} verdict for step " \
                   f"{(step_index or 0) + 1}: {verdict.upper()}\n{report}"
        # worker
        note = (
            f" (selectors that matched no tools: {', '.join(unmatched)})"
            if unmatched
            else ""
        )
        if wrote:
            idx = self.workflow.current_step()
            if idx is not None:
                self.workflow.mark_wrote(idx)
                self.send_todos()
                note += (
                    f"\nNote: this sub-agent modified state — step {idx + 1} "
                    "requires a reviewer PASS (run_reviewer) and a verifier "
                    "PASS (run_verifier) before it can be marked done."
                )
        return f"Sub-agent '{name}' finished{note}:\n{report}"

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
