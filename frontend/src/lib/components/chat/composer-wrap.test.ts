/**
 * The composer's circle rules: Talk turns into Send, Stop follows while work
 * runs, and the text wraps the buttons below it only when it needs a second
 * line.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ARRIVAL_GUARD_MS, composerCircles, DOUBLE_PRESS_MS, createArrivalGuard, createWrapWatcher, MEASURED_CHARS, needsWrap, singleLineTextWidth } from './composer-wrap';

const idle = { callAvailable: true, hasContent: false, working: false, busy: false };

describe('composerCircles (Talk turns into Send)', () => {
	it('shows Talk while there is nothing to send and a call can start', () => {
		expect(composerCircles(idle)).toEqual(['talk']);
	});

	it('turns Talk into Send once there is sendable content', () => {
		expect(composerCircles({ ...idle, hasContent: true })).toEqual(['send']);
	});

	it('falls back to the resting Send where no call can start', () => {
		expect(composerCircles({ ...idle, callAvailable: false })).toEqual(['send']);
	});

	it('keeps Talk and adds Stop as the last circle while work runs with no draft', () => {
		expect(composerCircles({ ...idle, working: true })).toEqual(['talk', 'stop']);
	});

	it('shows Send then Stop while work runs with a draft (Send queues it)', () => {
		expect(composerCircles({ ...idle, working: true, hasContent: true })).toEqual(['send', 'stop']);
	});

	it('shows only Stop while work runs with no draft and no call', () => {
		expect(composerCircles({ ...idle, working: true, callAvailable: false })).toEqual(['stop']);
	});

	it('gives the slot to the spinner while a worktree is created or a send waits', () => {
		expect(composerCircles({ ...idle, busy: true })).toEqual(['busy']);
		expect(composerCircles({ ...idle, busy: true, hasContent: true, working: true })).toEqual(['busy']);
	});
});

describe('needsWrap', () => {
	it('keeps one line inline', () => {
		expect(needsWrap('Fix the tests', 1)).toBe(false);
	});

	it('wraps once the text needs a second line at the single-line width', () => {
		expect(needsWrap('A long request that runs past the edge', 2)).toBe(true);
	});

	it('wraps for a line break, including one the caret has just made', () => {
		expect(needsWrap('first\nsecond', 1)).toBe(true);
		expect(needsWrap('first line\n', 1)).toBe(true);
		expect(needsWrap('one line   ', 1)).toBe(false);
	});

	it('never wraps an empty composer', () => {
		expect(needsWrap('', 3)).toBe(false);
		expect(needsWrap('   \n ', 3)).toBe(false);
	});
});

describe('singleLineTextWidth', () => {
	const base = { text: 500, island: 672, circles: 70 };

	it('is the measured width while nothing has changed', () => {
		expect(singleLineTextWidth(base, { island: 672, circles: 70 })).toBe(500);
	});

	it('narrows by the width Stop adds to the circles, and widens back when it goes', () => {
		expect(singleLineTextWidth(base, { island: 672, circles: 108 })).toBe(462);
		expect(singleLineTextWidth(base, { island: 672, circles: 70 })).toBe(500);
	});

	it('follows the box when it is resized', () => {
		expect(singleLineTextWidth(base, { island: 772, circles: 70 })).toBe(600);
		expect(singleLineTextWidth(base, { island: 572, circles: 108 })).toBe(362);
	});
});

describe('createWrapWatcher', () => {
	afterEach(() => vi.unstubAllGlobals());

	/** A composer whose widths the test sets; lines are counted at 10px a character. */
	function composer(text: string) {
		const frames = new Map<number, FrameRequestCallback>();
		let nextFrame = 0;
		vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
			frames.set(++nextFrame, cb);
			return nextFrame;
		});
		vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => { frames.delete(id); }));
		const el = (width: number) => {
			const node = document.createElement('div');
			document.body.append(node);
			Object.defineProperty(node, 'clientWidth', { configurable: true, get: () => width });
			return node;
		};
		const state = { text, wrapped: false, island: 672, circles: 70, reserve: 0, editorWidth: 500, widths: [] as number[], measured: [] as number[] };
		const editor = el(0);
		Object.defineProperty(editor, 'clientWidth', { configurable: true, get: () => state.editorWidth });
		const island = el(0);
		Object.defineProperty(island, 'clientWidth', { configurable: true, get: () => state.island });
		const circles = el(0);
		Object.defineProperty(circles, 'offsetWidth', { configurable: true, get: () => state.circles });
		const watcher = createWrapWatcher({
			getText: () => state.text,
			getEditor: () => editor,
			getIsland: () => island,
			getCircles: () => circles,
			getReserve: () => state.reserve,
			isWrapped: () => state.wrapped,
			setWrapped: (w) => { state.wrapped = w; },
			countLines: (t, _e, width) => { state.widths.push(width); state.measured.push(t.length); return Math.ceil((t.length * 10) / width); }
		});
		const run = () => {
			watcher.schedule();
			const pending = [...frames.values()];
			frames.clear();
			pending.forEach((cb) => cb(0));
		};
		return { state, run, watcher };
	}

	it('settles a text update immediately and cancels its already queued observer frame', () => {
		const { state, watcher, run } = composer('x'.repeat(52));
		watcher.schedule();
		expect(state.wrapped).toBe(false);
		watcher.measureNow();
		expect(state.wrapped).toBe(true);
		expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
		state.editorWidth = 640;
		state.circles = 108;
		watcher.schedule();
		watcher.schedule();
		run();
		expect(state.widths).toEqual([500, 462]);
		state.text = '';
		watcher.measureNow();
		expect(state.wrapped).toBe(false);
	});

	it.each(['island', 'editorWidth'] as const)('keeps the current layout while %s is unmeasurable, then resumes', (box) => {
		const { state, run } = composer('short');
		state.wrapped = true;
		const width = state[box];
		state[box] = 0;
		run();
		expect(state.wrapped).toBe(true);
		expect(state.widths).toEqual([]);
		state[box] = width;
		run();
		expect(state.wrapped).toBe(false);
	});

	// #912: the hidden copy re-lays out all it holds, so a 200 KB paste copied
	// whole on every change cost a long task per keystroke.
	it('wraps text with a line break without laying out any of it', () => {
		const { state, run } = composer(`Pasted log${'\n'.repeat(1000)}end`);
		run();
		expect(state.wrapped).toBe(true);
		expect(state.measured).toEqual([]);
	});

	it('measures only the start of a very long single line', () => {
		const { state, run } = composer('word '.repeat(40_000));
		run();
		expect(state.wrapped).toBe(true);
		expect(state.measured).toEqual([MEASURED_CHARS]);
	});

	it('measures a short line whole, without its trailing spaces', () => {
		const { state, run } = composer(`fits${' '.repeat(5000)}`);
		run();
		expect(state.wrapped).toBe(false);
		expect(state.measured).toEqual([4]);
	});

	it('stays wrapped when Stop arrives, measuring at the narrower single-line width', () => {
		// 52 characters: 520px of text, past the 500px single-line width.
		const { state, run } = composer('x'.repeat(52));
		run();
		expect(state.wrapped).toBe(true);
		// Wrapped, the editor now spans the box; Stop widens the circles by 38px.
		state.editorWidth = 640;
		state.circles = 108;
		run();
		expect(state.widths.at(-1)).toBe(462);
		expect(state.wrapped).toBe(true);
	});

	it('unwraps once the text fits the single-line width again, without a flip', () => {
		const { state, run } = composer('x'.repeat(52));
		run();
		state.editorWidth = 640;
		state.circles = 108;
		run();
		// Stop leaves and the text shrinks to 49 characters: 490px fits in 500px.
		state.circles = 70;
		state.text = 'x'.repeat(49);
		run();
		expect(state.widths.at(-1)).toBe(500);
		expect(state.wrapped).toBe(false);
		// Measured again in the single-line layout: still one line, no re-wrap.
		state.editorWidth = 500;
		run();
		expect(state.wrapped).toBe(false);
	});

	// Stop takes no seat while hidden; a chat reserves its 38px in the
	// decision, so a one-line draft keeps its layout when Stop comes or goes.
	it.each([
		['a draft that fits beside Stop stays inline', 44, false],
		['a draft that only fits without Stop stays wrapped', 48, true]
	])('decides as if Stop were shown: %s', (_name, chars, wraps) => {
		const { state, run } = composer('x'.repeat(chars));
		state.reserve = 38;
		run();
		expect(state.wrapped).toBe(wraps);
		// Stop arrives: the circles widen by its seat, the reserve goes.
		if (!wraps) state.editorWidth = 462; else state.editorWidth = 640;
		state.circles = 108;
		state.reserve = 0;
		run();
		expect(state.wrapped).toBe(wraps);
		// Stop leaves again.
		if (!wraps) state.editorWidth = 500;
		state.circles = 70;
		state.reserve = 38;
		run();
		expect(state.wrapped).toBe(wraps);
		expect(state.widths).toEqual([462, 462, 462]);
	});
});

