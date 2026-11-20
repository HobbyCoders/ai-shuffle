import { execFile } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { promisify } from 'node:util';
import { posix, win32 } from 'node:path';
import type { App, LoginItemSettingsOptions } from 'electron';

export type LoginItemState = 'enabled' | 'disabled' | 'blocked' | 'unavailable';
export interface LoginItemControl {
	get(): LoginItemState;
	set(enabled: boolean): Promise<{ state: LoginItemState; failed: boolean }>;
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

// Use the stock Windows host to perform the write inside the same Win32 mutex
// that NSIS holds through removal/rollback (including same-directory upgrades).
// The Global namespace covers the same install across logon sessions. Unlike a
// lockfile, ownership is released by Windows if either process dies.
const windowsWriteScript = String.raw`
$ErrorActionPreference = 'Stop'
$mutex = [Threading.Mutex]::new($false, ('Global\AIShuffle.Startup.' + $env:SHUFFLE_LOGIN_GENERATION))
try {
    try { if (-not $mutex.WaitOne(5000)) { exit 1 } }
    catch [Threading.AbandonedMutexException] { } # ownership transferred to us
    try {
        if (-not [IO.File]::Exists($env:SHUFFLE_LOGIN_EXE) -or
            [IO.File]::ReadAllText($env:SHUFFLE_LOGIN_STAMP, [Text.Encoding]::Unicode) -cne $env:SHUFFLE_LOGIN_RECEIPT) { exit 1 }
        $run = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Microsoft\Windows\CurrentVersion\Run')
        try {
            $name = $env:SHUFFLE_LOGIN_NAME
            $command = '"' + $env:SHUFFLE_LOGIN_EXE + '"'
            $existing = $run.GetValue($name, $null)
            if ($null -ne $existing -and $existing -ne $command) { exit 1 }
            if ($env:SHUFFLE_LOGIN_ENABLE -eq '1') { $run.SetValue($name, $command, 'String') }
            else { $run.DeleteValue($name, $false) }
            $approved = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', $true)
            if ($null -ne $approved) {
                try { $approved.DeleteValue($name, $false) } finally { $approved.Dispose() }
            }
        } finally { $run.Dispose() }
    } finally { $mutex.ReleaseMutex() }
} finally { $mutex.Dispose() }
`;
const windowsWrite = async (values: Record<string, string>) => {
	await promisify(execFile)(win32.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
		['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(windowsWriteScript, 'utf16le').toString('base64')],
		{ windowsHide: true, timeout: 15_000, env: { ...process.env, ...values } });
};

/** Only user-initiated writes. The OS, not a saved preference, is the source of truth. */
export function loginItemControl(app: LoginItemApi, host: Host, windows = {
	readStamp: readFileSync, exists: existsSync, write: windowsWrite
}): LoginItemControl | undefined {
	if (host.preview || typeof app.getLoginItemSettings !== 'function' || typeof app.setLoginItemSettings !== 'function') return;

	let options: LoginItemSettingsOptions;
	let get: LoginItemControl['get'];
	let write: (enabled: boolean) => void | Promise<void>;
	if (host.platform === 'win32') {
		// The installed entry point is stable across source updates. Never fall back
		// to process.execPath: in the updated app it is the versioned npm Electron.
		const executable = host.installedExecutable;
		if (!executable || !win32.isAbsolute(executable) || !/\.exe$/i.test(executable) || /["\0]/.test(executable)) return;
		// Without a per-user installer stamp the uninstaller cannot promise to
		// clean up this Run entry. An in-app update alone must not enable it.
		if (!host.localAppData) return;
		const stamp = win32.join(win32.dirname(executable), 'resources', 'login-item-uninstall-v4');
		const read = () => {
			try { return windows.readStamp(stamp, 'utf16le').replace(/^\uFEFF/, ''); }
			catch { return ''; }
		};
		const receipt = read();
		const generation = receipt.slice(0, 38);
		if (!/^\{[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}\}$/i.test(generation) ||
			receipt.slice(38).toLowerCase() !== host.localAppData.toLowerCase() || !windows.exists(executable)) return;
		const stillInstalled = () => read() === receipt && windows.exists(executable);
		write = enabled => windows.write({
			SHUFFLE_LOGIN_GENERATION: generation, SHUFFLE_LOGIN_RECEIPT: receipt,
			SHUFFLE_LOGIN_EXE: executable, SHUFFLE_LOGIN_STAMP: stamp, SHUFFLE_LOGIN_NAME: host.appId,
			SHUFFLE_LOGIN_ENABLE: enabled ? '1' : '0'
		});
		// Electron parses the lookup path as a Windows command line. The install directory may have spaces.
		options = { path: `"${executable}"`, args: [] };
		get = () => {
			if (!stillInstalled()) return 'unavailable';
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
		options = { type: 'mainAppService' };
		write = openAtLogin => app.setLoginItemSettings({ ...options, openAtLogin });
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
		async set(openAtLogin) {
			let failed = false;
			try { await write(openAtLogin); }
			catch { failed = true; }
			// A write can fail silently, or the OS may require approval. Report readback.
			const state = get();
			return { state, failed: failed || state !== (openAtLogin ? 'enabled' : 'disabled') };
		}
	};
}
