/* Voice Workflow Client frontend.
 *
 * Audio path (self-interruption fix):
 *   Mic -> AudioWorklet (16 kHz Int16) -> adaptive VAD here -> WebSocket.
 *   TTS PCM is scheduled into an AudioContext whose output is routed through
 *   a MediaStreamDestination into a hidden <audio> element — audio played via
 *   a media element is part of the browser's echo-cancellation reference, so
 *   speaker output is subtracted from the mic signal (raw WebAudio output is
 *   not, which is why the old build interrupted itself on speakers).
 *   On top of AEC, a software gate compares mic energy against the *known*
 *   playback envelope with an adaptive coupling estimate, and barge-in while
 *   the assistant speaks requires sustained speech (~0.3 s), like current
 *   voice assistants. "Manual" mode disables auto barge-in entirely (⏹/Esc).
 *
 * Workflow UI: plan approval, per-step review/verify badges, human approval
 * cards for risky tool calls, skill selection, MCP app iframes (course
 * explorer / charts) with a read-only tool bridge and workflow-context chips,
 * live activity log, and a settings overlay (X top-right).
 */
"use strict";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------
const CHUNK_MS = 32;               // worklet chunk duration (512 samples @16 kHz)
const VAD_BASE = 0.012;            // baseline RMS threshold for speech
const VAD_ATTACK_CHUNKS = 2;       // voiced chunks to open capture (idle)
const BARGE_ATTACK_CHUNKS = 9;     // ~290 ms of sustained speech to barge in
const VAD_RELEASE_MS = 750;        // trailing silence that closes an utterance
const PREROLL_CHUNKS = 10;         // ~320 ms sent retroactively at speech start
const ECHO_MARGIN = 2.3;           // mic must exceed est. echo by this factor
const INTERRUPT_COOLDOWN_MS = 600; // ignore VAD right after an interrupt

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let ws = null;
let wsReady = false;
let serverState = "connecting";    // listening | transcribing | thinking | speaking
let cfg = {};                      // last /api/config-shaped payload
let ttsSampleRate = 22050;

let micCtx = null, micStream = null, micLive = false;
let capturing = false, voicedRun = 0, silenceMs = 0;
let preroll = [];
let noiseFloor = 0.004;            // adaptive ambient noise estimate
let calibrating = 0;               // chunks left in initial calibration
let calibSamples = [];
let echoGain = 1.5;                // adaptive speaker->mic coupling estimate
let interruptCooldownUntil = 0;

let playCtx = null, playDest = null, nextPlayTime = 0;
const activeSources = new Set();
const playbackSegs = [];           // {start, end (wall ms), rms}

let currentAssistantEl = null;
let currentAgent = "manager";
let streamingAgent = "manager";
let agents = [];
let skills = [];
let workflowState = { stage: "idle", task: "", skill: null, todos: [] };
let wfChips = [];                  // [{label, text}] context for the next message
const toolCards = new Map();
const subCards = new Map();
const approvalCards = new Map();
const appCards = new Map();

const audioPrefs = loadAudioPrefs();

