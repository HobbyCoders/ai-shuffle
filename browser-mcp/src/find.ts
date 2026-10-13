/**
 * browser_find core: a cheap-model semantic element search that keeps the big
 * accessibility tree OUT of the calling agent's context.
 *
 * The tool takes a full interactive snapshot internally, hands it to a small,
 * fast model with the query, and returns only the ≤20 refs that match. The
 * sub-call is best-effort: if there is no auth env, the call times out, the HTTP
 * response is an error, or the model output cannot be parsed, we DEGRADE to a
 * deterministic local text scorer over the same snapshot. The tool never fails
 * because the sub-call failed.
 *
 * Everything here is browser-free and the network client takes an injectable
 * `fetch`, so the whole module is unit-testable without a page or a real API.
 */

import { parseAriaYaml, type SnapNode } from "./snapshot.js";

/** One element pulled out of the snapshot: its ref and its role/name text. */
export interface FindEntry {
  ref: string;
  role: string;
  name: string;
}

/** A returned match. role/name are authoritative (taken from the snapshot). */
export interface FindMatch {
  ref: string;
  role: string;
  name: string;
  reason?: string;
}

export interface FindResult {
  matches: FindMatch[];
  /** true when the semantic sub-call was skipped or failed and we fell back. */
  degraded: boolean;
  /** short human-readable reason present only when degraded. */
  reason?: string;
}

/** The env subset browser_find consults (mirrors the per-session injection). */
export interface FindEnv {
  ANTHROPIC_BASE_URL?: string;
  ANTHROPIC_AUTH_TOKEN?: string;
  ANTHROPIC_API_KEY?: string;
  ANTHROPIC_SMALL_FAST_MODEL?: string;
}

/** Injectable fetch (global fetch in production, a stub in tests). */
export type FetchLike = typeof fetch;

export const FIND_MAX_MATCHES = 20;
export const FIND_TIMEOUT_MS = 4000;
export const FIND_MAX_TOKENS = 800;
export const DEFAULT_SMALL_MODEL = "claude-haiku-4-5-20251001";
export const DEFAULT_BASE_URL = "https://api.anthropic.com";
export const ANTHROPIC_VERSION = "2023-06-01";

export const FIND_SYSTEM_PROMPT =
  "You locate elements in a web page's accessibility tree. You are given a QUERY " +
  "describing what to find and an accessibility tree whose actionable elements " +
  "carry a ref token like [ref=e12]. Return ONLY a JSON array (no prose, no code " +
  'fences) of up to 20 elements that best match the query, most relevant first. ' +
  'Each element is an object {"ref":"e12","role":"button","name":"Submit","reason":"short why"}. ' +
  "Use only refs that appear in the tree. If nothing matches, return [].";

/** Build the user turn: the query followed by the tree. */
export function buildUserMessage(query: string, snapshotText: string): string {
  return `Query: ${query}\n\nAccessibility tree:\n${snapshotText}`;
}

/**
 * Walk a snapshot's rendered YAML (the same text the model sees) and pull out
 * every ref-carrying node with its role and name. Reuses the snapshot parser so
 * the shapes stay in lockstep with what browser_snapshot emits.
 */
export function entriesFromSnapshot(text: string): FindEntry[] {
  const roots = parseAriaYaml(text);
  const out: FindEntry[] = [];
  const walk = (n: SnapNode): void => {
    if (n.ref) out.push({ ref: n.ref, role: n.role, name: n.name });
    n.children.forEach(walk);
  };
  roots.forEach(walk);
  return out;
}

/** Pick the auth header for the sub-call, or null when no credentials exist. */
export function chooseAuthHeaders(env: FindEnv): Record<string, string> | null {
  if (env.ANTHROPIC_AUTH_TOKEN) {
    return { Authorization: `Bearer ${env.ANTHROPIC_AUTH_TOKEN}` };
  }
  if (env.ANTHROPIC_API_KEY) {
    return { "x-api-key": env.ANTHROPIC_API_KEY };
  }
  return null;
}

/**
 * Robustly extract a JSON array from a model's text output: strip code fences,
 * try a direct parse, then fall back to the outermost [...] span so surrounding
 * prose is tolerated. Throws when no array can be recovered.
 */
export function extractJsonArray(raw: string): unknown[] {
  const stripped = raw
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/, "")
    .trim();

  try {
    const v = JSON.parse(stripped);
    if (Array.isArray(v)) return v;
  } catch {
    /* fall through to span extraction */
  }

  const start = stripped.indexOf("[");
  const end = stripped.lastIndexOf("]");
  if (start >= 0 && end > start) {
    try {
      const v = JSON.parse(stripped.slice(start, end + 1));
      if (Array.isArray(v)) return v;
    } catch {
      /* fall through to throw */
    }
  }
  throw new Error("unparseable model output");
}

/**
 * Parse the model's JSON into matches, validating each ref against the snapshot:
 * unknown refs are dropped, role/name are taken from the snapshot (not the model)
 * so they anchor to reality, duplicates are removed, and the list is capped.
 */
