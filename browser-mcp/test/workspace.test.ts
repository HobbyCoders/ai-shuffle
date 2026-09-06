import { afterEach, describe, expect, it, vi } from "vitest";
import { Session } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";
import { BrowserWorkspace, HUMAN_LEASE_MS, setWorkspaceRuntimeIdentity } from "../src/workspace.js";
import { validateCommand, validateInputCommand } from "../src/workspace-protocol.js";
import { TOOL_MAP } from "../src/tools/index.js";
import { runWait } from "../src/tools/wait.js";

const runtimes: BrowserWorkspace[] = [];
const make = () => { const session = new Session(new BrowserManager()); const ws = new BrowserWorkspace(session); runtimes.push(ws); return { session, ws }; };
let seq = 0;
const command = (ws: BrowserWorkspace, op = "takeover", args = {}, extra = {}) => ({ id: `c${++seq}`, runtime_id: ws.runtimeId, tab_id: null, viewer_id: "alice", op, args, expires_at: Date.now() + 30000, ...extra });
const deferred = () => { let resolve!: () => void; const promise = new Promise<void>(r => { resolve = r; }); return { promise, resolve }; };
afterEach(() => { runtimes.forEach(w => w.stop()); runtimes.length = 0; vi.useRealTimers(); vi.restoreAllMocks(); });

