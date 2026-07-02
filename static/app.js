/* Voice Client frontend.
 *
 * Mic audio -> AudioWorklet (16 kHz Int16 chunks) -> energy VAD here ->
 * WebSocket binary frames to the server. Assistant TTS comes back as raw
 * PCM16 binary frames scheduled into an AudioContext. Speaking while audio
 * is playing (or the model is thinking) triggers barge-in: playback stops
 * instantly and the server cancels LLM + TTS generation.
 *
 * Multi-agent UI: @agent mentions in the text field switch the active agent
 * (voice always talks to the active one); sub-agents spawned by the manager
 * stream into collapsible cards inside the chat.
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
let currentAgent = "assistant";   // active agent
let streamingAgent = "assistant"; // agent of the bubble being streamed
let agents = [];                  // [{name, description, access}]
let lastVoice = "";               // TTS voice string for the header line
const toolCards = new Map();      // tool_call id -> element
const subCards = new Map();       // subagent id -> {card, textEl, body, calls: Map}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const chatEl = $("chat"), chatWrap = $("chat-wrap");
const statusPill = $("status-pill"), connDot = $("conn-dot"), modelInfo = $("model-info");
const micBtn = $("mic-btn"), vadFill = $("vad-fill");
const textInput = $("text-input"), sendBtn = $("send-btn"), resetBtn = $("reset-btn");
const agentChip = $("active-agent"), agentList = $("agent-list");
const initiatorStatus = $("initiator-status"), todoList = $("todo-list");
const mentionPop = $("mention-pop");
const themeBtn = $("theme-btn");
const convBtn = $("conv-btn"), convPanel = $("conv-panel");
const convName = $("conv-name"), convSaveBtn = $("conv-save-btn"), convList = $("conv-list");
const modelEditBtn = $("model-edit-btn"), modelInput = $("model-input");

// ---------------------------------------------------------------------------
// Theme (light / dark)
// ---------------------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themeBtn.textContent = theme === "light" ? "🌙" : "☀️";
  themeBtn.title = theme === "light" ? "Switch to dark mode" : "Switch to light mode";
  try { localStorage.setItem("voice-client-theme", theme); } catch { /* private mode */ }
}
themeBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
applyTheme((() => {
  try { return localStorage.getItem("voice-client-theme") || "dark"; }
  catch { return "dark"; }
})());

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
      lastVoice = msg.voice;
      updateModelInfo(msg.model, msg.speech_enabled);
      if (msg.agents) { agents = msg.agents; renderAgents(); }
      setActiveAgent(msg.agent || "assistant", false);
      break;
    case "state":
      setState(msg.state);
      break;
    case "agent_changed":
      setActiveAgent(msg.agent, true);
      break;
    case "transcript":
      addUserMessage(msg.text);
      break;
    case "assistant_start":
      currentAssistantEl = null; // created lazily on first delta
      streamingAgent = msg.agent || currentAgent;
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
      interruptSubCards();
      break;
    case "tool_call":
      addToolCard(msg);
      break;
    case "tool_result":
      completeToolCard(msg);
      break;
    case "subagent_start":
      addSubCard(msg);
      break;
    case "subagent_delta":
      appendSubText(msg);
      break;
    case "subagent_tool_call":
      addSubToolCall(msg);
      break;
    case "subagent_tool_result":
      completeSubToolCall(msg);
      break;
    case "subagent_done":
      finishSubCard(msg);
      break;
    case "todos":
      renderTodos(msg.todos);
      break;
    case "saved":
      addNotice(`Conversation saved as “${msg.name}”.`);
      refreshConversations();
      break;
    case "loaded":
      chatEl.innerHTML = "";
      toolCards.clear();
      subCards.clear();
      currentAssistantEl = null;
      replayTranscript(msg.transcript || []);
      renderTodos(msg.todos || []);
      setActiveAgent(msg.agent || "assistant", false);
      addNotice(`Loaded conversation “${msg.name}”.`);
      break;
    case "tts_end":
      break;
    case "history_reset":
      chatEl.innerHTML = "";
      toolCards.clear();
      subCards.clear();
      currentAssistantEl = null;
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
// Agents
// ---------------------------------------------------------------------------
function setActiveAgent(name, announce) {
  const changed = name !== currentAgent;
  currentAgent = name;
  agentChip.textContent = `@${name}`;
  renderAgents();
  if (announce && changed) addNotice(`Switched to @${name}.`);
}

