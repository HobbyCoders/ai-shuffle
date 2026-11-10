/** Execute MessageArea's actual pre-render transaction with controlled ticks.
 * jsdom has no scroll layout; geometry alone is supplied by the probe. */
import { describe, expect, it, vi } from 'vitest';
import { createMessageSender, type MessageSenderDeps } from '$lib/stores/session/message-sender';
import type { ActiveSessionState } from '$lib/stores/active-session';
import { pendingQueue } from '$lib/stores/session/pending-queue';
import { isUserScrollUpGesture } from '$lib/stores/session/auto-scroll';
import { createTranscriptWindow } from './transcript-window';
import { landedFromQueue } from '$lib/utils/queue-handoff';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Only transport/voice edges are stubbed: admission and all four echo branches
// execute createMessageSender itself, then feed its emitted store snapshots into
// the production pre-render transaction and rendered tail style binding below.
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { current: { sessions: [] } } }));
vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: () => false, sendVoiceText: vi.fn() }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(), voiceContextFor: () => ({}) }));
vi.mock('$lib/api/git', () => ({ reopenWorktree: vi.fn(), generateBranchName: vi.fn(), generateBranchNameLocal: () => 'auto/test' }));
vi.mock('$lib/api/client', () => ({ api: { post: vi.fn(), get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const slotStyle = source.match(/style:min-height=\{(`[^`]+`)\}/)?.[1];
if (!slotStyle) throw new Error('Missing rendered tail slot binding');
const renderSlotStyle = new Function('tailQueueSlotPx', `return ${slotStyle}`);
function between(start: string, end: string) {
	const from = source.indexOf(start), to = source.indexOf(end, from);
	if (from < 0 || to <= from) throw new Error(`Missing floor transaction: ${start}`);
	return source.slice(from, to);
}
const transaction = between("let layoutBoundary = '';", '// Rendered geometry has one lifecycle').replace(': QueueHandoffMark | null', '');
const reserve = between('function readingFloorReserve()', 'async function maintainCurrentTurnAnchor').replace('(): number', '()');
const beginShift = between('windowShifting = true;', 'mountedRange = next;');
const beginTurn = between('function startCurrentTurnAnchor(', '/**\n\t * Where a stopped turn').replace('messageId: string', 'messageId');

type Session = Pick<ActiveSessionState, 'sessionId' | 'messages' | 'isStreaming' | 'liveTurnId' | 'error' | 'queuedMessages' | 'localSendIntent'>;
const session = (id: string, count = 2): Session => ({ sessionId: id, messages: Array(count).fill({}), isStreaming: false, liveTurnId: null, error: null, queuedMessages: [] });

function probe() {
	// Render the real style binding into an actual slot element. jsdom supplies
	// no layout, so its measured queue height remains explicitly controlled.
	const slot = document.createElement('div');
	slot.innerHTML = '<div class="tail-controls"></div>';
	const tail = slot.firstElementChild as HTMLElement;
	const env = {
		session: session('long'), scrollTop: 5000, proseEnd: 200,
		follows: true, pendingHeight: 0, gap: 18, calls: [] as string[], ticks: [] as (() => void)[],
		landedFromQueue
	};
	const create = new Function('env', `
		let $activeSession = env.session, lastKnownScrollTop = env.scrollTop;
		let followedViewportEnd = 0, followedFloorContext = null;
		let presentedTailError = null, tailErrorOffsetPx = 0, tailQueueSlotPx = 0;
		let windowShifting = false, windowShiftVersion = 0;
		let isMounted = true, shouldAutoScroll = true, programmaticScrollDepth = 0;
		let currentTurnAnchorId = 'anchor', cleanup, showJumpToLatest = false;
		const clearJumpRevealTimer = () => {};
		const containerRef = { clientHeight: 600, scrollTop: 0, getBoundingClientRect: () => ({ top: 0 }) };
		const tailSpacerRef = { getBoundingClientRect: () => ({ top: env.proseEnd }) };
		const tailControlsRef = { getBoundingClientRect: () => ({ height: env.pendingHeight }) }, chatColumnRef = {};
		const getComputedStyle = () => ({ rowGap: String(env.gap) });
		const activeSession = {}, get = () => env.session;
		const liveActiveSession = { clearError: () => { env.session = { ...env.session, error: null }; } };
		const untrack = fn => fn();
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const requestAnimationFrame = fn => { fn(); return 0; };
		const maintainCurrentTurnAnchor = () => env.calls.push('anchor');
		const followGrowthNow = () => env.calls.push('follow');
		const landedFromQueue = env.landedFromQueue;
		let effect;
		const $effect = { pre: fn => { effect = fn; } };
		${reserve}
		${transaction}
		${beginTurn}
		return {
			start: fromQueue => startCurrentTurnAnchor('next', fromQueue),
			dismiss: () => dismissTailError(),
			run() {
				cleanup?.();
				$activeSession = env.session;
				lastKnownScrollTop = env.scrollTop;
				shouldAutoScroll = env.follows;
				cleanup = effect();
			},
			shift() { windowShiftVersion++; ${beginShift} },
			endShift() { windowShifting = false; },
			floor: () => followedViewportEnd,
			reserve: () => readingFloorReserve(),
			queueSlot: () => tailQueueSlotPx,
			errorOffset: () => tailErrorOffsetPx,
			presentedError: () => presentedTailError,
			depth: () => programmaticScrollDepth
		};
	`);
	const state = create(env) as { dismiss(): void; start(fromQueue: boolean): void; run(): void; shift(): void; endShift(): void; floor(): number; reserve(): number; queueSlot(): number; errorOffset(): number; presentedError(): string | null; depth(): number };
	return { env, ...state, render() { state.run(); tail.style.minHeight = renderSlotStyle(state.queueSlot()); return tail; }, async flush() { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); } };
}

/** The real sender owns intent publication; this adapter only records its updates. */
function senderSnapshots(initial: Session, authoritative = false, switchOnConnection?: Session) {
	const asActive = (value: Session) => ({ title: 'Existing chat', profile: 'claude-code', project: 'proj-1',
		worktreeIntent: 'local', pendingPeerMessages: [], ...value } as unknown as ActiveSessionState);
	let state = asActive(initial);
	const snapshots: ActiveSessionState[] = [];
	const deps = {
		getState: () => state,
		update: (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); snapshots.push(state); },
		internal: { pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
			stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null },
		wsConn: { isConnected: () => {
			if (switchOnConnection) { state = asActive(switchOnConnection); switchOnConnection = undefined; }
			return true;
		}, connect: vi.fn(), send: () => true, resetReconnectAttempts: vi.fn() },
		sessionEphemeralCache: { remove: vi.fn() }, appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(), stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => ({ sendMessage: vi.fn(), createWorktree: vi.fn(), autoCreateWorktree: vi.fn() })
	} as unknown as MessageSenderDeps;
	const sender = createMessageSender(deps);
	return {
		getState: () => state,
		async send(skipMessageAppend = false) {
			const start = snapshots.length;
			const authority = vi.spyOn(pendingQueue, 'authoritySpokeSince').mockReturnValue(authoritative);
			try { expect(await sender.sendMessage('fresh local send', { skipMessageAppend })).toBe(true); }
			finally { authority.mockRestore(); }
			return snapshots.slice(start);
		}
	};
}

function presentedSlot() {
	const p = probe(); p.render();
	p.env.pendingHeight = 164.9375;
	p.env.session = { ...session('long', 18), error: 'CLI error', isStreaming: true };
	expect(p.render().style.minHeight).toBe('164.9375px');
	return p;
}

function captureLongFloor(p: ReturnType<typeof probe>) {
	p.run();
	p.env.session = session('long', 3);
	p.run();
	expect(p.floor()).toBe(5600);
}

describe('MessageArea followed-floor coordinate ownership', () => {
	it.each([
		{ name: 'optimistic queue', streaming: true, skip: false, authoritative: false },
		{ name: 'authoritative queue without local echo', streaming: true, skip: false, authoritative: true },
		{ name: 'pre-appended message', streaming: false, skip: true, authoritative: false },
		{ name: 'transcript append', streaming: false, skip: false, authoritative: false }
	])('renders the released slot from real sender snapshots: $name', async ({ streaming, skip, authoritative }) => {
		const p = presentedSlot();
		// Automatic queued handoff (including clearing error) is not fresh intent.
		p.env.pendingHeight = 86.71875;
		p.env.session = { ...p.env.session, error: null, isStreaming: streaming };
		p.start(true);
		expect(p.render().style.minHeight).toBe('164.9375px');
		const producer = senderSnapshots(p.env.session, authoritative);
		const snapshots = await producer.send(skip);
		expect(snapshots.length).toBeGreaterThan(0);
		for (const snapshot of snapshots.filter(s => s.error === null)) {
			p.env.session = snapshot;
			expect(p.render().style.minHeight).toBe('0px');
			expect(p.errorOffset()).toBe(0);
			expect(p.presentedError()).toBeNull();
		}
		if (streaming || skip) expect(producer.getState().messages).toBe(snapshots[0].messages);
		// Later fromQueue delivery and Stop must not reintroduce the old slot.
		p.env.pendingHeight = 0;
		p.env.session = { ...producer.getState(), queuedMessages: [], isStreaming: false };
		p.start(true);
		expect(p.render().style.minHeight).toBe('0px');
	});

	it('ignores a stale real-send revision after a newer error was presented', async () => {
		const p = presentedSlot();
		const producer = senderSnapshots(p.env.session);
		const first = (await producer.send()).at(-1)!;
		const second = (await producer.send()).at(-1)!;
		p.env.session = second; p.render();
		p.env.pendingHeight = 86.71875;
		p.env.session = { ...second, error: 'newer error' };
		expect(p.render().style.minHeight).toBe('86.71875px');
		p.env.session = first;
		expect(p.render().style.minHeight).toBe('86.71875px');
		expect(p.presentedError()).toBe('newer error');
	});

	it('ignores real intent captured for another session when the view changed during send', async () => {
		const p = presentedSlot();
		const producer = senderSnapshots({ ...p.env.session, sessionId: 'other' }, false, p.env.session);
		const snapshots = await producer.send();
		const emitted = snapshots.at(-1)!;
		expect(emitted.sessionId).toBe('long');
		expect(emitted.localSendIntent?.sessionId).toBe('other');
		p.env.session = emitted;
		expect(p.render().style.minHeight).toBe('164.9375px');
		expect(p.presentedError()).toBe('CLI error');
	});

	it('retains the rendered slot through automatic error clearing, queue drain and Stop without fresh intent', () => {
		const p = presentedSlot();
		p.env.pendingHeight = 0;
		p.env.session = { ...p.env.session, error: null, queuedMessages: [], isStreaming: false };
		p.start(true);
		expect(p.render().style.minHeight).toBe('164.9375px');
		expect(p.presentedError()).toBe('CLI error');
	});


	it('holds the already-presented queue slot through error dequeue and transcript handoff', () => {
		const p = probe();
		p.run();
		p.env.pendingHeight = 164.9375;
		p.env.session = { ...session('long', 18), error: 'CLI error' };
		p.run();
		expect(p.queueSlot()).toBe(164.9375);
		expect(p.errorOffset()).toBe(182.9375);
		// The real producer removes one pending row, then delivers a user row.
		// Neither commit may resize the slot beneath an already-read error.
		p.env.pendingHeight = 86.71875;
		p.env.session = { ...session('long', 19), error: 'enriched CLI error' };
		p.run();
		expect(p.queueSlot()).toBe(164.9375);
		expect(p.errorOffset()).toBe(182.9375);
		const minHeight = source.match(/style:min-height=\{(`[^`]+`)\}/)?.[1] ?? '`0px`';
		const slotHeight = parseFloat(new Function('tailQueueSlotPx', `return ${minHeight}`)(p.queueSlot()));
		expect(662 - Math.max(p.env.pendingHeight, slotHeight)).toBe(497.0625);
		p.env.pendingHeight = 0;
		p.env.session = { ...session('long', 21), error: 'enriched CLI error' };
		p.run();
		expect(p.queueSlot()).toBe(164.9375);
	});

	it.each([false, true])('a fresh send releases the slot, but queued handoff retains it (fromQueue=%s)', fromQueue => {
		const p = probe(); p.run();
		p.env.pendingHeight = 164.9375;
		p.env.session = { ...session('long', 18), error: 'CLI error' }; p.run();
		p.start(fromQueue);
		expect(p.queueSlot()).toBe(fromQueue ? 164.9375 : 0);
	});

	it('releases both slots when the reader dismisses the error', () => {
		const p = probe(); p.run();
		p.env.pendingHeight = 164.9375;
		p.env.session = { ...session('long', 18), error: 'CLI error' }; p.run();
		p.dismiss();
		expect(p.queueSlot()).toBe(0);
		expect(p.errorOffset()).toBe(0);
		expect(p.env.session.error).toBeNull();
		expect(source).toContain('onclick={dismissTailError}');
	});

	it.each(['switch', 'clear'] as const)('releases the error queue slot on %s', action => {
		const p = probe(); p.run();
		p.env.pendingHeight = 164.9375;
		p.env.session = { ...session('long', 18), error: 'CLI error' }; p.run();
		p.env.session = action === 'switch' ? session('other') : session('long', 0);
		p.run();
		expect(p.queueSlot()).toBe(0);
	});

	it('bottom-aligns the queue inside its retained slot, without hiding or transforming it', () => {
		expect(source).toContain('style:min-height={`${tailQueueSlotPx}px`}');
		const css = source.slice(source.indexOf('\n\t.tail-controls {'));
		expect(css.slice(0, css.indexOf('}'))).toContain('justify-content: flex-end;');
		const nested = source.slice(source.indexOf('.tail-controls :global(.pending-messages) {'));
		expect(nested.slice(0, nested.indexOf('}'))).toContain('min-height: inherit;');
		expect(nested.slice(0, nested.indexOf('}'))).toContain('justify-content: flex-end;');
	});

	it('does not carry the outgoing long scroll offset into a short completed chat', () => {
		const p = probe();
		captureLongFloor(p);
		p.env.session = session('short', 1);
		p.run();
		expect(p.floor()).toBe(0);
		expect(p.reserve()).toBe(0);
	});

	it('clears the floor without recapturing old coordinates when the transcript is cleared', () => {
		const p = probe();
		captureLongFloor(p);
		p.env.session = session('long', 0);
		p.run();
		expect(p.floor()).toBe(0);
		expect(p.reserve()).toBe(0);
	});

	it('drops the floor and pending layout work when bounded eviction changes coordinates', async () => {
		const p = probe();
		captureLongFloor(p);
		p.shift();
		p.endShift();
		await p.flush();
		expect(p.floor()).toBe(0);
		expect(p.env.calls).toEqual([]);
		expect(p.depth()).toBe(0);
	});

	it('rejects a deferred transaction even after a rapid switch back to the same session', async () => {
		const p = probe();
		captureLongFloor(p);
		p.env.session = session('short', 1); p.run();
		p.env.session = session('long', 3); p.run();
		await p.flush();
		expect(p.env.calls).toEqual([]);
		expect(p.floor()).toBe(0);
		expect(p.depth()).toBe(0);
	});

	it('keeps pending same-context follow work through consecutive queue or terminal boundaries', async () => {
		const p = probe();
		captureLongFloor(p);
		p.env.session = session('long', 4); p.run();
		await p.flush();
		expect(p.env.calls).toEqual(['anchor', 'follow', 'anchor', 'follow']);
	});

	it('rejects every pending callback across a switch, not just the latest one', async () => {
		const p = probe();
		captureLongFloor(p);
		p.env.session = session('long', 4); p.run();
		p.env.session = session('short', 1); p.run();
		p.env.session = session('long', 4); p.run();
		await p.flush();
		expect(p.env.calls).toEqual([]);
	});

	it('still protects same-context terminal growth and never captures a scrolled-up reader', async () => {
		const p = probe();
		captureLongFloor(p);
		await p.flush();
		expect(p.env.calls).toEqual(['anchor', 'follow']);
		p.env.follows = false;
		p.env.session = session('long', 4); p.run();
		await p.flush();
		expect(p.env.calls).toEqual(['anchor', 'follow']);
	});
});

