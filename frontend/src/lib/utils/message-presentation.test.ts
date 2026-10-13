import { describe, expect, it } from 'vitest';
import {
	MAX_COLLAPSED_USER_MESSAGE_LENGTH,
	MAX_COLLAPSED_USER_MESSAGE_LINES,
	shouldCollapseUserMessage,
	canRewindUserMessage
} from './message-presentation';

describe('canRewindUserMessage', () => {
	const user = { id: 'user', role: 'user' as const, content: 'ordinary legacy prompt' };
	it('retains legacy normal prompts, including uuid-less content resolution', () => {
		expect(canRewindUserMessage(user)).toBe(true);
		expect(canRewindUserMessage({ ...user, jsonlUuid: 'checkpoint' }, true)).toBe(true);
		expect(canRewindUserMessage({ ...user, checkpointBacked: true }, true)).toBe(true);
	});
	it('refuses explicit and inferred steers without hiding their message', () => {
		expect(canRewindUserMessage({ ...user, checkpointBacked: false, jsonlUuid: 'stale' })).toBe(false);
		expect(canRewindUserMessage(user, true)).toBe(false);
		expect(canRewindUserMessage({ ...user, type: 'command' })).toBe(false);
		expect(canRewindUserMessage({ ...user, role: 'assistant' })).toBe(false);
	});
});

describe('shouldCollapseUserMessage', () => {
	it('leaves short and empty prompts fully visible', () => {
		expect(shouldCollapseUserMessage('')).toBe(false);
		expect(shouldCollapseUserMessage('Fix the failing test.')).toBe(false);
	});

	it('collapses prompts beyond the content threshold', () => {
		expect(shouldCollapseUserMessage('x'.repeat(MAX_COLLAPSED_USER_MESSAGE_LENGTH))).toBe(false);
		expect(shouldCollapseUserMessage('x'.repeat(MAX_COLLAPSED_USER_MESSAGE_LENGTH + 1))).toBe(true);
	});

	it('collapses prompts beyond the line threshold', () => {
		expect(shouldCollapseUserMessage(Array(MAX_COLLAPSED_USER_MESSAGE_LINES).fill('line').join('\n'))).toBe(false);
		expect(shouldCollapseUserMessage(Array(MAX_COLLAPSED_USER_MESSAGE_LINES + 1).fill('line').join('\n'))).toBe(true);
	});
});
