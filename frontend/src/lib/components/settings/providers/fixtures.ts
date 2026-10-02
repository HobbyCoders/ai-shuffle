/**
 * Test data for the Providers collection: model_providers rows and a Claude
 * Code mode summary shaped like the worker's answers.
 */
import type { ModelProvider } from '$lib/api/model-providers';
import type { ClaudeAuthSummary } from '$lib/api/claude-auth';

export function row(partial: Partial<ModelProvider> = {}): ModelProvider {
	return {
		id: 'p1',
		name: 'OpenAI API key',
		provider_type: 'openai',
		base_url: 'https://api.openai.com/v1',
		api_dialect: null,
		auth_token_set: true,
		auth_token_masked: 'sk-…Qm7d',
		models: [],
		default_model: null,
		small_fast_model: null,
		extra_env: {},
		context_window: null,
		max_output_tokens: null,
		enabled: true,
		created_at: null,
		updated_at: null,
		...partial
	};
}

export const chatgptRow = (partial: Partial<ModelProvider> = {}): ModelProvider =>
	row({
		id: 'chatgpt-1',
		name: 'ChatGPT',
		provider_type: 'chatgpt',
		base_url: 'https://chatgpt.com/backend-api/codex',
		auth_mode: 'chatgpt',
		auth_token_set: false,
		auth_token_masked: '',
		codex_access_token_set: true,
		codex_refresh_token_set: true,
		needs_reauth: false,
		models: [{ id: 'gpt-6.1', label: 'GPT-6.1' }],
		...partial
	});

export const grokRow = (partial: Partial<ModelProvider> = {}): ModelProvider =>
	row({
		id: 'grok-1',
		name: 'SuperGrok',
		provider_type: 'grok',
		base_url: 'https://api.x.ai/v1',
		auth_mode: 'grok',
		auth_token_set: false,
		auth_token_masked: '',
		codex_access_token_set: true,
		needs_reauth: true,
		...partial
	});

export const gatewayRow = (partial: Partial<ModelProvider> = {}): ModelProvider =>
	row({
		id: 'gw-1',
		name: 'Homelab vLLM',
		provider_type: 'custom',
		base_url: 'http://10.0.0.200:8000/v1',
		api_dialect: 'openai',
		auth_token_masked: '…k3p9',
		context_window: 131072,
		models: [{ id: 'qwen3-coder', label: 'Qwen3 Coder', context_window: 131072 }],
		...partial
	});

export function claudeSummary(partial: Partial<ClaudeAuthSummary> = {}): ClaudeAuthSummary {
	const mode = partial.mode ?? 'subscription';
	return {
		mode,
		mode_label: mode === 'off' ? 'Off' : mode === 'api_key' ? 'Anthropic API key' : 'Claude subscription',
		explicit: true,
		modes: [
			{ value: 'subscription', label: 'Claude subscription' },
			{ value: 'api_key', label: 'Anthropic API key' },
			{ value: 'off', label: 'Off' }
		],
		cli: {
			installed: true,
			signed_in: true,
			email: 'owner@example.com',
			org_name: 'HobbyCoders',
			plan: 'max',
			auth_method_label: 'Claude account',
			error: null
		},
		api_key: { set: true, last4: '4f2a' },
		gate: {
			ready: mode !== 'off',
			mode: mode === 'off' ? null : mode,
			claude_credential_ready: mode !== 'off',
			reason: mode === 'off' ? 'Claude is off on this worker.' : null,
			license_allows_runs: true
		},
		...partial
	};
}
