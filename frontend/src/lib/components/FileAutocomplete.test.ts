import { afterEach, expect, it, vi } from 'vitest';

import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import FileAutocomplete from './FileAutocomplete.svelte';

const { getNotesAutocomplete } = vi.hoisted(() => ({ getNotesAutocomplete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ getNotesAutocomplete, getSubagentsForAutocomplete: vi.fn() }));
vi.mock('$lib/api/git', () => ({ getWorktreeFiles: vi.fn().mockResolvedValue({ files: [] }) }));

afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllGlobals(); });
const note = (title: string) => ({ id: 'note-test', title, preview: 'truncated preview', tags: [], file_path: 'notes/test.md' });
const props = { visible: true, projectId: 'project', onSelect: vi.fn(), onClose: vi.fn() };



it.each(['Enter', 'Tab'])('blocks %s before the live editor query reaches component effects', async (key) => {
    getNotesAutocomplete.mockResolvedValue([note('Result A')]);
    const { component } = render(FileAutocomplete, { ...props, inputValue: '@Notes/alpha' });
    await screen.findByText('Result A');
    const event = new KeyboardEvent('keydown', { key, cancelable: true });
    expect(component.handleKeyDown(event, '@Notes/beta')).toBe(true);
    expect(event.defaultPrevented).toBe(true);
    expect(props.onSelect).not.toHaveBeenCalled();
});

it('discards the pending generation when the mention is removed without hiding first', async () => {
    const pending = deferredNotes();
    getNotesAutocomplete.mockReturnValue(pending.promise);
    const { rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/alpha' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalled());
    await rerender({ inputValue: '' });
    pending.resolve([note('Discarded note')]);
    await waitFor(() => expect(screen.getByText('No matching items')).toBeTruthy());
    expect(screen.queryByText('Discarded note')).toBeNull();
});

function deferredNotes() {
    let resolve!: (notes: ReturnType<typeof note>[]) => void;
    const promise = new Promise<ReturnType<typeof note>[]>(done => { resolve = done; });
    return { promise, resolve };
}

it.each(['Enter', 'Tab'])('consumes %s while the next note query is pending, then selects only its result', async (key) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    const pending = deferredNotes();
    getNotesAutocomplete.mockResolvedValueOnce([note('Result A')]).mockReturnValueOnce(pending.promise);
    const { component, rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/alpha' });
    await screen.findByText('Result A');
    await rerender({ inputValue: '@Notes/beta' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenLastCalledWith('beta', 30));
    const event = new KeyboardEvent('keydown', { key, cancelable: true });
    expect(component.handleKeyDown(event)).toBe(true);
    expect(event.defaultPrevented).toBe(true);
    expect(props.onSelect).not.toHaveBeenCalled();
    pending.resolve([note('Result B')]);
    await screen.findByText('Result B');
    expect(component.handleKeyDown(new KeyboardEvent('keydown', { key, cancelable: true }))).toBe(true);
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ name: 'Result B' }));
});

it.each(['Enter', 'Tab'])('consumes %s on the initial empty Notes query while loading', async (key) => {
    const pending = deferredNotes();
    getNotesAutocomplete.mockReturnValue(pending.promise);
    const { component } = render(FileAutocomplete, { ...props, inputValue: '@Notes/' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalledWith(undefined, 30));
    const event = new KeyboardEvent('keydown', { key, cancelable: true });
    expect(component.handleKeyDown(event)).toBe(true);
    expect(event.defaultPrevented).toBe(true);
    expect(props.onSelect).not.toHaveBeenCalled();
});

it.each(['', '@'])('invalidates an in-flight note response when the query is cleared to %j', async (inputValue) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ files: [] }) }));
    const pending = deferredNotes();
    getNotesAutocomplete.mockReturnValue(pending.promise);
    const { component, rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/alpha' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalled());
    await rerender({ inputValue, visible: false });
    pending.resolve([note('Stale note')]);
    await Promise.resolve(); await Promise.resolve();
    await rerender({ visible: true, inputValue: '@' });
    await screen.findByRole('option', { name: /Notes/ });
    expect(screen.queryByText('Stale note')).toBeNull();
    component.handleKeyDown(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
    expect(props.onSelect).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'note' }));
});

it('invalidates notes on mode change and ignores their late response', async () => {
    const pending = deferredNotes();
    const rootResponse = deferredNotes();
    getNotesAutocomplete.mockResolvedValueOnce([note('Result A')]).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(rootResponse.promise));
    const { component, rerender } = render(FileAutocomplete, { ...props, inputValue: '@Notes/alpha' });
    await screen.findByText('Result A');
    await rerender({ inputValue: '@Notes/beta' });
    await rerender({ inputValue: '@' });
    expect(component.handleKeyDown(new KeyboardEvent('keydown', { key: 'Tab', cancelable: true }))).toBe(true);
    expect(props.onSelect).not.toHaveBeenCalled();
    pending.resolve([note('Late note')]);
    await Promise.resolve(); await Promise.resolve();
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(screen.queryByText('Late note')).toBeNull();
});

it('keeps Escape, parent backspace and arrows available while notes are loading', async () => {
    getNotesAutocomplete.mockReturnValue(deferredNotes().promise);
    const { component } = render(FileAutocomplete, { ...props, inputValue: '@Notes/' });
    await waitFor(() => expect(getNotesAutocomplete).toHaveBeenCalled());
    for (const key of ['ArrowUp', 'ArrowDown', 'Escape', 'Backspace']) {
        const event = new KeyboardEvent('keydown', { key, cancelable: true });
        expect(component.handleKeyDown(event)).toBe(true);
        expect(event.defaultPrevented).toBe(true);
    }
    expect(props.onClose).toHaveBeenCalledOnce();
    expect(props.onSelect).toHaveBeenCalledExactlyOnceWith({ name: '', type: 'directory', path: '' });
});


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
