# Scenario: prepare a course week on Canvas with the workflow system

An end-to-end test of the task→review→verification workflow: skill selection,
server-routed tool grants, human plan approval, approval checkpoints on risky
writes, enforced reviewer/verifier passes, MCP apps, and the audit log.

> ⚠️ **The Canvas tools are LIVE.** Every write tool changes a real Canvas
> instance immediately. Run phases 4–6 only against a **sandbox / test course**
> where you are the teacher and no real students are enrolled. In particular:
>
> - `grade_submission` changes real student grades.
> - `create_announcement` notifies every enrolled student.
> - `delete_module` / `delete_module_item` are the **only** delete tools —
>   assignments, quizzes, and pages created during the test must be removed
>   manually in the Canvas UI afterwards.
>
> Phases 0–3 are safe anywhere (no tool, or read-only tools). See also
> `SECURITY_CHECKLIST.md`.

## Setup

1. Conda env `mcpagents` has the Canvas server's requirements installed.
2. `examples/Canvas_MCP/.env` contains `CANVAS_BASE_URL` and
   `CANVAS_API_TOKEN` (sandbox account token).
3. Start the app (`python -m uvicorn app.main:app --port 8000`) and check:
   - Settings ⚙ → MCP Servers: **Canvas** connected (~37 tools).
   - Agents panel: initiator line like `37 tools → 19 read · 18 modify`.
   - Settings ⚙ → General: approval mode = **high-risk only** (default).
   - Apps panel shows **course explorer**.

Replace `<COURSE_ID>` with your sandbox course id (phase 1 finds it).

## Phase 0 — wiring check (no Canvas calls)

> `@manager Deploy one sub-agent with no tools that writes a two-sentence
> checklist for preparing a new course week. Do not plan first, just run it.`

**Expect:** a 🤖 worker card with an empty tool list that still reports back.

## Phase 1 — apps + read-only reconnaissance (safe)

1. Click **course explorer** in the Apps panel. Browse to your sandbox
   course; check the Modules/Assignments/Quizzes tabs, expand a module, and
   open 📊 grades on an assignment (inline histogram).
2. Click **+ workflow** on the course entry — a context chip appears above
   the input. Then type: `Summarize the current structure of this course.`

**Expect:** the chip's context is attached to the message; the explorer's
browsing calls appear in the Activity panel as `app_tool_call` events (all
read-only — try nothing else: the bridge refuses write tools).

3. Ask for charts by voice or text:
   > `@explorer show me the grading progress donut for course <COURSE_ID>`

**Expect:** an interactive chart card plus a spoken summary of the numbers.

## Phase 2 — plan + human approval (safe)

> `@manager In course <COURSE_ID>, prepare a new week called "Week 3: NumPy
> Fundamentals": one module, an overview page inside it, one 20-point
> assignment due next Friday 23:59, and a 3-question quiz — everything left
> unpublished.`

**Expect:**

- Workflow panel: skill chip switches to **content builder**
  (`canvas-content-builder`), stage → *Plan*.
- The manager calls `run_planner` (planner card, read-only grants).
- The plan lands in the panel and stage flips to **Approve** — the manager
  *cannot* run workers now (structurally blocked, try telling it to).
- No write tool has been called.

## Phase 3 — approve the plan (safe)

Click **✓ Approve plan** (or just say **"approve"**).

**Expect:** stage → *Execute*, and the manager resumes hands-free.

## Phase 4 — execution with review/verify (SANDBOX ONLY)

Watch the workflow run. **Pass** looks like:

- Worker sub-agents with category grants (e.g. `["modules"]` → all module
  tools), scoped to the Canvas server.
- Steps marked `in_progress` → after the worker: a 🔎 **reviewer** card and a
  ✅ **verifier** card (read-only tools) whose verdicts appear as `R✓ V✓`
  badges on the step — only then does the step turn done. Try asking the
  manager to skip review: `set_todo_status` refuses.
- `upload_file` or any publish/delete/grade call pauses on a 🛡️ **approval
  card** (high-risk). Deny one once — the worker must report the denial and
  the manager must adapt, not retry silently.

## Phase 5 — verification + privacy (safe)

> `@verifier Confirm that course <COURSE_ID> now has a "Week 3: NumPy
> Fundamentals" module whose page, assignment and quiz are all unpublished.`

Then, if the sandbox has (fake) student submissions:

> `@explorer List the submissions for assignment <ID> in course <COURSE_ID>.`

**Expect:** with privacy ON, the chat and LLM context show `Student-1`,
`Student-2` … instead of names; the audit log (Settings → Privacy) shows the
same pseudonyms.

## Phase 6 — cleanup (SANDBOX ONLY)

> `@manager Remove the "Week 3: NumPy Fundamentals" module from course
> <COURSE_ID>: delete its items, then the module itself. Tell me which
> leftover objects I must delete manually.`

**Expect:** plan → approval → a worker with `["modules"]`; `delete_module*`
calls pause for approval (high-risk); the manager lists the page/assignment/
quiz you must remove in the Canvas UI.

## Pass checklist

- [ ] Explorer app browsed live data and pushed context chips; bridge stayed
      read-only.
- [ ] Skill auto-selected; grants stayed within the Canvas server.
- [ ] Plan paused for approval; saying "approve" resumed it.
- [ ] Every write step got reviewer + verifier PASS before turning done;
      closing without them was refused.
- [ ] High-risk calls paused on approval cards; a denial was handled
      gracefully.
- [ ] Privacy pseudonyms in chat/logs; real names in Canvas-bound writes.
- [ ] `logs/session-*.jsonl` contains the full audit trail.
- [ ] Speaker playback never interrupted the assistant; speaking over it
      stopped it within ~0.3 s.
