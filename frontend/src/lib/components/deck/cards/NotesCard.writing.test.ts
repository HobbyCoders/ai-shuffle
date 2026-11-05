import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import NotesCard from './NotesCard.svelte';
import { getNote, getNotes, updateNote } from '$lib/api/client';
import { resourceSync } from '$lib/services/resource-sync';
vi.mock('$lib/api/client', () => ({
 getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
 deleteNote: vi.fn(), getNoteTags: vi.fn().mockResolvedValue([]),
 getNoteAttachments: vi.fn().mockResolvedValue([]), uploadNoteAttachment: vi.fn(),
 deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));
const note = { id: 'writing', title: 'A quieter workspace', content: '# Release plan\n- [x] **Ready** for [review](https://example.com)', tags: ['Planning'], is_pinned: false, file_path: null, created_at: '2026-09-01', updated_at: '2026-09-01' };
beforeEach(() => {
 vi.clearAllMocks(); sessionStorage.clear();
 vi.mocked(getNotes).mockResolvedValue([note]); vi.mocked(getNote).mockResolvedValue(note);
 vi.mocked(updateNote).mockImplementation(async (_id, data) => ({ ...note, ...data }));
 vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); resourceSync.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); });
it('opens content in Preview with the title edited only in the H1 and save status inline', async () => {
 const view = render(NotesCard);
 await fireEvent.click(await screen.findByText(note.title));
 expect(screen.getByRole('tab', { name: 'Preview' }).getAttribute('aria-selected')).toBe('true');
 const title = screen.getByRole('textbox', { name: 'Note title' });
 expect(title.closest('h1')).not.toBeNull();
 expect(screen.getAllByRole('textbox', { name: 'Note title' })).toHaveLength(1);
 expect(view.container.querySelector('.st-dhs')?.contains(screen.getByText('Saved'))).toBe(true);
 expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
 expect(screen.queryByRole('button', { name: 'Delete note' })).toBeNull();
 await fireEvent.click(screen.getByRole('button', { name: 'More note actions' }));
 await fireEvent.click(screen.getByRole('menuitem', { name: 'Delete note' }));
 expect(screen.getByRole('dialog')).toBeTruthy();
});
it('strips markdown from the list excerpt and opens an empty note in Edit', async () => {
 render(NotesCard);
 const row = (await screen.findByText(note.title)).closest('button')!;
 expect(row.textContent).not.toMatch(/# Release|\[x\]|\*\*|https:/);
 expect(within(row).getByText(/Release plan Ready for review/)).toBeTruthy();
 await fireEvent.click(screen.getAllByRole('button', { name: 'New note' })[0]);
 expect(screen.getByRole('tab', { name: 'Edit' }).getAttribute('aria-selected')).toBe('true');
 expect(screen.getByRole('textbox', { name: 'Note body' })).toBeTruthy();
});
it('keeps phone metadata and save status together inside the collapsed details', async () => {
 vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
 render(NotesCard);
 await fireEvent.click(await screen.findByText(note.title));
 const details = screen.getByText(/^Tags & files/).closest('details')!;
 expect(details.open).toBe(false);
 expect(details.textContent).toContain('Global · Updated');
 expect(details.textContent).toContain('Saved');
 expect(screen.getByRole('region', { name: 'Note content' })).toBeTruthy();
});
