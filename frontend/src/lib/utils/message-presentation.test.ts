import { describe, expect, it } from 'vitest';
import {
	MAX_COLLAPSED_USER_MESSAGE_LENGTH,
	MAX_COLLAPSED_USER_MESSAGE_LINES,
	shouldCollapseUserMessage
} from './message-presentation';

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
