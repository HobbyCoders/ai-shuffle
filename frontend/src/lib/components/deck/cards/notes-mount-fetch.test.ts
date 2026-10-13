/**
 * Regression test for R14: NotesCard fetched the list TWICE on every open —
 * once from onMount, and again 300 ms later from the search/filter `$effect`'s
 * unavoidable first run — showing a spinner both times.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	getNotes: vi.fn(async () => []),
	getNoteTags: vi.fn(async () => [])
}));

vi.mock('$lib/api/client', () => ({
	getNotes: mocks.getNotes,
	getNoteTags: mocks.getNoteTags,
	getNote: vi.fn(async () => ({})),
	createNote: vi.fn(),
	updateNote: vi.fn(),
	deleteNote: vi.fn(),
	getNoteAttachments: vi.fn(async () => []),
	uploadNoteAttachment: vi.fn(),
	deleteNoteAttachment: vi.fn(),
	noteAttachmentDownloadUrl: vi.fn(() => '')
}));

import NotesCard from './NotesCard.svelte';

beforeEach(() => {
	mocks.getNotes.mockClear();
	mocks.getNoteTags.mockClear();
});

afterEach(() => cleanup());

describe('NotesCard mount', () => {
	it('fetches the note list exactly once, even after the filter debounce window', async () => {
		vi.useFakeTimers();
		try {
			render(NotesCard);
			// Let onMount + the first effect flush.
			await vi.advanceTimersByTimeAsync(0);
			expect(mocks.getNotes).toHaveBeenCalledTimes(1);

			// The filter effect's debounce used to fire a second identical load.
			await vi.advanceTimersByTimeAsync(1_000);
			expect(mocks.getNotes).toHaveBeenCalledTimes(1);
			expect(mocks.getNoteTags).toHaveBeenCalledTimes(1);
		} finally {
			vi.useRealTimers();
		}
	});
});
