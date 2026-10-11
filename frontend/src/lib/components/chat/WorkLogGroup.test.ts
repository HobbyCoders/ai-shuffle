import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import type { ChatMessage } from '$lib/stores/types';
import WorkLogGroup from './WorkLogGroup.svelte';
import { buildTranscriptModel, type DisplayItem } from '$lib/utils/work-log';
import { createFoldState } from '$lib/stores/session/fold-state';
import { deriveUserTurnNavigationItems, turnAnchorId } from '$lib/utils/transcript-navigation';

/** jsdom has no ResizeObserver. Drive reserved height from the stub. */
function installFakeRO(initialHeight = 80) {
	let height = initialHeight;
	let cb: ResizeObserverCallback | null = null;
	let el: Element | null = null;

	class FakeRO {
		constructor(callback: ResizeObserverCallback) {
			cb = callback;
		}
		observe = (target: Element) => {
			el = target;
			Object.defineProperty(target, 'getBoundingClientRect', {
				value: () => ({ height }),
				configurable: true
			});
			cb?.([], this as unknown as ResizeObserver);
		};
		unobserve = () => {};
		disconnect = () => {
			cb = null;
			el = null;
		};
	}

	const g = globalThis as unknown as { ResizeObserver?: unknown };
	const previous = g.ResizeObserver;
	g.ResizeObserver = FakeRO;

	return {
		async setHeight(next: number) {
			height = next;
			if (el) {
				Object.defineProperty(el, 'getBoundingClientRect', {
					value: () => ({ height }),
					configurable: true
				});
			}
			cb?.([], {} as ResizeObserver);
			await tick();
		},
		restore() {
			if (previous) g.ResizeObserver = previous;
			else delete g.ResizeObserver;
		}
	};
}

const message = {
	id: 'tool-1',
	role: 'assistant',
	type: 'tool_use',
	content: '',
	toolName: 'Read',
	toolStatus: 'complete'
} as ChatMessage;

const row = createRawSnippet<[ChatMessage, number]>((getMessage) => ({
	render: () => `<div data-testid="work-row">${getMessage().content || getMessage().toolName}</div>`
}));

const baseProps = {
	folded: [{ message, index: 0 }],
	tail: [],
	durationLabel: null,
	summary: 'Read 1 file',
	row
};

afterEach(cleanup);

