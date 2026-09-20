/* Cartwheel HW4 review app. Plain JavaScript, no build step.
   One conversation per screen (every turn of a session), notes in the margin,
   taxonomy and labels in the right panel, a progress view behind the toggle.
   Talks to the JSON API in analysis/review_app/app.py. */
'use strict';

const BUILTIN_QUEUES = new Set(['suggestions', 'unreviewed', 'final', 'all']);
const STATUS_GLYPH = { unreviewed: '○', noted: '◐', labelled: '●' };
const LONG_RESULT = 1500;   // characters; longer results collapse to their first lines
const INLINE_ARGS = 120;    // characters; longer arguments go behind a details toggle

const state = {
  meta: null,
  queues: [],
  queueName: null,
  list: [],            // conversations of the current queue (after any client-side filter)
  cur: -1,             // index of the open conversation in list, or -1
  conv: null,          // the open conversation, with annotations, suggestions, labels
  patterns: { modes: [] },
  annotations: [],     // every annotation on file (for mode notes and counts)
  progress: null,
  manifest: null,
  view: 'review',
  allIndex: {},        // session_id -> list summary, every conversation
  filter: null,        // {dim, value} client-side filter over the current queue
  expanded: new Set(), // result block keys the reviewer expanded
};

let openSeq = 0;       // guards against out-of-order conversation loads

const $ = (id) => document.getElementById(id);

