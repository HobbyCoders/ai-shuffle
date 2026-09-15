import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import FileAutocomplete from './FileAutocomplete.svelte';

const { getNotesAutocomplete } = vi.hoisted(() => ({ getNotesAutocomplete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ getNotesAutocomplete, getSubagentsForAutocomplete: vi.fn() }));
vi.mock('$lib/api/git', () => ({ getWorktreeFiles: vi.fn().mockResolvedValue({ files: [] }) }));

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
const note = (title: string) => ({ id: 'note-test', title, preview: 'truncated preview', tags: [], file_path: 'notes/test.md' });
const props = { visible: true, projectId: 'project', onSelect: vi.fn(), onClose: vi.fn() };

it('keeps the Notes virtual folder until the user enters it', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    render(FileAutocomplete, { ...props, inputValue: '@Notes' });
    await waitFor(() => expect(screen.getByRole('option', { name: /Notes/ })).toBeTruthy());
    expect(getNotesAutocomplete).not.toHaveBeenCalled();
});


it.each(['ALPHANEEDLE', '%', '_', 'lateNEEDLE'])('sends the literal note suffix %s and keeps content-only matches', async (search) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    getNotesAutocomplete.mockResolvedValue([note('Content match')]);
    render(FileAutocomplete, { ...props, inputValue: `@Notes/${search}` });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalledWith(search, 30));
    await waitFor(() => expect(screen.getByText('Content match')).toBeTruthy());
});

it('searches again as the note suffix changes and ignores a late response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    let finish!: (notes: ReturnType<typeof note>[]) => void;
    getNotesAutocomplete.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
        .mockResolvedValueOnce([note('Literal 50%')]);
    const { rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/old' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalledWith('old', 30));
    await rerender({ inputValue: '@Notes/%' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalledWith('%', 30));
    await waitFor(() => expect(screen.getByText((_, element) => element?.classList.contains('autocomplete-item-name') === true && element.textContent === 'Literal 50%')).toBeTruthy());
    finish([note('Stale result')]);
    await Promise.resolve(); await Promise.resolve();
    expect(screen.queryByText('Stale result')).toBeNull();
});

it('clears notes when the next request loses authorization', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    getNotesAutocomplete.mockResolvedValueOnce([note('Allowed note')]).mockRejectedValueOnce(new Error('Not authenticated'));
    const { rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/allowed' });
    await waitFor(() => expect(screen.getByText((_, element) => element?.classList.contains('autocomplete-item-name') === true && element.textContent === 'Allowed note')).toBeTruthy());
    await rerender({ inputValue: '@Notes/forbidden' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalledWith('forbidden', 30));
    await waitFor(() => expect(screen.getByText('No matching items')).toBeTruthy());
    await waitFor(() => expect(screen.queryByText((_, element) => element?.classList.contains('autocomplete-item-name') === true && element.textContent === 'Allowed note')).toBeNull());
});
