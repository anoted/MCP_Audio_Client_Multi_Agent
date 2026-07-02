"""Agent profiles, virtual tools, and the Initiator.

Primary agents (each keeps its own chat history in the session):
- assistant  default conversational agent, full tool access
- explorer   investigates with read-only tools
- planner    researches (read-only) and produces a to-do list via submit_plan
- manager    works through the to-do list by deploying tool-restricted
             sub-agents (run_subagent) and ticking items off (set_todo_status)

The Initiator is a one-shot background agent: whenever the MCP tool inventory
changes it classifies every tool as "read" or "modify" (LLM first, keyword
heuristic as fallback), assigns tool sets to the primary agents, and then
discards its working context — only the resulting assignment is kept.
"""
import asyncio
import json
from dataclasses import dataclass, field

from . import llm
from .config import settings
from .mcp_manager import MCPManager

DEFAULT_AGENT = "assistant"

_ALIASES = {
    "explore": "explorer",
    "plan": "planner",
    "manage": "manager",
    "chat": "assistant",
    "default": "assistant",
}


def resolve_agent(name: str) -> str | None:
    key = name.strip().lower()
    if key in AGENTS:
        return key
    return _ALIASES.get(key)


# --- virtual tool specs (handled by the session, never routed to MCP) --------

SUBMIT_PLAN_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": (
            "Save the final to-do list for the task. Each item is one concrete, "
            "actionable step. Replaces any previous plan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of steps.",
                }
            },
            "required": ["todos"],
        },
    },
}

SET_TODO_STATUS_SPEC = {
    "type": "function",
    "function": {
        "name": "set_todo_status",
        "description": "Update the status of one to-do item (1-based index).",
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "1-based item number."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done"],
                },
            },
            "required": ["index", "status"],
        },
    },
}

RUN_SUBAGENT_SPEC = {
    "type": "function",
    "function": {
        "name": "run_subagent",
        "description": (
            "Create and run a sub-agent to carry out one task. Give it a short "
            "name, a self-contained instruction, and the minimal list of tools "
            "it needs (exact tool names from the available-tools list). The "
            "sub-agent only sees the tools you grant it. Returns its final "
            "report."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short sub-agent name."},
                "instruction": {
                    "type": "string",
                    "description": "Complete, self-contained task description.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names the sub-agent may use.",
                },
            },
            "required": ["name", "instruction", "tools"],
        },
    },
}


# --- agent profiles ------------------------------------------------------------

_VOICE_STYLE = (
    "Your replies are spoken aloud with text-to-speech, so answer "
    "conversationally, keep it concise, and avoid markdown, tables, and code "
    "blocks unless explicitly requested."
)


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    virtual_tools: tuple = field(default_factory=tuple)

    @property
    def virtual_tool_names(self) -> set[str]:
        return {spec["function"]["name"] for spec in self.virtual_tools}


AGENTS: dict[str, AgentProfile] = {
    "assistant": AgentProfile(
        name="assistant",
        description="General voice assistant with full tool access.",
        system_prompt=settings.system_prompt,  # env-configurable (SYSTEM_PROMPT)
    ),
    "explorer": AgentProfile(
        name="explorer",
        description="Explores and researches with read-only tools.",
        system_prompt=(
            "You are Explorer, a read-only investigation agent. You look things "
            "up, inspect state, and report what you find — you never change "
            "anything. Only read-only tools are available to you; if a task "
            "would require modifying something, say so and suggest handing it "
            "to the manager. " + _VOICE_STYLE
        ),
    ),
    "planner": AgentProfile(
        name="planner",
        description="Researches a task and produces a to-do list.",
        system_prompt=(
            "You are Planner. Given a task, optionally use your read-only tools "
            "to gather context, then break the task into a short ordered list "
            "of concrete steps and save it by calling submit_plan. Each step "
            "should be small enough for one sub-agent to complete. After "
            "submitting, tell the user the plan in one or two spoken sentences "
            "and suggest switching to @manager to execute it. " + _VOICE_STYLE
        ),
        virtual_tools=(SUBMIT_PLAN_SPEC,),
    ),
    "manager": AgentProfile(
        name="manager",
        description="Executes the to-do list by deploying sub-agents.",
        system_prompt=(
            "You are Manager. You do not run tools yourself — you delegate. "
            "Work through the current to-do list one item at a time: mark the "
            "item in_progress with set_todo_status, deploy a sub-agent with "
            "run_subagent (give it a clear self-contained instruction and only "
            "the minimal tools it needs from the available-tools list), check "
            "its report, then mark the item done. If there is no plan yet, "
            "either ask the user or derive a quick plan yourself with "
            "submit-style steps described aloud. Keep the user informed with "
            "short spoken updates between steps. " + _VOICE_STYLE
        ),
        virtual_tools=(RUN_SUBAGENT_SPEC, SET_TODO_STATUS_SPEC),
    ),
}

SUBAGENT_PROMPT = (
    "You are '{name}', a focused sub-agent created by a manager agent. "
    "Complete the task you are given using only the tools available to you, "
    "then reply with a short plain-text report of what you did and found. "
    "Do not ask questions — make reasonable assumptions and finish."
)


# --- dynamic per-turn context ----------------------------------------------------