describe('WorkLogGroup', () => {
	it('hides settled agent messages, then expands them in transcript order', async () => {
		const messages: ChatMessage[] = [
			{ id: 'peer-1', role: 'system', type: 'system', systemSubtype: 'agent_message', content: 'First delivery' },
			{ ...message, id: 'read', content: 'Read the source' },
			{ id: 'peer-2', role: 'system', type: 'system', systemSubtype: 'agent_message', content: 'Second delivery' },
			{ id: 'answer', role: 'assistant', type: 'text', content: 'Final answer' }
		];
		const group = buildTranscriptModel(messages, { isStreaming: false, groupingEnabled: true }).items[0];
		if (group.kind !== 'work_log') throw new Error('expected work log');
		render(WorkLogGroup, { ...group, row });
		expect(screen.queryByText('First delivery')).toBeNull();
		expect(screen.queryByText('Second delivery')).toBeNull();
		expect(screen.getByText('Final answer')).toBeTruthy();
		expect(screen.getByText('Read 1 file and got 2 agent messages')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { expanded: false }));
		expect(screen.getAllByTestId('work-row').map(el => el.textContent)).toEqual([
			'First delivery', 'Read the source', 'Second delivery', 'Final answer'
		]);
		await fireEvent.click(screen.getByRole('button', { expanded: true }));
		expect(screen.getAllByTestId('work-row').map(el => el.textContent)).toEqual(['Final answer']);
	});

	it('drops gallery duplicates only from rows that fed the gallery, not from a post-steer answer', () => {
		const a = '/a.png';
		const b = '/b.png';
		const messages: ChatMessage[] = [
			{ id: 'prompt', role: 'user', content: 'Make images', turnId: 'turn-1' },
			{ id: 'answer', role: 'assistant', type: 'text', content: `Two options. ![A](${a}) ![B](${b})` },
			{ id: 'steer', role: 'user', content: 'Show A again', turnId: 'turn-1' },
			{ id: 'later', role: 'assistant', type: 'text', content: `Here it is: ![A](${a})` }
		];
		const group = buildTranscriptModel(messages, { isStreaming: false, groupingEnabled: true }).items[1];
		if (group.kind !== 'work_log') throw new Error('expected work log');
		expect(group.gallery?.map(item => item.src)).toEqual([a, b]);
		const suppressionRow = createRawSnippet<[ChatMessage, number, ReadonlySet<string>?]>((getMessage, _getIndex, getSources) => ({
			render: () => `<div data-testid="gallery-row" data-id="${getMessage().id}" data-suppressed="${[...(getSources?.() ?? [])].join(',')}"></div>`
		}));
		render(WorkLogGroup, { ...group, row: suppressionRow });
		const suppressed = Object.fromEntries(
			screen.getAllByTestId('gallery-row').map(el => [el.dataset.id, el.dataset.suppressed])
		);
		expect(suppressed).toEqual({ answer: `${a},${b}`, steer: '', later: '' });
	});

	it('keeps legacy embedded turn anchors when settled work is collapsed or expanded', async () => {
		const messages = [
			{ id: 'prompt', role: 'user', content: 'Start', turnId: 'turn-1' },
			{ ...message, turnId: 'turn-1' },
			{ id: 'steer / 1', role: 'user', content: 'Also check this', turnId: 'turn-1' },
			{ id: 'steer-2', role: 'user', content: 'And this', turnId: 'turn-1' },
			{ id: 'answer', role: 'assistant', type: 'text', content: 'Done', turnId: 'turn-1' }
		] as ChatMessage[];
		const model = buildTranscriptModel(messages, { isStreaming: false, groupingEnabled: true });
		const group = model.items.find(item => item.kind === 'work_log');
		if (!group || group.kind !== 'work_log') throw new Error('Expected a settled work log');
		const observed = new Set<HTMLElement>();
		const view = render(WorkLogGroup, { props: {
			...group, row,
			// Current grouping splits at steers. Pin the component's legacy ordered
			// sequence contract explicitly rather than expecting the builder to
			// put new steers back inside a disclosure.
			sequence: messages.slice(1).map((message, index) => ({
				message, index: index + 1, hiddenWhenCollapsed: message.role !== 'user'
			})),
			observeTurnAnchor: (node: HTMLElement) => {
				if (node.dataset.userTurnId) observed.add(node);
				return { destroy: () => { observed.delete(node); } };
			}
		} });
		function expectAnchors() {
			for (const item of deriveUserTurnNavigationItems(messages).slice(1)) {
				const anchor = document.getElementById(turnAnchorId(item.id));
				expect(anchor?.dataset.userTurnId).toBe(item.id);
				expect(anchor?.textContent).toContain(item.label);
				expect(observed.has(anchor!)).toBe(true);
			}
			expect(view.container.querySelectorAll('[data-user-turn-id]')).toHaveLength(2);
			expect(observed.size).toBe(2);
		}
		expectAnchors();
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expectAnchors();
		await fireEvent.click(screen.getByRole('button', { name: /Hide work details/i }));
		expectAnchors();
		view.unmount();
		expect(observed.size).toBe(0);
	});

	it('uses action text for a collapsed live group', () => {
		render(WorkLogGroup, { props: { ...baseProps, mode: 'live' } });
		expect(screen.getByRole('button', { name: /Show work details: Read 1 file/i })).toBeTruthy();
	});

	it('closes an inspected live group when it settles', async () => {
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByTestId('work-row')).toBeTruthy();

		await view.rerender({ ...baseProps, mode: 'settled', durationLabel: 'Worked for 4s' });
		expect(screen.queryByTestId('work-row')).toBeNull();
		expect(screen.getByRole('button', { name: /Show work details: Worked for 4s, Read 1 file/i })).toBeTruthy();
	});

	it('holds peak height while live and releases it on settle', async () => {
		const ro = installFakeRO(80);
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		const section = view.container.querySelector('section.worklog') as HTMLElement;
		expect(section.style.minHeight).toBe('80px');

		await view.rerender({ ...baseProps, mode: 'settled' as const, durationLabel: 'Worked for 4s' });
		expect(section.style.minHeight).toBe('');
		ro.restore();
	});

	it('does not keep the open-fold height after the user collapses it', async () => {
		const ro = installFakeRO(80);
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		const section = view.container.querySelector('section.worklog') as HTMLElement;
		expect(section.style.minHeight).toBe('80px');

		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		await ro.setHeight(240);
		expect(section.style.minHeight).toBe('80px');

		await fireEvent.click(screen.getByRole('button', { name: /Hide work details/i }));
		await ro.setHeight(80);
		expect(section.style.minHeight).toBe('80px');
		ro.restore();
	});

	it('releases reserved height when the same live tail row shrinks', async () => {
		const ro = installFakeRO(80);
		const tail = [{ message, index: 0 }];
		render(WorkLogGroup, { props: { ...baseProps, folded: [], tail, mode: 'live' as const } });
		const section = document.querySelector('section.worklog') as HTMLElement;

		await ro.setHeight(200);
		expect(section.style.minHeight).toBe('200px');

		await ro.setHeight(90);
		expect(section.style.minHeight).toBe('90px');
		ro.restore();
	});

	it('keeps peak height when a shorter successor replaces the tail', async () => {
		const ro = installFakeRO(80);
		const tailA = [{ message: { ...message, id: 'tool-a' }, index: 0 }];
		const tailB = [{ message: { ...message, id: 'tool-b' }, index: 1 }];
		const view = render(WorkLogGroup, {
			props: { ...baseProps, folded: [], tail: tailA, mode: 'live' as const }
		});
		const section = view.container.querySelector('section.worklog') as HTMLElement;

		await ro.setHeight(200);
		expect(section.style.minHeight).toBe('200px');

		await view.rerender({
			...baseProps,
			folded: tailA,
			tail: tailB,
			mode: 'live' as const
		});
		await ro.setHeight(90);
		expect(section.style.minHeight).toBe('200px');
		ro.restore();
	});

	it('keeps the terminal answer visible while settled work is collapsed', async () => {
		const answer = {
			id: 'answer-1',
			role: 'assistant',
			type: 'text',
			content: 'Final answer'
		} as ChatMessage;
		render(WorkLogGroup, {
			props: {
				...baseProps,
				mode: 'settled',
				durationLabel: 'Worked for 4s',
				sequence: [
					{ message, index: 0, hiddenWhenCollapsed: true },
					{ message: answer, index: 1, hiddenWhenCollapsed: false }
				]
			}
		});

		expect(screen.getByText('Final answer')).toBeTruthy();
		expect(screen.queryByText('Read')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByText('Read')).toBeTruthy();
		expect(screen.getByText('Final answer')).toBeTruthy();
	});

	describe('live runs and settle (spec §2, §6)', () => {
		type WorkLog = Extract<DisplayItem, { kind: 'work_log' }>;
		const turn: ChatMessage[] = [
			{ id: 'prompt', role: 'user', content: 'Audit it', turnId: 'turn-1' },
			{ ...message, id: 'read-a', content: 'Read A' },
			{ id: 'finding', role: 'assistant', type: 'text', content: 'Found:\n- a shared token' },
			{ ...message, id: 'read-b', content: 'Read B' },
			{ id: 'answer', role: 'assistant', type: 'text', content: 'Done.' }
		];
		const logOf = (isStreaming: boolean, messages = turn) => {
			const item = buildTranscriptModel(messages, { isStreaming, groupingEnabled: true }).items
				.find((entry) => entry.kind === 'work_log') as WorkLog;
			return { ...item, row: expandableRow };
		};
		/** A row with its own expansion state, kept on its DOM node: a remount loses it. */
		const expandableRow = createRawSnippet<[ChatMessage, number]>((getMessage) => ({
			render: () => `<div data-testid="work-row" data-id="${getMessage().id}"><button type="button">${getMessage().content}</button></div>`,
			setup: (node) => {
				node.querySelector('button')!.addEventListener('click', () => { (node as HTMLElement).dataset.expanded = 'true'; });
			}
		}));
		const rowEl = (id: string) => document.querySelector(`[data-testid="work-row"][data-id="${id}"]`) as HTMLElement | null;
		/** On screen: mounted and its sequence row not hidden. */
		const shown = (id: string) => {
			const wrapper = rowEl(id)?.closest<HTMLElement>('.sequence-row');
			return !!wrapper && !wrapper.hidden;
		};
		const memoryStorage = (): Storage => {
			const data = new Map<string, string>();
			return {
				get length() { return data.size; }, clear: () => data.clear(),
				getItem: (k) => data.get(k) ?? null, key: (i) => [...data.keys()][i] ?? null,
				removeItem: (k) => { data.delete(k); }, setItem: (k, v) => { data.set(k, String(v)); }
			};
		};

		it('folds the earlier run behind the live label and keeps the boundary and current run open', () => {
			render(WorkLogGroup, { ...logOf(true), folds: createFoldState(memoryStorage()) });
			expect(screen.getByRole('button', { name: 'Show work details: Read 1 file' })).toBeTruthy();
			expect(rowEl('read-a')).toBeNull();
			expect(['finding', 'read-b', 'answer'].map(shown)).toEqual([true, true, true]);
		});

		it('keeps a row expanded live still expanded after the turn settles', async () => {
			const view = render(WorkLogGroup, { ...logOf(true), folds: createFoldState(memoryStorage()) });
			const live = rowEl('read-b')!;
			await fireEvent.click(live.querySelector('button')!);
			expect(live.dataset.expanded).toBe('true');
			const finding = rowEl('finding');

			await view.rerender(logOf(false));
			// No row remounts at settle: the same nodes carry on.
			expect(rowEl('finding')).toBe(finding);
			expect(view.container.querySelector('section')!.dataset.workLogMode).toBe('settled');
			// Settled, the Read folds into the turn: same node, kept but hidden.
			expect(rowEl('read-b')).toBe(live);
			expect(shown('read-b')).toBe(false);
			expect(shown('finding')).toBe(true);
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			expect(rowEl('read-b')).toBe(live);
			expect(shown('read-b')).toBe(true);
			expect(live.dataset.expanded).toBe('true');
		});

		it('stores run and turn folds by turn key, so a remount restores them', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const first = render(WorkLogGroup, { ...logOf(true), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(shown('read-a')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:0': true });
			first.unmount();

			// A window remount, a session switch back or a reload (new store, same storage).
			render(WorkLogGroup, { ...logOf(true), folds: createFoldState(storage) });
			expect(shown('read-a')).toBe(true);
			await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
			expect(shown('read-a')).toBe(false);
			cleanup();

			const settled = render(WorkLogGroup, { ...logOf(false), folds });
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			expect(folds.isOpen('prompt:turn')).toBe(true);
			settled.unmount();
			render(WorkLogGroup, { ...logOf(false), folds: createFoldState(storage) });
			expect(screen.getByRole('button', { name: 'Hide work details' })).toBeTruthy();
		});

		it('moves folds opened under an optimistic id onto the server id', async () => {
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(folds.isOpen('prompt:0')).toBe(true);

			// The reducer reconciles the optimistic prompt in place and records its server id.
			const reconciled = [{ ...turn[0], serverId: 'srv-1' }, ...turn.slice(1)];
			await view.rerender(logOf(true, reconciled));
			expect(folds.current).toEqual({ 'srv-1:0': true });
			expect(shown('read-a')).toBe(true);
		});
	});
});
