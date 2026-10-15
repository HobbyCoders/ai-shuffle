import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import type { ChatMessage } from '$lib/stores/types';
import WorkLogGroup from './WorkLogGroup.svelte';
import TranscriptItems from './test-fixtures/TranscriptItems.svelte';
import QueuedWorkLog from './test-fixtures/QueuedWorkLog.svelte';
import { buildTranscriptModel, type DisplayItem } from '$lib/utils/work-log';
import { createFoldState } from '$lib/stores/session/fold-state';
import { deriveUserTurnNavigationItems, turnAnchorId } from '$lib/utils/transcript-navigation';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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
	it('reveals a distant protected user turn inside a giant group without exceeding the row cap', async () => {
		const entries = Array.from({ length: 2000 }, (_, index) => ({
			message: index === 500
				? { id: 'nested-user', role: 'user' as const, type: 'text' as const, content: 'Nested prompt' }
				: { ...message, id: `nested-${index}`, content: `Nested row ${index}` },
			index, hiddenWhenCollapsed: false
		}));
		const view = render(WorkLogGroup, { ...baseProps, mode: 'live', sequence: entries, folded: entries });
		expect(document.getElementById(turnAnchorId('nested-user'))).toBeNull();
		await view.rerender({ revealMessageId: 'nested-user' });
		expect(document.getElementById(turnAnchorId('nested-user'))).not.toBeNull();
		expect(screen.getAllByTestId('work-row')).toHaveLength(60);
	});

	it('bounds a 2,000-row open work log and pages without retaining all touched rows', async () => {
		const entries = Array.from({ length: 2000 }, (_, index) => ({
			message: { ...message, id: `large-${index}`, content: `Row ${index}` },
			index, hiddenWhenCollapsed: true
		}));
		render(WorkLogGroup, { ...baseProps, mode: 'settled', sequence: entries, folded: entries });
		await fireEvent.click(screen.getByRole('button', { expanded: false }));
		expect(screen.getAllByTestId('work-row')).toHaveLength(60);
		for (const el of screen.getAllByTestId('work-row').slice(0, 12)) await fireEvent.click(el);
		await fireEvent.click(screen.getByRole('button', { name: 'Show earlier log entries' }));
		expect(screen.getAllByTestId('work-row')).toHaveLength(60);
		expect(screen.queryByText('Row 1999')).toBeNull();
		expect(screen.getByText('Row 1920')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
		expect(screen.queryAllByTestId('work-row').length).toBeLessThanOrEqual(8);
	});

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
		// The bottom "Show fewer log entries" row (the header is covered below).
		await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
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
		await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
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

		await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
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

	describe('sticky fold header (spec §7)', () => {
		const answer = { id: 'answer-1', role: 'assistant', type: 'text', content: 'Final answer' } as ChatMessage;
		const settledProps = {
			...baseProps,
			mode: 'settled' as const,
			durationLabel: 'Worked for 4s',
			sequence: [
				{ message, index: 0, hiddenWhenCollapsed: true },
				{ message: answer, index: 1, hiddenWhenCollapsed: false }
			]
		};
		const header = () => document.querySelector<HTMLButtonElement>('[data-fold-header]');
		const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/WorkLogGroup.svelte'), 'utf8');

		it('renders no header while the group is closed', () => {
			render(WorkLogGroup, { props: settledProps });
			expect(header()).toBeNull();
			expect(screen.getByRole('button', { expanded: false }).textContent).toContain('Worked for 4s');
		});

		it('repeats the fold row as the sticky header of an open group', async () => {
			const view = render(WorkLogGroup, { props: settledProps });
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			const head = header()!;
			expect(head).toBeTruthy();
			// The existing fold row: same element, classes and label, now expanded.
			expect(head.tagName).toBe('BUTTON');
			expect(head.className).toMatch(/\bwlrow\b/);
			expect(head.className).toMatch(/\bfold\b/);
			expect(head.getAttribute('aria-expanded')).toBe('true');
			expect(head.getAttribute('aria-label')).toBe('Hide work details: Worked for 4s, Read 1 file');
			expect(head.textContent).toContain('Worked for 4s');
			// It heads the group: first row in the group's own box, so it sticks
			// for exactly as long as the group is on screen.
			expect(view.container.querySelector('.worklog-measure')!.firstElementChild).toBe(head);
			// The bottom row stays.
			expect(screen.getByRole('button', { name: 'Hide work details' }).textContent).toContain('Show fewer log entries');
			// The one layout rule the spec adds, on the existing fold row.
			expect(source).toMatch(/\.wlrow\.fold\[data-fold-header\],\s*\.wlrow\.fold\[data-run-header\] \{\s*position: sticky;\s*top: 0;/);
		});

		it('collapses the group when the header is clicked', async () => {
			render(WorkLogGroup, { props: settledProps });
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			expect(screen.getByText('Read')).toBeTruthy();
			await fireEvent.click(header()!);
			expect(header()).toBeNull();
			expect(screen.queryByText('Read')).toBeNull();
			expect(screen.getByText('Final answer')).toBeTruthy();
			expect(screen.getByRole('button', { expanded: false })).toBeTruthy();
		});

		it('brings the collapsed group back into view only when the header was stuck', async () => {
			const frames: FrameRequestCallback[] = [];
			const raf = globalThis.requestAnimationFrame;
			globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
			try {
				// An overflow-hidden layout box around the transcript scroller, as
				// the chat column is: it must never move.
				const column = document.createElement('div');
				column.style.overflowY = 'hidden';
				const scroller = document.createElement('div');
				scroller.style.overflowY = 'auto';
				column.appendChild(scroller);
				document.body.appendChild(column);
				const view = render(WorkLogGroup, { props: settledProps, target: scroller });
				const section = view.container.querySelector('section.worklog') as HTMLElement;
				let scrollerTop = 1000;
				Object.defineProperty(scroller, 'scrollTop', { get: () => scrollerTop, set: (v: number) => { scrollerTop = v; }, configurable: true });
				Object.defineProperty(column, 'scrollTop', { get: () => 0, set: () => { throw new Error('chat column scrolled'); }, configurable: true });
				const place = (el: Element, top: number) =>
					Object.defineProperty(el, 'getBoundingClientRect', { value: () => ({ top }), configurable: true });

				// At its natural place (the group's top is on screen): no scroll.
				await fireEvent.click(screen.getByRole('button', { expanded: false }));
				place(section, 120);
				place(header()!, 120);
				await fireEvent.click(header()!);
				frames.splice(0).forEach((cb) => cb(0));
				expect(scrollerTop).toBe(1000);

				// Stuck 52px down the page, 600px below the group's own top: only
				// the scroller moves, by exactly that distance.
				await fireEvent.click(screen.getByRole('button', { expanded: false }));
				place(section, -548);
				place(header()!, 52);
				await fireEvent.click(header()!);
				frames.splice(0).forEach((cb) => cb(0));
				expect(scrollerTop).toBe(400);
				column.remove();
			} finally {
				globalThis.requestAnimationFrame = raf;
			}
		});

		it('heads the legacy open shape too', async () => {
			render(WorkLogGroup, { props: { ...baseProps, mode: 'settled' as const, durationLabel: 'Worked for 4s' } });
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			expect(header()?.getAttribute('aria-label')).toBe('Hide work details: Worked for 4s, Read 1 file');
			await fireEvent.click(header()!);
			expect(header()).toBeNull();
			expect(screen.queryByTestId('work-row')).toBeNull();
		});
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
		// Live, the answer is still streaming: finished prose would end the run
		// and fold `read-b`, while these tests want it in the current, open run.
		const liveTurn = turn.map((m) => (m.id === 'answer' ? { ...m, streaming: true } : m));
		const logOf = (isStreaming: boolean, messages = isStreaming ? liveTurn : turn) => {
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
			expect(folds.current).toEqual({ 'prompt:run-read-a..finding': true });
			first.unmount();

			// A window remount, a session switch back or a reload (new store, same storage).
			const reloaded = createFoldState(storage);
			render(WorkLogGroup, { ...logOf(true), folds: reloaded });
			expect(shown('read-a')).toBe(true);
			await fireEvent.click(screen.getByRole('button', { name: 'Hide work details' }));
			expect(shown('read-a')).toBe(false);
			expect(reloaded.current).toEqual({});
			cleanup();

			const settled = render(WorkLogGroup, { ...logOf(false), folds: reloaded });
			await fireEvent.click(screen.getByRole('button', { expanded: false }));
			expect(reloaded.isOpen('prompt:turn')).toBe(true);
			settled.unmount();
			render(WorkLogGroup, { ...logOf(false), folds: createFoldState(storage) });
			expect(screen.getByRole('button', { name: 'Hide work details' })).toBeTruthy();
		});

		it('opens the settled fold when the reader had an earlier run open, and keeps it through a reload', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(shown('read-a')).toBe(true);

			await view.rerender(logOf(false));
			// The run merged into the turn's fold, which opened: nothing the reader
			// was reading closes at settle, and the promotion is stored.
			expect(shown('read-a')).toBe(true);
			expect(screen.getByRole('button', { name: 'Hide work details' })).toBeTruthy();
			expect(folds.current).toEqual({ 'prompt:turn': true });
			view.unmount();

			render(WorkLogGroup, { ...logOf(false), folds: createFoldState(storage) });
			expect(shown('read-a')).toBe(true);
			expect(screen.getByRole('button', { name: 'Hide work details' })).toBeTruthy();
		});

		it('heads an open run with its fold row, and keeps the group open under the sticky header at settle', async () => {
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			expect(document.querySelector('[data-fold-header]')).toBeNull();
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			// Live, the open run's fold row heads it and sticks within the run.
			const runHead = screen.getByRole('button', { name: 'Hide work details: Read 1 file' });
			expect(runHead.getAttribute('aria-expanded')).toBe('true');
			expect(runHead.hasAttribute('data-run-header')).toBe(true);
			expect(runHead.hasAttribute('data-fold-header')).toBe(false);
			const source = readFileSync(resolve(__dirname, 'WorkLogGroup.svelte'), 'utf8');
			expect(source).toMatch(/\.wlrow\.fold\[data-fold-header\],\s*\.wlrow\.fold\[data-run-header\] \{\s*position: sticky;\s*top: 0;/);
			const readA = rowEl('read-a');
			const finding = rowEl('finding');

			await view.rerender(logOf(false));
			const head = document.querySelector<HTMLButtonElement>('[data-fold-header]')!;
			expect(head.getAttribute('aria-expanded')).toBe('true');
			expect(head.getAttribute('aria-label')).toMatch(/^Hide work details: /);
			// Same nodes, still on screen: settling flipped the mode, not the tree.
			expect(rowEl('read-a')).toBe(readA);
			expect(rowEl('finding')).toBe(finding);
			expect(['read-a', 'finding', 'read-b', 'answer'].map(shown)).toEqual([true, true, true, true]);

			// The header collapses the settled turn and the store records it.
			await fireEvent.click(head);
			expect(shown('read-a')).toBe(false);
			expect(document.querySelector('[data-fold-header]')).toBeNull();
			expect(folds.isOpen('prompt:turn')).toBe(false);
		});

		it('keeps each open run sticky header inside its run, handing over to the next', async () => {
			const frames: FrameRequestCallback[] = [];
			const raf = globalThis.requestAnimationFrame;
			globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
			const flush = () => frames.splice(0).forEach((cb) => cb(0));
			try {
				const twoRuns: ChatMessage[] = [turn[0], ...turn.slice(1, 3), { ...message, id: 'read-c', content: 'Read C' },
					{ id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage,
					{ ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true }];
				const scroller = document.createElement('div');
				scroller.style.overflowY = 'auto';
				document.body.appendChild(scroller);
				const view = render(WorkLogGroup, { props: { ...logOf(true, twoRuns), folds: createFoldState(memoryStorage()) }, target: scroller });
				for (const button of screen.getAllByRole('button', { name: /^Show work details/ })) await fireEvent.click(button);
				const heads = [...view.container.querySelectorAll<HTMLElement>('[data-run-header]')];
				const ends = [...view.container.querySelectorAll<HTMLElement>('[data-run-end]')];
				expect(heads).toHaveLength(2);
				expect(ends).toHaveLength(2);
				const rect = (el: Element, top: number, bottom: number) =>
					Object.defineProperty(el, 'getBoundingClientRect', { value: () => ({ top, bottom, height: bottom - top }), configurable: true });
				heads.forEach((head) => Object.defineProperty(head, 'offsetHeight', { value: 32, configurable: true }));
				rect(scroller, 0, 600);

				// Inside run 1: its header is stuck at the top, run 2 is further down.
				rect(ends[0], 0, 300);
				rect(ends[1], 0, 900);
				await fireEvent.scroll(scroller);
				flush();
				expect(heads[0].style.transform).toBe('');
				expect(heads[0].hasAttribute('data-run-header-off')).toBe(false);

				// Run 1's last row reaches the header: the header rides up with it.
				rect(ends[0], 0, 20);
				await fireEvent.scroll(scroller);
				flush();
				expect(heads[0].style.transform).toBe('translateY(-12px)');
				expect(heads[0].hasAttribute('data-run-header-off')).toBe(false);

				// Run 1 has scrolled past and run 2's header has taken over the line:
				// run 1's header is fully off it and run 2's sits untouched.
				rect(ends[0], 0, -200);
				rect(ends[1], 0, 400);
				await fireEvent.scroll(scroller);
				flush();
				expect(heads[0].style.transform).toBe('translateY(-32px)');
				expect(heads[0].hasAttribute('data-run-header-off')).toBe(true);
				expect(heads[1].style.transform).toBe('');
				expect(heads[1].hasAttribute('data-run-header-off')).toBe(false);
				view.unmount();
				scroller.remove();
			} finally {
				globalThis.requestAnimationFrame = raf;
			}
		});

		describe('run header geometry without scrolling', () => {
			const twoRuns: ChatMessage[] = [turn[0], ...turn.slice(1, 3), { ...message, id: 'read-c', content: 'Read C' },
				{ id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage,
				{ ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true }];
			const rect = (el: Element, top: number, bottom = top) =>
				Object.defineProperty(el, 'getBoundingClientRect', { value: () => ({ top, bottom, height: bottom - top }), configurable: true });
			let frames: FrameRequestCallback[] = [];
			let raf: typeof globalThis.requestAnimationFrame;
			let observers: Array<() => void> = [];
			let previousRO: unknown;
			const flush = () => frames.splice(0).forEach((cb) => cb(0));
			/** Fire every ResizeObserver the component made, as a layout change would. */
			const resized = () => observers.forEach((fire) => fire());
			const g = globalThis as unknown as { ResizeObserver?: unknown };

			beforeEach(() => {
				frames = [];
				observers = [];
				raf = globalThis.requestAnimationFrame;
				globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
				previousRO = g.ResizeObserver;
				g.ResizeObserver = class {
					fire: () => void;
					constructor(cb: ResizeObserverCallback) {
						this.fire = () => cb([], this as unknown as ResizeObserver);
						observers.push(() => this.fire());
					}
					observe() {}
					unobserve() {}
					disconnect() { this.fire = () => {}; }
				};
			});
			afterEach(() => {
				globalThis.requestAnimationFrame = raf;
				if (previousRO) g.ResizeObserver = previousRO;
				else delete g.ResizeObserver;
				document.body.querySelectorAll('[data-test-scroller]').forEach((el) => el.remove());
			});

			async function mountOpen() {
				const column = document.createElement('div');
				column.dataset.testScroller = '';
				column.style.overflowY = 'hidden';
				const scroller = document.createElement('div');
				scroller.style.overflowY = 'auto';
				column.appendChild(scroller);
				document.body.appendChild(column);
				const view = render(WorkLogGroup, { props: { ...logOf(true, twoRuns), folds: createFoldState(memoryStorage()) }, target: scroller });
				for (const button of screen.getAllByRole('button', { name: /^Show work details/ })) await fireEvent.click(button);
				flush();
				const heads = [...view.container.querySelectorAll<HTMLElement>('[data-run-header]')];
				const ends = [...view.container.querySelectorAll<HTMLElement>('[data-run-end]')];
				rect(scroller, 0, 600);
				return { view, scroller, column, heads, ends };
			}

			it('re-places a pushed-off header when its height changes at a breakpoint', async () => {
				const { heads, ends } = await mountOpen();
				let height = 32;
				Object.defineProperty(heads[0], 'offsetHeight', { get: () => height, configurable: true });
				rect(ends[0], 0, -200);
				resized();
				flush();
				expect(heads[0].style.transform).toBe('translateY(-32px)');

				// Phone breakpoint: the header grows to 44px. No scroll event.
				height = 44;
				resized();
				flush();
				expect(heads[0].style.transform).toBe('translateY(-44px)');
				expect(heads[0].hasAttribute('data-run-header-off')).toBe(true);
			});

			it('re-places the header when a row inside the run expands and moves the run end', async () => {
				const { heads, ends } = await mountOpen();
				Object.defineProperty(heads[0], 'offsetHeight', { value: 32, configurable: true });
				rect(ends[0], 0, 20);
				resized();
				flush();
				expect(heads[0].style.transform).toBe('translateY(-12px)');

				// The reader expands a row in the run: its end moves down, no scroll.
				rect(ends[0], 0, 240);
				await fireEvent.click(rowEl('read-a')!.querySelector('button')!);
				flush();
				expect(heads[0].style.transform).toBe('');

				// Folding it back up (a content resize) brings the push-off back.
				rect(ends[0], 0, 10);
				resized();
				flush();
				expect(heads[0].style.transform).toBe('translateY(-22px)');
			});

			it('lands a long later run, collapsed from its stuck header, where the header was', async () => {
				const { heads, scroller, column } = await mountOpen();
				expect(heads).toHaveLength(2);
				let scrollerTop = 2000;
				Object.defineProperty(scroller, 'scrollTop', { get: () => scrollerTop, set: (v: number) => { scrollerTop = v; }, configurable: true });
				Object.defineProperty(column, 'scrollTop', { get: () => 0, set: () => { throw new Error('chat column scrolled'); }, configurable: true });
				// Run 2's header is stuck at the scroller's top, deep inside its run.
				const head = heads[1];
				const index = head.dataset.runHeader!;
				rect(head, 0, 32);
				await fireEvent.click(head);
				// Collapsed, run 2's fold row sits 700px above the view at this scroll
				// position; its viewport top follows the scroller.
				const fold = document.querySelector<HTMLElement>(`[data-run-fold="${index}"]`)!;
				expect(fold).toBeTruthy();
				Object.defineProperty(fold, 'getBoundingClientRect', { value: () => ({ top: 1300 - scrollerTop }), configurable: true });
				expect(fold.getBoundingClientRect().top).toBe(-700);
				flush();
				expect(scrollerTop).toBe(1300);
				expect(fold.getBoundingClientRect().top).toBe(0);
				expect(column.scrollTop).toBe(0);
			});

			it('leaves the scroll alone when a run header that was not stuck collapses', async () => {
				const { heads, scroller } = await mountOpen();
				let scrollerTop = 500;
				Object.defineProperty(scroller, 'scrollTop', { get: () => scrollerTop, set: (v: number) => { scrollerTop = v; }, configurable: true });
				rect(heads[1], 240, 272);
				await fireEvent.click(heads[1]);
				flush();
				expect(scrollerTop).toBe(500);
			});
		});

		it('collapses an open live run from its header', async () => {
			render(WorkLogGroup, { ...logOf(true), folds: createFoldState(memoryStorage()) });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(shown('read-a')).toBe(true);
			await fireEvent.click(screen.getByRole('button', { name: 'Hide work details: Read 1 file' }));
			expect(rowEl('read-a')).toBeNull();
			expect(screen.getByRole('button', { name: 'Show work details: Read 1 file' })).toBeTruthy();
		});

		it('settles closed when no run was open', async () => {
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			await view.rerender(logOf(false));
			expect(rowEl('read-a')).toBeNull();
			expect(screen.getByRole('button', { expanded: false })).toBeTruthy();
			expect(folds.current).toEqual({});
		});

		it('carries an open run into the settled fold without a fold key too', async () => {
			const view = render(WorkLogGroup, { ...logOf(true), foldKey: null });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			await view.rerender({ ...logOf(false), foldKey: null });
			expect(shown('read-a')).toBe(true);
			expect(screen.getByRole('button', { name: 'Hide work details' })).toBeTruthy();
		});

		it('keeps an open run open when earlier prose finishes later and shifts the run ordinals', async () => {
			const running: ChatMessage = { id: 'prose-0', role: 'assistant', type: 'text', content: 'Found:\n- partial', streaming: true };
			const base = [turn[0], running, ...turn.slice(1, 3), { ...message, id: 'read-c', content: 'Read C' },
				{ id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage,
				{ ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true }];
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true, base), folds });
			// Runs: [read-a] ended by the finding, [read-c] ended by the worker.
			const labels = screen.getAllByRole('button', { name: /^Show work details/ });
			expect(labels).toHaveLength(2);
			await fireEvent.click(labels[1]);
			expect(shown('read-c')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-read-c..worker': true });

			// prose-0 finishes: a new boundary ahead of the open run renumbers the runs.
			const failed = base.map((m) => (m === running ? { ...m, streaming: false } : m));
			await view.rerender(logOf(true, failed));
			expect(shown('read-c')).toBe(true);
			expect(rowEl('read-a')).toBeNull();
		});

		it('keeps a settled turn with nothing to fold in its group: worker card identity and expansion survive', async () => {
			const quiet: ChatMessage[] = [
				{ id: 'q-prompt', role: 'user', content: 'Review it', turnId: 'turn-q' },
				{ id: 'q-worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-q' } as ChatMessage,
				{ id: 'q-answer', role: 'assistant', type: 'text', content: 'Reviewer is on it.' }
			];
			const itemsOf = (isStreaming: boolean, messages = quiet) =>
				buildTranscriptModel(messages, { isStreaming, groupingEnabled: true }).items;
			const folds = createFoldState(memoryStorage());
			const view = render(TranscriptItems, { items: itemsOf(true), folds });
			const worker = rowEl('q-worker')!;
			expect(worker).toBeTruthy();
			await fireEvent.click(worker.querySelector('button')!);
			expect(worker.dataset.expanded).toBe('true');
			const answer = rowEl('q-answer');

			const done = quiet.map((m) => (m.id === 'q-worker' ? { ...m, agentStatus: 'completed' as const } : m));
			const settled = itemsOf(false, done);
			expect(settled.map((item) => item.kind)).toEqual(['message', 'work_log']);
			await view.rerender({ items: settled, folds });
			expect(view.container.querySelector('section')!.dataset.workLogMode).toBe('settled');
			// Same nodes, same expansion, every row still on screen and no disclosure drawn.
			expect(rowEl('q-worker')).toBe(worker);
			expect(worker.dataset.expanded).toBe('true');
			expect(rowEl('q-answer')).toBe(answer);
			expect(['q-worker', 'q-answer'].map(shown)).toEqual([true, true]);
			expect(screen.queryByRole('button', { name: /work details/i })).toBeNull();
			expect(screen.queryByText('Show fewer log entries')).toBeNull();
		});

		// Review of #2297: the run behind finished prose A was keyed by A, so
		// when newer prose B finished and took over as the run's boundary, the
		// stored open state no longer matched and the open run folded shut.
		it('keeps an open run open when newer prose finishes and ends it instead, through a reload', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const prompt = turn[0];
			const read = { ...message, id: 'read-x', content: 'Read X' };
			const proseA: ChatMessage = { id: 'prose-a', role: 'assistant', type: 'text', content: 'Checking the config.' };
			const proseB: ChatMessage = { id: 'prose-b', role: 'assistant', type: 'text', content: 'Config checked.' };
			const view = render(WorkLogGroup, { ...logOf(true, [prompt, read, proseA]), folds });
			expect(shown('read-x')).toBe(false);
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(shown('read-x')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-read-x..prose-a': true });

			// B streams: A still ends the run.
			await view.rerender(logOf(true, [prompt, read, proseA, { ...proseB, streaming: true }]));
			expect(shown('read-x')).toBe(true);
			// B finishes: the run now runs through A to B, same key, still open.
			await view.rerender(logOf(true, [prompt, read, proseA, proseB]));
			expect(shown('read-x')).toBe(true);
			expect(shown('prose-a')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-read-x..prose-b': true });
			view.unmount();

			// A remount or reload reads the same stored state.
			render(WorkLogGroup, { ...logOf(true, [prompt, read, proseA, proseB]), folds: createFoldState(storage) });
			expect(shown('read-x')).toBe(true);
			expect(screen.getByRole('button', { name: 'Hide work details: Read 1 file' })).toBeTruthy();
		});

		// Re-review of #2297: runs merge when newer prose replaces the prose
		// between them as the latest word. Opening the later run must keep the
		// merged run open, live and after a remount on the stored state.
		it('keeps an open run open when newer prose merges it into the run before it, through a reload', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const prompt = turn[0];
			const readA = { ...message, id: 'read-a', content: 'Read A' };
			const proseA: ChatMessage = { id: 'prose-a', role: 'assistant', type: 'text', content: 'Checking the config.' };
			const readB = { ...message, id: 'read-b', content: 'Read B' };
			const worker = { id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage;
			const proseB: ChatMessage = { id: 'prose-b', role: 'assistant', type: 'text', content: 'Reviewer is on it.' };
			const before = [prompt, readA, proseA, readB, worker];
			const view = render(WorkLogGroup, { ...logOf(true, before), folds });
			const labels = screen.getAllByRole('button', { name: /^Show work details/ });
			expect(labels).toHaveLength(2);
			await fireEvent.click(labels[1]);
			expect(shown('read-b')).toBe(true);

			await view.rerender(logOf(true, [...before, { ...proseB, streaming: true }]));
			expect(shown('read-b')).toBe(true);
			// B finishes: A no longer ends a run, so the two runs merge.
			await view.rerender(logOf(true, [...before, proseB]));
			expect(screen.getAllByRole('button', { name: /^Hide work details:/ })).toHaveLength(1);
			expect(shown('read-b')).toBe(true);
			view.unmount();

			render(WorkLogGroup, { ...logOf(true, [...before, proseB]), folds: createFoldState(storage) });
			expect(shown('read-b')).toBe(true);
		});

		// Re-review of #2297: a run splits when prose inside it finishes and
		// becomes a boundary. Both parts of a run the reader opened stay open.
		it('keeps both parts of an open run open when prose inside it finishes and splits it, through a reload', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const prompt = turn[0];
			const readX = { ...message, id: 'read-x', content: 'Read X' };
			const finding: ChatMessage = { id: 'finding-p', role: 'assistant', type: 'text', content: 'Found:\n- a shared token', streaming: true };
			const readY = { ...message, id: 'read-y', content: 'Read Y' };
			const worker = { id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage;
			const view = render(WorkLogGroup, { ...logOf(true, [prompt, readX, finding, readY, worker]), folds });
			await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
			expect(['read-x', 'read-y'].map(shown)).toEqual([true, true]);

			const split = [prompt, readX, { ...finding, streaming: false }, readY, worker];
			await view.rerender(logOf(true, split));
			expect(screen.getAllByRole('button', { name: /^Hide work details:/ })).toHaveLength(2);
			expect(['read-x', 'read-y'].map(shown)).toEqual([true, true]);
			view.unmount();

			render(WorkLogGroup, { ...logOf(true, split), folds: createFoldState(storage) });
			expect(['read-x', 'read-y'].map(shown)).toEqual([true, true]);
		});

		// Re-review of #2297: closing a run cleared only its current identities,
		// so one it carried earlier (the prose that ended it before newer prose
		// took over) stayed stored open and reopened the work log at settle.
		it('keeps a run closed through settle and a reload after its boundary moved', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const prompt = turn[0];
			const read = { ...message, id: 'read-x', content: 'Read X' };
			const proseA: ChatMessage = { id: 'prose-a', role: 'assistant', type: 'text', content: 'Checking the config.' };
			const proseB: ChatMessage = { id: 'prose-b', role: 'assistant', type: 'text', content: 'Config checked.' };
			const view = render(WorkLogGroup, { ...logOf(true, [prompt, read, proseA]), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(folds.current).toEqual({ 'prompt:run-read-x..prose-a': true });

			// B finishes and takes over the boundary: the run keeps its key.
			await view.rerender(logOf(true, [prompt, read, proseA, proseB]));
			expect(shown('read-x')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-read-x..prose-b': true });

			await fireEvent.click(screen.getByRole('button', { name: 'Hide work details: Read 1 file' }));
			expect(shown('read-x')).toBe(false);
			expect(folds.current).toEqual({});

			// Settled, the reader's closed log stays closed, and so after a reload.
			await view.rerender(logOf(false, [prompt, read, proseA, proseB]));
			expect(shown('read-x')).toBe(false);
			expect(folds.isOpen('prompt:turn')).toBe(false);
			view.unmount();
			render(WorkLogGroup, { ...logOf(false, [prompt, read, proseA, proseB]), folds: createFoldState(storage) });
			expect(shown('read-x')).toBe(false);
			expect(screen.getByRole('button', { expanded: false })).toBeTruthy();
		});

		// Re-review of #2297: storing a run under every identity it carried
		// overflowed the fold store's cap on a long run and never converged.
		// One entry per run: opening a 400-prose run is one bounded write.
		it('opens and keeps open a run of 800+ rows with a bounded number of storage writes', async () => {
			const base = memoryStorage();
			let writes = 0;
			const counting = { ...base, get length() { return base.length; },
				setItem: (k: string, v: string) => { writes++; base.setItem(k, v); } } as Storage;
			const folds = createFoldState(counting);
			const prompt = turn[0];
			const rows: ChatMessage[] = [{ ...message, id: 'read-0', content: 'Read 0' }];
			for (let i = 1; i <= 400; i++) {
				rows.push({ id: `prose-${i}`, role: 'assistant', type: 'text', content: `Step ${i}.` });
				rows.push({ ...message, id: `read-${i}`, content: `Read ${i}` });
			}
			const last: ChatMessage = { id: 'prose-last', role: 'assistant', type: 'text', content: 'Done.' };
			const view = render(WorkLogGroup, { ...logOf(true, [prompt, ...rows, last]), folds });
			const before = writes;
			await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
			expect(shown('read-400')).toBe(true);
			expect(writes - before).toBeGreaterThanOrEqual(1);
			expect(writes - before).toBeLessThanOrEqual(2);
			expect(folds.current).toEqual({ 'prompt:run-read-0..prose-last': true });
			// Newer prose finishing keeps the one entry and writes nothing.
			const after = writes;
			await view.rerender(logOf(true, [prompt, ...rows, last, { ...message, id: 'read-z', content: 'Read Z' },
				{ id: 'prose-z', role: 'assistant', type: 'text', content: 'Really done.' } as ChatMessage]));
			expect(shown('read-400')).toBe(true);
			expect(writes - after).toBeLessThanOrEqual(2);
			expect(folds.current).toEqual({ 'prompt:run-read-0..prose-z': true });
		});

		// Re-review of #2297: a split that happens while the group is unmounted
		// (a session switch mid-turn) must still open the later part on remount.
		it('keeps both parts of an open run open when it splits while the group is unmounted', async () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			const prompt = turn[0];
			const readA = { ...message, id: 'read-a', content: 'Read A' };
			const finding: ChatMessage = { id: 'finding-p', role: 'assistant', type: 'text', content: 'Found:\n- a shared token', streaming: true };
			const readB = { ...message, id: 'read-b', content: 'Read B' };
			const worker = { id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage;
			const view = render(WorkLogGroup, { ...logOf(true, [prompt, readA, finding, readB, worker]), folds });
			await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
			expect(['read-a', 'read-b'].map(shown)).toEqual([true, true]);
			view.unmount();

			// The finding completes while the reader is elsewhere: the run splits.
			const split = [prompt, readA, { ...finding, streaming: false }, readB, worker];
			const again = render(WorkLogGroup, { ...logOf(true, split), folds });
			expect(screen.getAllByRole('button', { name: /^Hide work details:/ })).toHaveLength(2);
			expect(['read-a', 'read-b'].map(shown)).toEqual([true, true]);
			expect(folds.current).toEqual({ 'prompt:run-read-a..finding-p': true, 'prompt:run-read-b..worker': true });
			again.unmount();

			// Settling drops the memory; the open runs become the open turn fold.
			const settled = render(WorkLogGroup, { ...logOf(false, split), folds });
			expect(folds.current).toEqual({ 'prompt:turn': true });
			settled.unmount();
		});

		// Round five of #2297: open state is decided from the stored ranges and
		// the current runs alone, so every path into a split reads the same.
		describe('runs stored as ranges', () => {
			const prompt = turn[0];
			const readA = { ...message, id: 'read-a', content: 'Read A' };
			const finding: ChatMessage = { id: 'finding-p', role: 'assistant', type: 'text', content: 'Found:\n- a shared token', streaming: true };
			const readB = { ...message, id: 'read-b', content: 'Read B' };
			const worker = { id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage;
			const whole = (first = prompt) => [first, readA, finding, readB, worker];
			const split = (first = prompt) => [first, readA, { ...finding, streaming: false }, readB, worker];

			it('keeps both parts open after a reload when the run split while away', async () => {
				const storage = memoryStorage();
				const view = render(WorkLogGroup, { ...logOf(true, whole()), folds: createFoldState(storage) });
				await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
				view.unmount();

				// A reload: nothing but the persisted store survives.
				render(WorkLogGroup, { ...logOf(true, split()), folds: createFoldState(storage) });
				expect(screen.getAllByRole('button', { name: /^Hide work details:/ })).toHaveLength(2);
				expect(['read-a', 'read-b'].map(shown)).toEqual([true, true]);
			});

			it('keeps both parts open when the turn gets its server id, remounts and splits', async () => {
				const storage = memoryStorage();
				const folds = createFoldState(storage);
				const view = render(WorkLogGroup, { ...logOf(true, whole()), folds });
				await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
				expect(Object.keys(folds.current)).toEqual(['prompt:run-read-a..worker']);
				view.unmount();

				const reconciled = { ...prompt, serverId: 'srv-1' };
				render(WorkLogGroup, { ...logOf(true, split(reconciled)), folds });
				expect(['read-a', 'read-b'].map(shown)).toEqual([true, true]);
				expect(folds.current).toEqual({ 'srv-1:run-read-a..finding-p': true, 'srv-1:run-read-b..worker': true });
			});

			it('opens both parts when the run is opened while a split is queued', async () => {
				const folds = createFoldState(memoryStorage());
				const view = render(QueuedWorkLog, { initial: { ...logOf(true, whole()), folds } });
				const label = screen.getByRole('button', { name: /^Show work details/ });
				// The finding completes, and the click lands before Svelte flushes it.
				view.component.queue({ ...logOf(true, split()), folds });
				label.dispatchEvent(new MouseEvent('click', { bubbles: true }));
				await tick();
				expect(screen.getAllByRole('button', { name: /^Hide work details:/ })).toHaveLength(2);
				expect(['read-a', 'read-b'].map(shown)).toEqual([true, true]);
			});

			// Review of #2297: the reducer acks an optimistic steer by keeping its
			// `id` and adding a `serverId`, and rows are keyed by `serverId` first.
			// A range opened before the ack must still name the run after it.
			it('keeps a run that starts at a steer open through the steer ack, closes it, and settles closed', async () => {
				const folds = createFoldState(memoryStorage());
				const steer: ChatMessage = { id: 'steer-opt', role: 'user', content: 'Check the tests too', turnId: 'turn-1' };
				const readS = { ...message, id: 'read-s', content: 'Read S' };
				const proseS: ChatMessage = { id: 'prose-s', role: 'assistant', type: 'text', content: 'Tests checked.' };
				const tail = { ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true };
				const live = (first: ChatMessage) => [prompt, readA, { ...finding, streaming: false }, first, readS, proseS, tail];
				const view = render(WorkLogGroup, { ...logOf(true, live(steer)), folds });
				const labels = screen.getAllByRole('button', { name: /^Show work details/ });
				expect(labels).toHaveLength(2);
				await fireEvent.click(labels[1]);
				expect(shown('read-s')).toBe(true);
				expect(folds.current).toEqual({ 'prompt:run-steer-opt..prose-s': true });

				// The ack: same row, now with its server id.
				const acked = live({ ...steer, serverId: 'srv-steer' });
				await view.rerender(logOf(true, acked));
				expect(shown('read-s')).toBe(true);
				expect(folds.current).toEqual({ 'prompt:run-srv-steer..prose-s': true });

				await fireEvent.click(screen.getByRole('button', { name: /^Hide work details:/ }));
				expect(shown('read-s')).toBe(false);
				expect(folds.current).toEqual({});

				const settled = acked.filter((m) => m.id !== 'tail');
				await view.rerender(logOf(false, settled));
				expect(folds.isOpen('prompt:turn')).toBe(false);
				expect(shown('read-s')).toBe(false);
			});

			// Review of #2297: the steer is acked while the group is unmounted and
			// the page reloads from server history, where the row has only its
			// server id. The range opened under the optimistic id must still open
			// the run, close with it, and never reopen the log at settle.
			it('keeps a steer-started run open after an ack while away and a reload from history, and settles closed after closing it', async () => {
				const storage = memoryStorage();
				const steer: ChatMessage = { id: 'steer-opt', role: 'user', content: 'Check the tests too', turnId: 'turn-1' };
				const readS = { ...message, id: 'read-s', content: 'Read S' };
				const proseS: ChatMessage = { id: 'prose-s', role: 'assistant', type: 'text', content: 'Tests checked.' };
				const tail = { ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true };
				const live = (first: ChatMessage) => [prompt, readA, { ...finding, streaming: false }, first, readS, proseS, tail];
				const first = render(WorkLogGroup, { ...logOf(true, live(steer)), folds: createFoldState(storage) });
				await fireEvent.click(screen.getAllByRole('button', { name: /^Show work details/ })[1]);
				expect(shown('read-s')).toBe(true);
				first.unmount();

				// Reloaded from server history: the steer row is only its server id.
				const history = live({ ...steer, id: 'srv-steer' });
				const folds = createFoldState(storage);
				expect(folds.current).toEqual({ 'prompt:run-steer-opt..prose-s': true });
				const view = render(WorkLogGroup, { ...logOf(true, history), folds });
				expect(shown('read-s')).toBe(true);
				expect(folds.current).toEqual({ 'prompt:run-srv-steer..prose-s': true });

				await fireEvent.click(screen.getByRole('button', { name: /^Hide work details:/ }));
				expect(shown('read-s')).toBe(false);
				expect(folds.current).toEqual({});

				await view.rerender(logOf(false, history.filter((m) => m.id !== 'tail')));
				expect(folds.isOpen('prompt:turn')).toBe(false);
				expect(shown('read-s')).toBe(false);
			});

			it('drops a run range that names no row of the settled turn instead of opening the log', () => {
				const folds = createFoldState(memoryStorage());
				folds.setOpen('prompt:run-gone..also-gone', true);
				render(WorkLogGroup, { ...logOf(false, split()), folds });
				expect(folds.current).toEqual({});
				expect(shown('read-a')).toBe(false);
			});

			it('does not open a run of another session whose turn has the same key', async () => {
				const folds = createFoldState(memoryStorage());
				const first = render(WorkLogGroup, { ...logOf(true, whole()), folds });
				await fireEvent.click(screen.getByRole('button', { name: /^Show work details/ }));
				first.unmount();

				const other = [prompt, { ...readA, id: 'b-read-a' }, { ...finding, id: 'b-finding' }, { ...readB, id: 'b-read-b' }, { ...worker, id: 'b-worker' }];
				render(WorkLogGroup, { ...logOf(true, other), folds });
				expect(['b-read-a', 'b-read-b'].map(shown)).toEqual([false, false]);
				// The other session's open run is not this group's to rewrite.
				expect(folds.current).toEqual({ 'prompt:run-read-a..worker': true });
			});
		});

		// A merge that happened while the group was unmounted: the stored key
		// of the absorbed run names a row of the merged run, so it moves there.
		it('moves a stored run onto the run that absorbed it while unmounted', () => {
			const storage = memoryStorage();
			const folds = createFoldState(storage);
			folds.setOpen('prompt:run-read-b', true);
			const prompt = turn[0];
			const merged: ChatMessage[] = [prompt, { ...message, id: 'read-a', content: 'Read A' },
				{ id: 'prose-a', role: 'assistant', type: 'text', content: 'Checking the config.' },
				{ ...message, id: 'read-b', content: 'Read B' },
				{ id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage,
				{ id: 'prose-b', role: 'assistant', type: 'text', content: 'Reviewer is on it.' }];
			render(WorkLogGroup, { ...logOf(true, merged), folds });
			expect(shown('read-b')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-read-a..worker': true });
		});

		it('moves folds opened under an optimistic id onto the server id', async () => {
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(folds.isOpen('prompt:run-read-a..finding')).toBe(true);

			// The reducer reconciles the optimistic prompt in place and records its server id.
			const reconciled = [{ ...turn[0], serverId: 'srv-1' }, ...turn.slice(1)];
			await view.rerender(logOf(true, reconciled));
			expect(folds.current).toEqual({ 'srv-1:run-read-a..finding': true });
			expect(shown('read-a')).toBe(true);
		});
	});
});
