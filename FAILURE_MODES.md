# Failure modes

A catalog of the ways this workflow system fails, split by whether the failure
is **avoidable** (a fix or habit prevents it) or **not avoidable** (it will
happen eventually; the system's job is to survive it and make it visible).
Each entry says how you notice it and what limits the damage. Companion
documents: `SECURITY_CHECKLIST.md` (hardening) and
`scenarios/` (guided tests that exercise several of these on purpose).

## Avoidable failures

These come from configuration, prompt/description quality, or operator habits.

### A1 — Tool misclassified by the initiator
The initiator labels every MCP tool `read` or `modify` (LLM first, keyword
heuristic as fallback). A modify tool mislabeled `read` would flow to
read-only agents and skip approval gates — the single worst config failure.
- **Notice:** Settings → MCP Servers shows each tool's class; the Agents
  panel shows the `x read · y modify` split. Spot-check after every server
  change (see `SECURITY_CHECKLIST.md`).
- **Avoid:** write tool descriptions whose *name verb* matches the action
  (`create_*`, `list_*`); the heuristic trusts the name's leading verb first.
  Fix the description and restart so the initiator re-runs.
- **Backstop:** unknown tools default to `modify` (the safe direction), and
  the app bridge re-checks the class server-side.

### A2 — Worker under-granted or over-granted
A sub-agent missing a tool mid-task fails its step; one granted `all` for a
narrow step widens the blast radius of a bad LLM turn.
- **Notice:** the worker card lists its tools; `subagent` audit events record
  the grant expansion, including selectors that matched nothing.
- **Avoid:** grant categories (`["read", "quizzes"]`), not everything; the
  manager prompt teaches this. Unmatched selectors are reported back to the
  manager so it can re-grant instead of guessing.

### A3 — Vague sub-agent instruction
Workers start with no shared memory. An instruction like "grade the rest"
without course/assignment ids makes the worker guess — and verifiers can only
catch *wrongly done* work, not *differently understood* work.
- **Notice:** reviewer FAIL verdicts citing missing ids; workers reporting
  assumptions.
- **Avoid:** the manager/planner prompts require self-contained instructions
  with every id. When it still happens, the review/verify gate is the net.

### A4 — Missing trigger words: wrong or no skill selected
Skill selection is deterministic keyword scoring over `triggers:` in
`skills/*.md`. A task phrased without any trigger runs the generic workflow —
no server routing, no checklist injection.
- **Notice:** the Workflow panel shows the selected skill (or none).
- **Avoid:** extend the skill's `triggers:` line; override the skill manually
  in the Workflow panel; see `scenarios/triage_eval_cases.md` for the
  phrases each shipped skill is expected to catch.

### A5 — Governance switched off in a real course
`APPROVAL_MODE=off` executes plans without the human pause; privacy off sends
real student names to the LLM provider; audit off leaves no trail.
- **Avoid:** the defaults are safe (approval `high`, privacy on, audit on).
  Only relax them in throwaway sandboxes — checklist item, not code.

### A6 — Testing writes against a live course
`grade_submission` posts real grades; `create_announcement` notifies every
enrolled student; assignments/quizzes/pages have **no delete tool** and must
be removed by hand.
- **Avoid:** sandbox course, fake students, `scenarios/*.md` phase warnings.
  This is the most consequential avoidable failure and it is purely
  operational.

## Not avoidable — design for survival

These will occur in normal use. The mitigations bound the damage and surface
the event; they cannot prevent it.

### N1 — User gives wrong input (the verifier cannot save you)
The user says course 71186 but means 71190, or "10 points" while the rubric
says 20. Every agent then does the *wrong thing correctly*: the worker
executes it, the reviewer passes it, the verifier confirms reality matches
the (wrong) intent.
- **Survive:** the human plan-approval pause is the designed catch point —
  the plan echoes the ids and values back before anything runs. High-risk
  approval cards repeat the target one more time (with real names revealed).
  Nothing downstream re-checks intent; read the plan.

### N2 — Voice mishearing (ASR)
Numbers, names, and dates are exactly what ASR gets wrong ("assignment
fifteen" → "fifty"). A misheard id flows into the task like any other wrong
input (N1).
- **Survive:** same catch points as N1; the transcript is shown before the
  plan runs, and typed input is always available for ids.

### N3 — LLM emits malformed output
Observed in practice: `google/diffusiongemma-26b-a4b-it` leaked
`<|channel>thought` scratchpad tokens and failed the initiator's JSON
classification. Any model can return unparseable tool arguments or verdicts
missing the PASS/FAIL first line.
- **Survive:** the initiator falls back to the keyword heuristic (`method:
  heuristic` in the Agents panel); tool-argument errors return to the model
  for retry; a verdict without a leading PASS is treated as FAIL (fail-closed).

### N4 — Approval times out while the user is away
An undecided approval card times out (default 600 s, `APPROVAL_TIMEOUT_S`)
and resolves as **denied**; interruption (barge-in, reload) also denies all
pending cards.
- **Survive:** deny-by-default is the intended direction. The worker learns
  the denial and reports it; the step stays open; approve mode/timeout are
  settings, not code.

### N5 — Canvas rate limiting / API errors mid-plan
Canvas meters each token with a leaky bucket; bursts get 403 "Rate Limit
Exceeded". Transient 5xx and permission 403s (e.g. student-role courses)
also happen mid-step.
- **Survive:** the MCP server paces, backs off, and retries rate-limited
  calls (`CANVAS_MAX_CONCURRENT`, `CANVAS_RATE_RETRIES`); hard errors surface
  in the worker's report and the step fails review rather than half-passing.
  Both audit logs (host `logs/`, server `examples/Canvas_MCP/logs/`) show
  which call failed.

### N6 — Reviewer/verifier share the model's blind spots
Verdicts come from the same LLM family as the worker: a systematic
misunderstanding can pass review and verification.
- **Survive:** the verifier re-reads *real state* with read-only tools
  (independent evidence, not opinion), and human approval remains the actual
  gate for high-risk writes. Do not approve writes you haven't read.

### N7 — Novel prompt injection in tool results
Submission text is untrusted student input that flows into the LLM. The
injection guard is pattern-based; a novel phrasing will get through it.
- **Survive:** layered containment rather than detection: sub-agent prompts
  treat tool results as data; workers hold only the tools their step needs;
  plan gating means no write grants exist outside an approved plan; approval
  cards precede risky writes; `injection_flagged` audit events mark what the
  guard *did* catch so you know to read the source.

### N8 — Session interrupted mid-plan
A reload or crash mid-execution leaves a plan with some steps done.
- **Survive:** workflow state serializes into the conversation save file
  (stage, per-step review/verify records); pending approvals die as denials
  (N4); completed Canvas writes are visible via `@verifier` before resuming.

## Reading the logs when something fails

- Host: `logs/session-*.jsonl` — every user input, grant expansion, tool
  call/result, approval, verdict, injection flag (browse in Settings →
  Privacy & Security).
- Server: `examples/Canvas_MCP/logs/canvas-mcp-*.jsonl` — every tool
  invocation with PII-masked args, ok/error, duration. Written by the MCP
  server process itself, so it is the independent cross-check when the host
  log and reality seem to disagree (disable with `CANVAS_MCP_AUDIT=0`).
- A denied approval must show `approval_result` `approved=false` and **no**
  subsequent `tool_result` on the host side — and no matching entry in the
  server log at all.
