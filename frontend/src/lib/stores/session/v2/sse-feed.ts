/**
 * SSE feed client — fetch-streaming reader for the per-session event log.
 *
 * V2 Phase 1. Reads
 * `GET /api/v1/sessions/{id}/events?from_seq=N` as an SSE body through
 * `$lib/api/runtime` (cookie auth on the web UI; Bearer token on the bundled
 * mobile UI — the endpoint's require_auth accepts either, so no EventSource is
 * needed).
 *
 * Parses SSE frames incrementally (`id:` / `event:` / `data:`, multi-line data,
 * `:`-prefixed keepalive comments ignored), tracks `lastSeq` from each frame's
 * `id:`, and on disconnect reconnects with exponential backoff (1s → 15s cap) at
 * `from_seq=lastSeq` (the server filter is exclusive: `seq > from_seq`, so this
 * is gap-free and duplicate-free). Resubscribes on `visibilitychange`/`online`/`pageshow`
 * (BFCache restore), mirroring ws-connection.ts's revive behavior. `stop()` aborts cleanly.
 *
 * Half-open detection: a dead TCP path doesn't error the fetch reader — it just
 * parks `reader.read()` forever, so backoff/reconnect never triggers. The server
 * guarantees wire activity at least every 15s (`: keepalive` comments on the live
 * tail — app/api/events.py::KEEPALIVE_SECONDS), so the idle watchdog treats 45s
 * of wire silence as half-open and force-reconnects at the cursor. Consumers can
 * also `revive()` explicitly when an out-of-band signal says the network just
 * changed (e.g. the chat WS reconnected).
 *
 * No new npm deps: uses `fetch` + `ReadableStream` + `TextDecoder`.
 */

import { apiBase, withAuth } from '$lib/api/runtime';

// ─── Public API ───────────────────────────────────────────────────────────────

export interface SseFeedOptions {
	/** Called once per parsed data frame with the decoded JSON payload. */
	onEvent: (event: Record<string, unknown>) => void;
	/** Optional: called each time a connection successfully opens (first connect
	 *  AND every reconnect), before any frame from that connection is delivered.
	 *  Lets the consumer re-arm per-connect state (e.g. the history-catch-up flag)
	 *  so a reconnect's fresh backfill batches before the next `caught_up`. */
	onOpen?: () => void;
	/** Optional: called on transient errors (before a reconnect is scheduled). */
	onError?: (err: unknown) => void;
	/** Optional: called once when the server answers the connect with 404 or
	 *  403 — the session is gone or this viewer may not read it. That answer
	 *  will not change on retry, so the feed stops itself first and never
	 *  reconnects (#1908). Network errors and 5xx still reconnect with backoff. */
	onUnavailable?: (status: number) => void;
	/** Seed cursor. The first request uses `from_seq`; reconnects use lastSeq (server filter is exclusive). */
	fromSeq?: number;
	/** Log epoch the seed cursor belongs to (from an earlier `caught_up`). Sent
	 *  with any resume (`from_seq > 0`) so the server can answer a rewritten log
	 *  with a `reset` frame instead of serving events past a cursor that no longer
	 *  means anything. */
	epoch?: string | null;
}

export interface SseFeed {
	/** Whether the reader loop is currently active. */
	readonly running: boolean;
	/** Highest frame `id:` (seq) seen so far. */
	readonly lastSeq: number;
	/** Log epoch from the latest `caught_up` (or `reset`), or the seed; null when
	 *  the server has not reported one (older servers never do). */
	readonly epoch: string | null;
	/**
	 * Abort a possibly-half-open reader and reconnect immediately at the cursor
	 * (`from_seq=lastSeq` — gap-free and duplicate-free). Same operation the
	 * visibility/online/pageshow listeners run; exposed so a consumer with an
	 * out-of-band network signal (e.g. a WS reconnect) can heal the stream
	 * without waiting on the stall watchdog. No-op after stop().
	 */
	revive: () => void;
	/** Abort the stream and tear down revive listeners. Idempotent. */
	stop: () => void;
}

// ─── Constants ────────────────────────────────────────────────────────────────

/** What the server reports as the epoch of a log with no rows yet. */
const EMPTY_LOG_EPOCH = '0';

const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 15_000;

// ── Idle watchdog ──────────────────────────────────────────────────────────
// A healthy stream emits a frame or keepalive comment at least every 15s. A
// half-open TCP connection (the peer vanished but no RST/FIN reached us) leaves
// `reader.read()` hanging forever with no error and no `done` — the reconnect
// loop never fires because nothing threw. The watchdog force-reconnects when no
// bytes have arrived for well over the keepalive cadence.
export const IDLE_TIMEOUT_MS = 45_000;
const IDLE_WATCHDOG_TICK_MS = 10_000;

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

