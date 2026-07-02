/**
 * Tests for session-poller.ts — focused on loadSession reconciliation.
 *
 * Regression coverage for the "loading a chat from history loses messages /
 * shows stale tail" bug. loadSession() fetches the FULL current state with
 * ?since_update_id=0, so it must ALWAYS apply the server response — even when a
 * stale sessionState entry for the same session lingers with a lastUpdateId that
 * happens to equal the server's last_update_id. Without forcing lastUpdateId → 0
 * at the start of loadSession, applyServerState's fast-path skip would drop the
 * response, leaving the UI pinned to old (cached) messages.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sessionPoller } from './session-poller';
import { sessionStateManager } from './session-state.svelte';
import type { PollAppliedData } from './session-poller';

const SESSION_ID = 'session-history-load';

function makeLiveResponse(opts: {
	lastUpdateId: number;
	isStreaming: boolean;
	messages: Array<Record<string, unknown>>;
}) {
	return {
		ok: true,
		status: 200,
		json: async () => ({
			last_update_id: opts.lastUpdateId,
			is_streaming: opts.isStreaming,
			messages: opts.messages,
			initial_message_count: 0,
			usage: {},
		}),
	} as unknown as Response;
}

describe('session-poller · loadSession reconciliation', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SESSION_ID);
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it('applies fresh server messages even when a stale entry has a matching lastUpdateId', async () => {
		// Simulate a stale sessionState entry left behind from a previous visit:
		// it has lastUpdateId=7 and only contains the user message (the assistant
		// reply streamed in afterwards but was never reconciled for this entry).
		sessionStateManager.initSession(SESSION_ID);
		sessionStateManager.applyServerState(
			SESSION_ID,
			[{ id: 'msg-1', role: 'user', type: 'text', content: 'hello', streaming: false }],
			false,
			7,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		// Server now reports the SAME last_update_id (7) but the full, newer
		// message set including the assistant reply. The pre-fix fast-path would
		// skip applying because existing.lastUpdateId === 7 === server's.
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 7,
				isStreaming: false,
				messages: [
					{ id: 'msg-1', role: 'user', type: 'text', content: 'hello', streaming: false },
					{ id: 'msg-2', role: 'assistant', type: 'text', content: 'hi there!', streaming: false },
				],
			})
		);

		let applied: PollAppliedData | null = null;
		sessionPoller.onPollApplied((sid, data) => {
			if (sid === SESSION_ID) applied = data;
		});

		const result = await sessionPoller.loadSession(SESSION_ID);

		// onPollApplied must fire with the FULL server state (not be skipped).
		expect(applied).not.toBeNull();
		expect(result).not.toBeNull();
		const session = sessionStateManager.getSession(SESSION_ID);
		expect(session!.messages.map(m => m.id)).toEqual(['msg-1', 'msg-2']);
		expect(session!.messages[session!.messages.length - 1].content).toBe('hi there!');
	});

	it('carries toolScreenshot from server tool_use messages through to the store', async () => {
		const dataUri = 'data:image/png;base64,iVBORscreenshot';
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 5,
				isStreaming: false,
				messages: [
					{
						id: 'tool-shot',
						role: 'assistant',
						type: 'tool_use',
						content: '',
						toolName: 'mcp__claude-in-chrome__computer',
						toolId: 'tool-shot',
						toolStatus: 'complete',
						toolResult: 'screenshot of page',
						toolScreenshot: dataUri,
						streaming: false,
					},
				],
			})
		);

		const result = await sessionPoller.loadSession(SESSION_ID);
		expect(result).not.toBeNull();
		const session = sessionStateManager.getSession(SESSION_ID);
		const toolMsg = session!.messages.find(m => m.id === 'tool-shot');
		expect(toolMsg).toBeTruthy();
		expect(toolMsg!.toolScreenshot).toBe(dataUri);
	});

	it('omits toolScreenshot for tool_use messages without an image', async () => {
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 5,
				isStreaming: false,
				messages: [
					{
						id: 'tool-plain',
						role: 'assistant',
						type: 'tool_use',
						content: '',
						toolName: 'Bash',
						toolId: 'tool-plain',
						toolStatus: 'complete',
						toolResult: 'ok',
						streaming: false,
					},
				],
			})
		);

		await sessionPoller.loadSession(SESSION_ID);
		const session = sessionStateManager.getSession(SESSION_ID);
		const toolMsg = session!.messages.find(m => m.id === 'tool-plain');
		expect(toolMsg).toBeTruthy();
		expect(toolMsg!.toolScreenshot).toBeUndefined();
	});

	it('applies server state on first load when no prior entry exists', async () => {
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 3,
				isStreaming: false,
				messages: [
					{ id: 'msg-1', role: 'user', type: 'text', content: 'q', streaming: false },
					{ id: 'msg-2', role: 'assistant', type: 'text', content: 'a', streaming: false },
				],
			})
		);

		const result = await sessionPoller.loadSession(SESSION_ID);
		expect(result).not.toBeNull();
		expect(result!.messages.map(m => m.id)).toEqual(['msg-1', 'msg-2']);
	});
});

describe('session-poller · poll watchdog (stuck _inFlight recovery)', () => {
	// Mirror the module-private timing constants.
	const STREAMING_INTERVAL = 150; // STREAMING_POLL_INTERVAL_MS
	const POLL_TIMEOUT = 15000; // POLL_TIMEOUT_MS

	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		vi.useFakeTimers();
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SESSION_ID);
		vi.unstubAllGlobals();
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	it('aborts a stalled poll so the loop recovers instead of wedging forever', async () => {
		// The first poll hangs until its AbortController fires; later polls resolve.
		// This reproduces a `/live` request the backend holds open mid-teardown
		// after the SDK client is killed — the exact case that used to pin
		// `_inFlight` and freeze the UI (stuck "streaming") until a page reload.
		let calls = 0;
		fetchMock.mockImplementation((_url: string, opts: { signal: AbortSignal }) => {
			calls += 1;
			if (calls === 1) {
				return new Promise<Response>((_resolve, reject) => {
					opts.signal.addEventListener('abort', () =>
						reject(new DOMException('Aborted', 'AbortError'))
					);
				});
			}
			return Promise.resolve(
				makeLiveResponse({ lastUpdateId: 9, isStreaming: false, messages: [] })
			);
		});

		sessionPoller.startStreaming(SESSION_ID);

		// While the immediate poll hangs, interval ticks must NOT issue overlapping
		// requests — the loop is correctly serialized by `_inFlight`. Pre-fix, this
		// serialization is also what wedged it forever: still exactly one request.
		await vi.advanceTimersByTimeAsync(STREAMING_INTERVAL * 5);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(sessionPoller.isPolling(SESSION_ID)).toBe(true);

		// Advance past the watchdog: the stalled fetch aborts → `finally` clears
		// `_inFlight` → after the error backoff window (an aborted poll counts as
		// one failure → 1s) the next interval tick issues a fresh poll that
		// resolves. Without the watchdog this advance would still show exactly
		// one call.
		await vi.advanceTimersByTimeAsync(POLL_TIMEOUT + 2000);
		expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
	});
});

describe('session-poller · error backoff (503 hot-loop)', () => {
	// Regression for e2e Bug B: with the backend answering 503 on /live, the
	// 150ms streaming timer kept firing requests at full rate (188× GET observed)
	// with no backoff — a hot-loop that hammers an already-unhealthy backend.
	const SID = 'session-backoff';
	const STREAMING_INTERVAL = 150; // STREAMING_POLL_INTERVAL_MS

	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		vi.useFakeTimers();
		vi.spyOn(Math, 'random').mockReturnValue(0); // deterministic jitter
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SID);
		vi.unstubAllGlobals();
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	it('backs off exponentially on repeated HTTP errors instead of polling at full rate', async () => {
		fetchMock.mockResolvedValue({ ok: false, status: 503 } as unknown as Response);

		sessionPoller.startStreaming(SID);
		await vi.advanceTimersByTimeAsync(0); // settle the immediate first poll
		expect(fetchMock).toHaveBeenCalledTimes(1);

		// 10s of a persistently-503 backend. At the raw streaming rate that would
		// be ~66 requests; exponential backoff (1s, 2s, 4s, 8s …) allows only the
		// first poll plus a handful of retries.
		await vi.advanceTimersByTimeAsync(10_000);
		expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(6);
		// The timer itself must survive the backoff — it skips, not stops.
		expect(sessionPoller.isPolling(SID)).toBe(true);
	});

	it('clears the backoff and resumes full-rate polling once the backend recovers', async () => {
		let calls = 0;
		fetchMock.mockImplementation(() => {
			calls += 1;
			if (calls <= 2) {
				return Promise.resolve({ ok: false, status: 503 } as unknown as Response);
			}
			return Promise.resolve(
				makeLiveResponse({ lastUpdateId: calls, isStreaming: true, messages: [] })
			);
		});

		sessionPoller.startStreaming(SID);
		// Two failures ride the backoff ladder (1s, then 2s), then the third
		// attempt succeeds: 0ms → fail, ~1s → fail, ~3s → success.
		await vi.advanceTimersByTimeAsync(4_000);
		const callsAtRecovery = fetchMock.mock.calls.length;
		expect(callsAtRecovery).toBeGreaterThanOrEqual(3);

		// Recovery must clear the backoff: the next second of streaming-rate ticks
		// polls at full rate again (~6 ticks), not once-per-backoff-window.
		await vi.advanceTimersByTimeAsync(1_000);
		const callsAfter = fetchMock.mock.calls.length - callsAtRecovery;
		expect(callsAfter).toBeGreaterThanOrEqual(Math.floor(1000 / STREAMING_INTERVAL) - 1);
	});

	it('pollFinal fires even while the session is inside an error-backoff window', async () => {
		// Regression: handleDone schedules pollFinal for the authoritative
		// post-streaming state. When a transient error had armed the backoff,
		// _poll's early return swallowed that final poll and the transcript
		// stayed stuck on the last streamed frame.
		fetchMock.mockResolvedValue({ ok: false, status: 503 } as unknown as Response);

		sessionPoller.startStreaming(SID);
		await vi.advanceTimersByTimeAsync(0); // first poll fails → ~1s backoff armed
		expect(fetchMock).toHaveBeenCalledTimes(1);

		sessionPoller.pollFinal(SID);
		await vi.advanceTimersByTimeAsync(150); // pollFinal's own delay
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('pollNow fires even while the session is inside an error-backoff window', async () => {
		// pollNow is driven by a WS notification — a positive signal that the
		// backend is alive and has new content. It must not be swallowed by a
		// backoff armed during an earlier blip.
		fetchMock.mockResolvedValue({ ok: false, status: 503 } as unknown as Response);

		sessionPoller.startStreaming(SID);
		await vi.advanceTimersByTimeAsync(0);
		expect(fetchMock).toHaveBeenCalledTimes(1);

		sessionPoller.pollNow(SID);
		await vi.advanceTimersByTimeAsync(100); // pollNow's debounce
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('startStreaming on an already-polling session clears a deep backoff', async () => {
		// Regression: startStreaming cleared the backoff AFTER its
		// already-at-streaming-rate early return. A new user message into a
		// session whose poller sat in a deep backoff (server briefly down) hit
		// the no-op path, and every interval tick stayed swallowed until the
		// stale deadline passed.
		let calls = 0;
		fetchMock.mockImplementation(() => {
			calls += 1;
			if (calls <= 2) {
				return Promise.resolve({ ok: false, status: 503 } as unknown as Response);
			}
			return Promise.resolve(
				makeLiveResponse({ lastUpdateId: calls, isStreaming: true, messages: [] })
			);
		});

		sessionPoller.startStreaming(SID);
		// Fail twice: first poll → 1s backoff, retry at ~1s → 2s backoff, so the
		// deadline now sits ~2s out while the timer keeps ticking.
		await vi.advanceTimersByTimeAsync(1_200);
		const callsBefore = fetchMock.mock.calls.length;
		expect(callsBefore).toBe(2);

		// New user message → startStreaming again (no-op path: timer already at
		// streaming rate). The next interval ticks must poll immediately.
		sessionPoller.startStreaming(SID);
		await vi.advanceTimersByTimeAsync(500);
		expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
	});
});

describe('session-poller · compaction full-replace safety net', () => {
	// Regression: "messages vanish after a compaction point while watching a live
	// session". On SDK compaction the backend flags needs_full_refresh, so the
	// next /live poll returns a FULL snapshot (initial_message_count=0). Pre-fix,
	// _applyPollResult did `messages = streamingMessages` for a full replace,
	// wiping any existing rendered assistant/tool/thinking history the snapshot's
	// (short/empty-seeded) buffer was missing — only user prompts + the compaction
	// card survived the downstream Phase-1 merge. Fix: while STREAMING, union-merge
	// the existing messages the snapshot is missing (by id) AHEAD of the snapshot.
	const SID = 'session-compaction';

	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SID);
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	function seedMixedRoleHistory() {
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				{ id: 'tool-1', role: 'assistant', type: 'tool_use', content: 'Using tool: Read', toolName: 'Read', toolId: 'tool-1', streaming: false },
			],
			true, // isStreaming
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);
	}

	it('retains assistant/tool history on a streaming full-replace (compaction) snapshot', async () => {
		seedMixedRoleHistory();

		// Compaction full snapshot: buffer was short-seeded, so it only carries the
		// new user prompt + compaction card + partial turn — missing text-1/tool-1.
		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: true,
				messages: [
					{ id: 'user-2', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'sys-compact', role: 'system', type: 'system', content: 'Context compacted', systemSubtype: 'compact_boundary', streaming: false },
					{ id: 'text-2', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		// Let the immediate poll resolve.
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		// Pre-fix: ids would be exactly [user-2, sys-compact, text-2] — history gone.
		// Post-fix: the missing existing messages are kept ahead of the snapshot.
		expect(ids).toContain('text-1');
		expect(ids).toContain('tool-1');
		// The new snapshot messages are present too.
		expect(ids).toContain('user-2');
		expect(ids).toContain('sys-compact');
		expect(ids).toContain('text-2');
		// Missing-existing messages come AHEAD of the snapshot's messages.
		expect(ids.indexOf('text-1')).toBeLessThan(ids.indexOf('user-2'));
	});

	it('does NOT resurrect removed messages on an ENDED full-replace (rewind)', async () => {
		seedMixedRoleHistory();

		// Rewind trims the live store and forces a full refresh, but on an ENDED
		// session (is_streaming=false). The snapshot legitimately omits tool-1 (and
		// everything after the checkpoint) — it must NOT be resurrected.
		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				messages: [
					{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
					{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SID);

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		expect(ids).toEqual(['user-1', 'text-1']);
		expect(ids).not.toContain('tool-1');
	});
});

describe('session-poller · rewind full-refresh honored while streaming', () => {
	// Bug 1 (REWIND RESURRECTION): a rewind executed WHILE the session streams
	// (backend truncate_after_uuid path) produces a full snapshot
	// (initial_message_count=0) with is_streaming=true. The streaming union-merge
	// branch would re-insert every rewind-trimmed message (matched by id only),
	// visually no-oping the rewind. The backend now tags that one-shot full
	// refresh with full_refresh_reason:"rewind". When present, the poller must
	// SKIP the union-merge entirely and do a plain full replace so deletions
	// propagate.
	const SID = 'session-rewind-stream';

	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SID);
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	function seedHistory() {
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				{ id: 'tool-1', role: 'assistant', type: 'tool_use', content: 'Using tool: Read', toolName: 'Read', toolId: 'tool-1', streaming: false },
				{ id: 'text-2', role: 'assistant', type: 'text', content: 'more reply', streaming: false },
			],
			true, // isStreaming
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);
	}

	it('plain-replaces (trimmed messages GONE) on a streaming full-refresh tagged rewind', async () => {
		seedHistory();

		// Rewind-while-streaming: full snapshot, still streaming, carrying the
		// rewind tag. The snapshot legitimately omits tool-1/text-2 (trimmed at
		// the checkpoint). Those must NOT be resurrected by the union-merge.
		fetchMock.mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({
				last_update_id: 9,
				is_streaming: true,
				initial_message_count: 0,
				usage: {},
				full_refresh_reason: 'rewind',
				messages: [
					{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
					{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				],
			}),
		} as unknown as Response);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		expect(ids).toEqual(['user-1', 'text-1']);
		expect(ids).not.toContain('tool-1');
		expect(ids).not.toContain('text-2');
	});

	it('still union-merges a streaming full-refresh WITHOUT the rewind tag (compaction)', async () => {
		// Guard: the rewind gate must not swallow the compaction safety net. A
		// full-refresh with no reason (or a non-rewind reason) keeps the
		// history-preserving union-merge.
		seedHistory();

		fetchMock.mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({
				last_update_id: 9,
				is_streaming: true,
				initial_message_count: 0,
				usage: {},
				// no full_refresh_reason
				messages: [
					{ id: 'user-2', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'text-3', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			}),
		} as unknown as Response);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		// Compaction net still preserves the pre-existing history.
		expect(ids).toContain('text-1');
		expect(ids).toContain('tool-1');
		expect(ids).toContain('user-2');
	});
});

describe('session-poller · anchored union-merge insertion order', () => {
	// Bug 2 (ORDER SCRAMBLE): prepend-all is only correct when the kept messages
	// are a fresh tail with no preceding anchor. For a TAIL-truncated seed —
	// rendered [A,B,C,D] vs snapshot [A,B,user,partial] — prepend-all yields
	// [C,D,A,B,user,partial]: the kept tail jumps to the TOP and stays there.
	// Fix: anchored insertion — splice each contiguous missing run immediately
	// AFTER its nearest preceding existing neighbor that IS present in the
	// snapshot. Runs with no preceding anchor go to the head (empty-seed case).
	const SID = 'session-anchor-merge';

	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
	});

	afterEach(() => {
		sessionPoller.stopAll();
		sessionStateManager.removeSession(SID);
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it('anchors a kept tail after its preceding snapshot neighbor (NOT at the head)', async () => {
		// Rendered [A, B, C, D]; snapshot is a tail-truncated seed extended with
		// the live turn: [A, B, user, partial]. C, D are missing from the
		// snapshot and their preceding present neighbor is B → they must land
		// right after B, before the new snapshot tail.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'A', role: 'user', type: 'text', content: 'A', streaming: false },
				{ id: 'B', role: 'assistant', type: 'text', content: 'B', streaming: false },
				{ id: 'C', role: 'assistant', type: 'tool_use', content: 'C', toolName: 'Read', toolId: 'C', streaming: false },
				{ id: 'D', role: 'assistant', type: 'text', content: 'D', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: true,
				messages: [
					{ id: 'A', role: 'user', type: 'text', content: 'A', streaming: false },
					{ id: 'B', role: 'assistant', type: 'text', content: 'B', streaming: false },
					{ id: 'user', role: 'user', type: 'text', content: 'new prompt', streaming: false },
					{ id: 'partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		// Anchored: C,D spliced right after B; NOT jumped to the head.
		expect(ids).toEqual(['A', 'B', 'C', 'D', 'user', 'partial']);
	});

	it('sends a kept run with no preceding anchor to the head (empty-seed case)', async () => {
		// The snapshot shares NO earlier message with the rendered list, so the
		// missing run has no preceding present neighbor → head (current behavior
		// for the empty/truncated-seed compaction case must still hold).
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'old-1', role: 'assistant', type: 'text', content: 'old reply', streaming: false },
				{ id: 'old-2', role: 'assistant', type: 'tool_use', content: 'old tool', toolName: 'Read', toolId: 'old-2', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: true,
				messages: [
					{ id: 'new-user', role: 'user', type: 'text', content: 'prompt', streaming: false },
					{ id: 'new-partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const ids = session!.messages.map(m => m.id);
		// No preceding anchor → kept run stays at the head, ahead of the snapshot.
		expect(ids).toEqual(['old-1', 'old-2', 'new-user', 'new-partial']);
	});

	it('treats a uuid match as present-in-snapshot so id-family shifts dedup instead of duplicate', async () => {
		// The same logical message can carry different `id` families across
		// polls (live-store id text-{12hex} vs parser id text-{uuid}-{n}) while
		// sharing the transcript uuid. A uuid match must count as "present in
		// snapshot" so the existing copy is dropped (absorbed as an update),
		// not resurrected as a duplicate ahead of the snapshot.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'A', role: 'user', type: 'text', content: 'A', streaming: false },
				// A message from a prior poll: convertServerMessage would have
				// mapped the transcript uuid into jsonlUuid on the stored object.
				{ id: 'text-oldid', role: 'assistant', type: 'text', content: 'shared reply', streaming: false, jsonlUuid: 'uuid-shared' },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: true,
				messages: [
					{ id: 'A', role: 'user', type: 'text', content: 'A', streaming: false },
					// Same uuid, DIFFERENT id family — this is the SAME message.
					{ id: 'text-newid-0', role: 'assistant', type: 'text', content: 'shared reply', streaming: false, uuid: 'uuid-shared' },
					{ id: 'partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const msgs = session!.messages;
		// Dedup by uuid: the shared-uuid message appears exactly ONCE (absorbed as
		// an update), not resurrected as a second card ahead of the snapshot.
		// (The surviving id may be either family — applyServerState's
		// preserveMessageIds keeps the prior id for DOM stability — so we assert on
		// the uuid, not the id.)
		expect(msgs.length).toBe(3);
		const sharedByUuid = msgs.filter(m => m.jsonlUuid === 'uuid-shared');
		expect(sharedByUuid.length).toBe(1);
		// And crucially the tail order is preserved (no jump-to-head duplicate).
		expect(msgs.map(m => m.content)).toEqual(['A', 'shared reply', 'partial']);
	});
});
