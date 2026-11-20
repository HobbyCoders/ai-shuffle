import { posix, win32 } from 'node:path';
import type { App, LoginItemSettingsOptions, Settings } from 'electron';

export type LoginItemState = 'enabled' | 'disabled' | 'blocked' | 'unavailable';
export interface LoginItemControl {
	get(): LoginItemState;
	set(enabled: boolean): { state: LoginItemState; failed: boolean };
}

interface Host {
	platform: NodeJS.Platform;
	packaged: boolean;
	preview: boolean;
	installedExecutable?: string;
	appPath: string;
	systemVersion: string;
	appId: string;
}

type LoginItemApi = Pick<App, 'getLoginItemSettings' | 'setLoginItemSettings'>;

/** Only user-initiated writes. The OS, not a saved preference, is the source of truth. */
export function loginItemControl(app: LoginItemApi, host: Host): LoginItemControl | undefined {
	if (host.preview || typeof app.getLoginItemSettings !== 'function' || typeof app.setLoginItemSettings !== 'function') return;

	let options: LoginItemSettingsOptions;
	let get: LoginItemControl['get'];
	let writeOptions: Omit<Settings, 'openAtLogin'>;
	if (host.platform === 'win32') {
		// The installed entry point is stable across source updates. Never fall back
		// to process.execPath: in the updated app it is the versioned npm Electron.
		const executable = host.installedExecutable;
		if (!executable || !win32.isAbsolute(executable) || !/\.exe$/i.test(executable) || /["\0]/.test(executable)) return;
		// Electron parses the lookup path as a Windows command line (and strips
		// quotes before writing). The product name and install directory can have spaces.
		options = { path: `"${executable}"`, args: [] };
		writeOptions = { ...options, name: host.appId };
		get = () => {
			const item = app.getLoginItemSettings(options);
			// openAtLogin matches the exact command but ignores Task Manager's
			// Startup Apps setting. Only check approval of our per-user entry.
			if (!item.openAtLogin) return 'disabled';
			return item.launchItems.some(entry =>
				entry.scope === 'user' && entry.name.toLowerCase() === host.appId.toLowerCase() && entry.enabled
			) ? 'enabled' : 'blocked';
		};
	} else if (host.platform === 'darwin') {
		// Register the packaged main bundle, not a helper executable. Don't register
		// the distribution image or a Gatekeeper-translocated launch.
		if (!host.packaged || !(Number(host.systemVersion.split('.')[0]) >= 13) ||
			!posix.isAbsolute(host.appPath) || !/\.app\/Contents\/Resources\/app\.asar$/.test(host.appPath) ||
			host.appPath.startsWith('/Volumes/') || host.appPath.includes('/AppTranslocation/')) return;
		options = writeOptions = { type: 'mainAppService' };
		get = () => {
			switch (app.getLoginItemSettings(options).status) {
				case 'enabled': return 'enabled';
				case 'not-registered': return 'disabled';
				case 'requires-approval': return 'blocked';
				default: return 'unavailable';
			}
		};
	} else return;

	return {
		get,
		set(openAtLogin) {
			let failed = false;
			try { app.setLoginItemSettings({ ...writeOptions, openAtLogin }); }
			catch { failed = true; }
			// Electron can silently fail to write, or the OS may require approval.
			// Never return the requested value in place of a read from the OS.
			const state = get();
			return { state, failed: failed || state !== (openAtLogin ? 'enabled' : 'disabled') };
		}
	};
}
