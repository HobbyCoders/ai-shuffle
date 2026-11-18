// @vitest-environment node
/** Run MessageArea's actual reserve, follow, handoff and release code against a
 * small layout model, and check what a reader would see: the applied tail
 * padding, scrollTop, and where the queued bubbles, the reply's end and the
 * error banner land on screen. jsdom has no layout; the model below is the
 * CSS of .chat-column, .tail-controls and .tail-error in MessageArea.svelte. */
import { describe, expect, it } from 'vitest';
import { transformSync } from 'esbuild';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import * as autoScroll from '$lib/stores/session/auto-scroll';
import { landedFromQueue } from '$lib/utils/queue-handoff';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
function between(start: string, end: string) {
	const from = source.indexOf(start), to = source.indexOf(end, from);
	if (from < 0 || to <= from) throw new Error(`Missing source block: ${start}`);
	return source.slice(from, to);
}
const observerStart = 'const observer = new MutationObserver(() => {';
const observer = between(observerStart, 'observer.observe(chatColumnRef');
const resizeStart = 'resizeObserver = new ResizeObserver((entries) => {';
const resize = between(resizeStart, 'resizeObserver.observe(containerRef);');
const code = transformSync([
	between('\tfunction tailControlsExtent(', '\t/**\n\t * Where a stopped turn'),
	between("\tlet layoutBoundary = '';", '\t// Rendered geometry has one lifecycle'),
	between("\t// Give a queue handoff's held slot back", '\t// Keep a freshly-sent pending bubble in view'),
	`const onMutation = () => {${observer.slice(observerStart.length, observer.lastIndexOf('});'))}};`,
	`const onResize = (entries) => {${resize.slice(resizeStart.length, resize.lastIndexOf('});'))}};`
].join('\n'), { loader: 'ts' }).code;

const ROW_GAP = 18, BUBBLE_GAP = 12, BOTTOM = 24;
type Layout = {
	clientHeight: number; scrollTop: number; proseEnd: number; anchorTop: number;
	bubbles: number[]; banner: number;
};
type Session = { sessionId: string; messages: unknown[]; isStreaming: boolean; liveTurnId: string | null;
	error: string | null; queuedMessages: { id: string }[]; stopRequestedTurnId?: string | null };

