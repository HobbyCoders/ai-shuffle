/**
 * Test data for the Providers collection: model_providers rows and a Claude
 * Code mode summary shaped like the worker's answers.
 */
import type { ModelProvider } from '$lib/api/model-providers';
import type { ClaudeAuthSummary, ClaudeCloudConfig, ClaudeCloudMethod } from '$lib/api/claude-auth';

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

const f = (
	name: string,
	label: string,
	env: string,
	secret: boolean,
	kind: ClaudeCloudMethod['fields'][number]['kind'] = 'text',
	choice: string | null = null,
	required = true
) => ({ name, label, env, secret, kind, required, choice });

/** The worker's four cloud methods, as `/settings/claude-auth/cloud` lists them. */
export const CLOUD_METHODS: ClaudeCloudMethod[] = [
	{
		id: 'bedrock',
		label: 'Amazon Bedrock',
		docs: 'https://code.claude.com/docs/en/amazon-bedrock',
		allowlist: ['CLAUDE_CODE_USE_BEDROCK', 'AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
			'AWS_SESSION_TOKEN', 'AWS_BEARER_TOKEN_BEDROCK'],
		choices: [
			{ id: 'keys', label: 'AWS access keys' },
			{ id: 'bearer', label: 'Bedrock API key' }
		],
		fields: [
			f('region', 'AWS region', 'AWS_REGION', false, 'region'),
			f('access_key_id', 'Access key ID', 'AWS_ACCESS_KEY_ID', true, 'text', 'keys'),
			f('secret_access_key', 'Secret access key', 'AWS_SECRET_ACCESS_KEY', true, 'text', 'keys'),
			f('session_token', 'Session token', 'AWS_SESSION_TOKEN', true, 'text', 'keys', false),
			f('bearer_token', 'Bedrock API key', 'AWS_BEARER_TOKEN_BEDROCK', true, 'text', 'bearer')
		]
	},
	{
		id: 'vertex',
		label: 'Google Vertex AI',
		docs: 'https://code.claude.com/docs/en/google-vertex-ai',
		allowlist: ['CLAUDE_CODE_USE_VERTEX', 'CLOUD_ML_REGION', 'ANTHROPIC_VERTEX_PROJECT_ID',
			'GOOGLE_APPLICATION_CREDENTIALS'],
		choices: [],
		fields: [
			f('region', 'Region', 'CLOUD_ML_REGION', false, 'region'),
			f('project_id', 'Project ID', 'ANTHROPIC_VERTEX_PROJECT_ID', false),
			f('credentials_json', 'Service account key (JSON)', 'GOOGLE_APPLICATION_CREDENTIALS', true, 'json')
		]
	},
	{
		id: 'foundry',
		label: 'Microsoft Foundry',
		docs: 'https://code.claude.com/docs/en/microsoft-foundry',
		allowlist: ['CLAUDE_CODE_USE_FOUNDRY', 'ANTHROPIC_FOUNDRY_RESOURCE', 'ANTHROPIC_FOUNDRY_BASE_URL',
			'ANTHROPIC_FOUNDRY_API_KEY'],
		choices: [
			{ id: 'resource', label: 'Resource name' },
			{ id: 'base_url', label: 'Base URL' }
		],
		fields: [
			f('resource', 'Resource name', 'ANTHROPIC_FOUNDRY_RESOURCE', false, 'resource', 'resource'),
			f('base_url', 'Base URL', 'ANTHROPIC_FOUNDRY_BASE_URL', false, 'url', 'base_url'),
			f('api_key', 'API key', 'ANTHROPIC_FOUNDRY_API_KEY', true)
		]
	},
	{
		id: 'gateway',
		label: 'LLM gateway',
		docs: 'https://code.claude.com/docs/en/llm-gateway-connect',
		allowlist: ['ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN'],
		choices: [],
		fields: [
			f('base_url', 'Base URL', 'ANTHROPIC_BASE_URL', false, 'url'),
			f('auth_token', 'Auth token', 'ANTHROPIC_AUTH_TOKEN', true)
		]
	}
];

/** `/settings/claude-auth/cloud` with nothing saved, or with `partial` applied. */
export function cloudConfig(partial: Partial<ClaudeCloudConfig> = {}): ClaudeCloudConfig {
	return {
		method: null,
		values: {},
		secrets: {},
		status: { method: null, method_label: null, configured: false, ready: false, missing: [], storage_locked: false },
		methods: CLOUD_METHODS,
		warning:
			'Agents on this worker can run shell commands and can read these credentials, including the Vertex AI key file on this worker\'s disk, with their own tools and in terminals. A project\'s or your own .claude/settings.json can still change where Claude connects, and this worker\'s proxy and certificate settings still apply. Use an account with the smallest permissions that work, and only on a worker you trust.',
		gateway_warning: 'All Claude traffic from this worker goes to the address you enter. The browser tool also receives this address and token.',
		...partial
	};
}