function loadAudioPrefs() {
  const def = { mode: "smart", sensitivity: 1.0 };
  try { return { ...def, ...(JSON.parse(localStorage.getItem("vc-audio") || "{}")) }; }
  catch { return def; }
}
function saveAudioPrefs() {
  try { localStorage.setItem("vc-audio", JSON.stringify(audioPrefs)); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const chatEl = $("chat"), chatWrap = $("chat-wrap");
const statusPill = $("status-pill"), connDot = $("conn-dot"), modelChip = $("model-chip");
const micBtn = $("mic-btn"), stopBtn = $("stop-btn"), vadFill = $("vad-fill");
const textInput = $("text-input"), sendBtn = $("send-btn"), resetBtn = $("reset-btn");
const agentChip = $("active-agent"), agentList = $("agent-list");
const initiatorStatus = $("initiator-status"), todoList = $("todo-list");
const mentionPop = $("mention-pop");
const themeBtn = $("theme-btn");
const convBtn = $("conv-btn"), convPanel = $("conv-panel");
const convName = $("conv-name"), convSaveBtn = $("conv-save-btn"), convList = $("conv-list");
const ttsAudioEl = $("tts-out");
const activityLog = $("activity-log");
const wfStageBadge = $("wf-stage-badge"), wfStages = $("wf-stages"), wfTask = $("wf-task");
const skillSelect = $("skill-select");
const planApproval = $("plan-approval"), planNote = $("plan-note");
const wfContextBar = $("wf-context-bar"), wfChipsEl = $("wf-chips");
const appsSection = $("apps-section"), appsList = $("apps-list");
const promptsSection = $("prompts-section"), promptsList = $("prompts-list");
const rvDock = $("rv-dock"), rvBody = $("rv-body"), rvCount = $("rv-count");

// Review & Verify dock (bottom-right of the chat column): reviewer/verifier
// runs render here so the main thread stays clean.
function dockAppend(card) {
  rvDock.classList.remove("hidden");
  rvBody.appendChild(card);
  rvCount.textContent = String(rvBody.querySelectorAll(".sub-card").length);
  rvBody.scrollTop = rvBody.scrollHeight;
}
function dockClear() {
  rvBody.innerHTML = "";
  rvCount.textContent = "";
  rvDock.classList.add("hidden");
}
$("rv-head").addEventListener("click", () => {
  rvDock.classList.toggle("collapsed");
  $("rv-caret").textContent = rvDock.classList.contains("collapsed") ? "▸" : "▾";
});

// ---------------------------------------------------------------------------
// Icons — crisp stroke SVGs (Feather-style), replacing emoji everywhere
// ---------------------------------------------------------------------------
const ICONS = {
  mic: '<path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z"/><path d="M19 12a7 7 0 0 1-14 0"/><line x1="12" y1="19" x2="12" y2="22"/>',
  sun: '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="4.9" y1="4.9" x2="7" y2="7"/><line x1="17" y1="17" x2="19.1" y2="19.1"/><line x1="4.9" y1="19.1" x2="7" y2="17"/><line x1="17" y1="7" x2="19.1" y2="4.9"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  compass: '<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
  list: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  shieldCheck: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 11.5 11 13.5 15 9.5"/>',
  message: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  bot: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="10" y="10" width="4" height="4"/><line x1="9" y1="2" x2="9" y2="5"/><line x1="15" y1="2" x2="15" y2="5"/><line x1="9" y1="19" x2="9" y2="22"/><line x1="15" y1="19" x2="15" y2="22"/><line x1="19" y1="9" x2="22" y2="9"/><line x1="19" y1="15" x2="22" y2="15"/><line x1="2" y1="9" x2="5" y2="9"/><line x1="2" y1="15" x2="5" y2="15"/>',
  grid: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  volume: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
  volumeX: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>',
};
function icon(name, size = 18) {
  return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

// static header/footer icons
$("logo-icon").innerHTML = icon("mic", 23);
$("conv-btn").innerHTML = icon("save");
$("settings-btn").innerHTML = icon("gear");
$("stop-btn").innerHTML = icon("stop", 17);
$("rv-ico").innerHTML = icon("shieldCheck", 16);

// ---------------------------------------------------------------------------
// Voice output mute — silences TTS only; the response keeps running
// ---------------------------------------------------------------------------
const voiceBtn = $("voice-btn");
let voiceMuted = (() => {
  try { return localStorage.getItem("vc-voice-muted") === "1"; }
  catch { return false; }
})();
function updateVoiceBtn() {
  voiceBtn.innerHTML = icon(voiceMuted ? "volumeX" : "volume", 15);
  voiceBtn.classList.toggle("muted", voiceMuted);
  voiceBtn.title = voiceMuted
    ? "Voice output is muted — click to unmute (applies from the next sentence)"
    : "Mute voice output — the response keeps running silently";
}
voiceBtn.addEventListener("click", () => {
  voiceMuted = !voiceMuted;
  try { localStorage.setItem("vc-voice-muted", voiceMuted ? "1" : "0"); } catch { /* ok */ }
  if (voiceMuted) stopPlayback();     // immediate local silence
  send({ type: "set_voice", enabled: !voiceMuted });
  updateVoiceBtn();
});
updateVoiceBtn();

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themeBtn.innerHTML = icon(theme === "light" ? "moon" : "sun");
  themeBtn.title = theme === "light" ? "Switch to dark mode" : "Switch to light mode";
  try { localStorage.setItem("voice-client-theme", theme); } catch { /* private mode */ }
  broadcastToApps({ mcpApp: true, type: "theme", theme });
}
themeBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
applyTheme((() => {
  try { return localStorage.getItem("voice-client-theme") || "dark"; }
  catch { return "dark"; }
})());
function currentTheme() { return document.documentElement.dataset.theme || "dark"; }

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
      cfg = msg;
      ttsSampleRate = msg.tts_sample_rate;
      updateModelChip();
      if (msg.agents) { agents = msg.agents; renderAgents(); }
      if (msg.skills) { skills = msg.skills; renderSkillOptions(); renderSkillsTab(); }
      setActiveAgent(msg.agent || "manager", false);
      syncSettingsForm();
      if (voiceMuted) send({ type: "set_voice", enabled: false });
      break;
    case "voice":
      voiceMuted = !msg.enabled;
      updateVoiceBtn();
      break;
    case "state": setState(msg.state); break;
    case "agent_changed": setActiveAgent(msg.agent, true); break;
    case "transcript": addUserMessage(msg.text); break;
    case "assistant_start":
      currentAssistantEl = null;
      streamingAgent = msg.agent || currentAgent;
      break;
    case "assistant_delta": appendAssistantText(msg.text); break;
    case "assistant_done": finalizeAssistant(false); break;
    case "interrupted":
      stopPlayback();
      finalizeAssistant(true);
      interruptSubCards();
      cancelPendingApprovalCards();
      break;
    case "tool_call": addToolCard(msg); break;
    case "tool_result": completeToolCard(msg); break;
    case "subagent_start": addSubCard(msg); break;
    case "subagent_delta": appendSubText(msg); break;
    case "subagent_tool_call": addSubToolCall(msg); break;
    case "subagent_tool_result": completeSubToolCall(msg); break;
    case "subagent_done": finishSubCard(msg); break;
    case "todos": renderTodos(msg.todos); break;
    case "workflow": renderWorkflow(msg); break;
    case "plan_review":
      renderWorkflow(msg);
      addNotice("Plan ready — approve or request changes in the Workflow panel (or just say “approve”).");
      break;
    case "approval_request": addApprovalCard(msg); break;
    case "approval_resolved": resolveApprovalCard(msg); break;
    case "app": addAppCard(msg); break;
    case "app_tool_result": routeAppToolResult(msg); break;
    case "log": addActivity(msg.entry); break;
    case "saved":
      addNotice(`Conversation saved as “${msg.name}”.`);
      refreshConversations();
      break;
    case "loaded":
      clearChat();
      replayTranscript(msg.transcript || []);
      renderWorkflow(msg.workflow || { stage: "idle", todos: msg.todos || [] });
      setActiveAgent(msg.agent || "manager", false);
      addNotice(`Loaded conversation “${msg.name}”.`);
      break;
    case "tts_end": break;
    case "history_reset":
      clearChat();
      renderWorkflow({ stage: "idle", task: "", skill: null, todos: [] });
      addNotice("Conversation cleared.");
      break;
    case "error": addError(msg.message); break;
  }
}

function clearChat() {
  chatEl.innerHTML = "";
  toolCards.clear();
  subCards.clear();
  approvalCards.clear();
  appCards.clear();
  currentAssistantEl = null;
  dockClear();
}

