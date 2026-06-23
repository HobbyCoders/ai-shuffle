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
