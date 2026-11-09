/**
 * Settings > Memory against a mocked memories API: filtering and search, the
 * explicit Save payload, a stale save keeping the draft, confirmed delete, a
 * failed load never reading as "no memories", and memory text staying out of
 * the Settings search index.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: http
}));

import { settingsNav } from '$lib/settings/navigation.svelte';
import { MODULES } from '$lib/settings/registry';
import MemoryCollection from './MemoryCollection.svelte';
import {
	MemoryModel,
	PAGE_SIZE,
	acquireMemoryModel,
	filterMemories,
	formFromMemory,
	memoryMeta,
	releaseMemoryModel,
	validateMemoryForm,
	type MemoryItem
} from './memory-model.svelte';

const memory = (over: Partial<MemoryItem> = {}): MemoryItem => ({
	id: 'm1',
	revision: 3,
	text: 'Shayne prefers short status updates',
	when: 'when reporting progress',
	status: 'active',
	tier: 'user',
	source: 'user',
	scope: 'global',
	scope_label: 'All projects',
	always: false,
	needs_review: false,
	this_worker_only: false,
	created_at: 1_700_000_000,
	updated_at: 1_700_000_000,
	last_recalled_at: null,
	expires_at: null,
	uses: 0,
	...over
});

const rows = [
	memory(),
	memory({ id: 'm2', text: 'Old deploy notes', when: null, status: 'archived', tier: 'agent', scope_label: 'Website' }),
	memory({ id: 'm3', text: 'Two answers disagree here', when: 'when two facts clash', needs_review: true, tier: 'learned' })
];
const limits = { text_chars: 2000, when_chars: 140 };

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('memory');
	if (item) settingsNav.go({ category: 'memory', item });
	const page = render(MemoryCollection);
	await waitFor(() => expect(http.get).toHaveBeenCalled());
	return page;
}

const textBox = () => screen.getByRole('textbox', { name: 'What to remember' }) as HTMLTextAreaElement;
const saveButton = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;

beforeEach(() => {
	vi.clearAllMocks();
	releaseMemoryModel(new MemoryModel());
	http.get.mockResolvedValue({ items: rows, limits });
	http.put.mockImplementation(async (_path: string, body: { text: string; when: string | null }) =>
		memory({ text: body.text, when: body.when, revision: 3 })
	);
	http.delete.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('memory rules', () => {
	const view = { filter: 'active', scope: '', query: '' } as const;

	it('filters by status, review state, place and text', () => {
		expect(filterMemories(rows, view).map((m) => m.id)).toEqual(['m1', 'm3']);
		expect(filterMemories(rows, { ...view, filter: 'archived' }).map((m) => m.id)).toEqual(['m2']);
		expect(filterMemories(rows, { ...view, filter: 'review' }).map((m) => m.id)).toEqual(['m3']);
		expect(filterMemories(rows, { ...view, filter: 'all', scope: 'Website' }).map((m) => m.id)).toEqual(['m2']);
		expect(filterMemories(rows, { ...view, query: 'PROGRESS' }).map((m) => m.id)).toEqual(['m1']);
	});

	it('keeps a just-saved row listed although it no longer matches', () => {
		expect(filterMemories(rows, { ...view, filter: 'archived' }, new Set(['m1'])).map((m) => m.id)).toEqual(['m1', 'm2']);
	});

	it('writes the row line in plain words', () => {
		expect(memoryMeta(rows[1])).toContain('An agent saved');
		expect(memoryMeta(rows[1])).toContain('Archived');
		expect(memoryMeta(rows[2])).toContain('Needs review');
	});

	it('refuses empty text, over-long text and removing a when-line', () => {
		const form = formFromMemory(memory());
		expect(validateMemoryForm(form, limits)).toEqual({});
		expect(validateMemoryForm({ ...form, text: '   ' }, limits).text).toBeTruthy();
		expect(validateMemoryForm({ ...form, text: 'x'.repeat(2001) }, limits).text).toBeTruthy();
		expect(validateMemoryForm({ ...form, when: '' }, limits).when).toBeTruthy();
		expect(validateMemoryForm({ ...formFromMemory(memory({ when: null })), when: '' }, limits)).toEqual({});
	});

	it('counts whitespace the way the worker saves it', async () => {
		const model = new MemoryModel();
		await model.load();
		model.ensure('m1');
		model.forms.m1.text = `  ${rows[0].text.replace(/ /g, '   ')}  `;
		expect(model.dirty('m1')).toBe(false);
	});

	it('shows a page of rows, then more on request', async () => {
		http.get.mockResolvedValue({ items: Array.from({ length: PAGE_SIZE + 5 }, (_, i) => memory({ id: `m${i}` })), limits });
		const model = new MemoryModel();
		await model.load();
		expect(model.shown).toHaveLength(PAGE_SIZE);
		expect(model.hiddenCount).toBe(5);
		model.showMore();
		expect(model.shown).toHaveLength(PAGE_SIZE + 5);
	});

	it('keeps a draft across pages and drops a clean model', async () => {
		const model = acquireMemoryModel();
		await model.load();
		model.ensure('m1');
		model.forms.m1.text = 'A draft';
		releaseMemoryModel(model);
		const back = acquireMemoryModel();
		expect(back.forms.m1.text).toBe('A draft');
		back.discard('m1');
		releaseMemoryModel(back);
		expect(acquireMemoryModel()).not.toBe(back);
	});

	it('flags a newer saved version without touching the draft', async () => {
		const model = new MemoryModel();
		await model.load();
		model.ensure('m1');
		model.forms.m1.text = 'My draft';
		http.get.mockResolvedValue({ items: [memory({ revision: 4, text: 'Newer' })], limits });
		await model.load();
		expect(model.remote.m1).toBe('changed');
		expect(model.forms.m1.text).toBe('My draft');
		expect(model.forms.m1.base.revision).toBe(3);
	});
});

describe('Settings > Memory page', () => {
	it('lists active memories by default and filters by search', async () => {
		await show();
		await screen.findByText(rows[0].text);
		expect(screen.queryByText('Old deploy notes')).toBeNull();
		await fireEvent.input(screen.getByPlaceholderText('Search memories'), { target: { value: 'disagree' } });
		await waitFor(() => expect(screen.queryByText(rows[0].text)).toBeNull());
		expect(screen.getByText('Two answers disagree here')).toBeTruthy();
	});

	it('says nothing is saved yet only when the list really is empty', async () => {
		http.get.mockResolvedValue({ items: [], limits });
		await show();
		await screen.findByText(/No memories yet/);
	});

	it('shows a retry on a failed load, never an empty list', async () => {
		http.get.mockRejectedValueOnce({ status: 500, detail: 'boom' });
		await show();
		await screen.findByText('Could not load your memories.');
		expect(screen.queryByText(/No memories yet/)).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await screen.findByText(rows[0].text);
	});

	it('saves only when changed, sending the version it was opened from', async () => {
		await show('m1');
		await screen.findByRole('textbox', { name: 'What to remember' });
		expect(saveButton().disabled).toBe(true);
		await fireEvent.input(textBox(), { target: { value: 'Keep updates very short' } });
		expect(saveButton().disabled).toBe(false);
		await fireEvent.click(saveButton());
		await waitFor(() => expect(http.put).toHaveBeenCalledTimes(1));
		expect(http.put).toHaveBeenCalledWith('/memories/m1', {
			text: 'Keep updates very short',
			when: 'when reporting progress',
			expected_revision: 3
		});
		await waitFor(() => expect(saveButton().disabled).toBe(true));
	});

	it('keeps the draft on a stale save and offers the latest version', async () => {
		http.put.mockRejectedValue({ status: 409, detail: 'This memory changed since you opened it.' });
		await show('m1');
		await screen.findByRole('textbox', { name: 'What to remember' });
		await fireEvent.input(textBox(), { target: { value: 'My careful draft' } });
		await fireEvent.click(saveButton());
		await screen.findByText(/changed since you opened it/);
		expect(textBox().value).toBe('My careful draft');
		http.get.mockResolvedValue({ items: [memory({ revision: 4, text: 'The newer text' })], limits });
		await fireEvent.click(screen.getByRole('button', { name: 'Load latest' }));
		await waitFor(() => expect(textBox().value).toBe('The newer text'));
	});

	it('shows a readable message when writing is refused', async () => {
		http.put.mockRejectedValue({ status: 403, detail: 'Forbidden' });
		await show('m1');
		await screen.findByRole('textbox', { name: 'What to remember' });
		await fireEvent.input(textBox(), { target: { value: 'Blocked edit' } });
		await fireEvent.click(saveButton());
		await screen.findByText('You need owner access to change memories.');
		expect(textBox().value).toBe('Blocked edit');
	});

	it('deletes only after confirming, with the version it was opened from', async () => {
		await show('m1');
		await screen.findByRole('textbox', { name: 'What to remember' });
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		expect(http.delete).not.toHaveBeenCalled();
		await screen.findByText(/permanently on all your workers/);
		// The trigger has swapped for the confirm, which keeps the same word.
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(http.delete).toHaveBeenCalledWith('/memories/m1?expected_revision=3'));
		await waitFor(() => expect(settingsNav.selectedItem('memory')).toBeFalsy());
		await waitFor(() => expect(screen.queryByText(rows[0].text)).toBeNull());
	});

	it('keeps memory text out of the Settings search index', () => {
		const entry = MODULES.memory;
		expect(entry.entries.map((e) => e.id)).toEqual(['memory.list']);
		expect('searchSource' in entry).toBe(false);
	});
});
