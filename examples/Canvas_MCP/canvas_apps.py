"""Interactive MCP-app HTML for the Canvas server (course explorer + charts).

Both documents are fully self-contained (no external requests — they render
inside a sandboxed iframe in the voice client) and speak a small postMessage
protocol with the host:

  app -> host : {mcpApp, type:"ready"}                      request init
                {mcpApp, type:"tool_call", req_id, tool, args}   read-only bridge
                {mcpApp, type:"workflow_add", text, label}  push item into workflow
                {mcpApp, type:"insert_input", text}         prefill the chat input
                {mcpApp, type:"resize", height}
  host -> app : {mcpApp, type:"init", data, theme}
                {mcpApp, type:"tool_result", req_id, ok, result}
                {mcpApp, type:"theme", theme}

The host only bridges tools classified read-only, so nothing an app does can
modify Canvas. Charts follow a validated palette (single-hue bars, blue/yellow/
neutral parts-of-whole) with tooltips and a table view for accessibility.
"""

# Shared bridge + theme plumbing injected into both apps.
_BRIDGE_JS = """
const pending = new Map(); let seq = 0;
function callTool(tool, args) {
  return new Promise((resolve, reject) => {
    const id = 'r' + (++seq);
    pending.set(id, { resolve, reject });
    parent.postMessage({ mcpApp: true, type: 'tool_call', req_id: id, tool, args }, '*');
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('tool call timed out')); } }, 90000);
  });
}
function toWorkflow(text, label) { parent.postMessage({ mcpApp: true, type: 'workflow_add', text, label }, '*'); }
function askChat(text) { parent.postMessage({ mcpApp: true, type: 'insert_input', text }, '*'); }
function fit() { requestAnimationFrame(() => parent.postMessage({ mcpApp: true, type: 'resize', height: document.documentElement.scrollHeight + 2 }, '*')); }
function setTheme(t) { document.body.dataset.theme = t === 'light' ? 'light' : 'dark'; }
window.addEventListener('message', (e) => {
  const m = e.data;
  if (!m || !m.mcpApp) return;
  if (m.type === 'init') { setTheme(m.theme); boot(m.data || {}); }
  else if (m.type === 'tool_result') {
    const p = pending.get(m.req_id);
    if (p) { pending.delete(m.req_id); m.ok ? p.resolve(m.result) : p.reject(new Error(String(m.result).slice(0, 400))); }
  } else if (m.type === 'theme') { setTheme(m.theme); }
});
function jparse(text) { try { return JSON.parse(text); } catch { return null; } }
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' +
    d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}
parent.postMessage({ mcpApp: true, type: 'ready' }, '*');
"""

# Chart tokens per the validated reference palette (light / dark selected steps).
_CHART_CSS = """
body { margin: 0; font: 15.6px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
body, .root {
  --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,.1);
  --s1: #2a78d6; --s2: #eda100; --s3: #b9b7ae; --hover: rgba(11,11,11,.05);
}
body[data-theme="dark"], body[data-theme="dark"] .root {
  --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,.1);
  --s1: #3987e5; --s2: #c98500; --s3: #55544e; --hover: rgba(255,255,255,.07);
}
body { background: var(--surface); color: var(--ink); }
.root { padding: 14px 16px; }
h1 { font-size: 16.8px; font-weight: 600; margin: 0 0 2px; }
.sub { color: var(--ink-2); font-size: 14.4px; margin-bottom: 10px; }
.bar-actions { display: flex; gap: 8px; margin-top: 10px; }
button {
  font: 14.4px system-ui, sans-serif; color: var(--ink-2); background: transparent;
  border: 1px solid var(--border); border-radius: 7px; padding: 4px 10px; cursor: pointer;
}
button:hover { background: var(--hover); color: var(--ink); }
svg text { font: 13.2px system-ui, sans-serif; fill: var(--muted); }
svg .val { fill: var(--ink-2); font-weight: 600; }
.tip {
  position: fixed; pointer-events: none; z-index: 5; display: none;
  background: var(--surface); color: var(--ink); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 9px; font-size: 14.4px;
  box-shadow: 0 4px 14px rgba(0,0,0,.25);
}
.tip b { display: block; }
table { border-collapse: collapse; width: 100%; margin-top: 10px; display: none; }
table.show { display: table; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--grid);
         font-variant-numeric: tabular-nums; font-size: 14.4px; }
th { color: var(--muted); font-weight: 500; }
.legend { display: flex; gap: 14px; margin: 8px 0 0; flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-2); font-size: 14.4px; }
.legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.hero { font-size: 31.2px; font-weight: 650; fill: var(--ink); }
"""

