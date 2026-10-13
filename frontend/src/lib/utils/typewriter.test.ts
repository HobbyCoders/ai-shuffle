import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LOW_CORE_THRESHOLD, Typewriter, shouldAnimateText, snapToWordBoundary } from './typewriter';

/** Collect every reveal length the controller emits. */
function collector() {
	const lengths: number[] = [];
	return { lengths, onUpdate: (n: number) => lengths.push(n) };
}

describe('snapToWordBoundary', () => {
	it('advances to the end of the word it lands in', () => {
		const text = 'the quick brown fox';
		// Landing inside "quick" reveals through the following space.
		expect(snapToWordBoundary(text, 6)).toBe(10);
	});

	it('always makes progress', () => {
		const text = 'abcdefghij';
		expect(snapToWordBoundary(text, 3)).toBeGreaterThan(3);
	});

	it('does not swallow the buffer on a very long token', () => {
		const text = `${'x'.repeat(500)} tail`;
		expect(snapToWordBoundary(text, 10, 24)).toBe(34);
	});

	it('clamps at the end of the text', () => {
		expect(snapToWordBoundary('done', 4)).toBe(4);
		expect(snapToWordBoundary('done', 99)).toBe(4);
	});
});

describe('Typewriter (animated)', () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('shows everything already present on the first streaming update', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('already streamed content', true);
		expect(lengths).toEqual(['already streamed content'.length]);
		tw.destroy();
	});

	it('reveals later content in word chunks on a timer, not per frame', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('Hello', true);
		lengths.length = 0;

		tw.setTarget('Hello there my old friend', true);
		// Nothing is revealed synchronously — the cadence owns it.
		expect(lengths).toEqual([]);

		vi.advanceTimersByTime(30);
		expect(lengths.length).toBe(1);
		// Each step lands on a word boundary (or the end of the buffer).
		for (const length of lengths) {
			const revealed = 'Hello there my old friend'.slice(0, length);
			expect(revealed === 'Hello there my old friend' || revealed.endsWith(' ')).toBe(true);
		}

		vi.advanceTimersByTime(1000);
		expect(lengths.at(-1)).toBe('Hello there my old friend'.length);
		tw.destroy();
	});

	it('emits far fewer updates than a per-frame reveal would', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('x', true);
		lengths.length = 0;

		const words = Array.from({ length: 60 }, (_, i) => `word${i}`).join(' ');
		tw.setTarget(`x ${words}`, true);
		vi.advanceTimersByTime(1000);

		// The old loop emitted one reveal per character per animation frame.
		expect(lengths.length).toBeLessThanOrEqual(1000 / 30 + 1);
		expect(lengths.length).toBeLessThan(words.length);
		tw.destroy();
	});

	it('skips the animation for a large jump (reconnect / history load)', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('seed', true);
		lengths.length = 0;

		const huge = `seed${'y'.repeat(Typewriter.SKIP_CHARS + 10)}`;
		tw.setTarget(huge, true);
		expect(lengths).toEqual([huge.length]);
		tw.destroy();
	});

	it('reveals everything and stops the timer when streaming ends', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('one', true);
		tw.setTarget('one two three', true);
		tw.setTarget('one two three', false);
		expect(lengths.at(-1)).toBe('one two three'.length);

		const emitted = lengths.length;
		vi.advanceTimersByTime(500);
		expect(lengths.length).toBe(emitted);
		tw.destroy();
	});

	it('stops scheduling work once destroyed', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: true, chunkMs: 30 });
		tw.setTarget('a', true);
		tw.setTarget('a bunch more words arriving', true);
		tw.destroy();
		const emitted = lengths.length;
		vi.advanceTimersByTime(1000);
		expect(lengths.length).toBe(emitted);
	});
});

describe('Typewriter (opted out)', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it('passes content straight through with no timer at all', () => {
		const { lengths, onUpdate } = collector();
		const tw = new Typewriter(onUpdate, { animate: false });
		expect(tw.animating).toBe(false);

		tw.setTarget('Hello', true);
		tw.setTarget('Hello there my old friend', true);
		expect(lengths).toEqual([5, 'Hello there my old friend'.length]);

		vi.advanceTimersByTime(1000);
		expect(lengths).toEqual([5, 'Hello there my old friend'.length]);
		tw.destroy();
	});
});

describe('shouldAnimateText', () => {
	const originalMatchMedia = window.matchMedia;
	const originalCores = Object.getOwnPropertyDescriptor(
		Object.getPrototypeOf(navigator),
		'hardwareConcurrency'
	);

	function stubMatchMedia(reduced: boolean) {
		window.matchMedia = ((query: string) => ({
			matches: reduced && query.includes('prefers-reduced-motion'),
			media: query,
			addEventListener() {},
			removeEventListener() {},
			addListener() {},
			removeListener() {},
			onchange: null,
			dispatchEvent: () => false
		})) as unknown as typeof window.matchMedia;
	}

	function stubCores(cores: number) {
		Object.defineProperty(navigator, 'hardwareConcurrency', {
			configurable: true,
			get: () => cores
		});
	}

	afterEach(() => {
		window.matchMedia = originalMatchMedia;
		if (originalCores) {
			Object.defineProperty(Object.getPrototypeOf(navigator), 'hardwareConcurrency', originalCores);
		}
		Reflect.deleteProperty(navigator, 'hardwareConcurrency');
	});

	it('is off under prefers-reduced-motion', () => {
		stubMatchMedia(true);
		stubCores(16);
		expect(shouldAnimateText()).toBe(false);
	});

	it('is off on low-core machines', () => {
		stubMatchMedia(false);
		stubCores(LOW_CORE_THRESHOLD);
		expect(shouldAnimateText()).toBe(false);
	});

	it('is on for a capable machine that has not asked for less motion', () => {
		stubMatchMedia(false);
		stubCores(LOW_CORE_THRESHOLD + 4);
		expect(shouldAnimateText()).toBe(true);
	});
});