function view(layout: Layout, anchored: boolean) {
	const store: Record<string, unknown> = {
		...autoScroll, landedFromQueue, layout, anchored,
		handoffPainted: false, heldBlankPx: 0, handoffExcessFreed: false, transcriptChangedSinceFrame: false,
		document: { getElementById: () => anchorEl },
		getComputedStyle: () => ({ rowGap: String(ROW_GAP), marginTop: '0', marginBottom: '0', getPropertyValue: () => '0' }),
	};
	// A name only some revisions declare (or a no-op side channel) resolves here.
	const scope = new Proxy(store, {
		has: (target, key) => typeof key === 'string' && (key in target || !(key in globalThis)),
		get: (target, key) => (typeof key !== 'string' ? undefined : key in target ? target[key] : (() => undefined)),
		set: (target, key, value) => { target[key as string] = value; return true; }
	});
	const anchorEl = { getBoundingClientRect: () => ({ top: layout.anchorTop - layout.scrollTop }), compareDocumentPosition: () => 0 };
	return new Function('scope', `with (scope) {
		let $activeSession = null, lastKnownScrollTop = 0, bannerError = null;
		let followedViewportEnd = 0, followedFloorContext = null, presentedTailError = null, tailErrorOffsetPx = 0;
		let tailQueueSlotPx = 0, handoffSlotPx = 0;
		let tailReservePx = MIN_TRANSCRIPT_TAIL_RESERVE, latestTurnTailPx = MIN_TRANSCRIPT_TAIL_RESERVE;
		let windowShifting = false, windowShiftVersion = 0, isMounted = true, shouldAutoScroll = true;
		let programmaticScrollDepth = 0, showJumpToLatest = false, selectionBlocksNavigation = false;
		let currentTurnAnchorId = anchored ? 'turn' : null, anchorMaintenanceRaf = null, anchorPlacementOwedFor = null;
		let cleanup, frames = [], effects = [], viewportHeight = 0;
		const transcript = { latestUserTurnId: 'turn' };
		const activeSession = {}, get = () => $activeSession;
		const notSentHold = { messages: [], bubbles: [] }, restingOnNotSentRows = () => false;
		const pendingMessages = { get length() { return $activeSession?.queuedMessages?.length ?? 0; } };
		const $effect = { pre: fn => effects.push(fn) };
		const untrack = fn => fn();
		const turnAnchorId = id => id;
		const requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
		const cancelAnimationFrame = () => {};

		// What the browser has applied: Svelte state reaches the DOM on commit.
		let padding = MIN_TRANSCRIPT_TAIL_RESERVE, minHeight = 0, errorOffset = 0;
		const maxTop = () => Math.max(0, layout.proseEnd + padding - layout.clientHeight);
		const clamp = () => { layout.scrollTop = Math.min(Math.max(0, layout.scrollTop), maxTop()); };
		function commit() {
			padding = Math.max(tailReservePx, latestTurnTailPx);
			minHeight = tailQueueSlotPx;
			errorOffset = tailErrorOffsetPx;
			clamp();
		}
		const tick = () => Promise.resolve().then(commit);
		const docked = () => layout.bubbles.length ? layout.bubbles.reduce((a, b) => a + b, 0) + ${BUBBLE_GAP} * (layout.bubbles.length - 1) : 0;
		// .tail-controls: bottom: calc(24px - var(--transcript-tail-reserve)) under the zero-height spacer.
		const slotBottom = () => layout.proseEnd + padding - ${BOTTOM} - layout.scrollTop;
		const slotHeight = () => Math.max(minHeight, docked());
		const containerRef = {
			get clientHeight() { return layout.clientHeight; },
			get scrollHeight() { return layout.proseEnd + padding; },
			get scrollTop() { return layout.scrollTop; },
			set scrollTop(v) { layout.scrollTop = v; clamp(); },
			getBoundingClientRect: () => ({ top: 0, bottom: layout.clientHeight }),
			contains: () => true
		};
		const tailSpacerRef = { getBoundingClientRect: () => ({ top: layout.proseEnd - layout.scrollTop }) };
		const firstRow = { getBoundingClientRect: () => ({ top: slotBottom() - docked() }) };
		const tailControlsRef = {
			getBoundingClientRect: () => ({ top: slotBottom() - slotHeight(), bottom: slotBottom(), height: slotHeight() }),
			querySelector: () => layout.bubbles.length ? { firstElementChild: firstRow } : null
		};
		const tailErrorRef = { getBoundingClientRect: () => {
			const shown = presentedTailError && layout.banner > 0 ? layout.banner : 0;
			const bottom = slotBottom() - errorOffset;
			return { top: bottom - shown, bottom, height: shown };
		} };
		const chatColumnRef = {};
		function setProgrammaticScrollTop(top) {
			containerRef.scrollTop = Math.max(0, top);
			lastKnownScrollTop = containerRef.scrollTop;
		}
		${code}
		return {
			/** One store update: the pre-render effects, the DOM commit, then the observer. */
			async update(session, dom = () => {}) {
				cleanup?.();
				$activeSession = session;
				bannerError = session.error;
				lastKnownScrollTop = layout.scrollTop;
				cleanup = effects[0]();
				effects.slice(1).forEach(fn => fn());
				dom();
				commit();
				await Promise.resolve();
				onMutation();
				for (let i = 0; i < 6; i++) await Promise.resolve();
				commit();
			},
			/** Reply text grows in place (a streaming fold). */
			async grow(px) {
				layout.proseEnd += px;
				commit();
				await Promise.resolve();
				onMutation();
				for (let i = 0; i < 6; i++) await Promise.resolve();
				commit();
			},
			/** An animation frame and the microtasks before its paint. */
			async frame() {
				frames.splice(0).forEach(fn => fn());
				for (let i = 0; i < 8; i++) await Promise.resolve();
				commit();
			},
			async resize(height) {
				layout.clientHeight = height;
				clamp();
				onResize([{ contentRect: { height } }]);
				for (let i = 0; i < 6; i++) await Promise.resolve();
			},
			/** The reader scrolls by px (negative: up), which sets following as handleScroll would. */
			scroll(px) { layout.scrollTop += px; clamp(); lastKnownScrollTop = layout.scrollTop; shouldAutoScroll = px >= 0; },
			start(id) { startCurrentTurnAnchor(id, true); },
			async place() { await maintainCurrentTurnAnchor(true); commit(); },
			read: () => ({
				padding, reserve: tailReservePx, scrollTop: layout.scrollTop, slotTop: slotBottom() - slotHeight(),
				bubblesTop: slotBottom() - docked(), slotBottom: slotBottom(), replyEnd: layout.proseEnd - layout.scrollTop,
				banner: tailErrorRef.getBoundingClientRect(), errorOffset: tailErrorOffsetPx, minHeight
			})
		};
	}`)(scope) as {
		update(session: Session, dom?: () => void): Promise<void>;
		grow(px: number): Promise<void>;
		frame(): Promise<void>;
		resize(height: number): Promise<void>;
		scroll(px: number): void;
		start(id: string): void;
		place(): Promise<void>;
		read(): { padding: number; reserve: number; scrollTop: number; slotTop: number; bubblesTop: number; slotBottom: number;
			replyEnd: number; banner: { top: number; bottom: number; height: number }; errorOffset: number; minHeight: number };
	};
}