const anchorMaintenance = between('async function maintainCurrentTurnAnchor(', 'function followGrowthNow()')
	.replace('beforeShrink?: number', 'beforeShrink');
function maintenanceAcrossShift() {
	const root = document.createElement('div');
	root.innerHTML = '<div id="maintained-user"></div>';
	document.body.appendChild(root);
	const env = { top: 5000, writes: [] as number[], ticks: [] as (() => void)[] };
	const result = new Function('env', 'root', `
		let containerRef = root, tailSpacerRef = {}, currentTurnAnchorId = 'user';
		let windowShiftVersion = 0, windowShifting = false, tailReservePx = 0, shouldAutoScroll = true;
		Object.defineProperty(root, 'scrollTop', { get: () => env.top });
		Object.defineProperty(root, 'clientHeight', { value: 600 });
		root.getBoundingClientRect = () => ({ top: 52 });
		root.firstElementChild.getBoundingClientRect = () => ({ top: 5152 - env.top });
		const turnAnchorId = () => 'maintained-user', transcriptContentEnd = () => 5300;
		const readingFloorReserve = () => 0, resolveAnchoredTailReserve = () => 300, tailControlsExtent = () => 0;
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const resolveCurrentTurnAnchorScrollTop = top => top, resolveAnchoredRevealDelta = () => 0;
		const setProgrammaticScrollTop = top => { env.top = top; env.writes.push(top); };
		const updateJumpToLatestVisibility = () => {}, updateCurrentTurnIndex = () => {}, cancelCurrentTurnAnchor = () => {};
		${anchorMaintenance}
		return { start: initial => maintainCurrentTurnAnchor(initial, 5000),
			rebase() { windowShiftVersion++; windowShifting = true; env.top = 4560; windowShifting = false; } };
	`)(env, root) as { start(initial: boolean): Promise<void>; rebase(): void };
	return { env, ...result, dispose: () => root.remove() };
}

