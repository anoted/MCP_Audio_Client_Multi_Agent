/* NIM Audio Client frontend.
 *
 * Mic audio -> AudioWorklet (16 kHz Int16 chunks) -> energy VAD here ->
 * WebSocket binary frames to the server. Assistant TTS comes back as raw
 * PCM16 binary frames scheduled into an AudioContext. Speaking while audio
 * is playing (or the model is thinking) triggers barge-in: playback stops
 * instantly and the server cancels LLM + TTS generation.
 */
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const CHUNK_MS = 32;              // worklet chunk duration (512 samples @16 kHz)
const VAD_THRESHOLD = 0.015;      // RMS threshold for speech
const BARGE_IN_FACTOR = 2.2;      // stricter threshold while TTS is playing
const VAD_ATTACK_CHUNKS = 2;      // consecutive voiced chunks to open capture
const VAD_RELEASE_MS = 750;       // trailing silence that closes an utterance
const PREROLL_CHUNKS = 8;         // ~250 ms sent retroactively at speech start

let ws = null;
let wsReady = false;
let serverState = "connecting";   // listening | transcribing | thinking | speaking
let ttsSampleRate = 22050;

let micCtx = null, micStream = null, micLive = false;
let capturing = false, voicedRun = 0, silenceMs = 0;
let preroll = [];

let playCtx = null, nextPlayTime = 0;
const activeSources = new Set();

let currentAssistantEl = null;    // streaming bubble
const toolCards = new Map();      // tool_call id -> element

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const chatEl = $("chat"), chatWrap = $("chat-wrap");
const statusPill = $("status-pill"), connDot = $("conn-dot"), modelInfo = $("model-info");
const micBtn = $("mic-btn"), vadFill = $("vad-fill");
const textInput = $("text-input"), sendBtn = $("send-btn"), resetBtn = $("reset-btn");

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => { wsReady = true; connDot.className = "dot on"; };
  ws.onclose = () => {
    wsReady = false;
    connDot.className = "dot off";
    setState("disconnected");
    stopPlayback();
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) { playChunk(ev.data); return; }
    handleServerEvent(JSON.parse(ev.data));
  };
}

function send(obj) { if (wsReady) ws.send(JSON.stringify(obj)); }

function handleServerEvent(msg) {
  switch (msg.type) {
    case "config":
      ttsSampleRate = msg.tts_sample_rate;
      modelInfo.textContent = `${msg.model} · ${msg.voice}` +
        (msg.speech_enabled ? "" : " · ⚠ speech not configured");
      break;
    case "state":
      setState(msg.state);
      break;
    case "transcript":
      addUserMessage(msg.text);
      break;
    case "assistant_start":
      currentAssistantEl = null; // created lazily on first delta
      break;
    case "assistant_delta":
      appendAssistantText(msg.text);
      break;
    case "assistant_done":
      finalizeAssistant(false);
      break;
    case "interrupted":
      stopPlayback();
      finalizeAssistant(true);
      break;
    case "tool_call":
      addToolCard(msg);
      break;
    case "tool_result":
      completeToolCard(msg);
      break;
    case "tts_end":
      break;
    case "history_reset":
      chatEl.innerHTML = "";
      addNotice("Conversation cleared.");
      break;
    case "error":
      addError(msg.message);
      break;
  }
}

function setState(s) {
  serverState = s;
  statusPill.textContent = s;
  statusPill.className = `pill ${s}`;
}

// ---------------------------------------------------------------------------
// Chat rendering
// ---------------------------------------------------------------------------
function scrollChat() { chatWrap.scrollTop = chatWrap.scrollHeight; }

function addNotice(text) {
  const el = document.createElement("div");
  el.className = "notice";
  el.textContent = text;
  chatEl.appendChild(el);
  scrollChat();
}

function addError(text) {
  const el = document.createElement("div");
  el.className = "sys-error";
  el.textContent = text;
  chatEl.appendChild(el);
  scrollChat();
}

function addUserMessage(text) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.textContent = text;
  chatEl.appendChild(el);
  scrollChat();
}

function appendAssistantText(text) {
  if (!currentAssistantEl) {
    currentAssistantEl = document.createElement("div");
    currentAssistantEl.className = "msg assistant streaming";
    chatEl.appendChild(currentAssistantEl);
  }
  currentAssistantEl.textContent += text;
  scrollChat();
}

function finalizeAssistant(interrupted) {
  if (!currentAssistantEl) return;
  currentAssistantEl.classList.remove("streaming");
  if (interrupted) {
    const tag = document.createElement("span");
    tag.className = "interrupted-tag";
    tag.textContent = "⏹ interrupted";
    currentAssistantEl.appendChild(tag);
  }
  currentAssistantEl = null;
}

