# NIM Audio Client

A browser voice assistant with server-side speech processing:

- **Speech-to-text** — NVIDIA NIM **Parakeet CTC 1.1B** ASR (Riva gRPC)
- **Text LLM** — any **OpenAI-compatible** chat-completions endpoint (OpenAI, NVIDIA
  `integrate.api.nvidia.com`, vLLM, Ollama, …), streamed, with tool calling
- **Text-to-speech** — NVIDIA NIM **Magpie TTS Multilingual** (Riva gRPC), streamed
  sentence-by-sentence for low latency
- **Barge-in** — speak over the assistant and it stops instantly; the server
  cancels LLM + TTS generation mid-stream
- **MCP tools** — register MCP servers (stdio / streamable HTTP / SSE) from the UI;
  their tools are exposed to the LLM and every tool call + result is visualized
  in the chat as an expandable card

```
Browser ──16 kHz PCM──▶ FastAPI ──▶ Parakeet ASR (NIM, gRPC)
   ▲                       │
   │                       ├──▶ OpenAI-compatible LLM ◀──▶ MCP servers (tools)
   │                       │
   └──◀─PCM audio──────────┴──▶ Magpie TTS (NIM, gRPC)
```

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

## How interruption (barge-in) works

- The mic worklet streams 32 ms chunks continuously; an energy VAD with
  attack/release opens and closes utterances (with ~250 ms pre-roll so word
  onsets aren't clipped).
- While TTS is playing, the VAD uses a stricter threshold (plus browser echo
  cancellation keeps the speaker signal out of the mic). Two consecutive voiced
  chunks while the assistant is speaking/thinking ⇒ the client stops playback
  immediately and sends `interrupt`.
- Server-side, the whole ASR→LLM→TTS response runs as one cancellable asyncio
  task. On interrupt it is cancelled, a stop event ends the Riva TTS stream, and
  every audio chunk carries a generation number so late chunks from an already
  cancelled response are dropped instead of reaching the speaker. The partial
  assistant text stays in history marked `[interrupted by user]`.

## MCP servers

Register servers in the right-hand panel (or edit `mcp_servers.json`, created on
first registration). Tools are namespaced as `server__tool` for the LLM. A demo
stdio server ships in `examples/demo_mcp_server.py`:

> name `demo` · transport `stdio` · command `python examples/demo_mcp_server.py`

Then ask by voice: *"what time is it?"* or *"roll three dice"* — the tool call
and its result render as a card in the conversation.

REST API: `GET/POST /api/mcp/servers`, `DELETE /api/mcp/servers/{name}`,
`POST /api/mcp/servers/{name}/reconnect`.

## Project layout

```
app/main.py         FastAPI app, WebSocket, MCP REST API
app/session.py      per-connection pipeline + interruption handling
app/speech.py       Riva NIM ASR/TTS (Parakeet, Magpie)
app/llm.py          OpenAI-compatible streaming client + sentence splitter
app/mcp_manager.py  MCP registration, connections, tool routing
static/             browser UI (VAD, playback, chat + tool cards, MCP panel)
examples/           demo stdio MCP server
```
