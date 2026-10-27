/**
 * The browser side of version-skew handling (see app-update.ts), installed
 * once by the root layout on every client: the desktop web UI, /mobile/ in
 * a browser and the phone apps' WebView.
 *
 * The question "is there a newer build?" is SvelteKit's own: `updated.check()`
 * fetches this build's `_app/version.json` (under the app's base, so
 * `/mobile/_app/version.json` for the phone UI) with no-cache request headers
 * (the desktop service worker answers it from the network only) and
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
	AUTO_RELOAD_COOLDOWN_MS,
	createAppUpdateMonitor,
	isChunkLoadError,
	registerReloadBlocker,
	reloadBlockers,
	setAppUpdateListener,
	type AppUpdateMonitor
} from './app-update';

// ── Loop guard and cap ──────────────────────────────────────────────────────

/** The last automatic reload in this tab: the build it left and when. */
const AUTO_RELOAD_KEY = 'ai-shuffle-update-auto-reload';

function lastAutoReload(): { from: string; at: number } | null {
	try {
		const raw = JSON.parse(sessionStorage.getItem(AUTO_RELOAD_KEY) ?? 'null');
		return raw && typeof raw.from === 'string' && typeof raw.at === 'number' ? raw : null;
	} catch {
		return null;
	}
}

/**
 * An automatic reload is allowed unless the last one left this very build
 * (it came back on it: a version file that does not match the pages) or
 * ran within the cooldown (replicas flapping between two builds).
 */
export function autoReloadAllowed(now = Date.now()): boolean {
	const last = lastAutoReload();
	if (!last) return true;
	return last.from !== version && now - last.at >= AUTO_RELOAD_COOLDOWN_MS;
}

function reloadPage(): void {
	try {
		sessionStorage.setItem(AUTO_RELOAD_KEY, JSON.stringify({ from: version, at: Date.now() }));
	} catch { /* storage off: no loop guard */ }
	window.location.reload();
}

// ── Unsaved input anywhere on the page ──────────────────────────────────────
//
// A field's `defaultValue` is no guide: Svelte's `bind:value` sets the live
// value only, so every bound field showing a saved value (a note title, a
// project path, a filled search box) would look edited. Instead the page
// remembers what each field held the moment the user first reached for it
// (focus, a press, a key, or its label) and calls it unsaved while it holds
// something else. A field nobody touched is clean whatever the app put in it.

/** Input types whose value the user changes. Buttons, hidden and file inputs are not. */
const EDITABLE_INPUTS = new Set([
	'', 'text', 'search', 'email', 'url', 'tel', 'password', 'number',
	'date', 'datetime-local', 'month', 'time', 'week',
	'checkbox', 'radio', 'range', 'color'
]);

type Field = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

/** A value no field ever holds: the field changed before its start was seen. */
const UNKNOWN_START = '\u0000unknown';

/** Each touched field's value from before the user touched it. */
let initialValues = new WeakMap<Field, string>();
let lastEditable: HTMLElement | null = null;

function isField(el: unknown): el is Field {
	return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}

function tracked(el: Field): boolean {
	if (el.disabled) return false;
	if (el instanceof HTMLInputElement) return EDITABLE_INPUTS.has(el.type) && !el.readOnly;
	if (el instanceof HTMLTextAreaElement) return !el.readOnly;
	return true;
}

/** What a field holds now, comparable with what it held before. */
function fieldValue(el: Field): string {
	if (el instanceof HTMLSelectElement) {
		return Array.from(el.options).filter((o) => o.selected).map((o) => o.value).join('\n');
	}
	if (el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio')) {
		return el.checked ? 'on' : 'off';
	}
	return el.value;
}

/** The other buttons of a radio's group: choosing one unchecks them. */
function radioGroup(el: HTMLInputElement): HTMLInputElement[] {
	if (!el.name) return [el];
	const root = el.form ?? el.getRootNode();
	if (!(root instanceof Document || root instanceof DocumentFragment || root instanceof HTMLFormElement)) return [el];
	return Array.from(root.querySelectorAll<HTMLInputElement>('input[type="radio"]'))
		.filter((r) => r.name === el.name && r.form === el.form);
}

