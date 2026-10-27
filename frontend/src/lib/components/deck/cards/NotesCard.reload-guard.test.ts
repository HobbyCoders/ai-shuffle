/** #707 review: an update never reloads the page over an unsaved or saving note. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import NotesCard from './NotesCard.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { reloadBlockers } from '$lib/services/app-update';
import { getNote, getNotes } from '$lib/api/client';

vi.mock('$lib/api/client', () => ({
	getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
	deleteNote: vi.fn(), getNoteTags: vi.fn().mockResolvedValue([]),
	getNoteAttachments: vi.fn().mockResolvedValue([]), uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));

const note = { id: 'A', title: 'Note A', content: 'content A', tags: [], is_pinned: false, file_path: null, created_at: '2026-09-01', updated_at: '2026-09-01' };

beforeEach(() => {
	vi.mocked(getNotes).mockResolvedValue([note]);
	vi.mocked(getNote).mockResolvedValue(note);
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); resourceSync.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); });

it('an edited note blocks the reload until the panel goes away', async () => {
	render(NotesCard);
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
	expect(reloadBlockers()).not.toContain('note-unsaved');
	await fireEvent.input(screen.getByPlaceholderText(/Write your note/), { target: { value: 'Unsaved here' } });
	expect(reloadBlockers()).toContain('note-unsaved');
	cleanup();
	expect(reloadBlockers()).not.toContain('note-unsaved');
});
