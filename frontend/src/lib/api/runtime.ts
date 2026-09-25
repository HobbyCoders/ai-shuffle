/**
 * Runtime API configuration — where the backend lives and how we authenticate.
 *
 * Two deployments share this code:
 *
 * - **Worker-served web UI** (default). The page is served by the worker it
 *   talks to, so every API path is same-origin (`/api/v1/...`), auth is the
 *   `session` cookie, and WebSockets derive from `window.location`.
 *
 * - **Bundled mobile UI** (the Android app). The page is loaded from the app's
 *   own asset origin (`https://appassets.androidplatform.net/...`) and talks to
 *   a worker on a different origin. Before any script runs, the app sets
 *
 *   ```js
 *   window.__SHUFFLE_MOBILE__ = {
 *     apiOrigin: 'https://worker.example',   // the paired worker's HTTPS origin
 *     token: 'shc_…',                        // paired-session token (Bearer)
 *     version: '0.8.6',                      // app versionName (optional)
 *     deviceName: 'Pixel 9',                 // (optional)
 *     sendCookies: false                     // optional; true only when the
 *                                            // worker sits behind Cloudflare
 *                                            // Access and the app installed
 *                                            // the CF cookie (SameSite=None)
 *   };
 *   ```
 *
 *   Every helper below then returns absolute URLs on that origin, adds
 *   `Authorization: Bearer <token>`, omits cookies (unless `sendCookies`), and
 *   appends `?token=` to WebSocket URLs (the chat/global/browser sockets accept
 *   it; see app/api/ws_auth.py).
 *
 *   The app can hand the page a fresh token without a reload through
 *   `window.shuffleMobileSetToken('shc_…')` or a `message` event whose data is
 *   `{ type: 'shuffle-mobile-token', token: 'shc_…' }` posted from the app's
 *   own origin.
 *
 * Nothing here depends on SvelteKit, so it is safe to import from any module
 * (including the plain `.ts` API clients) and to unit-test without a DOM.
 */

export interface ShuffleMobileRuntime {
	/** Worker origin, e.g. `https://worker.example` (no path, no trailing slash). */
	apiOrigin: string;
	/** Paired-session token (`shc_…`) sent as a Bearer credential. */
	token: string;
	/** App versionName, for diagnostics. */
	version?: string;
	/** Human name of this device, for diagnostics. */
	deviceName?: string;
	/**
	 * Send cookies with API requests. Off by default: auth is the Bearer
	 * token. The app turns this on only for Cloudflare-Access workers where it
	 * installed the `CF_Authorization` cookie itself.
	 */
	sendCookies?: boolean;
}

declare global {
	interface Window {
		__SHUFFLE_MOBILE__?: ShuffleMobileRuntime;
		shuffleMobileSetToken?: (token: string) => void;
	}
}

/** `message` event type the app may post to rotate the token. */
export const MOBILE_TOKEN_MESSAGE_TYPE = 'shuffle-mobile-token';

