/**
 * Keep worker file links from replacing the app.
 *
 * Chat renders `/api/files/by-path?...` and `/api/generated-*` links as plain
 * anchors. A plain click on one navigates the whole window to the response:
 * fine when the server answers with an attachment (the browser turns the
 * navigation into a download), but a refusal (403 "outside workspace"), a
 * missing file or a server error replaces the app with a bare JSON page, and an
 * inline type (an image) does the same. In the desktop app there is no back
 * button, so the user is stranded.
 *
 * One delegated listener on the document handles every such click instead: it
 * fetches the file first, then saves it from a Blob on success, or shows a
 * short in-app error on failure. The window never leaves the app.
 *
 * Left alone: clicks another handler already took (the Android shell's own
 * download bridge listens earlier and prevents default), modified or non-primary
 * clicks (open in a new tab / window), and anchors with an explicit target
 * other than the current frame.
 */
import { apiOrigin, withAuth } from '$lib/api/runtime';
import { notify } from '$lib/stores/notifications';
import { filenameFromMediaUrl } from './media';

/** How long the error banner stays up. */
const ERROR_BANNER_MS = 7000;
/** Long enough for any browser (and the Android bridge) to take the Blob. */
const REVOKE_AFTER_MS = 60_000;
/** Show a "Downloading…" notice only when the fetch takes longer than this. */
const PROGRESS_AFTER_MS = 800;

/** The worker URL a click would navigate to, when it is an API response, else null. */
export function workerApiUrl(href: string, base: string = location.href): URL | null {
	let url: URL;
	try {
		url = new URL(href, base);
	} catch {
		return null;
	}
	if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
	const origins = new Set([new URL(base).origin]);
	const worker = apiOrigin();
	if (worker) {
		try { origins.add(new URL(worker).origin); } catch { /* not a URL: ignore */ }
	}
	if (!origins.has(url.origin)) return null;
	return url.pathname.startsWith('/api/') ? url : null;
}

/** A plain user click that would navigate the current frame. */
function navigatesHere(event: MouseEvent, anchor: HTMLAnchorElement): boolean {
	if (event.defaultPrevented || event.button !== 0) return false;
	if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
	const target = (anchor.getAttribute('target') || '').trim().toLowerCase();
	return target === '' || target === '_self' || target === '_top' || target === '_parent';
}

/** The server's `detail` string, when the error body carries one. */
async function serverDetail(response: Response): Promise<string> {
	const body = await response.json().catch(() => null);
	return body && typeof body.detail === 'string' ? body.detail : '';
}

/** What went wrong, in plain words, for a non-OK file response. */
export function describeFileError(status: number, detail = ''): string {
	const lower = detail.toLowerCase();
	if (status === 403 && lower.includes('outside')) {
		return 'This file is outside the folders AI Shuffle may share, so it cannot be downloaded.';
	}
	if (status === 401) return 'Your sign-in has expired. Sign in again, then retry the download.';
	if (status === 403) return 'You do not have access to this file.';
	if (status === 404) return 'This file no longer exists, or it has been moved.';
	if (status === 413) return 'This file is too large to download here.';
	if (status >= 500) return `The worker could not send this file (error ${status}). Try again in a moment.`;
	return detail ? `The file could not be downloaded: ${detail}` : `The file could not be downloaded (error ${status}).`;
}

/** `filename` / `filename*` from a Content-Disposition header. */
export function dispositionFilename(header: string | null): string | null {
	if (!header) return null;
	const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header);
	if (star) {
		try { return decodeURIComponent(star[1].trim()); } catch { /* fall through */ }
	}
	const plain = /filename\s*=\s*("?)([^";]+)\1/.exec(header);
	return plain ? plain[2].trim() : null;
}

function showError(message: string): void {
	notify.banner(message, { severity: 'error', title: 'Download failed', persist: false, duration_ms: ERROR_BANNER_MS });
}

function saveBlob(blob: Blob, filename: string): void {
	const objectUrl = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = objectUrl;
	a.download = filename;
	a.rel = 'noopener';
	a.style.display = 'none';
	document.body.appendChild(a);
	a.click();
	a.remove();
	setTimeout(() => URL.revokeObjectURL(objectUrl), REVOKE_AFTER_MS);
}

const inFlight = new Set<string>();

/** Fetch `url` and save it, or report why not. Never navigates. */
export async function downloadWorkerFile(url: URL, suggestedName = ''): Promise<boolean> {
	const key = url.href;
	if (inFlight.has(key)) return false;
	inFlight.add(key);
	// A large file can take a while to arrive; say so instead of looking dead.
	let progressId: string | null = null;
	const progressTimer = setTimeout(() => {
		const name = suggestedName || filenameFromMediaUrl(key) || 'file';
		progressId = notify.banner(`Downloading ${name}…`, { severity: 'info', persist: false, duration_ms: 0 });
	}, PROGRESS_AFTER_MS);
	try {
		let response: Response;
		try {
			response = await fetch(key, withAuth());
		} catch {
			showError('Could not reach the worker to download this file. Check the connection and try again.');
			return false;
		}
		if (!response.ok) {
			showError(describeFileError(response.status, await serverDetail(response)));
			return false;
		}
		const blob = await response.blob();
		const name = suggestedName
			|| dispositionFilename(response.headers.get('content-disposition'))
			|| filenameFromMediaUrl(key)
			|| 'download';
		saveBlob(blob, name);
		return true;
	} catch {
		showError('The download was interrupted. Try again.');
		return false;
	} finally {
		clearTimeout(progressTimer);
		if (progressId) notify.dismissLive(progressId);
		inFlight.delete(key);
	}
}

function onClick(event: MouseEvent): void {
	const origin = event.target instanceof Element ? event.target : null;
	const anchor = origin?.closest('a[href]') as HTMLAnchorElement | null;
	if (!anchor || !navigatesHere(event, anchor)) return;
	const url = workerApiUrl(anchor.getAttribute('href') ?? '');
	if (!url) return;
	event.preventDefault();
	void downloadWorkerFile(url, anchor.getAttribute('download')?.trim() ?? '');
}

/**
 * Install the guard on `doc`. Capture phase: SvelteKit's router takes every
 * same-origin anchor click on the app root (before it bubbles to the
 * document), prevents default and then navigates the window itself, so a
 * bubble-phase guard would never see these links. The Android shell's own
 * capture listener is registered at document start, earlier than this one,
 * so it still runs first and the guard steps aside when it took the click.
 * Returns the teardown.
 */
export function installFileLinkGuard(doc: Document = document): () => void {
	doc.addEventListener('click', onClick, true);
	return () => doc.removeEventListener('click', onClick, true);
}