CHART_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>""" + _CHART_CSS + """</style></head>
<body data-theme="dark">
<div class="root">
  <h1 id="title"></h1>
  <div class="sub" id="subtitle"></div>
  <div id="chart"></div>
  <div class="legend" id="legend"></div>
  <table id="table"></table>
  <div class="bar-actions">
    <button id="tgl-table">Table view</button>
    <button id="to-wf">Send to workflow</button>
  </div>
</div>
<div class="tip" id="tip"></div>
<script>
""" + _BRIDGE_JS + """
const NS = 'http://www.w3.org/2000/svg';
let DATA = null;
const tip = document.getElementById('tip');

function el(name, attrs, parentNode) {
  const node = document.createElementNS(NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (parentNode) parentNode.appendChild(node);
  return node;
}
function showTip(evt, html) {
  tip.innerHTML = html; tip.style.display = 'block';
  const x = Math.min(evt.clientX + 12, innerWidth - tip.offsetWidth - 8);
  tip.style.left = x + 'px'; tip.style.top = (evt.clientY + 12) + 'px';
}
function hideTip() { tip.style.display = 'none'; }
function css(v) { return getComputedStyle(document.body).getPropertyValue(v).trim(); }

// Rounded-top bar anchored to the baseline (4px data-end radius).
function barPath(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
}

function drawBars(host, labels, values, opts) {
  const W = Math.max(host.clientWidth || 520, 320), H = 220;
  const m = { t: 14, r: 12, b: opts.rotate ? 58 : 30, l: 34 };
  const svg = el('svg', { width: '100%', viewBox: `0 0 ${W} ${H}` , role: 'img'}, host);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(1, ...values);
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = max * i / ticks, y = m.t + ih - ih * i / ticks;
    el('line', { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: css(i ? '--grid' : '--axis'), 'stroke-width': 1 }, svg);
    const t = el('text', { x: m.l - 6, y: y + 3, 'text-anchor': 'end' }, svg);
    t.textContent = opts.fmt ? opts.fmt(v) : Math.round(v);
  }
  const n = values.length, gap = 2, bw = Math.max(4, Math.min(48, iw / n - gap));
  const maxIdx = values.indexOf(Math.max(...values));
  values.forEach((v, i) => {
    const x = m.l + (iw / n) * i + (iw / n - bw) / 2;
    const h = max ? (v / max) * ih : 0, y = m.t + ih - h;
    const p = el('path', { d: barPath(x, y, bw, Math.max(h, v > 0 ? 2 : 0), 4), fill: css('--s1') }, svg);
    p.addEventListener('mousemove', (e) => showTip(e, `<b>${esc(labels[i])}</b>${opts.tipFmt ? opts.tipFmt(v) : v}`));
    p.addEventListener('mouseleave', hideTip);
    if (i === maxIdx && v > 0) {  // selective direct label on the max
      const t = el('text', { x: x + bw / 2, y: y - 5, 'text-anchor': 'middle', class: 'val' }, svg);
      t.textContent = opts.fmt ? opts.fmt(v) : v;
    }
    const t = el('text', {
      x: x + bw / 2, y: m.t + ih + 14, 'text-anchor': opts.rotate ? 'end' : 'middle',
      transform: opts.rotate ? `rotate(-32 ${x + bw / 2} ${m.t + ih + 14})` : '',
    }, svg);
    t.textContent = labels[i].length > 14 ? labels[i].slice(0, 13) + '…' : labels[i];
  });
}

function drawDonut(host, parts) {
  const W = Math.max(host.clientWidth || 520, 320), H = 210;
  const svg = el('svg', { width: '100%', viewBox: `0 0 ${W} ${H}`, role: 'img' }, host);
  const cx = W / 2, cy = H / 2, R = 76, SW = 26;
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  let a0 = -Math.PI / 2;
  const padA = 2 / R; // ≈2px surface gap between segments
  parts.forEach((p) => {
    const frac = p.value / total, a1 = a0 + frac * Math.PI * 2;
    if (p.value > 0) {
      const s = a0 + (frac * Math.PI * 2 > padA * 2 ? padA / 2 : 0);
      const e = a1 - (frac * Math.PI * 2 > padA * 2 ? padA / 2 : 0);
      const large = e - s > Math.PI ? 1 : 0;
      const seg = el('path', {
        d: `M${cx + R * Math.cos(s)},${cy + R * Math.sin(s)} A${R},${R} 0 ${large} 1 ${cx + R * Math.cos(e)},${cy + R * Math.sin(e)}`,
        stroke: css(p.color), 'stroke-width': SW, fill: 'none',
      }, svg);
      seg.addEventListener('mousemove', (ev) => showTip(ev, `<b>${esc(p.label)}</b>${p.value} (${Math.round(frac * 100)}%)`));
      seg.addEventListener('mouseleave', hideTip);
    }
    a0 = a1;
  });
  const hero = el('text', { x: cx, y: cy + 2, 'text-anchor': 'middle', class: 'hero' }, svg);
  hero.textContent = Math.round((parts[0].value / total) * 100) + '%';
  const cap = el('text', { x: cx, y: cy + 22, 'text-anchor': 'middle' }, svg);
  cap.textContent = parts[0].label;
}

