"""Skill registry: reusable workflow playbooks the agents select per task.

A skill is one markdown file in `skills/` with a small front-matter block:

    ---
    name: canvas-grading
    title: Grade submissions against a rubric
    description: one line shown in the UI
    servers: Canvas            # MCP servers this workflow routes to
    categories: submissions    # tool categories typically needed
    risk: high                 # read | write | high — drives approval gating
    triggers: grade, rubric    # keywords that select this skill
    ---
    ## Plan guidance
    ...injected into the planner prompt...
    ## Review checklist
    ...injected into the reviewer prompt...
    ## Verification
    ...injected into the verifier prompt...

Selection is deterministic keyword scoring over the triggers (word-boundary
matches, longest-trigger-wins tie-break), so it is testable offline and never
adds LLM latency. The selected skill scopes sub-agent tool grants to its
servers (server routing) and feeds its checklists to the reviewer/verifier.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings


@dataclass
class Skill:
    name: str
    title: str = ""
    description: str = ""
    servers: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    risk: str = "write"  # read | write | high
    triggers: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def plan_guidance(self) -> str:
        return self.sections.get("plan guidance", "")

    @property
    def review_checklist(self) -> str:
        return self.sections.get("review checklist", "")

    @property
    def verification(self) -> str:
        return self.sections.get("verification", "")

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "servers": self.servers,
            "categories": self.categories,
            "risk": self.risk,
        }


def _parse_skill(text: str) -> Skill | None:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, val = line.partition(":")
        if _:
            meta[key.strip().lower()] = val.split("#", 1)[0].strip()
    if not meta.get("name"):
        return None

    def _list(key: str) -> list[str]:
        return [p.strip() for p in meta.get(key, "").split(",") if p.strip()]

    sections: dict[str, str] = {}
    current, buf = None, []
    for line in m.group(2).splitlines():
        head = re.match(r"^##\s+(.+)$", line)
        if head:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = head.group(1).strip().lower(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()

    return Skill(
        name=meta["name"],
        title=meta.get("title", meta["name"]),
        description=meta.get("description", ""),
        servers=_list("servers"),
        categories=_list("categories"),
        risk=meta.get("risk", "write"),
        triggers=[t.lower() for t in _list("triggers")],
        sections=sections,
    )


class SkillRegistry:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}

    def load(self, directory: str | Path | None = None) -> None:
        self.skills.clear()
        path = Path(directory or settings.skills_dir)
        if not path.exists():
            return
        for file in sorted(path.glob("*.md")):
            try:
                skill = _parse_skill(file.read_text(encoding="utf-8"))
            except OSError:
                continue
            if skill:
                self.skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self.skills.get((name or "").strip().lower())

    def select(self, task: str) -> Skill | None:
        """Best-scoring skill for a task, or None (generic workflow)."""
        text = (task or "").lower()
        best, best_score = None, 0.0
        for skill in self.skills.values():
            score = 0.0
            for trig in skill.triggers:
                hits = len(re.findall(rf"\b{re.escape(trig)}", text))
                # multi-word triggers are stronger signals
                score += hits * (2.0 if " " in trig else 1.0)
            if score > best_score:
                best, best_score = skill, score
        return best

    def describe(self) -> list[dict]:
        return [s.describe() for s in self.skills.values()]


registry = SkillRegistry()
