/**
 * The phone UI inside the AI Shuffle browser extension's side panel.
 *
 * The panel frames `/mobile/` and hands the page its session in the URL
 * fragment; `app.html` turns that into `window.__SHUFFLE_MOBILE__` with
 * `native.platform === 'extension'` and the panel's origin. This tells the
 * panel which chat is open, so it can let that chat use this browser (it does
 * so only for chats with no browser picked yet). Only the panel's exact origin
 * receives the message.
 */
import { activeSessionId } from '$lib/stores/active-session';

export const EXTENSION_SESSION_MESSAGE = 'shuffle-extension-session';
const PANEL_ORIGIN = /^chrome-extension:\/\/[a-p]{32}$/;

let installed = false;

export function extensionPanelOrigin(): string | null {
	if (typeof window === 'undefined' || window.top === window) return null;
	const runtime = window.__SHUFFLE_MOBILE__ as { native?: { platform?: unknown }; extension?: { origin?: unknown } } | undefined;
	const origin = runtime?.extension?.origin;
	if (runtime?.native?.platform !== 'extension' || typeof origin !== 'string' || !PANEL_ORIGIN.test(origin)) return null;
	return origin;
}

export function installExtensionPanelBridge(): void {
	const origin = extensionPanelOrigin();
	if (installed || !origin) return;
	installed = true;
	activeSessionId.subscribe((sessionId) => {
		window.parent.postMessage({ type: EXTENSION_SESSION_MESSAGE, sessionId: sessionId ?? null }, origin);
	});
}
