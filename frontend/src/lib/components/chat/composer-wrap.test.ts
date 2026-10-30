/**
 * The composer's circle rules: Talk turns into Send, Stop follows while work
 * runs, and the text wraps the buttons below it only when it needs a second
 * line.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { composerCircles, createWrapWatcher, MEASURED_CHARS, needsWrap, singleLineTextWidth } from './composer-wrap';

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
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length; });
		vi.stubGlobal('cancelAnimationFrame', () => {});
		const el = (width: number) => {
			const node = document.createElement('div');
			document.body.append(node);
			Object.defineProperty(node, 'clientWidth', { configurable: true, get: () => width });
			return node;
		};
		const state = { text, wrapped: false, island: 672, circles: 70, editorWidth: 500, widths: [] as number[], measured: [] as number[] };
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
			isWrapped: () => state.wrapped,
			setWrapped: (w) => { state.wrapped = w; },
			countLines: (t, _e, width) => { state.widths.push(width); state.measured.push(t.length); return Math.ceil((t.length * 10) / width); }
		});
		const run = () => { watcher.schedule(); frames.splice(0).forEach((cb) => cb(0)); };
		return { state, run, watcher };
	}

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
});
