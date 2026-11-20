const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const { closeSync, mkdtempSync, mkdirSync, openSync, rmSync, writeFileSync } = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createInterface } = require('node:readline');
const { loginItemControl } = require('../../dist/main/login-item.js');

// Run the real filesystem guard in the same Electron runtime as the installed shell.
assert.ok(process.versions.electron, 'expected to run under Electron');
async function verifyGuard() {
	const win = JSON.parse(process.argv[2]);
	const dir = mkdtempSync(path.join(os.tmpdir(), 'shuffle-startup-guard-'));
	try {
		const installedExecutable = path.join(dir, 'AI Shuffle.exe');
		const resources = path.join(dir, 'resources');
		const host = { ...win, installedExecutable, localAppData: dir };
		const guard = path.join(dir, 'ai-shuffle-desktop', 'login-item-lock');
		mkdirSync(path.dirname(guard));
		writeFileSync(guard, '');
		writeFileSync(installedExecutable, '');
		mkdirSync(resources);
		writeFileSync(path.join(resources, 'login-item-uninstall-v3'), `\uFEFF${dir}`, 'utf16le');
		const powershell = script => ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')];
		const psPath = file => `'${file.replaceAll("'", "''")}'`;
		const openExclusive = `[IO.File]::Open(${psPath(guard)}, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)`;
		let reads = 0;
		let writes = 0;
		const control = loginItemControl({
			setLoginItemSettings() { writes++; },
			getLoginItemSettings() {
				reads++;
				const probe = spawnSync('powershell.exe', powershell(`try { (${openExclusive}).Dispose(); exit 1 } catch [IO.IOException] { if ((($_.Exception.GetBaseException()).HResult -band 65535) -ne 32) { throw }; exit 0 }`), {
					encoding: 'utf8', timeout: 15_000
				});
				assert.ifError(probe.error);
				assert.equal(probe.status, 0, probe.stdout + probe.stderr);
				return { openAtLogin: false };
			}
		}, host);
		assert.deepEqual(control.set(false), { state: 'disabled', failed: false });
		assert.deepEqual([writes, reads], [1, 1]);

		// Redirected PowerShell stdin can reach EOF in the Windows test shard.
		// Acknowledge the lock on stdout and wait for an explicit release file.
		const release = path.join(dir, 'release');
		const holder = spawn('powershell.exe', powershell(`
			$ErrorActionPreference = 'Stop'
			$lock = ${openExclusive}
			try {
				[Console]::WriteLine('held')
				while (-not [IO.File]::Exists(${psPath(release)})) { Start-Sleep -Milliseconds 25 }
			} finally { $lock.Dispose() }
		`), { stdio: ['ignore', 'pipe', 'pipe'], timeout: 15_000 });
		const closed = new Promise(resolve => {
			holder.once('error', error => resolve({ error }));
			holder.once('close', (status, signal) => resolve({ status, signal }));
		});
		let stderr = '';
		holder.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
		const lines = createInterface({ input: holder.stdout });
		try {
			const held = await Promise.race([once(lines, 'line').then(([line]) => line), closed.then(() => stderr)]);
			assert.equal(held, 'held', 'native holder must acknowledge opening the guard');
			assert.throws(() => closeSync(openSync(guard, 'r')), { code: 'EBUSY' }, 'the real guard path must be locked');
			assert.deepEqual(control.set(true), { state: 'unavailable', failed: true });
			assert.deepEqual([writes, reads], [1, 1], 'no Electron call while the native handle is held');
		} finally {
			lines.close();
			writeFileSync(release, '');
			const { error, status, signal } = await closed;
			assert.ifError(error);
			assert.equal(status, 0, stderr || signal);
		}
		assert.deepEqual(control.set(false), { state: 'disabled', failed: false }, 'can write again when the exclusive holder exits');
		assert.deepEqual([writes, reads], [2, 2]);
	} finally { rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
}

verifyGuard().catch(error => {
	console.error(error);
	process.exitCode = 1;
});