function setState(s) {
  serverState = s;
  const pending = [...approvalCards.values()].some((c) => c.pending);
  statusPill.textContent = pending ? "awaiting approval" : s;
  statusPill.className = `pill ${pending ? "approval" : s}`;
  const busy = s === "thinking" || s === "speaking" || s === "transcribing";
  stopBtn.classList.toggle("hidden", !busy);
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------
const AGENT_ICONS = {
  manager: "compass", planner: "list", explorer: "search", reviewer: "eye",
  verifier: "shieldCheck", assistant: "message",
};
const ROLE_ICONS = { worker: "bot", planner: "list", reviewer: "eye",
                     verifier: "shieldCheck" };
const DOCK_ROLES = new Set(["reviewer", "verifier"]);

function setActiveAgent(name, announce) {
  const changed = name !== currentAgent;
  currentAgent = name;
  agentChip.textContent = `@${name}`;
  renderAgents();
  if (announce && changed) addNotice(`Switched to @${name}.`);
}

function agentThread(a) {
  if (a.thread) return a.thread;
  return ["manager", "planner", "explorer"].includes(a.name) ? "workflow" : a.name;
}

function renderAgents() {
  agentList.innerHTML = "";
  const shared = agents.filter((a) => agentThread(a) === "workflow");
  const indep = agents.filter(
    (a) => agentThread(a) !== "workflow" && a.workflow !== false);
  const general = agents.filter((a) => a.workflow === false);
  const divider = (text) => {
    const div = document.createElement("div");
    div.className = "agent-divider";
    div.textContent = text;
    return div;
  };
  const addItem = (a, host) => {
    const el = document.createElement("div");
    el.className = "agent-item" + (a.name === currentAgent ? " active" : "");
    el.innerHTML = `<span class="agent-ico"></span><div class="agent-txt">
        <div class="agent-name"></div><div class="agent-desc"></div></div>`;
    el.querySelector(".agent-ico").innerHTML = icon(AGENT_ICONS[a.name] || "bot");
    el.querySelector(".agent-name").textContent = `@${a.name}`;
    el.querySelector(".agent-desc").textContent = a.description;
    el.title = a.access || "";
    el.addEventListener("click", () => send({ type: "set_agent", agent: a.name }));
    host.appendChild(el);
  };
  if (shared.length) {
    agentList.appendChild(divider("shared session — switching steers it"));
    const grp = document.createElement("div");
    grp.className = "agent-group";
    shared.forEach((a) => addItem(a, grp));
    agentList.appendChild(grp);
  }
  if (indep.length) {
    agentList.appendChild(divider("independent checkers"));
    indep.forEach((a) => addItem(a, agentList));
  }
  if (general.length) {
    agentList.appendChild(divider("outside the workflow"));
    general.forEach((a) => addItem(a, agentList));
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
  const tag = document.createElement("div");
  tag.className = "agent-tag";
  tag.innerHTML = `${icon(AGENT_ICONS[agent] || "bot", 13)} `;
  tag.append(`@${agent}`);
  el.appendChild(tag);
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
      <span class="tool-icon">${icon("wrench", 14)}</span>
      <span class="tool-name"></span>
      <span class="tool-server"></span>
      <span class="tool-flag hidden" title="Injection guard flagged this result">${icon("alert", 16)}</span>
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
function settleToolCard(card, ok, result, flags) {
  card.classList.add(ok ? "done" : "failed");
  card.querySelector(".tool-status").textContent = ok ? "done" : "failed";
  card.querySelector(".result").textContent = result;
  if (flags && flags.length) {
    const f = card.querySelector(".tool-flag");
    f.classList.remove("hidden");
    f.title = `Injection guard: ${flags.join(", ")}`;
  }
}
function addToolCard(msg) {
  finalizeAssistant(false);
  const card = buildToolCard(msg.tool, msg.server, msg.arguments);
  chatEl.appendChild(card);
  toolCards.set(msg.id, card);
  scrollChat();
}
function completeToolCard(msg) {
  const card = toolCards.get(msg.id);
  if (!card) return;
  settleToolCard(card, msg.ok, msg.result, msg.flags);
  scrollChat();
}

// ---- sub-agent cards --------------------------------------------------------
function buildSubCard(id, name, task, tools, role) {
  const card = document.createElement("div");
  card.className = `sub-card open role-${role || "worker"}`;
  card.innerHTML = `
    <div class="sub-head">
      <span class="sub-caret">▾</span>
      <span class="sub-icon"></span>
      <span class="sub-name"></span>
      <span class="sub-hint"></span>
      <span class="sub-status">running…</span>
    </div>
    <div class="sub-body">
      <div class="lbl">instruction</div>
      <div class="sub-task"></div>
      <div class="lbl">tools granted</div>
      <div class="sub-tools"></div>
      <div class="sub-text"></div>
    </div>`;
  card.querySelector(".sub-icon").innerHTML = icon(ROLE_ICONS[role] || "bot", 16);
  card.querySelector(".sub-name").textContent = name;
  card.querySelector(".sub-hint").textContent = role || "sub-agent";
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
  const card = buildSubCard(msg.id, msg.name, msg.task, msg.tools, msg.role);
  const docked = DOCK_ROLES.has(msg.role);
  if (docked) dockAppend(card);
  else { chatEl.appendChild(card); scrollChat(); }
  subCards.set(msg.id, {
    card,
    docked,
    body: card.querySelector(".sub-body"),
    textEl: card.querySelector(".sub-text"),
    calls: new Map(),
  });
}
function subScroll(sub) {
  if (sub && sub.docked) rvBody.scrollTop = rvBody.scrollHeight;
  else scrollChat();
}
function appendSubText(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  sub.textEl.textContent += msg.text;
  subScroll(sub);
}
function addSubToolCall(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const det = document.createElement("details");
  det.className = "sub-tool";
  det.innerHTML = `<summary>${icon("wrench", 13)} <span class="st-name"></span>
                   <span class="st-status">running…</span></summary>
                   <pre class="st-args"></pre><pre class="st-result">…</pre>`;
  det.querySelector(".st-name").textContent = msg.name;
  let pretty = msg.arguments;
  try { pretty = JSON.stringify(JSON.parse(msg.arguments), null, 2); } catch { /* raw */ }
  det.querySelector(".st-args").textContent = pretty;
  sub.body.appendChild(det);
  sub.calls.set(msg.call_id, det);
  sub.textEl = document.createElement("div");
  sub.textEl.className = "sub-text";
  sub.body.appendChild(sub.textEl);
  subScroll(sub);
}
function completeSubToolCall(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const det = sub.calls.get(msg.call_id);
  if (!det) return;
  det.querySelector(".st-status").textContent =
    (msg.ok ? "done" : "failed") + (msg.flags && msg.flags.length ? " ⚠" : "");
  det.classList.add(msg.ok ? "done" : "failed");
  det.querySelector(".st-result").textContent = msg.result;
  subScroll(sub);
}
function finishSubCard(msg) {
  const sub = subCards.get(msg.id);
  if (!sub) return;
  const status = sub.card.querySelector(".sub-status");
  let label = msg.ok ? "✓ done" : "✗ failed";
  if (msg.ok && /^\s*PASS/i.test(msg.result)) label = "✓ PASS";
  else if (msg.ok && /^\s*FAIL/i.test(msg.result)) label = "✗ FAIL";
  status.textContent = label;
  sub.card.classList.add(msg.ok && !/^\s*FAIL/i.test(msg.result) ? "done" : "failed");
  sub.card.classList.remove("open");
  sub.card.querySelector(".sub-caret").textContent = "▸";
  subScroll(sub);
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
  const card = buildSubCard(e.id, e.name, e.task, e.tools, e.role);
  card.classList.remove("open");
  card.dataset.dock = DOCK_ROLES.has(e.role) ? "1" : "";
  card.querySelector(".sub-caret").textContent = "▸";
  const body = card.querySelector(".sub-body");
  card.querySelector(".sub-text").textContent = e.text || "";
  for (const ev of e.events || []) {
    const det = document.createElement("details");
    det.className = "sub-tool " + (ev.ok ? "done" : "failed");
    det.innerHTML = `<summary>${icon("wrench", 13)} <span class="st-name"></span>
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
  if (card.dataset.dock) dockAppend(card);
  else chatEl.appendChild(card);
}

// ---- approval cards ---------------------------------------------------------
function buildApprovalCard(msg, resolved) {
  const card = document.createElement("div");
  card.className = "approval-card" + (msg.risk === "high" ? " high" : "");
  card.innerHTML = `
    <div class="ap-head">
      <span class="ap-icon">${icon("shield", 17)}</span>
      <span class="ap-title">Approval needed</span>
      <span class="ap-risk"></span>
      <span class="ap-status"></span>
    </div>
    <div class="ap-body">
      <div class="ap-line"><b class="ap-tool"></b> <span class="ap-via"></span></div>
      <pre class="ap-args"></pre>
      <div class="ap-controls">
        <input class="ap-note" placeholder="optional note">
        <button class="ap-approve primary">✓ Approve</button>
        <button class="ap-deny danger-ghost">✗ Deny</button>
      </div>
    </div>`;
  card.querySelector(".ap-risk").textContent =
    msg.risk === "high" ? "high risk" : "write";
  card.querySelector(".ap-tool").textContent = msg.tool;
  card.querySelector(".ap-via").textContent =
    `on ${msg.server} · requested by ${msg.caller}`;
  card.querySelector(".ap-args").textContent = msg.arguments || "{}";
  if (!resolved) {
    card.querySelector(".ap-approve").addEventListener("click", () => {
      send({ type: "approval", id: msg.id, approved: true,
             note: card.querySelector(".ap-note").value.trim() });
    });
    card.querySelector(".ap-deny").addEventListener("click", () => {
      send({ type: "approval", id: msg.id, approved: false,
             note: card.querySelector(".ap-note").value.trim() });
    });
  }
  return card;
}
function addApprovalCard(msg) {
  finalizeAssistant(false);
  const card = buildApprovalCard(msg, false);
  chatEl.appendChild(card);
  approvalCards.set(msg.id, { card, pending: true });
  setState(serverState); // refresh pill -> "awaiting approval"
  scrollChat();
}
function settleApprovalCard(entry, approved, note) {
  entry.pending = false;
  const card = entry.card;
  card.classList.add(approved ? "approved" : "denied");
  card.querySelector(".ap-status").textContent =
    approved ? "✓ approved" : "✗ denied";
  const ctl = card.querySelector(".ap-controls");
  ctl.innerHTML = note ? `<span class="ap-notetxt">note: </span>` : "";
  if (note) ctl.querySelector(".ap-notetxt").append(note);
}
function resolveApprovalCard(msg) {
  const entry = approvalCards.get(msg.id);
  if (!entry) return;
  settleApprovalCard(entry, msg.approved, msg.note);
  setState(serverState);
  scrollChat();
}
function cancelPendingApprovalCards() {
  for (const entry of approvalCards.values()) {
    if (entry.pending) settleApprovalCard(entry, false, "cancelled by interruption");
  }
  setState(serverState);
}
function addApprovalFull(e) {
  const card = buildApprovalCard(e, true);
  const entry = { card, pending: false };
  if (e.approved !== null && e.approved !== undefined) {
    settleApprovalCard(entry, e.approved, e.note);
  } else {
    settleApprovalCard(entry, false, "unresolved");
  }
  chatEl.appendChild(card);
}

// ---- MCP app cards (sandboxed iframes + bridge) -------------------------------
function addAppCard(msg) {
  finalizeAssistant(false);
  const card = document.createElement("div");
  card.className = "app-card";
  card.innerHTML = `
    <div class="app-head">
      <span class="app-icon">${icon("grid", 16)}</span>
      <span class="app-title"></span>
      <span class="app-server"></span>
      <button class="app-max ghost tiny" title="Expand">⤢</button>
      <button class="app-toggle ghost tiny" title="Collapse">▾</button>
    </div>`;
  card.querySelector(".app-title").textContent = msg.title;
  card.querySelector(".app-server").textContent = `via ${msg.server}`;
  const iframe = document.createElement("iframe");
  iframe.className = "app-frame";
  iframe.setAttribute("sandbox", "allow-scripts");
  iframe.srcdoc = msg.html;
  card.appendChild(iframe);
  card.querySelector(".app-toggle").addEventListener("click", (e) => {
    card.classList.toggle("collapsed");
    e.target.textContent = card.classList.contains("collapsed") ? "▸" : "▾";
  });
  card.querySelector(".app-max").addEventListener("click", () => {
    card.classList.toggle("maxed");
  });
  chatEl.appendChild(card);
  appCards.set(msg.id, { iframe, server: msg.server, data: msg.data, inited: false });
  scrollChat();
}

window.addEventListener("message", (e) => {
  const m = e.data;
  if (!m || !m.mcpApp) return;
  let appId = null, entry = null;
  for (const [id, en] of appCards) {
    if (en.iframe.contentWindow === e.source) { appId = id; entry = en; break; }
  }
  if (!entry) return;
  switch (m.type) {
    case "ready":
      entry.iframe.contentWindow.postMessage(
        { mcpApp: true, type: "init", data: entry.data, theme: currentTheme() }, "*");
      entry.inited = true;
      break;
    case "tool_call":
      send({
        type: "app_tool_call",
        server: entry.server,
        tool: String(m.tool || ""),
        args: m.args && typeof m.args === "object" ? m.args : {},
        req_id: `${appId}#${m.req_id}`,
      });
      break;
    case "workflow_add":
      addWfChip(String(m.label || "item").slice(0, 60), String(m.text || "").slice(0, 1500));
      send({ type: "app_workflow_add", text: String(m.text || "").slice(0, 1500) });
      break;
    case "insert_input":
      textInput.value = String(m.text || "").slice(0, 1000);
      textInput.focus();
      break;
    case "resize": {
      const h = Math.max(140, Math.min(680, Number(m.height) || 300));
      entry.iframe.style.height = `${h}px`;
      break;
    }
  }
});
function routeAppToolResult(msg) {
  const [appId, rid] = String(msg.req_id || "").split("#");
  const entry = appCards.get(appId);
  if (!entry || !rid) return;
  entry.iframe.contentWindow.postMessage(
    { mcpApp: true, type: "tool_result", req_id: rid, ok: msg.ok, result: msg.result },
    "*");
}
function broadcastToApps(payload) {
  for (const entry of appCards.values()) {
    if (entry.inited) entry.iframe.contentWindow.postMessage(payload, "*");
  }
}

