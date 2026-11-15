import { describe, expect, it } from 'vitest';
import {
	MAX_COLLAPSED_USER_MESSAGE_LENGTH,
	MAX_COLLAPSED_USER_MESSAGE_LINES,
	shouldCollapseUserMessage,
	canRewindUserMessage,
	typedWords,
	firstMessageWords
} from './message-presentation';
import sharedTitleCases from './attachment-title-cases.json';

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

describe('typedWords (#1654)', () => {
	it('cuts one attachment run into the last word', () => {
		expect(typedWords('QA E4 worktree B@/tmp/up/shot 1.png', ['/tmp/up/shot 1.png'])).toBe('QA E4 worktree B');
	});
	it('cuts several attachments wherever they sit', () => {
		const a = '/ws/uploads/a.png';
		const b = '/ws/uploads/b notes.md';
		expect(typedWords(`compare @${a} with @${b}\n\n@${a}`, [a, b])).toBe('compare with');
	});
	it('keeps a mention that is not an attachment, and is empty for attachments only', () => {
		expect(typedWords('email @user today', ['/ws/a.png'])).toBe('email @user today');
		expect(typedWords('@/ws/a.png', ['/ws/a.png'])).toBe('');
		expect(typedWords('  plain words  ', undefined)).toBe('plain words');
	});
});

// The same cases the server's derive_session_title is tested against
// (tests/core/test_session_title.py), so the two title rules cannot drift.
describe('first-message words match the server title rule', () => {
	it.each(sharedTitleCases)('$name', ({ prompt, paths, title, branchWords }) => {
		expect(firstMessageWords(prompt, paths)).toBe(title);
		expect(typedWords(prompt, paths)).toBe(branchWords);
	});
});
