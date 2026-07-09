# Scenario: prepare a course week on Canvas with the multi-agent system

A practical end-to-end test of the agent rework: category-based tool grants,
the manager calling the planner itself, and sub-agents that carry a full tool
category instead of one or two hand-picked tools.

> ⚠️ **The Canvas tools are LIVE.** Every write tool changes a real Canvas
> instance immediately. Run phases 3–5 only against a **sandbox / test course**
> where you are the teacher and no real students are enrolled. In particular:
>
> - `grade_submission` changes real student grades.
> - `create_announcement` notifies every enrolled student.
> - `delete_module` / `delete_module_item` are the **only** delete tools —
>   assignments, quizzes, and pages created during the test cannot be deleted
>   through MCP and must be removed manually in the Canvas UI afterwards.
>
> Phases 0–2 are safe anywhere: phase 0 touches no Canvas tool at all, and
> phases 1–2 use read-only tools.

## Setup

1. Conda env `mcpagents` has the Canvas server's requirements installed
   (`examples/Canvas_MCP/requirements.txt`).
2. `examples/Canvas_MCP/.env` contains `CANVAS_BASE_URL` and
   `CANVAS_API_TOKEN` (a token for the sandbox account).
3. `mcp_servers.json` already registers the server:
   `conda run --no-capture-output -n mcpagents python examples/Canvas_MCP/canvas_mcp_server.py`
   (stdio).
4. Start the app (`python -m uvicorn app.main:app --port 8000`), open the UI:
   - MCP panel: **Canvas** connected, ~31 tools listed.
   - Agents panel: initiator line like `31 tools → 13 read · 18 modify`
     (LLM classification may adjust the split slightly).

Replace `<COURSE_ID>` below with your sandbox course id (phase 1 finds it).

## Phase 0 — wiring check (no Canvas calls)

Type:

> `@manager Deploy one sub-agent with no tools that writes a two-sentence
> checklist for preparing a new course week. Do not plan first, just run it.`

**Expect:** a 🤖 sub-agent card with an empty tool list that still returns a
report. This proves delegation works before any live tool is involved.

## Phase 1 — read-only reconnaissance (safe)

> `@explorer List my Canvas courses and tell me which one looks like a
> sandbox or test course.`

then, with the id it found:

> `@explorer In course <COURSE_ID>, summarize the existing modules and
> assignments so I know the current structure.`

**Expect:** explorer calls only read tools (it physically has no others —
check the tool cards: `list_courses`, `list_modules`, `list_assignments`, …).

## Phase 2 — manager plans by itself (safe)

Go straight to the manager **without** visiting `@planner` first:

> `@manager In course <COURSE_ID>, prepare a new week called "Week 3: NumPy
> Fundamentals": one module, an overview page inside it, one 20-point
> assignment due next Friday 23:59, and a 3-question quiz — everything left
> unpublished. Plan first, then wait for my go.`

**Expect:**

- The manager calls **`run_planner`** (a `planner` sub-agent card appears
  with read-only tools granted) instead of asking you to switch agents.
- The Plan panel fills with ~5–7 concrete steps.
- No write tool has been called yet.

## Phase 3 — execution with category grants (SANDBOX ONLY)

> `@manager Go ahead with the plan.`

Watch each sub-agent card's granted tool list. **Pass** looks like grants by
category or class, e.g.:

| Step | Reasonable grant | Expands to |
|---|---|---|
| create the module | `["modules"]` | all 8 module tools |
| overview page + attach | `["pages", "modules"]` | page + module tools |
| assignment | `["assignments", "read"]` | 3 assignment tools + all read tools |
| quiz + questions | `["quizzes"]` | all 5 quiz tools |
| verify at the end | `["read"]` | all 13 read-only tools |

**Fail** (the old shallow behavior) looks like a sub-agent granted exactly one
or two hand-picked tools that then reports it was missing a tool mid-task —
e.g. a quiz builder holding only `create_quiz` and unable to call
`add_quiz_question`.

Also check: items get ticked `in_progress` → `done` in the Plan panel, and
each instruction repeats the ids the sub-agent needs (course id, module id
from the previous report, …) since sub-agents share no memory.

## Phase 4 — verification (safe)

> `@explorer In course <COURSE_ID>, show the "Week 3: NumPy Fundamentals"
> module with its items, and confirm the assignment and quiz are unpublished.`

## Phase 5 — cleanup (SANDBOX ONLY)

> `@manager Remove the "Week 3: NumPy Fundamentals" module from course
> <COURSE_ID>: delete its items, then the module itself. The underlying
> assignment, quiz, and page can't be deleted with your tools — just tell me
> so I can remove them in the Canvas UI.`

**Expect:** one sub-agent with `["modules"]` (or `["modules", "read"]`)
running `delete_module_item` / `delete_module`, and the manager telling you
which leftover objects to delete manually.

## Pass checklist

- [ ] Phase 0 sub-agent ran with zero tools and reported back.
- [ ] Explorer never had a write tool available.
- [ ] Manager produced the plan itself via `run_planner` — no manual
      `@planner` step was needed.
- [ ] Every executing sub-agent got a category/class grant (3+ tools), not a
      one-tool grant, and none stalled on a missing tool.
- [ ] Plan items were marked in-progress/done as work proceeded.
- [ ] Week 3 module, page, assignment, and quiz existed (unpublished) after
      phase 3 and the module was gone after phase 5.
