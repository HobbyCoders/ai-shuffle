/**
 * SSE feed client — fetch-streaming reader for the per-session event log.
 *
 * V2 Phase 1 (docs/V2_PHASE1_DESIGN.md §6). Reads
 * `GET /api/v1/sessions/{id}/events?from_seq=N` as an SSE body with
 * `credentials:'include'` (cookie auth — the endpoint's require_auth passes via
 * the cookie branch, so no EventSource / bearer token is needed).
 *
 * Parses SSE frames incrementally (`id:` / `event:` / `data:`, multi-line data,
 * `:`-prefixed keepalive comments ignored), tracks `lastSeq` from each frame's
 * `id:`, and on disconnect reconnects with exponential backoff (1s → 15s cap) at
 * `from_seq=lastSeq` (the server filter is exclusive: `seq > from_seq`, so this
 * is gap-free and duplicate-free). Resubscribes on `visibilitychange`/`online`, mirroring
 * ws-connection.ts's revive behavior. `stop()` aborts cleanly.
 *
 * No new npm deps: uses `fetch` + `ReadableStream` + `TextDecoder`.
 */

// ─── Public API ───────────────────────────────────────────────────────────────

export interface SseFeedOptions {
	/** Called once per parsed data frame with the decoded JSON payload. */
	onEvent: (event: Record<string, unknown>) => void;
	/** Optional: called on transient errors (before a reconnect is scheduled). */
	onError?: (err: unknown) => void;
	/** Seed cursor. The first request uses `from_seq`; reconnects use lastSeq (server filter is exclusive). */
	fromSeq?: number;
}

export interface SseFeed {
	/** Whether the reader loop is currently active. */
	readonly running: boolean;
	/** Highest frame `id:` (seq) seen so far. */
	readonly lastSeq: number;
	/** Abort the stream and tear down revive listeners. Idempotent. */
	stop: () => void;
}

// ─── Constants ────────────────────────────────────────────────────────────────

const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 15_000;

// ─── Frame parser (exported for unit tests) ───────────────────────────────────

export interface ParsedFrame {
	/** Frame `id:` value parsed as a number (NaN when absent/non-numeric). */
	id: number;
	/** Frame `event:` value, or '' when absent. */
	event: string;
	/** Joined multi-line `data:` payload (raw string). */
	data: string;
}

/**
 * Incremental SSE frame parser. Feed it decoded text chunks; it returns any
 * complete frames (delimited by a blank line) and retains the trailing partial
 * frame across calls. Keepalive comments (lines starting with ':') are ignored.
 *
 * Kept as a small stateful object so a single reader can process a chunked body
 * and the test suite can exercise arbitrary chunk boundaries.
 */
export function createFrameParser() {
	let buffer = '';

	function push(chunk: string): ParsedFrame[] {
		buffer += chunk;
		const frames: ParsedFrame[] = [];
		// Normalize CRLF → LF so \n\n splitting works regardless of transport.
		buffer = buffer.replace(/\r\n/g, '\n');
		let sep: number;
		// Frames are separated by a blank line (\n\n). Process every complete one.
		while ((sep = buffer.indexOf('\n\n')) !== -1) {
			const raw = buffer.slice(0, sep);
			buffer = buffer.slice(sep + 2);
			const frame = parseFrame(raw);
			if (frame) frames.push(frame);
		}
		return frames;
	}

	return { push };
}

function parseFrame(raw: string): ParsedFrame | null {
	let id = NaN;
	let event = '';
	const dataLines: string[] = [];
	let sawData = false;

	for (const line of raw.split('\n')) {
		if (line === '' || line.startsWith(':')) continue; // keepalive / blank
		const colon = line.indexOf(':');
		const field = colon === -1 ? line : line.slice(0, colon);
		// Per the SSE spec, a single leading space after the colon is stripped.
		let value = colon === -1 ? '' : line.slice(colon + 1);
		if (value.startsWith(' ')) value = value.slice(1);
		switch (field) {
			case 'id':
				id = Number(value);
				break;
			case 'event':
				event = value;
				break;
			case 'data':
				dataLines.push(value);
				sawData = true;
				break;
			default:
				break; // unknown field — ignore
		}
	}

	if (!sawData) return null; // comment-only / id-only frame carries no payload
	return { id, event, data: dataLines.join('\n') };
}

// ─── Feed factory ─────────────────────────────────────────────────────────────