const queued = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `q${i}` }));
const turn = (messages: number, queue: number, error: string | null = null): Session => ({
	sessionId: 'chat', messages: Array(messages).fill({}), isStreaming: true, liveTurnId: 'live', error, queuedMessages: queued(queue)
});
/** The reserve docked bubbles alone need under a reply that already fills the view. */
const settledReserve = (bubbles: number[]) =>
	autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE + bubbles.reduce((a, b) => a + b, 0) + BUBBLE_GAP * (bubbles.length - 1)
	+ autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE + ROW_GAP;

/** A followed, anchored turn (its reply fills the view by default), four short prompts queued. */
async function streamingTurn(clientHeight = 600, reply = 1000) {
	const layout: Layout = { clientHeight, scrollTop: 0, proseEnd: 2000, anchorTop: 2000 - reply, bubbles: [80, 80, 80, 80], banner: 0 };
	const v = view(layout, true);
	await v.update(turn(4, 4));
	await v.place();
	await v.grow(10);
	return { layout, v };
}
/** The next queued prompt's row lands in the same commit its bubble leaves. */
const dequeue = (v: ReturnType<typeof view>, layout: Layout, messages: number, bubbles: number[]) =>
	v.update(turn(messages, bubbles.length), () => { layout.proseEnd += 60; layout.bubbles = bubbles; });