function legend(parts) {
  const box = document.getElementById('legend');
  box.innerHTML = '';
  for (const p of parts) {
    const s = document.createElement('span');
    s.innerHTML = `<i style="background:${css(p.color)}"></i>${esc(p.label)} — ${p.value}`;
    box.appendChild(s);
  }
}

function table(head, rows) {
  const t = document.getElementById('table');
  t.innerHTML = '<tr>' + head.map((h) => `<th>${esc(h)}</th>`).join('') + '</tr>' +
    rows.map((r) => '<tr>' + r.map((c) => `<td>${esc(c)}</td>`).join('') + '</tr>').join('');
}

function boot(data) {
  DATA = data;
  document.getElementById('title').textContent = data.title || 'Chart';
  document.getElementById('subtitle').textContent = data.subtitle || '';
  const host = document.getElementById('chart');
  host.innerHTML = '';
  if (data.kind === 'histogram') {
    const labels = data.bins.map((b) => b.label);
    drawBars(host, labels, data.bins.map((b) => b.count), {
      tipFmt: (v) => `${v} submission${v === 1 ? '' : 's'}`,
    });
    table(['Score range', 'Submissions'], data.bins.map((b) => [b.label, b.count]));
  } else if (data.kind === 'bars') {
    drawBars(host, data.items.map((i) => i.label), data.items.map((i) => i.value), {
      rotate: true, fmt: (v) => Math.round(v) + '%', tipFmt: (v) => v.toFixed(1) + '% average',
    });
    table(['Assignment', 'Average %', 'Graded'], data.items.map((i) => [i.label, i.value.toFixed(1), i.graded]));
  } else if (data.kind === 'donut') {
    const parts = [
      { label: 'Graded', value: data.graded, color: '--s1' },
      { label: 'Needs grading', value: data.pending, color: '--s2' },
      { label: 'Not submitted', value: data.missing, color: '--s3' },
    ];
    drawDonut(host, parts);
    legend(parts);
    table(['State', 'Count'], parts.map((p) => [p.label, p.value]));
  }
  fit();
}

document.getElementById('tgl-table').addEventListener('click', () => {
  document.getElementById('table').classList.toggle('show'); fit();
});
document.getElementById('to-wf').addEventListener('click', () => {
  if (DATA) toWorkflow(DATA.workflow_text || DATA.title, DATA.title);
});
window.addEventListener('resize', () => DATA && boot(DATA));
</script>
</body></html>
"""

_EXPLORER_CSS = _CHART_CSS + """
.top { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.top h1 { flex: 1; }
.crumb { color: var(--ink-2); font-size: 14.4px; cursor: pointer; }
.crumb:hover { color: var(--ink); text-decoration: underline; }
.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--grid); margin-bottom: 8px; flex-wrap: wrap; }
.tab { padding: 6px 12px; font-size: 15px; color: var(--ink-2); cursor: pointer;
       border: none; background: none; border-bottom: 2px solid transparent; border-radius: 0; }
.tab.on { color: var(--ink); border-bottom-color: var(--s1); font-weight: 600; }
.row { display: flex; align-items: center; gap: 8px; padding: 8px 6px;
       border-bottom: 1px solid var(--grid); }