const rangeShift = between('function applyMountedRange(', 'function loadOlderItems()')
	.replace('next: TranscriptRange', 'next').replaceAll('<HTMLElement>', '')
	.replace('let delta: number | undefined;', 'let delta;');
function shiftedOrigin(activeAnchor: boolean) {
	const env = { top: 4900, order: [] as string[], ticks: [] as (() => void)[] };
	const state = new Function('env', 'activeAnchor', `
		let mountedRange = { start: 49, end: 71 }, mountedStart = 49, mountedEnd = 71;
		let windowShiftVersion = 0, windowShifting = false, followedViewportEnd = 5600, leadingGap = 0;
		let selectionBlocksNavigation = false, shouldAutoScroll = true, anchorRounding = null;
		let lastKnownScrollTop = 5000, lastScrollMetrics = null;
		const currentTurnAnchorId = activeAnchor ? 'user' : null;
		const transcriptWindow = { selectionLimited: false }, pinnedTranscriptRange = () => null;
		const containerRef = { get scrollTop() { return env.top; }, getBoundingClientRect: () => ({ top: 52 }) };
		const chatColumnRef = {}, windowSessionKey = 'long', mountedRangeCache = new Map(), SCROLL_CACHE_MAX = 1;
		const evictOldest = () => {}, markProgrammaticScrollSettling = () => {};
		const readScrollMetrics = () => ({ scrollTop: env.top });
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const refreshLatestTurnTailReserve = () => {};
		const maintainCurrentTurnAnchor = async () => {
			env.order.push(windowShifting ? 'reserve-before-unlock' : 'late-reserve');
			env.top = 4560;
			await tick();
		};
		${rangeShift}
		applyMountedRange({ start: 50, end: 72 }, false);
		return () => ({ known: lastKnownScrollTop, metrics: lastScrollMetrics, shifting: windowShifting });
	`)(env, activeAnchor) as () => { known: number; metrics: { scrollTop: number } | null; shifting: boolean };
	return { env, state, async flush() {
		for (let i = 0; i < 6; i++) { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); }
	} };
}

