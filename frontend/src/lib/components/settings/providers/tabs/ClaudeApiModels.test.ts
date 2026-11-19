/** Anthropic API key > Models: that key's own Models API list, chosen only on the selected key. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
import ClaudeApiModels from './ClaudeApiModels.svelte';

const ALIASES = [
	{ value: 'haiku', label: 'Haiku', runs: 'claude-haiku-5-5' },
	{ value: 'opus', label: 'Opus', runs: 'claude-opus-5' }
];
const keyModels = (id: string, models: string[] | null, enabled: string[] = [], active = id === 'acc-a') => ({
	account_id: id,
	active,
	aliases: ALIASES,
	catalog: models && { fetched_at: '2026-11-18T00:00:00Z', models: models.map((value) => ({ value, label: value.toUpperCase(), created_at: null })) },
	enabled
});
const REGISTRY = {
	default: 'sonnet',
	legacy_map: {},
	models: ['haiku', 'sonnet', 'opus', 'fable'].map((value) => ({ value, label: value, family: value })),
	providers: [],
	claude: { auth_kind: 'api', plan_type: null }
};

let saved: Record<string, ReturnType<typeof keyModels>>;

beforeEach(() => {
	saved = {
		'acc-a': keyModels('acc-a', ['claude-opus-4-6', 'claude-sonnet-4-6'], ['claude-sonnet-4-6']),
		'acc-b': keyModels('acc-b', ['claude-opus-4-1'], ['claude-sonnet-4-6'])
	};
	mocks.get.mockImplementation(async (url: string) => {
		const id = /api-keys\/([^/]+)$/.exec(url)?.[1];
		return id ? saved[decodeURIComponent(id)] : REGISTRY;
	});
	mocks.put.mockImplementation(async (_url: string, body: { model: string; enabled: boolean }) => ({
		models: body.enabled ? ['claude-sonnet-4-6', body.model] : ['claude-sonnet-4-6'].filter((v) => v !== body.model)
	}));
	mocks.post.mockImplementation(async (url: string) => saved[decodeURIComponent(/api-keys\/([^/]+)\/refresh/.exec(url)![1])]);
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

const boxes = () => within(screen.getByTestId('claude-api-versioned-models')).getAllByRole('checkbox') as HTMLInputElement[];

it('lists the selected key’s versions with their saved state and the aliases with what they run', async () => {
	render(ClaudeApiModels, { props: { accountId: 'acc-a', active: true } });
	await screen.findByTestId('claude-api-versioned-models');
	expect(mocks.get).toHaveBeenCalledWith('/models/claude/api-keys/acc-a');
	expect(boxes().map((b) => [b.checked, b.disabled])).toEqual([[false, false], [true, false]]);
	expect(within(screen.getByTestId('claude-api-alias-models')).getByText('Opus · claude-opus-5')).toBeTruthy();
	expect(screen.getByTestId('claude-api-models-status').textContent).toContain('Listed by Anthropic');
	expect(mocks.post).not.toHaveBeenCalled(); // Already listed: no automatic request.
});

it('checks one id at a time on the selected key', async () => {
	render(ClaudeApiModels, { props: { accountId: 'acc-a', active: true } });
	await screen.findByTestId('claude-api-versioned-models');
	await fireEvent.click(boxes()[0]);
	await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/models/claude/api-enabled', { model: 'claude-opus-4-6', enabled: true }));
	await waitFor(() => expect(boxes()[0].checked).toBe(true));
});

it('an unselected key shows its own list but cannot change the pickers', async () => {
	render(ClaudeApiModels, { props: { accountId: 'acc-b', active: false } });
	await screen.findByTestId('claude-api-versioned-models');
	expect(screen.getByText('claude-opus-4-1')).toBeTruthy();
	expect(screen.queryByText('claude-opus-4-6')).toBeNull();
	expect(boxes().every((b) => b.disabled)).toBe(true);
	expect(screen.getByText(/Select this key to choose/)).toBeTruthy();
});

it('a key never listed is asked once, and an empty list says so', async () => {
	saved['acc-a'] = keyModels('acc-a', null);
	mocks.post.mockResolvedValueOnce(keyModels('acc-a', []));
	render(ClaudeApiModels, { props: { accountId: 'acc-a', active: true } });
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/models/claude/api-keys/acc-a/refresh', {}));
	await screen.findByTestId('claude-api-models-empty');
	expect(mocks.post).toHaveBeenCalledTimes(1);
});

it('a refresh failure shows its plain reason and keeps the list', async () => {
	render(ClaudeApiModels, { props: { accountId: 'acc-a', active: true } });
	await screen.findByTestId('claude-api-versioned-models');
	mocks.post.mockRejectedValueOnce(new Error('Anthropic rejected this API key.'));
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic rejected this API key.');
	expect(boxes()).toHaveLength(2);
});

it('a reply for the previous key never lands on the next key’s page', async () => {
	let answerB: (value: unknown) => void = () => {};
	const { rerender } = render(ClaudeApiModels, { props: { accountId: 'acc-b', active: false } });
	await screen.findByTestId('claude-api-versioned-models');
	mocks.post.mockImplementationOnce(() => new Promise((resolve) => (answerB = resolve)));
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	await rerender({ accountId: 'acc-a', active: true });
	await screen.findByText('claude-opus-4-6');
	answerB(keyModels('acc-b', ['claude-opus-4-1', 'claude-haiku-4-5']));
	await new Promise((resolve) => setTimeout(resolve, 0));
	expect(screen.queryByText('claude-opus-4-1')).toBeNull();
	expect(screen.getByText('claude-opus-4-6')).toBeTruthy();
	expect(screen.queryByRole('alert')).toBeNull();
});
