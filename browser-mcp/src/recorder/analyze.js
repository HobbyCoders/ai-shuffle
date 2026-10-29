// Flight recorder analyzer: pure functions over a recording, no DOM, no I/O.
//
// A recording (what window.__flightRecorder.stop() returns) is:
//   { version, startedAt, durationMs, initial: { mounts: { Name: count } },
//     timeline: [entry, ...], stats: {...}, options: { cyclic: [...] } }
// Timeline entries, in time order (t = ms since the recording started):
//   { t, kind: 'state', frame, changes: { field: value | null } }   null = gone
//   { t, kind: 'mark', label }
//   { t, kind: 'mount', name, op: 'mount' | 'unmount' }
//   { t, kind: 'error', source: 'error' | 'rejection' | 'console', message }
//   { t, kind: 'request', method, url, status?, error?, aborted? }
//   { t, kind: 'duplicate_rows', ids: [...] }
//   { t, kind: 'layout_shift', value, input, sources: [...] }
//   { t, kind: 'navigation', type: 'load', path, lost? }
//   { t, kind: 'visibility', state: 'hidden' | 'visible' }
//   { t, kind: 'note', message }
//
// Marks split the timeline into steps (one per user action). Reversals and
// remounts are judged inside one step, so a test that deliberately goes
// A -> B -> A with a mark at each action is not flagged.
//
// This file is also embedded verbatim (minus `export`) in flight-recorder.js
// so the page can analyze itself. After editing it run
//   node browser-mcp/scripts/sync-recorder.mjs
// (a browser-mcp test fails until the embedded copy matches). Keep it free of
// imports and of top-level statements other than declarations.

export const ANALYZER_VERSION = 1;

/** Fields that legitimately go A -> B -> A (a load starts and ends). */
export const DEFAULT_CYCLIC_FIELDS = ['loading', 'streaming'];

/**
 * Fields that say which thing is on screen. A field that returns to a value
 * after one of these changed is showing a new thing (chat A active -> loading
 * -> chat B active), not going back, unless it was only a flash.
 */
export const DEFAULT_SCOPE_FIELDS = ['path', 'session'];

export const DEFAULT_THRESHOLDS = {
  /** One layout shift at or above this is flagged. */
  layoutShiftEntry: 0.05,
  /** Total unexpected layout shift at or above this is flagged. */
  layoutShiftTotal: 0.1,
  /** Across a scope change, a return to the same value counts only within this long. */
  scopeFlashMs: 150,
  /** Failed requests to this many sibling URLs are reported as one (`/parent/*`). */
  requestSiblings: 3,
};

const GONE = '(gone)';

function shown(value) {
  if (value === null || value === undefined) return GONE;
  return String(value);
}

