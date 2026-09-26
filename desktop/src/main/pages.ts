/**
 * The two documents the main window trusts, as pure URL checks (no Electron
 * imports, so the IPC guard and the navigation policy share one rule):
 *
 * - the bundled status page, `src/pages/worker.html` inside the app, by its
 *   exact file path (not "any file: URL");
 * - the connected worker's web UI, by its exact origin.
 */

export type TrustedPage = 'bundled' | 'worker';

export function originOf(url: string): string | null {
	try {
		const origin = new URL(url).origin;
		return origin === 'null' ? null : origin;
	} catch {
		return null;
	}
}

/** A file URL's path, decoded and with forward slashes; case-folded where the file system is. */
function filePath(url: URL, caseInsensitive: boolean): string {
	let value: string;
	try {
		value = decodeURIComponent(url.pathname);
	} catch {
		value = url.pathname;
	}
	value = value.replace(/\\/g, '/');
	return caseInsensitive ? value.toLowerCase() : value;
}

/** Whether `url` is the bundled status page (any query or hash), given that page's file URL. */
export function isBundledPage(url: string, bundledPageUrl: string, caseInsensitive = process.platform === 'win32'): boolean {
	let target: URL;
	let page: URL;
	try {
		target = new URL(url);
		page = new URL(bundledPageUrl);
	} catch {
		return false;
	}
	if (target.protocol !== 'file:' || page.protocol !== 'file:') return false;
	if (target.host !== page.host) return false;
	return filePath(target, caseInsensitive) === filePath(page, caseInsensitive);
}

/** Which trusted document `url` is, or null for anything else. */
export function trustedPage(url: string, workerOrigin: string | null, bundledPageUrl: string): TrustedPage | null {
	if (isBundledPage(url, bundledPageUrl)) return 'bundled';
	if (!workerOrigin || !/^https?:/.test(url)) return null;
	const origin = originOf(url);
	return origin !== null && origin === originOf(workerOrigin) ? 'worker' : null;
}

/** An http(s) URL that is safe to hand to the system browser, normalised, or null. */
export function externalHttpUrl(url: string): string | null {
	try {
		const parsed = new URL(url);
		if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
		if (parsed.username || parsed.password) return null;
		return parsed.href;
	} catch {
		return null;
	}
}