/* ---- small helpers ----------------------------------------------------- */

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function pretty(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
}
function compact(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }
function fmtDur(s) {
  if (s == null || isNaN(s)) return '';
  if (s < 1) return Math.round(s * 1000) + ' ms';
  return (Math.round(s * 10) / 10).toFixed(1) + ' s';
}
function fmtTs(s) {
  if (!s) return '';
  const d = new Date(String(s).replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1'));
  if (isNaN(d)) return String(s);
  return d.toLocaleTimeString([], { hour12: false });
}
function shortId(id) { return id ? String(id).slice(0, 8) : ''; }
function isBatch(name) {
  return !BUILTIN_QUEUES.has(name) && !name.startsWith('mode:') && !name.startsWith('role:');
}

async function api(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.status + ' ' + res.statusText;
    try {
      const j = await res.json();
      if (j && j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { /* keep the status text */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 2600);
}

function showError(msg) {
  $('error-text').textContent = msg;
  $('error-banner').hidden = false;
}

/* ---- data refreshers --------------------------------------------------- */

async function refreshQueues() {
  state.queues = await api('GET', '/api/queues');
  renderQueuePicker();
}
async function refreshPatterns() { state.patterns = await api('GET', '/api/patterns'); }
async function refreshAnnotations() {
  const doc = await api('GET', '/api/annotations');
  state.annotations = doc.annotations || [];
}
async function refreshProgress() { state.progress = await api('GET', '/api/progress'); }
async function refreshManifest() { state.manifest = await api('GET', '/api/samples'); }
async function refreshAllIndex() {
  const all = await api('GET', '/api/conversations?queue=all');
  state.allIndex = {};
  all.forEach((c) => { state.allIndex[c.session_id] = c; });
}

function applyFilter(list) {
  if (!state.filter) return list;
  return list.filter((c) => c[state.filter.dim] === state.filter.value);
}

/* The queue list again, keeping the open conversation's position. */
async function refreshList() {
  if (!state.queueName) return;
  const list = await api('GET', '/api/conversations?queue=' + encodeURIComponent(state.queueName));
  state.list = applyFilter(list);
  const sid = state.conv && state.conv.session_id;
  state.cur = sid ? state.list.findIndex((c) => c.session_id === sid) : -1;
  renderConvPicker();
  renderNav();
}

/* After any write: counts, statuses, queue sizes, panel. */
async function afterChange() {
  await Promise.all([refreshAnnotations(), refreshProgress(), refreshQueues(), refreshList(), refreshPatterns()]);
  renderCounts();
  renderPanel();
}

/* ---- queues and navigation --------------------------------------------- */

async function loadQueue(name, opts) {
  opts = opts || {};
  state.queueName = name;
  state.filter = opts.filter || null;
  const list = await api('GET', '/api/conversations?queue=' + encodeURIComponent(name));
  state.list = applyFilter(list);
  renderQueuePicker();
  renderConvPicker();
  renderFilterChip();
  const sid = state.conv && state.conv.session_id;
  const idx = sid ? state.list.findIndex((c) => c.session_id === sid) : -1;
  if (opts.keepCurrent && idx >= 0) {
    state.cur = idx;
    renderNav();
    return;
  }
  if (state.list.length) {
    await openConversation(state.list[0].session_id);
  } else {
    state.cur = -1;
    renderNav();
    toast('empty queue: ' + name);
  }
  switchView('review');
}

async function openConversation(sid, focusItem) {
  const seq = ++openSeq;
  let conv;
  try {
    conv = await api('GET', '/api/conversation/' + encodeURIComponent(sid));
  } catch (e) {
    toast('could not load ' + sid + ': ' + e.message);
    return;
  }
  if (seq !== openSeq) return;
  state.conv = conv;
  state.cur = state.list.findIndex((c) => c.session_id === sid);
  state.expanded = new Set();
  clearPending();
  cancelEvidenceMode();
  $('answer-key').open = false;
  history.replaceState(null, '', '#' + sid);
  renderIds();
  renderNav();
  renderAnswerKey();
  renderConversation();
  switchView('review');
  if (focusItem) scrollToItem(focusItem);
  else window.scrollTo(0, 0);
}

function stepQueue(delta) {
  const n = state.list.length;
  if (!n) return;
  const i = state.cur + delta;
  if (i < 0 || i >= n) {
    toast(delta > 0 ? 'end of queue' : 'start of queue');
    return;
  }
  openConversation(state.list[i].session_id);
}

function scrollToItem(id) {
  const hl = document.querySelector('.hl[data-item="' + id + '"]');
  if (hl) hl.scrollIntoView({ block: 'center' });
}

/* ---- top bar ----------------------------------------------------------- */

function renderIds() {
  const c = state.conv;
  const el = $('ids');
  if (!c) { el.innerHTML = '<span class="muted">no conversation</span>'; return; }
  el.innerHTML =
    '<span class="scenario">' + esc(c.scenario_id) + '</span>' +
    '<span class="mono muted" title="session ' + esc(c.session_id) + '">' + esc(shortId(c.session_id)) + '</span>' +
    '<span class="chip role role-' + esc(c.role) + '">' + esc(c.role || '?') + '</span>' +
    (c.scenario_group ? '<span class="chip">' + esc(c.scenario_group) + '</span>' : '') +
    (c.source === 'pilot' ? '<span class="chip pilot">pilot</span>' : '') +
    (c.turn_count > 1 ? '<span class="chip">' + c.turn_count + ' turns</span>' : '') +
    '<span class="mono muted" title="prompt version">' + esc(c.prompt_version || '') + '</span>';
}

function renderNav() {
  const n = state.list.length;
  $('pos').textContent = n ? ((state.cur >= 0 ? state.cur + 1 : '-') + ' / ' + n) : '0 / 0';
  $('prev').disabled = state.cur <= 0;
  $('next').disabled = n === 0 || state.cur >= n - 1;
  if (state.conv) $('conv-picker').value = state.conv.session_id;
}

function renderFilterChip() {
  const chip = $('filter-chip');
  if (!state.filter) { chip.hidden = true; return; }
  chip.hidden = false;
  chip.innerHTML = esc(state.filter.dim.replace(/_/g, ' ')) + ': ' + esc(state.filter.value) +
    ' <button type="button" class="link" data-action="clear-filter" title="clear the filter">×</button>';
}

function renderQueuePicker() {
  const sel = $('queue-picker');
  const groups = [
    ['Batches', (q) => isBatch(q.name)],
    ['Work', (q) => q.name === 'suggestions' || q.name === 'unreviewed'],
    ['Modes', (q) => q.name.startsWith('mode:')],
    ['Roles', (q) => q.name.startsWith('role:')],
    ['Everything', (q) => q.name === 'final' || q.name === 'all'],
  ];
  let html = '';
  groups.forEach(([label, pred]) => {
    const qs = state.queues.filter(pred);
    if (!qs.length) return;
    html += '<optgroup label="' + label + '">' +
      qs.map((q) => '<option value="' + esc(q.name) + '">' + esc(q.name) + ' (' + q.count + ')</option>').join('') +
      '</optgroup>';
  });
  sel.innerHTML = html;
  if (state.queueName) sel.value = state.queueName;
}

function renderConvPicker() {
  const sel = $('conv-picker');
  sel.innerHTML = state.list.map((c) => {
    const bits = [STATUS_GLYPH[c.status] || '○', esc(c.scenario_id), '·', esc(c.role)];
    if (c.turn_count > 1) bits.push('·', c.turn_count + ' turns');
    if (c.has_pending_suggestion) bits.push('·', 'suggestion');
    return '<option value="' + esc(c.session_id) + '">' + bits.join(' ') + '</option>';
  }).join('');
  if (state.conv) sel.value = state.conv.session_id;
}

function renderCounts() {
  const p = state.progress;
  const parts = [];
  if (p) parts.push(p.sample_size ? p.reviewed + ' / ' + p.sample_size + ' reviewed' : 'no batches yet');
  parts.push(state.annotations.filter((a) => a.kind !== 'no_failure').length + ' notes');
  parts.push(state.patterns.modes.length + ' modes');
  if (p) parts.push(p.suggestions_pending + ' suggestions pending');
  if (state.meta && !state.meta.scores_to_langfuse) parts.push('<span class="chip warn" title="labels are saved to files only; Langfuse scores are off">scores stay local</span>');
  $('counts').innerHTML = parts.join('<span class="dot">·</span>');
}

function renderBanner() {
  const m = state.meta;
  const b = $('offline-banner');
  if (m.warning || m.trace_source !== 'cache') {
    b.textContent = m.warning || ('Serving ' + m.trace_source + ', not the live Langfuse cache.');
    b.hidden = false;
  } else {
    b.hidden = true;
  }
  document.title = 'Cartwheel review · ' + m.conversation_count + ' conversations';
}

/* ---- answer key -------------------------------------------------------- */

function renderAnswerKey() {
  const k = state.conv && state.conv.answer_key;
  const el = $('answer-key-body');
  if (!k) { el.innerHTML = '<div class="muted">no answer key for this scenario</div>'; return; }
  const t = k.tuple || {};
  const tupleHtml = ['intent', 'record_state', 'applicable_policy', 'tools_needed', 'difficulty', 'user_style']
    .filter((f) => t[f] != null)
    .map((f) => '<span class="chip">' + esc(f.replace(/_/g, ' ')) + ': ' + esc(t[f]) + '</span>')
    .join(' ');
  const rows = [];
  if (k.outcome) rows.push(['Expected outcome', esc(k.outcome)]);
  if (k.criterion) rows.push(['Expected criterion', esc(k.criterion)]);
  if (!k.outcome && !k.criterion) rows.push(['Expected', '-']);
  rows.push(['Evaluation', esc(k.evaluation || '-')]);
  rows.push(['Reason', esc(k.reason || '-')]);
  rows.push(['Policy', esc(k.policy_id || '-')]);
  rows.push(['Source', esc([k.source_type, k.source_reference].filter(Boolean).join(' · ') || '-')]);
  rows.push(['Scenario group', esc(k.scenario_group || '-')]);
  rows.push(['Data-quality case', esc(k.data_quality_case_id || '-')]);
  rows.push(['Tuple', tupleHtml || '-']);
  rows.push(['Runner', esc([k.runner_status, k.runner_duration_s != null ? fmtDur(k.runner_duration_s) : null, k.runner_error].filter(Boolean).join(' · ') || '-')]);
  el.innerHTML = '<table class="kv">' +
    rows.map(([a, b]) => '<tr><th>' + esc(a) + '</th><td>' + b + '</td></tr>').join('') +
    '</table>';
}

/* ---- conversation ------------------------------------------------------ */

function renderConversation() {
  const c = state.conv;
  const el = $('conversation');
  if (!c) { el.innerHTML = ''; return; }
  el.innerHTML = c.turns.map(renderTurn).join('') + renderFooter();
  applyHighlights();
  layoutMargin();
  renderLabels();
}

function renderTurn(t) {
  const flags = (t.flags || []).map((f) => '<span class="flag">' + esc(f) + '</span>').join('');
  const reply = t.reply && t.reply.text
    ? '<div class="text" data-block="r">' + esc(t.reply.text) + '</div>'
    : '<div class="text missing">no reply: the turn ended before a final message</div>';
  return '<article class="turn" data-trace="' + esc(t.trace_id) + '">' +
    '<header class="turn-head">' +
      '<span class="turn-title">Turn ' + t.turn_index + ' of ' + t.turn_count + '</span>' +
      '<span class="sep">·</span><span class="mono">trace ' + esc(t.trace_id) + '</span>' +
      '<span class="sep">·</span><span class="dur">' + fmtDur(t.duration_s) + '</span>' +
      flags +
    '</header>' +
    '<div class="block user">' +
      '<div class="block-label">user <span class="ts">' + esc(fmtTs(t.user && t.user.ts)) + '</span></div>' +
      '<div class="text" data-block="u">' + esc(t.user && t.user.text) + '</div>' +
    '</div>' +
    (t.steps || []).map((s, i) => renderStep(s, i)).join('') +
    '<div class="block reply">' +
      '<div class="block-label">reply · shown to the user <span class="ts">user → reply ' + fmtDur(t.duration_s) + '</span></div>' +
      reply +
    '</div>' +
  '</article>';
}

function renderStep(s, i) {
  const key = s.obs_id || ('step' + i);
  const reasoning = s.reasoning
    ? '<div class="reasoning"><div class="block-label">reasoning · not shown to the user</div>' +
      '<div class="text" data-block="m:' + esc(key) + '">' + esc(s.reasoning) + '</div></div>'
    : '<div class="reasoning"><div class="block-label">no reasoning text</div></div>';
  const calls = (s.tool_calls || []).map((c, j) => renderCall(c, key, j)).join('');
  return '<div class="step">' + reasoning + calls + '</div>';
}

function renderCall(c, stepKey, j) {
  const key = c.obs_id || (stepKey + '-' + j);
  const args = compact(c.arguments);
  const argsHtml = args.length < INLINE_ARGS
    ? '<code class="args inline" data-block="a:' + esc(key) + '">' + esc(args) + '</code>'
    : '<details class="args"><summary>arguments · ' + args.length + ' chars</summary>' +
      '<pre data-block="a:' + esc(key) + '">' + esc(pretty(c.arguments)) + '</pre></details>';
  const failed = c.ok === false;
  const text = pretty(c.result);
  const long = !failed && text.length > LONG_RESULT;
  const collapsed = long && !state.expanded.has('t:' + key);
  let badges = '';
  if (failed) badges += '<span class="badge error">ok: false' + (c.error ? ' · ' + esc(c.error) : '') + '</span>';
  else if (c.permission_denied) badges += '<span class="badge error">permission denied</span>';
  let resultHtml;
  if (c.result == null) {
    resultHtml = '<div class="result missing muted">no result recorded</div>';
  } else {
    const cls = 'result' + (failed ? ' error' : '') + (long ? ' long' : '') + (collapsed ? ' collapsed' : '');
    const button = long
      ? '<button type="button" class="expand" data-action="toggle-result" data-key="t:' + esc(key) + '">' +
        (collapsed ? 'expand · ' + text.length.toLocaleString() + ' chars' : 'collapse') + '</button>'
      : '';
    resultHtml = '<div class="' + cls + '" data-result="t:' + esc(key) + '">' +
      '<pre data-block="t:' + esc(key) + '">' + esc(text) + '</pre>' + button + '</div>';
  }
  return '<div class="call">' +
    '<div class="call-head"><span class="tool-label">tool</span><span class="tool-name">' + esc(c.name) + '</span>' +
      badges + '<span class="dur">' + fmtDur(c.duration_s) + '</span></div>' +
    '<div class="call-args">' + argsHtml + '</div>' +
    resultHtml +
  '</div>';
}

function renderFooter() {
  const anns = (state.conv && state.conv.annotations) || [];
  const nf = anns.find((a) => a.kind === 'no_failure');
  const inner = nf
    ? '<span class="marked">✓ no failure observed</span>' +
      '<button type="button" class="link" data-action="undo-no-failure" data-id="' + esc(nf.id) + '">undo</button>'
    : '<button type="button" class="btn" data-action="no-failure" title="hotkey 0">No failure observed</button>';
  return '<footer class="conv-foot">' + inner + '</footer>';
}

function toggleResult(key) {
  if (state.expanded.has(key)) state.expanded.delete(key);
  else state.expanded.add(key);
  const box = document.querySelector('[data-result="' + key + '"]');
  if (!box) return;
  const open = state.expanded.has(key);
  box.classList.toggle('collapsed', !open);
  const btn = box.querySelector('.expand');
  if (btn) {
    const n = box.querySelector('pre').textContent.length;
    btn.textContent = open ? 'collapse' : 'expand · ' + n.toLocaleString() + ' chars';
  }
  layoutMargin();
}

/* ---- views ------------------------------------------------------------- */

function switchView(name) {
  state.view = name;
  $('review-view').hidden = name !== 'review';
  $('progress-view').hidden = name !== 'progress';
  document.querySelectorAll('#view-toggle button').forEach((b) => b.classList.toggle('on', b.dataset.view === name));
  if (name === 'progress') refreshProgressView();
  else layoutMargin();
}

function closeOverlays() {
  if (!$('popover').hidden) { cancelNote(); return; }
  if (state.evidenceMode || state.picker) { cancelEvidenceMode(); return; }
  if (!$('hotkeys').hidden) { $('hotkeys').hidden = true; return; }
  $('error-banner').hidden = true;
}

/* ---- hotkeys ----------------------------------------------------------- */

document.addEventListener('keydown', (e) => {
  const tag = (e.target && e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key) {
    case 'j': case 'ArrowRight': e.preventDefault(); stepQueue(1); break;
    case 'k': case 'ArrowLeft': e.preventDefault(); stepQueue(-1); break;
    case '?': e.preventDefault(); $('hotkeys').hidden = !$('hotkeys').hidden; break;
    case 'Escape': closeOverlays(); break;
    case '0': e.preventDefault(); markNoFailure(); break;
    default:
      if (/^[1-9]$/.test(e.key)) { e.preventDefault(); toggleModeKey(parseInt(e.key, 10)); }
  }
});

/* ---- static wiring ----------------------------------------------------- */

$('prev').addEventListener('click', () => stepQueue(-1));
$('next').addEventListener('click', () => stepQueue(1));
$('help-toggle').addEventListener('click', () => { $('hotkeys').hidden = !$('hotkeys').hidden; });
$('error-close').addEventListener('click', () => { $('error-banner').hidden = true; });
$('queue-picker').addEventListener('change', (e) => { loadQueue(e.target.value); e.target.blur(); });
$('conv-picker').addEventListener('change', (e) => { openConversation(e.target.value); e.target.blur(); });
$('view-toggle').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-view]');
  if (b) switchView(b.dataset.view);
});
$('filter-chip').addEventListener('click', (e) => {
  if (e.target.closest('[data-action="clear-filter"]')) loadQueue(state.queueName, { keepCurrent: true });
});
$('conversation').addEventListener('click', (e) => {
  const b = e.target.closest('[data-action]');
  if (!b) return;
  if (b.dataset.action === 'toggle-result') toggleResult(b.dataset.key);
  else if (b.dataset.action === 'no-failure') markNoFailure();
  else if (b.dataset.action === 'undo-no-failure') deleteAnnotation(b.dataset.id);
});
$('conversation').addEventListener('toggle', () => layoutMargin(), true);
window.addEventListener('resize', () => layoutMargin());

