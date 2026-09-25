/**
 * Loopback reachability.
 *
 * The shell an agent runs commands in and the browser this server drives are
 * often not the same network namespace: on a shared runner the shell is a
 * container and Chrome lives elsewhere. So a dev server the agent just started
 * on `http://localhost:5179` is real, reachable, and completely invisible to
 * `browser_navigate` — the request either dies with ERR_CONNECTION_REFUSED
 * (loopback allowed, wrong namespace) or is refused by the SSRF policy.
 *
 * The app knows the address the shell's host answers on and passes it as
 * BROWSER_LOOPBACK_HOST. With it we retry the same URL against that address
 * instead of handing the model a dead end; without it we at least say, in the
 * first sentence, the exact URL to try instead.
 */

/** Non-numeric loopback names Chromium resolves to 127.0.0.1 / ::1. */
const LOOPBACK_NAMES = new Set(["localhost", "ip6-localhost", "ip6-loopback"]);

/** True for a host that names this machine and nothing beyond it. */
export function isLoopbackHost(host: string): boolean {
  const bare = host.replace(/^\[|\]$/g, "").toLowerCase();
  if (LOOPBACK_NAMES.has(bare)) return true;
  if (bare === "::1" || bare === "0:0:0:0:0:0:0:1") return true;
  if (bare === "0.0.0.0") return true;
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(bare);
}

/** The URL's host, or "" when it cannot be parsed. */
function hostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

export function isLoopbackUrl(url: string): boolean {
  const host = hostname(url);
  return host !== "" && isLoopbackHost(host);
}

/** The configured stand-in for loopback, or undefined when there is none. */
export function loopbackHost(env: NodeJS.ProcessEnv = process.env): string | undefined {
  const value = (env.BROWSER_LOOPBACK_HOST ?? "").trim();
  if (!value || isLoopbackHost(value)) return undefined;
  return value;
}

/**
 * The same URL with its loopback host swapped for `host`, or undefined when
 * there is nothing to rewrite (not loopback, no host configured, unparseable).
 */
export function rewriteLoopback(url: string, host: string | undefined): string | undefined {
  if (!host || !isLoopbackUrl(url)) return undefined;
  try {
    const parsed = new URL(url);
    parsed.hostname = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
    return parsed.href;
  } catch {
    return undefined;
  }
}

/**
 * What to tell a model whose loopback navigation failed. The action comes
 * first: an audit found the useful sentence buried at the end of a long policy
 * string, and the one session that read it still gave up after one attempt.
 */
export function loopbackGuidance(
  url: string,
  host: string | undefined,
  reason?: string,
): string {
  const alternative = rewriteLoopback(url, host);
  const action = alternative
    ? `Browse ${alternative} instead — same server, an address the browser can route to.`
    : "Bind the server to 0.0.0.0 and browse it by IP instead: run `hostname -I` " +
      "(or `ipconfig`) in your shell and use that address with the same port.";
  const cause = reason
    ? `${url} was refused by the browser's egress policy.`
    : `${url} did not answer: the browser's loopback is not the loopback your shell sees ` +
      "(they are different machines or containers).";
  return reason ? `${action} ${cause} (Policy detail: ${reason})` : `${action} ${cause}`;
}

/** Chromium net errors that mean "nothing answered", i.e. worth one retry.
 * Behind the egress proxy a target that does not answer (or a name that does
 * not resolve) is ERR_EMPTY_RESPONSE for https, ws and plain http alike: the
 * proxy closes the connection unanswered. */
export const RETRYABLE_NET_ERRORS =
  /ERR_CONNECTION_REFUSED|ERR_CONNECTION_RESET|ERR_ADDRESS_UNREACHABLE|ERR_EMPTY_RESPONSE|ERR_CONNECTION_TIMED_OUT|ERR_NAME_NOT_RESOLVED|ERR_BLOCKED_BY_CLIENT|ERR_ABORTED|ERR_FAILED/;

/** Chromium's report of a CONNECT the egress proxy refused (its marked 403).
 * Policy, not an absent server: never retried. */
export const EGRESS_REFUSED_NET_ERRORS = /ERR_TUNNEL_CONNECTION_FAILED/;

/** The egress proxy's marker header on a refusal (see egress.ts), lower-cased. */
export const EGRESS_REFUSAL_HEADER = "x-browser-egress";

/**
 * What to tell a model whose navigation the browser's egress proxy refused.
 * The server may be up; retrying or rewriting the URL will not get past the
 * proxy. For loopback it also names the likely cause when loopback normally
 * works: the launcher could not load the app's policy and fell back to public
 * addresses only (its log says so).
 */
export function egressRefusal(url: string, reason?: string): string {
  const fallback = isLoopbackUrl(url)
    ? " If this browser normally reaches loopback, its egress proxy is running the fallback " +
      "policy (public addresses only); the browser launcher's log names the reason."
    : "";
  const detail = reason ? ` (Policy detail: ${reason})` : "";
  return (
    `blocked by the browser's egress proxy: ${url} is outside the addresses this browser may ` +
    "reach, so the request never left the browser. This is policy, not a server that is down; " +
    `retrying or rewriting the URL will not help.${fallback}${detail}`
  );
}