.row:hover { background: var(--hover); }
.row .txt { flex: 1; min-width: 0; }
.row .name { font-size: 15.6px; font-weight: 550; color: var(--ink);
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .meta { font-size: 13.8px; color: var(--muted); }
.row .meta b { color: var(--ink-2); font-weight: 550; }
.chip { display: inline-block; font-size: 12.6px; padding: 1px 7px; border-radius: 999px;
        border: 1px solid var(--border); color: var(--ink-2); margin-left: 6px; }
.chip.pub { color: #0ca30c; border-color: #0ca30c55; }
.chip.draft { color: var(--s2); border-color: var(--s2); opacity: .85; }
.iconbtn { border: 1px solid var(--border); background: none; border-radius: 7px;
           padding: 3px 8px; font-size: 13.8px; color: var(--ink-2); cursor: pointer; white-space: nowrap; }
.iconbtn:hover { background: var(--hover); color: var(--ink); }
.search { width: 100%; box-sizing: border-box; margin: 2px 0 6px; padding: 6px 10px;
          font: 15px system-ui, sans-serif; color: var(--ink);
          background: transparent; border: 1px solid var(--border); border-radius: 8px; }
.empty, .loading { color: var(--muted); font-size: 15px; padding: 14px 4px; }
.err { color: #d03b3b; font-size: 15px; padding: 8px 4px; }
.spin { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--grid);
        border-top-color: var(--s1); border-radius: 50%; animation: sp 0.8s linear infinite;
        vertical-align: -2px; margin-right: 6px; }
@keyframes sp { to { transform: rotate(360deg); } }
.mini { padding: 4px 0 10px 10px; }
"""

EXPLORER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>""" + _EXPLORER_CSS + """</style></head>
<body data-theme="dark">
<div class="root">
  <div class="top">
    <h1 id="head">Canvas explorer</h1>
    <span class="crumb" id="back" style="display:none">← all courses</span>
  </div>
  <div id="body"><div class="loading"><span class="spin"></span>Waiting for data…</div></div>
</div>
<div class="tip" id="tip"></div>
<script>
""" + _BRIDGE_JS + """
const CHART_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
  'style="vertical-align:-1px"><line x1="18" y1="20" x2="18" y2="10"/>' +
  '<line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>';
let COURSES = [], COURSE = null, OVERVIEW = null, TAB = 'modules';
const body = document.getElementById('body');
const head = document.getElementById('head');
const back = document.getElementById('back');
back.addEventListener('click', () => { COURSE = null; OVERVIEW = null; render(); });

function boot(data) { COURSES = data.courses || []; render(); }

function render() {
  if (!COURSE) { head.textContent = 'Canvas explorer'; back.style.display = 'none'; renderCourses(); }
  else { head.textContent = COURSE.name || 'Course ' + COURSE.id; back.style.display = ''; renderCourse(); }
}

function actionBtns(kind, item, course) {
  const wrap = document.createElement('span');
  const wf = document.createElement('button');
  wf.className = 'iconbtn'; wf.textContent = '+ workflow';
  wf.title = 'Add this item to the workflow context for the agents';
  wf.addEventListener('click', (e) => {
    e.stopPropagation();
    toWorkflow(wfText(kind, item, course), (item.name || item.title || kind));
    wf.textContent = '✓ added'; setTimeout(() => wf.textContent = '+ workflow', 1500);
  });
  const ask = document.createElement('button');
  ask.className = 'iconbtn'; ask.textContent = 'ask';
  ask.title = 'Ask the active agent about this item';
  ask.addEventListener('click', (e) => {
    e.stopPropagation();
    askChat(`About ${kind} "${item.name || item.title}" (id ${item.id ?? item.page_id ?? ''}) in course ${course.id}: `);
  });
  wrap.appendChild(wf); wrap.appendChild(ask);
  return wrap;
}

function wfText(kind, item, course) {
  const bits = [`Canvas ${kind} "${item.name || item.title}"`];
  const id = item.id ?? item.page_id ?? item.url;
  if (id != null) bits.push(`id ${id}`);
  bits.push(`course ${course.id} (${course.name})`);
  if (item.due_at) bits.push(`due ${item.due_at}`);
  if (item.points_possible != null) bits.push(`${item.points_possible} pts`);
  if (item.published != null) bits.push(item.published ? 'published' : 'unpublished');
  return bits.join(' — ');
}

function pubChip(published) {
  return published == null ? '' :
    `<span class="chip ${published ? 'pub' : 'draft'}">${published ? 'published' : 'draft'}</span>`;
}

function renderCourses() {
  body.innerHTML = '';
  if (!COURSES.length) { body.innerHTML = '<div class="empty">No active courses.</div>'; fit(); return; }
  for (const c of COURSES) {
    const row = document.createElement('div');
    row.className = 'row'; row.style.cursor = 'pointer';
    row.innerHTML = `<div class="txt"><div class="name">${esc(c.name)}</div>
      <div class="meta">${esc(c.course_code || '')}${c.term ? ' · ' + esc(c.term) : ''}${
        c.total_students != null ? ' · ' + c.total_students + ' students' : ''}</div></div>`;
    const open = document.createElement('button');
    open.className = 'iconbtn'; open.textContent = 'browse →';
    row.appendChild(open);
    row.addEventListener('click', () => openCourse(c));
    body.appendChild(row);
  }
  fit();
}

async function openCourse(c) {
  COURSE = c; OVERVIEW = null; TAB = 'modules'; render();
  body.innerHTML = '<div class="loading"><span class="spin"></span>Loading course…</div>'; fit();
  try {
    const res = await callTool('get_course_overview', { course_id: c.id });
    OVERVIEW = jparse(res) || {};
  } catch (err) {
    body.innerHTML = `<div class="err">Failed to load course: ${esc(err.message)}</div>`; fit(); return;
  }
  renderCourse();
}

const TABS = [
  ['modules', 'Modules'], ['assignments', 'Assignments'], ['quizzes', 'Quizzes'],
  ['pages', 'Pages'], ['announcements', 'Announcements'],
];

function renderCourse() {
  if (!OVERVIEW) return;
  body.innerHTML = '';
  const tabs = document.createElement('div'); tabs.className = 'tabs';
  for (const [key, label] of TABS) {
    const b = document.createElement('button');
    b.className = 'tab' + (TAB === key ? ' on' : '');
    const n = (OVERVIEW[key] || []).length;
    b.textContent = label + (n ? ` (${n})` : '');
    b.addEventListener('click', () => { TAB = key; renderCourse(); });
    tabs.appendChild(b);
  }
  body.appendChild(tabs);
  const items = OVERVIEW[TAB] || [];
  const search = document.createElement('input');
  search.className = 'search'; search.placeholder = 'Filter…';
  body.appendChild(search);
  const list = document.createElement('div');
  body.appendChild(list);
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    list.innerHTML = '';
    const shown = items.filter((i) => !q || JSON.stringify(i).toLowerCase().includes(q));
    if (!shown.length) list.innerHTML = '<div class="empty">Nothing here.</div>';
    for (const item of shown) list.appendChild(renderItem(item));
    fit();
  };
  search.addEventListener('input', draw);
  draw();
}

function renderItem(item) {
  const row = document.createElement('div'); row.className = 'row';
  const txt = document.createElement('div'); txt.className = 'txt';
  let meta = '';
  if (TAB === 'modules') {
    meta = `${(item.items || []).length} items`;
  } else if (TAB === 'assignments') {
    meta = [item.due_at ? 'due <b>' + esc(fmtDate(item.due_at)) + '</b>' : 'no due date',
            item.points_possible != null ? item.points_possible + ' pts' : '',
            item.needs_grading_count ? '<b>' + item.needs_grading_count + ' to grade</b>' : '']
      .filter(Boolean).join(' · ');
  } else if (TAB === 'quizzes') {
    meta = [item.quiz_type, item.question_count != null ? item.question_count + ' questions' : '',
            item.points_possible != null ? item.points_possible + ' pts' : ''].filter(Boolean).join(' · ');
  } else if (TAB === 'pages') {
    meta = item.updated_at ? 'updated ' + esc(fmtDate(item.updated_at)) : '';
  } else if (TAB === 'announcements') {
    meta = item.posted_at ? esc(fmtDate(item.posted_at)) : '';
  }
  txt.innerHTML = `<div class="name">${esc(item.name || item.title)}${pubChip(item.published)}</div>
                   <div class="meta">${meta}</div>`;
  row.appendChild(txt);
  if (TAB === 'assignments') {
    const g = document.createElement('button');
    g.className = 'iconbtn'; g.innerHTML = CHART_SVG + ' grades';
    g.title = 'Show the score distribution for this assignment';
    g.addEventListener('click', (e) => { e.stopPropagation(); toggleGrades(row, item, g); });
    row.appendChild(g);
  }
  row.appendChild(actionBtns(TAB.replace(/s$/, ''), item, COURSE));
  // Module rows expand to their items.
  if (TAB === 'modules' && (item.items || []).length) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => {
      const next = row.nextElementSibling;
      if (next && next.classList.contains('mini')) { next.remove(); fit(); return; }
      const mini = document.createElement('div'); mini.className = 'mini';
      for (const it of item.items) {
        const r = document.createElement('div'); r.className = 'row';
        r.innerHTML = `<div class="txt"><div class="name">${esc(it.title)}${pubChip(it.published)}</div>
                       <div class="meta">${esc(it.type || '')}</div></div>`;
        r.appendChild(actionBtns('module item', { ...it, name: it.title }, COURSE));
        mini.appendChild(r);
      }
      row.after(mini); fit();
    });
  }
  return row;
}

async function toggleGrades(row, item, btn) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains('mini')) { next.remove(); fit(); return; }
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  let stats = null;
  try { stats = jparse(await callTool('get_assignment_scores',
    { course_id: COURSE.id, assignment_id: item.id })); }
  catch (err) { stats = { error: err.message }; }
  btn.disabled = false; btn.innerHTML = CHART_SVG + ' grades';
  const mini = document.createElement('div'); mini.className = 'mini';
  if (!stats || stats.error) {
    mini.innerHTML = `<div class="err">${esc(stats && stats.error || 'No data')}</div>`;
  } else if (!stats.scores.length) {
    mini.innerHTML = `<div class="empty">No graded submissions yet (${stats.pending} awaiting grading, ${stats.missing} not submitted).</div>`;
  } else {
    const chart = document.createElement('div');
    mini.appendChild(chart);
    drawHistogram(chart, stats, item);
    const cap = document.createElement('div'); cap.className = 'meta';
    cap.style.cssText = 'font-size: 13.8px;color:var(--muted);padding:2px 0 4px';
    cap.textContent = `${stats.scores.length} graded · ${stats.pending} pending · ${stats.missing} not submitted · mean ${stats.mean}`;
    mini.appendChild(cap);
    const wf = document.createElement('button'); wf.className = 'iconbtn';
    wf.textContent = '+ send stats to workflow';
    wf.addEventListener('click', () => {
      toWorkflow(`Canvas assignment "${item.name}" (id ${item.id}, course ${COURSE.id}) grade stats: ` +
        `${stats.scores.length} graded, ${stats.pending} pending grading, ${stats.missing} not submitted, ` +
        `mean ${stats.mean}/${stats.points_possible}, min ${stats.min}, max ${stats.max}`, 'grade stats');
      wf.textContent = '✓ added';
    });
    mini.appendChild(wf);
  }
  row.after(mini); fit();
}

// Compact inline histogram (single hue, rounded data ends, tooltips).
function drawHistogram(host, stats, item) {
  const NS = 'http://www.w3.org/2000/svg';
  const max = Math.max(stats.points_possible || Math.max(...stats.scores) || 1, 1);
  const BINS = 8, counts = new Array(BINS).fill(0);
  for (const s of stats.scores) counts[Math.min(BINS - 1, Math.floor((s / max) * BINS))]++;
  const W = 420, H = 120, m = { t: 8, r: 6, b: 20, l: 6 };
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', '100%');
  host.appendChild(svg);
  const iw = W - m.l - m.r, ih = H - m.t - m.b, peak = Math.max(1, ...counts);
  const bw = iw / BINS - 2;
  const base = document.createElementNS(NS, 'line');
  base.setAttribute('x1', m.l); base.setAttribute('x2', W - m.r);
  base.setAttribute('y1', m.t + ih); base.setAttribute('y2', m.t + ih);
  base.setAttribute('stroke', getComputedStyle(document.body).getPropertyValue('--axis'));
  svg.appendChild(base);
  const tip = document.getElementById('tip');
  counts.forEach((c, i) => {
    const x = m.l + (iw / BINS) * i + 1;
    const h = (c / peak) * ih, y = m.t + ih - h, r = Math.min(4, bw / 2, h);
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', c ? `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${y + h} Z` : '');
    p.setAttribute('fill', getComputedStyle(document.body).getPropertyValue('--s1'));
    const lo = Math.round((i / BINS) * max), hi = Math.round(((i + 1) / BINS) * max);
    p.addEventListener('mousemove', (e) => {
      tip.innerHTML = `<b>${lo}–${hi} pts</b>${c} submission${c === 1 ? '' : 's'}`;
      tip.style.display = 'block';
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 130) + 'px';
      tip.style.top = (e.clientY + 12) + 'px';
    });
    p.addEventListener('mouseleave', () => tip.style.display = 'none');
    svg.appendChild(p);
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', x + bw / 2); t.setAttribute('y', H - 6); t.setAttribute('text-anchor', 'middle');
    t.textContent = i === 0 ? '0' : i === BINS - 1 ? String(max) : '';
    svg.appendChild(t);
  });
}
</script>
</body></html>
"""