describe("runtime identity", () => {
  it("echoes the app-owned identity without affecting other sessions", () => {
    const session = new Session(new BrowserManager());
    setWorkspaceRuntimeIdentity(session, { AIS_BROWSER_MODE: "runtime", _AIS_BROWSER_RUNTIME_ID: "owned-child" });
    const ws = new BrowserWorkspace(session);
    runtimes.push(ws);
    expect(ws.runtimeId).toBe("owned-child");
    expect(session.workspaceStatus?.().runtime_id).toBe("owned-child");
    expect(make().ws.runtimeId).not.toBe("owned-child");
    const explicit = new BrowserWorkspace(session, "explicit-id");
    runtimes.push(explicit);
    expect(explicit.runtimeId).toBe("explicit-id");
  });
  it.each([undefined, "proxy", "direct"])("ignores injected identity in %s mode", mode => {
    const session = new Session(new BrowserManager());
    setWorkspaceRuntimeIdentity(session, { AIS_BROWSER_MODE: mode, _AIS_BROWSER_RUNTIME_ID: "ignored" });
    const ws = new BrowserWorkspace(session);
    runtimes.push(ws);
    expect(ws.runtimeId).toMatch(/^[0-9a-f-]{36}$/);
    expect(ws.runtimeId).not.toBe("ignored");
    expect(make().ws.runtimeId).not.toBe(ws.runtimeId);
  });
  it("keeps the random default when runtime mode has no injected identity", () => {
    const session = new Session(new BrowserManager());
    setWorkspaceRuntimeIdentity(session, { AIS_BROWSER_MODE: "runtime" });
    const ws = new BrowserWorkspace(session);
    runtimes.push(ws);
    expect(ws.runtimeId).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("shared control queue", () => {
  it("preserves the panel's committed-text size limit", () => {
    expect(() => validateInputCommand("Input.insertText", { text: "x".repeat(16384) })).not.toThrow();
    expect(() => validateInputCommand("Input.insertText", { text: "x".repeat(16385) })).toThrow();
  });
  it("bounds live tabs so the active tab always fits in authoritative state", async () => {
    const manager = new BrowserManager();
    const session = new Session(manager);
    const create = vi.spyOn(manager, "newPage");
    Object.defineProperty(session, "pages", {
      value: new Map(Array.from({ length: 128 }, (_, i) => [`tab${i}`, {}])),
    });
    await expect(session.createTab()).rejects.toThrow("tab limit reached");
    expect(create).not.toHaveBeenCalled();
  });
  it("duplicate same-viewer pending takeover is harmless", async () => {
    const {session, ws} = make(); const gate = deferred();
    const active = session.runExclusive(()=>gate.promise); await Promise.resolve();
    const first = ws.receive(command(ws)); const second = ws.receive(command(ws));
    gate.resolve(); await active;
    expect((await first).ok).toBe(true); expect((await second).ok).toBe(true);
  });
  it("rechecks active tab after an earlier queued human selection", async () => {
    const {session, ws} = make(); await ws.receive(command(ws));
    let tab = "tab1";
    vi.spyOn(session,"currentTabId").mockImplementation(()=>tab);
    vi.spyOn(session,"selectTab").mockImplementation(id=>{tab=id;});
    const select = ws.receive(command(ws,"tabs",{action:"select",tab_id:"tab2"},{tab_id:"tab1"}));
    const stale = ws.receive(command(ws,"navigate",{url:"https://example.com"},{tab_id:"tab1"}));
    expect((await select).ok).toBe(true); expect((await stale).error).toContain("stale tab");
  });
  it("passes only allowed navigation and CDP commands to the current page", async () => {
    const {session, ws} = make(); await ws.receive(command(ws));
    const page = {goBack:vi.fn(async()=>{}),goForward:vi.fn(async()=>{}),reload:vi.fn(async()=>{}),goto:vi.fn(async()=>{})};
    const cdp = {send:vi.fn(async()=>{})};
    vi.spyOn(session,"currentTabId").mockReturnValue("tab1");
    vi.spyOn(session,"hasPage").mockReturnValue(true);
    vi.spyOn(session,"ensurePage").mockResolvedValue(page as never);
    vi.spyOn(session,"currentCDP").mockResolvedValue(cdp as never);
    for(const action of ["back","forward","reload","stop"]) expect((await ws.receive(command(ws,"command",{command:action},{tab_id:"tab1"}))).ok).toBe(true);
    expect(page.goBack).toHaveBeenCalledWith({waitUntil:"commit",timeout:15000});
    expect(page.goForward).toHaveBeenCalledWith({waitUntil:"commit",timeout:15000});
    expect(page.reload).toHaveBeenCalledWith({waitUntil:"domcontentloaded",timeout:15000});
    expect(cdp.send).toHaveBeenCalledWith("Page.stopLoading");
    expect((await ws.receive(command(ws,"input",{method:"Input.insertText",params:{text:"hello"}},{tab_id:"tab1"}))).ok).toBe(true);
    expect(cdp.send).toHaveBeenCalledWith("Input.insertText",{text:"hello"});
  });

  it("reserves immediately, rejects already queued agents, drains in-flight operation before takeover", async () => {
    const { session, ws } = make(); const gate = deferred(); const started = deferred();
    const active = session.runAgentExclusive(async () => { started.resolve(); await gate.promise; });
    await started.promise;
    const queued = session.runAgentExclusive(async () => "must not run");
    const rejected = expect(queued).rejects.toThrow("human_control");
    const take = ws.receive(command(ws));
    expect(ws.state()).toMatchObject({ busy: true, control: { owner: "agent", pending_viewer_id: "alice" } });
    await expect(session.runAgentExclusive(async () => {})).rejects.toThrow("human_control");
    gate.resolve(); await active; await rejected;
    expect(await take).toMatchObject({ ok: true });
    expect(ws.state().control).toEqual({ owner: "human", viewer_id: "alice", pending_viewer_id: null });
    const status = await TOOL_MAP.get("browser_status")!.run(session, {});
    expect(status.isError).toBeUndefined();
    expect(JSON.parse((status.content[0] as {text:string}).text).control.owner).toBe("human");
  });
  it("denies competing viewer and heartbeat theft; owner release restores agent", async () => {
    const { session, ws } = make(); await ws.receive(command(ws));
    for (const op of ["takeover", "heartbeat", "release"]) expect((await ws.receive(command(ws, op, {}, { viewer_id: "bob" }))).ok).toBe(false);
    expect((await ws.receive(command(ws))).ok).toBe(true);
    expect((await ws.receive(command(ws, "release"))).ok).toBe(true);
    await expect(session.runAgentExclusive(async () => 7)).resolves.toBe(7);
  });
  it("disconnect release cancels a pending takeover even before queue drains", async () => {
    const { session, ws } = make(); const gate = deferred();
    const active = session.runExclusive(() => gate.promise); await Promise.resolve();
    const take = ws.receive(command(ws));
    expect((await ws.receive(command(ws, "release"))).ok).toBe(true);
    const bob = ws.receive(command(ws, "takeover", {}, { viewer_id: "bob" }));
    gate.resolve(); await active;
    expect((await take).ok).toBe(false); expect((await bob).ok).toBe(true);
    expect(ws.state().control.viewer_id).toBe("bob");
  });
  it("expires idle human leases and refreshes only same-owner heartbeats", async () => {
    vi.useFakeTimers(); const { session, ws } = make(); await ws.receive(command(ws));
    await vi.advanceTimersByTimeAsync(60000);
    await ws.receive(command(ws, "heartbeat"));
    await vi.advanceTimersByTimeAsync(60000); expect(ws.state().control.owner).toBe("human");
    await vi.advanceTimersByTimeAsync(6000); expect(ws.state().control.owner).toBe("agent");
    await expect(session.runAgentExclusive(async () => 1)).resolves.toBe(1);
  });
  it("checks expiry again in queue and clears failed reservation", async () => {
    const { session, ws } = make(); const gate = deferred();
    const active = session.runExclusive(() => gate.promise); await Promise.resolve();
    const take = ws.receive(command(ws, "takeover", {}, { expires_at: Date.now() + 10 }));
    await new Promise(r => setTimeout(r, 20)); gate.resolve(); await active;
    expect(await take).toMatchObject({ ok: false }); expect(ws.state().control.owner).toBe("agent");
    await expect(session.runAgentExclusive(async () => {})).resolves.toBeUndefined();
  });
  it("rejects stale runtime, stale tabs, duplicate ids, and missing ownership without page acquisition", async () => {
    const { session, ws } = make(); const ensure = vi.spyOn(session, "ensurePage");
    const c = command(ws); expect((await ws.receive({ ...c, runtime_id: "old" })).ok).toBe(false);
    expect((await ws.receive(c)).ok).toBe(true); expect((await ws.receive(c)).ok).toBe(false);
    expect((await ws.receive(command(ws, "navigate", { url: "https://example.com" }, { tab_id: "old" }))).error).toContain("stale tab");
    await ws.receive(command(ws, "release"));
    expect((await ws.receive(command(ws, "tabs", { action: "create" }))).error).toContain("human control");
    expect(ensure).not.toHaveBeenCalled();
  });
  it("bounds queue and cancels queued commands at shutdown", async () => {
    const { session, ws } = make(); const gate = deferred();
    const active = session.runExclusive(() => gate.promise); await Promise.resolve();
    const pending = Array.from({length: 64}, () => ws.receive(command(ws)));
    expect((await ws.receive(command(ws))).error).toContain("queue full");
    ws.stop(); gate.resolve(); await active;
    expect((await Promise.all(pending)).every(r => !r.ok)).toBe(true);
    await expect(session.runAgentExclusive(async () => {})).rejects.toThrow("stopped");
  });
  it("does not publish a metadata result captured across a tab generation change", async () => {
    const { session, ws } = make(); const gate = deferred();
    vi.spyOn(session, "workspaceMetadata").mockImplementation(async () => { await gate.promise; return { target_id: "old", active_tab_id: "tab1", tabs: [], viewport: { mode: "fixed", width: 800, height: 600 } }; });
    const refresh = ws.refresh(); session.notifyNavigation(); gate.resolve(); await refresh;
    expect(ws.state().target_id).toBeNull();
  });
  it("coordinates require recapture after navigation/resize/takeover", async () => {
    const { session, ws } = make(); session.setLastScreenshot({scale:1, offsetX:0, offsetY:0});
    await ws.receive(command(ws));
    await expect(session.toViewportPoint([1, 2])).rejects.toThrow("stale_screenshot");
  });
});

describe("typed command validation", () => {
  it.each(["takeover", "release", "heartbeat"])("accepts %s empty args only", op => {
    const { ws } = make(); expect(validateCommand(command(ws, op), ws.runtimeId).op).toBe(op);
    expect(() => validateCommand(command(ws, op, { extra: true }), ws.runtimeId)).toThrow();
  });
  it.each([
    ["navigate", {url:"javascript:alert(1)"}], ["navigate", {url:"file:///tmp/a"}],
    ["viewport", {mode:"fit",width:239,height:240}], ["viewport", {mode:"fixed",width:3841,height:240}],
    ["viewport", {mode:"fit",width:240,height:2161}], ["viewport", {mode:"fit",width:NaN,height:240}],
    ["viewport", {mode:"fit",width:240.5,height:240}], ["tabs", {action:"select"}],
    ["tabs", {action:"close",tab_id:"tab1",url:"https://example.com"}], ["command", {command:"eval"}],
    ["input", {method:"Runtime.evaluate",params:{expression:"1"}}],
    ["input", {method:"Input.dispatchKeyEvent",params:{type:"keyDown",commands:["selectAll"]}}],
    ["input", {method:"Input.dispatchMouseEvent",params:{type:"mouseMoved",x:Infinity,y:0}}],
    ["input", {method:"Input.insertText",params:{text:"x".repeat(16385)}}],
  ])("rejects %s %j", (op, args) => {
    const { ws } = make(); expect(() => validateCommand(command(ws, op as string, args), ws.runtimeId)).toThrow();
  });
  it("accepts normalized keyboard, mouse and IME input", () => {
    expect(() => validateInputCommand("Input.insertText", {text:"你好"})).not.toThrow();
    expect(() => validateInputCommand("Input.dispatchKeyEvent", {type:"keyDown",key:"Enter",code:"Enter",windowsVirtualKeyCode:13,modifiers:0,text:"\r"})).not.toThrow();
    expect(() => validateInputCommand("Input.dispatchMouseEvent", {type:"mouseWheel",x:10,y:20,button:"left",clickCount:0,modifiers:0,deltaX:0,deltaY:120})).not.toThrow();
  });
  it.each([NaN, Infinity, 0, Date.now()+200000])("rejects bad deadline %s", expires_at => {
    const { ws } = make(); expect(() => validateCommand(command(ws, "takeover", {}, {expires_at}), ws.runtimeId)).toThrow();
  });
});

describe("navigation interruption", () => {
  it("stops a blocked navigation before the operation queue drains and recovers", async () => {
    const { session, ws } = make();
    await ws.receive(command(ws));
    const started = deferred(), gate = deferred();
    const page = {
      goto: vi.fn(async () => { started.resolve(); await gate.promise; throw new Error("navigation aborted"); }),
      reload: vi.fn(async () => {}),
    };
    const cdp = { send: vi.fn(async () => { gate.resolve(); }) };
    vi.spyOn(session, "currentTabId").mockReturnValue("tab1");
    vi.spyOn(session, "hasPage").mockReturnValue(true);
    vi.spyOn(session, "ensurePage").mockResolvedValue(page as never);
    vi.spyOn(session, "currentCDP").mockResolvedValue(cdp as never);
    const navigation = ws.receive(command(ws, "navigate", { url: "https://example.com" }, { tab_id: "tab1" }));
    await started.promise;
    const stopped = ws.receive(command(ws, "command", { command: "stop" }, { tab_id: "tab1" }));
    try {
      await Promise.resolve();
      expect(cdp.send).toHaveBeenCalledWith("Page.stopLoading");
    } finally {
      gate.resolve();
      await navigation;
      await stopped;
    }
    expect((await navigation).ok).toBe(false);
    expect((await stopped).ok).toBe(true);
    expect((await ws.receive(command(ws, "command", { command: "reload" }, { tab_id: "tab1" }))).ok).toBe(true);
    expect(page.reload).toHaveBeenCalledOnce();
  });

  it.each(["release", "tab", "runtime", "expiry"])("rechecks %s after acquiring the stop CDP session", async (change) => {
    if (change === "expiry") vi.useFakeTimers();
    const { session, ws } = make();
    await ws.receive(command(ws));
    const gate = deferred(), started = deferred();
    let tab = "tab1";
    const cdp = { send: vi.fn(async () => {}) };
    vi.spyOn(session, "currentTabId").mockImplementation(() => tab);
    vi.spyOn(session, "hasPage").mockReturnValue(true);
    vi.spyOn(session, "ensurePage").mockResolvedValue({} as never);
    vi.spyOn(session, "currentCDP").mockImplementation(async () => { started.resolve(); await gate.promise; return cdp as never; });
    const stopped = ws.receive(command(ws, "command", { command: "stop" }, { tab_id: "tab1" }));
    await started.promise;
    if (change === "release") await ws.receive(command(ws, "release"));
    else if (change === "tab") tab = "tab2";
    else if (change === "runtime") ws.stop();
    else vi.setSystemTime(Date.now() + HUMAN_LEASE_MS + 1);
    gate.resolve();
    expect((await stopped).ok).toBe(false);
    expect(cdp.send).not.toHaveBeenCalled();
  });
});

describe("bounded wait", () => {
  function fixture() {
    const {session, ws} = make();
    const locator = { first() {return this;}, isVisible: vi.fn(async () => true), count: vi.fn(async () => 1) };
    const page = { url: () => "https://example.com/ready", locator: () => locator, getByText: () => locator };
    vi.spyOn(session, "ensurePage").mockResolvedValue(page as never);
    vi.spyOn(session, "resolveRef").mockReturnValue(locator as never);
    return {session, ws, locator};
  }
  it.each([
    {condition:"url",url:"/ready"}, {condition:"url",url:"https://example.com/ready",match:"exact"},
    {condition:"text",text:"ready"}, {condition:"element",selector:"#ok",state:"attached"},
    {condition:"element",ref:"e1",state:"visible"},
  ])("matches %j", async input => {
    const {session} = fixture(); const result = await runWait(session, input as never);
    expect(JSON.parse((result.content[0] as {text:string}).text).matched).toBe(true);
  });
  it("hidden/detached resolve and timeout remains bounded", async () => {
    const {session, locator} = fixture(); locator.isVisible.mockResolvedValue(false); locator.count.mockResolvedValue(0);
    for (const state of ["hidden", "detached"] as const) expect(JSON.parse(((await runWait(session,{condition:"element",selector:"#ok",state})).content[0] as {text:string}).text).matched).toBe(true);
    const r = await runWait(session,{condition:"url",url:"missing",timeout_ms:10});
    expect(JSON.parse((r.content[0] as {text:string}).text)).toMatchObject({
      matched: false, timed_out: true, target: "missing", condition: "url", tab_id: null,
    });
  });
  it("takeover interrupts active wait", async () => {
    const {session, ws} = fixture();
    const waiting = session.runAgentExclusive(() => runWait(session,{condition:"url",url:"missing",timeout_ms:15000}));
    await Promise.resolve(); const reject = expect(waiting).rejects.toThrow("human_control");
    const take = ws.receive(command(ws)); await reject; expect((await take).ok).toBe(true);
  });
  it("invalid combinations fail before acquiring page", async () => {
    const {session} = fixture();
    for (const input of [{condition:"url"}, {condition:"element",ref:"e1",selector:"#ok"}, {condition:"url",url:"a",timeout_ms:16000}, {condition:"url",url:"a",state:"hidden"}]) expect((await runWait(session,input as never)).isError).toBe(true);
    expect(session.ensurePage).not.toHaveBeenCalled();
  });
});
