/** Flight recorder analyzer and formatter (src/recorder/analyze.js) on synthetic timelines. */
import { describe, it, expect } from "vitest";
import {
  analyzeRecording,
  detectDuplicateRows,
  detectErrors,
  detectFailedRequests,
  detectLayoutShift,
  detectRecordingGaps,
  detectRemounts,
  detectReversals,
  foldStates,
  formatAnomalies,
  formatReport,
  formatTimeline,
  DEFAULT_CYCLIC_FIELDS,
} from "../src/recorder/analyze.js";

type Entry = Record<string, unknown> & { t: number; kind: string };
const state = (t: number, frame: number, changes: Record<string, string | null>): Entry => ({ t, kind: "state", frame, changes });
const mark = (t: number, label: string): Entry => ({ t, kind: "mark", label });
const mount = (t: number, name: string, op: "mount" | "unmount" = "mount"): Entry => ({ t, kind: "mount", name, op });

function recording(timeline: Entry[], extra: Record<string, unknown> = {}) {
  const last = timeline.length ? timeline[timeline.length - 1].t : 0;
  return {
    version: 1, id: "fr-test", startedAt: 1_000_000, durationMs: last,
    initial: { mounts: {} }, timeline, stats: { frames: timeline.length, stateChanges: 0 },
    options: { cyclic: [...DEFAULT_CYCLIC_FIELDS] },
    ...extra,
  };
}

const NEW_CHAT = { path: "/", surface: "new", session: "none", composer: "centre", messages: "0", loading: "false", streaming: "false" };

