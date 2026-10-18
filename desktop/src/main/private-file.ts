/** Atomic, owner-private storage for generated local credentials. */
import { promises as fs } from 'node:fs';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { promisify } from 'node:util';

/**
 * The Windows ACL pass. Writing a directory ACL makes Windows re-propagate it
 * to every descendant, and userData holds the updater's versions, runtimes and
 * dependencies (about 620,000 files on one install: a 103 s pass, which failed
 * startup at the old 15 s limit). So it reads the ACL first and exits with
 * "unchanged" when the owner, protection and single full-control rule already
 * match what it would write; otherwise it writes them and prints "protected".
 */
export const PRIVATE_DIRECTORY_SCRIPT = [
	'$ErrorActionPreference = "Stop"',
	'$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User',
	'$current = [System.IO.Directory]::GetAccessControl($env:SHUFFLE_PRIVATE_DIRECTORY)',
	'$rules = @($current.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))',
	'if ($current.AreAccessRulesProtected -and $current.GetOwner([System.Security.Principal.SecurityIdentifier]) -eq $sid -and $rules.Count -eq 1 -and $rules[0].IdentityReference -eq $sid -and $rules[0].AccessControlType -eq "Allow" -and $rules[0].FileSystemRights -eq "FullControl" -and $rules[0].InheritanceFlags -eq "ContainerInherit,ObjectInherit" -and $rules[0].PropagationFlags -eq "None") { "unchanged"; exit 0 }',
	'$acl = New-Object System.Security.AccessControl.DirectorySecurity',
	'$acl.SetOwner($sid)',
	'$acl.SetAccessRuleProtection($true, $false)',
	'$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")',
	'$acl.AddAccessRule($rule)',
	// Set-Acl's provider can also try to write the audit section, requiring
	// SeSecurityPrivilege on an already protected directory. Persist only
	// the owner and access sections changed above; no audit ACL is touched.
	'[System.IO.Directory]::SetAccessControl($env:SHUFFLE_PRIVATE_DIRECTORY, $acl)',
	'"protected"'
].join('; ');

/** Create the directory and make it private to the current account. */
export async function privateDirectory(directory: string): Promise<void> {
	await protectDirectory(directory);
}

/**
 * privateDirectory, reporting what it did: 0700, or on Windows a protected ACL
 * with only the current account's full-control rule. Resolves true when it
 * changed the Windows ACL, false when it already matched (always false elsewhere).
 */
export async function protectDirectory(directory: string): Promise<boolean> {
	await fs.mkdir(directory, { recursive: true, mode: 0o700 });
	if (process.platform !== 'win32') {
		await fs.chmod(directory, 0o700);
		return false;
	}
	// chmod is not an ACL on Windows. Protect the directory BEFORE writing
	// secrets; children inherit only the current account's full-control ACE.
	// Asynchronous, so a needed pass over a large tree never freezes the main
	// process, and with room for that one-off pass to finish.
	try {
		const { stdout } = await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', PRIVATE_DIRECTORY_SCRIPT], {
			env: { ...process.env, SHUFFLE_PRIVATE_DIRECTORY: directory }, windowsHide: true, encoding: 'utf8', timeout: 180_000
		});
		return stdout.trim() !== 'unchanged';
	} catch (error) {
		const failure = error as Error & { stderr?: string; killed?: boolean };
		const detail = failure.killed ? 'PowerShell did not finish within 180 s' : (failure.stderr?.trim() || failure.message);
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
