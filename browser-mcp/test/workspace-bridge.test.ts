import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceBridge, bridgeConfig, reportBatch } from "../src/workspace-bridge.js";
import { Session } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";
const bridges: WorkspaceBridge[] = [];
afterEach(() => { bridges.forEach(b => b.stop()); bridges.length = 0; vi.useRealTimers(); vi.restoreAllMocks(); });
const json = (data: unknown) => new Response(JSON.stringify(data), {headers:{"content-type":"application/json"}});
function harness(options: { registerStatus?: number; reportFail?: boolean; pollStatus?: number; viewerCount?: number; reportStatus?: (body: any) => number | undefined } = {}) {
  const calls: {endpoint:string;body:any}[] = [];
  let deliver: ((commands: unknown[], viewerCount?: number) => void) | undefined;
  let failedReport = false;
  const fetcher = vi.fn(async (url: URL | RequestInfo, init?: RequestInit) => {
    const endpoint = String(url).split("/").at(-1)!;
    const body = JSON.parse(init!.body as string); calls.push({endpoint,body});
    if (endpoint === "register") return options.registerStatus ? new Response(null,{status:options.registerStatus}) : json({ok:true,viewer_count:options.viewerCount});
    if (endpoint === "report") {
      if (options.reportFail && !failedReport) {failedReport=true; return new Response(null,{status:503});}
      const status = options.reportStatus?.(body);
      if (status) return new Response(null,{status});
      return new Response(null,{status:204});
    }
    if (options.pollStatus) return new Response(null,{status:options.pollStatus});
    return new Promise<Response>((resolve,reject) => {
      const aborted = () => { deliver=undefined; reject(new Error("aborted")); };
      init!.signal!.addEventListener("abort",aborted,{once:true});
      deliver = (commands, viewerCount = options.viewerCount) => {init!.signal!.removeEventListener("abort",aborted); deliver=undefined; resolve(json({commands,viewer_count:viewerCount}));};
    });
  });
  const session = new Session(new BrowserManager());
  vi.spyOn(session, "workspaceMetadata").mockResolvedValue({target_id:"target-1",active_tab_id:"tab1",tabs:[{id:"tab1",url:"https://example.com",title:"fixture",current:true}],viewport:{mode:"fixed",width:1280,height:800}});
  const bridge = new WorkspaceBridge(session,{sessionId:"s",token:"test-token",baseUrl:"http://localhost/api/v1/browser/panel/control/"},fetcher as typeof fetch);
  bridges.push(bridge); bridge.start();
  return {session,bridge,calls,fetcher,send:(commands:unknown[], viewerCount?:number) => { if (!deliver) throw new Error("no poll"); deliver(commands, viewerCount); }};
}
describe("runtime bridge", () => {
  it("backs off passive viewers, ignores heartbeat activity, and refreshes on events", async () => {
    vi.useFakeTimers();
    const h = harness({viewerCount:1});
    await vi.advanceTimersByTimeAsync(600);
    h.send([{id:"take",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"owner",op:"takeover",args:{},expires_at:Date.now()+30000}]);
    await vi.advanceTimersByTimeAsync(11000);
    expect(h.bridge.workspace.state().control.owner).toBe("human");
    const metadata = vi.mocked(h.session.workspaceMetadata);
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(3000);
    expect(metadata).not.toHaveBeenCalled();
    h.send([{id:"heartbeat",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"owner",op:"heartbeat",args:{},expires_at:Date.now()+30000}]);
    await vi.advanceTimersByTimeAsync(500);
    expect(metadata).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500);
    expect(metadata).toHaveBeenCalledTimes(1);
    metadata.mockClear();
    h.session.onWorkspaceChange?.();
    await vi.advanceTimersByTimeAsync(300);
    expect(metadata).toHaveBeenCalled();
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(1000);
    expect(metadata).toHaveBeenCalled();
    h.bridge.stop();
  });

  it("pauses metadata without viewers and resumes for passive viewers, events and reopening", async () => {
    vi.useFakeTimers();
    const h = harness({viewerCount:0});
    await vi.advanceTimersByTimeAsync(600);
    const metadata = vi.mocked(h.session.workspaceMetadata);
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(metadata).not.toHaveBeenCalled();

    // The producer's viewer count includes passive viewers, not only owners.
    h.send([], 1);
    await vi.advanceTimersByTimeAsync(600);
    expect(metadata).toHaveBeenCalled();
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(1200);
    expect(metadata).toHaveBeenCalled();

    h.send([], 0);
    await vi.advanceTimersByTimeAsync(600);
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(metadata).not.toHaveBeenCalled();

    await h.session.runAgentExclusive(async () => {});
    await vi.advanceTimersByTimeAsync(600);
    expect(metadata).toHaveBeenCalled();
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(metadata).not.toHaveBeenCalled();

    h.send([], 1);
    await vi.advanceTimersByTimeAsync(600);
    expect(metadata).toHaveBeenCalled();
    h.bridge.stop();
    metadata.mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(metadata).not.toHaveBeenCalled();
  });
  it("does not turn changing metadata into a self-sustaining no-viewer poll", async () => {
    vi.useFakeTimers();
    const h = harness({viewerCount:0});
    let revision = 0;
    vi.mocked(h.session.workspaceMetadata).mockImplementation(async () => ({
      target_id:"target-1", active_tab_id:"tab1", editors:[],
      tabs:[{id:"tab1",url:"https://example.com",title:String(++revision),current:true}],
      viewport:{mode:"fixed",width:1280,height:800},
    }));
    await vi.advanceTimersByTimeAsync(600);
    vi.mocked(h.session.workspaceMetadata).mockClear();
    await vi.advanceTimersByTimeAsync(10000);
    expect(h.session.workspaceMetadata).not.toHaveBeenCalled();
  });
  it("retains a slower fallback for hosts without viewer counts", async () => {
    vi.useFakeTimers();
    const h = harness();
    await vi.advanceTimersByTimeAsync(600);
    vi.mocked(h.session.workspaceMetadata).mockClear();
    await vi.advanceTimersByTimeAsync(1000);
    expect(h.session.workspaceMetadata).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(h.session.workspaceMetadata).toHaveBeenCalled();
  });
  it("graceful shutdown reports unavailable, then aborts the poll; start is idempotent", async () => {
    const h = harness(); h.bridge.start();
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    expect(h.calls.filter(c=>c.endpoint==="register")).toHaveLength(1);
    await h.bridge.shutdown();
    expect(h.calls.some(c=>c.endpoint==="report" && c.body.state.available===false)).toBe(true);
    expect(h.bridge.workspace.state().control.owner).toBe("agent");
  });
  it("reports at most one payload-carrying result (copy text or list options) per batch", () => {
    const choices = { selected: 0, groups: [], options: [{ label: "A", disabled: false }] };
    const results = [{ id: "a", ok: true }, { id: "b", ok: true, choices }, { id: "c", ok: true }, { id: "d", ok: true, text: "copied" }, { id: "e", ok: true }];
    expect(reportBatch(results).map(r => r.id)).toEqual(["a"]);
    expect(reportBatch(results.slice(1)).map(r => r.id)).toEqual(["b", "c"]);
    expect(reportBatch(results.slice(3)).map(r => r.id)).toEqual(["d", "e"]);
    expect(reportBatch(Array.from({ length: 70 }, (_, i) => ({ id: `r${i}`, ok: true })))).toHaveLength(64);
  });
  it.each([
    ["refused by the server (422)", 50, 422],
    ["refused by the server (413)", 50, 413],
    ["too large to send at all", 1000, undefined],
  ])("turns an option list %s into an error for its viewer instead of resending it", async (_, count, status) => {
    const h = harness({ reportStatus: body => status && body.results.some((r: any) => r.choices) ? status : undefined });
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    // Control characters cost 6 bytes of JSON each: 1000 such labels exceed the body limit.
    const choices = { selected: 0, groups: [], options: Array.from({ length: count }, () => ({ label: "\u0001".repeat(200), disabled: false })) };
    vi.spyOn(h.bridge.workspace, "receive").mockImplementation(async (raw: any) => raw.op === "options" ? { id: raw.id, ok: true, choices } : { id: raw.id, ok: true });
    const command = (id: string, op: string) => ({id,runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"a",op,args:{},expires_at:Date.now()+30000});
    h.send([command("opt", "options"), command("next", "heartbeat")]);
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="report" && c.body.results.some((r:any)=>r.id==="next"))).toBe(true), {timeout:2500});
    const reported = h.calls.filter(c => c.endpoint === "report").flatMap(c => c.body.results);
    expect(reported.filter((r: any) => r.id === "opt").at(-1)).toEqual({ id: "opt", ok: false, error: "This list is too long to show here." });
    // Sent once at most (never, when it could not fit), not retried.
    expect(h.calls.filter(c => c.endpoint === "report" && c.body.results.some((r: any) => r.choices))).toHaveLength(status ? 1 : 0);
  });
  it.each([[429], [408]])("keeps a valid option list and retries it after a transient %i (a proxy, not a refusal)", async status => {
    let refusals = 0;
    const h = harness({ reportStatus: body => body.results.some((r: any) => r.choices) && refusals++ < 2 ? status : undefined });
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    const choices = { selected: 0, groups: [], options: [{ label: "A", disabled: false }] };
    vi.spyOn(h.bridge.workspace, "receive").mockImplementation(async (raw: any) => raw.op === "options" ? { id: raw.id, ok: true, choices } : { id: raw.id, ok: true });
    h.send([{id:"opt",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"a",op:"options",args:{},expires_at:Date.now()+30000}]);
    await vi.waitFor(() => expect(refusals).toBe(3), {timeout:4000});
    const reports = h.calls.filter(c => c.endpoint === "report").flatMap(c => c.body.results).filter((r: any) => r.id === "opt");
    expect(reports).toHaveLength(3);
    for (const r of reports) expect(r).toEqual({ id: "opt", ok: true, choices });
  });
  it("does not replace a valid queued list when a state-only report is refused with 422", async () => {
    let refusals = 0;
    const h = harness({ reportStatus: body => !body.results.some((r: any) => r.choices) && body.results.length && refusals++ < 1 ? 422 : undefined });
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    const choices = { selected: 0, groups: [], options: [{ label: "A", disabled: false }] };
    vi.spyOn(h.bridge.workspace, "receive").mockImplementation(async (raw: any) => raw.op === "options" ? { id: raw.id, ok: true, choices } : { id: raw.id, ok: true });
    const command = (id: string, op: string) => ({id,runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"a",op,args:{},expires_at:Date.now()+30000});
    h.send([command("beat", "heartbeat"), command("opt", "options")]);
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="report" && c.body.results.some((r:any)=>r.id==="opt"))).toBe(true), {timeout:4000});
    const reported = h.calls.filter(c => c.endpoint === "report").flatMap(c => c.body.results).filter((r: any) => r.id === "opt");
    expect(reported).toEqual([{ id: "opt", ok: true, choices }]);
  });
  it("keeps retrying a refused report that carries no payload (the state itself may be at fault)", async () => {
    let refusals = 0;
    const h = harness({ reportStatus: body => body.results.length && refusals++ < 2 ? 422 : undefined });
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    h.send([{id:"take",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"a",op:"takeover",args:{},expires_at:Date.now()+30000}]);
    await vi.waitFor(() => expect(refusals).toBe(3), {timeout:4000});
    const last = h.calls.filter(c => c.endpoint === "report").at(-1)!;
    expect(last.body.results).toEqual([{ id: "take", ok: true }]);
  });
  it("is opt-in and derives control siblings from legacy URL", () => {
    expect(bridgeConfig({})).toBeUndefined();
    expect(bridgeConfig({AIS_SESSION_ID:"s",AIS_PANEL_TOKEN:"t",AIS_PANEL_REGISTER_URL:"http://localhost/api/v1/browser/panel/register"})).toEqual({sessionId:"s",token:"t",baseUrl:"http://localhost/api/v1/browser/panel/control/"});
  });
  it("reports authoritative target and command result while next long poll is pending", async () => {
    const h = harness();
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    const c = {id:"take",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"alice",op:"takeover",args:{},expires_at:Date.now()+30000};
    h.send([c]);
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="report" && c.body.results.some((r:any)=>r.id==="take" && r.ok))).toBe(true));
    expect(h.calls.filter(c=>c.endpoint==="poll")).toHaveLength(2);
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="report" && c.body.state.target_id==="target-1")).toBe(true));
    for (const call of h.calls) {expect(call.body.session_id).toBe("s");expect(call.body.token).toBe("test-token");expect(call.body.runtime_id).toBe(h.bridge.workspace.runtimeId);}
    expect(new Set(h.calls.map(call => call.endpoint))).toEqual(new Set(["register", "poll", "report"]));
    for (const [url, init] of h.fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("origin")).toBe(new URL(String(url)).origin);
      expect(init?.redirect).toBe("error");
    }
  });
  it("retains results across report retry without replaying command", async () => {
    const h = harness({reportFail:true});
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    const receive = vi.spyOn(h.bridge.workspace,"receive");
    h.send([{id:"take",runtime_id:h.bridge.workspace.runtimeId,tab_id:null,viewer_id:"a",op:"takeover",args:{},expires_at:Date.now()+30000}]);
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="report" && c.body.results.some((r:any)=>r.id==="take"))).toBe(true),{timeout:2500});
    expect(receive).toHaveBeenCalledTimes(1);
  });
  it.each([409,401,403])("stops permanently on register HTTP %s", async registerStatus => {
    const h = harness({registerStatus});
    await vi.waitFor(() => expect(h.bridge.workspace.state().available).toBe(false));
    expect(h.calls).toHaveLength(1);
    await expect(h.session.runAgentExclusive(async()=>{})).rejects.toThrow("stopped");
  });
  it("does not re-register a superseded poll runtime", async () => {
    const h = harness({pollStatus:409});
    await vi.waitFor(() => expect(h.bridge.workspace.state().available).toBe(false));
    expect(h.calls.filter(c=>c.endpoint==="register")).toHaveLength(1);
  });
  it("shutdown aborts the active poll and expires ownership", async () => {
    const h = harness();
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    h.bridge.stop(); expect(h.bridge.workspace.state()).toMatchObject({available:false,control:{owner:"agent",pending_viewer_id:null}});
    expect(h.fetcher.mock.calls.find(([u])=>String(u).endsWith("/poll"))![1]!.signal!.aborted).toBe(true);
  });
});
