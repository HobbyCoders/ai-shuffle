/**
 * Idle-watchdog tests for the V2 SSE feed client.
 *
 * A healthy stream emits a frame or `:` keepalive comment at least every ~15s.
 * A half-open TCP connection (peer gone, no RST/FIN) leaves `reader.read()`
 * hanging forever with no error and no `done`, so the reconnect loop never fires
 * and the transcript strands. The watchdog force-reconnects after >45s of
 * silence (tick ~10s), and must NOT fire while keepalives keep arriving.
 *
 * These drive `createSseFeed` directly with a stubbed fetch + a ReadableStream we
 * control, under vitest fake timers (which also fake `Date`, so `Date.now()`
 * advances with the timer clock the watchdog compares against).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createSseFeed, IDLE_TIMEOUT_MS } from './sse-feed';

// A ReadableStream that never yields or closes — models a half-open reader whose
// `read()` promise hangs forever. The watchdog is the only thing that can
// dislodge it.
function pendingStream(): ReadableStream<Uint8Array> {
	return new ReadableStream({
		pull() {
			return new Promise<void>(() => {});
		},
	});
}

// A ReadableStream whose controller we keep, so the test can push keepalive/data
// chunks on demand to model a live tail.
function controllableStream(): {
	stream: ReadableStream<Uint8Array>;
	enqueue: (s: string) => void;
} {
	let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
	const enc = new TextEncoder();
	const stream = new ReadableStream<Uint8Array>({
		start(c) {
			ctrl = c;
		},
	});
	return {
		stream,
		enqueue: (s: string) => ctrl!.enqueue(enc.encode(s)),
	};
}

// Drain the microtask queue enough times to advance a ReadableStream read loop.
// Stream scheduling is microtask-based, so real awaits drain it even while
// vitest fake timers are installed.
const tick = async (n = 30) => {
	for (let i = 0; i < n; i++) await Promise.resolve();
};

describe('createSseFeed idle watchdog', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	it('force-reconnects after >45s of silence (half-open stream)', async () => {
		const fetchMock = vi.fn(async () => ({
			ok: true,
			status: 200,
			body: pendingStream(),
		}));
		vi.stubGlobal('fetch', fetchMock);

		const feed = createSseFeed('sess-1', { onEvent: () => {} });
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(1); // first connect, then it hangs

		// Advance past the 45s idle threshold. The ~10s watchdog tick fires at 50s,
		// sees no bytes since connect, aborts the half-open reader and reconnects.
		await vi.advanceTimersByTimeAsync(50_000);
		await tick();

		expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);

		feed.stop();
	});

	it('does NOT fire while 15s keepalives keep arriving (healthy stream)', async () => {
		const ctl = controllableStream();
		const fetchMock = vi.fn(async () => ({
			ok: true,
			status: 200,
			body: ctl.stream,
		}));
		vi.stubGlobal('fetch', fetchMock);

		const feed = createSseFeed('sess-1', { onEvent: () => {} });
		await tick();
		expect(fetchMock).toHaveBeenCalledTimes(1);

		// Simulate a healthy tail: a keepalive comment every 15s for a full minute.
		// Max silence between chunks (~15s) never crosses the 45s threshold, so the
		// watchdog must never reconnect.
		for (let t = 0; t < 60_000; t += 15_000) {
			await vi.advanceTimersByTimeAsync(15_000);
			ctl.enqueue(': keepalive\n\n');
			await tick();
		}

		// Still exactly one connection — no force-reconnect happened.
		expect(fetchMock).toHaveBeenCalledTimes(1);

		feed.stop();
	});

	it('exposes a 45s idle threshold', () => {
		expect(IDLE_TIMEOUT_MS).toBe(45_000);
	});
});
