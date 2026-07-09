# Voice Client

A browser voice assistant with server-side speech processing and a multi-agent
work system:

- **Speech-to-text** — NVIDIA NIM **Parakeet CTC 1.1B** ASR (Riva gRPC)
- **Text LLM** — any **OpenAI-compatible** chat-completions endpoint (OpenAI, NVIDIA
  `integrate.api.nvidia.com`, vLLM, Ollama, …), streamed, with tool calling;
  the model can be switched from the UI (✎ next to the model name)
- **Text-to-speech** — NVIDIA NIM **Magpie TTS Multilingual** (Riva gRPC), streamed
  sentence-by-sentence for low latency
- **Barge-in** — speak over the assistant and it stops instantly; the server
  cancels LLM + TTS generation mid-stream
- **MCP tools** — register MCP servers (stdio / streamable HTTP / SSE) from the UI;
  their tools are exposed to the LLM and every tool call + result is visualized
  in the chat as an expandable card
- **Multi-agent** — four primary agents plus manager-spawned sub-agents (below)
- **Save / load** — conversations persist as one JSON file each in
  `conversations/`; light & dark mode

```
Browser ──16 kHz PCM──▶ FastAPI ──▶ Parakeet ASR (NIM, gRPC)
   ▲                       │
   │                       ├──▶ active agent (OpenAI-compatible LLM)
   │                       │        ├──▶ MCP servers (tools)
   │                       │        └──▶ sub-agents (manager only)
   └──◀─PCM audio──────────┴──▶ Magpie TTS (NIM, gRPC)
```

## Agents

| Agent | Tools | Role |
|---|---|---|
| `@assistant` | all | default conversational agent |
| `@explorer` | read-only | investigates and reports, never modifies |
| `@planner` | read-only + `submit_plan` | breaks a task into a to-do list (shown in the **Plan** panel) |
| `@manager` | `run_planner`, `run_subagent`, `set_todo_status` | plans via the planner, then works through the plan by deploying sub-agents |
| *initiator* | — | one-shot background agent |

- **Initiator** runs once at startup (and again whenever the MCP tool inventory
  changes): it classifies every tool's **access** (*read* or *modify*) and its
  **category/keywords** (e.g. `assignments`, `modules`, `quizzes`) — with the
  LLM, falling back to a keyword heuristic on the tool names — assigns tool
  sets to the agents above, and discards its working context. Its status is
  shown in the **Agents** panel.
- **Switching agents**: type `@explorer`, `@planner` (`@plan`), `@manager` or
  `@assistant` in the text field — an autocomplete popup appears at `@` — or
  click an agent in the sidebar. A bare mention just switches; a mention plus
  text switches and sends. **Voice always goes to the active agent** (no voice
  commands to complicate audio). Each agent keeps its own conversation history.
- **Sub-agents**: the manager creates each one with a name, a self-contained
  instruction, and **tool grants by selector** — `read` (every read-only
  tool), `write`, `all`, a category/keyword like `assignments` or `modules`,
  `read:<category>` / `write:<category>` to narrow, or exact tool names. The
  initiator's classification expands selectors to the actual tool list, so
  sub-agents get a whole capability (e.g. all 8 module tools) instead of one
  or two hand-picked tools. Streamed output, tool calls and the final report
  render in a collapsible 🤖 card; the card auto-collapses when done.
- **Manager plans itself**: with `run_planner` the manager launches the
  planner (read-only tools + `submit_plan`) in the background and the
  resulting plan lands in the Plan panel — no need to visit `@planner` first.