describe("reversal", () => {
  it("flags a one-frame flicker A -> B -> A on one field", () => {
    const rec = recording([
      state(0, 0, NEW_CHAT),
      mark(10, "act click e4"),
      state(16, 1, { surface: "active", composer: "bottom" }),
      state(33, 2, { surface: "new", composer: "centre" }),
    ]);
    const anomalies = detectReversals(rec);
    expect(anomalies.map((a) => a.field).sort()).toEqual(["composer", "surface"]);
    const surface = anomalies.find((a) => a.field === "surface")!;
    expect(surface).toMatchObject({
      kind: "reversal", path: ["new", "active", "new"], frames: 1, flicker: true, away_ms: 17, count: 1, indices: [2, 3], t: [16, 33],
    });
    expect(surface.message).toBe("surface went new → active → new within one step: back to new after 17ms (1 frame) — a flicker");
  });

  it("flags a long excursion A -> B -> C -> A and names every value on the way", () => {
    const rec = recording([
      state(0, 0, { surface: "active" }),
      state(100, 6, { surface: "new" }),
      state(300, 18, { surface: "loading" }),
      state(500, 30, { surface: "active" }),
    ]);
    const [anomaly] = detectReversals(rec);
    expect(anomaly.path).toEqual(["active", "new", "loading", "active"]);
    expect(anomaly.flicker).toBe(false);
    expect(anomaly.away_ms).toBe(400);
    expect(anomaly.indices).toEqual([1, 2, 3]);
  });

  it("never flags normal monotonic transitions", () => {
    const rec = recording([
      state(0, 0, NEW_CHAT),
      mark(5, "send"),
      state(16, 1, { surface: "loading", session: "s:k2x9", composer: "bottom", loading: "true" }),
      state(200, 12, { surface: "active", messages: "1", loading: "false", streaming: "true" }),
      state(400, 24, { messages: "2" }),
      state(600, 36, { messages: "3", streaming: "false" }),
      state(650, 39, { path: "/chat/abc" }),
    ]);
    expect(analyzeRecording(rec)).toEqual([]);
  });

  it("does not pair values across different fields", () => {
    // x goes a -> b while y goes b -> a: each field only moved forward.
    const rec = recording([
      state(0, 0, { x: "a", y: "b" }),
      state(16, 1, { x: "b", y: "a" }),
      state(33, 2, { x: "c", y: "c" }),
    ]);
    expect(detectReversals(rec)).toEqual([]);
  });

  it("does not flag a return that a mark separates (a deliberate step back)", () => {
    const rec = recording([
      state(0, 0, { surface: "active" }),
      mark(10, "act click New chat"),
      state(16, 1, { surface: "new" }),
      mark(500, "act click previous chat"),
      state(520, 31, { surface: "loading" }),
      state(700, 42, { surface: "active" }),
    ]);
    expect(detectReversals(rec)).toEqual([]);
  });

  it("still flags a reversal that starts from the value carried into the step", () => {
    const rec = recording([
      state(0, 0, { surface: "new" }),
      mark(10, "act click create"),
      state(16, 1, { surface: "active" }),
      state(33, 2, { surface: "new" }),
      state(50, 3, { surface: "active" }),
    ]);
    const anomalies = detectReversals(rec);
    expect(anomalies).toHaveLength(1);
    expect(anomalies[0]).toMatchObject({ field: "surface", path: ["new", "active", "new"], step: 1, count: 2, indices: [2, 3, 4] });
  });

  it("counts a repeated flicker once per field per step", () => {
    const rec = recording([
      state(0, 0, { composer: "centre" }),
      state(16, 1, { composer: "bottom" }),
      state(33, 2, { composer: "centre" }),
      state(50, 3, { composer: "bottom" }),
      state(66, 4, { composer: "centre" }),
    ]);
    const anomalies = detectReversals(rec);
    expect(anomalies).toHaveLength(1);
    expect(anomalies[0].count).toBe(3);
    expect(anomalies[0].message).toContain("happened 3 times in this step");
    expect(anomalies[0].t).toEqual([16, 66]);
  });

  it("exempts cyclic fields (a load starts and ends) unless told otherwise", () => {
    const rec = recording([
      state(0, 0, { loading: "false", streaming: "false" }),
      state(16, 1, { loading: "true", streaming: "true" }),
      state(400, 24, { loading: "false", streaming: "false" }),
    ]);
    expect(detectReversals(rec)).toEqual([]);
    expect(detectReversals(rec, { cyclic: [] }).map((a) => a.field).sort()).toEqual(["loading", "streaming"]);
  });

  it("uses the cyclic list the recording was made with", () => {
    const rec = recording([
      state(0, 0, { spinner: "0" }),
      state(16, 1, { spinner: "1" }),
      state(33, 2, { spinner: "0" }),
    ], { options: { cyclic: ["spinner"] } });
    expect(detectReversals(rec)).toEqual([]);
  });

  it("treats a removed field as a value: a chat flashing on another page is a reversal", () => {
    const rec = recording([
      state(0, 0, { path: "/settings" }),
      state(16, 1, { surface: "new" }),
      state(33, 2, { surface: null }),
    ]);
    const [anomaly] = detectReversals(rec);
    expect(anomaly.path).toEqual(["(gone)", "new", "(gone)"]);
  });

  it("does not flag a field that just appears", () => {
    const rec = recording([state(0, 0, { path: "/" }), state(16, 1, { surface: "new" }), state(33, 2, { surface: "active" })]);
    expect(detectReversals(rec)).toEqual([]);
  });

  it("flags the path going back without a step", () => {
    const rec = recording([
      state(0, 0, { path: "/chat/a" }),
      state(16, 1, { path: "/new" }),
      state(33, 2, { path: "/chat/a" }),
    ]);
    expect(detectReversals(rec)[0]).toMatchObject({ field: "path", path: ["/chat/a", "/new", "/chat/a"] });
  });
});

