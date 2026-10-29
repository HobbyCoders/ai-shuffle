/**
 * browser_record — the UI flight recorder (src/recorder/flight-recorder.js).
 * Screenshots cannot catch a state that lasts one frame; this records a small
 * text signature every animation frame and flags what went wrong.
 *
 *   start  inject the recorder into the current tab, optionally throttle CPU
 *          and network, optionally collect screencast frames for keyframes.
 *          An init script re-injects it after full page loads, resuming from
 *          the handoff the old document leaves in sessionStorage; SvelteKit
 *          client routing keeps the same document and needs nothing.
 *   mark   name the next step. Every browser_act, browser_navigate,
 *          browser_eval, browser_upload and browser_resize on the recorded
 *          tab marks itself; a step on another tab is reported at stop as a
 *          tab_changed anomaly instead.
 *   stop   end it: the text timeline, anomalies, stats and (with keyframes)
 *          one contact-sheet image.
 *
 * Everything the recording touches lives on its own CDP session, so detaching
 * it at stop (or when max_duration_ms runs out) clears throttling and the
 * screencast even if a step in between failed.
 */

import type { CDPSession, Page } from "playwright-core";
import { raceWithTimeout, type Session } from "../session.js";
import { capText, errorResult, text, type McpContent, type McpToolResult } from "./result.js";
import {
  applyThrottle,
  clearThrottle,
  composeSheetInPage,
  NETWORK_PROFILE_NAMES,
  pickKeyframes,
  recorderSource,
  resumeScript,
  sheetColumns,
  sheetFrames,
  startScreencast,
  type Recording,
  type Screencast,
  type Throttle,
} from "../recorder/driver.js";

export const RECORD_DEFAULT_DURATION_MS = 120_000;
export const RECORD_MAX_KEYFRAMES = 12;
/** Text timeline cap in one result; the JSON body stays small beside it. */
export const RECORD_TEXT_MAX_CHARS = 40_000;
const PAGE_CALL_TIMEOUT_MS = 10_000;
const MARK_TIMEOUT_MS = 1_500;

export interface RecordInput {
  action: "start" | "mark" | "stop";
  label?: string;
  fields?: string[];
  selectors?: string[];
  cpu_throttle?: number;
  network?: string;
  keyframes?: boolean;
  max_duration_ms?: number;
}

/** Labels of steps that ran in another tab, kept for the tab_changed anomaly. */
const ELSEWHERE_MAX_LABELS = 10;

interface ActiveRecording {
  id: string;
  page: Page;
  /** The recorded tab; a step on any other tab is not marked here. */
  tabId: string | undefined;
  startedAt: number;
  /** Steps that ran while another tab was current. */
  elsewhere: { count: number; firstAt: number; labels: string[] };
  cdp: CDPSession;
  init?: { dispose(): Promise<void> };
  screencast?: Screencast;
  throttle: Throttle;
  maxDurationMs: number;
  timer?: ReturnType<typeof setTimeout>;
  /** Resources released by the max-duration timer; the page still holds the data. */
  expired: boolean;
  released: boolean;
}

const recordings = new WeakMap<Session, ActiveRecording>();

/** Is a recording running for this session? (browser_status, tests) */
export function activeRecording(session: Session): { id: string; expired: boolean } | undefined {
  const active = recordings.get(session);
  return active && !active.page.isClosed() ? { id: active.id, expired: active.expired } : undefined;
}

async function release(active: ActiveRecording): Promise<void> {
  if (active.released) return;
  active.released = true;
  if (active.timer) clearTimeout(active.timer);
  try { await active.screencast?.stop(); } catch { /* target gone */ }
  await clearThrottle(active.cdp, { cpu: active.throttle.cpu, network: active.throttle.network });
  try { await active.init?.dispose(); } catch { /* page closed */ }
  try { await raceWithTimeout(active.cdp.detach(), 3000, "detach timed out"); } catch { /* already detached */ }
}

function body(value: Record<string, unknown>): McpContent {
  return text(JSON.stringify(value));
}

