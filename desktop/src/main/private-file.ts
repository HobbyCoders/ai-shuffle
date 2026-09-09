/** Atomic, owner-private storage for generated local credentials. */
import { promises as fs } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

export async function privateDirectory(directory: string): Promise<void> {
	await fs.mkdir(directory, { recursive: true, mode: 0o700 });
	if (process.platform !== 'win32') {
		await fs.chmod(directory, 0o700);
		return;
	}
	// chmod is not an ACL on Windows. Protect the directory BEFORE writing
	// secrets; children inherit only the current account's full-control ACE.
	const script = [
		'$ErrorActionPreference = "Stop"',
		'$acl = New-Object System.Security.AccessControl.DirectorySecurity',
		'$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User',
		'$acl.SetOwner($sid)',
		'$acl.SetAccessRuleProtection($true, $false)',
		'$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")',
		'$acl.AddAccessRule($rule)',
		// Set-Acl's provider can also try to write the audit section, requiring
		// SeSecurityPrivilege on an already protected directory. Persist only
		// the owner and access sections changed above; no audit ACL is touched.
		'[System.IO.Directory]::SetAccessControl($env:SHUFFLE_PRIVATE_DIRECTORY, $acl)'
	].join('; ');
	const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
		env: { ...process.env, SHUFFLE_PRIVATE_DIRECTORY: directory }, windowsHide: true, encoding: 'utf8', timeout: 15_000
	});
	if (result.error || result.status !== 0) {
		const detail = result.error?.message || result.stderr.trim() || `PowerShell exited with code ${result.status}`;
		throw new Error(`Could not protect the local credential directory (${directory}). ${detail}`);
	}
}

export async function writePrivateFile(file: string, content: string): Promise<void> {
	await privateDirectory(path.dirname(file));
	const tmp = `${file}.${randomUUID()}.tmp`;
	try {
		await fs.writeFile(tmp, content, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
		await fs.rename(tmp, file);
	} finally {
		await fs.rm(tmp, { force: true });
	}
}
