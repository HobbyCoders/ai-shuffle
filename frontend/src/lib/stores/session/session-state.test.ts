import { afterEach, describe, expect, it } from 'vitest';
import { sessionStateManager, type SessionUsage } from './session-state.svelte';
import type { ChatMessage } from '../types';

const SESSION_ID = 'session-agent-usage';

const EMPTY_USAGE: SessionUsage = {
	inputTokens: 0,
	outputTokens: 0,
	cacheCreationTokens: 0,
	cacheReadTokens: 0,
	contextUsed: 0,
};

afterEach(() => {
	sessionStateManager.removeSession(SESSION_ID);
});

describe('session-state', () => {
	it('replaces subagent messages when only agentUsage changes', () => {
		const initialMessage: ChatMessage = {
			id: 'subagent-1',
			role: 'assistant',
			type: 'subagent',
			content: 'Running worker',
			streaming: true,
			agentStatus: 'running',
			agentChildren: [],
			agentUsage: {
				total_tokens: 12,
				tool_uses: 1,
				duration_ms: 1000,
			},
		};

		sessionStateManager.initSession(SESSION_ID);
		sessionStateManager.applyServerState(
			SESSION_ID,
			[initialMessage],
			true,
			1,
			EMPTY_USAGE
		);

		const firstSession = sessionStateManager.getSession(SESSION_ID);
		expect(firstSession).toBeDefined();
		const firstMessage = firstSession!.messages[0];

		const updatedMessage: ChatMessage = {
			...initialMessage,
			agentUsage: {
				total_tokens: 24,
				tool_uses: 1,
				duration_ms: 2000,
			},
		};

		sessionStateManager.applyServerState(
			SESSION_ID,
			[updatedMessage],
			true,
			2,
			EMPTY_USAGE,
			firstSession!.messages
		);

		const secondSession = sessionStateManager.getSession(SESSION_ID);
		expect(secondSession).toBeDefined();
		expect(secondSession!.messages[0]).not.toBe(firstMessage);
		expect(secondSession!.messages[0].id).toBe(firstMessage.id);
		expect(secondSession!.messages[0].agentUsage?.total_tokens).toBe(24);
		expect(secondSession!.messages[0].agentUsage?.duration_ms).toBe(2000);
	});

	it('replaces thinking messages when only thinkingTokens changes', () => {
		// Guards the live thinking-token counter: a poll where only the
		// estimated token count changed must re-render the thinking block
		// (new object ref) while preserving the message id for DOM stability.
		const initialMessage: ChatMessage = {
			id: 'thinking-1',
			role: 'assistant',
			type: 'thinking',
			content: 'Reasoning about the problem...',
			streaming: true,
			thinkingTokens: 150,
		};

		sessionStateManager.initSession(SESSION_ID);
		sessionStateManager.applyServerState(
			SESSION_ID,
			[initialMessage],
			true,
			1,
			EMPTY_USAGE
		);

		const firstSession = sessionStateManager.getSession(SESSION_ID);
		expect(firstSession).toBeDefined();
		const firstMessage = firstSession!.messages[0];

		const updatedMessage: ChatMessage = { ...initialMessage, thinkingTokens: 174 };

		sessionStateManager.applyServerState(
			SESSION_ID,
			[updatedMessage],
			true,
			2,
			EMPTY_USAGE,
			firstSession!.messages
		);

		const secondSession = sessionStateManager.getSession(SESSION_ID);
		expect(secondSession).toBeDefined();
		expect(secondSession!.messages[0]).not.toBe(firstMessage); // re-rendered
		expect(secondSession!.messages[0].id).toBe(firstMessage.id); // id preserved
		expect(secondSession!.messages[0].thinkingTokens).toBe(174);
	});

	it('carries a newly-backfilled jsonlUuid onto an otherwise-identical user message', () => {
		// Repro of the "rewind blocked until reload" bug. A just-sent user prompt
		// is delivered first WITHOUT a jsonlUuid (the post-turn backfill that
		// stamps the transcript uuid hasn't run yet), then a later poll delivers
		// the SAME message WITH the backfilled jsonlUuid. compareMessage must
		// propagate the new uuid — otherwise it deems the pair identical (it
		// ignores jsonlUuid) and returns the stale pre-uuid object, so
		// MessageActions sees messageUuid == null and refuses to rewind
		// ("Cannot rewind: this message is missing its checkpoint id").
		const withoutUuid: ChatMessage = {
			id: 'user-1782782012740',
			role: 'user',
			type: 'text',
			content: 'hello 2',
		};

		sessionStateManager.initSession(SESSION_ID);
		sessionStateManager.applyServerState(SESSION_ID, [withoutUuid], false, 1, EMPTY_USAGE);

		const firstSession = sessionStateManager.getSession(SESSION_ID);
		expect(firstSession).toBeDefined();
		expect(firstSession!.messages[0].jsonlUuid).toBeUndefined();

		const withUuid: ChatMessage = {
			...withoutUuid,
			jsonlUuid: '4705c137-fdcc-4fe9-be05-935a78a2c018',
		};

		sessionStateManager.applyServerState(
			SESSION_ID,
			[withUuid],
			false,
			2,
			EMPTY_USAGE,
			firstSession!.messages
		);

		const secondSession = sessionStateManager.getSession(SESSION_ID);
		expect(secondSession).toBeDefined();
		// id preserved for DOM stability, but the uuid must now be present.
		expect(secondSession!.messages[0].id).toBe('user-1782782012740');
		expect(secondSession!.messages[0].jsonlUuid).toBe(
			'4705c137-fdcc-4fe9-be05-935a78a2c018'
		);
	});
});