/* ---- notes: highlights, margin cards, selection popover ---------------- */

let pending = null;        // {span, ctx: {trace_id, block, quote}} while the popover is open
state.evidenceMode = null; // mode name while the next selection becomes label evidence
state.picker = null;       // mode name while the evidence picker is open in the panel

/* Everything that gets a highlight or a margin card for the open conversation. */
function itemsForConversation() {
  const c = state.conv;
  if (!c) return [];
  const anns = (c.annotations || []).map((a) => {
    let kind = 'note';
    if (a.kind === 'no_failure') kind = 'no_failure';
    else if (a.source === 'accepted_suggestion') kind = 'accepted';
    return Object.assign({}, a, { kind });
  });
  const sugg = (c.suggestions || [])
    .filter((s) => s.status === 'pending')
    .map((s) => Object.assign({}, s, { kind: 'sugg' }));
  return anns.concat(sugg);
}

function blockEl(traceId, block) {
  const turn = document.querySelector('#conversation .turn[data-trace="' + traceId + '"]');
  return turn ? turn.querySelector('[data-block="' + block + '"]') : null;
}

/* Rebuild each block's text with <span.hl> around the first occurrence of
   every quote that belongs to it. Blocks hold plain text, so textContent is
   the source of truth and re-applying is idempotent. */
