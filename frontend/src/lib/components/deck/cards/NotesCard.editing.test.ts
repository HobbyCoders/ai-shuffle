import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import NotesCard from './NotesCard.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import {
	createNote, deleteNote, getNote, getNoteAttachments, getNoteTags, getNotes, noteAttachmentDownloadUrl, updateNote
} from '$lib/api/client';

vi.mock('$lib/api/client', () => ({
	getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
	deleteNote: vi.fn(), getNoteTags: vi.fn(), getNoteAttachments: vi.fn(), uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));

const note = (id: string, over: Record<string, unknown> = {}) => ({
	id, title: `Note ${id}`, content: `content ${id}`, tags: [] as string[], is_pinned: false, file_path: null,
	created_at: '2026-09-01', updated_at: '2026-09-01', ...over
});
const body = () => screen.getByPlaceholderText(/Write your note/) as HTMLTextAreaElement;
const title = () => screen.getByPlaceholderText('Note title…') as HTMLInputElement;
const newNote = () => screen.getAllByRole('button', { name: 'New note' })[0];
const dialog = () => screen.queryByRole('dialog');
const confirmIn = (label: string) => within(dialog()!).getAllByRole('button', { name: label }).at(-1)!;

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	vi.mocked(getNotes).mockResolvedValue([note('A', { is_pinned: true, tags: ['ideas'] }), note('B')]);
	vi.mocked(getNoteTags).mockResolvedValue([{ name: 'ideas', count: 1, color: null }]);
	vi.mocked(getNoteAttachments).mockResolvedValue([]);
	vi.mocked(getNote).mockImplementation(async id => (id === 'A' ? note('A', { is_pinned: true, tags: ['ideas'] }) : note(id)));
	vi.mocked(updateNote).mockImplementation(async (id, data) => ({ ...note(id), ...data }));
	vi.mocked(createNote).mockImplementation(async data => ({ ...note('new'), ...data }));
	vi.mocked(deleteNote).mockResolvedValue(undefined as never);
	vi.mocked(noteAttachmentDownloadUrl).mockImplementation((n, a) => `/dl/${n}/${a}`);
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); sessionStorage.clear(); vi.unstubAllGlobals(); });

describe('Notes create, rename and delete', () => {
	it('creates a note with a normalised tag after the autosave delay, then renames it in place', async () => {
		render(NotesCard);
		await screen.findByRole('button', { name: /Note B content B/ });
		await fireEvent.click(newNote());
		expect((screen.getByTitle('Delete note') as HTMLButtonElement).disabled).toBe(true);
		const tag = screen.getByPlaceholderText('add tag…');
		await fireEvent.input(tag, { target: { value: ' Bug  Fix ' } });
		await fireEvent.keyDown(tag, { key: 'Enter' });
		await fireEvent.input(title(), { target: { value: '  Launch plan ' } });
		await fireEvent.input(body(), { target: { value: 'ship it' } });
		expect(screen.getByText('Unsaved')).toBeTruthy();
		expect(createNote).not.toHaveBeenCalled();
		await waitFor(() => expect(createNote).toHaveBeenCalledOnce(), { timeout: 2500 });
		expect(createNote).toHaveBeenCalledWith({ title: 'Launch plan', content: 'ship it', tags: ['bug-fix'], is_pinned: false });
		await waitFor(() => expect(screen.getByText('Saved')).toBeTruthy());
		expect(screen.getByRole('button', { name: /Launch plan ship it/ })).toBeTruthy();

		await fireEvent.input(title(), { target: { value: 'Launch plan v2' } });
		await waitFor(() => expect(updateNote).toHaveBeenCalledWith('new', expect.objectContaining({ title: 'Launch plan v2', tags: ['bug-fix'] })), { timeout: 2500 });
		expect(createNote).toHaveBeenCalledOnce();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove tag' }));
		await waitFor(() => expect(updateNote).toHaveBeenLastCalledWith('new', expect.objectContaining({ tags: [] })), { timeout: 2500 });
	});

	it('deletes the open note only after confirmation and returns to the empty editor', async () => {
		render(NotesCard);
		await fireEvent.click(await screen.findByRole('button', { name: /Note B content B/ }));
		await waitFor(() => expect(title().value).toBe('Note B'));
		await fireEvent.click(screen.getByTitle('Delete note'));
		expect(dialog()!.textContent).toContain('Delete this note? This cannot be undone.');
		await fireEvent.click(confirmIn('Cancel'));
		expect(deleteNote).not.toHaveBeenCalled();
		expect(title().value).toBe('Note B');

		vi.mocked(getNotes).mockResolvedValue([note('A', { is_pinned: true })]);
		await fireEvent.click(screen.getByTitle('Delete note'));
		await fireEvent.click(confirmIn('Delete'));
		await waitFor(() => expect(screen.getByText('Select a note')).toBeTruthy());
		expect(deleteNote).toHaveBeenCalledExactlyOnceWith('B');
		await waitFor(() => expect(screen.queryByRole('button', { name: /Note B content B/ })).toBeNull());
		expect(updateNote).not.toHaveBeenCalled();
	});

	it('closing an untitled draft asks first, and discarding it closes the editor without saving', async () => {
		render(NotesCard);
		await fireEvent.click(newNote());
		await fireEvent.input(body(), { target: { value: 'scratch thought' } });
		await fireEvent.click(screen.getByTitle('Close'));
		expect(dialog()!.textContent).toContain('You have unsaved changes. Discard them?');
		await fireEvent.click(confirmIn('Discard'));
		await waitFor(() => expect(screen.getByText('Select a note')).toBeTruthy());
		expect(createNote).not.toHaveBeenCalled();
		expect(sessionStorage.getItem('ai-shuffle-note-draft')).toBeNull();
	});

	it('refuses to attach a file to a draft that has no title yet', async () => {
		render(NotesCard);
		await fireEvent.click(newNote());
		const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
		const file = new File(['x'], 'a.txt', { type: 'text/plain' });
		Object.defineProperty(picker, 'files', { configurable: true, value: [file] });
		await fireEvent.change(picker);
		expect(await screen.findByText('Add a title before attaching files')).toBeTruthy();
		expect(createNote).not.toHaveBeenCalled();
	});
});

