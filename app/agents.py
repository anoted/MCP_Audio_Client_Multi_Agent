"""Agent profiles, virtual tools, and the Initiator.

Primary agents (each keeps its own chat history in the session):
- assistant  default conversational agent, full tool access
- explorer   investigates with read-only tools
- planner    researches (read-only) and produces a to-do list via submit_plan
- manager    plans via run_planner and works through the to-do list by
             deploying sub-agents (run_subagent) whose tools it grants by
             category / access class, ticking items off (set_todo_status)

The Initiator is a one-shot background agent: whenever the MCP tool inventory
changes it classifies every tool's access ("read" or "modify") and its domain
category/keywords (LLM first, keyword heuristic as fallback), assigns tool
sets to the primary agents, and then discards its working context — only the
resulting assignment is kept. The manager's grant selectors ("read", "all",
"assignments", "write:modules", exact names, …) are resolved against this
classification by Initiator.expand().
"""
import asyncio
import json
import re
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
            "name, a self-contained instruction (it shares no memory with you — "
            "include every id and fact it needs), and tool grants. Grant whole "
            "categories or access classes rather than single tools so the "
            "sub-agent has everything the task might need. It only sees the "
            "tools you grant. Returns its final report."
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
                    "description": (
                        "Tool grants, each one of: 'read' (every read-only "
                        "tool), 'write' (every modifying tool), 'all', a "
                        "category/keyword such as 'assignments' or 'modules' "
                        "(grants every matching tool), 'read:<keyword>' / "
                        "'write:<keyword>' to narrow a category by access, or "
                        "an exact tool name. Example: [\"read\", \"quizzes\"]."
                    ),
                },
            },
            "required": ["name", "instruction", "tools"],
        },
    },
}

RUN_PLANNER_SPEC = {
    "type": "function",
    "function": {
        "name": "run_planner",
        "description": (
            "Run the Planner agent: it researches with read-only tools and "
            "saves a new to-do list, replacing any current plan. Use it when "
            "there is no plan yet or the current plan no longer fits — you do "
            "not need the user to run the planner for you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What to plan, complete and self-contained (the "
                        "planner does not see your conversation)."
                    ),
                }
            },
            "required": ["task"],
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
        description="Plans via the planner and executes by deploying sub-agents.",
        system_prompt=(
            "You are Manager. You never run task tools yourself — you plan "
            "and delegate. If there is no to-do list yet (or it no longer "
            "fits the task), first call run_planner with the full task; it "
            "researches and saves the plan. Then work through the list one "
            "item at a time: mark it in_progress with set_todo_status, deploy "
            "a sub-agent with run_subagent, check its report, then mark it "
            "done. Sub-agents start blank — each instruction must contain "
            "every id, name, and fact from earlier reports that the sub-agent "
            "needs. Grant tools broadly using the grant selectors from the "
            "tool inventory: ['read'] for research or verification, a "
            "category like ['modules'] or ['assignments'] for focused work, "
            "combinations like ['read', 'quizzes'] when it must look things "
            "up and modify — so a sub-agent is never missing a tool mid-task. "
            "Grant single exact tools only when one tool clearly suffices. "
            "Keep the user informed with short spoken updates between steps. "
            + _VOICE_STYLE
        ),
        virtual_tools=(RUN_PLANNER_SPEC, RUN_SUBAGENT_SPEC, SET_TODO_STATUS_SPEC),
    ),
}

SUBAGENT_PROMPT = (
    "You are '{name}', a focused sub-agent created by a manager agent. "
    "Complete the task you are given using only the tools available to you, "
    "then reply with a short plain-text report of what you did and found. "
    "Do not ask questions — make reasonable assumptions and finish."
)

PLANNER_SUBAGENT_PROMPT = (
    "You are Planner, invoked by the manager as a background planning agent. "
    "Research the task with your read-only tools as needed, then break it "
    "into a short ordered list of concrete steps and save it by calling "
    "submit_plan — this is required. Each step must be small enough for one "
    "focused sub-agent to complete. After submit_plan succeeds, reply with a "
    "brief plain-text summary of the plan. Do not ask questions — make "
    "reasonable assumptions and finish."
)


# --- dynamic per-turn context ----------------------------------------------------