describe('bounded follower origin changes', () => {
	it.each([false, true])('rejects anchor work whose own await crosses eviction (initial=%s)', async initial => {
		const p = maintenanceAcrossShift();
		try {
			const pending = p.start(initial);
			p.rebase(); p.env.ticks.splice(0).forEach(resolve => resolve());
			await pending;
			expect(p.env.writes).toEqual([]);
			expect(p.env.top).toBe(4560);
		} finally { p.dispose(); }
	});

	it('publishes the rebased scroll metrics even when follower anchoring has no delta', async () => {
		const p = shiftedOrigin(false); await p.flush();
		expect(p.state()).toEqual({ known: 4900, metrics: { scrollTop: 4900 }, shifting: false });
	});

	it('consumes the active follower reserve in the same bounded commit before unlocking', async () => {
		const p = shiftedOrigin(true); await p.flush();
		expect(p.env.order).toEqual(['reserve-before-unlock']);
		expect(p.state()).toEqual({ known: 4560, metrics: { scrollTop: 4560 }, shifting: false });
	});
});

const rangePrefix = between('function applyMountedRange(', 'const container = containerRef;')
	.replace('next: TranscriptRange', 'next').replaceAll('<HTMLElement>', '');
const rangeCommit = between('const version = ++windowShiftVersion;', 'void tick().then(async () => {');
function appendProbe(overrides: Record<string, unknown> = {}) {
	const env = { start: 0, end: 2, next: { start: 0, end: 3 }, allowed: true, follows: true,
		shifting: false, gap: 0, leadingKey: 'first', firstKey: 'first', removed: 120.21875, headerHeight: 44, retainedKey: 'second', ...overrides };
	return new Function('env', `
		let mountedRange = { start: env.start, end: env.end };
		const mountedStart = mountedRange.start, mountedEnd = mountedRange.end;
		let followedViewportEnd = 5600, windowShiftVersion = 4, windowShifting = env.shifting, leadingGap = env.gap;
		let selectionBlocksNavigation = false, shouldAutoScroll = env.follows, anchorRounding = null;
		const transcriptWindow = { selectionLimited: false }, pinnedTranscriptRange = () => null;
		const first = { dataset: { transcriptKey: env.leadingKey }, getBoundingClientRect: () => ({ top: 100 }) };
		const retained = { dataset: { transcriptKey: env.retainedKey }, getBoundingClientRect: () => ({ top: 100 + env.removed }) };
		const chatColumnRef = { querySelector: () => first, querySelectorAll: () => [first, retained] };
		const earlierRowRef = { getBoundingClientRect: () => ({ height: env.headerHeight }) };
		const getComputedStyle = () => ({ rowGap: '18' });
		const displayItems = [{ key: env.firstKey }, { key: 'second' }, { key: 'third' }];
		const displayItemKey = item => item.key;
		const windowSessionKey = 'long', mountedRangeCache = new Map(), SCROLL_CACHE_MAX = 1;
		const evictOldest = () => {};
		${rangePrefix}
		${rangeCommit}
		}
		applyMountedRange(env.next, false, false, env.allowed);
		return { floor: followedViewportEnd, version: windowShiftVersion, shifting: windowShifting,
			gap: leadingGap, range: mountedRange, cached: mountedRangeCache.get(windowSessionKey) };
	`)(env);
}