export function createSseFeed(sessionId: string, options: SseFeedOptions): SseFeed {
	let running = true;
	let lastSeq = typeof options.fromSeq === 'number' ? Math.max(0, options.fromSeq) : 0;
	// `nextFrom` is the from_seq to use for the NEXT connect attempt. First
	// connect replays from the seed cursor; reconnects resume at lastSeq. The
	// server filter is exclusive (`seq > from_seq`), so `lastSeq` yields the
	// next unseen event with no gap and no duplicate.
	let nextFrom = lastSeq;
	let backoff = BACKOFF_MIN_MS;
	let controller: AbortController | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	// ── Revive listeners (mirror ws-connection.ts) ──────────────────────
	let visibilityHandler: (() => void) | null = null;
	let onlineHandler: (() => void) | null = null;

	function scheduleReconnect() {
		if (!running) return;
		if (reconnectTimer !== null) return;
		const delay = backoff;
		backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			void connect();
		}, delay);
	}

	/** Immediately (re)connect — used by revive handlers. */
	function reviveNow() {
		if (!running) return;
		if (reconnectTimer !== null) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		// Abort a possibly-half-open reader so connect() starts fresh.
		if (controller) {
			try {
				controller.abort();
			} catch {
				/* ignore */
			}
			controller = null;
		}
		backoff = BACKOFF_MIN_MS;
		void connect();
	}

	function setupReviveListeners() {
		if (typeof document !== 'undefined') {
			visibilityHandler = () => {
				if (!document.hidden) reviveNow();
			};
			document.addEventListener('visibilitychange', visibilityHandler);
		}
		if (typeof window !== 'undefined') {
			onlineHandler = () => reviveNow();
			window.addEventListener('online', onlineHandler);
		}
	}

	function teardownReviveListeners() {
		if (visibilityHandler && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', visibilityHandler);
			visibilityHandler = null;
		}
		if (onlineHandler && typeof window !== 'undefined') {
			window.removeEventListener('online', onlineHandler);
			onlineHandler = null;
		}
	}

	async function connect(): Promise<void> {
		if (!running) return;
		// If a reader is already active, don't stack another.
		if (controller) return;

		const ac = new AbortController();
		controller = ac;
		const from = nextFrom;
		const url = `/api/v1/sessions/${encodeURIComponent(sessionId)}/events?from_seq=${from}`;

		try {
			const res = await fetch(url, {
				method: 'GET',
				credentials: 'include',
				headers: { Accept: 'text/event-stream' },
				signal: ac.signal,
			});

			if (!res.ok || !res.body) {
				throw new Error(`SSE feed HTTP ${res.status}`);
			}

			// A successful connect resets the backoff clock.
			backoff = BACKOFF_MIN_MS;

			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			const parser = createFrameParser();

			for (;;) {
				const { value, done } = await reader.read();
				if (done) break;
				const chunk = decoder.decode(value, { stream: true });
				const frames = parser.push(chunk);
				for (const frame of frames) {
					if (!Number.isNaN(frame.id)) {
						lastSeq = Math.max(lastSeq, frame.id);
						// The server filters `WHERE seq > from_seq` (exclusive), so the
						// reconnect cursor is `lastSeq` — NOT `lastSeq + 1`. Passing
						// `lastSeq + 1` would make the server skip event `lastSeq + 1`
						// on every reconnect/revive (off-by-one drop). `lastSeq` is
						// both gap-free (> lastSeq starts at lastSeq+1) and
						// duplicate-free (lastSeq itself is excluded).
						nextFrom = lastSeq;
					}
					if (frame.data) {
						try {
							const payload = JSON.parse(frame.data) as Record<string, unknown>;
							options.onEvent(payload);
						} catch (e) {
							options.onError?.(e);
						}
					}
				}
			}

			// Stream ended (server closed the body) — reconnect from the cursor.
			if (controller === ac) controller = null;
			scheduleReconnect();
		} catch (err) {
			if (controller === ac) controller = null;
			if (ac.signal.aborted || !running) return; // intentional stop
			options.onError?.(err);
			scheduleReconnect();
		}
	}

	setupReviveListeners();
	void connect();

	return {
		get running() {
			return running;
		},
		get lastSeq() {
			return lastSeq;
		},
		stop() {
			if (!running) return;
			running = false;
			if (reconnectTimer !== null) {
				clearTimeout(reconnectTimer);
				reconnectTimer = null;
			}
			if (controller) {
				try {
					controller.abort();
				} catch {
					/* ignore */
				}
				controller = null;
			}
			teardownReviveListeners();
		},
	};
}