function addToolCard(msg) {
  // A tool call ends the current text bubble segment.
  finalizeAssistant(false);
  const card = document.createElement("div");
  card.className = "tool-card";
  card.innerHTML = `
    <div class="tool-head">
      <span class="tool-icon">🔧</span>
      <span class="tool-name"></span>
      <span class="tool-server"></span>
      <span class="tool-status">running…</span>
    </div>
    <div class="tool-body">
      <div class="lbl">arguments</div>
      <pre class="args"></pre>
      <div class="lbl">result</div>
      <pre class="result">…</pre>
    </div>`;
  card.querySelector(".tool-name").textContent = msg.tool;
  card.querySelector(".tool-server").textContent = `via ${msg.server}`;
  let args = msg.arguments;
  try { args = JSON.stringify(JSON.parse(msg.arguments), null, 2); } catch { /* raw */ }
  card.querySelector(".args").textContent = args;
  card.querySelector(".tool-head").addEventListener("click", () =>
    card.classList.toggle("open"));
  chatEl.appendChild(card);
  toolCards.set(msg.id, card);
  scrollChat();
}

function completeToolCard(msg) {
  const card = toolCards.get(msg.id);
  if (!card) return;
  card.classList.add(msg.ok ? "done" : "failed");
  card.querySelector(".tool-status").textContent = msg.ok ? "done" : "failed";
  card.querySelector(".result").textContent = msg.result;
  scrollChat();
}

// ---------------------------------------------------------------------------
// Microphone capture + VAD + barge-in
// ---------------------------------------------------------------------------
async function startMic() {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,   // keeps TTS playback out of the mic signal
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  micCtx = new AudioContext();
  await micCtx.audioWorklet.addModule("/static/pcm-worklet.js");
  const source = micCtx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(micCtx, "pcm-recorder");
  source.connect(node);
  node.port.onmessage = (ev) => onMicChunk(ev.data);
  micLive = true;
  micBtn.classList.add("live");
}

function stopMic() {
  if (capturing) { capturing = false; send({ type: "speech_end" }); }
  micLive = false;
  micBtn.classList.remove("live", "capturing");
  vadFill.style.width = "0";
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (micCtx) { micCtx.close(); micCtx = null; }
  preroll = [];
}

function rmsOf(int16) {
  let sum = 0;
  for (let i = 0; i < int16.length; i++) { const s = int16[i] / 32768; sum += s * s; }
  return Math.sqrt(sum / int16.length);
}

function assistantBusy() {
  const audioQueued = playCtx && nextPlayTime > playCtx.currentTime;
  return audioQueued || serverState === "speaking" || serverState === "thinking" ||
         serverState === "transcribing";
}

function onMicChunk(int16) {
  if (!micLive || !wsReady) return;
  const rms = rmsOf(int16);
  vadFill.style.width = `${Math.min(100, rms * 900)}%`;

  const busy = assistantBusy();
  const threshold = busy ? VAD_THRESHOLD * BARGE_IN_FACTOR : VAD_THRESHOLD;
  const voiced = rms > threshold;

  if (!capturing) {
    preroll.push(int16);
    if (preroll.length > PREROLL_CHUNKS) preroll.shift();
    voicedRun = voiced ? voicedRun + 1 : 0;
    if (voicedRun >= VAD_ATTACK_CHUNKS) {
      // Barge-in: user started talking over the assistant.
      if (busy) { stopPlayback(); send({ type: "interrupt" }); }
      capturing = true;
      silenceMs = 0;
      micBtn.classList.add("capturing");
      send({ type: "speech_start" });
      for (const c of preroll) ws.send(c.buffer);
      preroll = [];
    }
  } else {
    ws.send(int16.buffer);
    if (rms > VAD_THRESHOLD) {
      silenceMs = 0;
    } else {
      silenceMs += CHUNK_MS;
      if (silenceMs >= VAD_RELEASE_MS) {
        capturing = false;
        voicedRun = 0;
        micBtn.classList.remove("capturing");
        send({ type: "speech_end" });
      }
    }
  }
}

micBtn.addEventListener("click", async () => {
  if (micLive) { stopMic(); return; }
  try { await startMic(); }
  catch (err) { addError(`Microphone error: ${err.message}`); }
});

