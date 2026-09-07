/**
 * Application menu.
 *
 * Items that act on the web UI send a named command through the bridge and
 * let the page run the same code its keyboard shortcut would; the shell
 * never reaches into the page for them. The menu bar is hidden on Windows
 * and Linux (Alt shows it); the accelerators keep working regardless.
 */
import { app, Menu, shell, type MenuItemConstructorOptions } from 'electron';

export type DesktopCommand =
	| 'new-session'
	| 'command-palette'
	| 'toggle-sidebar'
	| 'open-settings'
	| 'focus-composer'
	| 'find';

export interface MenuActions {
	command(name: DesktopCommand): void;
	switchWorker(): void;
	checkForUpdates(): void;
	reload(): void;
	openDocs(): void;
}

export function installMenu(actions: MenuActions): void {
	const isMac = process.platform === 'darwin';

	const appMenu: MenuItemConstructorOptions[] = isMac
		? [
				{
					label: app.name,
					submenu: [
						{ role: 'about' },
						{ label: 'Check for Updates…', click: () => actions.checkForUpdates() },
						{ type: 'separator' },
						{ label: 'Settings…', accelerator: 'Cmd+,', click: () => actions.command('open-settings') },
						{ type: 'separator' },
						{ role: 'services' },
						{ type: 'separator' },
						{ role: 'hide' },
						{ role: 'hideOthers' },
						{ role: 'unhide' },
						{ type: 'separator' },
						{ role: 'quit' }
					]
				}
			]
		: [];

	const fileMenu: MenuItemConstructorOptions = {
		label: 'File',
		submenu: [
			{ label: 'New Session', accelerator: 'CmdOrCtrl+N', click: () => actions.command('new-session') },
			{ type: 'separator' },
			{ label: 'Switch Worker…', accelerator: 'CmdOrCtrl+Shift+O', click: () => actions.switchWorker() },
			...(isMac
				? []
				: ([
						{ type: 'separator' },
						{ label: 'Settings…', accelerator: 'Ctrl+,', click: () => actions.command('open-settings') },
						{ type: 'separator' },
						{ role: 'quit' }
					] as MenuItemConstructorOptions[]))
		]
	};

	const editMenu: MenuItemConstructorOptions = {
		label: 'Edit',
		submenu: [
			{ role: 'undo' },
			{ role: 'redo' },
			{ type: 'separator' },
			{ role: 'cut' },
			{ role: 'copy' },
			{ role: 'paste' },
			{ role: 'pasteAndMatchStyle' },
			{ role: 'selectAll' },
			{ type: 'separator' },
			{ label: 'Find in Page…', accelerator: 'CmdOrCtrl+F', click: () => actions.command('find') },
			...(isMac ? ([{ type: 'separator' }, { role: 'startSpeaking' }, { role: 'stopSpeaking' }] as MenuItemConstructorOptions[]) : [])
		]
	};

	const viewMenu: MenuItemConstructorOptions = {
		label: 'View',
		submenu: [
			{ label: 'Command Palette', accelerator: 'CmdOrCtrl+K', click: () => actions.command('command-palette') },
			{ label: 'Toggle Sidebar', accelerator: 'CmdOrCtrl+B', click: () => actions.command('toggle-sidebar') },
			{ label: 'Focus Composer', accelerator: 'CmdOrCtrl+L', click: () => actions.command('focus-composer') },
			{ type: 'separator' },
			{ label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => actions.reload() },
			{ role: 'toggleDevTools' },
			{ type: 'separator' },
			{ role: 'resetZoom' },
			{ role: 'zoomIn' },
			{ role: 'zoomOut' },
			{ type: 'separator' },
			{ role: 'togglefullscreen' }
		]
	};

	const windowMenu: MenuItemConstructorOptions = {
		label: 'Window',
		submenu: isMac
			? [{ role: 'minimize' }, { role: 'zoom' }, { type: 'separator' }, { role: 'front' }]
			: [{ role: 'minimize' }, { role: 'close' }]
	};

	const helpMenu: MenuItemConstructorOptions = {
		role: 'help',
		submenu: [
			{ label: 'Documentation', click: () => actions.openDocs() },
			{ label: 'Report a Problem', click: () => void shell.openExternal('https://github.com/HobbyCoders/ai-shuffle/issues') },
			...(isMac
				? []
				: ([
						{ type: 'separator' },
						{ label: 'Check for Updates…', click: () => actions.checkForUpdates() },
						{ label: `AI Shuffle Desktop ${app.getVersion()}`, enabled: false }
					] as MenuItemConstructorOptions[]))
		]
	};

	Menu.setApplicationMenu(Menu.buildFromTemplate([...appMenu, fileMenu, editMenu, viewMenu, windowMenu, helpMenu]));
}