// ---- transcript replay --------------------------------------------------------
function replayTranscript(events) {
  for (const e of events) {
    if (e.kind === "user") addUserMessage(e.text);
    else if (e.kind === "assistant") addAssistantFull(e.agent, e.text, e.interrupted);
    else if (e.kind === "tool") {
      const card = buildToolCard(e.tool || e.name, e.server, e.arguments);
      settleToolCard(card, e.ok, e.result || "", e.flags);
      chatEl.appendChild(card);
    } else if (e.kind === "subagent") addSubagentFull(e);
    else if (e.kind === "approval") addApprovalFull(e);
    else if (e.kind === "app") addAppCard(e);
  }
  scrollChat();
}

// ---------------------------------------------------------------------------
// Workflow panel
// ---------------------------------------------------------------------------
const STAGE_ORDER = ["planning", "plan_review", "executing", "complete"];

function renderWorkflow(wf) {
  workflowState = { ...workflowState, ...wf };
  const stage = workflowState.stage || "idle";
  wfStageBadge.textContent = stage.replace("_", " ");
  wfStageBadge.className = `badge stage-${stage}`;
  const reached = STAGE_ORDER.indexOf(stage);
  wfStages.querySelectorAll("span").forEach((el) => {
    const idx = STAGE_ORDER.indexOf(el.dataset.stage);
    el.classList.toggle("on", idx === reached);
    el.classList.toggle("past", reached > idx || stage === "complete");
  });
  if (workflowState.task) {
    wfTask.textContent = workflowState.task;
    wfTask.classList.remove("hidden");
    wfTask.title = workflowState.task;
  } else {
    wfTask.classList.add("hidden");
  }
  skillSelect.value = workflowState.skill || "";
  planApproval.classList.toggle("hidden", stage !== "plan_review");
  if (wf.todos) renderTodos(wf.todos);
}

