/** Flight recorder: embedded analyzer sync, Node driver helpers and browser_record wiring (no browser). */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { EventEmitter } from "node:events";
// @ts-expect-error plain .mjs build script without types
import { renderEmbedded, syncedRecorder, ANALYZE_PATH, RECORDER_PATH } from "../scripts/sync-recorder.mjs";
import {
  applyThrottle,
  clearThrottle,
  contactSheetHtml,
  NETWORK_PROFILES,
  pickKeyframes,
  recorderSource,
  resumeScript,
  sheetColumns,
  sheetFrames,
  startScreencast,
} from "../src/recorder/driver.js";
import { stepLabel } from "../src/tools/record.js";
import { TOOL_CATALOG } from "../src/catalog.js";
import { recordSchema, validateInput } from "../src/schemas.js";
import { SERVER_INSTRUCTIONS } from "../src/instructions.js";

describe("embedded analyzer", () => {
  it("flight-recorder.js embeds the current analyze.js (run scripts/sync-recorder.mjs after editing it)", () => {
    const recorder = readFileSync(RECORDER_PATH, "utf8").replace(/\r\n/g, "\n");
    expect(syncedRecorder(recorder, readFileSync(ANALYZE_PATH, "utf8"))).toBe(recorder);
  });

  it("refuses an analyze.js that imports or exports anything but declarations", () => {
    expect(() => renderEmbedded("import x from 'y';\nexport function a() {}")).toThrow(/only export declarations/);
    expect(() => renderEmbedded("export default 1;")).toThrow(/only export declarations/);
    expect(() => renderEmbedded("const a = 1;")).toThrow(/exports nothing/);
  });

  it("is one dependency-free classic script", () => {
    const source = recorderSource();
    expect(() => new Function(source)).not.toThrow();
    expect(source).not.toMatch(/^\s*(import|export)\b/m);
    expect(source).not.toMatch(/\brequire\(/);
  });
});

describe("resumeScript", () => {
  it("appends a resume call whose config cannot close the script tag or break out", () => {
    const script = resumeScript({ id: "fr-1", startEpoch: 5, opts: { selectors: ["x=</script><b>"] } });
    expect(script.startsWith(recorderSource())).toBe(true);
    expect(script).not.toContain("</script>");
    expect(script).toContain('"id":"fr-1"');
    expect(() => new Function(script)).not.toThrow();
  });
});

class FakeCdp extends EventEmitter {
  calls: Array<[string, unknown]> = [];
  fail = new Set<string>();
  async send(method: string, params?: unknown) {
    this.calls.push([method, params]);
    if (this.fail.has(method)) throw new Error(`${method} failed`);
    return {};
  }
}

describe("throttling", () => {
  it("applies CPU and network throttling and clears both", async () => {
    const cdp = new FakeCdp();
    const applied = await applyThrottle(cdp, { cpu: 4, network: "fast-3g" });
    expect(applied).toEqual({ cpu: 4, network: "fast-3g" });
    expect(cdp.calls).toEqual([
      ["Emulation.setCPUThrottlingRate", { rate: 4 }],
      ["Network.enable", undefined],
      ["Network.emulateNetworkConditions", { offline: false, ...NETWORK_PROFILES["fast-3g"] }],
    ]);
    cdp.calls = [];
    expect(await clearThrottle(cdp, applied)).toEqual([]);
    expect(cdp.calls.map(([m]) => m)).toEqual(["Emulation.setCPUThrottlingRate", "Network.emulateNetworkConditions", "Network.disable"]);
  });

  it("caps the CPU factor, skips no-ops and rejects unknown profiles", async () => {
    const cdp = new FakeCdp();
    expect(await applyThrottle(cdp, { cpu: 99 })).toEqual({ cpu: 20 });
    expect(await applyThrottle(new FakeCdp(), { cpu: 1, network: "none" })).toEqual({});
    await expect(applyThrottle(new FakeCdp(), { network: "dialup" })).rejects.toThrow(/unknown network profile/);
  });

  it("finishes cleanup even when a step fails", async () => {
    const cdp = new FakeCdp();
    cdp.fail.add("Emulation.setCPUThrottlingRate");
    const errors = await clearThrottle(cdp, { cpu: 4, network: "4g" });
    expect(errors).toHaveLength(1);
    expect(cdp.calls.map(([m]) => m)).toContain("Network.disable");
  });
});

describe("screencast collection", () => {
  it("acks every frame and thins frames evenly past the byte budget", async () => {
    const cdp = new FakeCdp();
    const cast = await startScreencast(cdp, { maxChars: 1000 });
    for (let i = 0; i < 40; i += 1) {
      cdp.emit("Page.screencastFrame", { sessionId: i, data: "x".repeat(100), metadata: { timestamp: 1000 + i / 100 } });
    }
    const frames = await cast.stop();
    expect(cdp.calls.filter(([m]) => m === "Page.screencastFrameAck")).toHaveLength(40);
    expect(frames.length).toBeLessThanOrEqual(10);
    expect(frames[0].ts).toBe(1_000_000);
    expect(frames[frames.length - 1].ts).toBeCloseTo(1_000_390, 0);
    expect(cast.stats()).toMatchObject({ received: 40 });
    expect(cdp.calls.map(([m]) => m)).toContain("Page.stopScreencast");
    expect(cdp.listenerCount("Page.screencastFrame")).toBe(0);
  });
});

describe("pickKeyframes", () => {
  const startedAt = 10_000;
  const rec = {
    startedAt,
    timeline: [
      { t: 0, kind: "state", changes: { surface: "new" } },
      { t: 5, kind: "mark", label: "go" },
      { t: 100, kind: "state", changes: { surface: "active" } },
      { t: 116, kind: "state", changes: { surface: "new" } },
      { t: 400, kind: "state", changes: { messages: "1" } },
      { t: 600, kind: "state", changes: { surface: "active" } },
    ],
    anomalies: [{ kind: "reversal", field: "surface", indices: [2, 3], t: [100, 116], message: "" }],
  };
  const frame = (t: number, data: string) => ({ ts: startedAt + t, data, width: 1440, height: 900 });

  it("takes the first frame after each change and labels anomalies", () => {
    const frames = [frame(1, "a"), frame(104, "b"), frame(120, "c"), frame(605, "e")];
    const picked = pickKeyframes(rec as never, frames);
    expect(picked.map((k) => [k.index, k.data, k.repaint, k.flags])).toEqual([
      [0, "a", true, []],
      [2, "b", true, ["reversal(surface)"]],
      [3, "c", true, ["reversal(surface)"]],
      [5, "e", true, []],
    ]);
    expect(picked[1].fields).toBe("surface=active");
  });

  it("shows a state that did not repaint with the last frame, only when an anomaly points at it", () => {
    const frames = [frame(1, "a"), frame(104, "b"), frame(120, "c")];
    expect(pickKeyframes(rec as never, frames).find((k) => k.index === 4)).toBeUndefined();
    const flagged = { ...rec, anomalies: [...rec.anomalies, { kind: "errors", indices: [4], t: [400, 400], message: "" }] };
    expect(pickKeyframes(flagged as never, frames).find((k) => k.index === 4)).toMatchObject({ data: "c", repaint: false, flags: ["errors"] });
  });

  it("lets a one-frame state own a frame swapped just after the next change", () => {
    const frames = [frame(1, "a"), frame(118, "late"), frame(135, "next")];
    const picked = pickKeyframes(rec as never, frames);
    expect(picked.find((k) => k.index === 2)?.data).toBe("late");
    expect(picked.find((k) => k.index === 3)?.data).toBe("next");
  });

  it("caps the count but keeps anomaly frames", () => {
    const many = { ...rec, timeline: [...rec.timeline] } as { startedAt: number; timeline: Array<Record<string, unknown>>; anomalies: unknown[] };
    const frames = [frame(1, "a"), frame(104, "b"), frame(120, "c"), frame(605, "e")];
    for (let i = 0; i < 30; i += 1) {
      many.timeline.push({ t: 700 + i * 50, kind: "state", changes: { n: String(i) } });
      frames.push(frame(702 + i * 50, `n${i}`));
    }
    const picked = pickKeyframes(many as never, frames, { max: 5 });
    expect(picked).toHaveLength(5);
    expect(picked.map((k) => k.index)).toEqual(expect.arrayContaining([2, 3]));
  });

  it("returns nothing without states or frames", () => {
    expect(pickKeyframes(rec as never, [])).toEqual([]);
    expect(pickKeyframes({ startedAt, timeline: [], anomalies: [] } as never, [frame(1, "a")])).toEqual([]);
  });
});

describe("contact sheet", () => {
  const keyframes = [
    { index: 2, t: 100, fields: 'surface=active <b>', flags: ["reversal(surface)"], approximate: false, repaint: true, data: "AAAA", width: 390, height: 844, frame_ms: 104 },
  ];
  it("renders an escaped self-contained grid", () => {
    const html = contactSheetHtml(keyframes as never, { title: "a <title>" });
    expect(html).toContain('src="data:image/jpeg;base64,AAAA"');
    expect(html).toContain("surface=active &lt;b&gt;");
    expect(html).toContain("a &lt;title&gt;");
    expect(html).not.toMatch(/<script/i);
  });
  it("uses more columns for portrait frames", () => {
    expect(sheetColumns(keyframes as never)).toBe(1);
    const six = Array.from({ length: 8 }, () => keyframes[0]);
    expect(sheetColumns(six as never)).toBe(6);
    expect(sheetColumns(six.map((k) => ({ ...k, width: 1440, height: 900 })) as never)).toBe(3);
    expect(sheetFrames(keyframes as never)[0]).toMatchObject({ flagged: true, caption: "#2 · 100ms · reversal(surface)\nsurface=active <b>" });
  });
});

describe("browser_record wiring", () => {
  it("is in the catalog with a flat schema and server instructions", () => {
    const tool = TOOL_CATALOG.find((t) => t.name === "browser_record")!;
    expect(tool.inputSchema).toBe(recordSchema);
    expect(validateInput(recordSchema, { action: "start", cpu_throttle: 4, network: "fast-3g", keyframes: true }).ok).toBe(true);
    expect(validateInput(recordSchema, { action: "rewind" }).ok).toBe(false);
    expect(validateInput(recordSchema, { action: "start", cpu_throttle: 50 }).ok).toBe(false);
    expect(validateInput(recordSchema, { action: "start", network: "dialup" }).ok).toBe(false);
    expect(SERVER_INSTRUCTIONS).toContain("FLIGHT RECORDER");
  });

  it("labels acting steps without their typed values or URL queries", () => {
    expect(stepLabel("browser_act", { kind: "click", ref: "e12" })).toBe("act click e12");
    expect(stepLabel("browser_act", { kind: "type", ref: "e3", value: "my password" })).toBe("act type e3");
    expect(stepLabel("browser_act", { kind: "key", key: "Enter" })).toBe("act key Enter");
    expect(stepLabel("browser_act", { kind: "click", coordinate: [10, 20] })).toBe("act click @10,20");
    expect(stepLabel("browser_navigate", { url: "http://h:1/a/b?token=secret#x" })).toBe("navigate http://h:1/a/b?…");
    expect(stepLabel("browser_eval", { code: "secret()" })).toBe("eval");
    expect(stepLabel("browser_resize", { width: 390, height: 844 })).toBe("resize 390x844");
    expect(stepLabel("browser_snapshot", {})).toBeUndefined();
    expect(stepLabel("browser_record", { action: "stop" })).toBeUndefined();
  });
});
