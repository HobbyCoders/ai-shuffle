import { describe, it, expect, vi, afterEach } from "vitest";
import {
  chooseAuthHeaders,
  extractJsonArray,
  parseMatches,
  scoreDegraded,
  extractResponseText,
  entriesFromSnapshot,
  findElements,
  FIND_MAX_MATCHES,
  type FindEntry,
  type FetchLike,
} from "../src/find.js";

const entries: FindEntry[] = [
  { ref: "e1", role: "button", name: "Submit order" },
  { ref: "e2", role: "link", name: "Home" },
  { ref: "e3", role: "textbox", name: "Search products" },
  { ref: "e4", role: "button", name: "Cancel" },
];
const byRef = new Map(entries.map((e) => [e.ref, e]));

/** A stub Response carrying an Anthropic Messages body. */
function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as unknown as Response;
}

function messages(text: string): unknown {
  return { content: [{ type: "text", text }] };
}

describe("extractJsonArray", () => {
  it("parses a bare JSON array", () => {
    expect(extractJsonArray('[{"ref":"e1"}]')).toEqual([{ ref: "e1" }]);
  });
  it("strips ```json code fences", () => {
    const raw = '```json\n[{"ref":"e1","role":"button"}]\n```';
    expect(extractJsonArray(raw)).toEqual([{ ref: "e1", role: "button" }]);
  });
  it("tolerates surrounding prose", () => {
    const raw = 'Sure! Here are the matches:\n[{"ref":"e2"}]\nHope that helps.';
    expect(extractJsonArray(raw)).toEqual([{ ref: "e2" }]);
  });
  it("returns an empty array for []", () => {
    expect(extractJsonArray("[]")).toEqual([]);
  });
  it("throws on genuinely malformed output", () => {
    expect(() => extractJsonArray("not json at all {")).toThrow(/unparseable/);
    expect(() => extractJsonArray("")).toThrow(/unparseable/);
  });
});

describe("parseMatches", () => {
  it("keeps known refs and takes role/name from the snapshot", () => {
    // Model lies about role/name; we override from the snapshot entry.
    const raw = '[{"ref":"e1","role":"wrong","name":"nope","reason":"the CTA"}]';
    expect(parseMatches(raw, byRef)).toEqual([
      { ref: "e1", role: "button", name: "Submit order", reason: "the CTA" },
    ]);
  });
  it("drops unknown refs the model hallucinated", () => {
    const raw = '[{"ref":"e1"},{"ref":"e999"},{"ref":"e3"}]';
    expect(parseMatches(raw, byRef).map((m) => m.ref)).toEqual(["e1", "e3"]);
  });
  it("de-duplicates repeated refs and skips non-object / ref-less items", () => {
    const raw = '[{"ref":"e1"},{"ref":"e1"},"junk",{"foo":1},{"ref":"e2"}]';
    expect(parseMatches(raw, byRef).map((m) => m.ref)).toEqual(["e1", "e2"]);
  });
  it("omits blank reasons and caps at FIND_MAX_MATCHES", () => {
    const big = new Map<string, FindEntry>();
    const items: string[] = [];
    for (let i = 1; i <= FIND_MAX_MATCHES + 5; i++) {
      big.set(`e${i}`, { ref: `e${i}`, role: "link", name: `L${i}` });
      items.push(`{"ref":"e${i}","reason":"  "}`);
    }
    const out = parseMatches(`[${items.join(",")}]`, big);
    expect(out).toHaveLength(FIND_MAX_MATCHES);
    expect(out[0].reason).toBeUndefined();
  });
});

describe("scoreDegraded", () => {
  it("ranks the best textual match first and is deterministic", () => {
    const a = scoreDegraded("submit order button", entries);
    const b = scoreDegraded("submit order button", entries);
    expect(a).toEqual(b); // deterministic
    expect(a[0].ref).toBe("e1"); // "Submit order" + role button wins
  });
  it("matches on role and name tokens, excludes zero-score entries", () => {
    const out = scoreDegraded("search", entries);
    expect(out.map((m) => m.ref)).toEqual(["e3"]); // only "Search products"
  });
  it("breaks ties by document order", () => {
    // Two buttons, query only hits the role token → equal score → order by index.
    const out = scoreDegraded("button", entries);
    expect(out.map((m) => m.ref)).toEqual(["e1", "e4"]);
  });
  it("returns nothing when there is no overlap", () => {
    expect(scoreDegraded("nonexistent zzz", entries)).toEqual([]);
  });
});

