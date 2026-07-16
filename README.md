# Voice Workflow Client

A browser voice assistant with server-side speech processing and a
**multi-agent task → review → verification workflow**:

- **Speech-to-text** — NVIDIA NIM **Parakeet CTC 1.1B** ASR (Riva gRPC)
- **Text LLM** — any **OpenAI-compatible** chat-completions endpoint, streamed,
  with tool calling; switchable from Settings
- **Text-to-speech** — NVIDIA NIM **Magpie TTS Multilingual** (Riva gRPC),
  streamed sentence-by-sentence
- **Echo-safe barge-in** — speak over the assistant to stop it; playing the
  assistant through speakers does **not** interrupt it (see Audio below)
- **MCP tools, prompts + apps** — register MCP servers from Settings; tools
  are exposed to the LLM, server-published **prompt templates** appear in the
  Prompts panel, and tools that return an app payload render as **interactive
  panels** (Canvas course explorer, grade charts) that can push data back into
  the workflow
- **Workflow governance** — skill playbooks, human plan approval, per-tool
  approval checkpoints, enforced reviewer/verifier passes, privacy-preserving
  processing, prompt-injection guard, and a per-session audit log

```
Browser ──16 kHz PCM──▶ FastAPI ──▶ Parakeet ASR (NIM, gRPC)
   ▲                       │
   │   workflow state,     ├──▶ active agent (OpenAI-compatible LLM)
   │   approvals, apps ◀───┤        ├──▶ MCP servers (privacy + injection
   │                       │        │    guard + approval gates in between)
   └──◀─PCM audio──────────┴──▶ Magpie TTS (NIM, gRPC)
```

## The workflow

Tell the **manager** (the default agent) what you want. It triages first:
simple questions and quick look-ups are answered directly (at most one
read-only worker) with no plan. A complex or state-modifying task runs this
pipeline — each arrow is enforced by the server, not just by prompts, and
workers only ever receive modifying tools while an approved plan is executing:

```
complex task ─▶ manager calls run_planner ─▶ skill selected
     ─▶ planner (read-only research ─▶ plan)
     ─▶ ⏸ HUMAN: approve plan (button, or say “approve”)
     ─▶ per step: worker sub-agent (scoped tools, server-routed)
              ├─ ⏸ HUMAN: approval checkpoint on risky tool calls
              ├─ reviewer  → PASS/FAIL   (work-product QA, no tools)
              └─ verifier  → PASS/FAIL   (re-checks real state, read-only)
        step can only be marked done after both PASS (if it modified state)
     ─▶ complete
```

| Agent | Tools | Role |
|---|---|---|
| `@manager` | delegation only | triages; plans complex tasks → approval → execute → review → verify |
| `@planner` | read-only + `submit_plan` | researches and produces the plan (usually invoked by the manager) |
| `@explorer` | read-only | investigates and reports on demand |
| `@reviewer` | read-only | quality gate — judges work products (PASS/FAIL) |
| `@verifier` | read-only | independent inspector — re-checks actual state |
| `@assistant` | all | general chat, kept **outside** the workflow as an example |

- **Skills** (`skills/*.md`) are workflow playbooks: plan guidance, a review
  checklist, verification steps, the MCP **servers** the workflow routes to,
  and trigger keywords. The best match is selected automatically when a task
  starts (override in the Workflow panel). Shipped skills: grading, content
  builder, quiz builder, announcements, read-only course audit.
- **Server routing**: sub-agent tool grants are expanded by the initiator's
  read/modify + category classification and restricted to the active skill's
  servers; selectors also support `Canvas:read`, `server:Canvas`, categories,
  and exact names.
- **Human approval checkpoints**: modifying tool calls pause on an approval
  card (Approve/Deny + note). Modes in Settings: every write / high-risk only
  (default: grades, publish, delete, announce, upload…) / off. Undecided
  requests time out as denied; interruption denies them too.
