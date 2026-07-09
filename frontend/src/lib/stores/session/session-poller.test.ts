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
	fullRefreshReason?: string;
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
			...(opts.fullRefreshReason ? { full_refresh_reason: opts.fullRefreshReason } : {}),
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

	it('lifts persisted metadata.timestamp to the top-level timestamp field', async () => {
		// Regression: the server nests each message's creation time in
		// metadata.timestamp; if it isn't lifted to ChatMessage.timestamp the UI
		// falls back to new Date() and every reloaded message shows "now".
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 3,
				isStreaming: false,
				messages: [
					{
						id: 'msg-old-user',
						role: 'user',
						type: 'text',
						content: 'question from yesterday',
						streaming: false,
						metadata: { timestamp: '2026-01-15T14:30:00+00:00' },
					},
					{
						id: 'msg-old-assistant',
						role: 'assistant',
						type: 'text',
						content: 'answer from yesterday',
						streaming: false,
						metadata: { timestamp: '2026-01-15T14:31:12+00:00', cost_usd: 0.01 },
					},
					{
						id: 'msg-no-ts',
						role: 'assistant',
						type: 'text',
						content: 'legacy message without timestamp',
						streaming: false,
					},
				],
			})
		);

		await sessionPoller.loadSession(SESSION_ID);
		const session = sessionStateManager.getSession(SESSION_ID);
		const byId = new Map(session!.messages.map(m => [m.id, m]));
		expect(byId.get('msg-old-user')!.timestamp).toBe('2026-01-15T14:30:00+00:00');
		expect(byId.get('msg-old-assistant')!.timestamp).toBe('2026-01-15T14:31:12+00:00');
		expect(byId.get('msg-no-ts')!.timestamp).toBeUndefined();
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

	it('maps per-message profileId (camelCase, with snake_case fallback) onto ChatMessage', async () => {
		// Regression: the server persists which profile produced each user
		// message; without this mapping every bubble fell back to the session's
		// CURRENT profile. GET/live JSONL paths emit `profileId`; the /live DB
		// fast path emits snake_case `profile_id`.
		fetchMock.mockResolvedValueOnce(
			makeLiveResponse({
				lastUpdateId: 4,
				isStreaming: false,
				messages: [
					{ id: 'msg-camel', role: 'user', type: 'text', content: 'q1', streaming: false, profileId: 'profile-a' },
					{ id: 'msg-snake', role: 'user', type: 'text', content: 'q2', streaming: false, profile_id: 'profile-b' },
					// Assistant reply now carries its turn's profile (snake_case from
					// the /live DB fast path) — this is what stops assistant bubbles
					// re-labelling to the current selection on a profile switch.
					{ id: 'msg-asst', role: 'assistant', type: 'text', content: 'reply', streaming: false, profile_id: 'profile-a' },
					{ id: 'msg-none', role: 'assistant', type: 'text', content: 'a', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SESSION_ID);
		const session = sessionStateManager.getSession(SESSION_ID);
		const byId = new Map(session!.messages.map(m => [m.id, m]));
		expect(byId.get('msg-camel')!.profileId).toBe('profile-a');
		expect(byId.get('msg-snake')!.profileId).toBe('profile-b');
		expect(byId.get('msg-asst')!.profileId).toBe('profile-a');
		expect(byId.get('msg-none')!.profileId).toBeUndefined();
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
		// everything after the checkpoint) — it must NOT be resurrected. The real
		// backend ALWAYS tags rewind snapshots (truncate_after_uuid sets the
		// one-shot reason AND trim_update_id serves 'rewind' to late pollers), so
		// the fixture carries the tag; the poller keys the trust-the-snapshot
		// replace off that reason, not off is_streaming.
		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				fullRefreshReason: 'rewind',
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

	it('retains rendered history on an ENDED full-refresh snapshot (turn_end)', async () => {
		// THE user-reported bug: "message list loses a bunch of messages live;
		// refresh restores them". end_streaming (and a compaction boundary landing
		// at turn end) raises the one-shot needs_full_refresh; when the poll that
		// consumes it already sees is_streaming=false, the old gate skipped the
		// union-merge and did a wholesale replace — a snapshot with fewer rendered
		// cards than the client silently DROPPED the difference (DB stayed
		// complete, so a page refresh brought everything back). A completeness
		// refresh must union-merge regardless of streaming state.
		seedMixedRoleHistory();

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				fullRefreshReason: 'turn_end',
				messages: [
					{ id: 'user-2', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'text-2', role: 'assistant', type: 'text', content: 'final reply', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SID);

		const ids = sessionStateManager.getSession(SID)!.messages.map(m => m.id);
		// Existing rendered history the short snapshot was missing survives...
		expect(ids).toContain('text-1');
		expect(ids).toContain('tool-1');
		// ...alongside the snapshot's own messages, in order.
		expect(ids).toContain('user-2');
		expect(ids).toContain('text-2');
		expect(ids.indexOf('tool-1')).toBeLessThan(ids.indexOf('user-2'));
	});

	it('retains rendered history on an ENDED UNATTRIBUTED full snapshot (legacy/compaction)', async () => {
		// Same drop, no reason field: a compaction one-shot consumed on a poll
		// after streaming ended (or an older backend that does not attribute).
		// Unknown/absent reasons must be treated as completeness refreshes.
		seedMixedRoleHistory();

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				messages: [
					{ id: 'user-2', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'sys-compact', role: 'system', type: 'system', content: 'Context compacted', systemSubtype: 'compact_boundary', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SID);

		const ids = sessionStateManager.getSession(SID)!.messages.map(m => m.id);
		expect(ids).toContain('text-1');
		expect(ids).toContain('tool-1');
		expect(ids).toContain('sys-compact');
	});

	it('honors a DISCARD full snapshot verbatim (stopped-turn prompt not resurrected)', async () => {
		// discard_empty_user_turn deliberately REMOVES the trailing user prompt
		// (Stop before any output) — like rewind, that deletion must propagate,
		// not be union-merged back in.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				{ id: 'user-2', role: 'user', type: 'text', content: 'stopped prompt', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				fullRefreshReason: 'discard',
				messages: [
					{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
					{ id: 'text-1', role: 'assistant', type: 'text', content: 'assistant reply', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SID);

		const ids = sessionStateManager.getSession(SID)!.messages.map(m => m.id);
		expect(ids).toEqual(['user-1', 'text-1']);
		expect(ids).not.toContain('user-2');
	});

	it('clears streaming flags on messages the ENDED union-merge splices back in', async () => {
		// A spliced-back existing message must not keep streaming:true once the
		// turn has ended, or its card shows the bouncing dots forever.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'text-1', role: 'assistant', type: 'text', content: 'partial reply', streaming: true },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: false,
				fullRefreshReason: 'turn_end',
				messages: [
					{ id: 'user-1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				],
			})
		);

		await sessionPoller.loadSession(SID);

		const session = sessionStateManager.getSession(SID)!;
		const spliced = session.messages.find(m => m.id === 'text-1');
		expect(spliced).toBeDefined();
		expect(spliced!.streaming).toBe(false);
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

	it('keeps each missing card after its own anchor when the compaction snapshot REORDERS present messages (non-monotonic anchors)', async () => {
		// THE COMPACTION SCRAMBLE. At a compaction/turn boundary the live store is
		// re-seeded from the durable transcript, so present messages arrive under a
		// DIFFERENT id family (uuid-matched) AND can be RE-ORDERED relative to the
		// client's current array (the id-family switch + granularity/summary
		// re-block). Meanwhile uuid-less cards (compact_boundary + system rows the
		// parser never reproduces the same way) are "missing" and must be spliced
		// back, each immediately AFTER its own preceding present neighbor.
		//
		// The anchored-insertion splice pre-computes anchor indices ONCE and shifts
		// every insert by a single GLOBAL offset — correct ONLY when the anchors are
		// monotonic in SNAPSHOT order. When the snapshot orders present messages
		// NON-monotonically vs the client array, the global offset shoves a missing
		// card past its anchor → the cards "jump around". A page refresh loads the
		// durable transcript into an EMPTY client (snapshot taken verbatim) so it
		// "settles" — the exact reported symptom.
		//
		// Client array (correct at the time it rendered): P1, Ma(sys), P2, Mb(sys).
		// Compaction snapshot re-orders the present pair to [P2, P1] (id-family
		// switch, uuid-matched) and drops the two uuid-less system cards.
		// Correct union-merge: Ma stays after P1, Mb stays after P2 → [P2, Mb, P1, Ma].
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'p1-live', role: 'assistant', type: 'text', content: 'p1', streaming: false, jsonlUuid: 'uP1' },
				{ id: 'ma-live', role: 'system', type: 'system', content: 'ma', systemSubtype: 'hook_event', streaming: false },
				{ id: 'p2-live', role: 'assistant', type: 'text', content: 'p2', streaming: false, jsonlUuid: 'uP2' },
				{ id: 'mb-live', role: 'system', type: 'system', content: 'mb', systemSubtype: 'hook_event', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeLiveResponse({
				lastUpdateId: 9,
				isStreaming: true,
				fullRefreshReason: 'compaction',
				messages: [
					// Present pair, id-family switched (uuid match) and REORDERED.
					{ id: 'text-uP2-0', role: 'assistant', type: 'text', content: 'p2', streaming: false, uuid: 'uP2' },
					{ id: 'text-uP1-0', role: 'assistant', type: 'text', content: 'p1', streaming: false, uuid: 'uP1' },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const session = sessionStateManager.getSession(SID);
		const order = session!.messages.map(m => m.content);
		// Each missing card must stay anchored to ITS OWN preceding neighbor.
		expect(order).toEqual(['p2', 'mb', 'p1', 'ma']);
	});
});

describe('session-poller · DELTA preserves union-merge-rescued tail cards', () => {
	// Bug: "messages disappear after sending into a stopped/idle session, until
	// refresh". User sends into a stopped session → the backend re-seeds its
	// buffer from the durable transcript (different id family, missing client-only
	// cards) and sets initial_message_count=K. The FIRST poll is FULL (imc=0): the
	// union-merge rescues the client's prior-turn tail (final assistant text +
	// system/"Task Updated" card) and splices them at index >= K. The SECOND poll
	// is DELTA (imc=K): the old `existingMessages.slice(0, K)` DROPPED everything
	// past K → the rescued tail cards vanished. Fix: the DELTA branch preserves
	// existing tail messages the delta is MISSING with the same anchored-insertion
	// the full branch uses (add-only, never drop completed rendered cards).
	const SID = 'session-delta-preserve';

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

	/** A DELTA /live response: initial_message_count=K, only the current turn. */
	function makeDeltaResponse(opts: {
		lastUpdateId: number;
		initialMessageCount: number;
		isStreaming: boolean;
		messages: Array<Record<string, unknown>>;
	}) {
		return {
			ok: true,
			status: 200,
			json: async () => ({
				last_update_id: opts.lastUpdateId,
				is_streaming: opts.isStreaming,
				initial_message_count: opts.initialMessageCount,
				usage: {},
				messages: opts.messages,
			}),
		} as unknown as Response;
	}

	it('keeps rescued prior-turn tail cards on a delta after a re-seeded turn (THE BUG)', async () => {
		// State after the first FULL poll union-merged the prior turn's tail back
		// in: [re-seeded history (K=2)] + [rescued final assistant text + system
		// card] (client-only ids, no uuid the delta will carry).
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'seed-u1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'seed-a1', role: 'assistant', type: 'text', content: 'seed reply', streaming: false },
				// Rescued tail cards (prior turn's final output, client-only):
				{ id: 'rescued-text', role: 'assistant', type: 'text', content: 'final answer', streaming: false },
				{ id: 'rescued-sys', role: 'system', type: 'system', content: 'Task Updated', systemSubtype: 'hook_event', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		// DELTA poll: initial_message_count=2, carrying only the NEW turn.
		fetchMock.mockResolvedValue(
			makeDeltaResponse({
				lastUpdateId: 9,
				initialMessageCount: 2,
				isStreaming: true,
				messages: [
					{ id: 'new-user', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'new-partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const ids = sessionStateManager.getSession(SID)!.messages.map(m => m.id);
		// Pre-fix: ids would be [seed-u1, seed-a1, new-user, new-partial] — the
		// rescued tail cards dropped. Post-fix: they survive, in order, positioned
		// right after the history and BEFORE the new user prompt.
		expect(ids).toEqual([
			'seed-u1', 'seed-a1', 'rescued-text', 'rescued-sys', 'new-user', 'new-partial',
		]);
		expect(ids.indexOf('rescued-text')).toBeLessThan(ids.indexOf('new-user'));
		expect(ids.indexOf('rescued-sys')).toBeLessThan(ids.indexOf('new-user'));
	});

	it('does NOT preserve a tail message still flagged streaming (in-flight bubble)', async () => {
		// A prior poll's live streaming bubble in the tail must be replaced wholesale
		// by the fresh streamingMessages — never preserved, or it would duplicate the
		// live bubble if its id churned.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'seed-u1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'seed-a1', role: 'assistant', type: 'text', content: 'seed reply', streaming: false },
				{ id: 'stale-stream', role: 'assistant', type: 'text', content: 'in flight', streaming: true },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeDeltaResponse({
				lastUpdateId: 9,
				initialMessageCount: 2,
				isStreaming: true,
				messages: [
					{ id: 'new-user', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'new-partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const ids = sessionStateManager.getSession(SID)!.messages.map(m => m.id);
		expect(ids).not.toContain('stale-stream');
		expect(ids).toEqual(['seed-u1', 'seed-a1', 'new-user', 'new-partial']);
	});

	it('does NOT preserve an optimistic user-* tail when the real user msg arrives in the delta', async () => {
		// message-sender appends an optimistic `user-${Date.now()}` before the server
		// persists it under a different id. Preserving it here while the real user
		// message arrives in the delta would double the user bubble — Phase-1 in
		// session-init.ts reconciles optimistic user-* separately.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'seed-a1', role: 'assistant', type: 'text', content: 'seed reply', streaming: false },
				{ id: 'user-1720000000000', role: 'user', type: 'text', content: 'second prompt', streaming: false },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeDeltaResponse({
				lastUpdateId: 9,
				initialMessageCount: 1,
				isStreaming: true,
				messages: [
					{ id: 'msg-9', role: 'user', type: 'text', content: 'second prompt', streaming: false },
					{ id: 'new-partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const msgs = sessionStateManager.getSession(SID)!.messages;
		// Exactly ONE user bubble — the optimistic user-* tail was NOT preserved as
		// a second card alongside the real user msg arriving in the delta. (The lone
		// bubble's id may be the optimistic one: applyServerState's preserveMessageIds
		// re-adopts it for DOM stability — that's outside this fix's scope; what
		// matters is there is no DUPLICATE.)
		const users = msgs.filter(m => m.role === 'user');
		expect(users.length).toBe(1);
		expect(users[0].content).toBe('second prompt');
		expect(msgs.length).toBe(3);
		expect(msgs.map(m => m.content)).toEqual(['seed reply', 'second prompt', 'partial']);
	});

	it('does NOT duplicate a tail card whose jsonlUuid matches a delta message', async () => {
		// The same logical message can carry a different id family across polls while
		// sharing the transcript uuid. A uuid match must count as present-in-delta so
		// the existing copy is absorbed as an update, not resurrected as a duplicate.
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'seed-u1', role: 'user', type: 'text', content: 'first prompt', streaming: false },
				{ id: 'text-oldid', role: 'assistant', type: 'text', content: 'shared reply', streaming: false, jsonlUuid: 'uuid-shared' },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeDeltaResponse({
				lastUpdateId: 9,
				initialMessageCount: 1,
				isStreaming: true,
				messages: [
					// Same uuid, DIFFERENT id family — this is the SAME message as text-oldid.
					{ id: 'text-newid-0', role: 'assistant', type: 'text', content: 'shared reply', streaming: false, uuid: 'uuid-shared' },
					{ id: 'partial', role: 'assistant', type: 'text', content: 'partial', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const msgs = sessionStateManager.getSession(SID)!.messages;
		// Shared-uuid message appears exactly once (absorbed as an update).
		expect(msgs.filter(m => m.jsonlUuid === 'uuid-shared').length).toBe(1);
		expect(msgs.map(m => m.content)).toEqual(['first prompt', 'shared reply', 'partial']);
	});

	it('leaves a normal steady-state streaming delta unchanged (no dup of finalized tail)', async () => {
		// Belt: the fix must not alter ordinary streaming. The tail holds the prior
		// poll's finalized text + still-streaming text; both reappear in the fresh
		// delta under stable ids, so nothing is preserved (no duplicates).
		sessionStateManager.initSession(SID);
		sessionStateManager.applyServerState(
			SID,
			[
				{ id: 'u1', role: 'user', type: 'text', content: 'prompt', streaming: false },
				{ id: 't1', role: 'assistant', type: 'text', content: 'first block', streaming: false },
				{ id: 't2', role: 'assistant', type: 'text', content: 'streaming so', streaming: true },
			],
			true,
			5,
			{ inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 }
		);

		fetchMock.mockResolvedValue(
			makeDeltaResponse({
				lastUpdateId: 9,
				initialMessageCount: 1,
				isStreaming: true,
				messages: [
					{ id: 't1', role: 'assistant', type: 'text', content: 'first block', streaming: false },
					{ id: 't2', role: 'assistant', type: 'text', content: 'streaming so far', streaming: true },
				],
			})
		);

		sessionPoller.startStreaming(SID);
		await Promise.resolve();
		await Promise.resolve();

		const msgs = sessionStateManager.getSession(SID)!.messages;
		expect(msgs.map(m => m.id)).toEqual(['u1', 't1', 't2']);
		// The streaming block updated in place; no duplicate finalized 'first block'.
		expect(msgs.filter(m => m.id === 't1').length).toBe(1);
		expect(msgs.find(m => m.id === 't2')!.content).toBe('streaming so far');
	});
});
