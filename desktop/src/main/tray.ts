/** Tray / menu-bar icon with the handful of actions that matter when the window is hidden. */
import { app, Menu, nativeImage, Tray } from 'electron';
import path from 'node:path';

// Keep the native tray alive for the lifetime of the app.
let tray: Tray | null = null;

export interface TrayActions {
	show(): void;
	restartWorker(): void;
	openWorkerStatus(): void;
	openInBrowser(): void;
	/** Quit, killing the worker if its graceful stop fails or hangs. */
	forceQuit(): void;
}

/**
 * `name` is the product name to show. Not `app.name`: that is the package name
 * ("ai-shuffle-desktop") in stable builds, and changing it would move the profile.
 */
export function installTray(actions: TrayActions, name: string): Tray | null {
	const iconPath = path.join(app.getAppPath(), 'build', 'icon.png');
	let image = nativeImage.createFromPath(iconPath);
	if (image.isEmpty()) return null;
	const size = process.platform === 'darwin' ? 18 : 16;
	image = image.resize({ width: size, height: size });
	tray = new Tray(image);
	tray.setToolTip(name);
	tray.setContextMenu(
		Menu.buildFromTemplate([
			{ label: `Open ${name}`, click: () => actions.show() },
			{ label: 'Open in Browser', click: () => actions.openInBrowser() },
			{ type: 'separator' },
			{ label: 'Restart Worker', click: () => actions.restartWorker() },
			{ label: 'Worker Status', click: () => actions.openWorkerStatus() },
			{ type: 'separator' },
			// Not role 'quit': this one still quits when the worker is wedged.
			{ label: 'Quit', click: () => actions.forceQuit() }
		])
	);
	tray.on('click', () => actions.show());
	return tray;
}