function renderTodos(todos) {
  workflowState.todos = todos || [];
  todoList.innerHTML = "";
  if (!todos || !todos.length) {
    todoList.innerHTML =
      `<div class="side-empty">No plan yet — just say what you need.</div>`;
    return;
  }
  todos.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = `todo-item ${t.status}`;
    const icon = t.status === "done" ? "✓" : t.status === "in_progress" ? "▶" : "○";
    el.innerHTML = `<span class="todo-icon"></span>
      <span class="todo-text"></span><span class="todo-badges"></span>`;
    el.querySelector(".todo-icon").textContent = icon;
    el.querySelector(".todo-text").textContent = `${i + 1}. ${t.text}`;
    const badges = el.querySelector(".todo-badges");
    const addBadge = (cls, txt, title) => {
      const b = document.createElement("span");
      b.className = `tb ${cls}`;
      b.textContent = txt;
      b.title = title;
      badges.appendChild(b);
    };
    if (t.wrote) addBadge("wrote", "✎", "This step modified state");
    if (t.review) addBadge(t.review === "pass" ? "ok" : "bad",
      `R${t.review === "pass" ? "✓" : "✗"}`, `Reviewer: ${t.review}`);
    if (t.verify) addBadge(t.verify === "pass" ? "ok" : "bad",
      `V${t.verify === "pass" ? "✓" : "✗"}`, `Verifier: ${t.verify}`);
    todoList.appendChild(el);
  });
}

$("plan-approve").addEventListener("click", () => {
  send({ type: "plan_decision", approved: true, note: planNote.value.trim() });
  planNote.value = "";
});
$("plan-reject").addEventListener("click", () => {
  send({ type: "plan_decision", approved: false, note: planNote.value.trim() });
  planNote.value = "";
});

function renderSkillOptions() {
  const current = skillSelect.value;
  skillSelect.innerHTML = `<option value="">auto / none</option>`;
  for (const s of skills) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.title || s.name;
    skillSelect.appendChild(opt);
  }
  skillSelect.value = workflowState.skill || current || "";
}
skillSelect.addEventListener("change", () =>
  send({ type: "set_skill", name: skillSelect.value }));

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------
const ACTIVITY_MAX = 250;
function addActivity(entry) {
  if (!entry) return;
  const el = document.createElement("div");
  el.className = `act-line act-${entry.kind}`;
  const detail = Object.entries(entry)
    .filter(([k]) => !["ts", "t", "kind"].includes(k))
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ")
    .slice(0, 220);
  el.innerHTML = `<span class="act-ts"></span><span class="act-kind"></span><span class="act-detail"></span>`;
  el.querySelector(".act-ts").textContent = entry.ts || "";
  el.querySelector(".act-kind").textContent = entry.kind;
  el.querySelector(".act-detail").textContent = detail;
  const atBottom =
    activityLog.scrollHeight - activityLog.scrollTop - activityLog.clientHeight < 40;
  activityLog.appendChild(el);
  while (activityLog.children.length > ACTIVITY_MAX) {
    activityLog.removeChild(activityLog.firstChild);
  }
  if (atBottom) activityLog.scrollTop = activityLog.scrollHeight;
}
$("activity-clear").addEventListener("click", () => { activityLog.innerHTML = ""; });