describe('automatic same-context follower appends', () => {
	it('does not invalidate coordinates or pending follow work when only the tail grows', () => {
		const state = appendProbe();
		expect(state).toMatchObject({ floor: 5600, version: 4, shifting: false, range: { start: 0, end: 3 }, cached: { start: 0, end: 3 } });
	});

	it('preserves existing leading space in both the DOM state and cached range', () => {
		expect(appendProbe({ gap: 12 })).toMatchObject({ floor: 5600, version: 4, shifting: false, gap: 12, cached: { leadingGap: 12 } });
	});

	it.each([
		['unmounted next leading key', { next: { start: 2, end: 3 } }],
		['explicit leading eviction', { next: { start: 1, end: 3 }, allowed: false }],
		['reader leading eviction', { next: { start: 1, end: 3 }, follows: false }],
		['overlapping leading eviction', { next: { start: 1, end: 3 }, shifting: true }],
		['trailing removal', { next: { start: 0, end: 1 } }],
		['replaced leading key', { firstKey: 'replacement' }],
		['new session or explicit navigation', { allowed: false }],
		['scrolled-up reader', { follows: false }],
		['a genuine shift already in progress', { shifting: true }],
		['transcript clear', { next: { start: 0, end: 0 } }]
	] as const)('retains the strict coordinate-shift path for %s', (_name, overrides) => {
		expect(appendProbe(overrides)).toMatchObject({ floor: 0, version: 5, shifting: true });
	});
});