function applyHighlights() {
  const byKey = {};
  itemsForConversation().forEach((it) => {
    if (!it.quote || !it.trace_id) return;
    const k = it.trace_id + '|' + it.block;
    (byKey[k] = byKey[k] || []).push(it);
  });
  document.querySelectorAll('#conversation [data-block]').forEach((el) => {
    const turn = el.closest('.turn');
    const mine = turn ? (byKey[turn.dataset.trace + '|' + el.dataset.block] || []) : [];
    const text = el.textContent;
    if (!mine.length) {
      if (el.firstElementChild) el.textContent = text;
      return;
    }
    const ranges = [];
    mine.forEach((it) => {
      const pos = text.indexOf(it.quote);
      if (pos >= 0) ranges.push([pos, pos + it.quote.length, it]);
    });
    ranges.sort((a, b) => a[0] - b[0]);
    let out = '';
    let cursor = 0;
    ranges.forEach(([s, e, it]) => {
      if (s < cursor) return; // overlapping quotes: the earlier one wins
      out += esc(text.slice(cursor, s));
      out += '<span class="hl' + (it.kind === 'sugg' ? ' sugg' : '') + '" data-item="' + esc(it.id) + '">' +
        esc(text.slice(s, e)) + '</span>';
      cursor = e;
    });
    out += esc(text.slice(cursor));
    el.innerHTML = out;
  });
  // a highlight hidden in a collapsed result or a closed details must be visible
  document.querySelectorAll('#conversation .hl').forEach((hl) => {
    const res = hl.closest('.result.collapsed');
    if (res) {
      state.expanded.add(res.dataset.result);
      res.classList.remove('collapsed');
      const b = res.querySelector('.expand');
      if (b) b.textContent = 'collapse';
    }
    const det = hl.closest('details');
    if (det && !det.open) det.open = true;
  });
}

