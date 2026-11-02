import { describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { TaskChip, TaskChipList } from '$lib/api/sessions';
import { createTaskChips, STARTED_CHIP_TTL_MS, STARTING_RECHECK_MS, taskChipModel, visibleTaskChips, type TaskChipDeps } from './task-chips';

const NOW = Date.parse('2026-11-02T12:00:00Z');

const chip = (id: string, patch: Partial<TaskChip> = {}): TaskChip => ({
	id, session_id: 'root', origin_session_id: 'root', origin_label: null, title: `Chip ${id}`, summary: 'Why', prompt: 'Brief',
	model: 'fake-ai', provider_id: null, model_label: 'Fake AI', effort: null, base: 'default', state: 'pending', error: null,
	retryable: false, started_session_id: null, started_session_title: null,
	created_at: new Date(NOW).toISOString(), updated_at: new Date(NOW).toISOString(), ...patch
});

function memoryStorage() {
	const data = new Map<string, string>();
	return { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => void data.set(key, value) };
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (error: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function setup(lists: Record<string, TaskChip[] | TaskChipList>, overrides: Partial<TaskChipDeps> = {}) {
	const list = vi.fn(async (sessionId: string) => {
		const value = lists[sessionId] ?? [];
		return Array.isArray(value) ? { chips: value, source: null } : value;
	});
	const deps: TaskChipDeps = {
		list,
		start: vi.fn(async (_session, id) => chip(id, { state: 'started', started_session_id: 'new-chat' })),
		dismiss: vi.fn(async (_session, id) => chip(id, { state: 'dismissed' })),
		storage: memoryStorage(),
		now: () => NOW,
		...overrides
	};
	return { store: createTaskChips(deps), deps, list };
}

describe('task chips store', () => {
	it('reads the chip list on opening a chat', async () => {
		const { store, list } = setup({ root: [chip('a')] });
		store.observe('root');
		await flush();
		expect(list).toHaveBeenCalledWith('root');
		expect(get(store).chips.map((row) => row.id)).toEqual(['a']);
	});

	it('re-reads on a frame for the open chat and ignores frames for other chats', async () => {
		const lists = { root: [chip('a')] };
		const { store, list } = setup(lists);
		store.observe('root');
		await flush();
		list.mockClear();

		store.receive('another-chat');
		await flush();
		expect(list).not.toHaveBeenCalled();

		lists.root = [chip('a'), chip('b')];
		store.receive('root');
		await flush();
		expect(get(store).chips.map((row) => row.id)).toEqual(['a', 'b']);
	});

	it('re-reads after a reconnect, because frames may have been missed', async () => {
		const lists = { root: [chip('a')] };
		const { store } = setup(lists);
		store.observe('root');
		await flush();
		lists.root = [chip('a', { state: 'failed', error: 'No checkout', retryable: true })];
		store.refresh();
		await flush();
		expect(get(store).chips[0].state).toBe('failed');
	});

	it("never shows another chat's chips, even when reads answer out of order", async () => {
		const reads: Record<string, ReturnType<typeof deferred<TaskChipList>>> = { one: deferred(), two: deferred() };
		const { store } = setup({}, { list: vi.fn((id: string) => reads[id].promise) });
		store.observe('one');
		store.observe('two');
		reads.two.resolve({ chips: [chip('from-two')], source: null });
		await flush();
		reads.one.resolve({ chips: [chip('from-one')], source: null });
		await flush();
		const view = get(store);
		expect(view.sessionId).toBe('two');
		expect(view.chips.map((row) => row.id)).toEqual(['from-two']);
	});

	it("publishes a chat's own last known chips straight away on switching back", async () => {
		const { store, list } = setup({ one: [chip('a')], two: [chip('b')] });
		store.observe('one');
		await flush();
		store.observe('two');
		await flush();
		list.mockClear();
		store.observe('one');
		expect(get(store).sessionId).toBe('one');
		expect(get(store).chips.map((row) => row.id)).toEqual(['a']);
		expect(list).toHaveBeenCalledWith('one');
	});

	it('keeps what it shows when a read fails', async () => {
		const lists = { root: [chip('a')] };
		const { store, list } = setup(lists);
		store.observe('root');
		await flush();
		list.mockRejectedValueOnce(new Error('offline'));
		store.refresh();
		await flush();
		expect(get(store).chips.map((row) => row.id)).toEqual(['a']);
	});

	it('runs a read that was asked for during another read', async () => {
		const first = deferred<TaskChipList>();
		const list = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue({ chips: [chip('a'), chip('b')], source: null });
		const { store } = setup({}, { list });
		store.observe('root');
		store.receive('root');
		expect(list).toHaveBeenCalledTimes(1);
		first.resolve({ chips: [chip('a')], source: null });
		await flush();
		expect(list).toHaveBeenCalledTimes(2);
		expect(get(store).chips).toHaveLength(2);
	});

	it('sends one start for a double click, and shows it as busy meanwhile', async () => {
		const answer = deferred<TaskChip>();
		const start = vi.fn(() => answer.promise);
		const { store } = setup({ root: [chip('a')] }, { start });
		store.observe('root');
		await flush();
		void store.start('a');
		void store.start('a');
		expect(start).toHaveBeenCalledTimes(1);
		expect(get(store).busy.a).toBe('start');
		answer.resolve(chip('a', { state: 'started', started_session_id: 'new-chat' }));
		await flush();
		expect(get(store).busy.a).toBeUndefined();
		expect(get(store).chips[0].state).toBe('started');
		expect(get(store).chips[0].started_session_id).toBe('new-chat');
	});

	it('does not let a read that began before the start answered undo it', async () => {
		const staleRead = deferred<TaskChipList>();
		const lists = { root: [chip('a')] };
		const { store, deps } = setup(lists);
		store.observe('root');
		await flush();
		(deps.list as ReturnType<typeof vi.fn>).mockReturnValueOnce(staleRead.promise);
		store.refresh();
		await store.start('a');
		expect(get(store).chips[0].state).toBe('started');

		// The older read answers with the chip as it was, and only earns another read.
		const states: string[] = [];
		const stop = store.subscribe((view) => states.push(view.chips[0]?.state));
		lists.root = [chip('a', { state: 'started', started_session_id: 'new-chat' })];
		staleRead.resolve({ chips: [chip('a')], source: null });
		await flush();
		stop();
		expect(states).not.toContain('pending');
		expect((deps.list as ReturnType<typeof vi.fn>).mock.calls.length).toBe(3);
	});

	it('shows a failed start from the worker as the chip itself', async () => {
		const failed = chip('a', { state: 'failed', error: 'Could not give the new chat its own checkout.', retryable: true });
		const { store } = setup({ root: [chip('a')] }, { start: vi.fn(async () => failed) });
		store.observe('root');
		await flush();
		await store.start('a');
		expect(get(store).chips[0]).toMatchObject({ state: 'failed', retryable: true });
		expect(get(store).errors).toEqual({});
	});

	it.each([
		['network', new TypeError('Failed to fetch'), /Could not reach the app/],
		['offline owner', { detail: 'unreachable', status: 503 }, /offline/]
	])('keeps the chip actionable and says why after a %s failure on the click', async (_name, error, words) => {
		const lists = { root: [chip('a')] };
		const { store, deps } = setup(lists, { start: vi.fn(async () => { throw error; }) });
		store.observe('root');
		await flush();
		await store.start('a');
		await flush();
		const view = get(store);
		expect(view.errors.a).toMatch(words);
		expect(view.busy.a).toBeUndefined();
		expect(view.chips[0].state).toBe('pending');

		(deps.start as ReturnType<typeof vi.fn>).mockResolvedValueOnce(chip('a', { state: 'started', started_session_id: 'n' }));
		await store.start('a');
		expect(get(store).errors).toEqual({});
		expect(get(store).chips[0].state).toBe('started');
	});

	it('shows what really happened when the answer to a start was lost', async () => {
		const lists = { root: [chip('a')] };
		const { store } = setup(lists, {
			start: vi.fn(async () => {
				lists.root = [chip('a', { state: 'started', started_session_id: 'n' })];
				throw new TypeError('Failed to fetch');
			})
		});
		store.observe('root');
		await flush();
		await store.start('a');
		await flush();
		expect(get(store).chips[0].state).toBe('started');
		expect(get(store).errors).toEqual({});
	});

	it('removes a dismissed chip and says so when a dismiss fails', async () => {
		const dismiss = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValue(chip('a', { state: 'dismissed' }));
		const { store } = setup({ root: [chip('a')] }, { dismiss });
		store.observe('root');
		await flush();
		await store.dismiss('a');
		await flush();
		expect(get(store).errors.a).toMatch(/dismiss/);
		expect(get(store).chips).toHaveLength(1);
		await store.dismiss('a');
		expect(get(store).chips).toEqual([]);
	});

	it('checks again on a chip started elsewhere, in case the frame is lost', async () => {
		vi.useFakeTimers();
		try {
			const lists = { root: [chip('a', { state: 'starting' })] };
			const { store } = setup(lists);
			store.observe('root');
			await vi.advanceTimersByTimeAsync(0);
			lists.root = [chip('a', { state: 'started', started_session_id: 'n' })];
			await vi.advanceTimersByTimeAsync(STARTING_RECHECK_MS);
			expect(get(store).chips[0].state).toBe('started');
			store.observe(null);
		} finally {
			vi.useRealTimers();
		}
	});

	it('clears a started chip once its chat is opened, remembered across a reload', async () => {
		const storage = memoryStorage();
		const started = chip('a', { state: 'started', started_session_id: 'new-chat' });
		const first = setup({ root: [started] }, { storage });
		first.store.observe('root');
		await flush();
		expect(get(first.store).chips).toHaveLength(1);
		first.store.clear('a');
		expect(get(first.store).chips).toEqual([]);

		const reloaded = setup({ root: [started] }, { storage });
		reloaded.store.observe('root');
		await flush();
		expect(get(reloaded.store).chips).toEqual([]);
	});

	it('clears the chip when its chat is opened by any route, because that chat names its source', async () => {
		const storage = memoryStorage();
		const started = chip('a', { state: 'started', started_session_id: 'new-chat' });
		const { store } = setup({
			'new-chat': { chips: [], source: { chip_id: 'a', session_id: 'root', title: 'Root' } },
			root: [started]
		}, { storage });
		store.observe('new-chat');
		await flush();
		expect(get(store).source?.session_id).toBe('root');
		store.observe('root');
		await flush();
		expect(get(store).chips).toEqual([]);
	});

	it('shows a chip started in another tab, and follows a clear made there', async () => {
		const storage = memoryStorage();
		const started = chip('a', { state: 'started', started_session_id: 'new-chat' });
		const { store } = setup({ root: [started] }, { storage });
		store.observe('root');
		await flush();
		expect(get(store).chips).toHaveLength(1);

		const otherTab = setup({ root: [started] }, { storage });
		otherTab.store.clear('a');
		store.syncSeen();
		expect(get(store).chips).toEqual([]);
	});

	it('survives storage that cannot be read or written', async () => {
		const storage = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
		const { store } = setup({ root: [chip('a', { state: 'started', started_session_id: 'n' })] }, { storage });
		store.observe('root');
		await flush();
		expect(get(store).chips).toHaveLength(1);
		store.clear('a');
		expect(get(store).chips).toEqual([]);
	});

	it('drops everything cached for a new signed-in owner', async () => {
		const lists: Record<string, TaskChip[]> = { root: [chip('private')] };
		const { store } = setup(lists);
		store.observe('root');
		await flush();
		lists.root = [];
		store.reset();
		expect(get(store).chips).toEqual([]);
		await flush();
		expect(get(store).chips).toEqual([]);
	});
});

describe('visibleTaskChips', () => {
	const hoursAgo = (hours: number) => new Date(NOW - hours * 3_600_000).toISOString();

	it('keeps every state the owner can act on, and hides dismissed', () => {
		const rows = [chip('p'), chip('s', { state: 'starting' }), chip('f', { state: 'failed' }), chip('d', { state: 'dismissed' })];
		expect(visibleTaskChips(rows, new Set(), NOW).map((row) => row.id)).toEqual(['p', 's', 'f']);
	});

	it('keeps old pending chips: nothing the owner has not decided may vanish', () => {
		expect(visibleTaskChips([chip('p', { updated_at: hoursAgo(24 * 90) })], new Set(), NOW)).toHaveLength(1);
	});

	it('keeps a started chip until it is seen or a day has passed', () => {
		const recent = chip('r', { state: 'started', updated_at: hoursAgo(23) });
		const old = chip('o', { state: 'started', updated_at: new Date(NOW - STARTED_CHIP_TTL_MS - 1000).toISOString() });
		expect(visibleTaskChips([recent, old], new Set(), NOW).map((row) => row.id)).toEqual(['r']);
		expect(visibleTaskChips([recent], new Set(['r']), NOW)).toEqual([]);
	});
});

describe('taskChipModel', () => {
	it('shows the label, and the effort when set', () => {
		expect(taskChipModel({ model: 'x', model_label: 'Sonnet', effort: null })).toBe('Sonnet');
		expect(taskChipModel({ model: 'x', model_label: 'Sonnet', effort: 'high' })).toBe('Sonnet · high');
	});

	it('falls back to the model id rather than showing nothing', () => {
		expect(taskChipModel({ model: 'odd-model', model_label: null, effort: null })).toBe('odd-model');
	});
});
