import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import type { ChatMessage } from '$lib/stores/types';
import WorkLogGroup from './WorkLogGroup.svelte';
import TranscriptItems from './test-fixtures/TranscriptItems.svelte';
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
			expect(folds.current).toEqual({ 'prompt:run-finding': true });
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

		it('keeps an open run open when a mid-run tool fails later and shifts the run ordinals', async () => {
			const running = { ...message, id: 'read-0', content: 'Read 0', toolStatus: 'running' as const };
			const base = [turn[0], running, ...turn.slice(1, 3), { ...message, id: 'read-c', content: 'Read C' },
				{ id: 'worker', type: 'subagent', role: 'assistant', agentName: 'Reviewer', agentStatus: 'running', toolId: 'task-1' } as ChatMessage,
				{ ...message, id: 'tail', content: 'Tail', toolStatus: 'running' as const, streaming: true }];
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true, base), folds });
			// Runs: [read-0, read-a] ended by the finding, [read-c] ended by the worker.
			const labels = screen.getAllByRole('button', { name: /^Show work details/ });
			expect(labels).toHaveLength(2);
			await fireEvent.click(labels[1]);
			expect(shown('read-c')).toBe(true);
			expect(folds.current).toEqual({ 'prompt:run-worker': true });

			// read-0 fails: a new boundary ahead of the open run renumbers the runs.
			const failed = base.map((m) => (m === running ? { ...m, toolStatus: 'error' as const } : m));
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

		it('moves folds opened under an optimistic id onto the server id', async () => {
			const folds = createFoldState(memoryStorage());
			const view = render(WorkLogGroup, { ...logOf(true), folds });
			await fireEvent.click(screen.getByRole('button', { name: 'Show work details: Read 1 file' }));
			expect(folds.isOpen('prompt:run-finding')).toBe(true);

			// The reducer reconciles the optimistic prompt in place and records its server id.
			const reconciled = [{ ...turn[0], serverId: 'srv-1' }, ...turn.slice(1)];
			await view.rerender(logOf(true, reconciled));
			expect(folds.current).toEqual({ 'srv-1:run-finding': true });
			expect(shown('read-a')).toBe(true);
		});
	});
});