def _inventory_text(mcp: MCPManager, initiator: "Initiator") -> str:
    specs = mcp.openai_tools()
    if not specs:
        return "No MCP tools are currently connected."
    lines = ["Available tools (name — access — description):"]
    for spec in specs:
        fn = spec["function"]
        access = initiator.classes.get(fn["name"], "unclassified")
        desc = (fn.get("description") or "").strip().replace("\n", " ")[:120]
        lines.append(f"- {fn['name']} — {access} — {desc}")
    return "\n".join(lines)


def _todos_text(todos: list[dict]) -> str:
    if not todos:
        return "Current to-do list: (empty — no plan submitted yet)."
    lines = ["Current to-do list:"]
    for i, item in enumerate(todos, 1):
        lines.append(f"{i}. [{item['status']}] {item['text']}")
    return "\n".join(lines)


def dynamic_context(
    agent: str, mcp: MCPManager, todos: list[dict], initiator: "Initiator"
) -> str | None:
    """Extra system context injected per turn (not stored in history)."""
    if agent == "planner":
        return _inventory_text(mcp, initiator)
    if agent == "manager":
        return _inventory_text(mcp, initiator) + "\n\n" + _todos_text(todos)
    return None


# --- Initiator --------------------------------------------------------------------

_MOD_WORDS = (
    "write", "creat", "delet", "remov", "updat", "set_", "set-", "add",
    "insert", "post", "send", "execut", "run", "install", "move", "renam",
    "edit", "modif", "upload", "kill", "stop", "start", "restart", "deploy",
    "patch", "clear", "reset", "submit", "publish", "schedul",
)
_READ_WORDS = (
    "get", "list", "read", "search", "find", "fetch", "query", "lookup",
    "describ", "show", "view", "check", "status", "info", "time", "now",
    "current", "count", "summar", "calc", "roll", "convert", "translat",
)

_CLASSIFY_SYSTEM = (
    "You classify tools for an agent system. Reply with ONLY a JSON object "
    'mapping every tool name to "read" (it only observes, retrieves, or '
    'computes) or "modify" (it creates, changes, deletes, sends, or executes '
    "anything with side effects). No other text."
)


def _heuristic_class(name: str, description: str) -> str:
    text = f"{name} {description}".lower()
    if any(w in text for w in _MOD_WORDS):
        return "modify"
    if any(w in text for w in _READ_WORDS):
        return "read"
    return "modify"  # unknown -> safe default


class Initiator:
    """One-shot background classifier; keeps only the tool assignment."""

    def __init__(self) -> None:
        self.status = "idle"  # idle | running | done | error
        self.classes: dict[str, str] = {}  # tool api name -> read | modify
        self.method = ""  # llm | heuristic
        self._lock = asyncio.Lock()

    async def run(self, mcp: MCPManager) -> None:
        async with self._lock:
            self.status = "running"
            try:
                specs = mcp.openai_tools()
                tools = [
                    (s["function"]["name"], s["function"].get("description") or "")
                    for s in specs
                ]
                if not tools:
                    self.classes = {}
                    self.method = "none"
                    self.status = "done"
                    return
                classes: dict[str, str] | None = None
                try:
                    classes = await asyncio.wait_for(self._classify_llm(tools), 45)
                    self.method = "llm"
                except Exception:  # noqa: BLE001 — fall back to keywords
                    classes = None
                if classes is None:
                    classes = {n: _heuristic_class(n, d) for n, d in tools}
                    self.method = "heuristic"
                # Anything the LLM missed gets the heuristic answer.
                for name, desc in tools:
                    classes.setdefault(name, _heuristic_class(name, desc))
                self.classes = {n: c for n, c in classes.items()
                                if n in {t[0] for t in tools}}
                self.status = "done"
            except Exception:  # noqa: BLE001
                self.status = "error"
            # Working context (prompts, LLM reply) goes out of scope here —
            # only self.classes survives.

    async def _classify_llm(self, tools: list[tuple[str, str]]) -> dict[str, str] | None:
        listing = "\n".join(
            f"- {name}: {desc.strip()[:200]}" for name, desc in tools
        )
        reply = await llm.complete(
            [
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": f"Tools:\n{listing}"},
            ],
            temperature=0.0,
        )
        start, end = reply.find("{"), reply.rfind("}")
        if start < 0 or end <= start:
            return None
        parsed = json.loads(reply[start : end + 1])
        if not isinstance(parsed, dict):
            return None
        out = {}
        for key, val in parsed.items():
            val = str(val).strip().lower()
            out[str(key)] = "read" if val == "read" else "modify"
        return out

    def allowed_for(self, agent: str) -> set[str] | None:
        """Tool names an agent may call. None means unrestricted."""
        if agent in ("explorer", "planner"):
            return {n for n, c in self.classes.items() if c == "read"}
        if agent == "manager":
            return set()  # manager only delegates via virtual tools
        return None  # assistant: everything

    def describe(self) -> dict:
        read = sum(1 for c in self.classes.values() if c == "read")
        return {
            "status": self.status,
            "method": self.method,
            "total": len(self.classes),
            "read": read,
            "modify": len(self.classes) - read,
        }


initiator = Initiator()


def describe_agents() -> list[dict]:
    info = initiator.describe()
    access = {
        "assistant": f"all {info['total']} tools" if info["total"] else "all tools",
        "explorer": f"{info['read']} read-only tools",
        "planner": f"{info['read']} read-only tools + submit_plan",
        "manager": "delegates via sub-agents",
    }
    return [
        {
            "name": p.name,
            "description": p.description,
            "access": access.get(p.name, ""),
        }
        for p in AGENTS.values()
    ]
