import { describe, expect, it } from 'vitest';
import { approvalCardFromRequest, assistantConfirmationText, currentAssistantQuestion, isAssistantConfirm } from './assistant-confirm';
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

describe('a safety check an earlier build marked assistant_confirm', () => {
	const confirm = { question: 'May I continue this turn on another model? Yes or no.' };
	const byTool: PermissionRequest = { request_id: 'g1', tool_name: 'ModelGuardrail', tool_input: {}, assistant_confirm: confirm };
	const byInput: PermissionRequest = { request_id: 'g2', tool_name: 'Other', tool_input: { guardrail: true }, assistant_confirm: confirm };
	const ordinary: PermissionRequest = { request_id: 'b1', tool_name: 'Bash', tool_input: { command: 'ls' }, assistant_confirm: confirm };

	it.each([['tool name', byTool], ['input marker', byInput]])('keeps its buttons, by %s', (_how, request) => {
		// Not asked in words, so ChatView's dialog filter keeps it...
		expect(isAssistantConfirm(request)).toBe(false);
		expect([request].filter((r) => !isAssistantConfirm(r))).toEqual([request]);
		// ...nothing marks it as the question on screen...
		expect(currentAssistantQuestion([request])).toBeNull();
		// ...and the transcript raises no in-words card for it.
		expect(approvalCardFromRequest(request.request_id, { ...request }, [])).toBeNull();
	});

	it('does not stand in front of an ordinary approval asked in words', () => {
		expect(currentAssistantQuestion([byTool, ordinary])?.request.request_id).toBe('b1');
		expect(approvalCardFromRequest('b1', { ...ordinary }, [])).not.toBeNull();
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
