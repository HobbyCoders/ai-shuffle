import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { page } from '$app/state';
import NotesCard from './NotesCard.svelte';
import { shellHistory, type ShellState } from '$lib/services/app-history.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { getNote, getNoteAttachments, getNoteTags, getNotes } from '$lib/api/client';

vi.mock('$lib/api/client', () => ({
	getNotes: vi.fn(), getNote: vi.fn(), createNote: vi.fn(), updateNote: vi.fn(),
	deleteNote: vi.fn(), getNoteTags: vi.fn(), getNoteAttachments: vi.fn(), uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(), noteAttachmentDownloadUrl: vi.fn()
}));
// The browser's entries, written the way SvelteKit's shallow goto writes them.
const router = vi.hoisted(() => ({ entries: [] as ShellState[], index: 0 }));
vi.mock('$app/navigation', () => ({
	goto: async (_url: string, { replace, state }: { replace: boolean; state: ShellState }) => {
		if (replace) router.entries[router.index] = state;
		else router.entries.splice(++router.index, Infinity, state);
		page.state = state;
	}
}));
const go = (delta: number) => {
	router.index += delta;
	page.state = router.entries[router.index];
	shellHistory.land(page.state as ShellState);
};
const note = (id: string) => ({
	id, title: `Note ${id}`, content: `content ${id}`, tags: [] as string[], is_pinned: false, file_path: null,
	created_at: '2026-09-01', updated_at: '2026-09-01'
});
const panels = () => router.entries.map((state) => state.shell?.place.panel);
const dialog = () => screen.queryByRole('dialog');

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	vi.mocked(getNotes).mockResolvedValue([note('A'), note('B')]);
	vi.mocked(getNoteTags).mockResolvedValue([]);
	vi.mocked(getNoteAttachments).mockResolvedValue([]);
	vi.mocked(getNote).mockImplementation(async (id) => note(id));
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.spyOn(history, 'state', 'get').mockImplementation(() => ({ 'sveltekit:metadata': { historyIndex: router.index } }));
	vi.spyOn(history, 'go').mockImplementation((delta) => go(delta ?? 0));
	router.entries = [{}];
	router.index = 0;
	page.state = {};
	shellHistory.start({}, { type: 'notes' });
});
afterEach(() => {
	cleanup();
	shellHistory.stop();
	resourceSync.clear();
	sessionStorage.clear();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

it('Cancel on the discard asked by Back keeps the draft and returns the browser to its entry, so the next Back reaches the note again', async () => {
	render(NotesCard);
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
	await waitFor(() => expect(panels()).toEqual([null, 'A']));
	await fireEvent.click(screen.getAllByRole('button', { name: 'New note' })[0]);
	await waitFor(() => expect(panels()).toEqual([null, 'A', 'new']));
	await fireEvent.input(screen.getByPlaceholderText(/Write your note/), { target: { value: 'keep me' } });

	go(-1);
	await waitFor(() => expect(dialog()).toBeTruthy());
	await fireEvent.click(within(dialog()!).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
	await waitFor(() => expect(router.index).toBe(2));
	expect(dialog()).toBeNull();
	expect((screen.getByPlaceholderText(/Write your note/) as HTMLTextAreaElement).value).toBe('keep me');
	expect(panels()).toEqual([null, 'A', 'new']);

	// Back again asks again, from the draft's own entry: note A is still one step back.
	go(-1);
	await waitFor(() => expect(dialog()).toBeTruthy());
	expect(router.index).toBe(1);
});
