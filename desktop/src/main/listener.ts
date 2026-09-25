/**
 * The local worker's listener token.
 *
 * The worker listens on loopback, and every local process can reach loopback:
 * other apps, scripts, and other OS users' processes on a shared machine.
 * Each worker the app spawns therefore gets a fresh random token in
 * SHUFFLE_DESKTOP_LISTENER_TOKEN, and the worker is expected to refuse local
 * requests that do not carry it in the X-Shuffle-Desktop-Listener header.
 *
 * The app's own window is the only client that should have it. The main
 * process adds the header to every request its session makes to the worker's
 * exact origin: page loads, fetches, EventSource and WebSocket handshakes.
 * A page cannot add headers to a navigation or a WebSocket handshake itself,
 * which is why the header is attached here rather than by the web UI. No
 * page can read the token; "Open in Browser" uses a one-time handoff URL.
 *
 * The token never goes to any other origin, including other loopback ports.
 */
import { randomBytes } from 'node:crypto';
import type { Session } from 'electron';

export const LISTENER_TOKEN_ENV = 'SHUFFLE_DESKTOP_LISTENER_TOKEN';
export const LISTENER_HEADER = 'X-Shuffle-Desktop-Listener';

export function newListenerToken(): string {
	return randomBytes(32).toString('hex');
}

export function isListenerToken(value: unknown): value is string {
	return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
}

/** Where the header goes: the worker's origin (http://127.0.0.1:<port>) and its token. */
export interface ListenerTarget {
	origin: string;
	token: string;
}

/** The same host and port as the worker, over http or ws. Nothing else. */
export function isWorkerRequest(url: string, origin: string): boolean {
	let target: URL;
	let worker: URL;
	try {
		target = new URL(url);
		worker = new URL(origin);
	} catch {
		return false;
	}
	if (worker.protocol !== 'http:') return false;
	if (target.protocol !== 'http:' && target.protocol !== 'ws:') return false;
	if (target.username || target.password) return false;
	return target.hostname === worker.hostname && target.port === worker.port;
}

/**
 * Request headers with the listener header set for the worker, and removed
 * everywhere else (a page must not be able to forward a copy elsewhere).
 */
export function withListenerHeader(url: string, headers: Record<string, string>, target: ListenerTarget | null): Record<string, string> {
	const next: Record<string, string> = {};
	for (const [name, value] of Object.entries(headers)) {
		if (name.toLowerCase() !== LISTENER_HEADER.toLowerCase()) next[name] = value;
	}
	if (target && isListenerToken(target.token) && isWorkerRequest(url, target.origin)) next[LISTENER_HEADER] = target.token;
	return next;
}

/**
 * Attach the header to the window's session. Electron allows one such
 * listener per session. No URL filter: every request passes through, so the
 * header is also stripped from anything that is not the worker.
 */
export function installListenerHeader(ses: Session, target: () => ListenerTarget | null): void {
	ses.webRequest.onBeforeSendHeaders((details, callback) => {
		callback({ requestHeaders: withListenerHeader(details.url, details.requestHeaders, target()) });
	});
}
