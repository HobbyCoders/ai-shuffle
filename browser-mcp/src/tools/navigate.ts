/**
 * browser_navigate — url or "back"/"forward". Doubles as session bootstrap: the
 * first call acquires the page. Returns {url, title} plus an interactive-filtered
 * snapshot so the model has an immediate map of the landing page.
 */

import type { Session } from "../session.js";
import { runWait } from "./wait.js";
import { contentWarning, jsonResult, type McpToolResult } from "./result.js";
import {
  EGRESS_REFUSAL_HEADER,
  EGRESS_REFUSED_NET_ERRORS,
  RETRYABLE_NET_ERRORS,
  behindEgressProxy,
  egressRefusal,
  isLoopbackUrl,
  loopbackGuidance,
  loopbackHost,
  rewriteLoopback,
  tunnelRefusal,
} from "../loopback.js";

/** Extract the host from a URL string, or "" if it has none / is unparseable. */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

/** Extract the origin from a URL string, or "" if it is unparseable. */
function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

/**
 * A cross-origin landing is the shape of an auth wall: the browser carries no
 * session cookies for the operator's own apps, so an app URL that answers with
 * a redirect to an identity provider is a sign-in demand, not a page. It still
 * reports ok:true (the navigation did happen), so the flag is what makes it
 * visible without the model having to diff hostnames itself.
 */
export const AUTH_WALL_NOTE =
  "landed on a different origin than requested — usually a sign-in redirect. " +
  "This browser holds no session cookies for your apps, so it cannot log itself " +
  "in; ask the user to sign in through the live browser panel, or use an " +
  "endpoint that does not need a session.";

/**
 * Turn a failed navigation into the most actionable error we can state.
 *
 * Loopback comes first: it is the failure an agent hits while trying to look at
 * a server it just started itself, and both shapes of it (refused by policy,
 * refused by the network) have the same remedy. Otherwise the SSRF guard's own
 * reason replaces Chromium's opaque ERR_BLOCKED_BY_CLIENT.
 */
function navigationFailure(session: Session, requested: string, err: unknown): Error {
  const message = err instanceof Error ? err.message : String(err);
  const host = hostOf(requested);
  if (EGRESS_REFUSED_NET_ERRORS.test(message)) {
    return new Error(
      behindEgressProxy()
        ? egressRefusal(requested, host ? session.ssrfBlockReason(host) : undefined)
        : tunnelRefusal(requested, message),
    );
  }
  if (!RETRYABLE_NET_ERRORS.test(message)) return err instanceof Error ? err : new Error(message);
  const reason = host ? session.ssrfBlockReason(host) : undefined;
  if (isLoopbackUrl(requested)) {
    return new Error(loopbackGuidance(requested, loopbackHost(), reason));
  }
  if (reason) return new Error(`blocked by SSRF policy: ${reason}`);
  return err instanceof Error ? err : new Error(message);
}

/**
 * Schemes browser_navigate opens. Browser-internal schemes (chrome:,
 * chrome-untrusted:, devtools:, chrome-extension:, view-source:, javascript:,
 * …) are refused: they expose the browser's own settings, command line and
 * profile rather than a web page, and no agent task needs them.
 */
const NAVIGABLE_SCHEMES = new Set(["http:", "https:", "file:", "about:", "data:"]);

/** Refusal message for a URL whose scheme browser_navigate will not open, else undefined. */
export function refusedScheme(url: string): string | undefined {
  let protocol: string;
  try {
    protocol = new URL(url).protocol.toLowerCase();
  } catch {
    return undefined; // let page.goto report the malformed URL itself
  }
  if (NAVIGABLE_SCHEMES.has(protocol)) return undefined;
  return `refusing to open a ${protocol} URL: browser_navigate opens http(s), file, about and data URLs only`;
}

export function normalizeUrl(input: string): string {
  const trimmed = input.trim();
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed)) return trimmed;
  if (trimmed.startsWith("about:") || trimmed.startsWith("data:")) return trimmed;
  // Bare domain / path → assume https.
  return `https://${trimmed}`;
}

