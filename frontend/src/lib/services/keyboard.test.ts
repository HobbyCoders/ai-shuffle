/**
 * Regression test for keyboard.ts — synthetic keydown events (e.g. dispatched
 * by browser autofill/password managers on page load) can arrive with
 * `e.key === undefined`. The global keydown handler must not throw when
 * building the shortcut-matching key string from such an event.
 *
 * Prod error log 2026-07-13 18:06: Uncaught TypeError: Cannot read properties
 * of undefined (reading 'toLowerCase').
 */

import { describe, it, expect, afterEach } from 'vitest';
import { registerShortcut, clearAllShortcuts, NOT_HANDLED } from './keyboard';

afterEach(() => {
	clearAllShortcuts();
});

describe('keyboard shortcut handler', () => {
	it('does not throw on a keydown event with an undefined key', () => {
		const registration = registerShortcut({
			id: 'test-shortcut',
			description: 'Test shortcut',
			key: 'k',
			cmdOrCtrl: true,
			category: 'general',
			action: () => {}
		});

		// Simulate a synthetic keydown event missing `key`, as dispatched by
		// some autofill/password-manager integrations.
		const event = new KeyboardEvent('keydown', {});
		Object.defineProperty(event, 'key', { value: undefined });

		expect(() => window.dispatchEvent(event)).not.toThrow();

		registration.unregister();
	});

	it.each([
		['prevents the default of a handled shortcut', () => {}, true],
		['prevents the default when the action returns false (only NOT_HANDLED opts out)', () => false, true],
		['leaves the event alone when the action returns NOT_HANDLED', () => NOT_HANDLED, false],
	])('%s', (_label, action, prevented) => {
		registerShortcut({ id: 'esc', description: 'Esc', key: 'Escape', category: 'general', action });
		const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });
		window.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(prevented);
	});
});
