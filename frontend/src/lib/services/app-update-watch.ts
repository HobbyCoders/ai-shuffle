/**
 * The browser side of version-skew handling (see app-update.ts), installed
 * once by the root layout on every client: the desktop web UI, /mobile/ in
 * a browser and the phone apps' WebView.
 *
 * The question "is there a newer build?" is SvelteKit's own: `updated.check()`
 * fetches this build's `_app/version.json` (under the app's base, so
 * `/mobile/_app/version.json` for the phone UI) without the HTTP cache and
 * compares it with the version this page was built with. It is asked only
 * when something happened that can mean an update, never on a timer:
 *
 *  - the page becomes visible again (an app resumed, a tab switched back),
 *  - the browser comes back online or the page is restored from bfcache,
 *  - live sync reconnects after a gap (a worker update restarts the server),
 *  - a lazy import fails (`vite:preloadError`, or a caller's report).
 */
import { version } from '$app/environment';
import { updated } from '$app/state';
import { get } from 'svelte/store';
import { liveBanners, notify } from '$lib/stores/notifications';
import {
	APP_UPDATED_MESSAGE,
	APP_UPDATE_READY_MESSAGE,
	createAppUpdateMonitor,
	isChunkLoadError,
	reloadBlockers,
	setAppUpdateListener,
	type AppUpdateMonitor
} from './app-update';

/** The build an automatic reload last left from (this tab only). */
const RELOADED_FROM_KEY = 'ai-shuffle-update-reloaded-from';

function readReloadedFrom(): string | null {
	try { return sessionStorage.getItem(RELOADED_FROM_KEY); } catch { return null; }
}

function reloadPage(): void {
	try { sessionStorage.setItem(RELOADED_FROM_KEY, version); } catch { /* storage off: no loop guard */ }
	window.location.reload();
}

let monitor: AppUpdateMonitor | null = null;
let bannerId: string | null = null;
let bannerBroken = false;

function showBanner(broken: boolean): void {
	// One banner: shown once, upgraded once if a feature then fails.
	if (bannerId && (bannerBroken || !broken)) return;
	// The feature that broke may already say so with its own Reload (the
	// voice call's outcome banner): one message on screen, not two.
	if (!bannerId && broken && get(liveBanners).some((b) => b.message === APP_UPDATED_MESSAGE)) return;
	if (bannerId) notify.dismissLive(bannerId);
	bannerBroken = broken;
	bannerId = notify.banner(broken ? APP_UPDATED_MESSAGE : APP_UPDATE_READY_MESSAGE, {
		severity: broken ? 'warning' : 'info',
		category: 'app-update',
		duration_ms: 0,
		action: { label: 'Reload', onClick: () => monitor?.reloadNow() }
	});
}

/** The page's monitor, for the service-worker path and tests. */
export function appUpdateMonitor(): AppUpdateMonitor | null {
	return monitor;
}

/** Start watching. Returns the teardown. Calling it again while installed is a no-op. */
export function installAppUpdateWatch(): () => void {
	if (typeof window === 'undefined' || monitor) return () => {};
	const current = createAppUpdateMonitor({
		checkForNewBuild: () => updated.check(),
		isHidden: () => document.visibilityState === 'hidden',
		blockers: reloadBlockers,
		reload: reloadPage,
		// A reload that came back on the same build would only repeat itself.
		autoReloadAllowed: () => readReloadedFrom() !== version,
		showBanner
	});
	monitor = current;

	const onVisibility = () => {
		if (document.visibilityState === 'hidden') current.hidden();
		else void current.visible();
	};
	const onOnline = () => void current.check('online');
	// A page restored from the back/forward cache is a resume too.
	const onPageShow = (event: PageTransitionEvent) => { if (event.persisted) void current.visible(); };
	// Vite's preload helper reports a dependency that would not load. The
	// error is still thrown to the importer, which reports it its own way.
	const onPreloadError = () => void current.check('chunk');
	// A bare import() of a missing chunk that nothing caught.
	const onRejection = (event: PromiseRejectionEvent) => {
		if (isChunkLoadError(event.reason)) void current.check('chunk');
	};

	document.addEventListener('visibilitychange', onVisibility);
	window.addEventListener('online', onOnline);
	window.addEventListener('pageshow', onPageShow);
	window.addEventListener('vite:preloadError', onPreloadError);
	window.addEventListener('unhandledrejection', onRejection);
	setAppUpdateListener({
		reconnected: () => void current.check('reconnect'),
		chunkFailed: () => void current.check('chunk')
	});

	return () => {
		document.removeEventListener('visibilitychange', onVisibility);
		window.removeEventListener('online', onOnline);
		window.removeEventListener('pageshow', onPageShow);
		window.removeEventListener('vite:preloadError', onPreloadError);
		window.removeEventListener('unhandledrejection', onRejection);
		setAppUpdateListener(null);
		if (bannerId) notify.dismissLive(bannerId);
		bannerId = null;
		bannerBroken = false;
		if (monitor === current) monitor = null;
	};
}