// ---------------------------------------------------------------------------
// Workflow context chips (data pushed from MCP apps)
// ---------------------------------------------------------------------------
function addWfChip(label, text) {
  wfChips.push({ label, text });
  renderWfChips();
}
function renderWfChips() {
  wfChipsEl.innerHTML = "";
  wfContextBar.classList.toggle("hidden", !wfChips.length);
  wfChips.forEach((c, i) => {
    const chip = document.createElement("span");
    chip.className = "wf-chip";
    chip.title = c.text;
    chip.innerHTML = `<span class="wfc-txt"></span><button class="wfc-x">✕</button>`;
    chip.querySelector(".wfc-txt").textContent = c.label;
    chip.querySelector(".wfc-x").addEventListener("click", () => {
      wfChips.splice(i, 1);
      renderWfChips();
    });
    wfChipsEl.appendChild(chip);
  });
}

// ---------------------------------------------------------------------------
// Apps panel
// ---------------------------------------------------------------------------
async function refreshApps() {
  try {
    const res = await fetch("/api/apps");
    const data = await res.json();
    renderApps(data.apps || []);
  } catch { /* server restarting */ }
}
function renderApps(apps) {
  appsSection.classList.toggle("hidden", !apps.length);
  appsList.innerHTML = "";
  for (const a of apps) {
    const el = document.createElement("div");
    el.className = "app-entry";
    el.innerHTML = `<span class="app-icon">${icon("grid", 16)}</span><div class="app-txt">
        <div class="app-name"></div><div class="app-desc"></div></div>
        <button class="ghost tiny">Open</button>`;
    el.querySelector(".app-name").textContent =
      a.tool.replace(/^open_/, "").replace(/_/g, " ");
    el.querySelector(".app-desc").textContent = `${a.server}`;
    el.title = a.description || "";
    el.querySelector("button").addEventListener("click", () =>
      send({ type: "open_app", server: a.server, tool: a.tool }));
    appsList.appendChild(el);
  }
}

// ---------------------------------------------------------------------------
// Prompts panel (reusable prompt templates published by MCP servers)
// ---------------------------------------------------------------------------
let promptsKey = "";
async function refreshPrompts() {
  try {
    const res = await fetch("/api/prompts");
    const data = await res.json();
    renderPrompts(data.prompts || []);
  } catch { /* server restarting */ }
}
function renderPrompts(prompts) {
  const key = JSON.stringify(prompts);
  if (key === promptsKey) return; // don't redraw under an open argument form
  promptsKey = key;
  promptsSection.classList.toggle("hidden", !prompts.length);
  promptsList.innerHTML = "";
  for (const p of prompts) {
    const el = document.createElement("div");
    el.className = "app-entry prompt-entry";
    el.innerHTML = `<span class="app-icon">${icon("message", 16)}</span><div class="app-txt">
        <div class="app-name"></div><div class="app-desc"></div></div>
        <button class="ghost tiny">Use</button>`;
    el.querySelector(".app-name").textContent = p.name.replace(/_/g, " ");
    el.querySelector(".app-desc").textContent = p.server;
    el.title = p.description || "";
    el.querySelector("button").addEventListener("click", () => openPromptForm(el, p));
    promptsList.appendChild(el);
  }
}
function openPromptForm(entry, p) {
  const old = promptsList.querySelector(".prompt-form");
  const wasOwn = old && old.previousSibling === entry;
  if (old) old.remove();
  if (wasOwn) return; // second click on the same entry toggles the form off
  const form = document.createElement("form");
  form.className = "prompt-form";
  if (p.description) {
    const desc = document.createElement("div");
    desc.className = "pf-desc";
    desc.textContent = p.description;
    form.appendChild(desc);
  }
  for (const a of p.arguments || []) {
    const input = document.createElement("input");
    input.name = a.name;
    input.placeholder = a.name + (a.required ? " *" : "");
    input.title = a.description || "";
    input.required = !!a.required;
    input.autocomplete = "off";
    form.appendChild(input);
  }
  const btns = document.createElement("div");
  btns.className = "pf-btns";
  btns.innerHTML = `<button type="submit" class="primary tiny">Send to agent</button>
      <button type="button" class="ghost tiny">Cancel</button>`;
  form.appendChild(btns);
  const err = document.createElement("div");
  err.className = "form-error";
  form.appendChild(err);
  btns.querySelector("[type=button]").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const args = {};
    for (const input of form.querySelectorAll("input")) {
      if (input.value.trim()) args[input.name] = input.value.trim();
    }
    const submit = btns.querySelector("[type=submit]");
    submit.disabled = true;
    try {
      const res = await fetch("/api/prompts/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server: p.server, name: p.name, args }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        err.textContent = detail.detail || `Failed (${res.status})`;
        return;
      }
      const data = await res.json();
      stopPlayback();
      send({ type: "text", text: data.text }); // ordinary user input: triage,
      form.remove();                           // approvals, audit all apply
    } catch (ex) {
      err.textContent = ex.message;
    } finally {
      submit.disabled = false;
    }
  });
  entry.after(form);
  form.querySelector("input")?.focus();
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
// Settings overlay
// ---------------------------------------------------------------------------
const settingsOverlay = $("settings-overlay");
const settingsBtn = $("settings-btn");

function openSettings(tab) {
  settingsOverlay.classList.remove("hidden");
  if (tab) switchTab(tab);
  refreshServers();
  refreshLogList();
  updateAudioDiag();
}
function closeSettings() { settingsOverlay.classList.add("hidden"); }
settingsBtn.addEventListener("click", () => openSettings());
$("settings-close").addEventListener("click", closeSettings);
settingsOverlay.addEventListener("mousedown", (e) => {
  if (e.target === settingsOverlay) closeSettings();
});
modelChip.addEventListener("click", () => openSettings("general"));

