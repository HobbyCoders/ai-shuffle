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

type Session = { sessionId: string; messages: unknown[]; isStreaming: boolean; liveTurnId: string | null; error: null; queuedMessages: never[] };
const session = (id: string, count = 2): Session => ({ sessionId: id, messages: Array(count).fill({}), isStreaming: false, liveTurnId: null, error: null, queuedMessages: [] });

function probe() {
	const env = {
		session: session('long'), scrollTop: 5000, proseEnd: 200,
		follows: true, calls: [] as string[], ticks: [] as (() => void)[]
	};
	const create = new Function('env', `
		let $activeSession = env.session, lastKnownScrollTop = env.scrollTop;
		let followedViewportEnd = 0, followedFloorSession = null;
		let presentedTailError = null, tailErrorOffsetPx = 0;
		let windowShifting = false, windowShiftVersion = 0;
		let isMounted = true, shouldAutoScroll = true, programmaticScrollDepth = 0;
		let currentTurnAnchorId = 'anchor', cleanup;
		const containerRef = { clientHeight: 600, scrollTop: 0, getBoundingClientRect: () => ({ top: 0 }) };
		const tailSpacerRef = { getBoundingClientRect: () => ({ top: env.proseEnd }) };
		const tailControlsRef = null, chatColumnRef = null;
		const activeSession = {}, get = () => env.session;
		const untrack = fn => fn();
		const tick = () => new Promise(resolve => env.ticks.push(resolve));
		const requestAnimationFrame = fn => { fn(); return 0; };
		const maintainCurrentTurnAnchor = () => env.calls.push('anchor');
		const followGrowthNow = () => env.calls.push('follow');
		let effect;
		const $effect = { pre: fn => { effect = fn; } };
		${reserve}
		${transaction}
		return {
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
			depth: () => programmaticScrollDepth
		};
	`);
	const state = create(env) as { run(): void; shift(): void; endShift(): void; floor(): number; reserve(): number; depth(): number };
	return { env, ...state, async flush() { env.ticks.splice(0).forEach(resolve => resolve()); await Promise.resolve(); } };
}

function captureLongFloor(p: ReturnType<typeof probe>) {
	p.run();
	p.env.session = session('long', 3);
	p.run();
	expect(p.floor()).toBe(5600);
}

describe('MessageArea followed-floor coordinate ownership', () => {
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