describe("reversal across a scope change (path, session)", () => {
  it("does not flag a chat switch whose loading state takes a normal time", () => {
    const rec = recording([
      state(0, 0, { surface: "active", session: "s:aaaaaa", messages: "12" }),
      mark(10, "act click chat B"),
      state(20, 1, { surface: "loading", session: "s:bbbbbb", messages: "0" }),
      state(420, 25, { surface: "active", messages: "6" }),
    ]);
    expect(analyzeRecording(rec)).toEqual([]);
  });

  it("flags a flash across a scope change and says what changed", () => {
    const rec = recording([
      state(0, 0, { surface: "active", session: "s:aaaaaa" }),
      mark(10, "act click chat B"),
      state(20, 1, { surface: "loading", session: "s:bbbbbb" }),
      state(85, 5, { surface: "active" }),
    ]);
    const [anomaly] = detectReversals(rec);
    expect(anomaly).toMatchObject({ field: "surface", scope_changed: ["session"], away_ms: 65 });
    expect(anomaly.message).toBe("surface went active → loading → active within one step: back to active after 65ms (4 frames) while session changed s:aaaaaa → s:bbbbbb");
  });

  it("flags the worktree flicker however long it lasts: the session comes back too", () => {
    const rec = recording([
      state(0, 0, { surface: "new", session: "none", composer: "centre" }),
      mark(5, "act click create worktree session"),
      state(300, 18, { surface: "active", session: "s:k2x9qa", composer: "bottom" }),
      state(900, 54, { surface: "new", session: "none", composer: "centre" }),
      state(1500, 90, { surface: "active", session: "s:k2x9qa", composer: "bottom" }),
    ]);
    const fields = detectReversals(rec).map((a) => a.field).sort();
    expect(fields).toEqual(["composer", "session", "surface"]);
  });

  it("honours custom scope fields and flash threshold", () => {
    const rec = recording([
      state(0, 0, { tab: "a", panel: "open" }),
      state(16, 1, { tab: "b", panel: "closed" }),
      state(400, 24, { panel: "open" }),
    ]);
    expect(detectReversals(rec)).toHaveLength(1);
    expect(detectReversals(rec, { scope: ["tab"] })).toEqual([]);
    expect(detectReversals(rec, { scope: ["tab"], thresholds: { scopeFlashMs: 500 } })).toHaveLength(1);
  });
});

describe("remount", () => {
  it("flags an unmount followed by a mount in one step", () => {
    const rec = recording([
      state(0, 0, { surface: "active" }),
      mark(5, "act click e2"),
      mount(20, "MessageArea", "unmount"),
      mount(36, "MessageArea"),
    ]);
    const [anomaly] = detectRemounts(rec);
    expect(anomaly).toMatchObject({ kind: "remount", name: "MessageArea", indices: [2, 3], t: [20, 36], step: 1 });
    expect(anomaly.message).toBe("MessageArea unmounted at 20ms and mounted again at 36ms (16ms gap) within one step");
  });

  it("does not flag a component that mounts once or leaves once", () => {
    const rec = recording([
      state(0, 0, { surface: "new" }),
      mount(10, "NewSessionHero", "unmount"),
      mount(10, "MessageArea"),
    ]);
    expect(detectRemounts(rec)).toEqual([]);
  });

  it("does not flag a remount that a mark separates", () => {
    const rec = recording([
      mount(10, "NewSessionHero", "unmount"),
      mark(100, "act click New chat"),
      mount(120, "NewSessionHero"),
    ]);
    expect(detectRemounts(rec)).toEqual([]);
  });

  it("flags two mounts without an unmount (a second instance)", () => {
    const rec = recording([mount(10, "ChatView"), mount(26, "ChatView")]);
    expect(detectRemounts(rec)[0]).toMatchObject({ name: "ChatView", message: "ChatView mounted 2 times within one step without unmounting" });
  });

  it("starts afresh after a full page load", () => {
    const rec = recording([
      mount(10, "ChatView", "unmount"),
      { t: 50, kind: "navigation", type: "load", path: "/" },
      mount(60, "ChatView"),
    ]);
    expect(detectRemounts(rec)).toEqual([]);
  });

  it("absorbs repeats into one anomaly per name per step", () => {
    const rec = recording([
      mount(10, "ChatInput", "unmount"), mount(11, "ChatInput"),
      mount(30, "ChatInput", "unmount"), mount(31, "ChatInput"),
    ]);
    const anomalies = detectRemounts(rec);
    expect(anomalies).toHaveLength(1);
    expect(anomalies[0]).toMatchObject({ count: 2, indices: [0, 1, 2, 3], message: "ChatInput was remounted 2 times in one step" });
  });
});

