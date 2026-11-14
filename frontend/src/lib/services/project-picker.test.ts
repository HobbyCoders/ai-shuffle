/**
 * Cmd/Ctrl+P on the new-session screen: the shortcut opens whichever picker
 * the screen offers, newest first, and declines (leaving the browser its own
 * Cmd/Ctrl+P) when none is on offer or the one on offer cannot open now.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	PROJECT_PICKER_ATTRIBUTE,
	isProjectPickerKey,
	openProjectPicker,
	projectPickerAriaShortcut,
	projectPickerShortcutHint,
	provideProjectPicker
} from './project-picker';

const withdraw: (() => void)[] = [];
afterEach(() => withdraw.splice(0).forEach((fn) => fn()));

describe('project picker opener', () => {
	it('declines with no picker on offer', () => {
		expect(openProjectPicker()).toBe(false);
	});

	it('opens the newest picker on offer, and the previous one once it is withdrawn', () => {
		const older = vi.fn(() => true);
		const newer = vi.fn(() => true);
		withdraw.push(provideProjectPicker(older));
		const withdrawNewer = provideProjectPicker(newer);
		expect(openProjectPicker()).toBe(true);
		expect(newer).toHaveBeenCalledOnce();
		expect(older).not.toHaveBeenCalled();
		withdrawNewer();
		withdrawNewer();
		expect(openProjectPicker()).toBe(true);
		expect(older).toHaveBeenCalledOnce();
	});

	it('passes on a picker that cannot open now', () => {
		withdraw.push(provideProjectPicker(() => false));
		expect(openProjectPicker()).toBe(false);
	});

	it('knows a key typed inside the open picker', () => {
		const picker = document.createElement('div');
		picker.setAttribute(PROJECT_PICKER_ATTRIBUTE, '');
		const input = document.createElement('input');
		picker.append(input);
		const other = document.createElement('input');
		const key = (target: Element) => {
			const event = new KeyboardEvent('keydown', { key: 'p', ctrlKey: true });
			Object.defineProperty(event, 'target', { value: target });
			return event;
		};
		expect(isProjectPickerKey(key(input))).toBe(true);
		expect(isProjectPickerKey(key(other))).toBe(false);
	});

	it('labels the shortcut for this platform (Ctrl+P off a Mac)', () => {
		expect(projectPickerShortcutHint()).toBe('Ctrl+P');
		expect(projectPickerAriaShortcut()).toBe('Control+P');
	});
});
