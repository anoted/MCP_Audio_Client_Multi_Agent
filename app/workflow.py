"""Workflow state machine + human approval gates.

The task → review → verification pipeline:

    task arrives (manager)
      └─ skill selected (skills.registry) → servers/categories routed
      └─ planner researches (read-only) and submits a plan
           └─ stage: plan_review — execution is BLOCKED until the human
              approves the plan (UI button or typed "approve")
      └─ manager executes step by step with scoped sub-agents
           └─ modify tools pass an approval checkpoint (mode-dependent)
           └─ any step that ran write tools cannot be marked done until a
              reviewer PASS and a verifier PASS are recorded for it
      └─ stage: complete

The enforcement lives here (not in prompts): `Workflow.completion_block()`
refuses to close unreviewed write steps, and `ApprovalGate` suspends the tool
call until the human decides (or the request times out → denied).
"""
import asyncio
import uuid

from .config import settings

# Tool-name fragments that mark an irreversible / outward-facing action.
HIGH_RISK_WORDS = (
    "grade", "publish", "announce", "delete", "remove", "upload",
    "message", "email", "send", "notify", "enroll", "invite",
)

STAGES = ("idle", "planning", "plan_review", "executing", "complete")


def risk_of(tool_api_name: str, access: str) -> str:
    """'read' | 'write' | 'high' for one classified tool."""
    if access == "read":
        return "read"
    name = tool_api_name.lower()
    if any(w in name for w in HIGH_RISK_WORDS):
        return "high"
    return "write"


def approval_required(risk: str, mode: str | None = None) -> bool:
    mode = mode or settings.approval_mode
    if mode == "off" or risk == "read":
        return False
    if mode == "all":
        return True
    return risk == "high"  # default mode: "high"


class Workflow:
    """Session-scoped workflow state; serializes into save/load files."""

    def __init__(self) -> None:
        self.stage = "idle"
        self.task = ""
        self.skill: str | None = None
        self.todos: list[dict] = []  # {text, status, wrote, review, verify}

    # -- plan lifecycle ------------------------------------------------------

    def begin(self, task: str, skill: str | None) -> None:
        self.task = task
        self.skill = skill
        self.stage = "planning"

    def set_plan(self, steps: list[str]) -> None:
        self.todos = [
            {"text": t, "status": "pending", "wrote": False,
             "review": "", "verify": ""}
            for t in steps
        ]
        # A plan always pauses for the human unless approvals are fully off.
        self.stage = "executing" if settings.approval_mode == "off" else "plan_review"

    def approve_plan(self) -> bool:
        if self.stage != "plan_review":
            return False
        self.stage = "executing"
        return True

    def reject_plan(self) -> None:
        self.todos = []
        self.stage = "planning"

    @property
    def plan_pending(self) -> bool:
        return self.stage == "plan_review"

    # -- step lifecycle -------------------------------------------------------

    def mark_wrote(self, index: int) -> None:
        if 0 <= index < len(self.todos):
            self.todos[index]["wrote"] = True

    def current_step(self) -> int | None:
        """Index of the step currently in progress (last one wins)."""
        idx = None
        for i, t in enumerate(self.todos):
            if t["status"] == "in_progress":
                idx = i
        return idx

    def record_review(self, index: int, verdict: str, kind: str) -> None:
        """kind is 'review' or 'verify'; verdict 'pass'/'fail'."""
        if 0 <= index < len(self.todos) and kind in ("review", "verify"):
            self.todos[index][kind] = verdict

    def completion_block(self, index: int) -> str | None:
        """Why step `index` may not be marked done yet (None = allowed).

        Structural task-review-verification: a step that executed write
        tools needs a reviewer PASS and a verifier PASS first.
        """
        if not 0 <= index < len(self.todos):
            return None
        step = self.todos[index]
        if not step.get("wrote"):
            return None
        missing = []
        if step.get("review") != "pass":
            missing.append("a reviewer PASS (call run_reviewer)")
        if step.get("verify") != "pass":
            missing.append("a verifier PASS (call run_verifier)")
        if not missing:
            return None
        return (
            f"Step {index + 1} executed modifying tools, so it cannot be "
            f"marked done until it has {' and '.join(missing)}."
        )

    def maybe_complete(self) -> bool:
        if self.stage == "executing" and self.todos and all(
            t["status"] == "done" for t in self.todos
        ):
            self.stage = "complete"
            return True
        return False

    # -- (de)serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "task": self.task,
            "skill": self.skill,
            "todos": self.todos,
        }

    def load(self, data: dict | None) -> None:
        data = data or {}
        self.stage = data.get("stage") if data.get("stage") in STAGES else "idle"
        self.task = data.get("task") or ""
        self.skill = data.get("skill")
        todos = data.get("todos") or []
        self.todos = [
            {
                "text": str(t.get("text", "")),
                "status": t.get("status", "pending"),
                "wrote": bool(t.get("wrote")),
                "review": t.get("review", ""),
                "verify": t.get("verify", ""),
            }
            for t in todos
            if isinstance(t, dict)
        ]


class ApprovalGate:
    """Suspends a tool call until the human approves or denies it."""

    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future] = {}

    def new_id(self) -> str:
        return uuid.uuid4().hex[:10]

    async def wait(self, approval_id: str) -> tuple[bool, str]:
        """Returns (approved, note). Timeout or cancellation ⇒ denied."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = fut
        try:
            return await asyncio.wait_for(fut, settings.approval_timeout_s)
        except asyncio.TimeoutError:
            return False, f"no decision within {int(settings.approval_timeout_s)}s"
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool, note: str = "") -> bool:
        fut = self.pending.get(approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result((approved, note))
        return True

    def cancel_all(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result((False, "cancelled by interruption"))
        self.pending.clear()