describe('Notes preview and filtering', () => {
	it('renders a sanitised markdown preview with attachment references, and toggles back to the editor', async () => {
		vi.mocked(getNoteAttachments).mockResolvedValue([{ id: 'att1', filename: 'spec.pdf' }] as never);
		vi.mocked(getNote).mockResolvedValue(note('B', {
			content: '# Heading\n\n**bold** <img src=x onerror="alert(1)"> @[spec.pdf](attachment://att1) @[gone.png](attachment://att9)'
		}));
		render(NotesCard);
		await fireEvent.click(await screen.findByRole('button', { name: /Note B content B/ }));
		await waitFor(() => expect(body().value).toContain('# Heading'));
		await screen.findByTitle('Download spec.pdf');
		await fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
		const region = screen.getByRole('region', { name: 'Note content' });
		expect(region.querySelector('h1')?.textContent).toBe('Heading');
		expect(region.querySelector('strong')?.textContent).toBe('bold');
		expect(region.querySelector('[onerror]')).toBeNull();
		const ref = within(region).getByRole('link', { name: '@spec.pdf' });
		expect(ref.getAttribute('href')).toBe('/dl/B/att1');
		expect(region.querySelector('.note-ref--missing')?.textContent).toBe('@gone.png');
		expect(screen.queryByPlaceholderText(/Write your note/)).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
		expect(body().value).toContain('**bold**');
	});

	it('searches and filters by tag through the API after the debounce', async () => {
		render(NotesCard);
		await screen.findByRole('button', { name: /Note B content B/ });
		expect(screen.getByText('Pinned')).toBeTruthy();
		expect(screen.getByText('Recent')).toBeTruthy();
		vi.mocked(getNotes).mockResolvedValue([]);
		await fireEvent.input(screen.getByPlaceholderText('Search notes…'), { target: { value: 'roadmap' } });
		expect(getNotes).toHaveBeenCalledTimes(1);
		await waitFor(() => expect(getNotes).toHaveBeenLastCalledWith({ search: 'roadmap', tag: undefined }));
		expect(await screen.findByText('No notes match')).toBeTruthy();
		vi.mocked(getNotes).mockResolvedValue([note('A', { is_pinned: true, tags: ['ideas'] })]);
		await fireEvent.click(screen.getByRole('button', { name: /^ideas/ }));
		await waitFor(() => expect(getNotes).toHaveBeenLastCalledWith({ search: 'roadmap', tag: 'ideas' }));
		expect(await screen.findByRole('button', { name: /Note A content A/ })).toBeTruthy();
	});
});
