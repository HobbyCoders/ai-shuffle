import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import NotesCard from './NotesCard.svelte';
import NotesViewHarness from './test-fixtures/NotesViewHarness.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { viewPanel } from '$lib/stores/view-panel.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { getNote, getNotes, updateNote } from '$lib/api/client';

vi.mock('$lib/api/client', () => ({
	getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
	deleteNote: vi.fn(), getNoteTags: vi.fn().mockResolvedValue([]),
	getNoteAttachments: vi.fn().mockResolvedValue([]), uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));
const note = { id: 'A', title: 'Note A', content: 'content A', tags: [], is_pinned: false, file_path: null, created_at: '2026-09-01', updated_at: '2026-09-01' };
beforeEach(() => {
	vi.clearAllMocks(); sessionStorage.clear();
	layout.setSidebarOverlay(false); platform.set('desktop');
	vi.mocked(getNotes).mockResolvedValue([note]);
	vi.mocked(getNote).mockResolvedValue(note);
	vi.mocked(updateNote).mockImplementation(async (_id, data) => ({ ...note, ...data }));
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); resourceSync.clear(); platform.set('desktop'); sessionStorage.clear(); vi.unstubAllGlobals(); });

it('docks only its list and retains a selected dirty buffer when the rail destination changes', async () => {
	const view = render(NotesViewHarness);
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
		await fireEvent.click(screen.getByRole('tab', { name: 'Edit' }));
	const body = screen.getByRole('textbox', { name: 'Note body' }) as HTMLTextAreaElement;
	await fireEvent.input(body, { target: { value: 'Unsaved across rail navigation' } });
	const host = view.container.querySelector('[data-test-host]')!;
	expect(host.contains(screen.getByRole('searchbox'))).toBe(true);
	expect(host.contains(body)).toBe(false);
	expect(viewPanel.panel).not.toBeNull();
	await view.rerender({ active: false });
	expect(viewPanel.panel).toBeNull();
	await view.rerender({ active: true });
	expect(viewPanel.panel).not.toBeNull();
	expect(screen.getByRole('textbox', { name: 'Note body' })).toBe(body);
	expect(body.value).toBe('Unsaved across rail navigation');
	expect(screen.getByRole('button', { name: /Note A content A/ }).getAttribute('aria-current')).toBe('true');
});

it('opens an initialItemId even when it is absent from the list', async () => {
	vi.mocked(getNotes).mockResolvedValue([]);
	render(NotesCard, { initialItemId: 'linked' });
	await fireEvent.click(await screen.findByRole('tab', { name: 'Edit' }));
	await waitFor(() => expect((screen.getByRole('textbox', { name: 'Note body' }) as HTMLTextAreaElement).value).toBe('content A'));
	expect(getNote).toHaveBeenCalledWith('linked');
});

it('starts the phone on its list, then opens a note and exposes the shared Back control', async () => {
	platform.set('mobile');
	const view = render(NotesCard, { mobile: true });
	await screen.findByRole('button', { name: /Note A content A/ });
	await tick();
	expect(view.container.querySelector('[data-view-shell="Notes"]')?.getAttribute('data-pane')).toBe('panel');
	expect(screen.queryByRole('textbox', { name: 'Note body' })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: /Note A content A/ }));
	expect(view.container.querySelector('[data-view-shell="Notes"]')?.getAttribute('data-pane')).toBe('detail');
	expect(screen.getByRole('button', { name: 'Back to Notes' })).toBeTruthy();
});

it('shows a retryable loading failure and the shared empty state after recovery', async () => {
	vi.mocked(getNotes).mockRejectedValueOnce(new Error('Notes unavailable')).mockResolvedValue([]);
	render(NotesCard);
	expect(screen.getByText('Loading notes…')).toBeTruthy();
	await screen.findByText('Notes unavailable');
	await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
	await screen.findByText('No notes yet');
	expect(screen.getByRole('button', { name: 'Create your first note' })).toBeTruthy();
});
