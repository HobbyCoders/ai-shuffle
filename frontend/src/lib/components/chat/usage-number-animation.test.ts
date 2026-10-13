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
	localStorage.clear();
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
	it('persists across new panel instances and keeps upward and downward flips distinct', () => {
		const node = document.createElement('span');
		createUsageNumberAnimation()(node, { key: 'install:session', text: '(9%)' }).destroy();
		const action = createUsageNumberAnimation()(node, { key: 'install:session', text: '(100%)' });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe('(9%)');
		expect(node.dataset.flipDirection).toBe('up');
		expect(node.querySelectorAll('.usage-flip-digit')).toHaveLength(3);
		advance(0); advance(1650);
		expect(node.textContent).toBe('(100%)');
		action.update({ key: 'install:session', text: '(4%)' });
		expect(node.dataset.flipDirection).toBe('down');
		node.className = 'ctx-pct danger'; // Svelte owns the action host's class attribute.
		expect(node.querySelector('.usage-number-flipping')).not.toBeNull();
		action.destroy();
		expect(frames.size).toBe(0);
	});
	it('ignores malformed storage and limits saved history', () => {
		localStorage.setItem('aihub.usageLastShown.v1', '{broken');
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		for (let i = 0; i < 205; i++) action(node, { key: String(i), text: '20%' }).destroy();
		expect(JSON.parse(localStorage.getItem('aihub.usageLastShown.v1')!)).toHaveLength(200);
	});
	it('works when storage is unavailable', () => {
		const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
		const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		action(node, { key: 'a', text: '20%' }).destroy();
		const next = action(node, { key: 'a', text: '10%' });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe('20%');
		next.destroy(); get.mockRestore(); set.mockRestore();
	});

	it.each([
		['24% left', '18% left'], ['$22.91 left', '$20.44 left'],
		['4% left', '100% left'], ['$3.10 used · $6.90 left', '$4.20 used · $5.80 left'],
		['$0.00 left', '$-2.00 left']
	])('flips from %s to %s on remount after an opening pause', (from, to) => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		const first = action(node, { key: 'provider', text: from });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(from);
		expect(frames.size).toBe(0);
		first.destroy();
		const reopened = action(node, { key: 'provider', text: to });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(from);
		advance(0); advance(450);
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(from);
		expect(node.querySelectorAll('.flap').length).toBeGreaterThan(0);
		advance(900);
		expect(node.querySelector('.usage-number-flipping') !== null).toBe(true);
		expect(node.querySelector('.usage-number-label')!.textContent).not.toBe(to);
		reopened.update({ key: 'provider', text: to });
		expect(frames.size).toBe(1);
		advance(1650);
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(to);
		expect(frames.size).toBe(0);
		reopened.destroy();
	});
	it('does not replay unchanged values or cross providers, installs, or units', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		for (const [key, text] of [['a', '24% left'], ['a', '24% left'], ['b', '10% left'], ['a', '$10.00 left']]) {
			action(node, { key, text }).destroy();
			expect(node.querySelector('.usage-number-label')!.textContent).toBe(text);
			expect(frames.size).toBe(0);
		}
	});
	it('cancels on close and resumes from the last visible value', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		action(node, { key: 'a', text: '90% left' }).destroy();
		const running = action(node, { key: 'a', text: '10% left' });
		advance(0); advance(1150);
		const last = node.querySelector('.usage-number-label')!.textContent;
		running.destroy();
		expect(frames.size).toBe(0);
		const reopened = action(node, { key: 'a', text: '5% left' });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(last);
		reopened.update({ key: 'a', text: '3% left' });
		expect(node.querySelector('.usage-number-label')!.textContent).toBe(last);
		advance(0); advance(1650);
		expect(node.querySelector('.usage-number-label')!.textContent).toBe('3% left');
		expect(frames.size).toBe(0);
		reopened.destroy();
	});
	it('honors reduced motion initially and when enabled during a tween', () => {
		const action = createUsageNumberAnimation();
		const node = document.createElement('span');
		action(node, { key: 'a', text: '90% left' }).destroy();
		motion.matches = true;
		action(node, { key: 'a', text: '80% left' }).destroy();
		expect(node.querySelector('.usage-number-label')!.textContent).toBe('80% left');
		expect(frames.size).toBe(0);
		motion.matches = false;
		const running = action(node, { key: 'a', text: '10% left' });
		motion.matches = true;
		motion.addEventListener.mock.calls.at(-1)![1]();
		expect(node.querySelector('.usage-number-label')!.textContent).toBe('10% left');
		expect(frames.size).toBe(0);
		running.destroy();
		expect(motion.removeEventListener).toHaveBeenCalled();
	});
});