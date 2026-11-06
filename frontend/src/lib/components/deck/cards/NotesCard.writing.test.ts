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
 vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
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
 expect(within(row).getByText('Ready for review · Planning')).toBeTruthy();
 await fireEvent.click(screen.getAllByRole('button', { name: 'New note' })[0]);
 expect(screen.getByRole('tab', { name: 'Edit' }).getAttribute('aria-selected')).toBe('true');
 expect(screen.getByRole('textbox', { name: 'Note body' })).toBeTruthy();
});
it.each(['# Release plan\n\nThe release body.', 'Release plan\n===\n\nThe release body.'])('separates a leading heading from the list preview: %s', async (content) => {
 vi.mocked(getNotes).mockResolvedValue([{ ...note, content, is_pinned: true }]);
 render(NotesCard);
 const row = (await screen.findByText(note.title)).closest('button')!;
 expect(within(row).getByText('The release body. · Planning')).toBeTruthy();
 expect(row.querySelector('svg.lucide-pin')).toBeTruthy();
 expect(row.querySelector('.st-tile')).toBeNull();
});
it('keeps a heading-only document useful in the list', async () => {
 vi.mocked(getNotes).mockResolvedValue([{ ...note, content: '# Heading only' }]);
 render(NotesCard);
 const row = (await screen.findByText(note.title)).closest('button')!;
 expect(within(row).getByText('Heading only · Planning')).toBeTruthy();
});
it('wraps the title while keeping pasted lines and Enter as one logical title', async () => {
 render(NotesCard);
 await fireEvent.click(await screen.findByText(note.title));
 const title = screen.getByRole('textbox', { name: 'Note title' }) as HTMLTextAreaElement;
 expect(title.tagName).toBe('TEXTAREA');
 await fireEvent.input(title, { target: { value: 'Release plan\nfor every\r\nworkspace' } });
 expect(title.value).toBe('Release plan for every workspace');
 await fireEvent.keyDown(title, { key: 'Enter', isComposing: true });
 expect(screen.getByRole('tab', { name: 'Preview' }).getAttribute('aria-selected')).toBe('true');
 await fireEvent.keyDown(title, { key: 'Enter' });
 await Promise.resolve();
 expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note body' }));
 expect(title.value).toBe('Release plan for every workspace');
});
it('uses read-only GFM tasks, semantic indented lists and nested markdown in Preview', async () => {
 vi.mocked(getNote).mockResolvedValue({ ...note, content: '- [x] **Ready** for [review](https://example.com)\n- [ ] An unfinished item\n  - Nested bullet\n\n1. Ordered item' });
 const view = render(NotesCard);
 await fireEvent.click(await screen.findByText(note.title));
 const preview = view.container.querySelector('.note-preview')!;
 const boxes = preview.querySelectorAll<HTMLInputElement>('li > input[type=checkbox]');
 expect(boxes).toHaveLength(2);
 expect([...boxes].map(box => [box.checked, box.disabled])).toEqual([[true, true], [false, true]]);
 expect(preview.textContent).not.toMatch(/\[[ x]\]/);
 expect(preview.querySelector('ul ul li')?.textContent).toContain('Nested bullet');
 expect(preview.querySelector('ol li')?.textContent).toBe('Ordered item');
 expect(preview.querySelector('strong')?.textContent).toBe('Ready');
 expect(preview.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
});
it('keeps phone metadata and save status together inside the collapsed details', async () => {
 vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
 render(NotesCard);
 await fireEvent.click(await screen.findByText(note.title));
 const details = screen.getByText(/^Tags & files/).closest('details')!;
 expect(details.open).toBe(false);
 expect(document.querySelector('.st-dh.compact-header')).toBeTruthy();
 expect(document.querySelector('.st-dhs')).toBeNull();
 expect(details.textContent).toContain('Global · Updated');
 expect(details.textContent).toContain('Saved');
 expect(screen.getByRole('region', { name: 'Note content' })).toBeTruthy();
});
