/**
 * AI Shuffle desktop: process entry.
 *
 * One instance, one window. On launch the last-used worker opens straight
 * away; otherwise the connect screen lists the saved workers and looks for
 * a local install on port 8000.
 */
import { app, nativeTheme, session } from 'electron';
import path from 'node:path';
import { registerIpc } from './ipc';
import { installMenu } from './menu';
import { installPermissionPolicy } from './navigation';
import { ServerStore } from './servers';
import { installTray } from './tray';
import { Updater } from './updater';
import { MainWindow, PARTITION } from './window';

const PROTOCOL = 'aishuffle';

app.setAppUserModelId('com.hobbycoders.aishuffle.desktop');

// Development helpers: an isolated profile and a headless screenshot, so the
// shell can be exercised from a script or CI without a person at the screen.
if (!app.isPackaged && process.env.SHUFFLE_DESKTOP_USER_DATA) {
	app.setPath('userData', process.env.SHUFFLE_DESKTOP_USER_DATA);
}

if (!app.requestSingleInstanceLock()) {
	app.quit();
} else {
	void main();
}

async function main(): Promise<void> {
	const userData = app.getPath('userData');
	const servers = new ServerStore(path.join(userData, 'servers.json'));
	const window = new MainWindow(userData);
	const updater = new Updater();

	app.on('second-instance', () => window.focus());
	app.on('activate', () => void open());
	app.on('window-all-closed', () => {
		if (process.platform !== 'darwin') app.quit();
	});

	if (app.isPackaged) {
		app.setAsDefaultProtocolClient(PROTOCOL);
	}

	await app.whenReady();
	await servers.load();

	const ses = session.fromPartition(PARTITION);
	ses.setUserAgent(`${ses.getUserAgent()} AIShuffleDesktop/${app.getVersion()}`);
	installPermissionPolicy(() => window.server?.url ?? null);

	registerIpc({ window, servers, updater });
	updater.subscribe((state) => window.send('desktop:update-state', state));
	nativeTheme.on('updated', () => window.send('desktop:native-theme', { dark: nativeTheme.shouldUseDarkColors }));

	const actions = {
		command: (name: string) => window.send('desktop:command', name),
		switchWorker: () => void servers.clearLast().then(() => window.showConnect()),
		checkForUpdates: () => void updater.check(),
		reload: () => window.browserWindow?.webContents.reload(),
		openDocs: () => {
			const server = window.server;
			if (server) window.send('desktop:command', 'open-docs');
			else window.openExternal('https://github.com/HobbyCoders/ai-shuffle#readme');
		},
		show: () => {
			window.focus();
			void open();
		}
	};
	installMenu(actions);
	installTray(actions);

	await open();
	updater.start();
	installScreenshotHook(window);

	async function open(): Promise<void> {
		if (window.browserWindow) {
			window.focus();
			return;
		}
		const last = servers.last();
		if (last) await window.loadServer(last);
		else await window.showConnect();
	}
}

/**
 * `SHUFFLE_DESKTOP_SHOT=/path/out.png` captures the window a few seconds after
 * load and quits. `SHUFFLE_DESKTOP_SHOT_JS` runs a snippet in the page first
 * (for example to open a feature page) and waits a moment for it to settle.
 */
function installScreenshotHook(window: MainWindow): void {
	const target = process.env.SHUFFLE_DESKTOP_SHOT;
	if (app.isPackaged || !target) return;
	const delay = Number(process.env.SHUFFLE_DESKTOP_SHOT_DELAY ?? 4000);
	const script = process.env.SHUFFLE_DESKTOP_SHOT_JS;
	setTimeout(async () => {
		try {
			const win = window.browserWindow;
			if (!win) return;
			if (script) {
				await win.webContents.executeJavaScript(script, true);
				await new Promise((resolve) => setTimeout(resolve, 2000));
			}
			const image = await win.webContents.capturePage();
			await import('node:fs/promises').then((fs) => fs.writeFile(target, image.toPNG()));
			console.log(`screenshot written to ${target}`);
		} catch (error) {
			console.error('screenshot failed', error);
		} finally {
			app.quit();
		}
	}, delay);
}
