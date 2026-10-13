import { describe, expect, it, vi } from 'vitest';
import { createPermissionHandler } from './permission-handler';

describe('permission-handler', () => {
	it('returns a failed question send so the UI can keep the draft and report it', () => {
		const wsSend = vi.fn(() => false);
		const handler = createPermissionHandler({ wsSend });
		expect(handler.sendUserQuestionResponse('q', 'tool', { Answer: 'yes' }, 'session')).toBe(false);
	});
	it('sends session-scoped permission responses', () => {
		const wsSend = vi.fn(() => true);
		const handler = createPermissionHandler({ wsSend });

		handler.sendPermissionResponse('perm-1', 'allow', 'session', 'npm *', 'session-1');

		expect(wsSend).toHaveBeenCalledWith({
			type: 'permission_response',
			request_id: 'perm-1',
			session_id: 'session-1',
			decision: 'allow',
			remember: 'session',
			pattern: 'npm *',
		});
	});

	it('sends session-scoped user question responses', () => {
		const wsSend = vi.fn(() => true);
		const handler = createPermissionHandler({ wsSend });

		handler.sendUserQuestionResponse(
			'question-1',
			'tool-1',
			{ Confirm: 'yes' },
			'session-1'
		);

		expect(wsSend).toHaveBeenCalledWith({
			type: 'user_question_response',
			request_id: 'question-1',
			session_id: 'session-1',
			tool_use_id: 'tool-1',
			answers: { Confirm: 'yes' },
		});
	});

	it('omits dismissed/skipped when the question was answered normally', () => {
		// Typed parameter so `mock.calls[0][0]` is the sent payload, not `never`.
		const wsSend = vi.fn((_message: unknown) => true);
		const handler = createPermissionHandler({ wsSend });

		handler.sendUserQuestionResponse('question-1', 'tool-1', { Confirm: 'yes' }, 'session-1', {
			dismissed: false,
			skipped: [],
		});

		const payload = wsSend.mock.calls[0][0] as Record<string, unknown>;
		expect(payload).not.toHaveProperty('dismissed');
		expect(payload).not.toHaveProperty('skipped');
	});

	it('flags a dismissed question so the backend can resolve the tool call', () => {
		const wsSend = vi.fn(() => true);
		const handler = createPermissionHandler({ wsSend });

		handler.sendUserQuestionResponse('question-1', 'tool-1', {}, 'session-1', {
			dismissed: true,
		});

		expect(wsSend).toHaveBeenCalledWith({
			type: 'user_question_response',
			request_id: 'question-1',
			session_id: 'session-1',
			tool_use_id: 'tool-1',
			answers: {},
			dismissed: true,
		});
	});

	it('carries skipped question keys alongside the answered ones', () => {
		const wsSend = vi.fn(() => true);
		const handler = createPermissionHandler({ wsSend });

		handler.sendUserQuestionResponse(
			'question-1',
			'tool-1',
			{ 'Which db?': 'Postgres' },
			'session-1',
			{ skipped: ['Which cache?'] }
		);

		expect(wsSend).toHaveBeenCalledWith({
			type: 'user_question_response',
			request_id: 'question-1',
			session_id: 'session-1',
			tool_use_id: 'tool-1',
			answers: { 'Which db?': 'Postgres' },
			skipped: ['Which cache?'],
		});
	});
});
