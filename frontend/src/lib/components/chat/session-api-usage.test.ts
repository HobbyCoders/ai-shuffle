import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { FAL_USAGE_ACCOUNT, lastPaidApiAccount, paidApiAccountOf, paidApiChargeOf } from './session-api-usage';

let seq = 0;
function call(tool: string, result: unknown, status: ChatMessage['toolStatus'] = 'complete'): ChatMessage {
	seq += 1;
	return {
		id: `m${seq}`,
		role: 'assistant',
		type: 'tool_use',
		content: '',
		toolName: tool,
		toolStatus: status,
		toolResult: typeof result === 'string' ? result : JSON.stringify(result),
	} as ChatMessage;
}

const completed = (source: string, account: string) => ({
	status: 'completed',
	choice: { source, account, worker: null, model: 'sample-model' },
	result: { success: true },
});

describe('paidApiAccountOf', () => {
	it('a finished generation names the account its choice ran on', () => {
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', completed('fal', 'fal-ai')))).toBe(FAL_USAGE_ACCOUNT);
		expect(paidApiAccountOf(call('mcp__fluid_bundled__mcp__ai-tools__generateVideo', completed('xai', 'sample-xai-key')))).toBe('sample-xai-key');
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', completed('openai', 'sample-openai-key')))).toBe('sample-openai-key');
	});

	it('plan sources, refusals, failures and running calls bill no paid API', () => {
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', completed('chatgpt', 'sample-plan')))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__generateVideo', completed('supergrok', 'sample-grok')))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', { ...completed('fal', 'fal-ai'), status: 'refused' }))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', completed('fal', 'fal-ai'), 'running'))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', completed('fal', 'fal-ai'), 'error'))).toBeNull();
	});

	it('the older fal-only tools count as fal unless they name another provider', () => {
		expect(paidApiAccountOf(call('mcp__ai-tools__editImage', { success: true, provider_used: 'fal-ai' }))).toBe(FAL_USAGE_ACCOUNT);
		expect(paidApiAccountOf(call('mcp__ai-tools__imageToVideo', { success: true }))).toBe(FAL_USAGE_ACCOUNT);
		expect(paidApiAccountOf(call('mcp__ai-tools__editImage', { success: true, provider_used: 'chatgpt_subscription' }))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__editImage', { success: false }))).toBeNull();
	});

	it('a charge keeps the worker that ran it; the fal-only tools name none', () => {
		const remote = { ...completed('fal', 'fal-ai'), choice: { source: 'fal', account: 'fal-ai', worker: 'device-other' } };
		expect(paidApiChargeOf(call('mcp__ai-tools__generateImage', remote))).toEqual({ account: FAL_USAGE_ACCOUNT, worker: 'device-other' });
		expect(paidApiChargeOf(call('mcp__ai-tools__generateImage', completed('fal', 'fal-ai')))).toEqual({ account: FAL_USAGE_ACCOUNT, worker: null });
		expect(paidApiChargeOf(call('mcp__ai-tools__editImage', { success: true }))).toEqual({ account: FAL_USAGE_ACCOUNT, worker: null });
	});

	it('other tools and unreadable results are ignored', () => {
		expect(paidApiAccountOf(call('Bash', completed('fal', 'fal-ai')))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__searchModels', { success: true }))).toBeNull();
		expect(paidApiAccountOf(call('mcp__ai-tools__generateImage', 'not json'))).toBeNull();
	});
});

describe('lastPaidApiAccount', () => {
	it('returns the most recent paid call and skips later unpaid ones', () => {
		const messages = [
			call('mcp__ai-tools__generateImage', completed('fal', 'fal-ai')),
			call('mcp__ai-tools__generateImage', completed('xai', 'sample-xai-key')),
			call('mcp__ai-tools__generateImage', completed('chatgpt', 'sample-plan')),
		];
		expect(lastPaidApiAccount(messages)).toBe('sample-xai-key');
		expect(lastPaidApiAccount([])).toBeNull();
	});
});
