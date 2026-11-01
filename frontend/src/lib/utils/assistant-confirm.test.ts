import { describe, expect, it } from 'vitest';
import { approvalCardFromRequest, approvalGuardrail, assistantConfirmationText, currentAssistantQuestion, isAssistantConfirm } from './assistant-confirm';
import type { PermissionRequest } from '$lib/api/client';

describe('assistantConfirmationText', () => {
	it('asks one plain yes-or-no question about the action', () => {
		expect(assistantConfirmationText({ tool_name: 'Bash', tool_input: { command: 'npm   test' } })).toBe('May I run `npm test`? Yes or no.');
		expect(assistantConfirmationText({ tool_name: 'Write', tool_input: { file_path: '/repo/a.md' } })).toBe('May I write `/repo/a.md`? Yes or no.');
		expect(assistantConfirmationText({ tool_name: 'Edit', tool_input: { file_path: 'b.ts' } })).toBe('May I change `b.ts`? Yes or no.');
		expect(assistantConfirmationText({ tool_name: 'WebFetch', tool_input: { url: 'https://example.com' } })).toBe('May I open https://example.com? Yes or no.');
	});

	it('names MCP tools by their short name and uses a description when given', () => {
		expect(assistantConfirmationText({ tool_name: 'mcp__github__merge_pull_request', tool_input: {} })).toBe('May I use merge_pull_request? Yes or no.');
		expect(assistantConfirmationText({ tool_name: 'Deploy', tool_input: { description: 'Deploy the site' } })).toBe('May I deploy the site? Yes or no.');
	});

	it('keeps long commands short and backticks from breaking out', () => {
		const sentence = assistantConfirmationText({ tool_name: 'Bash', tool_input: { command: `echo \`x\` ${'y'.repeat(300)}` } });
		expect(sentence.length).toBeLessThan(150);
		expect(sentence.match(/`/g)).toHaveLength(2);
	});
});

describe('currentAssistantQuestion', () => {
	const req = (id: string, confirm?: PermissionRequest['assistant_confirm']): PermissionRequest =>
		({ request_id: id, tool_name: 'Bash', tool_input: { command: `echo ${id}` }, ...(confirm !== undefined ? { assistant_confirm: confirm } : {}) });

	it('only requests marked assistant_confirm are asked in words', () => {
		expect(isAssistantConfirm(req('a'))).toBe(false);
		expect(isAssistantConfirm(req('b', null))).toBe(false);
		expect(isAssistantConfirm(req('c', { question: 'Q?' }))).toBe(true);
	});

	it('shows the oldest marked request in the server wording, one at a time', () => {
		const first = req('one', { question: 'Do you authorize me to run `git push`? Please answer yes or no.', chat_session_id: 'chat-1' });
		const result = currentAssistantQuestion([req('card'), first, req('two', { question: 'Second?' })]);
		expect(result?.request.request_id).toBe('one');
		expect(result?.text).toBe('Do you authorize me to run `git push`? Please answer yes or no.');
	});

	it('falls back to its own wording when the server sent none, and is empty without a marked request', () => {
		expect(currentAssistantQuestion([req('x', {})])?.text).toBe('May I run `echo x`? Yes or no.');
		expect(currentAssistantQuestion([req('card')])).toBeNull();
	});
});

describe('approvalCardFromRequest — a provider safety check', () => {
	// Mirrors tests/api/test_approval_transcript_fold.py (the REST fold), so a
	// reload reads the same card as the live reducer.
	const input = {
		guardrail: true,
		provider: 'anthropic',
		provider_label: 'Anthropic',
		category: 'cyber',
		explanation: 'Flagged by  the\ncyber classifier.',
		requested_model: 'claude-opus-4-8',
		suggested_model: 'claude-opus-4-7',
		suggested_by: 'provider',
		request_id: 'req_1',
		partial_output_discarded: true,
		message: 'ignored'
	};

	it('keeps the safety-check fields on the card', () => {
		const card = approvalCardFromRequest(
			'guardrail-t1',
			{ tool_name: 'ModelGuardrail', tool_input: input, assistant_confirm: { question: 'Continue?' } },
			[]
		);
		expect(card?.guardrail).toEqual({
			provider: 'anthropic',
			provider_label: 'Anthropic',
			category: 'cyber',
			explanation: 'Flagged by the cyber classifier.',
			requested_model: 'claude-opus-4-8',
			suggested_model: 'claude-opus-4-7',
			suggested_by: 'provider',
			partial_output_discarded: true
		});
	});

	it('adds nothing to an ordinary request', () => {
		const card = approvalCardFromRequest('p', { tool_name: 'Bash', tool_input: { command: 'ls' }, assistant_confirm: { question: 'Q?' } }, []);
		expect(card).not.toHaveProperty('guardrail');
		expect(approvalGuardrail({ tool_name: 'Bash', tool_input: {} })).toBeNull();
	});
});