const ORIGIN_PATTERN = /^https?:\/\/[^/?#\s]+$/;

let hooksInstalled = false;

function normalizeOrigin(raw: unknown): string | null {
	if (typeof raw !== 'string') return null;
	const trimmed = raw.trim().replace(/\/+$/, '');
	return ORIGIN_PATTERN.test(trimmed) ? trimmed : null;
}

/**
 * The runtime the app injected, or `null` on the worker-served web UI (and in
 * any non-browser context). Invalid shapes are ignored rather than half-used.
 */
export function mobileRuntime(): ShuffleMobileRuntime | null {
	if (typeof window === 'undefined') return null;
	const raw = window.__SHUFFLE_MOBILE__;
	if (!raw || typeof raw !== 'object') return null;
	const apiOrigin = normalizeOrigin(raw.apiOrigin);
	if (!apiOrigin || typeof raw.token !== 'string' || !raw.token) return null;
	installTokenHooks();
	return raw;
}

/** True when this page is the app-bundled mobile UI. */
export function isBundledMobile(): boolean {
	return mobileRuntime() !== null;
}

/** Backend origin: `''` (same origin) on the web UI, the worker origin when bundled. */
export function apiOrigin(): string {
	const runtime = mobileRuntime();
	return runtime ? normalizeOrigin(runtime.apiOrigin)! : '';
}

/** `'/api/v1'` on the web UI; `'https://worker/api/v1'` when bundled. */
export function apiBase(): string {
	return `${apiOrigin()}/api/v1`;
}

/**
 * Absolute form of any backend path (`/api/...`, `/health`). Use for the few
 * routes outside `/api/v1` and for URLs handed to `<img>`/`<a>`.
 */
export function apiUrl(path: string): string {
	return `${apiOrigin()}${path}`;
}

/** `Authorization` header when bundled; empty on the web UI (cookie auth). */
export function authHeaders(): Record<string, string> {
	const runtime = mobileRuntime();
	return runtime ? { Authorization: `Bearer ${runtime.token}` } : {};
}

/** Cookie policy: the web UI relies on the session cookie; the bundle does not. */
export function requestCredentials(): RequestCredentials {
	const runtime = mobileRuntime();
	if (!runtime) return 'include';
	return runtime.sendCookies ? 'include' : 'omit';
}

/**
 * Apply the auth policy to a `fetch` init: credentials always, the Bearer
 * header only when bundled. Authenticated worker requests never follow a
 * redirect, even if a caller asks to: a registry or intermediary must not
 * transfer a paired request to a different endpoint (or change its method).
 */
export function withAuth(init: RequestInit = {}): RequestInit {
	const out: RequestInit = { ...init, credentials: requestCredentials(), redirect: 'error' };
	const extra = authHeaders();
	if (Object.keys(extra).length === 0) return out;
	if (init.headers instanceof Headers) {
		const headers = new Headers(init.headers);
		for (const [key, value] of Object.entries(extra)) headers.set(key, value);
		out.headers = headers;
	} else if (Array.isArray(init.headers)) {
		out.headers = [...init.headers, ...Object.entries(extra)];
	} else {
		out.headers = { ...extra, ...(init.headers as Record<string, string> | undefined) };
	}
	return out;
}

/**
 * WebSocket URL for a backend path (`/api/v1/ws/chat`). On the web UI it is
 * derived from `window.location`; when bundled from the worker origin, with
 * `token=` appended so the socket authenticates without a cookie. Extra query
 * parameters are encoded here so callers never hand-build the query string.
 */
export function wsUrl(path: string, params: Record<string, string | null | undefined> = {}): string {
	const runtime = mobileRuntime();
	const query = new URLSearchParams();
	for (const [key, value] of Object.entries(params)) {
		if (value !== null && value !== undefined && value !== '') query.set(key, value);
	}
	let base: string;
	if (runtime) {
		const origin = normalizeOrigin(runtime.apiOrigin)!;
		base = origin.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:') + path;
		if (!query.has('token')) query.set('token', runtime.token);
	} else {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		base = `${protocol}//${window.location.host}${path}`;
	}
	const qs = query.toString();
	return qs ? `${base}?${qs}` : base;
}

/** Contract name: the bare WebSocket URL for `path` (token appended when bundled). */
export function wsBaseUrl(path: string): string {
	return wsUrl(path);
}

/**
 * Replace the bundled session token in place (no reload). No-op on the web
 * UI. Exposed to the app as `window.shuffleMobileSetToken` and via a
 * `message` event (see the module comment).
 */
export function setMobileToken(token: string): boolean {
	if (typeof window === 'undefined') return false;
	const raw = window.__SHUFFLE_MOBILE__;
	if (!raw || typeof token !== 'string' || !token) return false;
	if (Object.isFrozen(raw)) {
		window.__SHUFFLE_MOBILE__ = { ...raw, token };
	} else {
		raw.token = token;
	}
	return true;
}

function installTokenHooks(): void {
	if (hooksInstalled || typeof window === 'undefined') return;
	hooksInstalled = true;
	window.shuffleMobileSetToken = (token: string) => {
		setMobileToken(token);
	};
	window.addEventListener('message', (event: MessageEvent) => {
		// Only the app's own origin (the page origin) may rotate the token.
		if (event.origin !== window.location.origin) return;
		const data = event.data;
		if (!data || typeof data !== 'object') return;
		if ((data as { type?: unknown }).type !== MOBILE_TOKEN_MESSAGE_TYPE) return;
		const token = (data as { token?: unknown }).token;
		if (typeof token === 'string') setMobileToken(token);
	});
}
