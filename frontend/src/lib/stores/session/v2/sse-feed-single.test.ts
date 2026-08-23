/**
 * Single-feed invariant + revive coalescing for the SSE feed client.
 *
 * One browser instance must never hold more than one live feed per session,
 * and a burst of revive triggers (visibilitychange + online + pageshow + a WS
 * reconnect) must produce one reconnect, not one per trigger.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createSseFeed, liveFeedCount, REVIVE_COALESCE_MS } from './sse-feed';

// Emit the given chunks then park (never close) — a live tail.
function streamThenPending(chunks: string[]): ReadableStream<Uint8Array> {
	const enc = new TextEncoder();
	let i = 0;
	return new ReadableStream({
		pull(controller) {
			if (i < chunks.length) {
				controller.enqueue(enc.encode(chunks[i++]));
				return;
			}
			return new Promise<void>(() => {});
		},
	});
}

const tick = async (n = 30) => {
	for (let i = 0; i < n; i++) await Promise.resolve();
};

function setupFetch() {
	const urls: string[] = [];
	const signals: AbortSignal[] = [];
	const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
		urls.push(url);
		if (init?.signal) signals.push(init.signal);
		return {
			ok: true,
			status: 200,
			body:
				urls.length === 1
					? streamThenPending(['id: 1\ndata: {"seq":1}\n\n', 'id: 2\ndata: {"seq":2}\n\n'])
					: streamThenPending([]),
		};
	});
	vi.stubGlobal('fetch', fetchMock);
	return { urls, signals };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('single live feed per session', () => {
	it('a second feed for the same session closes the first', async () => {
		const { urls, signals } = setupFetch();
		const first = createSseFeed('sess-A', { onEvent: () => {} });
		await tick();
		expect(liveFeedCount()).toBe(1);
		expect(first.running).toBe(true);

		const second = createSseFeed('sess-A', { onEvent: () => {} });
		await tick();
		expect(first.running).toBe(false);
		expect(signals[0].aborted).toBe(true); // the first reader was torn down
		expect(second.running).toBe(true);
		expect(liveFeedCount()).toBe(1);
		expect(urls).toHaveLength(2);

		second.stop();
		expect(liveFeedCount()).toBe(0);
	});

	it('feeds for different sessions coexist', async () => {
		setupFetch();
		const a = createSseFeed('sess-A', { onEvent: () => {} });
		const b = createSseFeed('sess-B', { onEvent: () => {} });
		await tick();
		expect(liveFeedCount()).toBe(2);
		expect(a.running && b.running).toBe(true);
		a.stop();
		b.stop();
		expect(liveFeedCount()).toBe(0);
	});

	it('stopping a stale handle does not unregister the live replacement', async () => {
		setupFetch();
		const first = createSseFeed('sess-A', { onEvent: () => {} });
		const second = createSseFeed('sess-A', { onEvent: () => {} });
		await tick();
		first.stop(); // already stopped by the replacement; must be a no-op
		expect(liveFeedCount()).toBe(1);
		expect(second.running).toBe(true);
		second.stop();
		expect(liveFeedCount()).toBe(0);
	});
});

describe('revive coalescing', () => {
	it('a burst of revive triggers reconnects once', async () => {
		const { urls } = setupFetch();
		const feed = createSseFeed('sess-A', { onEvent: () => {} });
		await tick();
		expect(urls).toHaveLength(1);

		// Laptop wake: every trigger lands inside the same second.
		document.dispatchEvent(new Event('visibilitychange'));
		window.dispatchEvent(new Event('online'));
		feed.revive();
		feed.revive();
		await tick();

		expect(urls).toHaveLength(2);
		expect(urls[1]).toContain('from_seq=2');

		// Past the window a revive is honored again.
		await vi.advanceTimersByTimeAsync(REVIVE_COALESCE_MS);
		feed.revive();
		await tick();
		expect(urls).toHaveLength(3);
		feed.stop();
	});
});