function renderAgents() {
  agentList.innerHTML = "";
  for (const a of agents) {
    const el = document.createElement("div");
    el.className = "agent-item" + (a.name === currentAgent ? " active" : "");
    el.innerHTML = `<div class="agent-name"></div>
                    <div class="agent-desc"></div>
                    <div class="agent-access"></div>`;
    el.querySelector(".agent-name").textContent = `@${a.name}`;
    el.querySelector(".agent-desc").textContent = a.description;
    el.querySelector(".agent-access").textContent = a.access || "";
    el.addEventListener("click", () => send({ type: "set_agent", agent: a.name }));
    agentList.appendChild(el);
  }
}

async function refreshAgents() {
  try {
    const res = await fetch("/api/agents");
    const data = await res.json();
    agents = data.agents || agents;
    renderAgents();
    const init = data.initiator || {};
    let text = `initiator: ${init.status || "idle"}`;
    if (init.status === "done" && init.total > 0) {
      text = `initiator: ${init.total} tools → ${init.read} read · ` +
             `${init.modify} modify (${init.method})`;
    } else if (init.status === "done") {
      text = "initiator: no tools to classify";
    } else if (init.status === "running") {
      text = "initiator: classifying tools…";
    }
    initiatorStatus.textContent = text;
  } catch { /* server restarting */ }
}

// ---------------------------------------------------------------------------
// @mention popup
// ---------------------------------------------------------------------------
let mentionItems = [], mentionIdx = 0;

function updateMentionPop() {
  const m = /^@([\w-]*)$/.exec(textInput.value);
  if (!m || !agents.length) { hideMentionPop(); return; }
  const prefix = m[1].toLowerCase();
  mentionItems = agents.filter((a) => a.name.startsWith(prefix));
  if (!mentionItems.length) { hideMentionPop(); return; }
  mentionIdx = Math.min(mentionIdx, mentionItems.length - 1);
  mentionPop.innerHTML = "";
  mentionItems.forEach((a, i) => {
    const el = document.createElement("div");
    el.className = "mention-item" + (i === mentionIdx ? " sel" : "");
    el.innerHTML = `<b></b><span></span>`;
    el.querySelector("b").textContent = `@${a.name}`;
    el.querySelector("span").textContent = a.description;
    el.addEventListener("mousedown", (e) => { e.preventDefault(); pickMention(i); });
    mentionPop.appendChild(el);
  });
  mentionPop.classList.remove("hidden");
}

function hideMentionPop() { mentionPop.classList.add("hidden"); mentionItems = []; }

function pickMention(i) {
  const a = mentionItems[i];
  if (!a) return;
  textInput.value = `@${a.name} `;
  hideMentionPop();
  textInput.focus();
}

textInput.addEventListener("input", () => { mentionIdx = 0; updateMentionPop(); });
textInput.addEventListener("blur", () => setTimeout(hideMentionPop, 150));
textInput.addEventListener("keydown", (e) => {
  if (mentionItems.length) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      mentionIdx = (mentionIdx + 1) % mentionItems.length; updateMentionPop(); return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      mentionIdx = (mentionIdx - 1 + mentionItems.length) % mentionItems.length;
      updateMentionPop(); return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault(); pickMention(mentionIdx); return;
    }
    if (e.key === "Escape") { hideMentionPop(); return; }
  }
  if (e.key === "Enter") sendTyped();
});

agentChip.addEventListener("click", () => {
  textInput.value = "@";
  textInput.focus();
  mentionIdx = 0;
  updateMentionPop();
});

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