// ---------------------------------------------------------------------------
// TTS playback (raw PCM16 -> scheduled AudioBuffers)
// ---------------------------------------------------------------------------
function playChunk(arrayBuffer) {
  if (arrayBuffer.byteLength < 2) return;
  if (!playCtx) playCtx = new AudioContext();
  if (playCtx.state === "suspended") playCtx.resume();

  const int16 = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;

  const buf = playCtx.createBuffer(1, f32.length, ttsSampleRate);
  buf.getChannelData(0).set(f32);
  const src = playCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playCtx.destination);
  const t = Math.max(playCtx.currentTime + 0.06, nextPlayTime);
  src.start(t);
  nextPlayTime = t + buf.duration;
  activeSources.add(src);
  src.onended = () => activeSources.delete(src);
}

function stopPlayback() {
  for (const src of activeSources) { try { src.stop(); } catch { /* already done */ } }
  activeSources.clear();
  nextPlayTime = 0;
}

// ---------------------------------------------------------------------------
// Typed input
// ---------------------------------------------------------------------------
function sendTyped() {
  const text = textInput.value.trim();
  if (!text) return;
  stopPlayback();
  send({ type: "text", text });
  textInput.value = "";
}
sendBtn.addEventListener("click", sendTyped);
textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendTyped(); });
resetBtn.addEventListener("click", () => { stopPlayback(); send({ type: "reset" }); });

// ---------------------------------------------------------------------------
// MCP panel
// ---------------------------------------------------------------------------
const mcpList = $("mcp-list"), mcpForm = $("mcp-form"), mcpError = $("mcp-error");
const mcpTransport = $("mcp-transport"), mcpCommand = $("mcp-command"), mcpUrl = $("mcp-url");

mcpTransport.addEventListener("change", () => {
  const stdio = mcpTransport.value === "stdio";
  mcpCommand.classList.toggle("hidden", !stdio);
  mcpUrl.classList.toggle("hidden", stdio);
});

async function refreshServers() {
  try {
    const res = await fetch("/api/mcp/servers");
    renderServers(await res.json());
  } catch { /* server restarting */ }
}

function renderServers(servers) {
  mcpList.innerHTML = "";
  if (!servers.length) {
    mcpList.innerHTML = `<div class="mcp-empty">No MCP servers registered.
      Try the bundled demo: transport <b>stdio</b>, command
      <code>python examples/demo_mcp_server.py</code>.</div>`;
    return;
  }
  for (const s of servers) {
    const el = document.createElement("div");
    el.className = "mcp-server";
    const dot = s.connected ? "on" : (s.error ? "off" : "warn");
    el.innerHTML = `
      <div class="row">
        <span class="dot ${dot}"></span>
        <span class="name"></span>
        <span class="transport"></span>
        <span class="spacer"></span>
        <button class="reconnect ghost" title="Reconnect">↻</button>
        <button class="remove ghost" title="Remove">✕</button>
      </div>
      <div class="detail"></div>
      ${s.error ? '<div class="err"></div>' : ""}
      <div class="tool-chips"></div>`;
    el.querySelector(".name").textContent = s.name;
    el.querySelector(".transport").textContent = s.transport;
    el.querySelector(".detail").textContent = s.url || s.command;
    if (s.error) el.querySelector(".err").textContent = s.error;
    const chips = el.querySelector(".tool-chips");
    for (const t of s.tools) {
      const chip = document.createElement("span");
      chip.className = "tool-chip";
      chip.textContent = t.name;
      chip.title = t.description;
      chips.appendChild(chip);
    }
    el.querySelector(".remove").addEventListener("click", async () => {
      await fetch(`/api/mcp/servers/${encodeURIComponent(s.name)}`, { method: "DELETE" });
      refreshServers();
    });
    el.querySelector(".reconnect").addEventListener("click", async () => {
      el.querySelector(".reconnect").textContent = "…";
      await fetch(`/api/mcp/servers/${encodeURIComponent(s.name)}/reconnect`, { method: "POST" });
      refreshServers();
    });
    mcpList.appendChild(el);
  }
}

mcpForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  mcpError.textContent = "";
  const body = {
    name: $("mcp-name").value.trim(),
    transport: mcpTransport.value,
    command: mcpCommand.value.trim(),
    url: mcpUrl.value.trim(),
  };
  const btn = mcpForm.querySelector("button[type=submit]");
  btn.disabled = true; btn.textContent = "Connecting…";
  try {
    const res = await fetch("/api/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      mcpError.textContent = err.detail || `Failed (${res.status})`;
    } else {
      mcpForm.reset();
      mcpTransport.dispatchEvent(new Event("change"));
    }
  } catch (err) {
    mcpError.textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = "Add & connect";
    refreshServers();
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
connect();
refreshServers();
setInterval(refreshServers, 10000);