function noteCardHtml(it, found) {
  let tag;
  if (it.kind === 'sugg') tag = 'agent suggestion · ' + esc(it.mode || '?');
  else if (it.kind === 'accepted') tag = 'suggested · accepted' + (it.mode ? ' · ' + esc(it.mode) : '');
  else if (it.kind === 'no_failure') tag = 'no failure observed';
  else tag = 'you' + (it.mode ? ' · ' + esc(it.mode) : '');
  let html = '<div class="tag">' + tag + '</div>';
  if (it.quote) {
    html += '<div class="quote">“' + esc(clip(it.quote, 90)) + '”' +
      (found ? '' : ' <span class="muted">(quote not found in the text)</span>') + '</div>';
  }
  if (it.kind === 'no_failure') {
    html += '<div class="body muted">' + esc(it.note) + '</div>' +
      '<div class="acts hover"><button type="button" class="link" data-action="delete-note" data-id="' + esc(it.id) + '">undo</button></div>';
  } else if (it.kind === 'sugg') {
    if (it.note) html += '<div class="body">' + esc(it.note) + '</div>';
    if (it.method) html += '<div class="meta muted">method: ' + esc(it.method) + '</div>';
    html += '<div class="acts">' +
      '<button type="button" class="btn small accept" data-action="accept" data-id="' + esc(it.id) + '">Accept</button>' +
      '<button type="button" class="btn small" data-action="reject" data-id="' + esc(it.id) + '">Reject</button></div>';
  } else {
    html += '<div class="body" data-note-body="' + esc(it.id) + '">' + esc(it.note) + '</div>' +
      '<div class="acts hover">' +
      '<button type="button" class="link" data-action="edit-note" data-id="' + esc(it.id) + '">edit</button>' +
      '<button type="button" class="link" data-action="delete-note" data-id="' + esc(it.id) + '">delete</button></div>';
  }
  return html;
}

