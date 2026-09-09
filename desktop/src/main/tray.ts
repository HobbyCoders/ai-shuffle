/** Tray / menu-bar icon with the handful of actions that matter when the window is hidden. */
import { app, Menu, nativeImage, Tray } from 'electron';
import path from 'node:path';

export interface TrayActions {
	show(): void;
	restartWorker(): void;
	openWorkerStatus(): void;
	updatesEnabled?: boolean;
	openInBrowser(): void;
	checkForUpdates(): void;
}

export function installTray(actions: TrayActions): Tray | null {
	const iconPath = path.join(app.getAppPath(), 'build', 'icon.png');
	let image = nativeImage.createFromPath(iconPath);
	if (image.isEmpty()) return null;
	const size = process.platform === 'darwin' ? 18 : 16;
	image = image.resize({ width: size, height: size });
	const tray = new Tray(image);
	tray.setToolTip(app.name);
	tray.setContextMenu(
		Menu.buildFromTemplate([
			{ label: `Open ${app.name}`, click: () => actions.show() },
			{ label: 'Open in Browser', click: () => actions.openInBrowser() },
			{ type: 'separator' },
			{ label: 'Restart Worker', click: () => actions.restartWorker() },
			{ label: 'Check for Updates…', enabled: actions.updatesEnabled !== false, click: () => actions.checkForUpdates() },
			{ type: 'separator' },
			{ label: 'Worker Status', click: () => actions.openWorkerStatus() },
			{ type: 'separator' },
			{ role: 'quit' }
		])
	);
	tray.on('click', () => actions.show());
	return tray;
}
