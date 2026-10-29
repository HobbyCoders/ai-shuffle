// Node-side helpers for the UI flight recorder, shared by the browser_record
// tool (src/tools/record.ts) and the Playwright journey helper
// (frontend/tests/flight-recorder/record.mjs). No dependencies: callers pass a
// Playwright CDPSession-like object ({ send, on, off }).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const RECORDER_PATH = fileURLToPath(new URL('./flight-recorder.js', import.meta.url));

let cachedSource = null;
/** The injectable recorder script (an IIFE that installs window.__flightRecorder). */
export function recorderSource() {
  if (cachedSource === null) cachedSource = readFileSync(RECORDER_PATH, 'utf8');
  return cachedSource;
}

/**
 * Init script for full page loads during a recording: installs the recorder
 * and resumes recording `id` from the handoff the previous document left in
 * sessionStorage. Runs in every frame; the recorder ignores all but the top.
 */
export function resumeScript(config) {
  const payload = JSON.stringify({ id: config.id, startEpoch: config.startEpoch, opts: config.opts || {} })
    .replace(/</g, '\\u003c');
  return `${recorderSource()}\n;(function(){try{var r=window.__flightRecorder;if(r&&r.resume)r.resume(${payload});}catch(e){}})();\n`;
}

/**
 * Network profiles for Network.emulateNetworkConditions. Throughput is bytes
 * per second; latency is added round-trip milliseconds.
 */
export const NETWORK_PROFILES = {
  'slow-3g': { latency: 2000, downloadThroughput: 50_000, uploadThroughput: 50_000 },
  'fast-3g': { latency: 563, downloadThroughput: 180_000, uploadThroughput: 84_375 },
  '4g': { latency: 150, downloadThroughput: 1_125_000, uploadThroughput: 375_000 },
};
export const NETWORK_PROFILE_NAMES = Object.keys(NETWORK_PROFILES);
export const MAX_CPU_THROTTLE = 20;

/** Apply CPU and/or network throttling on a CDP session. Returns what was applied. */
export async function applyThrottle(cdp, { cpu, network } = {}) {
  const applied = {};
  if (typeof cpu === 'number' && cpu > 1) {
    const rate = Math.min(MAX_CPU_THROTTLE, cpu);
    await cdp.send('Emulation.setCPUThrottlingRate', { rate });
    applied.cpu = rate;
  }
  if (network && network !== 'none') {
    const profile = NETWORK_PROFILES[network];
    if (!profile) throw new Error(`unknown network profile: ${network} (use ${NETWORK_PROFILE_NAMES.join(', ')})`);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', { offline: false, ...profile });
    applied.network = network;
  }
  return applied;
}

/** Undo {@link applyThrottle}. Never throws: cleanup must always finish. */
export async function clearThrottle(cdp, applied = { cpu: true, network: true }) {
  const errors = [];
  if (applied.cpu) {
    try { await cdp.send('Emulation.setCPUThrottlingRate', { rate: 1 }); } catch (error) { errors.push(error); }
  }
  if (applied.network) {
    try {
      await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      await cdp.send('Network.disable');
    } catch (error) { errors.push(error); }
  }
  return errors;
}

/**
 * Collect screencast frames (compositor output with swap timestamps) so
 * keyframes can be picked after the fact for the moments the signature
 * changed. Frames are kept under a byte budget; when it is exceeded every
 * other frame is dropped, so coverage thins evenly instead of losing the start.
 */
export async function startScreencast(cdp, options = {}) {
  const maxChars = options.maxChars ?? 64 * 1024 * 1024;
  const frames = [];
  let chars = 0;
  let decimations = 0;
  let received = 0;
  let stopped = false;
  const onFrame = (event) => {
    received += 1;
    if (!stopped) {
      cdp.send('Page.screencastFrameAck', { sessionId: event.sessionId }).catch(() => {});
    }
    const ts = event.metadata && typeof event.metadata.timestamp === 'number' ? event.metadata.timestamp * 1000 : Date.now();
    frames.push({ ts, data: event.data, width: event.metadata?.deviceWidth, height: event.metadata?.deviceHeight });
    chars += event.data.length;
    if (chars > maxChars && frames.length > 4) {
      decimations += 1;
      const kept = frames.filter((_, i) => i % 2 === 0 || i === frames.length - 1);
      frames.length = 0;
      frames.push(...kept);
      chars = kept.reduce((sum, frame) => sum + frame.data.length, 0);
    }
  };
  cdp.on('Page.screencastFrame', onFrame);
  await cdp.send('Page.enable');
  await cdp.send('Page.startScreencast', {
    format: 'jpeg',
    quality: options.quality ?? 70,
    maxWidth: options.maxWidth ?? 960,
    maxHeight: options.maxHeight ?? 960,
    everyNthFrame: 1,
  });
  return {
    frames,
    stats: () => ({ received, kept: frames.length, decimations }),
    async stop() {
      if (stopped) return frames;
      stopped = true;
      try { await cdp.send('Page.stopScreencast'); } catch { /* target gone */ }
      cdp.off('Page.screencastFrame', onFrame);
      return frames;
    },
  };
}

