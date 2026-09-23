import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { planSigning, AZURE, WINDOWS_PFX, MAC_CERTIFICATE, MAC_API_KEY, MAC_APPLE_ID } from '../scripts/signing.mjs';

const script = fileURLToPath(new URL('../scripts/signing.mjs', import.meta.url));
const filled = (names) => Object.fromEntries(names.map((name) => [name, `value-of-${name}`]));
// A secret that is not configured reaches the job as an empty string.
const blank = (names) => Object.fromEntries(names.map((name) => [name, '']));
const everything = [...AZURE, ...WINDOWS_PFX, ...MAC_CERTIFICATE, ...MAC_API_KEY, ...MAC_APPLE_ID];

test('no secrets (forks, pull requests) builds unsigned and never auto-discovers an identity', () => {
	for (const platform of ['win', 'mac', 'linux']) {
		const plan = planSigning(platform, blank(everything));
		assert.equal(plan.signed, false, platform);
		assert.equal(plan.mode, 'unsigned');
		assert.equal(plan.env.CSC_IDENTITY_AUTO_DISCOVERY, 'false');
	}
	assert.deepEqual(planSigning('mac', {}).args, []);
	assert.deepEqual(planSigning('win', {}).args, []);
});

test('Windows signs with Azure Trusted Signing through electron-builder azureSignOptions', () => {
	const plan = planSigning('win', { ...blank(everything), ...filled(AZURE), AZURE_TRUSTED_SIGNING_PUBLISHER: 'HobbyCoders LLC' });
	assert.equal(plan.mode, 'azure-trusted-signing');
	assert.equal(plan.signed, true);
	assert.deepEqual(plan.args, [
		'--config.win.azureSignOptions.endpoint=value-of-AZURE_TRUSTED_SIGNING_ENDPOINT',
		'--config.win.azureSignOptions.codeSigningAccountName=value-of-AZURE_TRUSTED_SIGNING_ACCOUNT',
		'--config.win.azureSignOptions.certificateProfileName=value-of-AZURE_TRUSTED_SIGNING_PROFILE',
		'--config.win.azureSignOptions.publisherName=HobbyCoders LLC'
	]);
	assert.deepEqual(plan.env, {}, 'secrets are mapped by the build step, never written to GITHUB_ENV');
});

test('Windows signs with a PFX instead when only the PFX secrets are set', () => {
	const plan = planSigning('win', filled(WINDOWS_PFX));
	assert.equal(plan.mode, 'windows-pfx');
	assert.deepEqual(plan.args, []);
});

test('partial or conflicting configuration fails instead of shipping unsigned', () => {
	assert.throws(() => planSigning('win', { AZURE_TENANT_ID: 't' }), /only partly configured.*AZURE_CLIENT_ID/);
	assert.throws(() => planSigning('win', { WINDOWS_CERTIFICATE_PFX_BASE64: 'x' }), /WINDOWS_CERTIFICATE_PASSWORD/);
	assert.throws(() => planSigning('win', { ...filled(AZURE), ...filled(WINDOWS_PFX) }), /Both/);
	assert.throws(() => planSigning('mac', filled(MAC_API_KEY)), /without the Developer ID certificate/);
	assert.throws(() => planSigning('mac', { ...filled(MAC_CERTIFICATE), ...filled(MAC_API_KEY), ...filled(MAC_APPLE_ID) }), /Both notarization/);
	assert.throws(() => planSigning('mac', { ...filled(MAC_CERTIFICATE), APPLE_ID: 'me@example.com' }), /APPLE_APP_SPECIFIC_PASSWORD/);
	assert.throws(() => planSigning('solaris', {}), /Unknown platform/);
});

test('macOS signs with Developer ID and notarizes with either notarytool credential', () => {
	const api = planSigning('mac', { ...filled(MAC_CERTIFICATE), ...filled(MAC_API_KEY) });
	assert.equal(api.mode, 'developer-id+notary-api-key');
	assert.equal(api.notarized, true);
	assert.deepEqual(api.args, []);
	const appleId = planSigning('mac', { ...filled(MAC_CERTIFICATE), ...filled(MAC_APPLE_ID) });
	assert.equal(appleId.mode, 'developer-id+notary-apple-id');
	const signedOnly = planSigning('mac', filled(MAC_CERTIFICATE));
	assert.equal(signedOnly.signed, true);
	assert.equal(signedOnly.notarized, false);
	assert.deepEqual(signedOnly.args, []);
});

test('DESKTOP_REQUIRE_SIGNING refuses an unsigned Windows or macOS release but not Linux', () => {
	assert.throws(() => planSigning('win', { DESKTOP_REQUIRE_SIGNING: 'true' }), /DESKTOP_REQUIRE_SIGNING/);
	assert.throws(() => planSigning('mac', { DESKTOP_REQUIRE_SIGNING: 'true' }), /DESKTOP_REQUIRE_SIGNING/);
	assert.equal(planSigning('linux', { DESKTOP_REQUIRE_SIGNING: 'true' }).signed, false);
	assert.equal(planSigning('win', { DESKTOP_REQUIRE_SIGNING: 'true', ...filled(WINDOWS_PFX) }).signed, true);
});

test('the CLI writes the args file and GitHub outputs and never prints a secret value', async (t) => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-signing-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	const files = { GITHUB_ENV: path.join(dir, 'env'), GITHUB_OUTPUT: path.join(dir, 'out') };
	const env = { PATH: process.env.PATH, ...files, ...filled(AZURE) };
	const stdout = execFileSync(process.execPath, [script, 'win', '--args-file', path.join(dir, 'args'), '--github-env'], { env, encoding: 'utf8' });
	assert.match(stdout, /azure-trusted-signing/);
	assert.equal(/value-of-/.test(stdout), false);
	assert.equal((await readFile(path.join(dir, 'args'), 'utf8')).trim().split('\n').length, 4);
	assert.match(await readFile(files.GITHUB_OUTPUT, 'utf8'), /signing_mode=azure-trusted-signing\nsigned=true\nnotarized=false/);
	assert.equal(/value-of-/.test(await readFile(files.GITHUB_ENV, 'utf8').catch(() => '')), false);
	const failed = spawnSync(process.execPath, [script, 'win'], { env: { PATH: process.env.PATH, AZURE_TENANT_ID: 'secret-tenant' }, encoding: 'utf8' });
	assert.equal(failed.status, 1);
	assert.match(failed.stderr, /::error::Azure Trusted Signing is only partly configured/);
	assert.equal(/secret-tenant/.test(failed.stdout + failed.stderr), false);
});