function switchTab(name) {
  document.querySelectorAll(".sw-tab").forEach((t) =>
    t.classList.toggle("on", t.dataset.tab === name));
  document.querySelectorAll(".sw-panel").forEach((p) =>
    p.classList.toggle("hidden", p.dataset.panel !== name));
}
document.querySelectorAll(".sw-tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

function updateModelChip() {
  modelChip.textContent = `${cfg.model || "…"} · ${cfg.voice || ""}` +
    (cfg.speech_enabled ? "" : " · ⚠ speech not configured");
}
function syncSettingsForm() {
  $("set-model").value = cfg.model || "";
  $("set-voice").value = cfg.voice || "";
  $("set-endpoint").textContent = cfg.llm_base_url ? `endpoint: ${cfg.llm_base_url}` : "";
  $("set-approval").value = cfg.approval_mode || "high";
  $("set-privacy").checked = !!cfg.privacy_enabled;
  $("set-injection").checked = !!cfg.injection_guard_enabled;
  $("set-audit").checked = !!cfg.audit_enabled;
  $("set-bargein").value = audioPrefs.mode;
  $("set-sensitivity").value = audioPrefs.sensitivity;
  $("sens-val").textContent = `×${Number(audioPrefs.sensitivity).toFixed(2)}`;
}
async function putSettings(changes) {
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    if (res.ok) {
      const data = await res.json();
      cfg = { ...cfg, ...data.config };
      updateModelChip();
      const keys = Object.keys(data.applied || {});
      if (keys.length) addNotice(`Settings updated: ${keys.join(", ")}.`);
    } else {
      addError(`Settings update failed (${res.status}).`);
    }
  } catch (err) {
    addError(`Settings update failed: ${err.message}`);
  }
}
$("set-model-apply").addEventListener("click", () => {
  const v = $("set-model").value.trim();
  if (v) putSettings({ llm_model: v });
});
$("set-voice-apply").addEventListener("click", () => {
  const v = $("set-voice").value.trim();
  if (v) putSettings({ tts_voice: v });
});
$("set-approval").addEventListener("change", () =>
  putSettings({ approval_mode: $("set-approval").value }));
$("set-privacy").addEventListener("change", () =>
  putSettings({ privacy_enabled: $("set-privacy").checked }));
$("set-injection").addEventListener("change", () =>
  putSettings({ injection_guard_enabled: $("set-injection").checked }));
$("set-audit").addEventListener("change", () =>
  putSettings({ audit_enabled: $("set-audit").checked }));

$("set-bargein").addEventListener("change", () => {
  audioPrefs.mode = $("set-bargein").value;
  saveAudioPrefs();
});
$("set-sensitivity").addEventListener("input", () => {
  audioPrefs.sensitivity = Number($("set-sensitivity").value);
  $("sens-val").textContent = `×${audioPrefs.sensitivity.toFixed(2)}`;
  saveAudioPrefs();
});
$("recal-btn").addEventListener("click", () => {
  calibrating = 25;
  calibSamples = [];
  addNotice("Recalibrating noise floor — keep quiet for a second.");
});
function updateAudioDiag() {
  const diag = $("audio-diag");
  if (!micLive) { diag.textContent = "mic off"; return; }
  diag.textContent =
    `noise floor ${noiseFloor.toFixed(4)} · echo coupling ×${echoGain.toFixed(2)}` +
    ` · mode ${audioPrefs.mode}`;
}
setInterval(() => {
  if (!settingsOverlay.classList.contains("hidden")) updateAudioDiag();
}, 1000);

// logs tab
async function refreshLogList() {
  try {
    const res = await fetch("/api/logs");
    const files = await res.json();
    const list = $("log-list");
    list.innerHTML = files.length ? "" :
      `<div class="side-empty">No log files yet.</div>`;
    for (const f of files.slice(0, 15)) {
      const el = document.createElement("div");
      el.className = "log-item";
      el.innerHTML = `<span class="log-name"></span><span class="log-meta"></span>`;
      el.querySelector(".log-name").textContent = f.name;
      el.querySelector(".log-meta").textContent =
        `${f.modified} · ${(f.size / 1024).toFixed(1)} kB`;
      el.addEventListener("click", async () => {
        const entries = await (await fetch(`/api/logs/${encodeURIComponent(f.name)}`)).json();
        const view = $("log-view");
        view.textContent = entries.map((e) => {
          const rest = Object.entries(e)
            .filter(([k]) => !["ts", "t", "kind"].includes(k))
            .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
            .join(" ");
          return `${e.ts}  ${e.kind}  ${rest}`;
        }).join("\n") || "(empty)";
        view.classList.remove("hidden");
      });
      list.appendChild(el);
    }
  } catch { /* server restarting */ }
}

// skills tab
function renderSkillsTab() {
  const list = $("skills-list");
  list.innerHTML = skills.length ? "" :
    `<div class="side-empty">No skills found in skills/.</div>`;
  for (const s of skills) {
    const el = document.createElement("div");
    el.className = "skill-item";
    el.innerHTML = `<div class="skill-top"><b class="sk-title"></b>
        <span class="sk-risk badge"></span></div>
      <div class="sk-desc"></div><div class="sk-meta"></div>`;
    el.querySelector(".sk-title").textContent = s.title || s.name;
    el.querySelector(".sk-risk").textContent = s.risk;
    el.querySelector(".sk-risk").classList.add(`risk-${s.risk}`);
    el.querySelector(".sk-desc").textContent = s.description;
    el.querySelector(".sk-meta").textContent =
      [s.servers.length ? `servers: ${s.servers.join(", ")}` : "",
       s.categories.length ? `categories: ${s.categories.join(", ")}` : ""]
        .filter(Boolean).join(" · ");
    list.appendChild(el);
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!settingsOverlay.classList.contains("hidden")) { closeSettings(); return; }
  if (serverState === "thinking" || serverState === "speaking" ||
      serverState === "transcribing") {
    stopPlayback();
    send({ type: "interrupt" });
    interruptCooldownUntil = performance.now() + INTERRUPT_COOLDOWN_MS;
  }
});

// ---------------------------------------------------------------------------
// TTS playback — AEC-friendly path + playback envelope tracking
// ---------------------------------------------------------------------------
function ensurePlayback() {
  if (playCtx) return;
  playCtx = new AudioContext();
  // Route through a MediaStream + <audio> element: media-element output is in
  // the browser's echo-cancellation reference, raw WebAudio output is not.
  playDest = playCtx.createMediaStreamDestination();
  ttsAudioEl.srcObject = playDest.stream;
  ttsAudioEl.play().catch(() => { /* resumes on first user gesture */ });
}

function playChunk(arrayBuffer) {
  if (arrayBuffer.byteLength < 2 || voiceMuted) return;
  ensurePlayback();
  if (playCtx.state === "suspended") playCtx.resume();

  const int16 = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(int16.length);
  let sum = 0;
  for (let i = 0; i < int16.length; i++) {
    const s = int16[i] / 32768;
    f32[i] = s;
    sum += s * s;
  }
  const rms = Math.sqrt(sum / int16.length);

  const buf = playCtx.createBuffer(1, f32.length, ttsSampleRate);
  buf.getChannelData(0).set(f32);
  const src = playCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playDest);
  const t = Math.max(playCtx.currentTime + 0.06, nextPlayTime);
  src.start(t);
  nextPlayTime = t + buf.duration;

  // Wall-clock envelope segment for the echo gate.
  const wallStart = performance.now() + (t - playCtx.currentTime) * 1000;
  playbackSegs.push({ start: wallStart, end: wallStart + buf.duration * 1000, rms });
  while (playbackSegs.length && playbackSegs[0].end < performance.now() - 1500) {
    playbackSegs.shift();
  }

  activeSources.add(src);
  src.onended = () => activeSources.delete(src);
}