describe('queue handoff hold geometry', () => {
	it('gives each dequeue its freed blank back on the pass that measures it, with the slot still', async () => {
		const { layout, v } = await streamingTurn();
		const { slotTop, slotBottom } = v.read();
		await dequeue(v, layout, 5, [80, 80]);
		// The handoff paint keeps the slot and the reserve it was painted with.
		expect(v.read()).toMatchObject({ slotTop, slotBottom, padding: settledReserve([80, 80, 80, 80]) });
		// No reply growth needed: the pass after that paint gives the blank back.
		await v.frame(); await v.frame();
		expect(v.read()).toMatchObject({ slotTop, slotBottom, padding: settledReserve([80, 80]) });
		expect(v.read().replyEnd).toBe(layout.clientHeight - settledReserve([80, 80]));
		// A second dequeue under the same hold frees its bubble at once.
		await dequeue(v, layout, 6, [80]);
		expect(v.read()).toMatchObject({ slotTop, slotBottom, padding: settledReserve([80]) });
		await v.grow(100);
		expect(v.read()).toMatchObject({ slotTop, slotBottom, padding: settledReserve([80]) });
		expect(v.read().replyEnd).toBe(layout.clientHeight - settledReserve([80]));
	});

	it('gives the blank back under continuous streaming, a reply update before every frame', async () => {
		const { layout, v } = await streamingTurn();
		const { slotTop } = v.read();
		await dequeue(v, layout, 5, [80]);
		for (let i = 0; i < 3; i++) { await v.grow(5); await v.frame(); }
		expect(v.read()).toMatchObject({ slotTop, padding: settledReserve([80]) });
		expect(v.read().replyEnd).toBe(layout.clientHeight - settledReserve([80]));
	});

	it('lets an active anchor drop a taller passive reserve measured before it', async () => {
		const layout: Layout = { clientHeight: 600, scrollTop: 0, proseEnd: 2000, anchorTop: 1000, bubbles: [80, 80, 80, 80], banner: 0 };
		const v = view(layout, false);
		await v.update(turn(4, 4));
		await v.grow(10);
		expect(v.read().padding).toBe(settledReserve([80, 80, 80, 80]));
		// The next queued prompt lands and takes the turn's anchor.
		layout.anchorTop = layout.proseEnd;
		await dequeue(v, layout, 5, [80, 80, 80]);
		v.start('next');
		await v.place();
		const { slotTop } = v.read();
		for (let i = 0; i < 16; i++) { await v.grow(50); await v.frame(); }
		expect(v.read()).toMatchObject({ slotTop, padding: settledReserve([80, 80, 80]) });
		expect(v.read().replyEnd).toBe(layout.clientHeight - settledReserve([80, 80, 80]));
	});

	it('sizes the reserve for a height-only resize from the docked bubbles, as if never held', async () => {
		// A short reply: the prompt's own room, not the queue, sizes the reserve.
		const held = await streamingTurn(560, 100);
		await dequeue(held.v, held.layout, 5, [80]);
		await held.v.frame(); await held.v.frame();
		const fresh = { layout: { ...held.layout, scrollTop: 0, bubbles: [80] } as Layout, v: null as unknown as ReturnType<typeof view> };
		fresh.v = view(fresh.layout, true);
		await fresh.v.update(turn(5, 1));
		await fresh.v.place();
		await fresh.v.grow(0);
		// The keyboard opens: only the container's height changes.
		await held.v.resize(300); await held.v.frame(); await held.v.frame();
		await fresh.v.resize(300); await fresh.v.frame(); await fresh.v.frame();
		expect(held.v.read().padding).toBe(fresh.v.read().padding);
		expect(held.v.read().replyEnd).toBe(fresh.v.read().replyEnd);
		expect(held.v.read().bubblesTop).toBe(fresh.v.read().bubblesTop);
	});

	it.each([
		['the reader scrolled up', true, async (v: ReturnType<typeof view>) => v.scroll(-300)],
		['Stop is pending', false, async (v: ReturnType<typeof view>) => v.update({ ...turn(5, 1), stopRequestedTurnId: 'live' })]
	])('keeps the held blank where %s, then gives it back once followed again', async (_name, scrolledUp, change) => {
		const { layout, v } = await streamingTurn();
		await dequeue(v, layout, 5, [80]);
		await change(v);
		const before = v.read();
		await v.frame(); await v.frame();
		await v.grow(20);
		// No reserve clamp moves the reader: the padding stays, and so does a scrolled-up reader.
		expect(v.read().padding).toBe(settledReserve([80, 80, 80, 80]));
		if (scrolledUp) expect(v.read().scrollTop).toBe(before.scrollTop);
		await v.update(turn(5, 1));
		v.scroll(0);
		await v.grow(20);
		expect(v.read().padding).toBe(settledReserve([80]));
	});

	it('lets only the bubbles take input, not the held blank over the reply (wiring)', () => {
		const css = source.slice(source.indexOf('<style>'));
		expect(css).toMatch(/\.tail-controls \{[^}]*pointer-events: none;/);
		expect(css).toMatch(/\.tail-controls :global\(\.userrow\) \{\s*pointer-events: auto;/);
	});

	describe('an error after the hold gave its blank back (390x400)', () => {
		async function givenBack() {
			const { layout, v } = await streamingTurn(400);
			await dequeue(v, layout, 5, [80, 80]);
			await v.frame(); await v.frame();
			layout.banner = 60;
			return { layout, v };
		}

		it('raises the banner just above the docked bubbles, on screen', async () => {
			const { v } = await givenBack();
			await v.update(turn(5, 2, 'CLI error'));
			const { banner, bubblesTop } = v.read();
			expect(banner.bottom).toBe(bubblesTop - ROW_GAP);
			expect(banner.top).toBeGreaterThanOrEqual(0);
		});

		it('places it from the leaving bubble when the last one goes in the same commit', async () => {
			const { layout, v } = await givenBack();
			await v.update(turn(5, 1), () => { layout.bubbles = [80]; });
			await v.update(turn(6, 0, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = []; });
			const { banner, slotBottom } = v.read();
			expect(banner.bottom).toBe(slotBottom - 80 - ROW_GAP);
			expect(banner.top).toBeGreaterThanOrEqual(0);
		});

		it('reserves only the docked bubbles while the banner steps aside for its Error row', async () => {
			const { layout, v } = await givenBack();
			layout.banner = 0;
			await v.update(turn(5, 2, 'CLI error'));
			await v.grow(20);
			expect(v.read().padding).toBe(settledReserve([80, 80]));
			expect(v.read().replyEnd).toBe(layout.clientHeight - settledReserve([80, 80]));
		});
	});
});
