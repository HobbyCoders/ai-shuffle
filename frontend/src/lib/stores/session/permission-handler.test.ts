import { describe, expect, it, vi } from 'vitest';
import { createPermissionHandler } from './permission-handler';

describe('permission-handler', () => {
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
});
