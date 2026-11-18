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
	bubbles: number[]; banner: number; width?: number;
};
type Session = { sessionId: string; messages: unknown[]; isStreaming: boolean; liveTurnId: string | null;
	error: string | null; queuedMessages: { id: string }[]; stopRequestedTurnId?: string | null };

function view(layout: Layout, anchored: boolean) {
	const store: Record<string, unknown> = {
		...autoScroll, landedFromQueue, layout, anchored,
		handoffPainted: false, heldBlankPx: 0, handoffHold: 0, tailErrorBaselinePx: 0, tailErrorWidthPx: 0, tailErrorDockedPx: 0, tailErrorPaintedDockedPx: 0, tailErrorDockedAt: '', handoffExcessFreed: false, transcriptChangedSinceFrame: false,
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
			getBoundingClientRect: () => ({ top: slotBottom() - slotHeight(), bottom: slotBottom(), height: slotHeight(), width: layout.width ?? 358 }),
			querySelector: () => layout.bubbles.length ? { firstElementChild: firstRow } : null
		};
		const tailErrorRef = { getBoundingClientRect: () => {
			const shown = presentedTailError && layout.banner > 0 ? layout.banner : 0;
			const bottom = slotBottom() - errorOffset;
			return { top: bottom - shown, bottom, height: shown };
		} };
		// The column's padding is the tail reserve, and it ends the scroll content.
		const chatColumnRef = { getBoundingClientRect: () => ({ bottom: layout.proseEnd + padding - layout.scrollTop }) };
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

		it('keeps the banner above a docked bubble that wraps taller after it', async () => {
			const { layout, v } = await givenBack();
			await v.update(turn(5, 2, 'CLI error'));
			// A narrower column wraps the first bubble onto more lines.
			layout.bubbles = [128, 80];
			await v.resize(400); await v.frame();
			const { banner, bubblesTop } = v.read();
			expect(banner.bottom).toBeLessThanOrEqual(bubblesTop - ROW_GAP);
			expect(banner.top).toBeGreaterThanOrEqual(0);
		});

		it('gives a narrow column\'s wrap clearance back when it widens, and none of it moves on a dequeue', async () => {
			const { layout, v } = await givenBack();
			await v.update(turn(5, 2, 'CLI error'));
			const taken = v.read();
			// Narrow: both long bubbles wrap taller and lift the banner above them.
			Object.assign(layout, { width: 298, bubbles: [128, 128] });
			await v.resize(400); await v.frame();
			const lifted = v.read();
			expect(lifted.banner.bottom).toBeLessThanOrEqual(lifted.bubblesTop - ROW_GAP);
			expect(lifted.errorOffset).toBeGreaterThan(taken.errorOffset);
			expect(lifted.banner.top).toBeGreaterThanOrEqual(0);
			// A dequeue at that width leaves the banner where it is read.
			await v.update(turn(6, 1, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = [128]; });
			await v.grow(20);
			expect(v.read()).toMatchObject({ errorOffset: lifted.errorOffset, banner: lifted.banner });
			// Widen: the bubble unwraps and the banner goes back to where the error put it.
			Object.assign(layout, { width: 358, bubbles: [80] });
			await v.resize(400); await v.frame();
			const widened = v.read();
			expect(widened).toMatchObject({ errorOffset: taken.errorOffset, banner: taken.banner });
			// Its reserve follows: the reply consumes the lift's room and ends under the banner as before.
			await v.grow(200);
			expect(v.read()).toMatchObject({ padding: taken.padding, replyEnd: layout.clientHeight - taken.padding, banner: taken.banner });
			// Drain: the last bubble leaves; the banner stays at the takeover position, not the narrow one.
			await v.update(turn(7, 0, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = []; });
			await v.grow(20);
			expect(v.read()).toMatchObject({ errorOffset: taken.errorOffset, padding: taken.padding, banner: taken.banner });
		});

		it('gives back only the lift that would put the banner off screen when the queue drains narrow', async () => {
			const { layout, v } = await givenBack();
			await v.update(turn(5, 2, 'CLI error'));
			const taken = v.read();
			// Narrow: the two long bubbles wrap taller than the pane can show under the banner.
			Object.assign(layout, { width: 298, bubbles: [180, 180] });
			await v.resize(400); await v.frame();
			const lifted = v.read();
			expect(lifted.banner.bottom).toBeLessThanOrEqual(lifted.bubblesTop - ROW_GAP);
			// The queue drains at that width; the column never widens again.
			await v.update(turn(6, 1, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = [180]; });
			await v.grow(20);
			await v.update(turn(7, 0, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = []; });
			const drained = v.read();
			// At the followed bottom the whole banner is inside the viewport, and no
			// lower than it has to be: a still banner is kept as far as it is readable.
			expect(drained.scrollTop).toBe(layout.proseEnd + drained.padding - layout.clientHeight);
			expect(drained.banner.top).toBeGreaterThanOrEqual(0);
			expect(drained.banner.bottom).toBeLessThanOrEqual(layout.clientHeight);
			expect(drained.banner.top).toBeLessThanOrEqual(1);
			expect(drained.errorOffset).toBeGreaterThan(taken.errorOffset);
			// The reserve gave the lift back with it: it holds the banner, no blank beyond.
			expect(drained.padding).toBe(drained.errorOffset + 60 + ROW_GAP + 2 * autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE);
			// It stays readable, and still, as the next reply streams under it.
			await v.grow(200);
			expect(v.read()).toMatchObject({ errorOffset: drained.errorOffset, banner: drained.banner, padding: drained.padding });
		});

		describe('giving back a narrow column\'s excess lift', () => {
			/** An error over two queued bubbles that a narrower column wraps taller than the pane shows under the banner. */
			async function narrowLift(anchored: boolean) {
				const layout: Layout = { clientHeight: 400, scrollTop: 0, proseEnd: 2000, anchorTop: 1000, bubbles: [80, 80], banner: 60 };
				const v = view(layout, anchored);
				await v.update(turn(4, 2));
				if (anchored) await v.place();
				await v.grow(10);
				await v.update(turn(4, 2, 'CLI error'));
				Object.assign(layout, { width: 298, bubbles: [180, 180] });
				await v.resize(400); await v.frame();
				// At the scroll bottom, where a passive follow pins the reader (pinBelowCurrentReserve).
				v.scroll(layout.proseEnd);
				return { layout, v, lifted: v.read() };
			}
			/** A queued prompt is withdrawn: its bubble leaves and no transcript row comes. */
			const withdraw = (v: ReturnType<typeof view>, layout: Layout, bubbles: number[]) =>
				v.update(turn(4, bubbles.length, 'CLI error'), () => { layout.bubbles = bubbles; });
			const bannerReserve = (offset: number) => offset + 60 + ROW_GAP + 2 * autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE;

			it('keeps a scrolled-up reader where they are, then gives the lift back once they follow again (S15)', async () => {
				const { layout, v, lifted } = await narrowLift(false);
				v.scroll(-48);
				const away = v.read();
				await withdraw(v, layout, [180]);
				await v.frame(); await v.grow(0);
				// Nothing the reader asked for: no clamp, and the banner stays where it was read.
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, errorOffset: lifted.errorOffset, padding: away.padding });
				// Back at the bottom: the next pass gives the excess back with its reserve.
				v.scroll(48);
				await v.grow(0);
				const followed = v.read();
				expect(followed.errorOffset).toBeLessThan(lifted.errorOffset);
				expect(followed.padding).toBe(bannerReserve(followed.errorOffset));
				expect(followed.banner.top).toBeGreaterThanOrEqual(0);
				expect(followed.banner.bottom).toBeLessThanOrEqual(layout.clientHeight);
				// In the app, following again is what asks for that pass (wiring).
				expect(source).toMatch(/if \(!shouldAutoScroll\) return;\s*untrack\(\(\) => \{\s*if \(!presentedTailError \|\| handoffSlotPx\) return;\s*if \(currentTurnAnchorId\) scheduleCurrentTurnAnchorMaintenance\(\);\s*else scheduleLatestTurnTailRefresh\(\);/);
			});

			/** A settled error over one queued bubble, its banner shown or stepped aside for its Error row. */
			async function settledError(anchored: boolean, banner: number, bubbles = [80]) {
				const layout: Layout = { clientHeight: 400, scrollTop: 0, proseEnd: 2000, anchorTop: 1000, bubbles, banner };
				const v = view(layout, anchored);
				await v.update(turn(4, bubbles.length));
				if (anchored) await v.place();
				await v.grow(10);
				await v.update(turn(4, bubbles.length, 'CLI error'));
				await v.frame(); await v.grow(0);
				v.scroll(layout.proseEnd);
				return { layout, v };
			}

			it.each([
				['an inline Error row, passive follow', false, 0], ['an inline Error row, active anchor', true, 0],
				['the banner shown, passive follow', false, 60], ['the banner shown, active anchor', true, 60]
			])('keeps a scrolled-up reader still when the last bubble is withdrawn under %s (S18)', async (_name, anchored, banner) => {
				const { layout, v } = await settledError(anchored, banner);
				v.scroll(-48);
				const away = v.read();
				await withdraw(v, layout, []);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
				expect(v.read().padding).toBeGreaterThanOrEqual(away.padding - 48);
				// Following again re-measures to the reserve and lift of a reader who never
				// left; one the withdrawal already brought to the bottom is not moved for it.
				v.scroll(layout.proseEnd);
				const resumed = v.read().scrollTop;
				await v.grow(0); await v.frame(); await v.grow(0);
				const stayed = await settledError(anchored, banner);
				await withdraw(stayed.v, stayed.layout, []);
				for (let i = 0; i < 3; i++) { await stayed.v.frame(); await stayed.v.grow(0); }
				expect(v.read()).toMatchObject({ padding: stayed.v.read().padding, errorOffset: stayed.v.read().errorOffset, scrollTop: resumed });
				expect(resumed).toBeGreaterThanOrEqual(stayed.v.read().scrollTop - 48);
			});

			it('keeps a scrolled-up reader still through a narrow drain with the banner stepped aside (S18)', async () => {
				const { layout, v } = await narrowLift(false);
				layout.banner = 0;
				await v.grow(0);
				v.scroll(-48);
				const away = v.read();
				await withdraw(v, layout, [180]);
				await withdraw(v, layout, []);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
			});

			/** The column narrows (the two bubbles wrap taller), then widens back. */
			async function narrowThenWiden(v: ReturnType<typeof view>, layout: Layout) {
				const steps: ReturnType<typeof v.read>[] = [];
				for (const [width, bubbles] of [[298, [180, 180]], [358, [80, 80]]] as const) {
					Object.assign(layout, { width, bubbles: [...bubbles] });
					await v.resize(400); await v.frame(); await v.grow(0);
					steps.push(v.read());
				}
				return steps;
			}

			// Scrolling up cancels an active anchor (handleScroll), so a detached reader is a passive one.
			it('gives a reader far above the queue back the reserve a narrow→wide resize grew, under an inline Error row (S19)', async () => {
				const { layout, v } = await settledError(false, 0, [80, 80]);
				v.scroll(-600);
				const away = v.read();
				const [narrow, wide] = await narrowThenWiden(v, layout);
				expect(narrow).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
				expect(narrow.padding).toBeGreaterThan(away.padding);
				expect(wide).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd, padding: away.padding });
			});

			it('keeps a reader near the bottom still through a narrow→wide resize and then a withdrawal (S19)', async () => {
				const { layout, v } = await settledError(false, 0, [80, 80]);
				v.scroll(-48);
				const away = v.read();
				const [narrow, wide] = await narrowThenWiden(v, layout);
				for (const step of [narrow, wide]) expect(step).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
				expect(wide.padding).toBe(away.padding);
				await withdraw(v, layout, [80]);
				await withdraw(v, layout, []);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
			});

			// The last bubble takes the controls' gap and breathing room with it: the
			// applied reserve falls by more than the bubble. Room between the two is the
			// boundary a share-sized hold missed.
			it('spends no more than the room below a reader when the last bubble goes, under an inline Error row (S20)', async () => {
				const { layout, v } = await settledError(false, 0, [80]);
				const room = 90;
				v.scroll(-room);
				const away = v.read();
				expect(away.padding - autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE).toBeGreaterThan(room);
				await withdraw(v, layout, []);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd, padding: away.padding - room });
				// Following again applies what a reader who never left has.
				v.scroll(layout.proseEnd);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				const stayed = await settledError(false, 0, [80]);
				await withdraw(stayed.v, stayed.layout, []);
				for (let i = 0; i < 3; i++) { await stayed.v.frame(); await stayed.v.grow(0); }
				expect(v.read().padding).toBe(stayed.v.read().padding);
			});

			it('gives up the room below a reader once, however many passes and small scrolls the banner hides (S20)', async () => {
				const { layout, v } = await settledError(false, 60, [200]);
				v.scroll(-50);
				await withdraw(v, layout, []);
				// The kept lift holds the painted reserve; the reader nudges up and passes run.
				for (let i = 0; i < 3; i++) { v.scroll(-10); await v.frame(); await v.grow(0); }
				const away = v.read();
				// The banner steps aside for an inline Error row shorter than itself.
				await v.update(turn(4, 0, 'CLI error'), () => { layout.banner = 0; layout.proseEnd += 40; });
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd + 40 });
			});

			it('keeps the detached reserve through zero geometry, and drops it on a session switch (S18)', async () => {
				const { layout, v } = await settledError(false, 0);
				v.scroll(-48);
				const away = v.read();
				await withdraw(v, layout, []);
				await v.resize(0); await v.frame(); await v.grow(0);
				await v.resize(400); await v.frame(); await v.grow(0);
				expect(v.read()).toMatchObject({ scrollTop: away.scrollTop, replyEnd: away.replyEnd });
				// Another chat, no queue and no error: nothing of the old slot is kept. The
				// switch opens it at the bottom, following (the session-switch effect).
				await v.update({ ...turn(4, 0), sessionId: 'other' });
				v.scroll(0);
				for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
				expect(v.read().padding).toBe(autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE);
			});

			it('leaves the floor of an ordinary dequeue still waiting for its transcript row', async () => {
				const { layout, v } = await streamingTurn();
				const before = v.read();
				// The bubble leaves; its row lands in a later commit.
				await v.update(turn(4, 3), () => { layout.bubbles = [80, 80, 80]; });
				await v.frame(); await v.grow(0);
				expect(v.read()).toMatchObject({ scrollTop: before.scrollTop, padding: before.padding });
			});

			it.each([['an active anchor', true], ['a passive follow', false]])(
				'releases the followed floor with the lift when the queue drains by withdrawal, under %s (S16)', async (_name, anchored) => {
					const { layout, v, lifted } = await narrowLift(anchored);
					await withdraw(v, layout, [180]);
					await withdraw(v, layout, []);
					for (let i = 0; i < 3; i++) { await v.frame(); await v.grow(0); }
					const drained = v.read();
					expect(drained.errorOffset).toBeLessThan(lifted.errorOffset);
					// The reserve holds the banner and no blank beyond: the floor gave the same amount back.
					expect(drained.padding).toBe(bannerReserve(drained.errorOffset));
					expect(drained.scrollTop).toBe(layout.proseEnd + drained.padding - layout.clientHeight);
					expect(drained.banner.top).toBeGreaterThanOrEqual(0);
					expect(drained.banner.bottom).toBeLessThanOrEqual(layout.clientHeight);
				});

			it.each([
				['a zero-height pane', async (v: ReturnType<typeof view>) => { await v.resize(0); await v.frame(); await v.grow(0); },
					async (v: ReturnType<typeof view>) => { await v.resize(400); await v.frame(); await v.grow(0); }],
				['a hidden column', async (v: ReturnType<typeof view>, layout: Layout) => { layout.width = 0; await v.grow(0); },
					async (v: ReturnType<typeof view>, layout: Layout) => { layout.width = 298; await v.grow(0); }]
			])('keeps a settled lift through %s (S17)', async (_name, hide, show) => {
				const { layout, v } = await narrowLift(true);
				await withdraw(v, layout, [180]);
				await withdraw(v, layout, []);
				await v.frame(); await v.grow(0);
				const settled = v.read();
				await hide(v, layout);
				await show(v, layout);
				expect(v.read()).toMatchObject({ errorOffset: settled.errorOffset, banner: settled.banner, padding: settled.padding });
			});
		});

		it('reserves nothing for an emptied slot while the banner steps aside for its Error row', async () => {
			const { layout, v } = await givenBack();
			layout.banner = 0;
			await v.update(turn(5, 2, 'CLI error'));
			await v.update(turn(6, 0, 'CLI error'), () => { layout.proseEnd += 60; layout.bubbles = []; });
			// The next turn's reply consumes the space the leaving bubble read in.
			await v.grow(200);
			// No bubble and no banner: the reply ends where an unqueued turn's would.
			expect(v.read().padding).toBe(autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE);
			expect(v.read().replyEnd).toBe(layout.clientHeight - autoScroll.MIN_TRANSCRIPT_TAIL_RESERVE);
		});
	});

	it('puts an error that lands before the give-back pass on screen, above the docked bubble (390x400)', async () => {
		const { layout, v } = await streamingTurn(400);
		await dequeue(v, layout, 5, [80]);
		layout.banner = 60;
		await v.update(turn(5, 1, 'CLI error'));
		const { banner, bubblesTop } = v.read();
		expect(banner.bottom).toBe(bubblesTop - ROW_GAP);
		expect(banner.top).toBeGreaterThanOrEqual(0);
		// Its reserve is the bubble and banner on screen now, not the hold's blank.
		const reserve = settledReserve([80]) + ROW_GAP + 60;
		expect(v.read()).toMatchObject({ padding: reserve, replyEnd: layout.clientHeight - reserve });
		await v.grow(20);
		expect(v.read()).toMatchObject({ padding: reserve, replyEnd: layout.clientHeight - reserve });
	});

	it('lets no frame of an older hold mark a newer hold painted', async () => {
		const { layout, v } = await streamingTurn();
		await dequeue(v, layout, 5, [80, 80, 80]);
		await v.frame();
		// Two sends grow the queue past the held slot; the next prompt lands.
		await v.update(turn(5, 5), () => { layout.bubbles = [80, 80, 80, 80, 80]; });
		await dequeue(v, layout, 6, [80, 80, 80, 80]);
		const painted = v.read();
		// The older hold's second frame runs before the newer hold has painted.
		await v.frame();
		await v.grow(5);
		expect(v.read()).toMatchObject({ slotTop: painted.slotTop, padding: painted.padding });
		await v.frame(); await v.grow(5);
		expect(v.read()).toMatchObject({ slotTop: painted.slotTop, padding: settledReserve([80, 80, 80, 80]) });
	});
});