function stripQuery(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}${parsed.pathname}${parsed.search ? "?…" : ""}`;
  } catch {
    return url.split(/[?#]/)[0];
  }
}

/** The mark an agent step leaves in a running recording. Never typed values. */
export function stepLabel(tool: string, input: Record<string, unknown>): string | undefined {
  const short = tool.replace(/^browser_/, "");
  switch (tool) {
    case "browser_act": {
      const target = typeof input.ref === "string" ? input.ref
        : Array.isArray(input.coordinate) ? `@${input.coordinate.join(",")}` : "";
      const key = input.kind === "key" && typeof input.key === "string" ? input.key : "";
      return ["act", String(input.kind ?? ""), target, key].filter(Boolean).join(" ");
    }
    case "browser_navigate":
      return `navigate ${typeof input.url === "string" ? stripQuery(input.url) : ""}`.trim();
    case "browser_eval":
    case "browser_upload":
      return short;
    case "browser_resize":
      return input.preset ? `resize ${String(input.preset)}` : `resize ${String(input.width)}x${String(input.height)}`;
    default:
      return undefined;
  }
}

/**
 * Called before each tool runs (inside the session's exclusive turn): while a
 * recording runs, an acting step on the recorded tab marks itself so the
 * detectors judge each step on its own. A step on another tab (after
 * browser_tabs select or create) is not marked there; stop reports it as a
 * tab_changed anomaly instead. Best effort and bounded.
 */
export async function markStep(session: Session, tool: string, input: Record<string, unknown>): Promise<void> {
  const active = recordings.get(session);
  if (!active || active.expired || active.page.isClosed()) return;
  const label = stepLabel(tool, input);
  if (!label) return;
  if (session.currentTabId() !== active.tabId) {
    const elsewhere = active.elsewhere;
    if (!elsewhere.count) elsewhere.firstAt = Math.max(0, Date.now() - active.startedAt);
    elsewhere.count += 1;
    if (elsewhere.labels.length < ELSEWHERE_MAX_LABELS) elsewhere.labels.push(label);
    return;
  }
  try {
    await raceWithTimeout(active.page.evaluate((value) => {
      const recorder = (window as unknown as { __flightRecorder?: { mark(label: string): unknown } }).__flightRecorder;
      return recorder ? recorder.mark(value) : null;
    }, label), MARK_TIMEOUT_MS, "mark timed out");
  } catch { /* a navigating page: the step still runs */ }
}

async function start(session: Session, input: RecordInput): Promise<McpToolResult> {
  const previous = recordings.get(session);
  if (previous && !previous.page.isClosed()) {
    return errorResult("recording_active: a recording is already running. Call browser_record action=stop first.", {
      outcome: "not_executed", recording_id: previous.id,
    });
  }
  if (previous) {
    recordings.delete(session);
    await release(previous);
  }
  if (input.network !== undefined && input.network !== "none" && !NETWORK_PROFILE_NAMES.includes(input.network)) {
    return errorResult(`invalid_input: network must be one of none, ${NETWORK_PROFILE_NAMES.join(", ")}`, { outcome: "not_executed" });
  }
  const page = await session.ensurePage();
  // Chrome paints (and screencasts) only the foreground tab.
  await session.bringToFront();
  const maxDurationMs = input.max_duration_ms ?? RECORD_DEFAULT_DURATION_MS;
  const options = {
    ...(input.fields?.length ? { fields: input.fields } : {}),
    ...(input.selectors?.length ? { selectors: input.selectors } : {}),
    maxDurationMs,
  };

  const cdp = await raceWithTimeout(page.context().newCDPSession(page), 5000, "CDP session timed out");
  const active: ActiveRecording = {
    id: "", page, tabId: session.currentTabId(), startedAt: Date.now(), elsewhere: { count: 0, firstAt: 0, labels: [] },
    cdp, throttle: {}, maxDurationMs, expired: false, released: false,
  };
  try {
    active.throttle = await applyThrottle(cdp, { cpu: input.cpu_throttle, network: input.network });
    if (input.keyframes) active.screencast = await startScreencast(cdp, { maxChars: 48 * 1024 * 1024 });
    // A string evaluates through CDP, so a page CSP without unsafe-eval cannot block it.
    await raceWithTimeout(page.evaluate(recorderSource()), PAGE_CALL_TIMEOUT_MS, "recorder install timed out");
    const started = await raceWithTimeout(page.evaluate((opts) => {
      const recorder = (window as unknown as { __flightRecorder?: { start(o: unknown): unknown } }).__flightRecorder;
      return recorder ? recorder.start({ ...opts, restart: true }) : { ok: false, error: "the page refused the recorder" };
    }, options), PAGE_CALL_TIMEOUT_MS, "recorder start timed out") as { ok: boolean; id?: string; startedAt?: number; fields?: string[]; error?: string };
    if (!started.ok || !started.id || typeof started.startedAt !== "number") {
      await release(active);
      return errorResult(`invalid_input: ${started.error ?? "recorder did not start"}`, { outcome: "not_executed" });
    }
    active.id = started.id;
    active.startedAt = started.startedAt;
    active.init = await page.addInitScript({
      content: resumeScript({ id: started.id, startEpoch: started.startedAt, opts: options }),
    });
    active.timer = setTimeout(() => {
      active.expired = true;
      void release(active);
    }, maxDurationMs + 2000);
    active.timer.unref?.();
    recordings.set(session, active);
    return {
      content: [body({
        ok: true,
        recording_id: started.id,
        fields: started.fields ?? [],
        ...(active.throttle.cpu ? { cpu_throttle: active.throttle.cpu } : {}),
        ...(active.throttle.network ? { network: active.throttle.network } : {}),
        keyframes: Boolean(active.screencast),
        max_duration_ms: maxDurationMs,
        note: "Recording. Acting tools mark each step; add browser_record action=mark for anything else. Call action=stop for the timeline.",
      })],
    };
  } catch (error) {
    try {
      await raceWithTimeout(page.evaluate(() => {
        const recorder = (window as unknown as { __flightRecorder?: { stop(): unknown } }).__flightRecorder;
        if (recorder) recorder.stop();
      }), 2000, "stop timed out");
    } catch { /* page gone */ }
    await release(active);
    throw error;
  }
}

async function mark(session: Session, input: RecordInput): Promise<McpToolResult> {
  const active = recordings.get(session);
  if (!active || active.page.isClosed()) return errorResult("not_recording: call browser_record action=start first.", { outcome: "not_executed" });
  const result = await raceWithTimeout(active.page.evaluate((label) => {
    const recorder = (window as unknown as { __flightRecorder?: { mark(l: string): unknown } }).__flightRecorder;
    return recorder ? recorder.mark(label) : { ok: false, error: "the recorder is not in this document" };
  }, input.label ?? ""), PAGE_CALL_TIMEOUT_MS, "mark timed out") as { ok: boolean; index?: number; error?: string };
  if (!result.ok) return errorResult(`not_recording: ${result.error ?? "mark failed"}`, { outcome: "not_executed" });
  return { content: [body({
    ok: true, recording_id: active.id, index: result.index,
    ...(session.currentTabId() !== active.tabId ? {
      note: `marked in the recorded tab ${active.tabId ?? "?"}, not the current tab; steps on the current tab are not recorded.`,
    } : {}),
  })] };
}

function compactStats(stats: Record<string, any>): Record<string, unknown> {
  return {
    frames: stats.frames,
    state_changes: stats.stateChanges,
    longest_frame_gap_ms: stats.maxFrameGapMs,
    long_frames: stats.longFrames,
    layout_shift: stats.layoutShift?.total,
    errors: stats.errors,
    failed_requests: stats.failedRequests,
    duplicate_rows: stats.duplicateRows,
    page_loads: Math.max(0, (stats.segments ?? 1) - 1),
    ...(stats.lostSegments ? { lost_segments: stats.lostSegments } : {}),
    ...(stats.handoffDropped ? { handoff_dropped_earliest: stats.handoffDropped } : {}),
    ...(stats.truncated ? { truncated: true, dropped: stats.dropped } : {}),
    recorder_ms_per_frame: stats.sampleMsAvg,
    ...(stats.internalErrors ? { recorder_internal_errors: stats.internalErrors } : {}),
  };
}

async function stop(session: Session): Promise<McpToolResult> {
  const active = recordings.get(session);
  if (!active) return errorResult("not_recording: no recording is running. Start one with browser_record action=start.", { outcome: "not_executed" });
  recordings.delete(session);
  let recording: Recording | null = null;
  let frames: Awaited<ReturnType<Screencast["stop"]>> = [];
  let screencastStats: ReturnType<Screencast["stats"]> | undefined;
  try {
    if (active.page.isClosed()) {
      return errorResult("recording_lost: the recorded tab was closed, so its recording is gone.", { recording_id: active.id });
    }
    recording = await raceWithTimeout(active.page.evaluate(() => {
      const recorder = (window as unknown as { __flightRecorder?: { stop(): unknown } }).__flightRecorder;
      return recorder ? recorder.stop() : null;
    }), PAGE_CALL_TIMEOUT_MS, "recorder stop timed out") as Recording | null;
    if (active.screencast) {
      frames = await active.screencast.stop();
      screencastStats = active.screencast.stats();
    }
  } finally {
    await release(active);
  }
  if (!recording || recording.ok === false || recording.id !== active.id) {
    return errorResult(
      "recording_lost: this document has no recording to return. A full navigation to a page the recorder could not resume on " +
        "(another origin, or storage blocked) ends it. Start a new recording.",
      { recording_id: active.id, ...(recording?.error ? { detail: recording.error } : {}) },
    );
  }

  const content: McpContent[] = [];
  const keyframes = active.screencast ? pickKeyframes(recording, frames, { max: RECORD_MAX_KEYFRAMES }) : [];
  let sheetNote: string | undefined;
  let sheet: string | undefined;
  if (keyframes.length) {
    try {
      sheet = await raceWithTimeout(
        active.page.evaluate(composeSheetInPage, { frames: sheetFrames(keyframes), columns: sheetColumns(keyframes) }),
        PAGE_CALL_TIMEOUT_MS,
        "contact sheet timed out",
      );
    } catch (error) {
      sheetNote = `contact sheet unavailable: ${error instanceof Error ? error.message : String(error)}`;
    }
  } else if (active.screencast) {
    sheetNote = frames.length ? "no keyframe matched a signature change" : "the screencast sent no frames (was the tab in the background?)";
  }
  const anomalies: Array<Record<string, unknown>> = (recording.anomalies ?? []).map(({ kind, message, t, indices, field, name }) => ({
    kind, message, t, indices, ...(field ? { field } : {}), ...(name ? { name } : {}),
  }));
  const elsewhere = active.elsewhere;
  if (elsewhere.count) {
    const more = elsewhere.count - elsewhere.labels.length;
    anomalies.push({
      kind: "tab_changed",
      t: elsewhere.firstAt,
      message: `${elsewhere.count} step${elsewhere.count === 1 ? "" : "s"} ran in another tab, so this recording of tab ` +
        `${active.tabId ?? "?"} does not cover ${elsewhere.count === 1 ? "it" : "them"}: ${elsewhere.labels.join("; ")}` +
        `${more > 0 ? `; and ${more} more` : ""}. Select the recorded tab (browser_tabs op=select) before acting, or stop and start a recording on the other tab.`,
    });
  }
  content.push(body({
    ok: true,
    recording_id: recording.id,
    duration_ms: recording.durationMs,
    anomaly_count: anomalies.length,
    anomalies,
    stats: compactStats(recording.stats ?? {}),
    ...(active.throttle.cpu || active.throttle.network ? { throttle: active.throttle } : {}),
    ...(active.expired ? { note: `max_duration_ms (${active.maxDurationMs}) ran out before stop; the timeline ends there.` } : {}),
    ...(recording.analysisError ? { analysis_error: recording.analysisError } : {}),
    ...(active.screencast ? {
      keyframes: keyframes.map(({ index, t, frame_ms, fields, flags, approximate, repaint }) => ({ index, t, frame_ms, fields, flags, approximate, repaint })),
      ...(screencastStats ? { screencast_frames: screencastStats.received } : {}),
      ...(sheetNote ? { keyframes_note: sheetNote } : {}),
    } : {}),
  }));
  const { text: timeline, truncated } = capText(recording.text ?? "", RECORD_TEXT_MAX_CHARS);
  content.push(text(truncated ? `${timeline}\n… (timeline cut at ${RECORD_TEXT_MAX_CHARS} chars)` : timeline));
  if (sheet) content.push({ type: "image", data: sheet, mimeType: "image/jpeg" });
  return { content };
}

export async function runRecord(session: Session, input: RecordInput): Promise<McpToolResult> {
  switch (input.action) {
    case "start": return start(session, input);
    case "mark": return mark(session, input);
    case "stop": return stop(session);
    default: return errorResult("invalid_input: action must be start, mark or stop", { outcome: "not_executed" });
  }
}