export async function runNavigate(
  session: Session,
  input: { url: string },
): Promise<McpToolResult> {
  const target = input.url.trim().toLowerCase();
  if (target !== "back" && target !== "forward") {
    const refused = refusedScheme(normalizeUrl(input.url));
    if (refused) throw new Error(refused);
  }
  const page = await session.ensurePage();

  // A committed navigation invalidates all refs.
  session.notifyNavigation();

  let rewroteTo: string | undefined;
  if (target === "back" || target === "forward") {
    await (target === "back"
      ? page.goBack({ waitUntil: "domcontentloaded" })
      : page.goForward({ waitUntil: "domcontentloaded" }));
  } else {
    const requested = normalizeUrl(input.url);
    const refusal = (url: string) => {
      const host = hostOf(url);
      return new Error(egressRefusal(url, host ? session.ssrfBlockReason(host) : undefined));
    };
    // A refused tunnel is policy only behind the app's egress proxy; behind
    // anything else it is one more way for nothing to answer.
    const policyRefused = (message: string) =>
      behindEgressProxy() && EGRESS_REFUSED_NET_ERRORS.test(message);
    const retryable = (message: string) =>
      RETRYABLE_NET_ERRORS.test(message) ||
      (!behindEgressProxy() && EGRESS_REFUSED_NET_ERRORS.test(message));
    let response: Awaited<ReturnType<typeof page.goto>> | undefined;
    try {
      response = await page.goto(requested, { waitUntil: "domcontentloaded" });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const alternative = rewriteLoopback(requested, loopbackHost());
      if (alternative && retryable(message)) {
        // The shell's loopback is not the browser's. One retry against the
        // address the app says this host answers on, then give up loudly.
        try {
          response = await page.goto(alternative, { waitUntil: "domcontentloaded" });
          rewroteTo = alternative;
        } catch (retryErr) {
          // The proxy refusing the alternative is the answer, not the first
          // attempt's "nothing answered".
          if (policyRefused(retryErr instanceof Error ? retryErr.message : String(retryErr))) {
            throw refusal(alternative);
          }
          throw navigationFailure(session, requested, err);
        }
      } else {
        throw navigationFailure(session, requested, err);
      }
    }
    // A plain-http refusal is the proxy's own 403 page, a "successful"
    // navigation (the retry's included); its marker header says it is
    // policy, not the site.
    if (response?.headers()[EGRESS_REFUSAL_HEADER] === "blocked") {
      throw refusal(rewroteTo ?? requested);
    }
  }

  // DOMContentLoaded can precede hydration. A landmark can have a ref before
  // controls exist, so ref_count alone is not a readiness check. Bound this
  // wait, never wait for network idle, and keep human takeover interruptible.
  await runWait(session, {
    condition: "element",
    selector: 'button:visible, a[href]:visible, input:visible, select:visible, textarea:visible, [role="button"]:visible, [role="link"]:visible, [role="textbox"]:visible, [tabindex]:visible, [contenteditable="true"]:visible',
    timeout_ms: 1500,
  });
  session.agentAdmission?.();
  const snap = await session.snapshot({ filter: "interactive" });
  // Emptiness is judged from the page, never from this interactive-filtered
  // snapshot: a login form, a canvas fixture and a prose page all have few or
  // no refs while being perfectly rendered.
  // Only zero-ref snapshots need the extra content diagnostic. Refs are not
  // proof that hydration finished or that the requested app view succeeded.
  const content = snap.refs.length === 0 ? await session.pageContent() : undefined;
  // The title comes from Playwright, which does not depend on a page eval
  // succeeding; only the body measurement does.
  const title = await page.title().catch(() => content?.title ?? "");
  const warning = content
    ? contentWarning({ ...content, title }, snap.refs.length)
    : undefined;
  const landed = page.url();
  const requestedOrigin =
    target === "back" || target === "forward"
      ? ""
      : originOf(rewroteTo ?? normalizeUrl(input.url));
  const landedOrigin = originOf(landed);
  const redirected =
    requestedOrigin !== "" && landedOrigin !== "" && requestedOrigin !== landedOrigin;
  const rewriteNote = rewroteTo
    ? `the browser cannot reach your shell's loopback, so this opened ${rewroteTo} instead — use that address for this server from now on`
    : undefined;
  const notes = [
    ...(rewriteNote ? [rewriteNote] : []),
    ...(redirected ? [AUTH_WALL_NOTE] : []),
    ...(warning ? [warning] : []),
  ];
  return jsonResult(
    {
      ok: true,
      url: landed,
      title,
      snapshot_id: snap.snapshot_id,
      ref_count: snap.refs.length,
      truncated: snap.truncated,
      ...(rewroteTo ? { rewrote_loopback_to: rewroteTo, note: rewriteNote } : {}),
      ...(redirected ? { redirected_to_origin: landedOrigin } : {}),
      ...(warning ? { warning } : {}),
    },
    notes.length ? `${snap.text}\n[warning] ${notes.join(" ")}`.trim() : snap.text,
  );
}
