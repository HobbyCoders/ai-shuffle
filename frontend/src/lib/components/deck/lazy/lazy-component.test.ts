import { describe, expect, it, vi } from 'vitest';
import { createLazyComponent, preloadWhenIdle } from './lazy-component';

const COMPONENT = { name: 'FakePanel' };

describe('createLazyComponent', () => {
	it('does not import anything until asked', () => {
		const loader = vi.fn(async () => ({ default: COMPONENT }));
		const handle = createLazyComponent('x', loader);
		expect(loader).not.toHaveBeenCalled();
		expect(handle.resolved).toBeNull();
		expect(handle.loading).toBe(false);
	});

	it('resolves the module default and caches it for synchronous reads', async () => {
		const loader = vi.fn(async () => ({ default: COMPONENT }));
		const handle = createLazyComponent('x', loader);

		await expect(handle.load()).resolves.toBe(COMPONENT);
		expect(handle.resolved).toBe(COMPONENT);
		expect(handle.error).toBeNull();

		// A second load is a cache hit, not a second chunk fetch.
		await expect(handle.load()).resolves.toBe(COMPONENT);
		expect(loader).toHaveBeenCalledTimes(1);
	});

	it('dedupes concurrent loads onto one in-flight import', async () => {
		let release: (v: { default: unknown }) => void = () => {};
		const loader = vi.fn(() => new Promise<{ default: unknown }>((res) => (release = res)));
		const handle = createLazyComponent('x', loader);

		const a = handle.load();
		const b = handle.load();
		handle.preload();
		expect(handle.loading).toBe(true);

		release({ default: COMPONENT });
		await expect(a).resolves.toBe(COMPONENT);
		await expect(b).resolves.toBe(COMPONENT);
		expect(loader).toHaveBeenCalledTimes(1);
	});

	it('awaits `prepare` before the panel is considered ready', async () => {
		const order: string[] = [];
		const handle = createLazyComponent(
			'docs',
			async () => {
				order.push('component');
				return { default: COMPONENT };
			},
			{
				prepare: async () => {
					await Promise.resolve();
					order.push('prepare');
				}
			}
		);

		await handle.load();
		expect(order).toContain('prepare');
		expect(order).toContain('component');
	});

	it('propagates a chunk-load failure and does NOT cache the rejection', async () => {
		const loader = vi
			.fn()
			.mockRejectedValueOnce(new Error('Failed to fetch dynamically imported module'))
			.mockResolvedValueOnce({ default: COMPONENT });
		const handle = createLazyComponent('x', loader as never);

		await expect(handle.load()).rejects.toThrow(/dynamically imported/);
		expect(handle.error).toBeInstanceOf(Error);
		expect(handle.resolved).toBeNull();
		expect(handle.loading).toBe(false);

		// The retry path must actually re-attempt the import.
		await expect(handle.load()).resolves.toBe(COMPONENT);
		expect(handle.error).toBeNull();
		expect(loader).toHaveBeenCalledTimes(2);
	});

	it('preload never rejects, even when the chunk fetch fails', async () => {
		const loader = vi.fn().mockRejectedValue(new Error('offline'));
		const handle = createLazyComponent('x', loader as never);
		const unhandled = vi.fn();
		process.on('unhandledRejection', unhandled);

		handle.preload();
		await new Promise((r) => setTimeout(r, 0));

		process.off('unhandledRejection', unhandled);
		expect(unhandled).not.toHaveBeenCalled();
	});
});

describe('preloadWhenIdle', () => {
	it('warms every handle in order and stops when cancelled', async () => {
		vi.useFakeTimers();
		try {
			const loaded: string[] = [];
			const make = (id: string) =>
				createLazyComponent(id, async () => {
					loaded.push(id);
					return { default: COMPONENT };
				});
			const handles = [make('a'), make('b')];

			const cancel = preloadWhenIdle(handles, { startDelayMs: 10 });
			expect(loaded).toEqual([]);

			await vi.advanceTimersByTimeAsync(10);
			expect(loaded).toContain('a');

			await vi.advanceTimersByTimeAsync(500);
			expect(loaded).toEqual(['a', 'b']);
			cancel();
		} finally {
			vi.useRealTimers();
		}
	});

	it('cancelling before the start delay warms nothing', async () => {
		vi.useFakeTimers();
		try {
			const loaded: string[] = [];
			const handle = createLazyComponent('a', async () => {
				loaded.push('a');
				return { default: COMPONENT };
			});
			const cancel = preloadWhenIdle([handle], { startDelayMs: 50 });
			cancel();
			await vi.advanceTimersByTimeAsync(200);
			expect(loaded).toEqual([]);
		} finally {
			vi.useRealTimers();
		}
	});
});
