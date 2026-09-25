import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  RingBuffer,
  NetworkBuffer,
  cappedConsoleEntry,
  CONSOLE_ENTRY_MAX_CHARS,
} from "../src/buffers.js";
import { compileMatcher, PATTERN_MAX_LEN } from "../src/match.js";
import { parseBatchAction, runBatch, BATCH_MAX_ACTIONS } from "../src/tools/batch.js";
import { resolveDims, RESIZE_PRESET_DIMS } from "../src/tools/resize.js";
import {
  validatePaths,
  isUnderDir,
  uploadTimeoutMs,
  UPLOAD_TIMEOUT_BASE_MS,
  UPLOAD_TIMEOUT_MAX_MS,
} from "../src/tools/upload.js";
import { runNetwork, NETWORK_BODY_MAX_BYTES } from "../src/tools/network.js";
import { raceWithTimeout } from "../src/session.js";
import type { Session } from "../src/session.js";
import type { McpToolResult } from "../src/tools/result.js";
import type { JsonSchema } from "../src/schemas.js";

/** Identity realpath for the pure upload tests (paths are fictional, not on disk). */
const idRealpath = (p: string) => p;

function firstText(res: McpToolResult): string {
  const t = res.content.find((c) => c.type === "text");
  return t && t.type === "text" ? t.text : "";
}

describe("RingBuffer", () => {
  it("drops the oldest past the cap and preserves order", () => {
    const rb = new RingBuffer<number>(3);
    for (const n of [1, 2, 3, 4, 5]) rb.push(n);
    expect(rb.all()).toEqual([3, 4, 5]);
    expect(rb.length).toBe(3);
    rb.clear();
    expect(rb.all()).toEqual([]);
  });
});

describe("NetworkBuffer", () => {
  const mkResp = () => ({}) as unknown as import("playwright-core").Response;
  it("assigns incrementing ids and looks entries up by id", () => {
    const nb = new NetworkBuffer();
    const a = nb.record({ method: "GET", url: "/a", status: 200, resourceType: "document", response: mkResp() });
    const b = nb.record({ method: "POST", url: "/b", status: 201, resourceType: "fetch", response: mkResp() });
    expect(a.id).toBe("req1");
    expect(b.id).toBe("req2");
    expect(nb.find("req2")?.url).toBe("/b");
    expect(nb.find("nope")).toBeUndefined();
  });
  it("keeps ids monotonic even as old entries are evicted", () => {
    const nb = new NetworkBuffer();
    for (let i = 0; i < 600; i++) {
      nb.record({ method: "GET", url: `/${i}`, status: 200, resourceType: "fetch", response: mkResp() });
    }
    // Capacity 500: the oldest were dropped, but ids never rewound.
    expect(nb.length).toBe(500);
    expect(nb.find("req1")).toBeUndefined();
    expect(nb.find("req600")?.url).toBe("/599");
  });
});

describe("compileMatcher", () => {
  it("treats a valid regex as a regex", () => {
    const m = compileMatcher("err?or");
    expect(m("error")).toBe(true);
    expect(m("eror")).toBe(true);
    expect(m("nope")).toBe(false);
    // Regex metachar semantics apply: '.' is any char.
    expect(compileMatcher("a.c")("axc")).toBe(true);
  });
  it("falls back to substring for invalid regex", () => {
    const m = compileMatcher("[unclosed");
    expect(m("has [unclosed here")).toBe(true);
    expect(m("clean")).toBe(false);
  });
  it("treats an over-length pattern as a literal substring (ReDoS guard)", () => {
    // 202 chars, contains regex metachars. As a regex "a." means a + any char;
    // as a literal substring it means the exact text "a.a.a…".
    const literal = "a.".repeat(101);
    expect(literal.length).toBeGreaterThan(PATTERN_MAX_LEN);
    const m = compileMatcher(literal);
    expect(m(literal)).toBe(true); // literal substring present
    expect(m("axax".repeat(60))).toBe(false); // would match as regex, not as substring
  });
  it("still compiles a normal-length regex", () => {
    const m = compileMatcher("^ab.d$");
    expect(m("abcd")).toBe(true);
    expect(m("abd")).toBe(false);
  });
});

