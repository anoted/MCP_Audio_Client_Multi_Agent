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
| `@manager` | `run_subagent`, `set_todo_status` | works through the plan by deploying sub-agents |
| *initiator* | — | one-shot background agent |

- **Initiator** runs once at startup (and again whenever the MCP tool inventory
  changes): it classifies every tool as *read* or *modify* — with the LLM,
  falling back to a keyword heuristic — assigns tool sets to the agents above,
  and discards its working context. Its status is shown in the **Agents** panel.
- **Switching agents**: type `@explorer`, `@planner` (`@plan`), `@manager` or
  `@assistant` in the text field — an autocomplete popup appears at `@` — or
  click an agent in the sidebar. A bare mention just switches; a mention plus
  text switches and sends. **Voice always goes to the active agent** (no voice
  commands to complicate audio). Each agent keeps its own conversation history.
- **Sub-agents**: the manager creates each one with a name, a self-contained
  instruction, and an explicit tool allowlist (it only sees those tools). Its
  streamed output, tool calls and final report render in a collapsible 🤖 card
  in the chat; the card auto-collapses when the sub-agent finishes.

Typical flow: *"@planner set up X"* → plan appears in the Plan panel →
*"@manager go"* → manager marks steps in-progress, runs sub-agents, ticks
steps off.

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

REST API: `GET/POST /api/mcp/servers`, `DELETE /api/mcp/servers/{name}`,
`POST /api/mcp/servers/{name}/reconnect`, plus `GET /api/agents`,
`POST /api/model`, `GET/DELETE /api/conversations`.

## Project layout

```
app/main.py          FastAPI app, WebSocket, REST (MCP, agents, model, conversations)
app/session.py       per-connection multi-agent pipeline + interruption handling
app/agents.py        agent profiles, virtual tools, Initiator (tool classifier)
app/conversations.py save/load conversations (one JSON file each)
app/speech.py        Riva NIM ASR/TTS (Parakeet, Magpie)
app/llm.py           OpenAI-compatible streaming client + sentence splitter
app/mcp_manager.py   MCP registration, connections, tool routing
static/              browser UI (VAD, playback, chat, agents/plan/MCP panels)
examples/            demo stdio MCP server
```