// ─── Single-feed invariant ───────────────────────────────────────────────────
// One live feed per (this browser instance, session). A second createSseFeed
// for a session still being read closes the earlier reader first: the server
// saw the same device open three feeds to one session within a second
// (2026-08-22), each a full backfill from the same cursor, while it was
// already overloaded.
const liveFeeds = new Map<string, SseFeed>();

/** Number of feeds currently reading (exported for tests / diagnostics). */
export function liveFeedCount(): number {
	return liveFeeds.size;
}

// Revive triggers arrive in bursts: a laptop wake fires `visibilitychange`,
// `online` and `pageshow` back to back, and the chat WS reconnect calls
// `revive()` on top. Each would abort the fresh reader and open another.
// Coalesce them: once a revive has started a connect, further revives inside
// this window are no-ops — the reader they would create is already on its way.
export const REVIVE_COALESCE_MS = 1_000;

// ─── Feed factory ─────────────────────────────────────────────────────────────

export function createSseFeed(sessionId: string, options: SseFeedOptions): SseFeed {
	// Idempotent per (device, session): a previous live feed for this session
	// is closed before the new one opens.
	liveFeeds.get(sessionId)?.stop();

	let running = true;
	let lastReviveAt = 0;
	let lastSeq = typeof options.fromSeq === 'number' ? Math.max(0, options.fromSeq) : 0;
	// `nextFrom` is the from_seq to use for the NEXT connect attempt. First
	// connect replays from the seed cursor; reconnects resume at lastSeq. The
	// server filter is exclusive (`seq > from_seq`), so `lastSeq` yields the
	// next unseen event with no gap and no duplicate.
	let nextFrom = lastSeq;
	let epoch: string | null = typeof options.epoch === 'string' && options.epoch ? options.epoch : null;
	let backoff = BACKOFF_MIN_MS;
	let controller: AbortController | null = null;
	// Ownership token for one concrete fetch/reader attempt. revive()/stop()
	// invalidate it before aborting, so buffered work from the old reader cannot
	// publish frames or caught_up into the replacement connection.
	let connectionAttempt = 0;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	// Timestamp of the last raw chunk received (frame OR keepalive comment).
	// Reset at every connect attempt so a still-connecting stream isn't killed;
	// updated on every `reader.read()` yield. The watchdog compares against it.
	let lastFrameAt = Date.now();
	let watchdogTimer: ReturnType<typeof setInterval> | null = null;

	// ── Revive listeners (mirror ws-connection.ts) ──────────────────────
	let visibilityHandler: (() => void) | null = null;
	let onlineHandler: (() => void) | null = null;
	let pageShowHandler: ((e: PageTransitionEvent) => void) | null = null;

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
		const now = Date.now();
		if (now - lastReviveAt < REVIVE_COALESCE_MS) return;
		lastReviveAt = now;
		if (reconnectTimer !== null) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		// Invalidate this attempt BEFORE aborting: fetch/reader implementations can
		// still resolve buffered data after AbortController fires.
		connectionAttempt++;
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
			// pageshow with persisted=true fires on BFCache restore, where
			// visibilitychange may not fire — the reader would otherwise stay
			// half-open forever. Mirrors ws-connection.ts's pageshow revive.
			pageShowHandler = (e: PageTransitionEvent) => {
				if (e.persisted) reviveNow();
			};
			window.addEventListener('pageshow', pageShowHandler as EventListener);
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
		if (pageShowHandler && typeof window !== 'undefined') {
			window.removeEventListener('pageshow', pageShowHandler as EventListener);
			pageShowHandler = null;
		}
	}

	// ── Idle watchdog ───────────────────────────────────────────────────
	function checkIdle(): void {
		if (!running) return;
		const silentMs = Date.now() - lastFrameAt;
		if (silentMs > IDLE_TIMEOUT_MS) {
			console.warn(
				`[sse-feed] no frame/keepalive for ${silentMs}ms (> ${IDLE_TIMEOUT_MS}ms) — ` +
					'forcing reconnect (suspected half-open stream)'
			);
			// Reset the clock so the next tick doesn't re-fire before the fresh
			// connect lands, then force a clean reconnect through the existing
			// revive path: aborts the half-open reader, resets backoff, and
			// reconnects immediately at from_seq=lastSeq.
			lastFrameAt = Date.now();
			reviveNow();
		}
	}

	function startWatchdog(): void {
		if (watchdogTimer !== null) return; // exactly one watchdog per feed instance
		if (typeof setInterval === 'undefined') return;
		watchdogTimer = setInterval(checkIdle, IDLE_WATCHDOG_TICK_MS);
	}

	function stopWatchdog(): void {
		if (watchdogTimer !== null) {
			clearInterval(watchdogTimer);
			watchdogTimer = null;
		}
	}

	async function connect(): Promise<void> {
		if (!running) return;
		// If a reader is already active, don't stack another.
		if (controller) return;

		// Reset the idle clock at the START of every (re)connect attempt so a
		// stream that is still connecting (no bytes yet) isn't instantly killed
		// by the watchdog.
		lastFrameAt = Date.now();

		const ac = new AbortController();
		const attempt = ++connectionAttempt;
		controller = ac;
		const ownsAttempt = () =>
			running && connectionAttempt === attempt && controller === ac;
		const from = nextFrom;
		// A resume names the log it came from; a full load (from_seq=0) has no
		// cursor to check, so it never sends one.
		const sentEpoch = from > 0 ? epoch : null;
		const url =
			`${apiBase()}/sessions/${encodeURIComponent(sessionId)}/events?from_seq=${from}` +
			(sentEpoch ? `&epoch=${encodeURIComponent(sentEpoch)}` : '');

		try {
			const res = await fetch(url, withAuth({
				method: 'GET',
				headers: { Accept: 'text/event-stream' },
				signal: ac.signal,
			}));

			if (!ownsAttempt()) {
				try {
					await res.body?.cancel();
				} catch {
					/* stale response cleanup is best-effort */
				}
				return;
			}
			if (res.status === 404 || res.status === 403) {
				// Terminal: retrying cannot make an unreadable session readable.
				try {
					await res.body?.cancel();
				} catch {
					/* best-effort */
				}
				feed.stop();
				options.onUnavailable?.(res.status);
				return;
			}
			if (!res.ok || !res.body) {
				throw new Error(`SSE feed HTTP ${res.status}`);
			}

			// A successful connect resets the backoff clock.
			backoff = BACKOFF_MIN_MS;
			// Signal the fresh connection so the consumer can re-arm per-connect
			// state before any frame lands (the backfill for this connection is
			// about to stream, terminated by a `caught_up` sentinel).
			options.onOpen?.();

			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			const parser = createFrameParser();

			for (;;) {
				const { value, done } = await reader.read();
				if (!ownsAttempt()) {
					try {
						await reader.cancel();
					} catch {
						/* stale reader cleanup is best-effort */
					}
					return;
				}
				if (done) break;
				// Any raw bytes — a data frame OR a `:` keepalive comment — mean the
				// stream is alive. Stamp BEFORE parsing so keepalives (which yield no
				// frames) still keep the watchdog satisfied.
				lastFrameAt = Date.now();
				const chunk = decoder.decode(value, { stream: true });
				const frames = parser.push(chunk);
				for (const frame of frames) {
					if (!ownsAttempt()) return;
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
							// Surface the named SSE `event:` type to the consumer. Normal
							// event frames already carry `type` in their payload (the wire
							// envelope), so this is a no-op for them; control frames whose
							// data omits `type` (e.g. the `caught_up` sentinel, data
							// `{"seq":N}`) get it stamped here so the controller can act on
							// them without a separate channel.
							if (payload.type === undefined && frame.event) {
								payload.type = frame.event;
							}
							if (payload.type === 'caught_up') {
								// Absent on older servers: resumes then go unchecked.
								epoch = typeof payload.epoch === 'string' && payload.epoch ? payload.epoch : null;
							} else if (epoch === EMPTY_LOG_EPOCH && !Number.isNaN(frame.id) && typeof payload.id === 'string' && payload.id) {
								// The log was empty at caught_up, so this first row is the new
								// log's lowest-seq row and its id IS the epoch (see the server's
								// get_log_epoch). Adopting it keeps the next resume from being
								// reset as if the log had been cleared.
								epoch = payload.id;
							} else if (payload.type === 'reset') {
								// A reset only answers the epoch this request sent; one without
								// it has nothing to invalidate.
								if (!sentEpoch) continue;
								// The server continues with a full load of the new log, so
								// the old cursor is meaningless from here on.
								lastSeq = 0;
								nextFrom = 0;
								epoch = typeof payload.epoch === 'string' && payload.epoch ? payload.epoch : null;
							}
							options.onEvent(payload);
						} catch (e) {
							options.onError?.(e);
						}
					}
				}
			}

			// Stream ended (server closed the body) — reconnect from the cursor,
			// but only if this attempt still owns the feed.
			if (!ownsAttempt()) return;
			controller = null;
			scheduleReconnect();
		} catch (err) {
			if (!ownsAttempt()) return;
			controller = null;
			if (ac.signal.aborted || !running) return; // intentional stop
			options.onError?.(err);
			scheduleReconnect();
		}
	}

	setupReviveListeners();
	startWatchdog();
	void connect();

	const feed: SseFeed = {
		get running() {
			return running;
		},
		get lastSeq() {
			return lastSeq;
		},
		get epoch() {
			return epoch;
		},
		revive() {
			reviveNow();
		},
		stop() {
			if (!running) return;
			running = false;
			connectionAttempt++;
			if (liveFeeds.get(sessionId) === feed) liveFeeds.delete(sessionId);
			stopWatchdog();
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
	liveFeeds.set(sessionId, feed);
	return feed;
}