Typical flow: *"@manager set up X"* → manager calls `run_planner` → plan
appears in the Plan panel → manager marks steps in-progress, runs sub-agents
with category grants, ticks steps off. (Running `@planner` manually first
still works.)

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env     # then edit .env
```

In `.env` set at minimum:

| Variable | What |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Your OpenAI-compatible text model |
| `NVIDIA_API_KEY` | `nvapi-…` key from [build.nvidia.com](https://build.nvidia.com) |
| `CONVERSATIONS_DIR` | optional; folder for saved conversations (default `conversations/`) |

The default ASR/TTS function IDs target the hosted NIM API
([parakeet-ctc-1.1b-asr](https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr),
[magpie-tts-multilingual](https://build.nvidia.com/nvidia/magpie-tts-multilingual));
if NVIDIA rotates them, copy the current `function-id` from the model page.
For a **locally deployed NIM container**, point `RIVA_ASR_SERVER` /
`RIVA_TTS_SERVER` at it (e.g. `localhost:50051`), clear the function IDs, and
set `RIVA_USE_SSL=false`.

## Run

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, click the mic, and talk. Typed input works too
(useful before speech keys are configured). Use **http://localhost / 127.0.0.1**
or HTTPS — browsers only allow microphone access in secure contexts.

## Quality of life

- **Save / load** — 💾 in the header: save the current conversation under a
  name (all agent histories, the plan, and the full chat transcript are
  restored on load), and load or delete previous ones. One JSON file per
  conversation in `conversations/`.
- **Light / dark mode** — ☀️/🌙 in the header; the choice is remembered.
- **Model switch** — click ✎ next to the model name, type the exact model
  name, press Enter. Applies from the next reply.

## How interruption (barge-in) works

- The mic worklet streams 32 ms chunks continuously; an energy VAD with
  attack/release opens and closes utterances (with ~250 ms pre-roll so word
  onsets aren't clipped).
- While TTS is playing, the VAD uses a stricter threshold (plus browser echo
  cancellation keeps the speaker signal out of the mic). Two consecutive voiced
  chunks while the assistant is speaking/thinking ⇒ the client stops playback
  immediately and sends `interrupt`.
- Server-side, the whole ASR→LLM→TTS response — sub-agents included — runs as
  one cancellable asyncio task. On interrupt it is cancelled, a stop event ends
  the Riva TTS stream, and every audio chunk carries a generation number so
  late chunks from an already cancelled response are dropped instead of
  reaching the speaker. The partial assistant text stays in history marked
  `[interrupted by user]`.

## MCP servers

Register servers in the right-hand panel (or edit `mcp_servers.json`, created on
first registration). Tools are namespaced as `server__tool` for the LLM. A demo
stdio server ships in `examples/demo_mcp_server.py`:

> name `demo` · transport `stdio` · command `python examples/demo_mcp_server.py`

Then ask by voice: *"what time is it?"* or *"roll three dice"* — the tool call
and its result render as a card in the conversation.

### Canvas MCP

`examples/Canvas_MCP/` is a self-contained Canvas LMS MCP server (~31 tools:
assignments, submissions/grading, quizzes, modules, pages, files,
announcements). `mcp_servers.json` registers it over stdio through the
`mcpagents` conda env:

> name `Canvas` · transport `stdio` · command
> `conda run --no-capture-output -n mcpagents python examples/Canvas_MCP/canvas_mcp_server.py`

Its `CANVAS_BASE_URL` / `CANVAS_API_TOKEN` live in `examples/Canvas_MCP/.env`
(loaded by the server itself). **The tools operate on a live Canvas instance**
— see `scenarios/canvas_agents_scenario.md` for a guided multi-agent test
scenario and which phases are safe outside a sandbox course.

REST API: `GET/POST /api/mcp/servers`, `DELETE /api/mcp/servers/{name}`,
`POST /api/mcp/servers/{name}/reconnect`, plus `GET /api/agents`,
`POST /api/model`, `GET/DELETE /api/conversations`.

## Project layout

```
app/main.py          FastAPI app, WebSocket, REST (MCP, agents, model, conversations)
app/session.py       per-connection multi-agent pipeline + interruption handling
app/agents.py        agent profiles, virtual tools, Initiator (access + category classifier, grant selectors)
app/conversations.py save/load conversations (one JSON file each)
app/speech.py        Riva NIM ASR/TTS (Parakeet, Magpie)
app/llm.py           OpenAI-compatible streaming client + sentence splitter
app/mcp_manager.py   MCP registration, connections, tool routing
static/              browser UI (VAD, playback, chat, agents/plan/MCP panels)
examples/            demo stdio MCP server + Canvas_MCP (separate sub-project)
scenarios/           guided test scenarios (Canvas multi-agent run)
```