function playbackEnvAt(wall) {
  // Max playback RMS in a window generous enough to cover output latency
  // and room reverb (~120 ms early, ~350 ms tail).
  let env = 0;
  for (const s of playbackSegs) {
    if (wall >= s.start - 120 && wall <= s.end + 350) env = Math.max(env, s.rms);
  }
  return env;
}

function stopPlayback() {
  for (const src of activeSources) { try { src.stop(); } catch { /* done */ } }
  activeSources.clear();
  nextPlayTime = 0;
  playbackSegs.length = 0;
}

// ---------------------------------------------------------------------------
// Microphone capture + adaptive VAD + echo-aware barge-in
// ---------------------------------------------------------------------------
async function startMic() {
  const constraints = {
    echoCancellation: true,    // AEC reference now includes our TTS playback
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
  };
  // Chrome's voice-isolation, where available, further suppresses non-voice.
  if (navigator.mediaDevices.getSupportedConstraints().voiceIsolation) {
    constraints.voiceIsolation = true;
  }
  micStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
  micCtx = new AudioContext();
  await micCtx.audioWorklet.addModule("/static/pcm-worklet.js");
  const source = micCtx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(micCtx, "pcm-recorder");
  source.connect(node);
  node.port.onmessage = (ev) => onMicChunk(ev.data);
  micLive = true;
  micBtn.classList.add("live");
  calibrating = 25;              // ~0.8 s ambient calibration
  calibSamples = [];
  voicedRun = 0;
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
  const wall = performance.now();

  // Initial ambient calibration: collect, then set the noise floor.
  if (calibrating > 0) {
    calibSamples.push(rms);
    if (--calibrating === 0) {
      const sorted = [...calibSamples].sort((a, b) => a - b);
      noiseFloor = Math.min(0.03, Math.max(0.002,
        sorted[Math.floor(sorted.length / 2)] * 1.1));
    }
    return; // never capture while calibrating
  }

  const env = playbackEnvAt(wall);
  const playbackActive = env > 0.0005;
  const busy = assistantBusy();

  // Slow ambient tracking while idle and quiet.
  if (!busy && !capturing && rms < noiseFloor * 2) {
    noiseFloor = Math.min(0.03, Math.max(0.002, noiseFloor * 0.99 + rms * 0.01));
  }
  // Adapt the speaker->mic coupling estimate while TTS plays and the user is
  // not speaking (voicedRun === 0 keeps a real barge-in from polluting it).
  if (playbackActive && !capturing && voicedRun === 0 && env > 0.002) {
    echoGain = Math.min(4, echoGain * 0.9 + (rms / env) * 0.1);
  }

  const base = Math.max(VAD_BASE, noiseFloor * 3) / audioPrefs.sensitivity;
  let voiced;
  if (busy && playbackActive) {
    // Echo gate: mic must clearly exceed both the strict threshold and the
    // expected echo level for the audio playing right now.
    voiced = rms > Math.max(base * 1.8, echoGain * env * ECHO_MARGIN);
  } else if (busy) {
    voiced = rms > base * 1.4; // thinking/transcribing: no echo, mildly strict
  } else {
    voiced = rms > base;
  }
  if (wall < interruptCooldownUntil) voiced = false;

  if (!capturing) {
    // Manual mode: never auto-interrupt — ignore speech while busy.
    if (busy && audioPrefs.mode === "manual") { voicedRun = 0; return; }
    preroll.push(int16);
    if (preroll.length > PREROLL_CHUNKS) preroll.shift();
    voicedRun = voiced ? voicedRun + 1 : 0;
    const needed = busy && playbackActive ? BARGE_ATTACK_CHUNKS
      : busy ? Math.max(4, VAD_ATTACK_CHUNKS) : VAD_ATTACK_CHUNKS;
    if (voicedRun >= needed) {
      if (busy) {  // barge-in: sustained real speech over the assistant
        stopPlayback();
        send({ type: "interrupt" });
        interruptCooldownUntil = 0;
      }
      capturing = true;
      silenceMs = 0;
      micBtn.classList.add("capturing");
      send({ type: "speech_start" });
      for (const c of preroll) ws.send(c.buffer);
      preroll = [];
    }
  } else {
    ws.send(int16.buffer);
    if (rms > base) {
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
stopBtn.addEventListener("click", () => {
  stopPlayback();
  send({ type: "interrupt" });
  interruptCooldownUntil = performance.now() + INTERRUPT_COOLDOWN_MS;
});

// ---------------------------------------------------------------------------
// Typed input
// ---------------------------------------------------------------------------
function sendTyped() {
  let text = textInput.value.trim();
  if (!text) return;
  if (wfChips.length) {
    text += "\n\nContext from Canvas apps:\n" +
      wfChips.map((c) => `- ${c.text}`).join("\n");
    wfChips = [];
    renderWfChips();
  }
  stopPlayback();
  send({ type: "text", text });
  textInput.value = "";
  hideMentionPop();
}
sendBtn.addEventListener("click", sendTyped);
resetBtn.addEventListener("click", () => { stopPlayback(); send({ type: "reset" }); });

// ---------------------------------------------------------------------------
// MCP panel (inside Settings)
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
      refreshApps();
      refreshPrompts();
    });
    el.querySelector(".reconnect").addEventListener("click", async () => {
      el.querySelector(".reconnect").textContent = "…";
      await fetch(`/api/mcp/servers/${encodeURIComponent(s.name)}/reconnect`, { method: "POST" });
      refreshServers();
      refreshAgents();
      refreshApps();
      refreshPrompts();
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
    refreshApps();
    refreshPrompts();
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
connect();
refreshServers();
refreshAgents();
refreshApps();
refreshPrompts();
setInterval(() => {
  refreshServers(); refreshAgents(); refreshApps(); refreshPrompts();
}, 10000);
