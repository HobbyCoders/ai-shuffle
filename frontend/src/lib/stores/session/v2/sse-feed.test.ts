/**
 * Frame-parser tests for the V2 SSE feed client (docs/V2_PHASE1_DESIGN.md §6).
 *
 * `createFrameParser` is the deterministic, side-effect-free core of the feed:
 * it turns an arbitrarily-chunked SSE body into complete `{ id, event, data }`
 * frames. These tests exercise chunk boundaries, keepalives, multi-line data,
 * CRLF transport, and the leading-space rule.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createFrameParser, createSseFeed, type ParsedFrame } from './sse-feed';

function frames(...chunks: string[]): ParsedFrame[] {
	const p = createFrameParser();
	const out: ParsedFrame[] = [];
	for (const c of chunks) out.push(...p.push(c));
	return out;
}

describe('createFrameParser', () => {
	it('parses a single well-formed frame', () => {
		const [f] = frames('id: 7\nevent: turn.started\ndata: {"a":1}\n\n');
		expect(f).toEqual({ id: 7, event: 'turn.started', data: '{"a":1}' });
	});

	it('strips exactly one leading space after the colon', () => {
		const [f] = frames('id:3\ndata:  two-spaces\n\n');
		// one space stripped, one retained
		expect(f.id).toBe(3);
		expect(f.data).toBe(' two-spaces');
	});

	it('ignores keepalive comment lines (starting with ":")', () => {
		const out = frames(': keepalive\n\n');
		expect(out).toHaveLength(0);
	});

	it('drops a comment-only / id-only frame that carries no data', () => {
		const out = frames('id: 5\n\n');
		expect(out).toHaveLength(0); // no data field → not surfaced
	});

	it('joins multi-line data with newlines', () => {
		const [f] = frames('event: x\ndata: line1\ndata: line2\n\n');
		expect(f.data).toBe('line1\nline2');
	});

	it('retains a partial frame across chunk boundaries', () => {
		const p = createFrameParser();
		expect(p.push('id: 1\ndata: {"hal')).toHaveLength(0); // incomplete
		const done = p.push('f":true}\n\n');
		expect(done).toHaveLength(1);
		expect(done[0]).toEqual({ id: 1, event: '', data: '{"half":true}' });
	});

	it('splits multiple frames delivered in one chunk', () => {
		const out = frames('id: 1\ndata: a\n\nid: 2\ndata: b\n\n');
		expect(out.map((f) => [f.id, f.data])).toEqual([
			[1, 'a'],
			[2, 'b'],
		]);
	});

	it('normalizes CRLF transport to LF', () => {
		const [f] = frames('id: 9\r\nevent: e\r\ndata: hi\r\n\r\n');
		expect(f).toEqual({ id: 9, event: 'e', data: 'hi' });
	});

	it('yields NaN id when the id field is absent or non-numeric', () => {
		const [noId] = frames('data: x\n\n');
		expect(Number.isNaN(noId.id)).toBe(true);
		const [badId] = frames('id: abc\ndata: y\n\n');
		expect(Number.isNaN(badId.id)).toBe(true);
	});

	it('interleaves keepalives between real frames without losing data', () => {
		const out = frames(': ka\n\n', 'id: 4\ndata: real\n\n', ': ka\n\n');
		expect(out).toHaveLength(1);
		expect(out[0]).toEqual({ id: 4, event: '', data: 'real' });
	});
});

// ─── Reconnect cursor (off-by-one regression) ────────────────────────────────

describe('createSseFeed reconnect cursor', () => {
	// A ReadableStream that emits the given string chunks then closes.
	function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
		const enc = new TextEncoder();
		let i = 0;
		return new ReadableStream({
			pull(controller) {
				if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]));
				else controller.close();
			},
		});
	}

	// A ReadableStream that never produces or closes — keeps the reader parked so
	// the feed doesn't loop reconnecting after we've captured the second URL.
	function pendingStream(): ReadableStream<Uint8Array> {
		return new ReadableStream({
			pull() {
				return new Promise<void>(() => {});
			},
		});
	}

	// Flush the microtask queue enough times to drain a chunked stream read loop.
	// ReadableStream scheduling is microtask-based (not timer-based), so real
	// awaits drain it even while vitest fake timers are installed.
	const tick = async (n = 30) => {
		for (let i = 0; i < n; i++) await Promise.resolve();
	};

	beforeEach(() => vi.useFakeTimers());
	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	it('reconnects at from_seq=lastSeq (server filter is exclusive), not lastSeq+1', async () => {
		const urls: string[] = [];
		const fetchMock = vi.fn(async (url: string) => {
			urls.push(url);
			// First connect delivers seq 1–3 then closes → triggers a reconnect.
			// Second connect parks so we can inspect its URL without churn.
			return urls.length === 1
				? {
						ok: true,
						status: 200,
						body: streamFromChunks([
							'id: 1\ndata: {"seq":1}\n\n',
							'id: 2\ndata: {"seq":2}\n\n',
							'id: 3\ndata: {"seq":3}\n\n',
						]),
					}
				: { ok: true, status: 200, body: pendingStream() };
		});
		vi.stubGlobal('fetch', fetchMock);

		const events: Record<string, unknown>[] = [];
		const feed = createSseFeed('sess-1', { onEvent: (e) => events.push(e) });

		// Drain the first connect + full stream. When it closes, the feed schedules
		// a reconnect at the 1s backoff.
		await tick();
		expect(events).toHaveLength(3);
		expect(feed.lastSeq).toBe(3);

		// Fire the backoff timer → second connect.
		await vi.advanceTimersByTimeAsync(1_000);
		await tick();
		expect(urls.length).toBeGreaterThanOrEqual(2);

		// First connect replays from the seed cursor.
		expect(urls[0]).toContain('from_seq=0');
		// The reconnect must resume at lastSeq (3), NOT lastSeq+1 (4). The server
		// filters `WHERE seq > from_seq` (exclusive), so from_seq=4 would skip
		// event 4 forever; from_seq=3 is gap-free and duplicate-free.
		expect(urls[1]).toContain('from_seq=3');
		expect(urls[1]).not.toContain('from_seq=4');

		feed.stop();
	});
});
