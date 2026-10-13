/**
 * Keyboard shortcut registry — registration/teardown, combo conflicts and the
 * input-focus guard. Events are dispatched on real jsdom elements so the guard
 * sees a real `event.target`.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { registerShortcut, clearAllShortcuts, getShortcuts, type KeyboardShortcut } from './keyboard';

function shortcut(overrides: Partial<KeyboardShortcut> = {}): KeyboardShortcut {
	return { id: 'test', description: 'test', key: 'k', cmdOrCtrl: true, category: 'general', action: vi.fn(), ...overrides };
}

function press(target: EventTarget, init: KeyboardEventInit): KeyboardEvent {
	const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
	target.dispatchEvent(event);
	return event;
}

afterEach(() => {
	clearAllShortcuts();
	document.body.innerHTML = '';
	vi.restoreAllMocks();
});

describe('keyboard registry — conflicts and teardown', () => {
	it('two owners of one combo: the newest fires, and neither teardown deletes the other', () => {
		const spotlight = shortcut({ id: 'spotlight', action: vi.fn() });
		const voice = shortcut({ id: 'global-stt', action: vi.fn() });
		const a = registerShortcut(spotlight);
		const b = registerShortcut(voice);

		press(window, { key: 'k', ctrlKey: true });
		expect(voice.action).toHaveBeenCalledTimes(1);
		expect(spotlight.action).not.toHaveBeenCalled();

		// The OLDER owner unmounts first: the newer binding must survive.
		a.unregister();
		press(window, { key: 'k', ctrlKey: true });
		expect(voice.action).toHaveBeenCalledTimes(2);
		expect(getShortcuts().map((s) => s.id)).toEqual(['global-stt']);

		// Re-register the older owner, then drop the newer: the older takes over.
		const a2 = registerShortcut(spotlight);
		b.unregister();
		b.unregister(); // idempotent — must not remove anyone else
		press(window, { key: 'k', ctrlKey: true });
		expect(spotlight.action).toHaveBeenCalledTimes(1);
		expect(voice.action).toHaveBeenCalledTimes(2);
		a2.unregister();
	});

	it('detaches the window listener once the last shortcut is gone', () => {
		const remove = vi.spyOn(window, 'removeEventListener');
		const first = shortcut({ id: 'one', key: 'n' });
		const second = shortcut({ id: 'two', key: 'm' });
		const r1 = registerShortcut(first);
		const r2 = registerShortcut(second);

		r1.unregister();
		expect(remove).not.toHaveBeenCalledWith('keydown', expect.any(Function));
		r2.unregister();
		expect(remove).toHaveBeenCalledWith('keydown', expect.any(Function));

		press(window, { key: 'm', ctrlKey: true });
		expect(second.action).not.toHaveBeenCalled();
	});
});

describe('keyboard registry — input-focus guard', () => {
	it('ignores typing in inputs, textareas and contenteditable unless allowed; Escape always works', () => {
		const plain = shortcut({ id: 'plain', key: '?', cmdOrCtrl: false });
		const inInput = shortcut({ id: 'stt', key: 'j', allowInInput: true });
		const escape = shortcut({ id: 'close', key: 'Escape', cmdOrCtrl: false });
		registerShortcut(plain);
		registerShortcut(inInput);
		registerShortcut(escape);

		const input = document.createElement('input');
		const textarea = document.createElement('textarea');
		const editable = document.createElement('div');
		// jsdom does not derive isContentEditable from the attribute.
		Object.defineProperty(editable, 'isContentEditable', { value: true });
		document.body.append(input, textarea, editable);

		for (const target of [input, textarea, editable]) {
			const typed = press(target, { key: '?' });
			expect(typed.defaultPrevented).toBe(false); // the character still reaches the field
			press(target, { key: 'j', ctrlKey: true });
			press(target, { key: 'Escape' });
		}
		expect(plain.action).not.toHaveBeenCalled();
		expect(inInput.action).toHaveBeenCalledTimes(3);
		expect(escape.action).toHaveBeenCalledTimes(3);

		// Outside a field the same key fires and suppresses the browser default.
		const fired = press(document.body, { key: '?' });
		expect(plain.action).toHaveBeenCalledTimes(1);
		expect(fired.defaultPrevented).toBe(true);

		// Held keys auto-repeat: never re-fire a toggle.
		press(document.body, { key: '?', repeat: true });
		expect(plain.action).toHaveBeenCalledTimes(1);
	});
});