/* Margin cards sit at their highlight's top, measured as the difference of
   two getBoundingClientRect tops (no scrollTop: that double-counts), and
   stack downwards with an 8px minimum gap. */
function layoutMargin() {
  const col = $('margin');
  if (!col || $('review-view').hidden) return;
  col.innerHTML = '';
  const items = itemsForConversation();
  if (!items.length) return;
  const colRect = col.getBoundingClientRect();
  const placed = items.map((it) => {
    let anchor = null;
    if (it.quote) anchor = document.querySelector('#conversation .hl[data-item="' + it.id + '"]');
    const found = !!anchor;
    if (!anchor && it.kind === 'no_failure') anchor = document.querySelector('#conversation .conv-foot');
    if (!anchor && it.trace_id) anchor = document.querySelector('#conversation .turn[data-trace="' + it.trace_id + '"]');
    const top = anchor ? anchor.getBoundingClientRect().top - colRect.top : 0;
    return { it, top: Math.max(0, top), found };
  }).sort((a, b) => a.top - b.top);
  let lastBottom = 0;
  placed.forEach(({ it, top, found }) => {
    const card = document.createElement('div');
    card.className = 'mnote ' + it.kind;
    card.dataset.item = it.id;
    card.innerHTML = noteCardHtml(it, found);
    const y = Math.max(top, lastBottom);
    card.style.top = y + 'px';
    col.appendChild(card);
    lastBottom = y + card.offsetHeight + 8;
  });
}