function quoted(value) {
  const text = shown(value);
  return /[\s="]/.test(text) || text === '' ? JSON.stringify(text) : text;
}

function ms(t) {
  return `${Math.round(t)}ms`;
}

function clip(text, max) {
  const value = String(text);
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function entriesOf(recording) {
  return recording && Array.isArray(recording.timeline) ? recording.timeline : [];
}

/** Field values after every timeline entry (index-aligned), starting from nothing. */
export function foldStates(recording) {
  const states = [];
  let current = {};
  for (const entry of entriesOf(recording)) {
    if (entry && entry.kind === 'state' && entry.changes) {
      current = { ...current };
      for (const [field, value] of Object.entries(entry.changes)) {
        current[field] = value === undefined ? null : value;
      }
    }
    states.push(current);
  }
  return states;
}

function cyclicSet(recording, options) {
  if (options && Array.isArray(options.cyclic)) return new Set(options.cyclic);
  const recorded = recording && recording.options && Array.isArray(recording.options.cyclic)
    ? recording.options.cyclic
    : DEFAULT_CYCLIC_FIELDS;
  return new Set(recorded);
}

function scopeFieldsOf(recording, options) {
  if (options && Array.isArray(options.scope)) return options.scope;
  if (recording && recording.options && Array.isArray(recording.options.scope)) return recording.options.scope;
  return DEFAULT_SCOPE_FIELDS;
}

/**
 * reversal: inside one step a tracked field returns to a value it had already
 * left (new -> active -> new, or A -> B -> C -> A). Cyclic fields are exempt.
 * When a scope field (path, session) differs between leaving and returning,
 * the field is showing something new, so only a flash (scopeFlashMs) counts.
 */
export function detectReversals(recording, options = {}) {
  const timeline = entriesOf(recording);
  const cyclic = cyclicSet(recording, options);
  const scopeFields = scopeFieldsOf(recording, options);
  const flashMs = { ...DEFAULT_THRESHOLDS, ...(options.thresholds || {}) }.scopeFlashMs;
  const anomalies = [];
  let current = {};
  // field -> runs of distinct values in the current step
  let runs = new Map();
  // field -> anomaly already raised in this step
  let raised = new Map();
  let step = 0;

  function scopeOf(state, field) {
    const scope = {};
    for (const name of scopeFields) if (name !== field) scope[name] = name in state ? state[name] : null;
    return scope;
  }

  function runsFor(field, before) {
    let list = runs.get(field);
    if (!list) {
      const value = field in before ? before[field] : null;
      list = [{ value, index: null, t: null, scope: scopeOf(before, field) }];
      runs.set(field, list);
    }
    return list;
  }

  timeline.forEach((entry, index) => {
    if (!entry) return;
    if (entry.kind === 'mark') {
      step += 1;
      runs = new Map();
      raised = new Map();
      return;
    }
    if (entry.kind !== 'state' || !entry.changes) return;
    // Every change in an entry happened in the same frame: judge each field
    // against the whole state before and after it.
    const before = current;
    current = { ...current };
    for (const [field, raw] of Object.entries(entry.changes)) current[field] = raw === undefined ? null : raw;
    for (const field of Object.keys(entry.changes)) {
      const value = current[field];
      const list = runsFor(field, before);
      if (cyclic.has(field)) continue;
      const last = list[list.length - 1];
      if (last.value === value) continue;
      const scope = scopeOf(current, field);
      list.push({ value, index, t: entry.t, scope });
      let earlier = -1;
      for (let i = list.length - 3; i >= 0; i -= 1) {
        if (list[i].value === value) { earlier = i; break; }
      }
      if (earlier < 0) continue;
      const path = list.slice(earlier).map((run) => shown(run.value));
      const departure = list[earlier + 1];
      const awayMs = Math.max(0, entry.t - departure.t);
      const moved = Object.keys(scope).filter((name) => list[earlier].scope[name] !== scope[name]);
      if (moved.length && awayMs > flashMs) continue;
      const scopeNote = moved.length
        ? ` while ${moved.map((name) => `${name} changed ${shown(list[earlier].scope[name])} → ${shown(scope[name])}`).join(' and ')}`
        : '';
      const frames = typeof entry.frame === 'number' && typeof timeline[departure.index].frame === 'number'
        ? entry.frame - timeline[departure.index].frame
        : null;
      const indices = list.slice(earlier + 1).map((run) => run.index);
      const existing = raised.get(field);
      if (existing) {
        existing.count += 1;
        for (const i of indices) if (!existing.indices.includes(i)) existing.indices.push(i);
        existing.t[1] = entry.t;
        existing.message = `${existing.base} (happened ${existing.count} times in this step)`;
        continue;
      }
      const flicker = frames !== null && frames <= 2;
      const base = `${field} went ${path.join(' → ')} within one step: back to ${quoted(value)} after ${ms(awayMs)}`
        + (frames !== null ? ` (${frames} frame${frames === 1 ? '' : 's'})` : '')
        + scopeNote
        + (flicker ? ' — a flicker' : '');
      const anomaly = {
        kind: 'reversal',
        field,
        path,
        step,
        away_ms: Math.round(awayMs),
        ...(frames !== null ? { frames } : {}),
        flicker,
        ...(moved.length ? { scope_changed: moved } : {}),
        count: 1,
        indices,
        t: [departure.t, entry.t],
        base,
        message: base,
      };
      raised.set(field, anomaly);
      anomalies.push(anomaly);
    }
  });
  return anomalies.map(({ base, ...rest }) => rest);
}

/**
 * remount: inside one step a named component (data-ui-mount) is unmounted and
 * mounted again, or mounted more than once. A full page load resets the
 * baseline: everything mounts afresh after a navigation.
 */
export function detectRemounts(recording) {
  const timeline = entriesOf(recording);
  const anomalies = [];
  let unmounted = new Map(); // name -> unmount entry index (this step)
  let mounts = new Map(); // name -> [indices] (this step)
  let raised = new Map();
  let step = 0;
  timeline.forEach((entry, index) => {
    if (!entry) return;
    if (entry.kind === 'mark') {
      step += 1;
      unmounted = new Map();
      mounts = new Map();
      raised = new Map();
      return;
    }
    if (entry.kind === 'navigation') {
      unmounted = new Map();
      mounts = new Map();
      return;
    }
    if (entry.kind !== 'mount' || typeof entry.name !== 'string') return;
    const name = entry.name;
    if (entry.op === 'unmount') {
      unmounted.set(name, index);
      return;
    }
    if (entry.op !== 'mount') return;
    const list = mounts.get(name) || [];
    list.push(index);
    mounts.set(name, list);
    const gone = unmounted.get(name);
    if (gone === undefined && list.length < 2) return;
    const existing = raised.get(name);
    if (existing) {
      existing.count += 1;
      if (gone !== undefined && !existing.indices.includes(gone)) existing.indices.push(gone);
      existing.indices.push(index);
      existing.t[1] = entry.t;
      existing.message = `${name} was remounted ${existing.count} times in one step`;
      return;
    }
    let message;
    let indices;
    let start;
    if (gone !== undefined) {
      const gap = Math.max(0, entry.t - timeline[gone].t);
      message = `${name} unmounted at ${ms(timeline[gone].t)} and mounted again at ${ms(entry.t)} (${ms(gap)} gap) within one step`;
      indices = [gone, index];
      start = timeline[gone].t;
    } else {
      message = `${name} mounted ${list.length} times within one step without unmounting`;
      indices = list.slice();
      start = timeline[list[0]].t;
    }
    const anomaly = { kind: 'remount', name, step, count: 1, indices, t: [start, entry.t], message };
    raised.set(name, anomaly);
    anomalies.push(anomaly);
  });
  return anomalies;
}

/** layout_shift: unexpected (no recent input) layout shift above a threshold. */
export function detectLayoutShift(recording, options = {}) {
  const thresholds = { ...DEFAULT_THRESHOLDS, ...(options.thresholds || {}) };
  const timeline = entriesOf(recording);
  const shifts = [];
  timeline.forEach((entry, index) => {
    if (entry && entry.kind === 'layout_shift' && !entry.input && typeof entry.value === 'number') {
      shifts.push({ entry, index });
    }
  });
  const statsTotal = recording && recording.stats && recording.stats.layoutShift
    && typeof recording.stats.layoutShift.total === 'number'
    ? recording.stats.layoutShift.total
    : null;
  const listedTotal = shifts.reduce((sum, item) => sum + item.entry.value, 0);
  const total = statsTotal === null ? listedTotal : Math.max(statsTotal, listedTotal);
  const big = shifts.filter((item) => item.entry.value >= thresholds.layoutShiftEntry);
  if (!big.length && total < thresholds.layoutShiftTotal) return [];
  const ranked = (big.length ? big : shifts).slice().sort((a, b) => b.entry.value - a.entry.value);
  const top = ranked[0];
  const sources = top && Array.isArray(top.entry.sources) ? top.entry.sources.slice(0, 3) : [];
  const message = `layout shifted ${total.toFixed(3)} in total`
    + (top ? `; biggest ${top.entry.value.toFixed(3)} at ${ms(top.entry.t)}` : '')
    + (sources.length ? ` (moved: ${sources.join(', ')})` : '');
  const indices = ranked.slice(0, 5).map((item) => item.index).sort((a, b) => a - b);
  return [{
    kind: 'layout_shift',
    total: Number(total.toFixed(4)),
    ...(top ? { largest: Number(top.entry.value.toFixed(4)) } : {}),
    sources,
    indices,
    t: indices.length ? [timeline[indices[0]].t, timeline[indices[indices.length - 1]].t] : [0, 0],
    message,
  }];
}

function grouped(timeline, kind, keyOf, accept = () => true) {
  const groups = new Map();
  timeline.forEach((entry, index) => {
    if (!entry || entry.kind !== kind || !accept(entry)) return;
    const key = keyOf(entry);
    const group = groups.get(key);
    if (group) {
      group.indices.push(index);
    } else {
      groups.set(key, { entry, indices: [index] });
    }
  });
  return [...groups.values()];
}

/** errors: window errors, unhandled rejections and console.error, grouped by text. */
export function detectErrors(recording) {
  const timeline = entriesOf(recording);
  return grouped(timeline, 'error', (e) => `${e.source}\u0000${e.message}`).map(({ entry, indices }) => {
    const label = entry.source === 'console' ? 'console.error'
      : entry.source === 'rejection' ? 'unhandled rejection' : 'uncaught error';
    const times = indices.length > 1 ? `${indices.length}× ` : '';
    return {
      kind: 'errors',
      source: entry.source,
      count: indices.length,
      indices,
      t: [timeline[indices[0]].t, timeline[indices[indices.length - 1]].t],
      message: `${times}${label} at ${ms(entry.t)}: ${clip(entry.message, 200)}`,
    };
  });
}

function parentPath(url) {
  const bare = String(url).replace(/\?.*$/, '');
  const cut = bare.lastIndexOf('/');
  return cut > 0 ? bare.slice(0, cut) : null;
}

/**
 * failed_requests: fetch/XHR answered >= 400 or failed on the network (aborts
 * excluded), grouped by URL. When `requestSiblings` or more URLs under one
 * parent fail the same way (PUT /preferences/a, /b, /c → 403) they are one
 * anomaly for `parent/*` with a few examples.
 */
export function detectFailedRequests(recording, options = {}) {
  const timeline = entriesOf(recording);
  const siblings = { ...DEFAULT_THRESHOLDS, ...(options.thresholds || {}) }.requestSiblings;
  const outcomeOf = (e) => (e.status === undefined ? `e:${e.error}` : `s:${e.status}`);
  const groups = grouped(timeline, 'request', (e) => `${e.method} ${e.url} ${outcomeOf(e)}`, (e) => !e.aborted);
  const families = new Map();
  for (const group of groups) {
    const parent = parentPath(group.entry.url);
    if (parent === null) continue;
    const key = `${group.entry.method} ${parent} ${outcomeOf(group.entry)}`;
    const family = families.get(key) || [];
    family.push(group);
    families.set(key, family);
  }
  const merged = new Set();
  const results = [];
  for (const family of families.values()) {
    if (family.length < siblings) continue;
    family.forEach((group) => merged.add(group));
    const first = family[0];
    const indices = family.flatMap((group) => group.indices).sort((a, b) => a - b);
    results.push({ entry: first.entry, indices, url: `${parentPath(first.entry.url)}/*`, examples: family.map((group) => group.entry.url) });
  }
  for (const group of groups) if (!merged.has(group)) results.push({ ...group, url: group.entry.url });
  return results
    .sort((a, b) => a.indices[0] - b.indices[0])
    .map(({ entry, indices, url, examples }) => {
      const outcome = typeof entry.status === 'number' ? `HTTP ${entry.status}` : `network error (${entry.error || 'failed'})`;
      const times = indices.length > 1 ? `${indices.length}× ` : '';
      const across = examples
        ? ` (${examples.length} URLs, e.g. ${examples.slice(0, 3).map((example) => example.slice(example.lastIndexOf('/') + 1)).join(', ')})`
        : '';
      return {
        kind: 'failed_requests',
        method: entry.method,
        url,
        ...(typeof entry.status === 'number' ? { status: entry.status } : { error: entry.error || 'failed' }),
        count: indices.length,
        ...(examples ? { urls: examples.length } : {}),
        indices,
        t: [timeline[indices[0]].t, timeline[indices[indices.length - 1]].t],
        message: `${times}${entry.method} ${url} → ${outcome} at ${ms(timeline[indices[0]].t)}${across}`,
      };
    });
}

/** duplicate_rows: the same data-ui-row id present more than once in the DOM. */
export function detectDuplicateRows(recording) {
  const timeline = entriesOf(recording);
  const groups = new Map();
  timeline.forEach((entry, index) => {
    if (!entry || entry.kind !== 'duplicate_rows' || !Array.isArray(entry.ids)) return;
    for (const id of entry.ids) {
      const group = groups.get(id);
      if (group) group.indices.push(index);
      else groups.set(id, { id, first: entry.t, indices: [index] });
    }
  });
  if (!groups.size) return [];
  const all = [...groups.values()];
  const indices = [...new Set(all.flatMap((group) => group.indices))].sort((a, b) => a - b);
  const ids = all.map((group) => group.id);
  return [{
    kind: 'duplicate_rows',
    ids,
    indices,
    t: [timeline[indices[0]].t, timeline[indices[indices.length - 1]].t],
    message: `${ids.length} transcript row id${ids.length === 1 ? ' was' : 's were'} on the page twice: `
      + `${ids.slice(0, 5).map((id) => clip(id, 48)).join(', ')}${ids.length > 5 ? ', …' : ''} (first at ${ms(all[0].first)})`,
  }];
}

/**
 * recording_gap: part of the recording was lost (a full navigation without a
 * handoff, the earliest entries trimmed to fit a page-load handoff, or the
 * size cap dropping later entries).
 */
export function detectRecordingGaps(recording) {
  const timeline = entriesOf(recording);
  const anomalies = [];
  timeline.forEach((entry, index) => {
    if (entry && entry.kind === 'navigation' && entry.lost) {
      anomalies.push({
        kind: 'recording_gap',
        indices: [index],
        t: [entry.t, entry.t],
        message: `a full page load at ${ms(entry.t)} lost the previous document's recording (cross-origin navigation or storage blocked)`,
      });
    }
  });
  const stats = recording && recording.stats;
  if (stats && stats.handoffDropped > 0) {
    const before = stats.handoffDroppedBeforeMs || 0;
    anomalies.push({
      kind: 'recording_gap',
      indices: [],
      t: [0, before],
      message: `${stats.handoffDropped} earliest entries (before ${ms(before)}) were dropped to fit the page-load handoff`,
    });
  }
  if (stats && stats.truncated) {
    anomalies.push({
      kind: 'recording_gap',
      indices: [],
      t: [0, recording.durationMs || 0],
      message: `the recording hit its size cap; ${stats.dropped || 0} later entries were not kept`,
    });
  }
  return anomalies;
}

const ORDER = ['reversal', 'remount', 'duplicate_rows', 'errors', 'failed_requests', 'layout_shift', 'recording_gap'];

/** Every detector, ordered by kind then time. */
export function analyzeRecording(recording, options = {}) {
  const anomalies = [
    ...detectReversals(recording, options),
    ...detectRemounts(recording),
    ...detectDuplicateRows(recording),
    ...detectErrors(recording),
    ...detectFailedRequests(recording, options),
    ...detectLayoutShift(recording, options),
    ...detectRecordingGaps(recording),
  ];
  return anomalies.sort((a, b) => (ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind)) || (a.t[0] - b.t[0]));
}

/** "/path  a=1 b=2": the path (when given) leads, two spaces, then the fields. */
function stateLine(values, withPath) {
  const fields = Object.entries(values)
    .filter(([field]) => field !== 'path')
    .map(([field, value]) => `${field}=${quoted(value)}`)
    .join(' ');
  const path = withPath ? shown(values.path) : '';
  return [path, fields].filter(Boolean).join('  ') || '(no visible change)';
}

function describeEntry(entry, state) {
  switch (entry.kind) {
    case 'state': {
      const changes = entry.changes || {};
      return stateLine(changes, 'path' in changes);
    }
    case 'mark':
      return `> mark ${JSON.stringify(clip(entry.label, 120))}`;
    case 'mount':
      return `${entry.op === 'unmount' ? '-' : '+'} ${entry.name} ${entry.op === 'unmount' ? 'unmounted' : 'mounted'}`;
    case 'error':
      return `! ${entry.source === 'console' ? 'console.error' : entry.source === 'rejection' ? 'unhandled rejection' : 'error'}: ${clip(entry.message, 160)}`;
    case 'request':
      return `${entry.aborted ? '·' : '!'} ${entry.method} ${entry.url} → ${typeof entry.status === 'number' ? entry.status : (entry.aborted ? 'aborted' : entry.error || 'failed')}`;
    case 'duplicate_rows':
      return `! duplicate rows: ${entry.ids.slice(0, 5).map((id) => clip(id, 48)).join(', ')}`;
    case 'layout_shift':
      return `~ layout shift ${Number(entry.value).toFixed(3)}${entry.input ? ' (after input)' : ''}${entry.sources && entry.sources.length ? ` ${entry.sources.slice(0, 3).join(', ')}` : ''}`;
    case 'navigation':
      return `* full page load${entry.path ? ` ${entry.path}` : ''}${entry.lost ? ' (previous document lost)' : ''}`;
    case 'visibility':
      return `* page ${entry.state}`;
    case 'note':
      return `* ${clip(entry.message, 160)}`;
    default:
      return `* ${entry.kind}`;
  }
}

/**
 * Text timeline, one line per entry:
 *      0ms  /new  surface=new composer=centre
 *     48ms  surface=active composer=bottom   << reversal(surface)
 * The first state line shows every field; later lines show only what changed.
 * Lines an anomaly points at end with "<< kind". Long timelines keep the
 * start, the end, every mark and the lines around each anomaly.
 */
export function formatTimeline(recording, options = {}) {
  const timeline = entriesOf(recording);
  const anomalies = options.anomalies || (recording && recording.anomalies) || [];
  const maxLines = options.maxLines || 400;
  const flagged = new Map();
  for (const anomaly of anomalies) {
    const label = anomaly.field ? `${anomaly.kind}(${anomaly.field})` : anomaly.name ? `${anomaly.kind}(${anomaly.name})` : anomaly.kind;
    for (const index of anomaly.indices || []) {
      const list = flagged.get(index) || [];
      if (!list.includes(label)) list.push(label);
      flagged.set(index, list);
    }
  }
  const width = Math.max(6, ...timeline.map((entry) => ms(entry.t || 0).length));
  const states = foldStates(recording);
  const lines = [];
  let first = true;
  const initialMounts = recording && recording.initial && recording.initial.mounts
    ? Object.keys(recording.initial.mounts)
    : [];
  if (initialMounts.length) {
    lines.push({ index: -1, text: `${'0ms'.padStart(width)}  mounted at start: ${initialMounts.join(', ')}` });
  }
  timeline.forEach((entry, index) => {
    if (!entry) return;
    let body;
    if (entry.kind === 'state' && first) {
      first = false;
      body = stateLine(states[index], 'path' in states[index]);
    } else {
      body = describeEntry(entry, states[index]);
    }
    const marks = flagged.get(index);
    lines.push({ index, text: `${ms(entry.t || 0).padStart(width)}  ${body}${marks ? `   << ${marks.join(', ')}` : ''}`, keep: Boolean(marks) || entry.kind === 'mark' });
  });
  if (lines.length <= maxLines) return lines.map((line) => line.text).join('\n');
  const keep = new Set();
  const edge = Math.max(5, Math.floor(maxLines / 8));
  lines.forEach((line, position) => {
    if (position < edge || position >= lines.length - edge) keep.add(position);
    if (line.keep) for (let d = -3; d <= 3; d += 1) keep.add(position + d);
  });
  const out = [];
  let skipped = 0;
  lines.forEach((line, position) => {
    if (keep.has(position)) {
      if (skipped) out.push(`${''.padStart(width)}  … ${skipped} line${skipped === 1 ? '' : 's'} …`);
      skipped = 0;
      out.push(line.text);
    } else {
      skipped += 1;
    }
  });
  if (skipped) out.push(`${''.padStart(width)}  … ${skipped} line${skipped === 1 ? '' : 's'} …`);
  return out.join('\n');
}

/** Numbered anomaly list, or "no anomalies". */
export function formatAnomalies(anomalies) {
  if (!anomalies || !anomalies.length) return 'no anomalies';
  return anomalies.map((anomaly, i) => `${i + 1}. ${anomaly.kind}: ${anomaly.message}`).join('\n');
}

/** Headline, timeline and anomalies: what an agent reads. */
export function formatReport(recording, options = {}) {
  const anomalies = options.anomalies || (recording && recording.anomalies) || analyzeRecording(recording, options);
  const stats = (recording && recording.stats) || {};
  const head = `flight recording: ${ms((recording && recording.durationMs) || 0)}, ${stats.frames || 0} frames, `
    + `${stats.stateChanges || 0} state changes, ${anomalies.length} anomal${anomalies.length === 1 ? 'y' : 'ies'}`
    + (stats.maxFrameGapMs ? `, longest frame gap ${ms(stats.maxFrameGapMs)}` : '');
  return `${head}\n${formatTimeline(recording, { ...options, anomalies })}\n\nanomalies:\n${formatAnomalies(anomalies)}`;
}