describe('automatic eviction retains the measured coordinate origin before rendering', () => {
	it.each([120.21875, 3223])('retains a %spx leading removal without invalidating follow work', removed => {
		const state = appendProbe({ next: { start: 1, end: 3 }, removed });
		expect(state).toMatchObject({ floor: 5600, version: 4, shifting: false,
			gap: removed - 62, cached: { start: 1, end: 3, leadingGap: removed - 62 } });
	});
	it('measures the first earlier control instead of assuming a fixed height', () => {
		expect(appendProbe({ next: { start: 1, end: 3 }, removed: 3223, headerHeight: 72 }))
			.toMatchObject({ gap: 3133, version: 4, shifting: false });
	});
	it('adds dropped height to an already-retained origin without charging the control twice', () => {
		expect(appendProbe({ start: 1, end: 3, next: { start: 2, end: 3 }, firstKey: 'unused', removed: 3223,
			leadingKey: 'old', retainedKey: 'third', gap: 400 }))
			.toMatchObject({ gap: 3623, floor: 5600, version: 4, shifting: false });
	});
});

// Execute each production older-edge entry point, with only layout/state edges
// supplied by the probe. The optional helper also lets the pre-fix inline rule
// execute for red-before proof instead of failing on a missing symbol.
const olderEdgeRule = (source.match(/function olderEdgeDistance\(scrollTop: number\) \{[^}]*\}/)?.[0] ?? '')
	.replace('scrollTop: number', 'scrollTop');
