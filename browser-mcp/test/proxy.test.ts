import { describe, it, expect, vi } from "vitest";
import { forwardTool } from "../src/proxy.js";

const env = { AIS_SESSION_ID: "session", AIS_PANEL_TOKEN: "private-token", AIS_PANEL_REGISTER_URL: "http://127.0.0.1:9999/api/v1/browser/panel/register" };

describe("shared runtime proxy", () => {
  it("forwards upload preflight to the owned runtime, never deriving it from proxy env", async () => {
    vi.stubEnv("BROWSER_UPLOAD_DIR", undefined);
    try {
      const result = { content: [{ type: "text", text: JSON.stringify({ uploads: { source: "browser_runtime", configured: true, enabled: true, check: { eligible: true } } }) }] };
      const transport = vi.fn(async () => new Response(JSON.stringify(result)));
      const args = { upload_paths: ["/workspace/bundle.aab"] };
      expect(await forwardTool("browser_status", args, env, transport)).toEqual(result);
      const [, options] = transport.mock.calls[0] as unknown as [URL, RequestInit];
      expect(JSON.parse(options.body as string).arguments).toEqual(args);
    } finally { vi.unstubAllEnvs(); }
  });

  it("forwards the complete MCP result unchanged", async () => {
    const result = { content: [{ type: "image", data: "YQ==", mimeType: "image/png" }], isError: true, structuredContent: { exact: [1, 2] }, _meta: { value: 1 } };
    const transport = vi.fn(async () => new Response(JSON.stringify(result)));
    expect(await forwardTool("browser_screenshot", { full_page: true }, env, transport)).toEqual(result);
    const [url, options] = transport.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/api/v1/browser/panel/rpc");
    expect(JSON.parse(options.body as string)).toEqual({ session_id: "session", token: "private-token", tool: "browser_screenshot", arguments: { full_page: true } });
    expect(options.redirect).toBe("error");
    expect(new Headers(options.headers).get("origin")).toBe(url.origin);
  });
  it.each([
    "http://localhost:8000",
    "https://worker.example:8443",
    "http://[::1]:8000",
  ])("uses the configured worker origin %s, not the navigation destination", async (origin) => {
    const transport = vi.fn(async () => new Response(JSON.stringify({ content: [] })));
    await forwardTool("browser_navigate", { url: "https://example.com" }, {
      ...env, AIS_PANEL_REGISTER_URL: `${origin}/api/v1/browser/panel/register?ignored=1#ignored`,
    }, transport);
    const [url, options] = transport.mock.calls[0] as unknown as [URL, RequestInit];
    expect(new Headers(options.headers).get("origin")).toBe(origin);
    expect(url.search).toBe("");
    expect(url.hash).toBe("");
  });
  it("does not leak server errors, tokens, or replay failed mutations", async () => {
    const transport = vi.fn(async () => { throw new Error("private-token"); });
    const result = await forwardTool("browser_navigate", {}, env, transport);
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result)).not.toContain("private-token");
    expect(transport).toHaveBeenCalledTimes(1);
  });
  it("bounds response bytes", async () => {
    const transport = vi.fn(async () => new Response("x".repeat(16 * 1024 * 1024 + 1)));
    expect((await forwardTool("browser_screenshot", {}, env, transport)).isError).toBe(true);
  });
  it("bounds request size before transport", async () => {
    const transport = vi.fn();
    expect((await forwardTool("browser_eval", { code: "x".repeat(270000) }, env, transport)).isError).toBe(true);
    expect(transport).not.toHaveBeenCalled();
  });
});
