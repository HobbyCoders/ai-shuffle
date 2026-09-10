/**
 * The one application window.
 *
 * The window is frameless with the OS window controls overlaid, so the web
 * UI's own top strip doubles as the title bar. Chromium exposes the controls'
 * geometry to the page through the Window Controls Overlay API
 * (`navigator.windowControlsOverlay`, `env(titlebar-area-*)`), and the page
 * marks the strip draggable with `-webkit-app-region: drag`.
 *
 * Only two kinds of document ever load here: the bundled status page
 * (file://) and the local worker's web UI at http://127.0.0.1:<port>.
 * Loading the worker's origin directly keeps every request same-origin, so
 * the server's CSRF and WebSocket origin checks behave exactly as they do
 * in a browser.
 */
import { app, BrowserWindow, nativeTheme, shell } from 'electron';
import path from 'node:path';
import { attachNavigationPolicy } from './navigation';
import { WindowStateStore } from './state';

export const PARTITION = 'persist:shuffle';
export const TITLE_BAR_HEIGHT = 52;

export interface TitleBarTheme {
	background: string;
	symbol: string;
}

const DARK_TITLE_BAR: TitleBarTheme = { background: '#000000', symbol: '#e9e9ec' };
const LIGHT_TITLE_BAR: TitleBarTheme = { background: '#ffffff', symbol: '#1d1d1f' };

export type PageMode = 'status';

export class MainWindow {
	onClose?: (event: { preventDefault(): void }) => void;
	private win: BrowserWindow | null = null;
	private workerUrl: string | null = null;
	private page: PageMode | null = null;
	private readonly stateStore: WindowStateStore;

	constructor(private readonly userData: string) {
		this.stateStore = new WindowStateStore(path.join(userData, 'window-state.json'));
	}

	get browserWindow(): BrowserWindow | null {
		return this.win && !this.win.isDestroyed() ? this.win : null;
	}

	/** The worker origin the window is showing, or null on a bundled page. */
	get currentWorkerUrl(): string | null {
		return this.page === null ? this.workerUrl : null;
	}

	/** Which bundled page is showing, or null when the worker UI is. */
	get currentPage(): PageMode | null {
		return this.page;
	}

	isOwnSender(webContentsId: number): boolean {
		return this.browserWindow?.webContents.id === webContentsId;
	}

	async ensure(): Promise<BrowserWindow> {
		const existing = this.browserWindow;
		if (existing) return existing;

		const state = await this.stateStore.load();
		const initialTheme = nativeTheme.shouldUseDarkColors ? DARK_TITLE_BAR : LIGHT_TITLE_BAR;
		const isMac = process.platform === 'darwin';

		const win = new BrowserWindow({
			width: state.bounds?.width ?? 1440,
			height: state.bounds?.height ?? 900,
			x: state.bounds?.x,
			y: state.bounds?.y,
			minWidth: 900,
			minHeight: 560,
			show: false,
			backgroundColor: initialTheme.background,
			title: 'AI Shuffle',
			titleBarStyle: isMac ? 'hiddenInset' : 'hidden',
			trafficLightPosition: isMac ? { x: 18, y: 18 } : undefined,
			titleBarOverlay: {
				color: initialTheme.background,
				symbolColor: initialTheme.symbol,
				height: TITLE_BAR_HEIGHT
			},
			autoHideMenuBar: true,
			webPreferences: {
				preload: path.join(__dirname, '../preload/index.js'),
				partition: PARTITION,
				contextIsolation: true,
				sandbox: true,
				nodeIntegration: false,
				spellcheck: true
			}
		});
		this.win = win;
		this.stateStore.track(win);
		attachNavigationPolicy(
			win,
			() => this.currentWorkerUrl,
			(message) => this.onLoadFailure(message)
		);

		win.once('ready-to-show', () => {
			if (state.maximized) win.maximize();
			win.show();
		});
		win.on('close', (event) => this.onClose?.(event));
		win.on('closed', () => {
			this.win = null;
		});
		const broadcast = () => this.send('desktop:window-state', this.windowState());
		win.on('maximize', broadcast);
		win.on('unmaximize', broadcast);
		win.on('enter-full-screen', broadcast);
		win.on('leave-full-screen', broadcast);
		win.on('focus', broadcast);
		win.on('blur', broadcast);
		win.webContents.on('page-title-updated', (event) => {
			// The document title is the session title; keep the OS title short.
			event.preventDefault();
			win.setTitle('AI Shuffle');
		});
		return win;
	}

	private loadFailureHandler: ((message: string) => void) | null = null;

	onLoadFailed(handler: (message: string) => void): void {
		this.loadFailureHandler = handler;
	}

	private onLoadFailure(message: string): void {
		this.loadFailureHandler?.(message);
	}

	windowState(): { maximized: boolean; fullscreen: boolean; focused: boolean } {
		const win = this.browserWindow;
		return {
			maximized: win?.isMaximized() ?? false,
			fullscreen: win?.isFullScreen() ?? false,
			focused: win?.isFocused() ?? false
		};
	}

	async loadWorker(url: string): Promise<void> {
		const win = await this.ensure();
		this.workerUrl = new URL(url).origin;
		this.page = null;
		await win.loadURL(url).catch(() => {
			/* did-fail-load reports through onLoadFailed. */
		});
	}

	async showPage(mode: PageMode, query: Record<string, string> = {}): Promise<void> {
		const win = await this.ensure();
		this.page = mode;
		await win.loadFile(path.join(app.getAppPath(), 'src/pages/worker.html'), { query: { mode, ...query } }).catch(() => {
			/* The window is closing, or a newer navigation replaced this one. */
		});
	}

	setTitleBarTheme(theme: TitleBarTheme): void {
		const win = this.browserWindow;
		if (!win) return;
		win.setBackgroundColor(theme.background);
		if (process.platform !== 'darwin') {
			try {
				win.setTitleBarOverlay({ color: theme.background, symbolColor: theme.symbol, height: TITLE_BAR_HEIGHT });
			} catch {
				/* Not every Linux window manager honours the overlay. */
			}
		}
	}

	send(channel: string, payload?: unknown): void {
		const win = this.browserWindow;
		if (win) win.webContents.send(channel, payload);
	}

	focus(): void {
		const win = this.browserWindow;
		if (!win) return;
		if (win.isMinimized()) win.restore();
		win.show();
		win.focus();
	}

	openExternal(url: string): void {
		try {
			const parsed = new URL(url);
			if (parsed.protocol === 'http:' || parsed.protocol === 'https:' || parsed.protocol === 'mailto:') {
				void shell.openExternal(parsed.href);
			}
		} catch {
			/* Ignore malformed links. */
		}
	}
}