/** Remember what `el` holds before the user changes it (first time only). */
function rememberStart(el: Field): void {
	if (!tracked(el)) return;
	const fields = el instanceof HTMLInputElement && el.type === 'radio' ? radioGroup(el) : [el];
	for (const field of fields) {
		if (!initialValues.has(field)) initialValues.set(field, fieldValue(field));
	}
}

/** The field an event is about to change: the target, or the control of a label pressed. */
function fieldFor(target: EventTarget | null): Field | null {
	if (isField(target)) return target;
	if (target instanceof Element) {
		const control = target.closest('label')?.control;
		if (isField(control)) return control;
	}
	return null;
}

function onFocusIn(event: FocusEvent): void {
	const target = event.target;
	if (target instanceof HTMLElement && (target.isContentEditable || target.matches('input, textarea'))) {
		lastEditable = target;
	}
	const field = fieldFor(target);
	if (field) rememberStart(field);
}

/** A press or key can change a toggle that never takes focus (Safari, a label). */
function onBeforeChange(event: Event): void {
	const field = fieldFor(event.target);
	if (field) rememberStart(field);
}

/** A change whose start nobody saw (autofill, a script's event): unknown, so unsaved. */
function onChange(event: Event): void {
	const field = fieldFor(event.target);
	if (!field || !tracked(field) || initialValues.has(field)) return;
	if (field instanceof HTMLInputElement && field.type === 'checkbox') {
		initialValues.set(field, field.checked ? 'off' : 'on');
	} else {
		initialValues.set(field, UNKNOWN_START);
	}
}

const PRE_CHANGE_EVENTS = ['pointerdown', 'mousedown', 'touchstart', 'keydown'] as const;

/**
 * Why a reload would lose something entered into the page, or null.
 *
 * Every settings and editor form, approval feedback, a question's answer, a
 * provider key: a field (text, select, checkbox, radio, range) the user
 * changed from what it held when they first touched it, a file input
 * holding files, or the focused (or last focused) rich-text editor holding
 * text. A field changed back to its start is clean again.
 */
export function unsavedFieldReason(doc: Document = document): string | null {
	for (const el of doc.querySelectorAll<Field>('input, textarea, select')) {
		if (el instanceof HTMLInputElement && el.type === 'file') {
			if (!el.disabled && el.files?.length) return 'file-input';
			continue;
		}
		if (!tracked(el)) continue;
		const start = initialValues.get(el);
		if (start !== undefined && fieldValue(el) !== start) return 'unsaved-field';
	}
	for (const el of [doc.activeElement, lastEditable]) {
		if (el instanceof HTMLElement && el.isConnected && el.isContentEditable && el.textContent?.trim()) {
			return 'unsaved-editor';
		}
	}
	return null;
}

let monitor: AppUpdateMonitor | null = null;
let bannerId: string | null = null;
let bannerBroken = false;

function showBanner(broken: boolean): void {
	// The feature that broke may already offer its own Reload (the voice
	// call's outcome banner, category 'app-update'): one on screen, not two.
	// It takes the place of this one whichever came first.
	const live = get(liveBanners);
	if (live.some((b) => b.category === 'app-update' && b.id !== bannerId)) {
		if (bannerId && live.some((b) => b.id === bannerId)) notify.dismissLive(bannerId);
		return;
	}
	// One banner: shown once, upgraded once if a feature then fails.
	if (bannerId && (bannerBroken || !broken)) return;
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
		autoReloadAllowed: () => autoReloadAllowed(),
		showBanner
	});
	monitor = current;
	const unblock = registerReloadBlocker(() => unsavedFieldReason());

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

	document.addEventListener('focusin', onFocusIn, true);
	for (const type of PRE_CHANGE_EVENTS) document.addEventListener(type, onBeforeChange, { capture: true, passive: true });
	document.addEventListener('input', onChange, true);
	document.addEventListener('change', onChange, true);
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
		unblock();
		lastEditable = null;
		initialValues = new WeakMap();
		document.removeEventListener('focusin', onFocusIn, true);
		for (const type of PRE_CHANGE_EVENTS) document.removeEventListener(type, onBeforeChange, true);
		document.removeEventListener('input', onChange, true);
		document.removeEventListener('change', onChange, true);
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
