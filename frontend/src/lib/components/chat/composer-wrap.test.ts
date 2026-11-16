/**
 * The composer's circle rules: Talk turns into Send, Stop follows while work
 * runs, and the text wraps the buttons below it only when it needs a second
 * line.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ARRIVAL_GUARD_MS, composerCircles, DOUBLE_PRESS_MS, createArrivalGuard, createPressTracker, RELEASE_TO_CLICK_MS, createWrapWatcher, MEASURED_CHARS, needsWrap, singleLineTextWidth } from './composer-wrap';

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

	it('arms nothing where a seat was empty: the row growing leaves the mic alone', () => {
		// Work starting in another tab: mic · Talk becomes mic · Talk · Stop.
		const g = guard();
		g.update(['mic', 'talk'], false);
		clock = 1000;
		g.update(['mic', 'talk', 'stop'], false);
		// The mic moved into a seat no button held: a tap there meant nothing else.
		expect(g.ignores('mic', 1100)).toBe(false);
		expect(g.ignores('mic', 1100, 2)).toBe(false);
		// Talk took the mic's seat and Stop took Talk's: replacements stay guarded.
		expect(g.ignores('talk', 1100)).toBe(true);
		expect(g.ignores('stop', 1100)).toBe(true);
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

describe('createPressTracker (#1632)', () => {
	const stop = { name: 'stop' };
	const talk = { name: 'talk' };

	it('gives a click the press of its own pointer, once', () => {
		const t = createPressTracker<object>();
		t.down(1, 1350, talk);
		t.down(7, 1360, stop);
		expect(t.take(1, 1450, talk)).toEqual({ at: 1350, target: talk });
		expect(t.take(1, 1460, talk)).toBeNull();
		expect(t.take(7, 1470, stop)).toEqual({ at: 1360, target: stop });
	});

	it('keeps only the latest press of a pointer and forgets a cancelled one', () => {
		const t = createPressTracker<object>();
		t.down(1, 100, stop);
		t.down(1, 900, talk);
		expect(t.take(1, 950, talk)).toEqual({ at: 900, target: talk });
		t.down(2, 1000, talk);
		t.cancel(2);
		expect(t.take(2, 1100, talk)).toBeNull();
	});

	it('gives no press to a click from no pointer, or to one made before the press', () => {
		const t = createPressTracker<object>();
		t.down(1, 100, talk);
		expect(t.take(-1, 200, talk)).toBeNull();
		expect(t.take(1, 50, talk)).toBeNull();
	});

	it('keeps a press however long it is held, until its release', () => {
		const t = createPressTracker<object>();
		t.down(1, 1100, talk);
		// Other pointers coming and going do not drop a live press.
		t.down(2, 9000, stop);
		t.up(2, 9050);
		t.down(3, 9000 + RELEASE_TO_CLICK_MS + 5000, stop);
		t.up(1, 1100 + 30_000);
		expect(t.take(1, 1100 + 30_000, talk)).toEqual({ at: 1100, target: talk, releasedAt: 1100 + 30_000 });
	});

	it('drops a press released without a click once its click can no longer come', () => {
		// Pressed on Talk, dragged off, released elsewhere: no click follows.
		const t = createPressTracker<object>();
		t.down(1, 100, talk);
		t.up(1, 300);
		expect(t.take(1, 300 + RELEASE_TO_CLICK_MS + 1, talk)).toBeNull();
		expect(t.take(undefined, 300 + RELEASE_TO_CLICK_MS + 1, talk)).toBeNull();
		// Abandoned presses do not pile up: the next press sweeps them out.
		for (let id = 10; id < 20; id++) { t.down(id, 400 + id, talk); t.up(id, 450 + id); }
		t.down(99, 5000, stop);
		expect(t.size).toBe(1);
		t.clear();
		expect(t.size).toBe(0);
	});

	it('keeps two pointers down at once apart, and a reused pointer id takes its new press', () => {
		const t = createPressTracker<object>();
		t.down(1, 100, talk);
		t.down(2, 150, stop);
		t.up(2, 200);
		expect(t.take(2, 200, stop)).toEqual({ at: 150, target: stop, releasedAt: 200 });
		t.up(1, 400);
		expect(t.take(1, 400, talk)).toEqual({ at: 100, target: talk, releasedAt: 400 });
		// A mouse or pen reuses its id: a new press replaces the old one.
		t.down(1, 1000, stop);
		t.down(1, 2000, talk);
		expect(t.take(1, 2050, talk)).toEqual({ at: 2000, target: talk });
	});

	it('matches a click without a pointer id only to the latest press on the same button', () => {
		const t = createPressTracker<object>();
		t.down(3, 100, stop);
		expect(t.take(undefined, 200, talk)).toBeNull();
		expect(t.take(undefined, 200, stop)).toEqual({ at: 100, target: stop });
		expect(t.take(undefined, 300, stop)).toBeNull();
	});
});

describe('createArrivalGuard at a queue handoff', () => {
	let clock = 0;
	const guard = () => createArrivalGuard(() => clock);
	afterEach(() => { clock = 0; });
	// Stop leaves for ~150 ms between a turn and the queued one, then comes back.
	const handoff = () => {
		const g = guard();
		g.update(['mic', 'talk', 'stop'], false);
		clock = 1000;
		g.update(['mic', 'talk'], false);
		clock = 1150;
		g.update(['mic', 'talk', 'stop'], false);
		return g;
	};

	it('never ignores Stop coming back to the seat it held moments ago', () => {
		const g = handoff();
		expect(g.ignores('stop', 1170)).toBe(false);
		expect(g.ignores('stop', 1450)).toBe(false);
		expect(g.ignores('stop', 1170, 2)).toBe(false);
		expect(g.ignores('talk', 1170)).toBe(false);
	});

	it('still ignores a press on the brief Talk in between', () => {
		const g = guard();
		g.update(['mic', 'talk', 'stop'], false);
		clock = 1000;
		g.update(['mic', 'talk'], false);
		expect(g.ignores('talk', 1050)).toBe(true);
		expect(g.ignores('mic', 1050)).toBe(true);
	});

	it('keeps a returning button\'s own guard when it had only just arrived', () => {
		const g = guard();
		g.update(['mic', 'send'], false);
		clock = 1000;
		g.update(['mic', 'stop'], false);
		clock = 1100;
		g.update(['mic', 'talk'], false);
		clock = 1200;
		g.update(['mic', 'stop'], false);
		expect(g.ignores('stop', 1300)).toBe(true);
		expect(g.ignores('stop', 1000 + ARRIVAL_GUARD_MS)).toBe(false);
	});

	it('guards a button that returns after the window, or to a seat another button held', () => {
		const g = guard();
		g.update(['mic', 'talk', 'stop'], false);
		clock = 1000;
		g.update(['mic', 'talk'], false);
		clock = 1000 + ARRIVAL_GUARD_MS;
		g.update(['mic', 'talk', 'stop'], false);
		expect(g.ignores('stop', clock + 20)).toBe(true);
		// Send held the seat before: Stop taking it is an arrival.
		const h = guard();
		h.update(['mic', 'send'], false);
		clock = 5000;
		h.update(['mic', 'talk'], false);
		clock = 5100;
		h.update(['mic', 'stop'], false);
		expect(h.ignores('stop', 5120)).toBe(true);
	});

	it('forgets who held a seat once the row is placed again', () => {
		const g = guard();
		g.update(['mic', 'talk', 'stop'], false);
		clock = 1000;
		g.update(['mic', 'talk'], true);
		clock = 1100;
		g.update(['mic', 'talk', 'stop'], false);
		expect(g.ignores('stop', 1120)).toBe(true);
	});
});