describe("cappedConsoleEntry", () => {
  it("truncates text and location longer than the per-entry cap", () => {
    const bigText = "x".repeat(CONSOLE_ENTRY_MAX_CHARS + 500);
    const bigLoc = "http://h/" + "y".repeat(CONSOLE_ENTRY_MAX_CHARS + 500);
    const e = cappedConsoleEntry("log", bigText, bigLoc);
    expect(e.level).toBe("log");
    expect(e.text.length).toBe(CONSOLE_ENTRY_MAX_CHARS);
    expect(e.location.length).toBe(CONSOLE_ENTRY_MAX_CHARS);
  });
  it("leaves short entries untouched", () => {
    const e = cappedConsoleEntry("warn", "hi", "u:1:2");
    expect(e).toEqual({ level: "warn", text: "hi", location: "u:1:2" });
  });
});

describe("raceWithTimeout", () => {
  it("resolves with the promise value when it wins", async () => {
    await expect(raceWithTimeout(Promise.resolve(42), 1000, "too slow")).resolves.toBe(42);
  });
  it("rejects with the timeout message when the promise is too slow", async () => {
    const slow = new Promise<number>((res) => setTimeout(() => res(1), 1000));
    await expect(raceWithTimeout(slow, 5, "eval timed out")).rejects.toThrow("eval timed out");
  });
});

describe("runNetwork body size guard", () => {
  function fakeSession(headers: Record<string, string>, onText: () => string): Session {
    const entry = {
      id: "req1",
      url: "http://h/big",
      status: 200,
      response: { headers: () => headers, text: async () => onText() },
    };
    return {
      ensurePage: async () => undefined,
      findNetworkEntry: (id: string) => (id === "req1" ? entry : undefined),
    } as unknown as Session;
  }

  it("returns a note without reading the body when content-length exceeds the cap", async () => {
    let read = false;
    const session = fakeSession(
      { "content-length": String(NETWORK_BODY_MAX_BYTES + 1) },
      () => {
        read = true;
        return "should not be read";
      },
    );
    const res = await runNetwork(session, { request_id: "req1" });
    const body = JSON.parse(firstText(res));
    expect(read).toBe(false);
    expect(body.note).toMatch(/body too large/);
    expect(body.size).toBe(NETWORK_BODY_MAX_BYTES + 1);
  });

  it("reads a small textual body normally", async () => {
    const session = fakeSession(
      { "content-type": "application/json", "content-length": "5" },
      () => '{"a":1}',
    );
    const res = await runNetwork(session, { request_id: "req1" });
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    expect(body.truncated).toBe(false);
  });
});

describe("parseBatchAction", () => {
  it("parses a well-formed action", () => {
    const { action, error } = parseBatchAction('{"tool":"browser_snapshot","input":{"filter":"all"}}');
    expect(error).toBeUndefined();
    expect(action).toEqual({ tool: "browser_snapshot", input: { filter: "all" } });
  });
  it("defaults input to {}", () => {
    const { action } = parseBatchAction('{"tool":"browser_read_text"}');
    expect(action).toEqual({ tool: "browser_read_text", input: {} });
  });
  it("rejects invalid JSON, non-object, bad tool, and nested batch", () => {
    expect(parseBatchAction("not json").error).toMatch(/valid JSON/);
    expect(parseBatchAction("[]").error).toMatch(/object/);
    expect(parseBatchAction('{"tool":5}').error).toMatch(/tool must be a string/);
    expect(parseBatchAction('{"tool":"browser_batch"}').error).toMatch(/cannot be nested/);
    expect(parseBatchAction('{"tool":"x","input":[]}').error).toMatch(/input must be an object/);
  });
});

