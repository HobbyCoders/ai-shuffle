/**
 * Where the window may navigate and what pop-ups it may open.
 *
 * - The main frame stays on the connected worker's origin or the bundled
 *   status page (that exact file, not any file: URL). Links elsewhere, and
 *   server redirects that would leave those two, go to the system browser
 *   when they are plain http(s) URLs and are dropped otherwise.
 * - `window.open` from the worker page is allowed as a child window when the
 *   page needs it back: the account sign-in pop-up (opened on `about:blank`
 *   and then pointed at the hub, which posts the completion proof to its
 *   opener) and provider OAuth pop-ups (opened with explicit window
 *   features). Plain `_blank` links open externally.
 * - Permissions are granted only to the worker origin, and only the ones the
 *   web UI uses: the microphone (voice and dictation, `getUserMedia` with
 *   audio), clipboard writes (copy buttons, `navigator.clipboard.writeText`)
 *   and fullscreen (native `<video controls>`). Nothing reads the clipboard,
 *   captures the screen, locks the pointer or shows web notifications, so
 *   those are denied too. Every other origin, including OAuth, hub and Plaid
 *   pop-ups, gets nothing.
 */
import { BrowserWindow, shell, session, type WebContents } from 'electron';
import { attachTextContextMenu } from './context-menu';
import { externalHttpUrl, originOf, trustedPage } from './pages';
import { PARTITION } from './window';

const WORKER_PERMISSIONS = new Set(['media', 'clipboard-sanitized-write', 'fullscreen']);

/**
 * The permission rule for both the request and the check handler. `mediaTypes`
 * is what a media request asks for; only audio is granted (no camera).
 */
export function permissionAllowed(
	permission: string,
	requestingUrl: string | null | undefined,
	workerOrigin: string | null,
	mediaTypes: readonly string[] = []
): boolean {
	if (!workerOrigin || !requestingUrl) return false;
	const requesting = originOf(requestingUrl);
	if (requesting === null || requesting !== originOf(workerOrigin)) return false;
	if (!WORKER_PERMISSIONS.has(permission)) return false;
	if (permission === 'media') return mediaTypes.every((type) => type === 'audio');
	return true;
}

/** Whether the main frame may show `url`: the worker origin or the bundled status page. */
export function mainFrameAllowed(url: string, workerOrigin: string | null, bundledPageUrl: string): boolean {
	return trustedPage(url, workerOrigin, bundledPageUrl) !== null;
}

function openExternally(url: string): void {
	const safe = externalHttpUrl(url);
	if (safe) void shell.openExternal(safe);
}

export function installPermissionPolicy(currentOrigin: () => string | null): void {
	const ses = session.fromPartition(PARTITION);
	ses.setPermissionRequestHandler((contents, permission, callback, details) => {
		const mediaTypes = 'mediaTypes' in details ? details.mediaTypes ?? [] : [];
		callback(permissionAllowed(permission, details.requestingUrl || contents?.getURL(), currentOrigin(), mediaTypes));
	});
	ses.setPermissionCheckHandler((_contents, permission, requestingOrigin, details) => {
		const mediaTypes = permission === 'media' && details.mediaType ? [details.mediaType] : [];
		return permissionAllowed(permission, requestingOrigin || details.requestingUrl, currentOrigin(), mediaTypes);
	});
}

export function attachNavigationPolicy(
	win: BrowserWindow,
	currentOrigin: () => string | null,
	onLoadFailure: (message: string) => void,
	bundledPageUrl: string
): void {
	const contents = win.webContents;
	attachTextContextMenu(win);

	contents.on('will-navigate', (event, url) => {
		if (mainFrameAllowed(url, currentOrigin(), bundledPageUrl)) return;
		event.preventDefault();
		openExternally(url);
	});
	// A server redirect does not fire will-navigate: a worker URL that 3xx's
	// to another origin would otherwise keep the preload bridge on that page.
	contents.on('will-redirect', (details) => {
		if (!details.isMainFrame || mainFrameAllowed(details.url, currentOrigin(), bundledPageUrl)) return;
		details.preventDefault();
		openExternally(details.url);
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
		openExternally(url);
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
		openExternally(url);
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
