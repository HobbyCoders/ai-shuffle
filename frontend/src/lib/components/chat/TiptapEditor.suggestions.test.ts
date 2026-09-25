import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import TiptapEditor from './TiptapEditor.svelte';

afterEach(cleanup);

describe('inline reply suggestion', () => {
	it('accepts the placeholder by tap without submitting or changing the draft itself', async () => {
		const accept = vi.fn();
		const { component, getByRole, queryByText, rerender } = render(TiptapEditor, {
			props: { suggestion: 'Check the logs', onAcceptSuggestion: accept }
		});
		await tick();
		expect(component.getMessage()).toBe('');
		expect(queryByText('Dismiss')).toBeNull();
		const placeholder = getByRole('button', { name: 'Use suggested reply: Check the logs' });
		expect(placeholder.textContent?.trim()).toBe('Check the logs');
		expect(placeholder.getAttribute('type')).toBe('button');
		await fireEvent.click(placeholder);
		expect(accept).toHaveBeenCalledOnce();
		expect(component.getMessage()).toBe('');
		await rerender({ suggestion: '', onAcceptSuggestion: accept });
		expect(queryByText('Check the logs')).toBeNull();
	});

	it('does not accept over an existing draft or while disabled', async () => {
		const accept = vi.fn();
		const { component, queryByRole, rerender } = render(TiptapEditor, {
			props: { suggestion: 'Old suggestion', onAcceptSuggestion: accept, initialContent: 'My draft' }
		});
		await tick();
		expect(queryByRole('button')).toBeNull();
		expect(accept).not.toHaveBeenCalled();
		expect(component.getMessage()).toBe('My draft');
		await rerender({ suggestion: 'Old suggestion', onAcceptSuggestion: accept, disabled: true });
		expect(queryByRole('button')).toBeNull();
	});
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
