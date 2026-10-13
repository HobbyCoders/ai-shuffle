// Decide how a desktop build is signed from the secrets the workflow passed in.
//
//   node scripts/signing.mjs <win|mac|linux> [--args-file FILE] [--github-env]
//
// Reads only whether each secret is set, never prints a value. Prints a
// one-line summary. With --args-file, writes the extra electron-builder
// arguments one per line (the Azure Trusted Signing account settings). With
// --github-env, appends CSC_IDENTITY_AUTO_DISCOVERY to $GITHUB_ENV and the
// result to $GITHUB_OUTPUT (`signing_mode`, `signed`, `notarized`). The build
// step itself maps the secrets to electron-builder's variable names
// (WIN_CSC_LINK, CSC_LINK, APPLE_*, AZURE_*); a secret that is not set is an
// empty string there, which electron-builder ignores.
//
// No secrets at all (forks, pull requests, an owner who has not bought a
// certificate yet): an unsigned build, exactly as before. Some but not all of
// one method's secrets: fail, so a release never ships unsigned by accident.
// DESKTOP_REQUIRE_SIGNING=true: fail unless the build will be signed.
// Every secret is documented in desktop/RELEASING.md.
import { appendFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const set = (env, name) => typeof env[name] === 'string' && env[name].trim() !== '';

/** All or nothing: which of `names` are missing, when at least one is set. */
function partial(env, names) {
	const present = names.filter((name) => set(env, name));
	return present.length > 0 && present.length < names.length ? names.filter((name) => !set(env, name)) : null;
}

export const AZURE = ['AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_TRUSTED_SIGNING_ENDPOINT', 'AZURE_TRUSTED_SIGNING_ACCOUNT', 'AZURE_TRUSTED_SIGNING_PROFILE', 'AZURE_TRUSTED_SIGNING_PUBLISHER'];
export const WINDOWS_PFX = ['WINDOWS_CERTIFICATE_PFX_BASE64', 'WINDOWS_CERTIFICATE_PASSWORD'];
export const MAC_CERTIFICATE = ['MACOS_CERTIFICATE_P12_BASE64', 'MACOS_CERTIFICATE_PASSWORD'];
/** The build step writes APPLE_API_KEY_P8 to a file and passes its path as APPLE_API_KEY. */
export const MAC_API_KEY = ['APPLE_API_KEY_P8', 'APPLE_API_KEY_ID', 'APPLE_API_ISSUER'];
export const MAC_APPLE_ID = ['APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID'];

export class SigningError extends Error {}

/**
 * @returns {{ platform: string, mode: string, signed: boolean, notarized: boolean, args: string[], env: Record<string, string>, notes: string[] }}
 * `env` holds only non-secret settings for the build step.
 */
export function planSigning(platform, env = process.env) {
	const plan = { platform, mode: 'unsigned', signed: false, notarized: false, args: [], env: {}, notes: [] };
	const incomplete = (label, names) => {
		const missing = partial(env, names);
		if (missing) throw new SigningError(`${label} is only partly configured. Missing: ${missing.join(', ')}. Set all of them or none.`);
	};
	if (platform === 'win') {
		incomplete('Azure Trusted Signing', AZURE);
		incomplete('Windows PFX signing', WINDOWS_PFX);
		const azure = AZURE.every((name) => set(env, name));
		const pfx = WINDOWS_PFX.every((name) => set(env, name));
		if (azure && pfx) throw new SigningError('Both Azure Trusted Signing and a Windows PFX are configured. Remove one set of secrets.');
		if (azure) {
			plan.mode = 'azure-trusted-signing';
			plan.args = [
				`--config.win.azureSignOptions.endpoint=${env.AZURE_TRUSTED_SIGNING_ENDPOINT.trim()}`,
				`--config.win.azureSignOptions.codeSigningAccountName=${env.AZURE_TRUSTED_SIGNING_ACCOUNT.trim()}`,
				`--config.win.azureSignOptions.certificateProfileName=${env.AZURE_TRUSTED_SIGNING_PROFILE.trim()}`,
				`--config.win.azureSignOptions.publisherName=${env.AZURE_TRUSTED_SIGNING_PUBLISHER.trim()}`
			];
			// The TrustedSigning module reads AZURE_TENANT_ID, AZURE_CLIENT_ID and
			// AZURE_CLIENT_SECRET (the Entra app registration) from the environment.
		} else if (pfx) {
			plan.mode = 'windows-pfx';
		}
	} else if (platform === 'mac') {
		incomplete('The Developer ID certificate', MAC_CERTIFICATE);
		incomplete('Notarization with an App Store Connect API key', MAC_API_KEY);
		incomplete('Notarization with an Apple ID', MAC_APPLE_ID);
		const certificate = MAC_CERTIFICATE.every((name) => set(env, name));
		const apiKey = MAC_API_KEY.every((name) => set(env, name));
		const appleId = MAC_APPLE_ID.every((name) => set(env, name));
		if (apiKey && appleId) throw new SigningError('Both notarization methods are configured. Keep the App Store Connect API key or the Apple ID, not both.');
		if ((apiKey || appleId) && !certificate) throw new SigningError('Notarization is configured without the Developer ID certificate; Apple only notarizes signed apps.');
		if (certificate) {
			plan.mode = 'developer-id';
			if (apiKey || appleId) {
				// electron-builder notarizes with notarytool when the APPLE_* variables are set.
				plan.notarized = true;
				plan.mode = apiKey ? 'developer-id+notary-api-key' : 'developer-id+notary-apple-id';
			} else {
				// With no APPLE_* credentials electron-builder skips notarization.
				plan.notes.push('Signed but not notarized: Gatekeeper will still warn. Add the notarization secrets.');
			}
		}
	} else if (platform === 'linux') {
		plan.notes.push('AppImage and deb ship unsigned; SHA256SUMS next to them is the integrity check.');
	} else {
		throw new SigningError(`Unknown platform ${platform}; use win, mac or linux.`);
	}
	plan.signed = plan.mode !== 'unsigned';
	if (!plan.signed) {
		// Never let electron-builder pick up whatever identity the runner has.
		plan.env.CSC_IDENTITY_AUTO_DISCOVERY = 'false';
		if (platform !== 'linux') plan.notes.push('No signing secrets: building unsigned.');
	}
	if (platform !== 'linux' && !plan.signed && /^(1|true|yes)$/i.test(env.DESKTOP_REQUIRE_SIGNING ?? '')) {
		throw new SigningError('DESKTOP_REQUIRE_SIGNING is set but no signing secrets are configured. See desktop/RELEASING.md.');
	}
	return plan;
}

function main(argv) {
	const [platform, ...rest] = argv;
	const option = (name) => { const i = rest.indexOf(name); return i >= 0 ? rest[i + 1] : undefined; };
	let plan;
	try {
		plan = planSigning(platform);
	} catch (error) {
		if (!(error instanceof SigningError)) throw error;
		console.error(`::error::${error.message}`);
		process.exit(1);
	}
	const argsFile = option('--args-file');
	if (argsFile) writeFileSync(argsFile, plan.args.map((arg) => `${arg}\n`).join(''));
	if (rest.includes('--github-env')) {
		if (!process.env.GITHUB_ENV || !process.env.GITHUB_OUTPUT) throw new Error('--github-env needs GITHUB_ENV and GITHUB_OUTPUT');
		for (const [name, value] of Object.entries(plan.env)) appendFileSync(process.env.GITHUB_ENV, `${name}=${value}\n`);
		appendFileSync(process.env.GITHUB_OUTPUT, `signing_mode=${plan.mode}\nsigned=${plan.signed}\nnotarized=${plan.notarized}\n`);
	}
	// Names and mode only; never values.
	console.log(`Desktop signing for ${plan.platform}: ${plan.mode}${plan.notes.length ? ` (${plan.notes.join(' ')})` : ''}`);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) main(process.argv.slice(2));