- **Privacy-preserving processing**: student names/emails in people-type tool
  results are replaced with stable pseudonyms (`Student-1`) before reaching
  the LLM provider; outgoing tool arguments are de-pseudonymized so real
  content lands in Canvas. Emails/phones are always masked.
- **Injection guard**: tool results matching prompt-injection patterns are
  wrapped in a security notice and flagged (⚠️ on the card + activity log).
- **Audit log**: every user input, tool call/result, approval, sub-agent run,
  review/verify verdict, injection flag, and app action is written to
  `logs/session-*.jsonl` and streamed to the Activity panel. Browse files in
  Settings → Privacy & Security. The Canvas MCP server additionally keeps its
  own independent tool-call log (`examples/Canvas_MCP/logs/`, PII-masked
  arguments) as a cross-check written by the other side of the boundary.
- **MCP prompts**: prompt templates published by connected servers show up in
  the **Prompts** panel with one input per declared argument; the rendered
  prompt is sent to the active agent as ordinary user input, so triage, plan
  approval, tool approvals, and the audit log all apply unchanged.
- **Adversarial tests**: `python -m unittest discover -s tests` runs 82
  offline tests that attack the control points (grant escape, denied
  approvals, plan-gate bypass, injection, privacy leaks, app-bridge writes)
  and pin skill parsing + trigger scoring and prompt listing/rendering.
- **Failure modes**: `FAILURE_MODES.md` catalogs how the system fails —
  avoidable failures vs. those it can only survive (wrong user input, ASR
  mishearing, malformed LLM output, approval timeouts) — and where each one
  shows up in the logs.

## MCP apps (interactive visualization)

Tools that return `{"mcp_app": {resource, title, data}, "summary": ...}`
render as sandboxed iframe panels in the chat; the LLM only sees the summary.
Apps talk to the host via `postMessage` and may:

- call **read-only** tools through a bridge (writes are refused server-side),
- push items into the **workflow context** — chips above the input that are
  attached to your next message,
- prefill the chat input ("ask" buttons).

The bundled Canvas server ships a **course explorer** (browse courses →
modules/assignments/quizzes/pages/announcements, inline grade histograms,
“+ workflow” on every item — open it from the **Apps** panel) and three chart
tools the LLM can call: `render_grade_distribution`,
`render_assignment_averages`, `render_course_progress`.

## Audio: why it no longer interrupts itself

The old build played TTS through raw WebAudio, which Chrome's echo canceller
ignores — speaker output re-entered the mic and tripped barge-in. Now:

1. TTS is routed through a `MediaStreamDestination` into a hidden `<audio>`
   element, which **is** part of the browser's AEC reference, so speaker
   audio is subtracted from the mic signal.
