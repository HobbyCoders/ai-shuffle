import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const { privateDirectory, protectDirectory, writePrivateFile, PRIVATE_DIRECTORY_SCRIPT } = require('../dist/main/private-file.js');

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
		await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
	}
});

test('the Windows ACL pass skips exactly the rule it would write', () => {
	// The skip must compare the same owner, protection and rule the write sets;
	// a looser skip would leave a shared directory holding credentials.
	const lines = PRIVATE_DIRECTORY_SCRIPT.split('; ');
	const skip = lines.find(line => line.startsWith('if ($current.AreAccessRulesProtected'));
	assert.ok(skip, 'no skip check');
	for (const part of ['GetOwner([System.Security.Principal.SecurityIdentifier]) -eq $sid', '$rules.Count -eq 1', '$rules[0].IdentityReference -eq $sid',
		'$rules[0].AccessControlType -eq "Allow"', '$rules[0].FileSystemRights -eq "FullControl"', '$rules[0].InheritanceFlags -eq "ContainerInherit,ObjectInherit"',
		'$rules[0].PropagationFlags -eq "None"'])
		assert.ok(skip.includes(part), `the skip does not check ${part}`);
	assert.ok(PRIVATE_DIRECTORY_SCRIPT.includes('$rules[0].PropagationFlags -eq "None") { "unchanged"; exit 0 }'), 'the skip does not exit with "unchanged"');
	assert.ok(lines.includes('$acl.SetAccessRuleProtection($true, $false)'));
	assert.ok(lines.some(line => line.includes('FileSystemAccessRule($sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")')));
	// The skip comes before any write, and the write still happens after it.
	assert.ok(lines.indexOf(skip) < lines.findIndex(line => line.includes('SetAccessControl')));
	assert.equal(lines.at(-1), '"protected"');
});

test('an already private directory is not rewritten; a loosened one is', { skip: process.platform !== 'win32' && 'Windows ACLs' }, async () => {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-private-skip-'));
	try {
		const directory = path.join(root, 'userData');
		assert.equal(await protectDirectory(directory), true, 'the first pass did not protect the directory');
		// Every later launch: nothing to write, so nothing re-propagates.
		assert.equal(await protectDirectory(directory), false, 'an already private directory was rewritten');
		assert.equal(await protectDirectory(directory), false);
		// A directory someone loosened is still made private again.
		const loosen = [
			'$ErrorActionPreference = "Stop"',
			'$acl = [System.IO.Directory]::GetAccessControl($env:SHUFFLE_TEST_DIRECTORY)',
			'$everyone = New-Object System.Security.Principal.SecurityIdentifier("S-1-1-0")',
			'$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($everyone, "Read", "ContainerInherit,ObjectInherit", "None", "Allow")))',
			'[System.IO.Directory]::SetAccessControl($env:SHUFFLE_TEST_DIRECTORY, $acl)'
		].join('; ');
		const loosened = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', loosen], {
			env: { ...process.env, SHUFFLE_TEST_DIRECTORY: directory }, windowsHide: true, encoding: 'utf8'
		});
		assert.equal(loosened.status, 0, loosened.stderr);
		assert.equal(await protectDirectory(directory), true, 'a loosened directory was not made private again');
		assert.equal(await protectDirectory(directory), false);
	} finally {
		// root is the exact unique temporary directory created by this test.
		await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
	}
});
