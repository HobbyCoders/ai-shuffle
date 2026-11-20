/** A cloud, API-key or inactive Claude account's Models tab: the short ids only. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: vi.fn() },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
import BuiltInModels from './BuiltInModels.svelte';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';

beforeEach(async () => {
	// The ACTIVE sign-in is a subscription whose Claude Code lists Mythos.
	mocks.get.mockResolvedValue({
		default: 'sonnet',
		legacy_map: {},
		models: [
			{ value: 'haiku', label: 'Haiku 5.5', family: 'haiku' },
			{ value: 'sonnet', label: 'Sonnet 5.5', family: 'sonnet' },
			{ value: 'opus', label: 'Opus 5.5', family: 'opus' },
			{ value: 'fable', label: 'Fable 5.2', family: 'fable' },
			{ value: 'claude-mythos-5-1', label: 'Mythos 5.1', family: 'mythos' },
			{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus' }
		],
		providers: [],
		claude: { auth_kind: 'subscription', plan_type: null }
	});
	await hydrateModelRegistry();
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

it("does not show the active subscription's listed versions", () => {
	render(BuiltInModels);
	for (const label of ['Haiku 5.5', 'Sonnet 5.5', 'Opus 5.5', 'Fable 5.2']) {
		expect(screen.getByText(label)).toBeTruthy();
	}
	expect(screen.queryByText('Mythos 5.1')).toBeNull();
	expect(screen.queryByText('Opus 4.6')).toBeNull();
});
