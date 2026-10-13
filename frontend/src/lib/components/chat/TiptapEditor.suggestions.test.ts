import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import TiptapEditor from './TiptapEditor.svelte';

afterEach(cleanup);

// jsdom lacks PointerEvent; supply its touch-specific fields explicitly.
function tap(field: HTMLElement, time: number, x = 10) {
	const event = new Event('pointerup', { bubbles: true, cancelable: true });
	Object.defineProperties(event, {
		pointerType: { value: 'touch' }, isPrimary: { value: true },
		timeStamp: { value: time }, clientX: { value: x }, clientY: { value: 10 }
	});
	return fireEvent(field, event);
}

describe('inline reply suggestion', () => {
	it('single click only focuses; double click accepts without sending', async () => {
		const accept = vi.fn();
		const { component, getByRole, getByText } = render(TiptapEditor, {
			props: { suggestion: 'Check the logs', onAcceptSuggestion: accept }
		});
		await tick();
		const field = getByRole('textbox', { name: 'Message' });
		expect(getByText('Check the logs').tagName).toBe('SPAN');
		await fireEvent.click(field);
		expect(accept).not.toHaveBeenCalled();
		expect(component.getMessage()).toBe('');
		await fireEvent.doubleClick(field);
		expect(accept).toHaveBeenCalledOnce();
		expect(component.getMessage()).toBe('');
	});

	it('accepts two nearby touch taps, not a single tap or slow/distant taps', async () => {
		const accept = vi.fn();
		const { getByRole } = render(TiptapEditor, {
			props: { suggestion: 'Check the logs', onAcceptSuggestion: accept }
		});
		await tick();
		const field = getByRole('textbox', { name: 'Message' });
		await tap(field, 100);
		expect(accept).not.toHaveBeenCalled();
		await tap(field, 600);
		await tap(field, 700, 100);
		expect(accept).not.toHaveBeenCalled();
		await tap(field, 800, 102);
		expect(accept).toHaveBeenCalledOnce();
	});

	it.each([{ initialContent: 'My draft' }, { disabled: true }, { suggestion: '' }])(
		'does not accept when unavailable: %j', async extra => {
			const accept = vi.fn();
			const { getByRole } = render(TiptapEditor, {
				props: { suggestion: 'Old suggestion', onAcceptSuggestion: accept, ...extra }
			});
			await tick();
			const field = getByRole('textbox', { name: 'Message' });
			await fireEvent.doubleClick(field);
			await tap(field, 100);
			await tap(field, 200);
			expect(accept).not.toHaveBeenCalled();
		}
	);
});

describe('composer accessibility', () => {
	it('exposes the editor as a named multi-line text field', async () => {
		const { getByRole } = render(TiptapEditor);
		await tick();
		const field = getByRole('textbox', { name: 'Message' });
		expect(field.getAttribute('aria-multiline')).toBe('true');
		expect(field.getAttribute('contenteditable')).toBe('true');
	});
});