function nodeBlock(node) {
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  return el ? el.closest('#conversation [data-block]') : null;
}

/* mouseup inside the conversation: a non-empty selection inside one block
   becomes a pending highlight, wrapped BEFORE the input takes focus so the
   browser's selection clearing does not lose it. */
function onSelection(e) {
  if (e.target.closest('button, summary, select, input, textarea')) return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
  const quote = sel.toString().trim();
  if (!quote) return;
  const range = sel.getRangeAt(0);
  const startBlock = nodeBlock(range.startContainer);
  const endBlock = nodeBlock(range.endContainer);
  if (!startBlock || startBlock !== endBlock) {
    toast('select inside one block (user, reasoning, arguments, result or reply)');
    sel.removeAllRanges();
    return;
  }
  const turn = startBlock.closest('.turn');
  const ctx = { trace_id: turn.dataset.trace, block: startBlock.dataset.block, quote };
  if (state.evidenceMode) {
    sel.removeAllRanges();
    submitEvidence(state.evidenceMode, ctx);
    return;
  }
  clearPending();
  const span = document.createElement('span');
  span.className = 'hl pending';
  try {
    range.surroundContents(span);
  } catch (err) {
    // the range crosses an existing highlight: split it, re-applied on commit or cancel
    try { span.appendChild(range.extractContents()); range.insertNode(span); } catch (err2) { toast('could not mark that selection'); return; }
  }
  sel.removeAllRanges();
  pending = { span, ctx };
  const rect = span.getBoundingClientRect();
  const pop = $('popover');
  pop.hidden = false;
  const maxLeft = window.scrollX + document.documentElement.clientWidth - 360;
  pop.style.left = Math.max(8, Math.min(window.scrollX + rect.left, maxLeft)) + 'px';
  pop.style.top = (window.scrollY + rect.bottom + 6) + 'px';
  const input = $('note-input');
  input.value = '';
  input.focus();
}

/* Unwrap the pending span and hide the popover. Does not re-render. */
function clearPending() {
  if (pending && pending.span && pending.span.parentNode) {
    const span = pending.span;
    const parent = span.parentNode;
    while (span.firstChild) parent.insertBefore(span.firstChild, span);
    parent.removeChild(span);
    parent.normalize();
  }
  pending = null;
  $('popover').hidden = true;
}

function cancelNote() {
  clearPending();
  applyHighlights();
  layoutMargin();
}

async function commitNote() {
  if (!pending) return;
  const note = $('note-input').value.trim();
  if (!note) { cancelNote(); return; }
  const body = Object.assign({ session_id: state.conv.session_id, note }, pending.ctx);
  clearPending();
  try {
    await api('POST', '/api/annotation', body);
    toast('note saved');
    await refreshConversation();
    await afterChange();
  } catch (e) {
    toast('save failed: ' + e.message);
    applyHighlights();
    layoutMargin();
  }
}

/* Re-fetch the open conversation's notes, suggestions and labels without
   rebuilding the turns (keeps expanded results and scroll position). */
async function refreshConversation() {
  if (!state.conv) return;
  const sid = state.conv.session_id;
  const conv = await api('GET', '/api/conversation/' + encodeURIComponent(sid));
  if (!state.conv || state.conv.session_id !== sid) return;
  state.conv = conv;
  const foot = document.querySelector('#conversation .conv-foot');
  if (foot) foot.outerHTML = renderFooter();
  applyHighlights();
  layoutMargin();
  renderLabels();
}

function startEdit(id) {
  const body = document.querySelector('#margin [data-note-body="' + id + '"]');
  if (!body) return;
  const current = body.textContent;
  body.innerHTML = '<textarea class="edit" rows="3"></textarea><div class="hint">Enter saves · Esc cancels</div>';
  const ta = body.querySelector('textarea');
  ta.value = current;
  ta.focus();
  ta.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const note = ta.value.trim();
      if (!note) return;
      try {
        await api('PATCH', '/api/annotation/' + encodeURIComponent(id), { note });
        toast('note updated');
        await refreshConversation();
        await afterChange();
      } catch (err) {
        toast('update failed: ' + err.message);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      layoutMargin();
    }
  });
}

