/**
 * Regression counters for the shared run-header frame (#1282): one scroll
 * frame measures every open header before writing any, reads the scroller once,
 * coalesces bursts into one frame and leaves far off-screen headers alone.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { runHeaderStats, trackRunHeader } from './run-header-placement';

type Log = string[];

let frames: FrameRequestCallback[] = [];
let raf: typeof globalThis.requestAnimationFrame;
let observers: Array<{ cb: IntersectionObserverCallback; targets: Set<Element> }> = [];
const g = globalThis as unknown as { IntersectionObserver?: unknown };
let previousIO: unknown;

beforeEach(() => {
	frames = [];
	observers = [];
	raf = globalThis.requestAnimationFrame;
	globalThis.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
	previousIO = g.IntersectionObserver;
	g.IntersectionObserver = class {
		entry: { cb: IntersectionObserverCallback; targets: Set<Element> };
		constructor(cb: IntersectionObserverCallback) {
			this.entry = { cb, targets: new Set() };
			observers.push(this.entry);
		}
		observe(target: Element) { this.entry.targets.add(target); }
		unobserve(target: Element) { this.entry.targets.delete(target); }
		disconnect() { this.entry.targets.clear(); }
	};
});

afterEach(() => {
	globalThis.requestAnimationFrame = raf;
	if (previousIO) g.IntersectionObserver = previousIO;
	else delete g.IntersectionObserver;
	document.body.innerHTML = '';
});

const flush = () => frames.splice(0).forEach((cb) => cb(0));

/** An element whose geometry reads and transform writes are logged. */
function logged(log: Log, name: string, top: number, bottom: number, height = 0): HTMLElement {
	const el = document.createElement('div');
	Object.defineProperty(el, 'getBoundingClientRect', {
		value: () => { log.push(`read:${name}`); return { top, bottom, height: bottom - top }; },
		configurable: true
	});
	Object.defineProperty(el, 'offsetHeight', {
		get: () => { log.push(`read:${name}`); return height; },
		configurable: true
	});
	let transform = '';
	Object.defineProperty(el.style, 'transform', {
		get: () => transform,
		set: (value: string) => { log.push(`write:${name}`); transform = value; },
		configurable: true
	});
	return el;
}

function setup(log: Log) {
	const scroller = logged(log, 'scroller', 0, 600);
	document.body.appendChild(scroller);
	// Three open runs: the first is being pushed off, the others sit below.
	const runs = [
		{ head: logged(log, 'head0', 0, 32, 32), end: logged(log, 'end0', 0, 20) },
		{ head: logged(log, 'head1', 0, 32, 32), end: logged(log, 'end1', 0, 500) },
		{ head: logged(log, 'head2', 0, 32, 32), end: logged(log, 'end2', 0, 900) }
	];
	for (const { head, end } of runs) scroller.append(head, end);
	const tracked = runs.map(({ head, end }) => trackRunHeader({ node: head, scroller, end: () => end }));
	return { scroller, runs, tracked };
}

describe('run header placement', () => {
	it('measures every header before writing any, reading the scroller once per frame', () => {
		const log: Log = [];
		const { scroller, runs, tracked } = setup(log);
		flush();
		log.length = 0;
		Object.defineProperty(runs[0].end, 'getBoundingClientRect', {
			value: () => { log.push('read:end0'); return { top: 0, bottom: 10, height: 10 }; },
			configurable: true
		});
		scroller.dispatchEvent(new Event('scroll'));
		flush();
		const firstWrite = log.findIndex((entry) => entry.startsWith('write:'));
		const lastRead = log.findLastIndex((entry) => entry.startsWith('read:'));
		expect(firstWrite).toBeGreaterThan(lastRead);
		expect(log.filter((entry) => entry === 'read:scroller')).toHaveLength(1);
		expect(runs[0].head.style.transform).toBe('translateY(-22px)');
		// Headers whose placement did not change are not rewritten.
		expect(log.filter((entry) => entry.startsWith('write:'))).toEqual(['write:head0']);
		tracked.forEach((t) => t.release());
	});

	it('coalesces a burst of scroll events into one frame', () => {
		const log: Log = [];
		const { scroller, tracked } = setup(log);
		flush();
		const before = runHeaderStats.flushes;
		for (let i = 0; i < 10; i++) scroller.dispatchEvent(new Event('scroll'));
		expect(frames).toHaveLength(1);
		flush();
		expect(runHeaderStats.flushes - before).toBe(1);
		tracked.forEach((t) => t.release());
	});

	it('does not measure a header that is far off-screen, and places it on return', () => {
		const log: Log = [];
		const { scroller, runs, tracked } = setup(log);
		flush();
		const io = observers.at(-1)!;
		io.cb([{ target: runs[2].head, isIntersecting: false } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
		flush();
		log.length = 0;
		scroller.dispatchEvent(new Event('scroll'));
		flush();
		expect(log.some((entry) => entry.endsWith('2'))).toBe(false);
		expect(log).toContain('read:end0');
		io.cb([{ target: runs[2].head, isIntersecting: true } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
		flush();
		expect(log).toContain('read:end2');
		tracked.forEach((t) => t.release());
	});

	it('removes its listeners once the last header is released', () => {
		const log: Log = [];
		const { scroller, tracked } = setup(log);
		flush();
		tracked.forEach((t) => t.release());
		scroller.dispatchEvent(new Event('scroll'));
		expect(frames).toHaveLength(0);
	});
});
