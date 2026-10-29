/*
 * UI flight recorder. Dependency-free; inject into any page (addInitScript,
 * page.evaluate, browser_eval). Installs window.__flightRecorder:
 *
 *   start(opts)  begin recording            -> { ok, id, startedAt }
 *   mark(label)  name the next user action  -> { ok, index }
 *   stop()       end and analyze            -> recording { timeline, anomalies, stats, text, ... }
 *   peek()       cheap progress probe       -> { recording, entries, t }
 *   snapshot()   analyzed copy, keeps going
 *   signature()  the fields this page exposes right now
 *   resume(cfg)  continue after a full page load (used by the init script)
 *
 * Every animation frame it reads a signature (location.pathname+search and the
 * data-ui-* attributes on [data-ui-state] carriers, or opts.fields anywhere,
 * plus counts for opts.selectors) and records it only when it changed. It also
 * counts [data-ui-mount] elements mounting/unmounting, layout shifts, errors,
 * failed fetch/XHR requests and [data-ui-row] ids present twice.
 * Field names and how to add one: docs/internal/flight-recorder.md.
 *
 * It must never throw into the page and must cost close to nothing: every
 * hook is wrapped, wrappers pass through untouched once stopped, and the
 * per-frame work is a few querySelectorAll calls.
 */
