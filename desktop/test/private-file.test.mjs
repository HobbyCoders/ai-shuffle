import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const { privateDirectory, writePrivateFile } = require('../dist/main/private-file.js');

test('private directories and credential writes survive reopening beneath a protected parent', async () => {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-private-reopen-'));
	try {
		const postgres = path.join(root, 'postgres');
		const worker = path.join(root, 'worker');
		const credential = path.join(root, 'worker-settings.json');
		// First startup creates children, then persisting credentials protects
		// the parent. Reopening used to fail when re-protecting those children.
		await privateDirectory(postgres);
		await writePrivateFile(credential, 'test settings');
		await writePrivateFile(path.join(worker, '.env'), 'test connection');
		for (let i = 0; i < 3; i++) {
			await privateDirectory(postgres);
			await privateDirectory(root);
			await writePrivateFile(path.join(worker, '.env'), 'test connection');
		}
		assert.equal(await fs.readFile(credential, 'utf8'), 'test settings');
		assert.equal(await fs.readFile(path.join(worker, '.env'), 'utf8'), 'test connection');
		if (process.platform === 'win32') {
			const script = [
				'$ErrorActionPreference = "Stop"',
				'$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User',
				'$paths = @($env:SHUFFLE_TEST_DIRECTORY, (Join-Path $env:SHUFFLE_TEST_DIRECTORY "postgres"), (Join-Path $env:SHUFFLE_TEST_DIRECTORY "worker"))',
				'foreach ($p in $paths) {',
				'$acl = [System.IO.Directory]::GetAccessControl($p)',
				'if (!$acl.AreAccessRulesProtected -or $acl.GetOwner([System.Security.Principal.SecurityIdentifier]) -ne $sid) { throw "Wrong owner or inheritance" }',
				'$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))',
				'if ($rules.Count -ne 1 -or $rules[0].IdentityReference -ne $sid -or $rules[0].AccessControlType -ne "Allow" -or $rules[0].FileSystemRights -ne "FullControl" -or $rules[0].InheritanceFlags -ne "ContainerInherit,ObjectInherit") { throw "Directory is not owner-private" }',
				'}'
			].join('; ');
			const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
				env: { ...process.env, SHUFFLE_TEST_DIRECTORY: root }, windowsHide: true, encoding: 'utf8'
			});
			assert.equal(result.status, 0, result.stderr);
		} else {
			assert.equal((await fs.stat(root)).mode & 0o777, 0o700);
			assert.equal((await fs.stat(credential)).mode & 0o777, 0o600);
		}
	} finally {
		// root is the exact unique temporary directory created by this test.
		await fs.rm(root, { recursive: true, force: true });
	}
});
