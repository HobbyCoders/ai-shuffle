import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearAllShortcuts } from './keyboard';
import { registerSearchShortcut, searchKeyAction, searchKeyWorksInField } from './search-shortcut';
import { settingsNav } from '$lib/settings/navigation.svelte';

describe('searchKeyAction', () => {
	it.each([
		[{ spotlightOpen: true, settingsOnScreen: true }, 'close-spotlight'],
		[{ spotlightOpen: true, settingsOnScreen: false }, 'close-spotlight'],
		[{ spotlightOpen: false, settingsOnScreen: true }, 'settings-search'],
		[{ spotlightOpen: false, settingsOnScreen: false }, 'open-spotlight']
	] as const)('%j -> %s', (state, action) => {
		expect(searchKeyAction(state)).toBe(action);
	});

	it('only acts from a text field when it stays on the page', () => {
		expect(searchKeyWorksInField({ spotlightOpen: false, settingsOnScreen: false })).toBe(false);
		expect(searchKeyWorksInField({ spotlightOpen: false, settingsOnScreen: true })).toBe(true);
		expect(searchKeyWorksInField({ spotlightOpen: true, settingsOnScreen: false })).toBe(true);
	});
});

describe('registerSearchShortcut', () => {
	let open = false;
	const setOpen = vi.fn((next: boolean) => {
		open = next;
	});
	let release: (() => void) | null = null;

	beforeEach(() => {
		open = false;
		setOpen.mockClear();
		settingsNav.reset();
		registerSearchShortcut({ isSpotlightOpen: () => open, setSpotlightOpen: setOpen });
	});
	afterEach(() => {
		release?.();
		release = null;
		clearAllShortcuts();
		document.body.innerHTML = '';
	});

	function press(target: EventTarget = window) {
		const event = new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true });
		target.dispatchEvent(event);
		return event;
	}

	it('opens Spotlight, then closes it', () => {
		expect(press().defaultPrevented).toBe(true);
		expect(open).toBe(true);
		press();
		expect(open).toBe(false);
	});

	it('focuses Settings search instead while Settings is on screen, whatever registered first', () => {
		release = settingsNav.claimSearchShortcut();
		const before = settingsNav.searchFocusRequest;
		press();
		expect(open).toBe(false);
		expect(settingsNav.searchFocusRequest).toBe(before + 1);
		release();
		release = null;
		press();
		expect(open).toBe(true);
	});

	it('works from a Settings field, but not from other fields', () => {
		const field = document.body.appendChild(document.createElement('input'));
		const outside = press(field);
		expect(outside.defaultPrevented).toBe(false);
		expect(setOpen).not.toHaveBeenCalled();

		release = settingsNav.claimSearchShortcut();
		const before = settingsNav.searchFocusRequest;
		expect(press(field).defaultPrevented).toBe(true);
		expect(settingsNav.searchFocusRequest).toBe(before + 1);
	});

	it("closes Spotlight from Spotlight's own search box", () => {
		open = true;
		const field = document.body.appendChild(document.createElement('input'));
		press(field);
		expect(setOpen).toHaveBeenCalledWith(false);
	});

	it('releasing a claim twice counts once', () => {
		const a = settingsNav.claimSearchShortcut();
		const b = settingsNav.claimSearchShortcut();
		a();
		a();
		expect(settingsNav.searchClaimed).toBe(true);
		b();
		expect(settingsNav.searchClaimed).toBe(false);
	});
});
