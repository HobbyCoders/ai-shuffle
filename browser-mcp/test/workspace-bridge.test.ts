import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceBridge, bridgeConfig } from "../src/workspace-bridge.js";
import { Session } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";
const bridges: WorkspaceBridge[] = [];
afterEach(() => { bridges.forEach(b => b.stop()); bridges.length = 0; vi.restoreAllMocks(); });
const json = (data: unknown) => new Response(JSON.stringify(data), {headers:{"content-type":"application/json"}});
function harness(options: { registerStatus?: number; reportFail?: boolean; pollStatus?: number } = {}) {
  const calls: {endpoint:string;body:any}[] = [];
  let deliver: ((commands: unknown[]) => void) | undefined;
  let failedReport = false;
  const fetcher = vi.fn(async (url: URL | RequestInfo, init?: RequestInit) => {
    const endpoint = String(url).split("/").at(-1)!;
    const body = JSON.parse(init!.body as string); calls.push({endpoint,body});
    if (endpoint === "register") return options.registerStatus ? new Response(null,{status:options.registerStatus}) : json({ok:true});
    if (endpoint === "report") {
      if (options.reportFail && !failedReport) {failedReport=true; return new Response(null,{status:503});}
      return new Response(null,{status:204});
    }
    if (options.pollStatus) return new Response(null,{status:options.pollStatus});
    return new Promise<Response>((resolve,reject) => {
      const aborted = () => { deliver=undefined; reject(new Error("aborted")); };
      init!.signal!.addEventListener("abort",aborted,{once:true});
      deliver = commands => {init!.signal!.removeEventListener("abort",aborted); deliver=undefined; resolve(json({commands}));};
    });
  });
  const session = new Session(new BrowserManager());
  vi.spyOn(session, "workspaceMetadata").mockResolvedValue({target_id:"target-1",active_tab_id:"tab1",tabs:[{id:"tab1",url:"https://example.com",title:"fixture",current:true}],viewport:{mode:"fixed",width:1280,height:800}});
  const bridge = new WorkspaceBridge(session,{sessionId:"s",token:"test-token",baseUrl:"http://localhost/api/v1/browser/panel/control/"},fetcher as typeof fetch);
  bridges.push(bridge); bridge.start();
  return {session,bridge,calls,fetcher,send:(commands:unknown[]) => { if (!deliver) throw new Error("no poll"); deliver(commands); }};
}
describe("runtime bridge", () => {
  it("graceful shutdown reports unavailable, then aborts the poll; start is idempotent", async () => {
    const h = harness(); h.bridge.start();
    await vi.waitFor(() => expect(h.calls.some(c=>c.endpoint==="poll")).toBe(true));
    expect(h.calls.filter(c=>c.endpoint==="register")).toHaveLength(1);
    await h.bridge.shutdown();
    expect(h.calls.some(c=>c.endpoint==="report" && c.body.state.available===false)).toBe(true);
    expect(h.bridge.workspace.state().control.owner).toBe("agent");
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