async function deleteAnnotation(id) {
  try {
    await api('DELETE', '/api/annotation/' + encodeURIComponent(id));
    toast('note deleted');
    await refreshConversation();
    await afterChange();
  } catch (e) {
    toast('delete failed: ' + e.message);
  }
}

async function markNoFailure() {
  const c = state.conv;
  if (!c) return;
  if ((c.annotations || []).some((a) => a.kind === 'no_failure')) {
    toast('already marked: no failure observed');
    return;
  }
  const last = c.turns[c.turns.length - 1];
  try {
    await api('POST', '/api/no_failure', { session_id: c.session_id, trace_id: last.trace_id });
    toast('marked: no failure observed');
    await refreshConversation();
    await afterChange();
  } catch (e) {
    toast('could not mark: ' + e.message);
  }
}

async function decideSuggestion(id, status) {
  let reason = null;
  if (status === 'rejected') {
    reason = window.prompt('Reason for rejecting (optional)', '');
    if (reason === null) return;
    reason = reason.trim() || null;
  }
  try {
    await api('POST', '/api/suggestion/' + encodeURIComponent(id) + '/decision', { status, reason });
    toast(status === 'accepted' ? 'accepted: now a note tagged suggested · accepted' : 'rejected, kept on file');
    await refreshConversation();
    await afterChange();
  } catch (e) {
    toast('decision failed: ' + e.message);
  }
}

function linkHover(container, findOther) {
  container.addEventListener('mouseover', (e) => {
    const el = e.target.closest('[data-item]');
    if (!el) return;
    const other = findOther(el.dataset.item);
    if (other) other.classList.add('linked');
  });
  container.addEventListener('mouseout', (e) => {
    const el = e.target.closest('[data-item]');
    if (!el) return;
    const other = findOther(el.dataset.item);
    if (other) other.classList.remove('linked');
  });
}

$('conversation').addEventListener('mouseup', onSelection);
$('note-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); commitNote(); }
  else if (e.key === 'Escape') { e.preventDefault(); cancelNote(); }
});
document.addEventListener('mousedown', (e) => {
  const pop = $('popover');
  if (!pop.hidden && !pop.contains(e.target)) cancelNote();
});
$('margin').addEventListener('click', (e) => {
  const b = e.target.closest('[data-action]');
  if (!b) return;
  const id = b.dataset.id;
  if (b.dataset.action === 'accept') decideSuggestion(id, 'accepted');
  else if (b.dataset.action === 'reject') decideSuggestion(id, 'rejected');
  else if (b.dataset.action === 'edit-note') startEdit(id);
  else if (b.dataset.action === 'delete-note') deleteAnnotation(id);
});
linkHover($('margin'), (id) => document.querySelector('#conversation .hl[data-item="' + id + '"]'));
linkHover($('conversation'), (id) => document.querySelector('#margin .mnote[data-item="' + id + '"]'));

/* ---- filled in by later tasks (panel, progress) ------------------------ */

function cancelEvidenceMode() {}
function submitEvidence() {}
function renderLabels() {}
function renderPanel() {}
function refreshProgressView() {}
function toggleModeKey() {}

/* ---- boot -------------------------------------------------------------- */

async function boot() {
  try {
    state.meta = await api('GET', '/api/meta');
  } catch (e) {
    showError('The API did not answer: ' + e.message);
    return;
  }
  renderBanner();
  await Promise.all([refreshQueues(), refreshPatterns(), refreshAnnotations(), refreshProgress(), refreshManifest(), refreshAllIndex()]);
  renderCounts();
  renderPanel();
  const batch = state.queues.find((q) => isBatch(q.name));
  state.queueName = batch ? batch.name : 'final';
  state.list = await api('GET', '/api/conversations?queue=' + encodeURIComponent(state.queueName));
  renderQueuePicker();
  renderConvPicker();
  const hashSid = location.hash.slice(1);
  const start = hashSid && state.allIndex[hashSid] ? hashSid : (state.list[0] && state.list[0].session_id);
  if (start) await openConversation(start);
  else renderNav();
}

boot().catch((e) => showError('Boot failed: ' + e.message));