describe("layout_shift", () => {
  const shift = (t: number, value: number, input = false, sources = ["div.chat-column in ChatView"]): Entry => ({ t, kind: "layout_shift", value, input, sources });

  it("ignores small shifts and shifts that follow input", () => {
    const rec = recording([shift(10, 0.01), shift(20, 0.02), shift(30, 0.4, true)]);
    expect(detectLayoutShift(rec)).toEqual([]);
  });

  it("flags one shift above the threshold with its sources", () => {
    const rec = recording([shift(10, 0.01), shift(700, 0.12)]);
    const [anomaly] = detectLayoutShift(rec);
    expect(anomaly).toMatchObject({ kind: "layout_shift", total: 0.13, largest: 0.12, sources: ["div.chat-column in ChatView"] });
    expect(anomaly.message).toBe("layout shifted 0.130 in total; biggest 0.120 at 700ms (moved: div.chat-column in ChatView)");
  });

  it("flags many small shifts that add up", () => {
    const rec = recording(Array.from({ length: 6 }, (_, i) => shift(i * 100, 0.02)));
    expect(detectLayoutShift(rec)[0].total).toBeCloseTo(0.12, 5);
  });

  it("honours custom thresholds and the stats total", () => {
    const rec = recording([shift(10, 0.03)], { stats: { layoutShift: { total: 0.2 } } });
    expect(detectLayoutShift(rec)[0].total).toBe(0.2);
    expect(detectLayoutShift(recording([shift(10, 0.03)]), { thresholds: { layoutShiftEntry: 0.02 } })).toHaveLength(1);
  });
});