2. A software echo gate compares mic energy against the **known playback
   envelope** (we know exactly what's playing) with an adaptive coupling
   estimate — echo-level sound is ignored, real voices punch through.
3. Barge-in while the assistant speaks requires ~0.3 s of sustained speech
   above both gates; the noise floor auto-calibrates when the mic starts.
4. Settings → Audio: **Smart** (default) or **Manual** (never auto-interrupt;
   use the ⏹ button or Esc), plus a mic-sensitivity slider.

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env     # then edit .env
```

In `.env` set at minimum `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and
`NVIDIA_API_KEY` (from [build.nvidia.com](https://build.nvidia.com)).
Optional governance env defaults: `APPROVAL_MODE` (`all|high|off`),
`PRIVACY_ENABLED`, `INJECTION_GUARD`, `AUDIT_ENABLED`, `LOGS_DIR`,
`SKILLS_DIR`. Settings changed in the UI persist to `app_settings.json`.

For a locally deployed NIM container point `RIVA_ASR_SERVER` /
`RIVA_TTS_SERVER` at it, clear the function IDs, set `RIVA_USE_SSL=false`.

## Run

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, click the mic, and describe a task. Typed input
works too. Use localhost/HTTPS — browsers only allow mic access in secure
contexts. Run the tests with `python -m unittest discover -s tests`.

## UI

- **Settings** — ⚙️ opens an overlay (✕ top right): model/voice, approval
  mode, audio/barge-in, privacy & security toggles + log browser, MCP server
  registration, and the skills list.
- **Workflow panel** — stage bar (Plan → Approve → Execute → Done), active
  skill, plan with per-step badges (✎ modified state, R✓/R✗ review,
  V✓/V✗ verify), and the plan approval controls.
- **Prompts panel** — server-published prompt templates; **Use** opens an
  argument form, **Send to agent** renders the template on the server and
  sends it as your message.
- **Save / load** — 💾 in the header; one JSON file per conversation in
  `conversations/` (agent histories, workflow state, and the full transcript
  including approvals and apps are restored).
- **Light / dark mode** — ☀️/🌙; apps re-theme live.

## Canvas MCP

`examples/Canvas_MCP/` is a self-contained Canvas LMS MCP server (~37 tools:
assignments, submissions/grading, quizzes, modules, pages, files,
announcements, plus the explorer/chart apps, `canvas://` course resources,
and four reusable prompts — `grade_homework`, `assess_single_submission`,
`build_quiz`, `build_course_module` — surfaced in the client's Prompts
panel). `mcp_servers.json` registers it
over stdio through the `mcpagents` conda env. Its `CANVAS_BASE_URL` /
`CANVAS_API_TOKEN` live in `examples/Canvas_MCP/.env`. The server writes a
JSONL audit log of every tool call (PII-masked) to
`examples/Canvas_MCP/logs/` — env `CANVAS_MCP_AUDIT=0` disables it,
`CANVAS_MCP_LOG_DIR` moves it.

> ⚠️ **The Canvas tools operate on a live Canvas instance.** Read
> `SECURITY_CHECKLIST.md` before running write workflows, and use the guided
> sandbox tests: `scenarios/canvas_agents_scenario.md` (content building),
> `scenarios/canvas_grading_scenario.md` (grading), and
> `scenarios/triage_eval_cases.md` (routing eval table).

REST API: `GET/POST /api/mcp/servers`, `DELETE|POST /api/mcp/servers/{name}[/reconnect]`,
`GET /api/agents`, `GET /api/skills`, `GET /api/apps`, `GET /api/prompts`,
`POST /api/prompts/render`, `GET /api/logs[/{name}]`,
`GET /api/config`, `PUT /api/settings`, `POST /api/model`,
`GET/DELETE /api/conversations`.

## Project layout

```
app/main.py            FastAPI app, WebSocket, REST (MCP, agents, skills, logs, settings)
app/session.py         per-connection pipeline: agents, workflow enforcement,
                       approval gates, privacy/injection filtering, app bridge
app/agents.py          agent roles, virtual tools, Initiator (classifier + grant routing)
app/workflow.py        workflow state machine, approval gate, risk model
app/skills.py          skill registry (skills/*.md playbooks)
app/privacy.py         pseudonymizer + prompt-injection guard
app/audit.py           JSONL audit logger + live feed
app/conversations.py   save/load conversations
app/speech.py          Riva NIM ASR/TTS      app/llm.py  OpenAI-compatible client
app/mcp_manager.py     MCP registration, connections, tools, ui:// resources
static/                browser UI (echo-safe audio, workflow panel, settings
                       overlay, approval cards, MCP-app iframes)
skills/                workflow playbooks (Canvas grading, content, quiz, …)
tests/                 offline unit + adversarial suite (unittest)
examples/Canvas_MCP/   Canvas LMS MCP server + interactive apps (own audit log in logs/)
scenarios/             guided test scenarios + triage/skill-selection eval cases
SECURITY_CHECKLIST.md  manual verification + hardening checklist
FAILURE_MODES.md       failure-mode catalog: avoidable vs. survivable-only
```
