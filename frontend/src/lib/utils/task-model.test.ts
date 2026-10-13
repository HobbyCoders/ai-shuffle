/** The model label on running subagent tasks (#1827). */
import { describe, it, expect } from 'vitest';
import type { ProviderEntry } from '$lib/constants/models';
import { taskModel } from './task-model';

const openrouter = {
	id: 'prov-1',
	name: 'OpenRouter',
	provider_type: 'openrouter',
	models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
	default_model: 'openai/gpt-5.6',
	auth_kind: 'api',
	plan_type: null
} as unknown as ProviderEntry;

const labels: Record<string, string> = { 'prov:prov-1:openai/gpt-5.6': 'GPT-5.6', sonnet: 'Sonnet' };
const label = (value: string) => labels[value] ?? value;

describe('taskModel', () => {
	it('names a provider model as the model pickers do', () => {
		expect(taskModel('OpenRouter · openai/gpt-5.6', [openrouter], label)).toEqual({
			label: 'GPT-5.6',
			raw: 'OpenRouter · openai/gpt-5.6'
		});
	});

	it('names a native Claude model and drops the "asked for" note', () => {
		expect(taskModel('agent · sonnet (you asked for it)', [openrouter], label)?.label).toBe('Sonnet');
	});

	it('reads "inherit" as the provider default', () => {
		expect(taskModel('OpenRouter · inherit', [openrouter], label)?.label).toBe('GPT-5.6');
	});

	it('shows nothing when there is no label or no model to name', () => {
		expect(taskModel(null, [openrouter], label)).toBeNull();
		expect(taskModel('', [openrouter], label)).toBeNull();
		expect(taskModel('agent · inherit', [openrouter], label)).toBeNull();
	});

	it('falls back to the bare model id for an unknown provider', () => {
		expect(taskModel('Gone · some-model', [openrouter], label)?.label).toBe('some-model');
	});
});
