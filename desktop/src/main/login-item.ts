import { closeSync, existsSync, openSync, readFileSync } from 'node:fs';
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
	localAppData?: string;
	appPath: string;
	systemVersion: string;
	appId: string;
}

type LoginItemApi = Pick<App, 'getLoginItemSettings' | 'setLoginItemSettings'>;

// Matches the per-user path in installer-channel.nsh. No data is stored in it.
function withWindowsGuard<T>(file: string, change: () => T): T | undefined {
	let fd: number;
	try { fd = openSync(file, 'r'); }
	catch { return; }
	try { return change(); }
	finally { closeSync(fd); }
}

type WindowsFiles = {
	readStamp: (path: string, encoding: 'utf16le') => string;
	exists: typeof existsSync;
	guard: typeof withWindowsGuard;
};

/** Only user-initiated writes. The OS, not a saved preference, is the source of truth. */
export function loginItemControl(app: LoginItemApi, host: Host, files: WindowsFiles = {
	readStamp: readFileSync, exists: existsSync, guard: withWindowsGuard
}): LoginItemControl | undefined {
	if (host.preview || typeof app.getLoginItemSettings !== 'function' || typeof app.setLoginItemSettings !== 'function') return;

	let options: LoginItemSettingsOptions;
	let get: LoginItemControl['get'];
	let writeOptions: Omit<Settings, 'openAtLogin'>;
	let guard: (change: () => ReturnType<LoginItemControl['set']>) => ReturnType<LoginItemControl['set']> | undefined = change => change();
	if (host.platform === 'win32') {
		// The installed entry point is stable across source updates. Never fall back
		// to process.execPath: in the updated app it is the versioned npm Electron.
		const executable = host.installedExecutable;
		if (!executable || !win32.isAbsolute(executable) || !/\.exe$/i.test(executable) || /["\0]/.test(executable)) return;
		// Without a per-user installer stamp the uninstaller cannot promise to
		// clean up this Run entry. An in-app update alone must not enable it.
		if (!host.localAppData) return;
		const localAppData = host.localAppData.toLowerCase();
		const stamp = win32.join(win32.dirname(executable), 'resources', 'login-item-uninstall-v3');
		const stillInstalled = () => {
			try {
				return files.readStamp(stamp, 'utf16le').replace(/^\uFEFF/, '').toLowerCase() === localAppData && files.exists(executable);
			} catch { return false; }
		};
		if (!stillInstalled()) return;
		const guardFile = win32.join(host.localAppData, 'ai-shuffle-desktop', 'login-item-lock');
		if (!files.exists(guardFile)) return;
		// Recheck *under* the guard. This Electron process can outlive an uninstall;
		// an earlier capability check cannot authorize a later registry write.
		guard = change => files.guard(guardFile, () => stillInstalled() ? change() : undefined);
		// Electron parses the lookup path as a Windows command line (and strips
		// quotes before writing). The product name and install directory can have spaces.
		options = { path: `"${executable}"`, args: [] };
		writeOptions = { ...options, name: host.appId };
		get = () => {
			if (!stillInstalled() || !files.exists(guardFile)) return 'unavailable';
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
			return guard(() => {
				let failed = false;
				try { app.setLoginItemSettings({ ...writeOptions, openAtLogin }); }
				catch { failed = true; }
				// Electron can silently fail to write, or the OS may require approval.
				// Never return the requested value in place of a read from the OS.
				const state = get();
				return { state, failed: failed || state !== (openAtLogin ? 'enabled' : 'disabled') };
			}) ?? { state: 'unavailable', failed: true };
		}
	};
}