describe("runBatch", () => {
  const okTool = {
    inputSchema: { type: "object", properties: {}, additionalProperties: false } as JsonSchema,
    run: async (): Promise<McpToolResult> => ({ content: [{ type: "text", text: '{"ok":true}' }] }),
  };
  const errTool = {
    inputSchema: { type: "object", properties: {}, additionalProperties: false } as JsonSchema,
    run: async (): Promise<McpToolResult> => ({
      content: [{ type: "text", text: '{"ok":false,"error":"boom"}' }],
      isError: true,
    }),
  };
  const session = {} as Session;

  it("runs actions in order and reports each", async () => {
    const res = await runBatch(
      session,
      { actions: ['{"tool":"a"}', '{"tool":"a"}'] },
      () => okTool,
    );
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    expect(body.count).toBe(2);
    expect(body.results.every((r: { ok: boolean }) => r.ok)).toBe(true);
  });

  it("stops at the first failing tool", async () => {
    const lookup = (name: string) => (name === "bad" ? errTool : okTool);
    const res = await runBatch(
      session,
      { actions: ['{"tool":"a"}', '{"tool":"bad"}', '{"tool":"a"}'] },
      lookup,
    );
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(false);
    expect(body.count).toBe(2); // third action never ran
    expect(body.results[1].ok).toBe(false);
  });

  it("stops on an unknown tool", async () => {
    const res = await runBatch(session, { actions: ['{"tool":"ghost"}'] }, () => undefined);
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(false);
    expect(body.results[0].error).toMatch(/unknown tool/);
  });

  it("validates each action's input against the tool schema", async () => {
    const strictTool = {
      inputSchema: {
        type: "object",
        properties: { n: { type: "integer" } },
        required: ["n"],
        additionalProperties: false,
      } as JsonSchema,
      run: okTool.run,
    };
    const res = await runBatch(session, { actions: ['{"tool":"s","input":{}}'] }, () => strictTool);
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(false);
    expect(body.results[0].error).toMatch(/missing required/);
  });

  it("normalizes optional placeholders before dispatching a nested tool", async () => {
    let received: Record<string, unknown> | undefined;
    const tool = {
      inputSchema: {
        type: "object",
        properties: {
          kind: { type: "string" },
          ref: { type: "string" },
          coordinate: { type: "array", items: { type: "number" } },
        },
        required: ["kind"],
        additionalProperties: false,
      } as JsonSchema,
      run: async (_session: Session, input: Record<string, unknown>): Promise<McpToolResult> => {
        received = input;
        return { content: [{ type: "text", text: '{"ok":true}' }] };
      },
    };

    const res = await runBatch(
      session,
      {
        actions: [
          '{"tool":"browser_act","input":{"kind":"hover","ref":"e6","coordinate":[]}}',
        ],
      },
      () => tool,
    );

    expect(res.isError).not.toBe(true);
    expect(received).toEqual({ kind: "hover", ref: "e6" });
  });

  it("rejects empty and over-cap action lists", async () => {
    expect((await runBatch(session, { actions: [] }, () => okTool)).isError).toBe(true);
    const many = Array.from({ length: BATCH_MAX_ACTIONS + 1 }, () => '{"tool":"a"}');
    expect((await runBatch(session, { actions: many }, () => okTool)).isError).toBe(true);
  });

  it("re-emits image blocks from sub-results (screenshot inside a batch)", async () => {
    const imageTool = {
      inputSchema: okTool.inputSchema,
      run: async (): Promise<McpToolResult> => ({
        content: [
          { type: "image", data: "aGVsbG8=", mimeType: "image/jpeg" },
          { type: "text", text: "1280x720 — coordinates for browser_act must use this space" },
        ],
      }),
    };
    const lookup = (name: string) => (name === "shot" ? imageTool : okTool);
    const res = await runBatch(
      session,
      { actions: ['{"tool":"a"}', '{"tool":"shot"}'] },
      lookup,
    );
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    const images = res.content.filter((c) => c.type === "image");
    expect(images).toEqual([{ type: "image", data: "aGVsbG8=", mimeType: "image/jpeg" }]);
    // summary text still comes from the sub-result's text part
    expect(body.results[1].summary).toMatch(/1280x720/);
  });
});

describe("resize.resolveDims", () => {
  it("resolves presets", () => {
    expect(resolveDims({ preset: "mobile" })).toEqual(RESIZE_PRESET_DIMS.mobile);
    expect(resolveDims({ preset: "desktop" })).toEqual({ width: 1280, height: 800 });
  });
  it("prefers explicit width+height", () => {
    expect(resolveDims({ width: 900, height: 700 })).toEqual({ width: 900, height: 700 });
    expect(resolveDims({ preset: "mobile", width: 900, height: 700 })).toEqual({ width: 900, height: 700 });
  });
  it("requires a full pair or a preset", () => {
    expect(resolveDims({ width: 900 })).toMatch(/both/);
    expect(resolveDims({})).toMatch(/preset/);
  });
});