function newAssistantBubble(agent) {
  const el = document.createElement("div");
  el.className = "msg assistant";
  if (agent && agent !== "assistant") {
    const tag = document.createElement("div");
    tag.className = "agent-tag";
    tag.textContent = `@${agent}`;
    el.appendChild(tag);
  }
  const body = document.createElement("span");
  body.className = "msg-body";
  el.appendChild(body);
  chatEl.appendChild(el);
  return el;
}

function appendAssistantText(text) {
  if (!currentAssistantEl) {
    currentAssistantEl = newAssistantBubble(streamingAgent);
    currentAssistantEl.classList.add("streaming");
  }
  currentAssistantEl.querySelector(".msg-body").textContent += text;
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

function addAssistantFull(agent, text, interrupted) {
  const el = newAssistantBubble(agent);
  el.querySelector(".msg-body").textContent = text;
  if (interrupted) {
    const tag = document.createElement("span");
    tag.className = "interrupted-tag";
    tag.textContent = "⏹ interrupted";
    el.appendChild(tag);
  }
}

// ---- tool cards -----------------------------------------------------------
function buildToolCard(name, server, args) {
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
  card.querySelector(".tool-name").textContent = name;
  card.querySelector(".tool-server").textContent = `via ${server}`;
  let pretty = args;
  try { pretty = JSON.stringify(JSON.parse(args), null, 2); } catch { /* raw */ }
  card.querySelector(".args").textContent = pretty;
  card.querySelector(".tool-head").addEventListener("click", () =>
    card.classList.toggle("open"));
  return card;
}

function settleToolCard(card, ok, result) {
  card.classList.add(ok ? "done" : "failed");
  card.querySelector(".tool-status").textContent = ok ? "done" : "failed";
  card.querySelector(".result").textContent = result;
}

function addToolCard(msg) {
  // A tool call ends the current text bubble segment.
  finalizeAssistant(false);
  const card = buildToolCard(msg.tool, msg.server, msg.arguments);
  chatEl.appendChild(card);
  toolCards.set(msg.id, card);
  scrollChat();
}

function completeToolCard(msg) {
  const card = toolCards.get(msg.id);
  if (!card) return;
  settleToolCard(card, msg.ok, msg.result);
  scrollChat();
}

// ---- sub-agent cards (collapsible) -----------------------------------------
function buildSubCard(id, name, task, tools) {
  const card = document.createElement("div");
  card.className = "sub-card open";
  card.innerHTML = `
    <div class="sub-head">
      <span class="sub-caret">▾</span>
      <span class="sub-icon">🤖</span>
      <span class="sub-name"></span>
      <span class="sub-hint">sub-agent</span>
      <span class="sub-status">running…</span>
    </div>
    <div class="sub-body">
      <div class="lbl">instruction</div>
      <div class="sub-task"></div>
      <div class="lbl">tools granted</div>
      <div class="sub-tools"></div>
      <div class="sub-text"></div>
    </div>`;
  card.querySelector(".sub-name").textContent = name;
  card.querySelector(".sub-task").textContent = task;
  const toolsEl = card.querySelector(".sub-tools");
  if (tools && tools.length) {
    for (const t of tools) {
      const chip = document.createElement("span");
      chip.className = "tool-chip";
      chip.textContent = t;
      toolsEl.appendChild(chip);
    }
  } else {
    toolsEl.textContent = "(none)";
  }
  card.querySelector(".sub-head").addEventListener("click", () => {
    card.classList.toggle("open");
    card.querySelector(".sub-caret").textContent =
      card.classList.contains("open") ? "▾" : "▸";
  });
  return card;
}

function addSubCard(msg) {
  finalizeAssistant(false);
  const card = buildSubCard(msg.id, msg.name, msg.task, msg.tools);
  chatEl.appendChild(card);
  subCards.set(msg.id, {
    card,
    body: card.querySelector(".sub-body"),
    textEl: card.querySelector(".sub-text"),
    calls: new Map(),
  });
  scrollChat();
}

function appendSubText(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  sub.textEl.textContent += msg.text;
  scrollChat();
}

function addSubToolCall(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const det = document.createElement("details");
  det.className = "sub-tool";
  det.innerHTML = `<summary>🔧 <span class="st-name"></span>
                   <span class="st-status">running…</span></summary>
                   <pre class="st-args"></pre><pre class="st-result">…</pre>`;
  det.querySelector(".st-name").textContent = msg.name;
  let pretty = msg.arguments;
  try { pretty = JSON.stringify(JSON.parse(msg.arguments), null, 2); } catch { /* raw */ }
  det.querySelector(".st-args").textContent = pretty;
  sub.body.appendChild(det);
  sub.calls.set(msg.call_id, det);
  // Fresh text element so post-tool deltas appear below the tool line.
  sub.textEl = document.createElement("div");
  sub.textEl.className = "sub-text";
  sub.body.appendChild(sub.textEl);
  scrollChat();
}

function completeSubToolCall(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const det = sub.calls.get(msg.call_id);
  if (!det) return;
  det.querySelector(".st-status").textContent = msg.ok ? "done" : "failed";
  det.classList.add(msg.ok ? "done" : "failed");
  det.querySelector(".st-result").textContent = msg.result;
  scrollChat();
}

function finishSubCard(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const status = sub.card.querySelector(".sub-status");
  status.textContent = msg.ok ? "✓ done" : "✗ failed";
  sub.card.classList.add(msg.ok ? "done" : "failed");
  // Auto-collapse when finished; the head still shows the outcome.
  sub.card.classList.remove("open");
  sub.card.querySelector(".sub-caret").textContent = "▸";
  scrollChat();
}

function interruptSubCards() {
  for (const sub of subCards.values()) {
    const status = sub.card.querySelector(".sub-status");
    if (status.textContent === "running…") {
      status.textContent = "⏹ interrupted";
      sub.card.classList.add("failed");
    }
  }
}

function addSubagentFull(e) {
  const card = buildSubCard(e.id, e.name, e.task, e.tools);
  card.classList.remove("open");
  card.querySelector(".sub-caret").textContent = "▸";
  const body = card.querySelector(".sub-body");
  const textEl = card.querySelector(".sub-text");
  textEl.textContent = e.text || "";
  for (const ev of e.events || []) {
    const det = document.createElement("details");
    det.className = "sub-tool " + (ev.ok ? "done" : "failed");
    det.innerHTML = `<summary>🔧 <span class="st-name"></span>
                     <span class="st-status"></span></summary>
                     <pre class="st-args"></pre><pre class="st-result"></pre>`;
    det.querySelector(".st-name").textContent = ev.name;
    det.querySelector(".st-status").textContent = ev.ok ? "done" : "failed";
    det.querySelector(".st-args").textContent = ev.arguments;
    det.querySelector(".st-result").textContent = ev.result;
    body.appendChild(det);
  }
  const status = card.querySelector(".sub-status");
  status.textContent = e.status === "done" ? "✓ done" :
    e.status === "interrupted" ? "⏹ interrupted" : "✗ failed";
  card.classList.add(e.status === "done" ? "done" : "failed");
  chatEl.appendChild(card);
}

// ---- transcript replay (conversation load) ---------------------------------
function replayTranscript(events) {
  for (const e of events) {
    if (e.kind === "user") addUserMessage(e.text);
    else if (e.kind === "assistant") addAssistantFull(e.agent, e.text, e.interrupted);
    else if (e.kind === "tool") {
      const card = buildToolCard(e.tool || e.name, e.server, e.arguments);
      settleToolCard(card, e.ok, e.result || "");
      chatEl.appendChild(card);
    } else if (e.kind === "subagent") addSubagentFull(e);
  }
  scrollChat();
}

// ---------------------------------------------------------------------------
// Plan / to-do list panel
// ---------------------------------------------------------------------------
function renderTodos(todos) {
  todoList.innerHTML = "";
  if (!todos || !todos.length) {
    todoList.innerHTML =
      `<div class="side-empty">No plan yet — ask <b>@planner</b> for one.</div>`;
    return;
  }
  todos.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = `todo-item ${t.status}`;
    const icon = t.status === "done" ? "✓" : t.status === "in_progress" ? "▶" : "○";
    el.innerHTML = `<span class="todo-icon"></span><span class="todo-text"></span>`;
    el.querySelector(".todo-icon").textContent = icon;
    el.querySelector(".todo-text").textContent = `${i + 1}. ${t.text}`;
    todoList.appendChild(el);
  });
}

