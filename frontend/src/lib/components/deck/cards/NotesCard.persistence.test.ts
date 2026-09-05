import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import NotesCard from './NotesCard.svelte';
import { createNote, getNote, getNotes, updateNote } from '$lib/api/client';

vi.mock('$lib/api/client', () => ({
	getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
	deleteNote: vi.fn(), getNoteTags: vi.fn().mockResolvedValue([]),
	getNoteAttachments: vi.fn().mockResolvedValue([]), uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));

const note = (id: string) => ({ id, title: `Note ${id}`, content: `content ${id}`, tags: [], is_pinned: false, file_path: null, created_at: '2026-09-01', updated_at: '2026-09-01' });
const body = () => screen.getByPlaceholderText(/Write your note/) as HTMLTextAreaElement;
const title = () => screen.getByPlaceholderText('Note title…') as HTMLInputElement;
const newNote = () => screen.getAllByRole('button', { name: 'New note' })[0];
const flush = async () => { await Promise.resolve(); await tick(); };

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	vi.mocked(getNotes).mockResolvedValue([note('A'), note('B')]);
	vi.mocked(getNote).mockImplementation(async id => note(id));
	vi.mocked(updateNote).mockImplementation(async (id, data) => ({ ...note(id), ...data }));
	vi.mocked(createNote).mockImplementation(async data => ({ ...note('created'), ...data }));
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); sessionStorage.clear(); vi.unstubAllGlobals(); });

describe('Notes draft persistence', () => {
	it('asks before New note discards an untitled draft; cancel keeps it', async () => {
		render(NotesCard);
		await fireEvent.click(newNote());
		await fireEvent.input(body(), { target: { value: 'keep this draft' } });
		await fireEvent.click(newNote());
		expect(screen.getByRole('dialog')).toBeTruthy();
		expect(body().value).toBe('keep this draft');
		await fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' }).at(-1)!);
		expect(body().value).toBe('keep this draft');
		expect(createNote).not.toHaveBeenCalled();
	});

	it('flushes A before switching to B instead of letting a timer save B', async () => {
		render(NotesCard);
		await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
		await fireEvent.input(body(), { target: { value: 'edited A' } });
		await fireEvent.click(screen.getByRole('button', { name: /Note B content B/ }));
		await waitFor(() => expect(title().value).toBe('Note B'));
		expect(updateNote).toHaveBeenCalledExactlyOnceWith('A', expect.objectContaining({ content: 'edited A' }));
	});

	it('serializes a new-note create and saves edits made while it is in flight', async () => {
		let finish!: (value: ReturnType<typeof note>) => void;
		vi.mocked(createNote).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		render(NotesCard);
		await fireEvent.click(newNote());
		await fireEvent.input(title(), { target: { value: 'Draft' } });
		await fireEvent.input(body(), { target: { value: 'first revision' } });
		// Navigation flushes the same pending create; later edits are serialized.
		await fireEvent.click(newNote());
		expect(createNote).toHaveBeenCalledTimes(1);
		await fireEvent.input(body(), { target: { value: 'latest revision' } });
		finish({ ...note('created'), title: 'Draft', content: 'first revision' });
		await waitFor(() => expect(updateNote).toHaveBeenCalledWith('created', expect.objectContaining({ content: 'latest revision' })));
		expect(createNote).toHaveBeenCalledTimes(1);
	});

	it('does not let a stale refresh overwrite typing or the next note', async () => {
		let finish!: (value: ReturnType<typeof note>) => void;
		vi.mocked(getNote).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
		render(NotesCard);
		await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
		await fireEvent.input(body(), { target: { value: 'local edit' } });
		finish({ ...note('A'), content: 'stale response' });
		await flush();
		expect(body().value).toBe('local edit');
	});

	it('restores a draft for an existing note while the server copy is unchanged', async () => {
		sessionStorage.setItem('ai-shuffle-note-draft', JSON.stringify({
			note: note('A'), isCreatingNew: false, title: 'Note A', content: 'draft for A', tags: [], pinned: false
		}));
		render(NotesCard);
		await waitFor(() => expect(body().value).toBe('draft for A'));
		expect(updateNote).not.toHaveBeenCalled();
	});

	it('drops a draft whose note changed on the server instead of saving it back', async () => {
		sessionStorage.setItem('ai-shuffle-note-draft', JSON.stringify({
			note: note('A'), isCreatingNew: false, title: 'Note A', content: 'stale draft', tags: [], pinned: false
		}));
		vi.mocked(getNote).mockImplementation(async id => ({ ...note(id), content: 'edited elsewhere', updated_at: '2026-09-02' }));
		render(NotesCard);
		await waitFor(() => expect(sessionStorage.getItem('ai-shuffle-note-draft')).toBeNull());
		expect(screen.queryByPlaceholderText(/Write your note/)).toBeNull();
		await fireEvent.click(await screen.findByRole('button', { name: /Note B content B/ }));
		await waitFor(() => expect(title().value).toBe('Note B'));
		expect(updateNote).not.toHaveBeenCalled();
	});

	it('restores an untitled draft after the feature panel is unmounted', async () => {
		const first = render(NotesCard);
		await fireEvent.click(newNote());
		await fireEvent.input(body(), { target: { value: 'survive navigation' } });
		await first.unmount();
		render(NotesCard);
		await waitFor(() => expect(body().value).toBe('survive navigation'));
		expect(createNote).not.toHaveBeenCalled();
	});
});