describe("errors, requests, rows, gaps", () => {
  it("groups identical errors and keeps their sources apart", () => {
    const rec = recording([
      { t: 5, kind: "error", source: "console", message: "boom" },
      { t: 9, kind: "error", source: "console", message: "boom" },
      { t: 12, kind: "error", source: "error", message: "TypeError: x is undefined" },
      { t: 14, kind: "error", source: "rejection", message: "Error: nope" },
    ]);
    const anomalies = detectErrors(rec);
    expect(anomalies.map((a) => a.message)).toEqual([
      "2× console.error at 5ms: boom",
      "uncaught error at 12ms: TypeError: x is undefined",
      "unhandled rejection at 14ms: Error: nope",
    ]);
  });

  it("flags failed requests, not aborted ones", () => {
    const rec = recording([
      { t: 5, kind: "request", method: "GET", url: "/api/sessions/x", status: 404 },
      { t: 6, kind: "request", method: "GET", url: "/api/sessions/x", status: 404 },
      { t: 7, kind: "request", method: "POST", url: "/api/send", error: "TypeError: Failed to fetch" },
      { t: 8, kind: "request", method: "GET", url: "/api/history", error: "AbortError", aborted: true },
    ]);
    const anomalies = detectFailedRequests(rec);
    expect(anomalies.map((a) => a.message)).toEqual([
      "2× GET /api/sessions/x → HTTP 404 at 5ms",
      "POST /api/send → network error (TypeError: Failed to fetch) at 7ms",
    ]);
  });

  it("reports failures across sibling URLs as one parent/* anomaly", () => {
    const names = ["theme", "layout", "groups", "timezone"];
    const rec = recording([
      ...names.map((name, i) => ({ t: 10 + i, kind: "request", method: "PUT", url: `/api/v1/preferences/${name}?…`, status: 403 })),
      { t: 20, kind: "request", method: "PUT", url: "/api/v1/preferences/theme?…", status: 403 },
      { t: 30, kind: "request", method: "GET", url: "/api/v1/preferences/theme?…", status: 500 },
      { t: 40, kind: "request", method: "PUT", url: "/api/v1/other/x", status: 403 },
    ]);
    const anomalies = detectFailedRequests(rec);
    expect(anomalies.map((a) => a.message)).toEqual([
      "5× PUT /api/v1/preferences/* → HTTP 403 at 10ms (4 URLs, e.g. theme?…, layout?…, groups?…)",
      "GET /api/v1/preferences/theme?… → HTTP 500 at 30ms",
      "PUT /api/v1/other/x → HTTP 403 at 40ms",
    ]);
    expect(anomalies[0]).toMatchObject({ url: "/api/v1/preferences/*", urls: 4, count: 5 });
    expect(detectFailedRequests(rec, { thresholds: { requestSiblings: 10 } })).toHaveLength(6);
  });

  it("collects duplicate row ids into one anomaly", () => {
    const rec = recording([
      { t: 40, kind: "duplicate_rows", ids: ["m1"] },
      { t: 80, kind: "duplicate_rows", ids: ["m2", "m1"] },
    ]);
    const [anomaly] = detectDuplicateRows(rec);
    expect(anomaly).toMatchObject({ kind: "duplicate_rows", ids: ["m1", "m2"], indices: [0, 1] });
    expect(anomaly.message).toBe("2 transcript row ids were on the page twice: m1, m2 (first at 40ms)");
  });

  it("reports lost segments and truncation as recording gaps", () => {
    const rec = recording([{ t: 50, kind: "navigation", type: "load", path: "/x", lost: true }], {
      stats: { truncated: true, dropped: 12 },
    });
    expect(detectRecordingGaps(rec).map((a) => a.kind)).toEqual(["recording_gap", "recording_gap"]);
  });

  it("tells a trimmed page-load handoff (earliest entries) apart from the size cap (later entries)", () => {
    const trimmed = recording([{ t: 900, kind: "note", message: "40 earlier entries dropped to fit the page-load handoff" }], {
      stats: { truncated: false, dropped: 0, handoffDropped: 40, handoffDroppedBeforeMs: 900 },
    });
    expect(detectRecordingGaps(trimmed)).toEqual([{
      kind: "recording_gap", indices: [], t: [0, 900],
      message: "40 earliest entries (before 900ms) were dropped to fit the page-load handoff",
    }]);
    const both = recording([], { stats: { truncated: true, dropped: 7, handoffDropped: 40, handoffDroppedBeforeMs: 900 } });
    expect(detectRecordingGaps(both).map((a) => a.message)).toEqual([
      "40 earliest entries (before 900ms) were dropped to fit the page-load handoff",
      "the recording hit its size cap; 7 later entries were not kept",
    ]);
  });

  it("orders anomalies by kind, then time", () => {
    const rec = recording([
      { t: 1, kind: "error", source: "console", message: "late" },
      state(0, 0, { s: "a" }), state(16, 1, { s: "b" }), state(33, 2, { s: "a" }),
      mount(40, "X", "unmount"), mount(41, "X"),
    ]);
    expect(analyzeRecording(rec).map((a) => a.kind)).toEqual(["reversal", "remount", "errors"]);
  });

  it("tolerates empty and malformed input", () => {
    expect(analyzeRecording({})).toEqual([]);
    expect(analyzeRecording(null)).toEqual([]);
    expect(analyzeRecording({ timeline: [null, { t: 1, kind: "state" }, { t: 2, kind: "mount" }] })).toEqual([]);
    expect(formatTimeline({})).toBe("");
  });
});

describe("foldStates", () => {
  it("folds changes into the full state after each entry", () => {
    const states = foldStates(recording([state(0, 0, { a: "1", b: "2" }), mark(5, "x"), state(16, 1, { b: null })]));
    expect(states).toEqual([{ a: "1", b: "2" }, { a: "1", b: "2" }, { a: "1", b: null }]);
  });
});