// ---------------------------------------------------------------------------
// Conversations (save / load)
// ---------------------------------------------------------------------------
convBtn.addEventListener("click", () => {
  convPanel.classList.toggle("hidden");
  if (!convPanel.classList.contains("hidden")) refreshConversations();
});
document.addEventListener("click", (e) => {
  if (!convPanel.classList.contains("hidden") &&
      !convPanel.contains(e.target) && e.target !== convBtn) {
    convPanel.classList.add("hidden");
  }
});

convSaveBtn.addEventListener("click", () => {
  send({ type: "save", name: convName.value.trim() });
  convName.value = "";
});

async function refreshConversations() {
  try {
    const res = await fetch("/api/conversations");
    renderConversations(await res.json());
  } catch { /* server restarting */ }
}

function renderConversations(items) {
  convList.innerHTML = "";
  if (!items.length) {
    convList.innerHTML = `<div class="side-empty">No saved conversations yet.</div>`;
    return;
  }
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "conv-item";
    el.innerHTML = `
      <div class="conv-meta">
        <div class="conv-name"></div>
        <div class="conv-sub"></div>
      </div>
      <button class="load ghost">Load</button>
      <button class="del ghost" title="Delete">✕</button>`;
    el.querySelector(".conv-name").textContent = it.name;
    el.querySelector(".conv-sub").textContent =
      [it.saved_at, it.preview].filter(Boolean).join(" · ");
    el.querySelector(".load").addEventListener("click", () => {
      send({ type: "load", name: it.name });
      convPanel.classList.add("hidden");
    });
    el.querySelector(".del").addEventListener("click", async () => {
      await fetch(`/api/conversations/${encodeURIComponent(it.name)}`,
        { method: "DELETE" });
      refreshConversations();
    });
    convList.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// Model switch
// ---------------------------------------------------------------------------
function updateModelInfo(model, speechEnabled) {
  modelInfo.textContent = `${model} · ${lastVoice}` +
    (speechEnabled ? "" : " · ⚠ speech not configured");
  modelInfo.dataset.model = model;
  modelInfo.dataset.speech = speechEnabled ? "1" : "";
}

modelEditBtn.addEventListener("click", () => {
  modelInput.value = modelInfo.dataset.model || "";
  modelInfo.classList.add("hidden");
  modelEditBtn.classList.add("hidden");
  modelInput.classList.remove("hidden");
  modelInput.focus();
  modelInput.select();
});

function closeModelInput() {
  modelInput.classList.add("hidden");
  modelInfo.classList.remove("hidden");
  modelEditBtn.classList.remove("hidden");
}

modelInput.addEventListener("keydown", async (e) => {
  if (e.key === "Escape") { closeModelInput(); return; }
  if (e.key !== "Enter") return;
  const model = modelInput.value.trim();
  if (!model) { closeModelInput(); return; }
  try {
    const res = await fetch("/api/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    if (res.ok) {
      updateModelInfo(model, !!modelInfo.dataset.speech);
      addNotice(`LLM model set to ${model} (applies to the next reply).`);
    } else {
      const err = await res.json().catch(() => ({}));
      addError(err.detail || `Model change failed (${res.status}).`);
    }
  } catch (err) {
    addError(`Model change failed: ${err.message}`);
  }
  closeModelInput();
});
modelInput.addEventListener("blur", closeModelInput);

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
  hideMentionPop();
}
sendBtn.addEventListener("click", sendTyped);
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
      refreshAgents();
    });
    el.querySelector(".reconnect").addEventListener("click", async () => {
      el.querySelector(".reconnect").textContent = "…";
      await fetch(`/api/mcp/servers/${encodeURIComponent(s.name)}/reconnect`, { method: "POST" });
      refreshServers();
      refreshAgents();
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
    refreshAgents();
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
connect();
refreshServers();
refreshAgents();
setInterval(() => { refreshServers(); refreshAgents(); }, 10000);