function captionFor(recording, index, anomalies) {
  const entry = recording.timeline[index];
  const changes = entry && entry.changes ? Object.entries(entry.changes) : [];
  const fields = changes.map(([field, value]) => `${field}=${value === null ? '(gone)' : value}`).join(' ');
  const flags = anomalies
    .filter((anomaly) => (anomaly.indices || []).includes(index))
    .map((anomaly) => anomaly.field ? `${anomaly.kind}(${anomaly.field})` : anomaly.kind);
  return { t: entry ? entry.t : 0, fields, flags };
}

/**
 * Pick at most `max` screencast frames, one per signature change: the first
 * frame swapped at or after the change and before the next one. The
 * screencast only sends a frame when pixels change, so a state with no frame
 * in its window that did not repaint is shown by the last earlier frame
 * (`repaint: false`). States an anomaly points at are kept first; when none
 * of its frames can be placed it gets the nearest later frame, marked
 * approximate.
 */
export function pickKeyframes(recording, frames, options = {}) {
  const max = options.max ?? 12;
  const anomalies = recording.anomalies || [];
  const timeline = recording.timeline || [];
  const states = [];
  timeline.forEach((entry, index) => { if (entry && entry.kind === 'state') states.push(index); });
  if (!states.length || !frames.length) return [];
  const sorted = frames.slice().sort((a, b) => a.ts - b.ts);
  const flagged = new Set(anomalies.flatMap((anomaly) => anomaly.indices || []));
  const candidates = [];
  let previous = null;
  let cursor = 0;
  states.forEach((index, n) => {
    const from = recording.startedAt + timeline[index].t;
    const next = n + 1 < states.length ? recording.startedAt + timeline[states[n + 1]].t : Infinity;
    // A swap trails the rAF that read its state by up to a frame, so a state
    // of one or two frames may own a frame swapped just after the next change.
    // Frames are handed out in order: one frame never shows two states.
    const end = next - from <= 34 ? next + 12 : next;
    while (cursor < sorted.length && sorted[cursor].ts < from - 4) cursor += 1;
    let frame = cursor < sorted.length && sorted[cursor].ts < end ? sorted[cursor] : undefined;
    if (frame) cursor += 1;
    let approximate = false;
    let repaint = true;
    if (!frame) {
      const before = sorted.filter((f) => f.ts < from - 4);
      frame = before[before.length - 1];
      repaint = false;
    }
    if (!frame && flagged.has(index)) {
      frame = sorted.find((f) => f.ts >= from - 4) || sorted[sorted.length - 1];
      approximate = true;
    }
    if (!frame) return;
    // The same picture twice says nothing unless an anomaly points here.
    if (frame === previous && !flagged.has(index)) return;
    previous = frame;
    candidates.push({ index, frame, approximate, repaint, flagged: flagged.has(index) });
  });
  const chosen = new Map();
  for (const item of candidates) if (item.flagged && chosen.size < max) chosen.set(item.index, item);
  const rest = candidates.filter((item) => !chosen.has(item.index));
  if (rest.length && chosen.size < max) {
    const room = max - chosen.size;
    if (rest.length <= room) rest.forEach((item) => chosen.set(item.index, item));
    else {
      for (let k = 0; k < room; k += 1) {
        const item = rest[Math.round((k * (rest.length - 1)) / Math.max(1, room - 1))];
        chosen.set(item.index, item);
      }
    }
  }
  return [...chosen.values()]
    .sort((a, b) => a.index - b.index)
    .map((item) => ({
      index: item.index,
      ...captionFor(recording, item.index, anomalies),
      approximate: item.approximate,
      repaint: item.repaint,
      data: item.frame.data,
      width: item.frame.width,
      height: item.frame.height,
      frame_ms: Math.round(item.frame.ts - recording.startedAt),
    }));
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** A self-contained HTML grid of keyframes (data: URLs), for Playwright to screenshot. */
export function contactSheetHtml(keyframes, options = {}) {
  const title = options.title || 'flight recorder keyframes';
  const cells = keyframes.map((frame) => `
    <figure class="${frame.flags && frame.flags.length ? 'flagged' : ''}">
      <img src="data:image/jpeg;base64,${frame.data}" alt="">
      <figcaption>
        <b>#${frame.index} · ${Math.round(frame.t)}ms${frame.approximate ? ' · approx' : frame.repaint === false ? ' · no repaint' : ''}</b>
        ${frame.flags && frame.flags.length ? `<em>${escapeHtml(frame.flags.join(', '))}</em>` : ''}
        <span>${escapeHtml(frame.fields || '')}</span>
      </figcaption>
    </figure>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>
    :root { color-scheme: dark; }
    body { margin: 0; padding: 16px; background: #14161a; color: #e8e6e3; font: 12px/1.35 ui-monospace, Menlo, Consolas, monospace; }
    h1 { margin: 0 0 12px; font-size: 13px; font-weight: 600; letter-spacing: .02em; color: #9aa3ad; }
    .grid { display: grid; grid-template-columns: repeat(${options.columns || 3}, minmax(0, 1fr)); gap: 12px; }
    figure { margin: 0; background: #1d2026; border: 1px solid #2c3038; border-radius: 6px; overflow: hidden; }
    figure.flagged { border-color: #e0803d; box-shadow: 0 0 0 1px #e0803d; }
    img { display: block; width: 100%; height: auto; background: #000; }
    figcaption { padding: 6px 8px 8px; display: grid; gap: 2px; }
    b { color: #f2f0ec; } em { color: #f0a46a; font-style: normal; } span { color: #a7b0ba; word-break: break-word; }
  </style></head><body><h1>${escapeHtml(title)}</h1><div class="grid">${cells}</div></body></html>`;
}

/** Columns that keep a sheet roughly 1280px wide for this frame shape. */
export function sheetColumns(keyframes) {
  const sample = keyframes.find((frame) => frame.width && frame.height);
  const portrait = sample ? sample.height / sample.width > 1.2 : false;
  return Math.max(1, Math.min(keyframes.length, portrait ? 6 : 3));
}

/**
 * Page function: compose keyframes into one JPEG with OffscreenCanvas, inside
 * the page that was recorded (no new tab in a shared browser). Pass to
 * page.evaluate with { frames: [{ data, caption, flagged }], columns }.
 * Returns base64 JPEG, or throws.
 */
export async function composeSheetInPage(input) {
  const decode = (b64) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  };
  const bitmaps = [];
  for (const frame of input.frames) {
    bitmaps.push(await createImageBitmap(new Blob([decode(frame.data)], { type: 'image/jpeg' })));
  }
  const columns = Math.max(1, Math.min(input.columns || 3, bitmaps.length));
  const cellWidth = Math.floor((1280 - 16 * (columns + 1)) / columns);
  const caption = 34;
  const heights = bitmaps.map((bitmap) => Math.round((bitmap.height / bitmap.width) * cellWidth));
  const rows = Math.ceil(bitmaps.length / columns);
  const rowHeights = [];
  for (let r = 0; r < rows; r += 1) rowHeights.push(Math.max(...heights.slice(r * columns, r * columns + columns)) + caption);
  const width = 16 + columns * (cellWidth + 16);
  const height = 16 + rowHeights.reduce((sum, h) => sum + h + 16, 0);
  const canvas = new OffscreenCanvas(width, height);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#14161a';
  ctx.fillRect(0, 0, width, height);
  let y = 16;
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < columns; c += 1) {
      const i = r * columns + c;
      if (i >= bitmaps.length) break;
      const x = 16 + c * (cellWidth + 16);
      const frame = input.frames[i];
      ctx.fillStyle = frame.flagged ? '#e0803d' : '#2c3038';
      ctx.fillRect(x - 2, y - 2, cellWidth + 4, heights[i] + caption + 4);
      ctx.fillStyle = '#1d2026';
      ctx.fillRect(x, y, cellWidth, heights[i] + caption);
      ctx.drawImage(bitmaps[i], x, y, cellWidth, heights[i]);
      ctx.font = '12px ui-monospace, Menlo, Consolas, monospace';
      ctx.fillStyle = frame.flagged ? '#f0a46a' : '#e8e6e3';
      const lines = String(frame.caption).split('\n');
      lines.slice(0, 2).forEach((line, n) => {
        let text = line;
        while (text.length > 4 && ctx.measureText(text).width > cellWidth - 12) text = `${text.slice(0, -2)}…`;
        ctx.fillText(text, x + 6, y + heights[i] + 14 + n * 14);
        ctx.fillStyle = '#a7b0ba';
      });
    }
    y += rowHeights[r] + 16;
  }
  const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

/** Captions for {@link composeSheetInPage}: "#i · 120ms · flags" then the changed fields. */
export function sheetFrames(keyframes) {
  return keyframes.map((frame) => ({
    data: frame.data,
    flagged: Boolean(frame.flags && frame.flags.length),
    caption: `#${frame.index} · ${Math.round(frame.t)}ms${frame.approximate ? ' · approx' : frame.repaint === false ? ' · no repaint' : ''}${frame.flags && frame.flags.length ? ` · ${frame.flags.join(', ')}` : ''}\n${frame.fields || ''}`,
  }));
}
