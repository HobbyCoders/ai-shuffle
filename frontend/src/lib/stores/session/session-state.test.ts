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
});
