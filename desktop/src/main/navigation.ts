/**
 * Where the window may navigate and what pop-ups it may open.
 *
 * - The main frame stays on the connected worker's origin (or the bundled
 *   connect screen). Any other link goes to the system browser.
 * - `window.open` from the worker page is allowed as a child window when the
 *   page needs it back: the account sign-in pop-up (opened on `about:blank`
 *   and then pointed at the hub, which posts the completion proof to its
 *   opener) and provider OAuth pop-ups (opened with explicit window
 *   features). Plain `_blank` links open externally.
 * - Permission prompts for the worker origin are granted for the things the
 *   web UI actually uses (microphone for voice, notifications, clipboard).
 */
import { BrowserWindow, shell, session, type WebContents } from 'electron';
import { attachTextContextMenu } from './context-menu';
import { PARTITION } from './window';

const CHILD_ALLOWED_PERMISSIONS = new Set(['clipboard-read', 'clipboard-sanitized-write']);
const MAIN_ALLOWED_PERMISSIONS = new Set([
	'media',
	'notifications',
	'clipboard-read',
	'clipboard-sanitized-write',
	'fullscreen',
	'display-capture',
	'pointerLock'
]);

function originOf(url: string): string | null {
	try {
		return new URL(url).origin;
	} catch {
		return null;
	}
}

function isHttp(url: string): boolean {
	return url.startsWith('http://') || url.startsWith('https://');
}

export function installPermissionPolicy(currentOrigin: () => string | null): void {
	const ses = session.fromPartition(PARTITION);
	ses.setPermissionRequestHandler((contents, permission, callback, details) => {
		const requesting = originOf(details.requestingUrl ?? contents.getURL());
		const allowed = requesting !== null && requesting === currentOrigin();
		if (allowed && MAIN_ALLOWED_PERMISSIONS.has(permission)) return callback(true);
		if (CHILD_ALLOWED_PERMISSIONS.has(permission)) return callback(true);
		callback(false);
	});
	ses.setPermissionCheckHandler((_contents, permission, requestingOrigin) => {
		if (requestingOrigin && requestingOrigin === currentOrigin()) {
			return MAIN_ALLOWED_PERMISSIONS.has(permission);
		}
		return CHILD_ALLOWED_PERMISSIONS.has(permission);
	});
}

export function attachNavigationPolicy(
	win: BrowserWindow,
	currentOrigin: () => string | null,
	onLoadFailure: (message: string) => void
): void {
	const contents = win.webContents;
	attachTextContextMenu(win);

	contents.on('will-navigate', (event, url) => {
		if (url.startsWith('file:')) return;
		const origin = currentOrigin();
		if (origin && originOf(url) === origin) return;
		event.preventDefault();
		if (isHttp(url)) void shell.openExternal(url);
	});

	contents.setWindowOpenHandler(({ url, disposition }) => {
		const origin = currentOrigin();
		const target = originOf(url);
		// Pop-ups the page needs to keep talking to: the account sign-in window
		// (starts blank) and sized OAuth windows.
		if (url === 'about:blank' || disposition === 'new-window' || (target && target === origin)) {
			return {
				action: 'allow',
				overrideBrowserWindowOptions: {
					parent: win,
					width: 620,
					height: 760,
					autoHideMenuBar: true,
					webPreferences: {
						partition: PARTITION,
						contextIsolation: true,
						sandbox: true,
						nodeIntegration: false,
						preload: undefined
					}
				}
			};
		}
		if (isHttp(url)) void shell.openExternal(url);
		return { action: 'deny' };
	});

	contents.on('did-create-window', (child) => {
		hardenChild(child.webContents);
		attachTextContextMenu(child);
	});

	contents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
		if (!isMainFrame || code === -3 /* aborted by a newer navigation */) return;
		if (url.startsWith('file:')) return;
		onLoadFailure(describeLoadFailure(code, description));
	});

	contents.on('render-process-gone', (_event, details) => {
		if (details.reason === 'clean-exit') return;
		onLoadFailure(`The page stopped unexpectedly (${details.reason}).`);
	});
}

function hardenChild(child: WebContents): void {
	child.setWindowOpenHandler(({ url }) => {
		if (isHttp(url)) void shell.openExternal(url);
		return { action: 'deny' };
	});
}

function describeLoadFailure(code: number, description: string): string {
	switch (code) {
		case -105:
			return 'The worker address could not be found. Check the host name.';
		case -102:
			return 'The worker refused the connection. Is it running?';
		case -7:
		case -118:
			return 'The worker did not answer in time.';
		case -200:
		case -201:
		case -202:
			return 'The worker uses a certificate this app does not trust. Use a trusted certificate or open it over HTTP on the LAN.';
		default:
			return description ? `Could not open the worker: ${description}` : 'Could not open the worker.';
	}
}
