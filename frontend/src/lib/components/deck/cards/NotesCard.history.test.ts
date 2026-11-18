import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { page } from '$app/state';
import NotesCard from './NotesCard.svelte';
import { shellHistory, type ShellState } from '$lib/services/app-history.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { getNote, getNoteAttachments, getNoteTags, getNotes, updateNote } from '$lib/api/client';
import { platform } from '$lib/stores/platform.svelte';

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
	platform.set('desktop');
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

it('a second Back while the discard question is open: Cancel still returns to the draft’s own entry (S13)', async () => {
	render(NotesCard);
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
	await waitFor(() => expect(panels()).toEqual([null, 'A']));
	await fireEvent.click(await screen.findByRole('button', { name: /Note B content B/ }));
	await waitFor(() => expect(panels()).toEqual([null, 'A', 'B']));
	await fireEvent.click(screen.getAllByRole('button', { name: 'New note' })[0]);
	await waitFor(() => expect(panels()).toEqual([null, 'A', 'B', 'new']));
	await fireEvent.input(screen.getByPlaceholderText(/Write your note/), { target: { value: 'keep me' } });

	go(-1); // to B: asks
	await waitFor(() => expect(dialog()).toBeTruthy());
	go(-1); // to A before answering
	await waitFor(() => expect(router.index).toBe(1));
	await new Promise((resolve) => setTimeout(resolve));
	await fireEvent.click(within(dialog()!).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
	await waitFor(() => expect(router.index).toBe(3));
	expect((screen.getByPlaceholderText(/Write your note/) as HTMLTextAreaElement).value).toBe('keep me');
	expect(panels()).toEqual([null, 'A', 'B', 'new']);
});

it('Cancel returns to the draft’s entry even when the step it answers is still waiting for its note (S19)', async () => {
	vi.mocked(getNotes).mockImplementation(async (params) => (params?.search ? [note('B')] : [note('A'), note('B')]));
	render(NotesCard);
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
	await fireEvent.click(await screen.findByRole('button', { name: /Note B content B/ }));
	await fireEvent.click(screen.getAllByRole('button', { name: 'New note' })[0]);
	await waitFor(() => expect(panels()).toEqual([null, 'A', 'B', 'new']));
	await fireEvent.input(screen.getByPlaceholderText(/Write your note/), { target: { value: 'keep me' } });
	// Filter the list so B is listed and A is not.
	await fireEvent.input(screen.getByPlaceholderText('Search notes'), { target: { value: 'B' } });
	await waitFor(() => expect(screen.queryByRole('button', { name: /Note A content A/ })).toBeNull(), { timeout: 2000 });

	go(-1); // to B: asks
	await waitFor(() => expect(dialog()).toBeTruthy());
	go(-1); // to A, not in the filtered list: that step waits
	await new Promise((resolve) => setTimeout(resolve));
	await fireEvent.click(within(dialog()!).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
	await waitFor(() => expect(router.index).toBe(3));
	expect((screen.getByPlaceholderText(/Write your note/) as HTMLTextAreaElement).value).toBe('keep me');
	expect(panels()).toEqual([null, 'A', 'B', 'new']);
});

it('two Backs from a named note whose save then fails: Cancel returns to that note’s entry, not the first step (S19)', async () => {
	vi.mocked(getNotes).mockResolvedValue([note('A'), note('B'), note('C')]);
	let failSave!: (error: Error) => void;
	vi.mocked(updateNote).mockImplementation(() => new Promise((_, reject) => (failSave = reject)));
	render(NotesCard);
	for (const id of ['A', 'B', 'C']) await fireEvent.click(await screen.findByRole('button', { name: new RegExp(`Note ${id} content ${id}`) }));
	await waitFor(() => expect(panels()).toEqual([null, 'A', 'B', 'C']));
	await fireEvent.input(screen.getByRole('textbox', { name: 'Note title' }), { target: { value: 'edited C' } });

	go(-1); // to B: C's save starts and is held
	await waitFor(() => expect(updateNote).toHaveBeenCalledOnce());
	go(-1); // to A, before any answer
	await new Promise((resolve) => setTimeout(resolve));
	failSave(new Error('save failed'));
	await waitFor(() => expect(dialog()).toBeTruthy());
	await fireEvent.click(within(dialog()!).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
	await waitFor(() => expect(router.index).toBe(3));
	expect((screen.getByRole('textbox', { name: 'Note title' }) as HTMLTextAreaElement).value).toBe('edited C');
	expect(panels()).toEqual([null, 'A', 'B', 'C']);
});

it('a saved note past the list’s first page opens from its own request; a 404 clears it, a failure keeps it (S23)', async () => {
	const firstPage = [note('A'), ...Array.from({ length: 99 }, (_, i) => note(`other-${i}`))];
	vi.mocked(getNotes).mockResolvedValue(firstPage);
	router.entries = [{}, { shell: { id: 'old', place: { type: 'notes', panel: 'old' } } }, { shell: { id: 'gone', place: { type: 'notes', panel: 'gone' } } }];
	router.index = 1;
	shellHistory.stop();
	page.state = router.entries[1];
	shellHistory.start(router.entries[1] as ShellState, { type: 'notes' });
	let fail!: (error: unknown) => void;
	vi.mocked(getNote).mockImplementation((id) => id === 'old' && !fail
		? new Promise((_, reject) => (fail = reject))
		: id === 'gone' ? Promise.reject({ status: 404, detail: 'Note not found' }) : Promise.resolve(note(id)));
	render(NotesCard);
	await waitFor(() => expect(getNote).toHaveBeenCalledWith('old'));
	fail({ status: 503, detail: 'unavailable' }); // temporary: the selection stays
	await new Promise((resolve) => setTimeout(resolve));
	expect(router.entries[1].shell?.place.panel).toBe('old');
	// The list reloads (focus): the note is asked for again and opens.
	window.dispatchEvent(new Event('focus'));
	expect(await screen.findByDisplayValue('Note old')).toBeTruthy();
	expect(panels()).toEqual([undefined, 'old', 'gone']);

	go(1); // the deleted note: the server confirms it is gone
	await waitFor(() => expect(router.entries[2].shell?.place.panel).toBeNull());
	expect(screen.queryByDisplayValue('Note gone')).toBeNull();
});

it('phone app: choosing the open note again while its Back waits for a save is a no-op, as on main (S22)', async () => {
	shellHistory.stop();
	platform.set('mobile');
	let finishSave!: () => void;
	vi.mocked(updateNote).mockImplementation((id, data) => new Promise((resolve) => (finishSave = () => resolve({ ...note(id), ...data }))));
	const { rerender } = render(NotesCard, { initialItemId: null });
	await fireEvent.click(await screen.findByRole('button', { name: /Note A content A/ }));
	await fireEvent.input(await screen.findByRole('textbox', { name: 'Note title' }), { target: { value: 'edited A' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Back to Notes' }));
	await waitFor(() => expect(updateNote).toHaveBeenCalledOnce());
	await rerender({ initialItemId: 'A' }); // Spotlight reopens the same note
	await waitFor(() => expect(getNote).toHaveBeenCalledWith('A'));
	await new Promise((resolve) => setTimeout(resolve));
	finishSave();
	await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Note title' })).toBeNull());
});

it('phone app: an older Notes load that answers while a newer one waits still shows, as on main (S17)', async () => {
	shellHistory.stop();
	platform.set('mobile');
	const loads: { resolve: (rows: ReturnType<typeof note>[]) => void; reject: (error: Error) => void }[] = [];
	vi.mocked(getNotes).mockImplementation(() => new Promise((resolve, reject) => loads.push({ resolve, reject })));
	render(NotesCard);
	await waitFor(() => expect(loads).toHaveLength(1));
	await fireEvent.input(screen.getByPlaceholderText('Search notes'), { target: { value: 'A' } });
	await waitFor(() => expect(loads).toHaveLength(2), { timeout: 2000 });
	loads[0].resolve([note('A'), note('B')]);
	expect(await screen.findByRole('button', { name: /Note A content A/ })).toBeTruthy();
	// The newer load then fails: main's error banner with its Retry, as before.
	loads[1].reject(new Error('503'));
	expect(await screen.findByText('503')).toBeTruthy();
});