describe('createArrivalGuard (#1632)', () => {
	let clock = 0;
	const guard = () => createArrivalGuard(() => clock);
	afterEach(() => { clock = 0; });

	it('ignores a press on Talk just after it took the place Stop left', () => {
		const g = guard();
		g.update(['mic', 'talk', 'stop'], false);
		clock = 1000;
		g.update(['mic', 'talk'], false);
		expect(g.ignores('talk', 1120)).toBe(true);
		// Made before the change was drawn: aimed at Stop.
		expect(g.ignores('talk', 990)).toBe(true);
		expect(g.ignores('talk', 1000 + ARRIVAL_GUARD_MS)).toBe(false);
		// The mic slid over too, into Talk's old place.
		expect(g.ignores('mic', 1120)).toBe(true);
	});

	it('ignores a press on the mic just after it took a held Send\'s place', () => {
		const g = guard();
		g.update(['mic', 'send', 'stop'], false);
		clock = 500;
		g.update(['mic', 'stop'], false);
		expect(g.ignores('mic', 650)).toBe(true);
		expect(g.ignores('stop', 650)).toBe(false);
	});

	it('ignores the rest of a double click on Stop that took Send\'s place, even when the user caused it', () => {
		const g = guard();
		g.update(['mic', 'send'], false);
		clock = 300;
		// The user's own send: settled, so a single press on Stop counts.
		g.update(['mic', 'stop'], true);
		expect(g.ignores('stop', 400)).toBe(false);
		expect(g.ignores('stop', 400, 2)).toBe(true);
		expect(g.ignores('stop', 300 + DOUBLE_PRESS_MS, 2)).toBe(false);
		// A double click on a Stop that was already there is meant for it.
		clock = 5000;
		g.update(['mic', 'stop'], true);
		expect(g.ignores('stop', 5050, 2)).toBe(false);
	});

	it('ignores a press on Stop just after it replaced the spinner', () => {
		const g = guard();
		g.update(['mic', 'busy'], false);
		clock = 2000;
		g.update(['mic', 'stop'], false);
		expect(g.ignores('stop', 2100)).toBe(true);
		expect(g.ignores('stop', 2500)).toBe(false);
	});

	it('never guards Send, the first row, or a change the user made', () => {
		const g = guard();
		g.update(['mic', 'talk'], false);
		expect(g.ignores('talk', 1)).toBe(false);
		clock = 100;
		g.update(['mic', 'send', 'stop'], false);
		expect(g.ignores('send', 150)).toBe(false);
		expect(g.ignores('stop', 150)).toBe(true);
		// Typing settles the row: the user is looking at it.
		clock = 200;
		g.update(['mic', 'talk', 'stop'], true);
		expect(g.ignores('talk', 210)).toBe(false);
		expect(g.ignores('stop', 210)).toBe(false);
	});

	it('keeps an arrival through updates that change nothing', () => {
		const g = guard();
		g.update(['mic', 'send'], false);
		clock = 100;
		g.update(['mic', 'stop'], false);
		clock = 300;
		g.update(['mic', 'stop'], false);
		expect(g.ignores('stop', 350)).toBe(true);
		expect(g.ignores('stop', 100 + ARRIVAL_GUARD_MS)).toBe(false);
	});
});
