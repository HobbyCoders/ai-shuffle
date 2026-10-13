/**
 * The voice call's pop-out: the web UI's away call bar in a small frameless,
 * always-on-top window, so the call controls stay in reach over other apps
 * while the main window is minimised or behind them.
 *
 * The main window keeps the call. Its page owns the microphone, the audio and
 * the call's connection; this window only shows what that page sends it
 * (`CallState`) and sends button presses back (`CallCommand`). It cannot start
 * a call, reach the worker or load anything but its own bundled page:
 *
 * - The page is `src/pages/call.html`, with a CSP that allows only its own
 *   script and style and no network at all.
 * - It runs in an in-memory session of its own (no worker cookies), sandboxed
 *   with context isolation and no Node, and every permission is refused.
 * - Navigation, new windows and webviews are refused.
 * - Its preload exposes two things: a state listener and `command(name)`. The
 *   main process accepts a command only from this window's main frame showing
 *   that exact page, and only from the allowlist below.
 *
 * It closes when the call ends or the page asks (the dock button), when the
 * main window's page reloads or navigates away (that ends the call), and with
 * the main window. Closing it tells the main window, which shows the bar again.
 */
import { app, BrowserWindow, ipcMain, nativeTheme, screen, session, type IpcMainEvent, type Rectangle } from 'electron';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { isBundledPage } from './pages';

/** What the pop-out shows. The web UI sends it; `callState` validates it. */
export interface CallState {
	label: string;
	title: string;
	micMuted: boolean;
	agentMuted: boolean;
	micProblem: boolean;
}

/** Buttons the main window's page acts on. `dock` is handled here: it closes the pop-out. */
export const CALL_COMMANDS = ['toggle-mic', 'toggle-agent', 'hangup', 'return'] as const;
export type CallCommand = (typeof CALL_COMMANDS)[number];

export const POPOUT_PARTITION = 'call-popout';
export const POPOUT_SIZE = { width: 420, height: 60 } as const;
const MARGIN = 24;

/** Plain text of at most `limit` characters, with control characters removed; null for a non-string. */
function text(value: unknown, limit: number): string | null {
	if (typeof value !== 'string') return null;
	// eslint-disable-next-line no-control-regex
	const clean = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
	return clean.length > limit ? `${clean.slice(0, limit - 1).trimEnd()}…` : clean;
}

/** The state the page sent, validated and trimmed, or null when it is not one. */
export function callState(value: unknown): CallState | null {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
	const v = value as Record<string, unknown>;
	const label = text(v.label, 80);
	const title = text(v.title, 120);
	if (label === null || title === null) return null;
	return { label, title, micMuted: v.micMuted === true, agentMuted: v.agentMuted === true, micProblem: v.micProblem === true };
}

/**
 * Where the pop-out opens: the saved spot, moved fully onto the display it is
 * on; the bottom-right corner of the primary display when there is no saved
 * spot or its display is gone.
 */
export function placement(
	saved: { x: number; y: number } | null,
	workAreas: Rectangle[],
	primary: Rectangle,
	size: { width: number; height: number } = POPOUT_SIZE
): { x: number; y: number } {
	const fit = (area: Rectangle, x: number, y: number) => ({
		x: Math.round(Math.max(area.x, Math.min(area.x + area.width - size.width, x))),
		y: Math.round(Math.max(area.y, Math.min(area.y + area.height - size.height, y)))
	});
	if (saved) {
		const area = workAreas.find(
			(a) => saved.x < a.x + a.width && saved.x + size.width > a.x && saved.y < a.y + a.height && saved.y + size.height > a.y
		);
		if (area) return fit(area, saved.x, saved.y);
	}
	return fit(primary, primary.x + primary.width - size.width - MARGIN, primary.y + primary.height - size.height - MARGIN);
}

/** The parts of the main window the pop-out uses. */
export interface CallPopoutHost {
	readonly browserWindow: BrowserWindow | null;
	send(channel: string, payload?: unknown): void;
	focus(): void;
}

export class CallPopout {
	private win: BrowserWindow | null = null;
	private state: CallState | null = null;
	private detachHost: (() => void) | null = null;
	private saveTimer: NodeJS.Timeout | null = null;
	private readonly file: string;

	constructor(userData: string, private readonly host: CallPopoutHost) {
		this.file = path.join(userData, 'call-popout.json');
	}

	get pagePath(): string {
		return path.join(app.getAppPath(), 'src/pages/call.html');
	}

	get pageUrl(): string {
		return pathToFileURL(this.pagePath).href;
	}

	get isOpen(): boolean {
		return !!this.win && !this.win.isDestroyed();
	}

	/** Listens for the pop-out page's button presses. Call once. */
	register(): void {
		const ses = session.fromPartition(POPOUT_PARTITION);
		ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
		ses.setPermissionCheckHandler(() => false);
		ipcMain.on('call-popout:command', (event, command) => {
			if (this.fromPopoutPage(event)) this.command(command);
		});
	}

	/** True for a message from the pop-out's own main frame showing its bundled page. */
	fromPopoutPage(event: Pick<IpcMainEvent, 'sender' | 'senderFrame'>): boolean {
		const win = this.isOpen ? this.win! : null;
		if (!win || event.sender.id !== win.webContents.id) return false;
		const frame = event.senderFrame;
		const main = event.sender.mainFrame;
		if (!frame || !main) return false;
		if (frame !== main && (frame.processId !== main.processId || frame.routingId !== main.routingId)) return false;
		return isBundledPage(frame.url, this.pageUrl);
	}

