import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import NoteFileAutocomplete from './NoteFileAutocomplete.svelte';

const attachments = ['spec.pdf', 'a-very-long-file-name-with-design-decisions.md'].map((filename, i) => ({
	id: String(i), note_id: 'note', filename, content_type: null, size_bytes: 1024, created_at: '2026-09-01'
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('filters references, selects by keyboard, and closes with Escape when empty', async () => {
	Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
	const onSelect = vi.fn(), onClose = vi.fn();
	const view = render(NoteFileAutocomplete, { visible: true, query: '', attachments, onSelect, onClose });
	expect(screen.getAllByRole('option')).toHaveLength(2);
	view.component.handleKeyDown(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
	view.component.handleKeyDown(new KeyboardEvent('keydown', { key: 'Enter' }));
	expect(onSelect).toHaveBeenCalledWith(attachments[1]);
	await view.rerender({ query: 'spec' });
	expect(screen.getAllByRole('option')).toHaveLength(1);
	await fireEvent.click(screen.getByRole('button', { name: /spec.pdf/ }));
	expect(onSelect).toHaveBeenLastCalledWith(attachments[0]);
	await view.rerender({ query: 'missing' });
	expect(screen.getByText('No matching attachments')).toBeTruthy();
	expect(view.component.handleKeyDown(new KeyboardEvent('keydown', { key: 'Escape' }))).toBe(true);
	expect(onClose).toHaveBeenCalledOnce();
});

it('does not intercept keys while hidden', () => {
	const view = render(NoteFileAutocomplete, { visible: false, query: '', attachments, onSelect: vi.fn(), onClose: vi.fn() });
	expect(screen.queryByRole('listbox')).toBeNull();
	expect(view.component.handleKeyDown(new KeyboardEvent('keydown', { key: 'Enter' }))).toBe(false);
});
