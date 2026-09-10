import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createUsageNumberAnimation } from './usage-number-animation';

let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let motion: { matches: boolean; addEventListener: ReturnType<typeof vi.fn>; removeEventListener: ReturnType<typeof vi.fn> };
function advance(time: number) {
	const pending = [...frames.values()];
	frames.clear();
	pending.forEach(callback => callback(time));
}
beforeEach(() => {
	frames = new Map(); nextFrame = 0;
	motion = { matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() };
	vi.stubGlobal('matchMedia', () => motion);
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
		frames.set(++nextFrame, callback); return nextFrame;
	});
	vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});
afterEach(() => vi.unstubAllGlobals());

describe('usage number animation', () => {
	it.each([
		['24% left', '18% left'], ['$22.91 left', '$20.44 left'],
		['4% left', '100% left'], ['$3.10 used · $6.90 left', '$4.20 used · $5.80 left'],
		['$0.00 left', '$-2.00 left']
	])('counts from %s to %s on remount', (from, to) => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		const first = action(node, { key: 'provider', text: from });
		expect(node.textContent).toBe(from);
		expect(frames.size).toBe(0);
		first.destroy();
		const reopened = action(node, { key: 'provider', text: to });
		expect(node.textContent).toBe(from);
		advance(0); advance(350);
		expect(node.textContent).not.toBe(from);
		expect(node.textContent).not.toBe(to);
		reopened.update({ key: 'provider', text: to });
		expect(frames.size).toBe(1);
		advance(700);
		expect(node.textContent).toBe(to);
		expect(frames.size).toBe(0);
		reopened.destroy();
	});
	it('does not replay unchanged values or cross providers, installs, or units', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		for (const [key, text] of [['a', '24% left'], ['a', '24% left'], ['b', '10% left'], ['a', '$10.00 left']]) {
			action(node, { key, text }).destroy();
			expect(node.textContent).toBe(text);
			expect(frames.size).toBe(0);
		}
	});
	it('cancels on close and resumes from the last visible value', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		action(node, { key: 'a', text: '90% left' }).destroy();
		const running = action(node, { key: 'a', text: '10% left' });
		advance(0); advance(200);
		const last = node.textContent;
		running.destroy();
		expect(frames.size).toBe(0);
		const reopened = action(node, { key: 'a', text: '5% left' });
		expect(node.textContent).toBe(last);
		reopened.update({ key: 'a', text: '3% left' });
		expect(node.textContent).toBe(last);
		advance(0); advance(700);
		expect(node.textContent).toBe('3% left');
		expect(frames.size).toBe(0);
		reopened.destroy();
	});
	it('honors reduced motion initially and when enabled during a tween', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		action(node, { key: 'a', text: '90% left' }).destroy();
		motion.matches = true;
		action(node, { key: 'a', text: '80% left' }).destroy();
		expect(node.textContent).toBe('80% left');
		expect(frames.size).toBe(0);
		motion.matches = false;
		const running = action(node, { key: 'a', text: '10% left' });
		motion.matches = true;
		motion.addEventListener.mock.calls.at(-1)![1]();
		expect(node.textContent).toBe('10% left');
		expect(frames.size).toBe(0);
		running.destroy();
		expect(motion.removeEventListener).toHaveBeenCalled();
	});
});