(function () {
  'use strict';
  var VERSION = 1;
  var root = typeof window !== 'undefined' ? window : undefined;
  if (!root || !root.document) return;
  var existing = root.__flightRecorder;
  if (existing && typeof existing.version === 'number' && existing.version >= VERSION) return;

  /* @generated-begin analyze.js: do not edit here; edit analyze.js and run `node browser-mcp/scripts/sync-recorder.mjs` */
  var ANALYZER = (function () {
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

  const ANALYZER_VERSION = 1;

  /** Fields that legitimately go A -> B -> A (a load starts and ends). */
  const DEFAULT_CYCLIC_FIELDS = ['loading', 'streaming'];

  const DEFAULT_THRESHOLDS = {
    /** One layout shift at or above this is flagged. */
    layoutShiftEntry: 0.05,
    /** Total unexpected layout shift at or above this is flagged. */
    layoutShiftTotal: 0.1,
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
  function foldStates(recording) {
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

  /**
   * reversal: inside one step a tracked field returns to a value it had already
   * left (new -> active -> new, or A -> B -> C -> A). Cyclic fields are exempt.
   */
  function detectReversals(recording, options = {}) {
    const timeline = entriesOf(recording);
    const cyclic = cyclicSet(recording, options);
    const anomalies = [];
    const current = {};
    // field -> runs of distinct values in the current step
    let runs = new Map();
    // field -> anomaly already raised in this step
    let raised = new Map();
    let step = 0;

    function runsFor(field) {
      let list = runs.get(field);
      if (!list) {
        const value = field in current ? current[field] : null;
        list = [{ value, index: null, t: null }];
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
      for (const [field, raw] of Object.entries(entry.changes)) {
        const value = raw === undefined ? null : raw;
        const list = runsFor(field);
        current[field] = value;
        if (cyclic.has(field)) continue;
        const last = list[list.length - 1];
        if (last.value === value) continue;
        list.push({ value, index, t: entry.t });
        let earlier = -1;
        for (let i = list.length - 3; i >= 0; i -= 1) {
          if (list[i].value === value) { earlier = i; break; }
        }
        if (earlier < 0) continue;
        const path = list.slice(earlier).map((run) => shown(run.value));
        const departure = list[earlier + 1];
        const awayMs = Math.max(0, entry.t - departure.t);
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
          + (flicker ? ' — a flicker' : '');
        const anomaly = {
          kind: 'reversal',
          field,
          path,
          step,
          away_ms: Math.round(awayMs),
          ...(frames !== null ? { frames } : {}),
          flicker,
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
  function detectRemounts(recording) {
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
  function detectLayoutShift(recording, options = {}) {
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
  function detectErrors(recording) {
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

  /** failed_requests: fetch/XHR answered >= 400 or failed on the network (aborts excluded). */
  function detectFailedRequests(recording) {
    const timeline = entriesOf(recording);
    const key = (e) => `${e.method} ${e.url} ${e.status === undefined ? e.error : e.status}`;
    return grouped(timeline, 'request', key, (e) => !e.aborted).map(({ entry, indices }) => {
      const outcome = typeof entry.status === 'number' ? `HTTP ${entry.status}` : `network error (${entry.error || 'failed'})`;
      const times = indices.length > 1 ? `${indices.length}× ` : '';
      return {
        kind: 'failed_requests',
        method: entry.method,
        url: entry.url,
        ...(typeof entry.status === 'number' ? { status: entry.status } : { error: entry.error || 'failed' }),
        count: indices.length,
        indices,
        t: [timeline[indices[0]].t, timeline[indices[indices.length - 1]].t],
        message: `${times}${entry.method} ${entry.url} → ${outcome} at ${ms(entry.t)}`,
      };
    });
  }

  /** duplicate_rows: the same data-ui-row id present more than once in the DOM. */
  function detectDuplicateRows(recording) {
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

  /** recording_gap: part of the recording was lost (a full navigation without a handoff, or caps). */
  function detectRecordingGaps(recording) {
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
  function analyzeRecording(recording, options = {}) {
    const anomalies = [
      ...detectReversals(recording, options),
      ...detectRemounts(recording),
      ...detectDuplicateRows(recording),
      ...detectErrors(recording),
      ...detectFailedRequests(recording),
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
  function formatTimeline(recording, options = {}) {
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
  function formatAnomalies(anomalies) {
    if (!anomalies || !anomalies.length) return 'no anomalies';
    return anomalies.map((anomaly, i) => `${i + 1}. ${anomaly.kind}: ${anomaly.message}`).join('\n');
  }

  /** Headline, timeline and anomalies: what an agent reads. */
  function formatReport(recording, options = {}) {
    const anomalies = options.anomalies || (recording && recording.anomalies) || analyzeRecording(recording, options);
    const stats = (recording && recording.stats) || {};
    const head = `flight recording: ${ms((recording && recording.durationMs) || 0)}, ${stats.frames || 0} frames, `
      + `${stats.stateChanges || 0} state changes, ${anomalies.length} anomal${anomalies.length === 1 ? 'y' : 'ies'}`
      + (stats.maxFrameGapMs ? `, longest frame gap ${ms(stats.maxFrameGapMs)}` : '');
    return `${head}\n${formatTimeline(recording, { ...options, anomalies })}\n\nanomalies:\n${formatAnomalies(anomalies)}`;
  }
  return { ANALYZER_VERSION, DEFAULT_CYCLIC_FIELDS, DEFAULT_THRESHOLDS, foldStates, detectReversals, detectRemounts, detectLayoutShift, detectErrors, detectFailedRequests, detectDuplicateRows, detectRecordingGaps, analyzeRecording, formatTimeline, formatAnomalies, formatReport };
  })();
  /* @generated-end analyze.js */

  var KEY_PREFIX = '__flightRecorder:';
  var RESERVED = { mount: 1, row: 1, state: 1 };
  var SENSITIVE_PARAM = /token|secret|key|pass|auth|sig|code|session|credential|otp|jwt|ticket/i;
  var MAX_VALUE = 64;
  var DEFAULTS = { maxEntries: 5000, maxDurationMs: 300000, persist: true, console: true, network: true };

  var S = null; // the active recording, or null
  var lastResult = null;

  function now() {
    var perf = root.performance;
    if (perf && typeof perf.now === 'function' && typeof perf.timeOrigin === 'number') return perf.timeOrigin + perf.now();
    return Date.now();
  }
  function elapsed() { return S ? Math.max(0, Math.round(now() - S.startEpoch)) : 0; }
  function clip(text, max) {
    var value = String(text);
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  }
  function note(error) {
    if (S) S.stats.internalErrors += 1;
    if (S && !S.firstInternalError) S.firstInternalError = clip(error && error.message ? error.message : error, 160);
  }

  function push(entry, force) {
    if (!S) return -1;
    if (S.timeline.length >= S.opts.maxEntries && !(force && S.timeline.length < S.opts.maxEntries + 200)) {
      S.stats.truncated = true;
      S.stats.dropped += 1;
      return -1;
    }
    S.timeline.push(entry);
    return S.timeline.length - 1;
  }

  // ---- signature -----------------------------------------------------------

  function cleanPath() {
    var loc = root.location;
    var search = (loc.search || '').replace(/([?&])([^=&]*)=([^&]*)/g, function (match, sep, name) {
      return SENSITIVE_PARAM.test(name) ? sep + name + '=…' : match;
    });
    return clip(loc.pathname + search, 200);
  }
  // Error and request text is page data that can quote URLs or headers; drop
  // anything shaped like a credential before it enters the recording.
  function scrub(text) {
    return String(text)
      .replace(/([?&#;][^=&\s#?]*=)([^&\s#"'<>]*)/g, function (match, name) {
        return SENSITIVE_PARAM.test(name) ? name + '…' : match;
      })
      .replace(/\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+\/=-]{6,}/gi, '$1 …')
      .replace(/[A-Za-z0-9_+\/=-]{40,}/g, '…');
  }
  function cleanValue(value) {
    return clip(String(value).replace(/\s+/g, ' ').trim(), MAX_VALUE);
  }
  function cleanUrl(raw) {
    try {
      var url = new URL(String(raw), root.location.href);
      var base = url.origin === root.location.origin ? url.pathname : url.origin + url.pathname;
      return clip(base + (url.search ? '?…' : ''), 200);
    } catch (error) {
      return clip(String(raw).split(/[?#]/)[0], 200);
    }
  }
  function addValue(values, field, value) {
    var clean = cleanValue(value);
    values[field] = field in values ? values[field] + '|' + clean : clean;
  }

  function readSignature(opts) {
    var values = { path: cleanPath() };
    var doc = root.document;
    var i;
    var j;
    if (opts.fields && opts.fields.length) {
      for (i = 0; i < opts.fields.length; i += 1) {
        var field = opts.fields[i];
        var holders = doc.querySelectorAll('[data-ui-' + field + ']');
        for (j = 0; j < holders.length; j += 1) addValue(values, field, holders[j].getAttribute('data-ui-' + field));
      }
    } else {
      var carriers = doc.querySelectorAll('[data-ui-state]');
      for (i = 0; i < carriers.length; i += 1) {
        var names = carriers[i].getAttributeNames();
        for (j = 0; j < names.length; j += 1) {
          var name = names[j];
          if (name.lastIndexOf('data-ui-', 0) !== 0) continue;
          var key = name.slice(8);
          if (!key || RESERVED[key]) continue;
          addValue(values, key, carriers[i].getAttribute(name));
        }
      }
    }
    for (i = 0; i < opts.selectorList.length; i += 1) {
      var pair = opts.selectorList[i];
      values[pair[0]] = String(doc.querySelectorAll(pair[1]).length);
    }
    return values;
  }

  function diff(previous, next) {
    var changes = null;
    var field;
    for (field in next) {
      if (previous[field] !== next[field]) { changes = changes || {}; changes[field] = next[field]; }
    }
    for (field in previous) {
      if (!(field in next) && previous[field] !== null) { changes = changes || {}; changes[field] = null; }
    }
    return changes;
  }

  function sample() {
    var started = root.performance && root.performance.now ? root.performance.now() : 0;
    var values = readSignature(S.opts);
    var changes = diff(S.last, values);
    if (changes) {
      var merged = {};
      var field;
      for (field in S.last) merged[field] = S.last[field];
      for (field in changes) merged[field] = changes[field];
      S.last = merged;
      S.stats.stateChanges += 1;
      push({ t: elapsed(), kind: 'state', frame: S.frame, changes: changes });
    }
    if (S.rowsDirty) {
      S.rowsDirty = false;
      checkRows();
    }
    if (started) {
      var cost = root.performance.now() - started;
      S.stats.sampleMsTotal += cost;
      if (cost > S.stats.sampleMsMax) S.stats.sampleMsMax = cost;
    }
  }

  function tick() {
    if (!S || !S.active) return;
    S.raf = root.requestAnimationFrame(tick);
    try {
      var at = now();
      if (S.lastFrameEpoch) {
        var gap = at - S.lastFrameEpoch;
        if (!root.document.hidden) {
          if (gap > S.stats.maxFrameGapMs) S.stats.maxFrameGapMs = Math.round(gap);
          if (gap > 50) S.stats.longFrames += 1;
        }
      }
      S.lastFrameEpoch = at;
      S.frame += 1;
      S.stats.frames += 1;
      sample();
      if (at - S.startEpoch > S.opts.maxDurationMs) halt('reached maxDurationMs (' + S.opts.maxDurationMs + 'ms)');
    } catch (error) {
      note(error);
    }
  }

  // ---- rows ----------------------------------------------------------------

  function checkRows() {
    var rows = root.document.querySelectorAll('[data-ui-row]');
    var active = {};
    var fresh = [];
    if (rows.length > 1) {
      var seen = {};
      for (var i = 0; i < rows.length; i += 1) {
        var id = clip(rows[i].getAttribute('data-ui-row') || '', 80);
        if (!id) continue;
        if (seen[id]) {
          if (!active[id]) {
            active[id] = true;
            if (!S.duplicates[id]) fresh.push(id);
          }
        } else {
          seen[id] = true;
        }
      }
    }
    S.duplicates = active;
    if (fresh.length) {
      S.stats.duplicateRows += fresh.length;
      push({ t: elapsed(), kind: 'duplicate_rows', ids: fresh.slice(0, 20) });
    }
  }

  // ---- mounts --------------------------------------------------------------

  function marked(node) {
    var list = [];
    if (!node || node.nodeType !== 1) return list;
    if (node.hasAttribute('data-ui-mount')) list.push(node);
    if (node.firstElementChild) {
      var inner = node.querySelectorAll('[data-ui-mount]');
      for (var i = 0; i < inner.length; i += 1) list.push(inner[i]);
    }
    return list;
  }
  function countMount(name, delta) {
    var counts = S.stats.mounts[name] || (S.stats.mounts[name] = { mounted: 0, unmounted: 0 });
    if (delta > 0) counts.mounted += 1; else counts.unmounted += 1;
  }
  function onMutations(records) {
    if (!S || !S.active) return;
    try {
      var removed = [];
      var added = [];
      var i;
      var j;
      for (i = 0; i < records.length; i += 1) {
        var record = records[i];
        if (record.type !== 'childList') continue;
        for (j = 0; j < record.removedNodes.length; j += 1) {
          if (record.removedNodes[j].nodeType === 1) { removed.push(record.removedNodes[j]); S.rowsDirty = true; }
        }
        for (j = 0; j < record.addedNodes.length; j += 1) {
          if (record.addedNodes[j].nodeType === 1) { added.push(record.addedNodes[j]); S.rowsDirty = true; }
        }
      }
      var t = elapsed();
      for (i = 0; i < removed.length; i += 1) {
        var gone = marked(removed[i]);
        for (j = 0; j < gone.length; j += 1) {
          var el = gone[j];
          if (el.isConnected || !S.mounted.has(el)) continue;
          S.mounted.delete(el);
          var name = cleanValue(el.getAttribute('data-ui-mount'));
          countMount(name, -1);
          push({ t: t, kind: 'mount', name: name, op: 'unmount' });
        }
      }
      for (i = 0; i < added.length; i += 1) {
        var fresh = marked(added[i]);
        for (j = 0; j < fresh.length; j += 1) {
          var node = fresh[j];
          if (!node.isConnected) { S.stats.transientMounts += 1; continue; }
          if (S.mounted.has(node)) continue;
          S.mounted.add(node);
          var label = cleanValue(node.getAttribute('data-ui-mount'));
          countMount(label, 1);
          push({ t: t, kind: 'mount', name: label, op: 'mount' });
        }
      }
    } catch (error) {
      note(error);
    }
  }
  function baselineMounts() {
    var counts = {};
    var present = root.document.querySelectorAll('[data-ui-mount]');
    for (var i = 0; i < present.length; i += 1) {
      S.mounted.add(present[i]);
      var name = cleanValue(present[i].getAttribute('data-ui-mount'));
      counts[name] = (counts[name] || 0) + 1;
    }
    return counts;
  }

  // ---- layout shift --------------------------------------------------------

  function describeNode(node) {
    if (!node || node.nodeType !== 1) return null;
    var tag = node.tagName.toLowerCase();
    var cls = typeof node.className === 'string' && node.className.trim() ? '.' + node.className.trim().split(/\s+/)[0] : '';
    var owner = node.closest ? node.closest('[data-ui-mount]') : null;
    var where = owner ? ' in ' + cleanValue(owner.getAttribute('data-ui-mount')) : '';
    return clip(tag + cls + where, 80);
  }
  function onShifts(list) {
    if (!S || !S.active) return;
    try {
      var entries = list.getEntries();
      for (var i = 0; i < entries.length; i += 1) {
        var shift = entries[i];
        var value = Number(shift.value) || 0;
        var input = Boolean(shift.hadRecentInput);
        var ls = S.stats.layoutShift;
        ls.count += 1;
        if (input) ls.inputTotal += value; else ls.total += value;
        var sources = [];
        var raw = shift.sources || [];
        for (var j = 0; j < raw.length && sources.length < 5; j += 1) {
          var label = describeNode(raw[j].node);
          if (label) sources.push(label);
          if (label && !input) S.shiftSources[label] = (S.shiftSources[label] || 0) + value;
        }
        if (value >= 0.001) {
          var at = typeof shift.startTime === 'number' && root.performance && typeof root.performance.timeOrigin === 'number'
            ? Math.max(0, Math.round(root.performance.timeOrigin + shift.startTime - S.startEpoch))
            : elapsed();
          push({ t: at, kind: 'layout_shift', value: Number(value.toFixed(4)), input: input, sources: sources });
        }
      }
    } catch (error) {
      note(error);
    }
  }

  // ---- errors --------------------------------------------------------------

  function describeValue(value) {
    try {
      if (value instanceof Error) return value.name + ': ' + value.message;
      if (typeof value === 'string') return value;
      if (value && typeof value === 'object') {
        if (typeof value.message === 'string') return (value.name ? value.name + ': ' : '') + value.message;
        return JSON.stringify(value);
      }
      return String(value);
    } catch (error) {
      return Object.prototype.toString.call(value);
    }
  }
  function recordError(source, message) {
    if (!S || !S.active) return;
    S.stats.errors += 1;
    push({ t: elapsed(), kind: 'error', source: source, message: clip(scrub(message || '(no message)'), 300) });
  }
  function onError(event) {
    try {
      var target = event && event.target;
      if (target && target !== root && target.nodeType === 1) {
        // A resource (img/script/link) failed to load.
        var url = target.currentSrc || target.src || target.href;
        if (url) recordRequest('GET', cleanUrl(url), { error: 'resource ' + target.tagName.toLowerCase() + ' failed' });
        return;
      }
      recordError('error', event && (event.message || describeValue(event.error)));
    } catch (error) {
      note(error);
    }
  }
  function onRejection(event) {
    try { recordError('rejection', describeValue(event && event.reason)); } catch (error) { note(error); }
  }

  // ---- requests ------------------------------------------------------------

  function recordRequest(method, url, outcome) {
    if (!S || !S.active) return;
    var entry = { t: elapsed(), kind: 'request', method: String(method || 'GET').toUpperCase(), url: url };
    if (typeof outcome.status === 'number') entry.status = outcome.status;
    if (outcome.error) entry.error = clip(scrub(outcome.error), 120);
    if (outcome.aborted) entry.aborted = true;
    if (!outcome.aborted) S.stats.failedRequests += 1;
    push(entry);
  }

  var hooks = null; // installed page hooks, kept across recordings in this document

  function installHooks() {
    if (hooks) { hooks.active = true; return; }
    hooks = { active: true, xhr: typeof WeakMap === 'function' ? new WeakMap() : null };
    var h = hooks;
    // console.error
    try {
      if (root.console && typeof root.console.error === 'function') {
        var originalError = root.console.error;
        var wrappedError = function () {
          if (h.active && S && S.opts.console) {
            try {
              var parts = [];
              for (var i = 0; i < arguments.length && i < 6; i += 1) parts.push(describeValue(arguments[i]));
              recordError('console', parts.join(' '));
            } catch (error) { note(error); }
          }
          return originalError.apply(this, arguments);
        };
        root.console.error = wrappedError;
        h.ownsConsole = function () { return root.console.error === wrappedError; };
        h.restoreConsole = function () { root.console.error = originalError; };
      }
    } catch (error) { note(error); }
    // fetch: hand back a promise that settles exactly like the original
    // (same value or same rejection) so unhandled rejections stay unhandled.
    try {
      if (typeof root.fetch === 'function') {
        var originalFetch = root.fetch;
        var wrappedFetch = function fetch(input, init) {
          var pending = originalFetch.apply(this, arguments);
          if (!h.active || !S || !S.opts.network || !pending || typeof pending.then !== 'function') return pending;
          var method = 'GET';
          var url = '';
          try {
            method = (init && init.method) || (input && typeof input === 'object' && input.method) || 'GET';
            url = cleanUrl(typeof input === 'string' ? input : input && (input.url || input.href) || String(input));
          } catch (error) { note(error); }
          return pending.then(function (response) {
            try { if (response && response.status >= 400) recordRequest(method, url, { status: response.status }); } catch (error) { note(error); }
            return response;
          }, function (reason) {
            try {
              var aborted = Boolean(reason && reason.name === 'AbortError');
              recordRequest(method, url, { error: describeValue(reason), aborted: aborted });
            } catch (error) { note(error); }
            throw reason;
          });
        };
        root.fetch = wrappedFetch;
        h.ownsFetch = function () { return root.fetch === wrappedFetch; };
        h.restoreFetch = function () { root.fetch = originalFetch; };
      }
    } catch (error) { note(error); }
    // XMLHttpRequest
    try {
      var proto = root.XMLHttpRequest && root.XMLHttpRequest.prototype;
      if (proto && h.xhr) {
        var originalOpen = proto.open;
        var originalSend = proto.send;
        var wrappedOpen = function (method, url) {
          try { h.xhr.set(this, { method: String(method || 'GET'), url: cleanUrl(url) }); } catch (error) { note(error); }
          return originalOpen.apply(this, arguments);
        };
        var wrappedSend = function () {
          try {
            var info = h.xhr.get(this);
            if (info && !info.hooked && h.active && S && S.opts.network) {
              info.hooked = true;
              var xhr = this;
              xhr.addEventListener('abort', function () { info.aborted = true; });
              xhr.addEventListener('timeout', function () { info.timeout = true; });
              xhr.addEventListener('error', function () { info.failed = true; });
              xhr.addEventListener('loadend', function () {
                try {
                  if (!h.active) return;
                  if (xhr.status >= 400) recordRequest(info.method, info.url, { status: xhr.status });
                  else if (xhr.status === 0) {
                    recordRequest(info.method, info.url, {
                      error: info.aborted ? 'aborted' : info.timeout ? 'timeout' : 'network error',
                      aborted: Boolean(info.aborted),
                    });
                  }
                } catch (error) { note(error); }
              });
            }
          } catch (error) { note(error); }
          return originalSend.apply(this, arguments);
        };
        proto.open = wrappedOpen;
        proto.send = wrappedSend;
        h.ownsXhr = function () { return proto.open === wrappedOpen && proto.send === wrappedSend; };
        h.restoreXhr = function () {
          proto.open = originalOpen;
          proto.send = originalSend;
        };
      }
    } catch (error) { note(error); }
  }

  // All or nothing: when every wrapper is still the outermost one, put the
  // originals back and forget the hooks. If the page wrapped any of them after
  // us, unwrapping would drop its wrapper, so ours all stay as inert
  // pass-throughs (active=false) and the next start() simply re-arms them.
  function releaseHooks() {
    if (!hooks) return;
    hooks.active = false;
    var h = hooks;
    try {
      var ours = (!h.ownsConsole || h.ownsConsole()) && (!h.ownsFetch || h.ownsFetch()) && (!h.ownsXhr || h.ownsXhr());
      if (!ours) return;
      if (h.restoreConsole) h.restoreConsole();
      if (h.restoreFetch) h.restoreFetch();
      if (h.restoreXhr) h.restoreXhr();
      hooks = null;
    } catch (error) {
      note(error);
    }
  }

  // ---- lifecycle -----------------------------------------------------------

  function onVisibility() {
    if (!S || !S.active) return;
    try {
      push({ t: elapsed(), kind: 'visibility', state: root.document.hidden ? 'hidden' : 'visible' });
      S.lastFrameEpoch = 0;
      sample();
    } catch (error) { note(error); }
  }
  function storageKey() { return KEY_PREFIX + S.id; }
  function onPageHide(event) {
    if (!S || !S.active || !S.opts.persist) return;
    if (event && event.persisted) S.bfcached = true;
    try {
      root.sessionStorage.setItem(storageKey(), JSON.stringify(serialize()));
    } catch (error) {
      note(error);
    }
  }
  function onPageShow(event) {
    if (!S || !event || !event.persisted) return;
    try { root.sessionStorage.removeItem(storageKey()); } catch (error) { /* storage blocked */ }
    push({ t: elapsed(), kind: 'note', message: 'page restored from the back/forward cache' });
  }

  function serialize() {
    return {
      v: VERSION, id: S.id, startEpoch: S.startEpoch, opts: S.opts, timeline: S.timeline, stats: S.stats,
      last: S.last, frame: S.frame, initialMounts: S.initialMounts, shiftSources: S.shiftSources,
    };
  }

  function normalizeOptions(raw) {
    var input = raw && typeof raw === 'object' ? raw : {};
    var opts = {};
    for (var key in DEFAULTS) opts[key] = DEFAULTS[key];
    if (typeof input.maxEntries === 'number' && input.maxEntries > 0) opts.maxEntries = Math.min(50000, Math.floor(input.maxEntries));
    if (typeof input.maxDurationMs === 'number' && input.maxDurationMs > 0) opts.maxDurationMs = Math.min(3600000, input.maxDurationMs);
    if (input.persist === false) opts.persist = false;
    if (input.console === false) opts.console = false;
    if (input.network === false) opts.network = false;
    opts.fields = [];
    if (Array.isArray(input.fields)) {
      for (var i = 0; i < input.fields.length; i += 1) {
        var field = String(input.fields[i]).replace(/^data-ui-/, '');
        if (/^[a-z0-9][a-z0-9-]*$/.test(field) && !RESERVED[field]) opts.fields.push(field);
        else throw new Error('invalid field name: ' + input.fields[i]);
      }
    }
    opts.selectorList = [];
    var selectors = input.selectors;
    if (Array.isArray(selectors)) {
      for (var s = 0; s < selectors.length; s += 1) {
        var text = String(selectors[s]);
        var eq = text.indexOf('=');
        if (eq < 1) throw new Error('selector must be "name=css": ' + text);
        opts.selectorList.push([text.slice(0, eq).trim(), text.slice(eq + 1).trim()]);
      }
    } else if (selectors && typeof selectors === 'object') {
      for (var name in selectors) opts.selectorList.push([name, String(selectors[name])]);
    }
    for (var p = 0; p < opts.selectorList.length; p += 1) {
      var pair = opts.selectorList[p];
      if (!/^[A-Za-z0-9_-]+$/.test(pair[0]) || pair[0] === 'path') throw new Error('invalid selector name: ' + pair[0]);
      root.document.querySelectorAll(pair[1]); // throws on an invalid selector
    }
    var cyclic = Array.isArray(input.cyclic) ? input.cyclic.map(String) : ANALYZER.DEFAULT_CYCLIC_FIELDS.slice();
    if (!Array.isArray(input.cyclic)) for (var c = 0; c < opts.selectorList.length; c += 1) cyclic.push(opts.selectorList[c][0]);
    opts.cyclic = cyclic;
    return opts;
  }

  function freshStats() {
    return {
      frames: 0, stateChanges: 0, maxFrameGapMs: 0, longFrames: 0,
      layoutShift: { total: 0, inputTotal: 0, count: 0, top: [] },
      mounts: {}, transientMounts: 0, errors: 0, failedRequests: 0, duplicateRows: 0,
      segments: 1, lostSegments: 0, truncated: false, dropped: 0, internalErrors: 0,
      sampleMsTotal: 0, sampleMsMax: 0,
    };
  }

  function activate() {
    S.active = true;
    S.mounted = typeof WeakSet === 'function' ? new WeakSet() : { has: function () { return false; }, add: function () {}, delete: function () {} };
    S.rowsDirty = true;
    S.duplicates = S.duplicates || {};
    S.lastFrameEpoch = 0;
    installHooks();
    try {
      S.observer = new MutationObserver(onMutations);
      S.observer.observe(root.document, { childList: true, subtree: true });
    } catch (error) { note(error); }
    try {
      if (typeof PerformanceObserver === 'function') {
        S.shifts = new PerformanceObserver(onShifts);
        S.shifts.observe({ type: 'layout-shift', buffered: false });
      }
    } catch (error) { S.shifts = null; /* layout-shift is Chromium-only */ }
    root.addEventListener('error', onError, true);
    root.addEventListener('unhandledrejection', onRejection, true);
    root.document.addEventListener('visibilitychange', onVisibility, true);
    root.addEventListener('pagehide', onPageHide, true);
    root.addEventListener('pageshow', onPageShow, true);
    S.raf = root.requestAnimationFrame(tick);
  }

  function halt(reason) {
    if (!S || !S.active) return;
    S.active = false;
    try { root.cancelAnimationFrame(S.raf); } catch (error) { /* gone */ }
    try { if (S.observer) { onMutations(S.observer.takeRecords()); S.observer.disconnect(); } } catch (error) { note(error); }
    try { if (S.shifts) { onShifts({ getEntries: function () { return S.shifts.takeRecords ? S.shifts.takeRecords() : []; } }); S.shifts.disconnect(); } } catch (error) { note(error); }
    root.removeEventListener('error', onError, true);
    root.removeEventListener('unhandledrejection', onRejection, true);
    root.document.removeEventListener('visibilitychange', onVisibility, true);
    root.removeEventListener('pagehide', onPageHide, true);
    root.removeEventListener('pageshow', onPageShow, true);
    releaseHooks();
    S.endEpoch = now();
    if (reason !== 'stopped') S.timeline.push({ t: Math.round(S.endEpoch - S.startEpoch), kind: 'note', message: 'recording stopped: ' + reason });
  }

  function build() {
    var timeline = S.timeline.slice();
    // Layout shifts carry their own frame time; keep everything in time order.
    var indexed = timeline.map(function (entry, i) { return { entry: entry, i: i }; });
    indexed.sort(function (a, b) { return (a.entry.t - b.entry.t) || (a.i - b.i); });
    timeline = indexed.map(function (item) { return item.entry; });
    var ls = S.stats.layoutShift;
    ls.total = Number(ls.total.toFixed(4));
    ls.inputTotal = Number(ls.inputTotal.toFixed(4));
    ls.top = Object.keys(S.shiftSources).map(function (source) {
      return { source: source, value: Number(S.shiftSources[source].toFixed(4)) };
    }).sort(function (a, b) { return b.value - a.value; }).slice(0, 5);
    var stats = JSON.parse(JSON.stringify(S.stats));
    stats.sampleMsAvg = stats.frames ? Number((stats.sampleMsTotal / stats.frames).toFixed(3)) : 0;
    stats.sampleMsTotal = Number(stats.sampleMsTotal.toFixed(2));
    stats.sampleMsMax = Number(stats.sampleMsMax.toFixed(3));
    if (S.firstInternalError) stats.firstInternalError = S.firstInternalError;
    var end = S.endEpoch || now();
    var recording = {
      version: VERSION,
      id: S.id,
      startedAt: Math.round(S.startEpoch),
      durationMs: Math.max(0, Math.round(end - S.startEpoch)),
      initial: { mounts: S.initialMounts },
      timeline: timeline,
      stats: stats,
      options: {
        fields: S.opts.fields.slice(),
        selectors: S.opts.selectorList.map(function (pair) { return pair[0] + '=' + pair[1]; }),
        cyclic: S.opts.cyclic.slice(),
      },
      recording: Boolean(S.active),
    };
    try {
      recording.anomalies = ANALYZER.analyzeRecording(recording);
      recording.text = ANALYZER.formatReport(recording, { anomalies: recording.anomalies });
    } catch (error) {
      recording.anomalies = [];
      recording.analysisError = clip(error && error.message ? error.message : error, 200);
      recording.text = 'analysis failed: ' + recording.analysisError;
    }
    return recording;
  }

  function newId() {
    return 'fr-' + Math.round(now()).toString(36) + '-' + Math.floor(Math.random() * 1e9).toString(36);
  }

  function start(raw) {
    try {
      var input = raw && typeof raw === 'object' ? raw : {};
      if (S && S.active) {
        if (!input.restart) return { ok: false, error: 'already recording', id: S.id };
        halt('restarted');
      }
      var opts = normalizeOptions(input);
      S = {
        id: typeof input.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(input.id) ? input.id : newId(),
        startEpoch: now(), opts: opts, timeline: [], stats: freshStats(), last: {}, frame: 0,
        shiftSources: {}, duplicates: {},
      };
      activate();
      S.initialMounts = baselineMounts();
      sample();
      return { ok: true, id: S.id, startedAt: Math.round(S.startEpoch), fields: Object.keys(S.last) };
    } catch (error) {
      if (S && S.active) halt('failed to start');
      S = null;
      return { ok: false, error: clip(error && error.message ? error.message : error, 200) };
    }
  }

  function resume(config) {
    try {
      if (root.top !== root) return { ok: false, error: 'not the top frame' };
      if (!config || typeof config.id !== 'string') return { ok: false, error: 'missing id' };
      if (S && S.active) return { ok: S.id === config.id, id: S.id };
      var saved = null;
      try {
        var text = root.sessionStorage.getItem(KEY_PREFIX + config.id);
        if (text) {
          root.sessionStorage.removeItem(KEY_PREFIX + config.id);
          saved = JSON.parse(text);
        }
      } catch (error) { saved = null; }
      if (saved && saved.id === config.id && Array.isArray(saved.timeline)) {
        S = {
          id: saved.id, startEpoch: saved.startEpoch, opts: saved.opts, timeline: saved.timeline,
          stats: saved.stats, last: saved.last || {}, frame: saved.frame || 0,
          initialMounts: saved.initialMounts || {}, shiftSources: saved.shiftSources || {}, duplicates: {},
        };
        S.stats.segments += 1;
        activate();
        push({ t: elapsed(), kind: 'navigation', type: 'load', path: cleanPath() }, true);
      } else {
        var opts = normalizeOptions(config.opts || {});
        S = {
          id: config.id, startEpoch: typeof config.startEpoch === 'number' ? config.startEpoch : now(), opts: opts,
          timeline: [], stats: freshStats(), last: {}, frame: 0, initialMounts: {}, shiftSources: {}, duplicates: {},
        };
        S.stats.segments += 1;
        S.stats.lostSegments += 1;
        activate();
        push({ t: elapsed(), kind: 'navigation', type: 'load', path: cleanPath(), lost: true }, true);
      }
      // From an init script the document is still empty; a manual resume on a
      // loaded page records what is already there as mounted at the load.
      var present = root.document.querySelectorAll('[data-ui-mount]');
      for (var i = 0; i < present.length; i += 1) {
        if (S.mounted.has(present[i])) continue;
        S.mounted.add(present[i]);
        var name = cleanValue(present[i].getAttribute('data-ui-mount'));
        countMount(name, 1);
        push({ t: elapsed(), kind: 'mount', name: name, op: 'mount' });
      }
      return { ok: true, id: S.id, resumed: Boolean(saved) };
    } catch (error) {
      return { ok: false, error: clip(error && error.message ? error.message : error, 200) };
    }
  }

  function mark(label) {
    try {
      if (!S || !S.active) return { ok: false, error: 'not recording' };
      sample();
      var index = push({ t: elapsed(), kind: 'mark', label: clip(label === undefined ? '' : label, 200) }, true);
      return { ok: index >= 0, index: index };
    } catch (error) {
      note(error);
      return { ok: false, error: 'mark failed' };
    }
  }

  function stop() {
    try {
      if (!S) return lastResult ? lastResult : { ok: false, error: 'not recording' };
      if (S.active) {
        try { sample(); } catch (error) { note(error); }
        halt('stopped');
      }
      var result = build();
      result.ok = true;
      lastResult = result;
      S = null;
      return result;
    } catch (error) {
      S = null;
      return { ok: false, error: clip(error && error.message ? error.message : error, 200) };
    }
  }

  function snapshot() {
    try {
      if (!S) return lastResult ? lastResult : { ok: false, error: 'not recording' };
      var result = build();
      result.ok = true;
      return result;
    } catch (error) {
      return { ok: false, error: clip(error && error.message ? error.message : error, 200) };
    }
  }

  function peek() {
    return { recording: Boolean(S && S.active), id: S ? S.id : null, entries: S ? S.timeline.length : 0, t: elapsed(), frames: S ? S.stats.frames : 0 };
  }

  function signature(raw) {
    try { return readSignature(normalizeOptions(raw || (S ? { fields: S.opts.fields } : {}))); } catch (error) { return { error: String(error && error.message) }; }
  }

  try {
    if (existing && existing.peek && existing.peek().recording) existing.stop();
  } catch (error) { /* an older recorder that cannot stop */ }

  var api = {
    version: VERSION,
    start: start,
    mark: mark,
    stop: stop,
    peek: peek,
    snapshot: snapshot,
    signature: signature,
    resume: resume,
    lastResult: function () { return lastResult; },
    analyze: function (recording, options) { return ANALYZER.analyzeRecording(recording, options); },
    format: function (recording, options) { return ANALYZER.formatReport(recording, options); },
  };
  try {
    Object.defineProperty(root, '__flightRecorder', { value: api, configurable: true, writable: true, enumerable: false });
  } catch (error) {
    root.__flightRecorder = api;
  }
})();