def _inventory_text(
    mcp: MCPManager, initiator: "Initiator", selectors: bool = False
) -> str:
    specs = mcp.openai_tools()
    if not specs:
        return "No MCP tools are currently connected."
    lines = ["Available tools (name — access — category — description):"]
    for spec in specs:
        fn = spec["function"]
        access = initiator.classes.get(fn["name"], "unclassified")
        category = initiator.category.get(fn["name"], "-")
        desc = (fn.get("description") or "").strip().replace("\n", " ")[:120]
        lines.append(f"- {fn['name']} — {access} — {category} — {desc}")
    if selectors:
        summary = initiator.grant_summary()
        if summary:
            lines.append("")
            lines.append(f"Grant selectors for run_subagent: {summary}")
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
        return (
            _inventory_text(mcp, initiator, selectors=True)
            + "\n\n"
            + _todos_text(todos)
        )
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
    "mapping every tool name to an object with two keys: \"access\" — "
    "\"read\" (it only observes, retrieves, or computes) or \"modify\" (it "
    "creates, changes, deletes, sends, or executes anything with side "
    "effects) — and \"category\" — one lowercase word for the kind of thing "
    "the tool works on (e.g. assignments, submissions, quizzes, modules, "
    "pages, files, announcements). Use the SAME category word for every tool "
    "that works on the same kind of thing. No other text."
)

# Leading verbs stripped from tool names when deriving domain keywords.
_VERB_TOKENS = {
    "list", "get", "read", "fetch", "search", "find", "show", "view", "check",
    "create", "add", "update", "delete", "remove", "set", "publish", "upload",
    "send", "post", "grade", "run", "make", "new", "build", "edit",
}


def _heuristic_class(name: str, description: str) -> str:
    text = f"{name} {description}".lower()
    if any(w in text for w in _MOD_WORDS):
        return "modify"
    if any(w in text for w in _READ_WORDS):
        return "read"
    return "modify"  # unknown -> safe default


def _name_tokens(api_name: str) -> tuple[str, list[str]]:
    """Split 'Server__tool_name' into (server, [tokens])."""
    parts = api_name.lower().split("__", 1)
    server = parts[0] if len(parts) == 2 else ""
    tokens = [t for t in re.split(r"[^a-z0-9]+", parts[-1]) if t]
    return server, tokens


def _keyword_tags(api_name: str) -> set[str]:
    """Domain keywords for a tool: server name + non-verb name tokens."""
    server, tokens = _name_tokens(api_name)
    tags = {t for t in tokens if t not in _VERB_TOKENS and not t.isdigit()}
    if server:
        tags.add(server)
    return tags


def _heuristic_category(api_name: str) -> str:
    """First non-verb token of the tool name, e.g. list_assignments -> assignments."""
    _, tokens = _name_tokens(api_name)
    for tok in tokens:
        if tok not in _VERB_TOKENS and not tok.isdigit():
            return tok
    return "misc"


def _canonical_categories(category: dict[str, str]) -> dict[str, str]:
    """Merge singular/plural variants ('module'/'modules', 'quiz'/'quizzes')."""
    cats = sorted(set(category.values()), key=len)
    remap: dict[str, str] = {}
    for i, short in enumerate(cats):
        for longer in cats[i + 1:]:
            if longer.startswith(short) and len(longer) - len(short) <= 3:
                remap[longer] = remap.get(short, short)
    return {n: remap.get(c, c) for n, c in category.items()}


