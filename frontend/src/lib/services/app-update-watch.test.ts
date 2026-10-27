/**
 * The browser wiring of #707: the events that trigger a version check, the
 * one "App updated" banner and the reload.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const kit = vi.hoisted(() => ({ check: vi.fn(async () => true) }));
vi.mock('$app/state', () => ({ updated: { current: false, check: kit.check } }));
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn() } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));

import { autoReloadAllowed, installAppUpdateWatch, unsavedFieldReason } from './app-update-watch';
import {
	APP_UPDATED_MESSAGE,
	APP_UPDATE_READY_MESSAGE,
	AUTO_RELOAD_COOLDOWN_MS,
	CALL_LOAD_FAILED_MESSAGE,
	RESUME_GRACE_MS,
	registerReloadBlocker,
	reloadBlockers,
	reportServerReconnected
} from './app-update';
import { reportCallStartFailure } from './voiceCallOutcome';
import { notifications, notify } from '$lib/stores/notifications';

let visibility: DocumentVisibilityState = 'visible';
const reload = vi.fn();
let teardown: () => void = () => {};
const banners = () => notifications.current.live.filter((n) => n.category === 'app-update');
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function setVisibility(next: DocumentVisibilityState) {
	visibility = next;
	document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(1_000_000);
	visibility = 'visible';
	Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
	vi.stubGlobal('location', { ...window.location, reload });
	reload.mockClear();
	sessionStorage.clear();
	kit.check.mockClear().mockResolvedValue(true);
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	teardown = installAppUpdateWatch();
	// The page has been in use for a while.
	vi.setSystemTime(1_000_000 + RESUME_GRACE_MS + 1);
});

afterEach(() => {
	teardown();
	document.body.innerHTML = '';
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

it('the phone app coming back to the foreground on an old build reloads', async () => {
	setVisibility('hidden');
	setVisibility('visible');
	await flush();
	expect(kit.check).toHaveBeenCalledTimes(1);
	expect(reload).toHaveBeenCalledTimes(1);
	expect(banners()).toEqual([]);
});

it('a reload that came back on the same build offers the banner instead of looping', async () => {
	setVisibility('hidden');
	setVisibility('visible');
	await flush();
	expect(reload).toHaveBeenCalledTimes(1);
	// The "reloaded" page (same module state here) still runs the old build.
	teardown();
	teardown = installAppUpdateWatch();
	setVisibility('hidden');
	setVisibility('visible');
	await flush();
	expect(reload).toHaveBeenCalledTimes(1);
	expect(banners()).toHaveLength(1);
});

it('a reconnect while the user is on the page offers one neutral banner, then reloads on hide', async () => {
	reportServerReconnected();
	await flush();
	vi.setSystemTime(Date.now() + 60_000);
	reportServerReconnected();
	await flush();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toEqual([expect.objectContaining({ severity: 'info', message: APP_UPDATE_READY_MESSAGE, action: expect.objectContaining({ label: 'Reload' }) })]);

	setVisibility('hidden');
	expect(reload).toHaveBeenCalledTimes(1);
});

it("the banner's Reload reloads even over a draft", async () => {
	const undo = registerReloadBlocker(() => 'composer-draft');
	try {
		setVisibility('hidden');
		setVisibility('visible');
		await flush();
		expect(reload).not.toHaveBeenCalled();
		banners()[0].action!.onClick();
		expect(reload).toHaveBeenCalledTimes(1);
	} finally {
		undo();
	}
});

it('a lazy chunk that would not load upgrades the banner to "a feature broke"', async () => {
	reportServerReconnected();
	await flush();
	window.dispatchEvent(new Event('vite:preloadError'));
	await flush();
	expect(banners()).toEqual([expect.objectContaining({ severity: 'warning', message: APP_UPDATED_MESSAGE })]);
});

it('does not repeat what the voice call outcome banner already says', async () => {
	notify.banner(APP_UPDATED_MESSAGE, { category: 'app-update', action: { label: 'Reload', onClick: () => {} } });
	window.dispatchEvent(new Event('vite:preloadError'));
	await flush();
	expect(notifications.current.live.filter((n) => n.message === APP_UPDATED_MESSAGE)).toHaveLength(1);
});

it('stays quiet while the server runs this build', async () => {
	kit.check.mockResolvedValue(false);
	setVisibility('hidden');
	setVisibility('visible');
	window.dispatchEvent(new Event('online'));
	await flush();
	expect(reload).not.toHaveBeenCalled();
	expect(banners()).toEqual([]);
});

it('the voice outcome banner takes the place of an update banner already showing', async () => {
	reportServerReconnected();
	await flush();
	expect(banners()).toHaveLength(1);
	reportCallStartFailure(new TypeError('Importing a module script failed.'), 'fresh');
	await flush();
	expect(banners()).toEqual([expect.objectContaining({ message: CALL_LOAD_FAILED_MESSAGE, session_id: 'fresh' })]);
});

describe('unsaved input anywhere on the page', () => {
	it('a settings field typed into blocks the reload on resume and on hide', async () => {
		document.body.innerHTML = '<form><label>Name <input name="agent-name" value="Ada"></label></form>';
		const field = document.querySelector('input')!;
		expect(unsavedFieldReason()).toBeNull();
		field.value = 'Ada Lovelace';
		expect(reloadBlockers()).toContain('unsaved-field');

		setVisibility('hidden');
		setVisibility('visible');
		await flush();
		setVisibility('hidden');
		expect(reload).not.toHaveBeenCalled();
		expect(banners()).toHaveLength(1);

		// Saved (or reset): the next hide applies the update.
		field.value = 'Ada';
		setVisibility('visible');
		setVisibility('hidden');
		expect(reload).toHaveBeenCalledTimes(1);
	});

	it('counts a password field, a textarea and a field the app filled in; not buttons or toggles', () => {
		document.body.innerHTML = '<input type="checkbox"><input type="submit" value="Go"><input type="password">';
		document.querySelector<HTMLInputElement>('input[type=checkbox]')!.checked = true;
		expect(unsavedFieldReason()).toBeNull();
		document.querySelector<HTMLInputElement>('input[type=password]')!.value = 'sk-test';
		expect(unsavedFieldReason()).toBe('unsaved-field');

		document.body.innerHTML = '<textarea></textarea>';
		document.querySelector('textarea')!.value = 'Approve, but skip the migration';
		expect(unsavedFieldReason()).toBe('unsaved-field');

		document.body.innerHTML = '<input disabled>';
		document.querySelector('input')!.value = 'not editable';
		expect(unsavedFieldReason()).toBeNull();
	});

	it('the focused or last focused rich-text editor with text blocks; an empty one does not', () => {
		document.body.innerHTML = '<div contenteditable="true" tabindex="0"></div><button>x</button>';
		const editor = document.querySelector<HTMLElement>('[contenteditable]')!;
		// jsdom has no layout engine; make isContentEditable reflect the attribute.
		Object.defineProperty(editor, 'isContentEditable', { configurable: true, get: () => true });
		editor.focus();
		expect(unsavedFieldReason()).toBeNull();
		editor.textContent = 'Answer to the agent question';
		expect(unsavedFieldReason()).toBe('unsaved-editor');
		// Focus moved to a button (the file chooser, a tab switch): still counts.
		document.querySelector('button')!.focus();
		expect(unsavedFieldReason()).toBe('unsaved-editor');
	});
});

describe('the automatic reload cap', () => {
	const KEY = 'ai-shuffle-update-auto-reload';

	it('allows one, then refuses another within the cooldown or from the same build', () => {
		const now = 5_000_000;
		expect(autoReloadAllowed(now)).toBe(true);
		sessionStorage.setItem(KEY, JSON.stringify({ from: 'another-build', at: now - 1_000 }));
		expect(autoReloadAllowed(now)).toBe(false);
		sessionStorage.setItem(KEY, JSON.stringify({ from: 'another-build', at: now - AUTO_RELOAD_COOLDOWN_MS }));
		expect(autoReloadAllowed(now)).toBe(true);
		// This tab already left the build it runs now and came back on it.
		sessionStorage.setItem(KEY, JSON.stringify({ from: 'test', at: now - 10 * AUTO_RELOAD_COOLDOWN_MS }));
		expect(autoReloadAllowed(now)).toBe(false);
	});

	// Replicas behind one address answering A, B, A…: one reload, then the banner.
	it('a page flapping between builds reloads once per cooldown', async () => {
		sessionStorage.setItem(KEY, JSON.stringify({ from: 'another-build', at: Date.now() - 60_000 }));
		setVisibility('hidden');
		setVisibility('visible');
		await flush();
		expect(reload).not.toHaveBeenCalled();
		expect(banners()).toHaveLength(1);
	});
});