describe("upload path allowlist", () => {
  const dir = "/srv/uploads";
  const stat = (size: number, isFile = true) => () => ({ isFile: () => isFile, size });

  it("isUnderDir accepts files inside, rejects the dir itself and escapes", () => {
    expect(isUnderDir("/srv/uploads/a.png", dir)).toBe(true);
    expect(isUnderDir("/srv/uploads", dir)).toBe(false);
    expect(isUnderDir("/srv/uploads/../secret", dir)).toBe(false);
    expect(isUnderDir("/etc/passwd", dir)).toBe(false);
  });

  it("rejects everything when BROWSER_UPLOAD_DIR is unset", () => {
    const r = validatePaths(["/srv/uploads/a.png"], undefined, stat(10), idRealpath);
    expect("error" in r && r.error).toMatch(/BROWSER_UPLOAD_DIR is not set/);
  });

  it("accepts an in-dir regular file", () => {
    const r = validatePaths(["/srv/uploads/a.png"], dir, stat(1234), idRealpath);
    expect("paths" in r && r.paths).toEqual(["/srv/uploads/a.png"]);
  });

  it.each([10 * 1024 * 1024, 10 * 1024 * 1024 + 1, 100 * 1024 * 1024, 5 * 1024 ** 3])(
    "accepts an in-dir file of %i bytes without a size cap",
    (size) => {
      expect(validatePaths(["/srv/uploads/big.bin"], dir, stat(size), idRealpath)).toEqual({
        paths: ["/srv/uploads/big.bin"],
        totalBytes: size,
      });
    },
  );

  it("sums sizes across files into totalBytes", () => {
    const r = validatePaths(["/srv/uploads/a", "/srv/uploads/b"], dir, stat(1024), idRealpath);
    expect(r).toEqual({ paths: ["/srv/uploads/a", "/srv/uploads/b"], totalBytes: 2048 });
  });

  it("scales the setInputFiles timeout with total size, capped at 5 minutes", () => {
    const MiB = 1024 * 1024;
    expect(uploadTimeoutMs(0)).toBe(UPLOAD_TIMEOUT_BASE_MS);
    expect(uploadTimeoutMs(-1)).toBe(UPLOAD_TIMEOUT_BASE_MS);
    expect(uploadTimeoutMs(1 * MiB)).toBe(32_000);
    expect(uploadTimeoutMs(27 * MiB)).toBe(84_000);
    expect(uploadTimeoutMs(MiB / 2)).toBe(31_000);
    expect(uploadTimeoutMs(135 * MiB)).toBe(UPLOAD_TIMEOUT_MAX_MS);
    expect(uploadTimeoutMs(5 * 1024 * MiB)).toBe(UPLOAD_TIMEOUT_MAX_MS);
    expect(UPLOAD_TIMEOUT_MAX_MS).toBe(300_000);
  });

  it("rejects out-of-dir paths regardless of size", () => {
    const result = validatePaths(["/etc/passwd"], dir, stat(100 * 1024 * 1024), idRealpath);
    expect("error" in result && result.error).toMatch(/must be under BROWSER_UPLOAD_DIR/);
  });

  it("rejects non-regular files", () => {
    const result = validatePaths(["/srv/uploads/subdir"], dir, stat(0, false), idRealpath);
    expect("error" in result && result.error).toMatch(/not a regular file/);
  });

  it("rejects an empty path list", () => {
    const r = validatePaths([], dir, stat(1), idRealpath);
    expect("error" in r && r.error).toMatch(/not be empty/);
  });

  it("checks the allowlist on the REAL path (a symlink escaping the dir is rejected)", () => {
    // Fake realpath: the in-dir symlink resolves to a path OUTSIDE the dir.
    const realpath = (p: string) =>
      p === "/srv/uploads/escape.png" ? "/etc/shadow" : p === dir ? dir : p;
    const r = validatePaths(["/srv/uploads/escape.png"], dir, stat(10), realpath);
    expect("error" in r && r.error).toMatch(/must be under BROWSER_UPLOAD_DIR/);
  });
});

describe("upload symlink safety (real fs)", () => {
  it("rejects a symlink inside the dir pointing outside, accepts a real in-dir file", () => {
    const root = mkdtempSync(join(tmpdir(), "upload-test-"));
    const uploadDir = join(root, "uploads");
    const outsideDir = join(root, "outside");
    let madeSymlink = false;
    try {
      mkdirSync(uploadDir);
      mkdirSync(outsideDir);
      const goodFile = join(uploadDir, "ok.txt");
      const secret = join(outsideDir, "secret.txt");
      writeFileSync(goodFile, "hello");
      writeFileSync(secret, "top secret");
      const linkInside = join(uploadDir, "escape.txt");
      try {
        symlinkSync(secret, linkInside);
        madeSymlink = true;
      } catch {
        // Platform without symlink support — skip the symlink assertion.
      }

      // A genuine file inside the dir is accepted (real realpath/stat).
      const ok = validatePaths([goodFile], uploadDir);
      expect("paths" in ok).toBe(true);

      if (madeSymlink) {
        const escaped = validatePaths([linkInside], uploadDir);
        expect("error" in escaped && escaped.error).toMatch(/must be under BROWSER_UPLOAD_DIR/);
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