const readerScrollEdge = between('// Shift only in the reader\'s direction', '// Save scroll position');
const readerWheel = between('function handleWheel(', '// Touch: accumulate').replace('e: WheelEvent', 'e');
const readerTouch = between('let touchLastY = 0;', '/**\n\t * Put a reader').replaceAll('e: TouchEvent', 'e');
const readerLoadOlder = between('function loadOlderItems()', 'function loadNewerItems()');
function readerEdgeProbe(kind: 'scroll' | 'wheel' | 'touch' | 'explicit', top: number, gap: number, overrides = {}) {
	const env = { top, gap, up: true, shifting: false, held: false, older: 0, follows: true, preserveReading: false, ...overrides };
	new Function('env', 'kind', 'isUserScrollUpGesture', `
		let leadingGap = env.gap, windowShifting = env.shifting, mountedStart = 10, mountedEnd = 100;
		const containerRef = { scrollTop: env.top, scrollHeight: 10000, clientHeight: 634 };
		const currentScrollTop = env.top, lastScrollMetrics = { scrollTop: env.top + (env.up ? 120 : -120) };
		const manualNavigationReleaseUntil = env.held ? Date.now() + 10000 : 0, displayItems = Array(100);
		const disengageAutoScroll = () => { env.follows = false; };
		const transcriptWindow = { loadOlder: options => {
				env.preserveReading = options?.preserveReading === true;
				return { start: 6, end: 30 };
			} };
		const applyMountedRange = () => { env.older++; }, loadNewerItems = () => {};
		${olderEdgeRule}
		${readerLoadOlder}
		${readerWheel}
		${readerTouch}
		if (kind === 'explicit') loadOlderItems();
		if (kind === 'scroll') { ${readerScrollEdge} }
		if (kind === 'wheel') handleWheel({ deltaY: env.up ? -120 : 120 });
		if (kind === 'touch') {
			handleTouchStart({ touches: [{ clientY: 100 }] });
			handleTouchMove({ touches: [{ clientY: env.up ? 120 : 80 }] });
		}
	`)(env, kind, isUserScrollUpGesture);
	return env;
}

describe('older paging uses the retained mounted origin', () => {
	for (const kind of ['scroll', 'wheel', 'touch'] as const) {
		it.each([2400, 1500])(`${kind} pages before a retained gap leaves blank history at %spx`, top => {
			expect(readerEdgeProbe(kind, top, 3285.7)).toMatchObject({ older: 1, follows: false });
		});
		it(`${kind} does not page before approaching the mounted content`, () => {
			expect(readerEdgeProbe(kind, 4285.7, 3285.7).older).toBe(0);
		});
		it(`${kind} retains the zero-gap boundary behavior`, () => {
			expect(readerEdgeProbe(kind, kind === 'scroll' ? 899 : 0, 0)).toMatchObject({ older: 1, follows: false, preserveReading: true });
		});
		it(`${kind} does not load older history while moving newer`, () => {
			expect(readerEdgeProbe(kind, 1500, 3285.7, { up: false }).older).toBe(0);
		});
		it(`${kind} respects an in-flight range shift`, () => {
			expect(readerEdgeProbe(kind, 1500, 3285.7, { shifting: true }).older).toBe(0);
		});
	}
	it('ordinary scroll still respects an explicit navigation hold', () => {
		expect(readerEdgeProbe('scroll', 1500, 3285.7, { held: true }).older).toBe(0);
	});
	it('Show earlier is explicit navigation, not speculative older paging', () => {
		expect(readerEdgeProbe('explicit', 0, 0)).toMatchObject({ older: 1, follows: false, preserveReading: false });
		expect(source).toContain('class="earlier-button" onclick={loadOlderItems}');
	});
});