describe("chooseAuthHeaders", () => {
  it("prefers the bearer token (gateway/provider sessions)", () => {
    expect(chooseAuthHeaders({ ANTHROPIC_AUTH_TOKEN: "tok", ANTHROPIC_API_KEY: "key" })).toEqual({
      Authorization: "Bearer tok",
    });
  });
  it("falls back to x-api-key", () => {
    expect(chooseAuthHeaders({ ANTHROPIC_API_KEY: "key" })).toEqual({ "x-api-key": "key" });
  });
  it("returns null with no credentials", () => {
    expect(chooseAuthHeaders({})).toBeNull();
  });
});

describe("extractResponseText", () => {
  it("concatenates text blocks and ignores non-text", () => {
    expect(
      extractResponseText({ content: [{ type: "text", text: "a" }, { type: "tool_use" }, { type: "text", text: "b" }] }),
    ).toBe("ab");
  });
  it("is empty for a malformed body", () => {
    expect(extractResponseText({} as { content?: [] })).toBe("");
  });
});

describe("entriesFromSnapshot", () => {
  it("pulls ref/role/name out of rendered snapshot YAML", () => {
    const text = [
      '- navigation "Primary":',
      '  - link "Home" [ref=e2]',
      '- button "Submit order" [ref=e1]',
      "- paragraph: not interactive",
    ].join("\n");
    expect(entriesFromSnapshot(text)).toEqual([
      { ref: "e2", role: "link", name: "Home" },
      { ref: "e1", role: "button", name: "Submit order" },
    ]);
  });
});

describe("findElements", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const base = {
    query: "submit",
    snapshotText: '- button "Submit order" [ref=e1]',
    entries,
  };

  it("degrades (no network call) when no credentials are configured", async () => {
    const fetchImpl = vi.fn();
    const res = await findElements({ ...base, env: {}, fetchImpl: fetchImpl as unknown as FetchLike });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(res.degraded).toBe(true);
    expect(res.reason).toMatch(/no credentials/);
    expect(res.matches[0].ref).toBe("e1"); // local scorer still finds it
  });

  it("uses the semantic result on success and selects the bearer header", async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      expect(headers.Authorization).toBe("Bearer tok");
      expect(headers["anthropic-version"]).toBe("2023-06-01");
      return jsonResponse(messages('[{"ref":"e3","reason":"the field"}]'));
    });
    const res = await findElements({
      ...base,
      env: { ANTHROPIC_AUTH_TOKEN: "tok" },
      fetchImpl: fetchImpl as unknown as FetchLike,
    });
    expect(res.degraded).toBe(false);
    expect(res.matches).toEqual([{ ref: "e3", role: "textbox", name: "Search products", reason: "the field" }]);
  });

  it("degrades on an HTTP error", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}, false, 500));
    const res = await findElements({
      ...base,
      env: { ANTHROPIC_API_KEY: "key" },
      fetchImpl: fetchImpl as unknown as FetchLike,
    });
    expect(res.degraded).toBe(true);
    expect(res.reason).toMatch(/http 500/);
    expect(res.matches[0].ref).toBe("e1");
  });

  it("degrades on unparseable model output", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(messages("I could not find anything useful.")));
    const res = await findElements({
      ...base,
      env: { ANTHROPIC_API_KEY: "key" },
      fetchImpl: fetchImpl as unknown as FetchLike,
    });
    expect(res.degraded).toBe(true);
    expect(res.reason).toMatch(/unparseable/);
  });

  it("times out via AbortController and degrades with reason 'timeout'", async () => {
    vi.useFakeTimers();
    // A fetch that only settles when the abort signal fires.
    const fetchImpl = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted", "AbortError")),
          );
        }),
    );
    const p = findElements({
      ...base,
      env: { ANTHROPIC_API_KEY: "key" },
      fetchImpl: fetchImpl as unknown as FetchLike,
      timeoutMs: 4000,
    });
    await vi.advanceTimersByTimeAsync(4000);
    const res = await p;
    expect(res.degraded).toBe(true);
    expect(res.reason).toBe("timeout");
    expect(res.matches[0].ref).toBe("e1");
  });
});