	/** Runs a pop-out button. Anything outside the allowlist is ignored. */
	command(command: unknown): void {
		if (command === 'dock') {
			this.close();
			return;
		}
		if (typeof command !== 'string' || !(CALL_COMMANDS as readonly string[]).includes(command)) return;
		if (command === 'return') this.host.focus();
		this.host.send('desktop:call-popout-command', command);
	}

	/** Opens the pop-out (or refreshes an open one) with `state`. False when the state is not valid. */
	async open(state: unknown): Promise<boolean> {
		const valid = callState(state);
		const host = this.host.browserWindow;
		if (!valid || !host) return false;
		this.state = valid;
		if (this.isOpen) {
			this.push();
			this.win!.showInactive();
			return true;
		}
		const saved = await this.loadPosition();
		const at = placement(saved, screen.getAllDisplays().map((d) => d.workArea), screen.getPrimaryDisplay().workArea);
		// The host may have closed while the saved position loaded.
		if (!this.host.browserWindow) return false;
		if (this.isOpen) {
			this.push();
			return true;
		}
		const win = new BrowserWindow({
			...POPOUT_SIZE,
			...at,
			show: false,
			frame: false,
			resizable: false,
			minimizable: false,
			maximizable: false,
			fullscreenable: false,
			skipTaskbar: true,
			alwaysOnTop: true,
			title: 'AI Shuffle call',
			backgroundColor: nativeTheme.shouldUseDarkColors ? '#131617' : '#ffffff',
			webPreferences: {
				preload: path.join(__dirname, '../preload/call-popout.js'),
				partition: POPOUT_PARTITION,
				contextIsolation: true,
				sandbox: true,
				nodeIntegration: false,
				spellcheck: false
			}
		});
		this.win = win;
		// Above ordinary windows on every platform; on macOS also over full-screen apps.
		win.setAlwaysOnTop(true, 'floating');
		if (process.platform === 'darwin') win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

		const contents = win.webContents;
		contents.on('will-navigate', (event) => event.preventDefault());
		contents.on('will-redirect', (event) => event.preventDefault());
		contents.on('will-attach-webview', (event) => event.preventDefault());
		contents.setWindowOpenHandler(() => ({ action: 'deny' }));
		contents.on('did-finish-load', () => this.push());
		contents.on('render-process-gone', () => this.close());

		win.once('ready-to-show', () => {
			if (!win.isDestroyed()) win.showInactive();
		});
		win.on('moved', () => this.rememberPosition());
		win.on('move', () => this.rememberPosition());
		win.on('closed', () => this.onClosed(win));

		this.watchHost(host);
		await win.loadFile(this.pagePath).catch(() => {
			/* Closed while loading. */
		});
		return this.isOpen;
	}

	/** New state for an open pop-out; ignored when closed or not valid. */
	update(state: unknown): void {
		const valid = callState(state);
		if (!valid || !this.isOpen) return;
		this.state = valid;
		this.push();
	}

	close(): void {
		if (this.isOpen) this.win!.close();
	}

	private push(): void {
		if (this.isOpen && this.state) this.win!.webContents.send('call-popout:state', this.state);
	}

	/** The call lives in the main window's page: when that page goes, so does the pop-out. */
	private watchHost(host: BrowserWindow): void {
		this.detachHost?.();
		const navigated = (_event: unknown, _url: string, inPlace: boolean, mainFrame: boolean) => {
			if (mainFrame && !inPlace) this.close();
		};
		const gone = () => this.close();
		host.webContents.on('did-start-navigation', navigated);
		host.webContents.on('render-process-gone', gone);
		host.on('closed', gone);
		this.detachHost = () => {
			if (!host.isDestroyed()) {
				host.webContents.removeListener('did-start-navigation', navigated);
				host.webContents.removeListener('render-process-gone', gone);
				host.removeListener('closed', gone);
			}
		};
	}

	private onClosed(win: BrowserWindow): void {
		if (this.win !== win) return;
		this.win = null;
		this.state = null;
		this.detachHost?.();
		this.detachHost = null;
		this.flushPosition();
		this.host.send('desktop:call-popout-closed');
	}

	private position: { x: number; y: number } | null = null;

	private rememberPosition(): void {
		if (!this.isOpen) return;
		const [x, y] = this.win!.getPosition();
		this.position = { x, y };
		if (this.saveTimer) clearTimeout(this.saveTimer);
		this.saveTimer = setTimeout(() => this.flushPosition(), 400);
	}

	private flushPosition(): void {
		if (this.saveTimer) clearTimeout(this.saveTimer);
		this.saveTimer = null;
		const position = this.position;
		if (!position) return;
		void fs
			.mkdir(path.dirname(this.file), { recursive: true })
			.then(() => fs.writeFile(this.file, JSON.stringify(position), 'utf8'))
			.catch(() => {
				/* Losing the pop-out's spot is not worth surfacing. */
			});
	}

	private async loadPosition(): Promise<{ x: number; y: number } | null> {
		try {
			const parsed = JSON.parse(await fs.readFile(this.file, 'utf8')) as Record<string, unknown> | null;
			const x = parsed?.x;
			const y = parsed?.y;
			return typeof x === 'number' && Number.isFinite(x) && typeof y === 'number' && Number.isFinite(y) ? { x, y } : null;
		} catch {
			return null;
		}
	}
}