const readerScroll = between('function handleScroll()', '/**\n\t * Release stick-to-bottom');
describe('programmatic scrolls do not request automatic paging', () => {
	it.each(['event', 'queued frame'])('ignores a marked programmatic scroll at the %s stage', stage => {
		const probe = new Function('stage', `
			const containerRef = { scrollTop: 24 };
			let anchorRounding = null, lastKnownScrollTop = 0, lastScrollMetrics = null, scrollRafPending = false;
			let programmaticScrollDepth = stage === 'event' ? 1 : 0, callback = null;
			const scheduleReadingAnchorCapture = () => {}, readScrollMetrics = () => ({ scrollTop: 24 });
			const requestAnimationFrame = next => { callback = next; };
			${readerScroll}
			handleScroll();
			if (callback) { programmaticScrollDepth = 1; callback(); }
			return { known: lastKnownScrollTop, metrics: lastScrollMetrics, pending: scrollRafPending };
		`)(stage);
		expect(probe).toEqual({ known: 24, metrics: { scrollTop: 24 }, pending: false });
	});
});

// Execute the real automatic loader and anchor compensation, not just a stub
// asserting its option. Geometry alone models the prepended DOM in jsdom.
function olderPagingAnchorProbe(expensive: boolean) {
	const env = { top: 24, ticks: [] as (() => void)[] };
	const state = new Function('env', 'expensive', 'createTranscriptWindow', `
		let costs = [1000, expensive ? 8000 : 1000, 1000, 2000];
		const transcriptWindow = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => 3 });
		transcriptWindow.setItemCount(4);
		transcriptWindow.setItemCount(4, { followTail: false });
		costs = [1000, expensive ? 8000 : 1000, 1000, 6000];
		const repriced = transcriptWindow.setItemCount(4, { followTail: false });
		let mountedRange = expensive ? repriced : transcriptWindow.restoreRange({ start: 2, end: 4 });
		const mountedStart = mountedRange.start, mountedEnd = mountedRange.end;
		let windowShiftVersion = 0, windowShifting = false, followedViewportEnd = 0, leadingGap = 0;
		let selectionBlocksNavigation = false, shouldAutoScroll = false, anchorRounding = null;
		let lastKnownScrollTop = env.top, lastScrollMetrics = null;
		const currentTurnAnchorId = null, pinnedTranscriptRange = () => null;
		const rows = [0, 1, 2, 3].map(index => ({ dataset: { transcriptIndex: String(index) },
			get isConnected() { return index >= mountedRange.start && index < mountedRange.end; },
			getBoundingClientRect: () => ({ top: (index - mountedRange.start) * 130 - env.top,
				bottom: (index - mountedRange.start + 1) * 130 - env.top }) }));
		const containerRef = { get scrollTop() { return env.top; }, set scrollTop(top) { env.top = top; },
			getBoundingClientRect: () => ({ top: 0 }), querySelectorAll: () => rows.filter(row => row.isConnected) };
		const windowSessionKey = 'reader', mountedRangeCache = new Map(), SCROLL_CACHE_MAX = 1;
		const evictOldest = () => {}, markProgrammaticScrollSettling = () => {};
		const readScrollMetrics = () => ({ scrollTop: env.top });
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const refreshLatestTurnTailReserve = () => {}, disengageAutoScroll = () => {};
		${rangeShift}
		${readerLoadOlder}
		const before = rows[3].getBoundingClientRect().top;
		loadOlderItemsAutomatically();
		return () => ({ range: mountedRange, before, top: rows[3].getBoundingClientRect().top,
			connected: rows[3].isConnected, shifting: windowShifting });
	`)(env, expensive, createTranscriptWindow) as () => { range: { start: number; end: number }; before: number; top: number; connected: boolean; shifting: boolean };
	return { state, async flush() {
		for (let i = 0; i < 6; i++) { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); }
	} };
}

describe('automatic older paging preserves the reading reply anchor', () => {
	it.each([true, false])('retains the reply position after a streaming reprice (expensive prefix=%s)', async expensive => {
		const probe = olderPagingAnchorProbe(expensive);
		await probe.flush();
		const state = probe.state();
		expect(state).toMatchObject({ connected: true, shifting: false, range: { start: expensive ? 2 : 0, end: 4 } });
		expect(state.top).toBe(state.before);
	});
});