export function parseMatches(raw: string, entryByRef: Map<string, FindEntry>): FindMatch[] {
  const arr = extractJsonArray(raw);
  const out: FindMatch[] = [];
  const seen = new Set<string>();
  for (const item of arr) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const ref = rec.ref;
    if (typeof ref !== "string") continue;
    const entry = entryByRef.get(ref);
    if (!entry) continue; // unknown ref → drop
    if (seen.has(ref)) continue;
    seen.add(ref);
    const reason = typeof rec.reason === "string" && rec.reason.trim() !== "" ? rec.reason.trim() : undefined;
    out.push({ ref, role: entry.role, name: entry.name, reason });
    if (out.length >= FIND_MAX_MATCHES) break;
  }
  return out;
}

const TOKEN_RE = /[a-z0-9]+/g;

function tokenize(s: string): string[] {
  return s.toLowerCase().match(TOKEN_RE) ?? [];
}

/**
 * Deterministic local scorer used on the degradation path. Scores each element
 * by token overlap between the query and its role+name, with a bonus for
 * substring hits and for the whole normalized query appearing verbatim. Stable:
 * ties break by original document order, so the ranking is reproducible.
 */
export function scoreDegraded(
  query: string,
  entries: FindEntry[],
  limit = FIND_MAX_MATCHES,
): FindMatch[] {
  const qTokens = tokenize(query);
  const qNorm = qTokens.join(" ");
  const scored = entries.map((entry, index) => {
    const hay = `${entry.role} ${entry.name}`.toLowerCase();
    const hayTokens = new Set(tokenize(hay));
    let score = 0;
    for (const t of qTokens) {
      if (hayTokens.has(t)) score += 2;
      else if (hay.includes(t)) score += 1;
    }
    if (qNorm !== "" && hay.includes(qNorm)) score += 3;
    return { entry, index, score };
  });
  return scored
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, limit)
    .map((s) => ({ ref: s.entry.ref, role: s.entry.role, name: s.entry.name }));
}

/**
 * Perform the semantic sub-call. Throws on timeout (AbortError), any non-2xx
 * response, or unparseable output — the caller ({@link findElements}) turns any
 * throw into a degraded result. Global fetch (Node 20) is injected so tests can
 * stub it.
 */
export async function semanticFind(opts: {
  query: string;
  snapshotText: string;
  entryByRef: Map<string, FindEntry>;
  auth: Record<string, string>;
  env: FindEnv;
  fetchImpl: FetchLike;
  timeoutMs?: number;
}): Promise<FindMatch[]> {
  const base = (opts.env.ANTHROPIC_BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const model = opts.env.ANTHROPIC_SMALL_FAST_MODEL || DEFAULT_SMALL_MODEL;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? FIND_TIMEOUT_MS);
  try {
    const resp = await opts.fetchImpl(`${base}/v1/messages`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
        ...opts.auth,
      },
      body: JSON.stringify({
        model,
        max_tokens: FIND_MAX_TOKENS,
        system: FIND_SYSTEM_PROMPT,
        messages: [{ role: "user", content: buildUserMessage(opts.query, opts.snapshotText) }],
      }),
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`http ${resp.status}`);
    const data = (await resp.json()) as { content?: Array<{ type?: string; text?: string }> };
    const raw = extractResponseText(data);
    return parseMatches(raw, opts.entryByRef);
  } finally {
    clearTimeout(timer);
  }
}

/** Concatenate the text blocks of an Anthropic Messages response. */
export function extractResponseText(data: {
  content?: Array<{ type?: string; text?: string }>;
}): string {
  if (!data || !Array.isArray(data.content)) return "";
  return data.content
    .filter((b) => b && b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .join("");
}

function describeFailure(err: unknown): string {
  if (err instanceof Error) {
    if (err.name === "AbortError") return "timeout";
    return err.message || "error";
  }
  return "error";
}

/**
 * Orchestrator: try the semantic sub-call, degrade to the local scorer on any
 * missing-auth / timeout / HTTP / parse failure. Never throws.
 */
export async function findElements(opts: {
  query: string;
  snapshotText: string;
  entries: FindEntry[];
  env: FindEnv;
  fetchImpl: FetchLike;
  timeoutMs?: number;
}): Promise<FindResult> {
  const degrade = (reason: string): FindResult => ({
    matches: scoreDegraded(opts.query, opts.entries),
    degraded: true,
    reason,
  });

  const auth = chooseAuthHeaders(opts.env);
  if (!auth) return degrade("no credentials configured");

  const entryByRef = new Map(opts.entries.map((e) => [e.ref, e]));
  try {
    const matches = await semanticFind({
      query: opts.query,
      snapshotText: opts.snapshotText,
      entryByRef,
      auth,
      env: opts.env,
      fetchImpl: opts.fetchImpl,
      timeoutMs: opts.timeoutMs,
    });
    return { matches, degraded: false };
  } catch (err) {
    return degrade(describeFailure(err));
  }
}