describe("formatTimeline", () => {
  it("prints the full first state, then only what changed, with markers", () => {
    const rec = recording([
      state(0, 0, { path: "/new", surface: "new", composer: "centre" }),
      mark(4, "act click Create"),
      state(16, 1, { surface: "active", composer: "bottom" }),
      mount(16, "MessageArea"),
      state(33, 2, { surface: "new", composer: "centre" }),
      state(1200, 70, { path: "/chat/x", surface: "active", composer: "bottom" }),
    ], { initial: { mounts: { ChatView: 1, NewSessionHero: 1 } } });
    rec.anomalies = analyzeRecording(rec);
    expect(formatTimeline(rec)).toBe([
      "   0ms  mounted at start: ChatView, NewSessionHero",
      "   0ms  /new  surface=new composer=centre",
      "   4ms  > mark \"act click Create\"",
      "  16ms  surface=active composer=bottom   << reversal(surface), reversal(composer)",
      "  16ms  + MessageArea mounted",
      "  33ms  surface=new composer=centre   << reversal(surface), reversal(composer)",
      // A new page, long after: moving on, not going back.
      "1200ms  /chat/x  surface=active composer=bottom",
    ].join("\n"));
  });

  it("quotes values with spaces and shows removed fields", () => {
    const text = formatTimeline(recording([state(0, 0, { title: "Hello world", surface: "new" }), state(16, 1, { surface: null })]));
    expect(text).toBe('   0ms  title="Hello world" surface=new\n  16ms  surface=(gone)');
  });

  it("describes every entry kind", () => {
    const text = formatTimeline(recording([
      { t: 1, kind: "error", source: "rejection", message: "Error: x" },
      { t: 2, kind: "request", method: "GET", url: "/a", status: 500 },
      { t: 3, kind: "request", method: "GET", url: "/b", error: "x", aborted: true },
      { t: 4, kind: "duplicate_rows", ids: ["m1"] },
      { t: 5, kind: "layout_shift", value: 0.2, input: false, sources: ["div.x"] },
      { t: 6, kind: "navigation", type: "load", path: "/p" },
      { t: 7, kind: "visibility", state: "hidden" },
      { t: 8, kind: "note", message: "recording stopped: x" },
      mount(9, "Hero", "unmount"),
    ]));
    expect(text.split("\n").map((line) => line.slice(8))).toEqual([
      "! unhandled rejection: Error: x",
      "! GET /a → 500",
      "· GET /b → aborted",
      "! duplicate rows: m1",
      "~ layout shift 0.200 div.x",
      "* full page load /p",
      "* page hidden",
      "* recording stopped: x",
      "- Hero unmounted",
    ]);
  });

  it("elides a long timeline but keeps marks and anomaly lines", () => {
    const timeline: Entry[] = [state(0, 0, { n: "0" })];
    for (let i = 1; i <= 600; i += 1) timeline.push(state(i * 16, i, { n: String(i) }));
    timeline.push(mark(9700, "the step"));
    timeline.push(state(9716, 601, { n: "600-b" }), state(9733, 602, { n: "600" }));
    const rec = recording(timeline);
    rec.anomalies = analyzeRecording(rec);
    const text = formatTimeline(rec, { maxLines: 100 });
    const lines = text.split("\n");
    expect(lines.length).toBeLessThan(100);
    expect(text).toContain('> mark "the step"');
    expect(text).toContain("n=600-b   << reversal(n)");
    expect(text).toMatch(/… \d+ lines …/);
  });
});

describe("formatReport and formatAnomalies", () => {
  it("leads with a headline and ends with the numbered anomalies", () => {
    const rec = recording([state(0, 0, { s: "a" }), state(16, 1, { s: "b" }), state(33, 2, { s: "a" })], {
      durationMs: 40, stats: { frames: 3, stateChanges: 3, maxFrameGapMs: 17 },
    });
    const report = formatReport(rec);
    expect(report.split("\n")[0]).toBe("flight recording: 40ms, 3 frames, 3 state changes, 1 anomaly, longest frame gap 17ms");
    expect(report).toContain("anomalies:\n1. reversal: s went a → b → a within one step");
    expect(formatAnomalies([])).toBe("no anomalies");
  });
});
