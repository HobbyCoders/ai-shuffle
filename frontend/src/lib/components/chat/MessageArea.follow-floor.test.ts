/** Execute MessageArea's actual pre-render transaction with controlled ticks.
 * jsdom has no scroll layout; geometry alone is supplied by the probe. */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
function between(start: string, end: string) {
	const from = source.indexOf(start), to = source.indexOf(end, from);
	if (from < 0 || to <= from) throw new Error(`Missing floor transaction: ${start}`);
	return source.slice(from, to);
}
const transaction = between("let layoutBoundary = '';", '// Rendered geometry has one lifecycle');
const reserve = between('function readingFloorReserve()', 'async function maintainCurrentTurnAnchor').replace('(): number', '()');
const beginShift = between('windowShifting = true;', 'mountedRange = next;');
const beginTurn = between('function startCurrentTurnAnchor(', '/**\n\t * Where a stopped turn').replace('messageId: string', 'messageId');

type Session = { sessionId: string; messages: unknown[]; isStreaming: boolean; liveTurnId: string | null; error: string | null; queuedMessages: never[] };
const session = (id: string, count = 2): Session => ({ sessionId: id, messages: Array(count).fill({}), isStreaming: false, liveTurnId: null, error: null, queuedMessages: [] });

function probe() {
	const env = {
		session: session('long'), scrollTop: 5000, proseEnd: 200,
		follows: true, pendingHeight: 0, gap: 18, calls: [] as string[], ticks: [] as (() => void)[]
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
			depth: () => programmaticScrollDepth
		};
	`);
	const state = create(env) as { dismiss(): void; start(fromQueue: boolean): void; run(): void; shift(): void; endShift(): void; floor(): number; reserve(): number; queueSlot(): number; errorOffset(): number; depth(): number };
	return { env, ...state, async flush() { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); } };
}

function captureLongFloor(p: ReturnType<typeof probe>) {
	p.run();
	p.env.session = session('long', 3);
	p.run();
	expect(p.floor()).toBe(5600);
}

describe('MessageArea followed-floor coordinate ownership', () => {
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

const rangePrefix = between('function applyMountedRange(', 'const container = containerRef;')
	.replace('next: TranscriptRange', 'next').replaceAll('<HTMLElement>', '');
const rangeCommit = between('const version = ++windowShiftVersion;', 'void tick().then(async () => {');
function appendProbe(overrides: Record<string, unknown> = {}) {
	const env = { start: 0, end: 2, next: { start: 0, end: 3 }, allowed: true, follows: true,
		shifting: false, gap: 0, leadingKey: 'first', firstKey: 'first', ...overrides };
	return new Function('env', `
		let mountedRange = { start: env.start, end: env.end };
		const mountedStart = mountedRange.start, mountedEnd = mountedRange.end;
		let followedViewportEnd = 5600, windowShiftVersion = 4, windowShifting = env.shifting, leadingGap = env.gap;
		let selectionBlocksNavigation = false, shouldAutoScroll = env.follows, anchorRounding = null;
		const transcriptWindow = { selectionLimited: false }, pinnedTranscriptRange = () => null;
		const chatColumnRef = { querySelector: () => ({ dataset: { transcriptKey: env.leadingKey } }) };
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
		['leading eviction', { next: { start: 1, end: 3 } }],
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