class Initiator:
    """One-shot background classifier; keeps only the tool assignment."""

    def __init__(self) -> None:
        self.status = "idle"  # idle | running | done | error
        self.classes: dict[str, str] = {}  # tool api name -> read | modify
        self.category: dict[str, str] = {}  # tool api name -> primary category
        self.tags: dict[str, set[str]] = {}  # tool api name -> keyword tags
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
                    self.category = {}
                    self.tags = {}
                    self.method = "none"
                    self.status = "done"
                    return
                llm_result: dict[str, tuple[str, str]] | None = None
                try:
                    llm_result = await asyncio.wait_for(
                        self._classify_llm(tools), 45
                    )
                    self.method = "llm"
                except Exception:  # noqa: BLE001 — fall back to keywords
                    llm_result = None
                if llm_result is None:
                    llm_result = {}
                    self.method = "heuristic"
                classes: dict[str, str] = {}
                category: dict[str, str] = {}
                # Anything the LLM missed gets the heuristic answer; keyword
                # tags always come from the tool name so selector matching
                # works even without the LLM.
                for name, desc in tools:
                    access, cat = llm_result.get(name, ("", ""))
                    classes[name] = access or _heuristic_class(name, desc)
                    category[name] = cat or _heuristic_category(name)
                category = _canonical_categories(category)
                self.classes = classes
                self.category = category
                self.tags = {
                    name: _keyword_tags(name) | {category[name]}
                    for name, _ in tools
                }
                self.status = "done"
            except Exception:  # noqa: BLE001
                self.status = "error"
            # Working context (prompts, LLM reply) goes out of scope here —
            # only the classes/category/tags assignment survives.

    async def _classify_llm(
        self, tools: list[tuple[str, str]]
    ) -> dict[str, tuple[str, str]] | None:
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
        out: dict[str, tuple[str, str]] = {}
        for key, val in parsed.items():
            if isinstance(val, dict):
                access = str(val.get("access", "")).strip().lower()
                cat = str(val.get("category", "")).strip().lower()
            else:  # older single-word form
                access = str(val).strip().lower()
                cat = ""
            access = "read" if access == "read" else "modify"
            cat = re.sub(r"[^a-z0-9_-]", "", cat)[:30]
            out[str(key)] = (access, cat)
        return out

    # -- grant selectors -----------------------------------------------------

    def expand(
        self, selectors: list, available: set[str]
    ) -> tuple[set[str], list[str]]:
        """Resolve grant selectors to tool names.

        Each selector may be an access class ('read'/'write'/'all'), a
        category or keyword ('assignments', 'quiz', …), 'read:<keyword>' /
        'write:<keyword>', or an exact tool name. Returns (granted names,
        selectors that matched nothing).
        """
        granted: set[str] = set()
        unmatched: list[str] = []
        lower_names = {n.lower(): n for n in available}
        for raw in selectors or []:
            sel = str(raw).strip().lower()
            sel = sel.removesuffix(" tools").strip()
            if not sel:
                continue
            access = None
            prefix, _, rest = sel.partition(":")
            if rest.strip() and prefix in ("read", "write", "modify"):
                access = "read" if prefix == "read" else "modify"
                sel = rest.strip()
            matches = self._match(sel, available, lower_names)
            if access is not None:
                matches = {n for n in matches if self.classes.get(n) == access}
            if matches:
                granted |= matches
            else:
                unmatched.append(str(raw))
        return granted, unmatched

    def _match(
        self, sel: str, available: set[str], lower_names: dict[str, str]
    ) -> set[str]:
        if sel in ("all", "*", "everything"):
            return set(available)
        if sel in ("read", "readonly", "read-only", "read_only"):
            return {n for n in available if self.classes.get(n) == "read"}
        if sel in ("write", "modify", "mutate"):
            return {n for n in available if self.classes.get(n) == "modify"}
        if sel in lower_names:
            return {lower_names[sel]}
        out: set[str] = set()
        for name in available:
            tags = self.tags.get(name, set())
            if sel in tags or sel in name.lower():
                out.add(name)
            elif len(sel) >= 3 and any(
                # bidirectional prefix so 'quiz' matches 'quizzes' and back
                t.startswith(sel) or sel.startswith(t)
                for t in tags
                if len(t) >= 3
            ):
                out.add(name)
        return out

    def grant_summary(self) -> str:
        """One-line description of usable selectors for the manager prompt."""
        if not self.classes:
            return ""
        counts: dict[str, int] = {}
        for cat in self.category.values():
            counts[cat] = counts.get(cat, 0) + 1
        cats = ", ".join(f"'{c}' ({n})" for c, n in sorted(counts.items()))
        read = sum(1 for c in self.classes.values() if c == "read")
        return (
            f"'all' ({len(self.classes)} tools), 'read' ({read}), "
            f"'write' ({len(self.classes) - read}); categories: {cats}. "
            "Narrow with 'read:<category>' / 'write:<category>'; exact tool "
            "names and other keywords from tool names also match."
        )

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
            "categories": len(set(self.category.values())),
        }


initiator = Initiator()


def describe_agents() -> list[dict]:
    info = initiator.describe()
    access = {
        "assistant": f"all {info['total']} tools" if info["total"] else "all tools",
        "explorer": f"{info['read']} read-only tools",
        "planner": f"{info['read']} read-only tools + submit_plan",
        "manager": "run_planner + sub-agents (tools granted by category)",
    }
    return [
        {
            "name": p.name,
            "description": p.description,
            "access": access.get(p.name, ""),
        }
        for p in AGENTS.values()
    ]
