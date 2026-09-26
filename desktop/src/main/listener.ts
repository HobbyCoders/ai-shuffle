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
 * process adds the header to requests its session makes to the worker's
 * exact origin: page loads, fetches, EventSource and WebSocket handshakes.
 * A subresource request (fetch, XHR, WebSocket, image, ...) carries it only
 * when the document that made it sits under a worker-origin top frame, so an
 * OAuth, hub or Plaid pop-up at another origin cannot use the token. A
 * request with no frame and no page at all comes from a service worker (or
 * another frameless worker): it is judged by its referrer's origin instead,
 * which a page cannot set to another origin, so the worker origin's own
 * sw.js keeps working while a worker some other site registered gets
 * nothing. A top-level navigation to the worker carries it from any of the
 * app's windows: that is how the OAuth pop-up lands on the worker's callback, and
 * the worker still judges it by Sec-Fetch-Site and Origin like a browser.
 * A page cannot add headers to a navigation or a WebSocket handshake itself,
 * which is why the header is attached here rather than by the web UI. No
 * page can read the token; "Open in Browser" uses a one-time handoff URL.
 *
 * The token never goes to any other origin, including other loopback ports.
 */
import { randomBytes } from 'node:crypto';
import type { OnBeforeSendHeadersListenerDetails, Session } from 'electron';
import { originOf } from './pages';

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
	/**
	 * The origin the main window shows, when it is not the worker's own: only
	 * a development run pointed at a frontend dev server (SHUFFLE_DESKTOP_DEV_URL).
	 */
	pageOrigin?: string | null;
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

/** Who made a request, as far as the token decision needs to know. */
export interface RequestInitiator {
	resourceType: string;
	/**
	 * The origin of the top-level document the requesting frame sits in; for a
	 * frameless request (a service worker's), the origin of its referrer; null
	 * when unknown.
	 */
	topOrigin: string | null;
}

/** Whether a request to the worker may carry the token, given who made it. */
export function initiatorAllowed(initiator: RequestInitiator, target: Pick<ListenerTarget, 'origin' | 'pageOrigin'>): boolean {
	if (initiator.resourceType === 'mainFrame') return true;
	if (initiator.topOrigin === null) return false;
	return initiator.topOrigin === originOf(target.origin) || (!!target.pageOrigin && initiator.topOrigin === originOf(target.pageOrigin));
}

/**
 * The top frame's origin for a request: its frame's top, else the owning
 * page's URL. A request with neither (a service worker's own fetches, its
 * script and update checks) has only its referrer: the worker context that
 * made it, since the Fetch spec ignores a referrer from another origin.
 * No referrer means unknown.
 */
export function requestInitiator(details: Pick<OnBeforeSendHeadersListenerDetails, 'resourceType' | 'frame' | 'webContents'> & { referrer?: string }): RequestInitiator {
	let top: string | null = null;
	try {
		const frame = details.frame?.top ?? null;
		if (frame) top = frame.url;
		else if (details.webContents) top = details.webContents.isDestroyed() ? null : details.webContents.getURL();
		else if (!details.frame) top = details.referrer || null;
	} catch {
		top = null; // A frame that went away while the request was in flight.
	}
	return { resourceType: details.resourceType, topOrigin: top ? originOf(top) : null };
}

/**
 * Request headers with the listener header set for the worker when the
 * initiator is allowed, and removed everywhere else (a page must not be able
 * to forward a copy elsewhere).
 */
export function withListenerHeader(url: string, headers: Record<string, string>, target: ListenerTarget | null, initiator: RequestInitiator): Record<string, string> {
	const next: Record<string, string> = {};
	for (const [name, value] of Object.entries(headers)) {
		if (name.toLowerCase() !== LISTENER_HEADER.toLowerCase()) next[name] = value;
	}
	if (target && isListenerToken(target.token) && isWorkerRequest(url, target.origin) && initiatorAllowed(initiator, target)) {
		next[LISTENER_HEADER] = target.token;
	}
	return next;
}

/**
 * Attach the header to the window's session. Electron allows one such
 * listener per session. No URL filter: every request passes through, so the
 * header is also stripped from anything that is not the worker.
 */
export function installListenerHeader(ses: Session, target: () => ListenerTarget | null): void {
	ses.webRequest.onBeforeSendHeaders((details, callback) => {
		callback({ requestHeaders: withListenerHeader(details.url, details.requestHeaders, target(), requestInitiator(details)) });
	});
}
