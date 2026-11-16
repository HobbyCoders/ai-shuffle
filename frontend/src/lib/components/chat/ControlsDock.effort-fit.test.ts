/**
 * #1641 review S9: the "Effort set to …" banner belongs to the model pick made
 * in one chat. Opening another chat takes it away, and only a pick in this
 * dock raises it: a chat switch, its binding loading or a change from another
 * tab fits the effort silently.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { notify, notifications } from '$lib/stores/notifications';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';
import { api } from '$lib/api/client';
import { hydrateModelRegistry } from '$lib/constants/models';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn((url: unknown) => String(url).includes('/events?')
		? new Promise(() => {})
		: Promise.resolve({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) })));
});

vi.mock('$lib/api/client', async (importOriginal) => ({
	...await importOriginal<Record<string, unknown>>(),
	getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} })
}));

const REGISTRY = {
	models: [
		{ value: 'opus', label: 'Opus', family: 'opus', effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'high', effort_metadata_version: 1 },
		{ value: 'haiku', label: 'Haiku', family: 'haiku', effort_levels: ['low', 'medium', 'high'], default_effort: 'medium', effort_metadata_version: 1 },
	],
	default: 'opus', legacy_map: {}, providers: [], claude: { auth_kind: 'subscription', plan_type: null },
};

const fitBanners = () => notifications.current.live.filter(n => /^Effort set to/.test(n.message ?? ''));

beforeEach(async () => {
	const registryGet = vi.spyOn(api, 'get').mockResolvedValue(REGISTRY);
	await hydrateModelRegistry();
	registryGet.mockRestore();
	vi.spyOn(api, 'put').mockImplementation(((_path: string, body: Record<string, unknown>) =>
		Promise.resolve({ effort: body.effort ?? null, fast: false, context_window: 0 })) as typeof api.put);
	appMode.set('code');
	activeSession.newChat();
	activeSession.setProject('proj-fit', null);
	activeSession.setModelOverride('opus');
	await activeSession.setEffortLevelOverride('max');
});

afterEach(() => {
	cleanup();
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	vi.restoreAllMocks();
	activeSession.current.sessionId = null;
	activeSession.newChat();
});
afterAll(() => vi.unstubAllGlobals());

async function pickHaiku() {
	await fireEvent.click(screen.getByRole('button', { name: /^Model:/ }));
	await fireEvent.click(await screen.findByRole('option', { name: /^Haiku/ }));
	await waitFor(() => expect(get(activeSession).modelOverride).toBe('haiku'));
	await tick();
}

describe('effort-fit banner (#1641, review S9)', () => {
	it('says so when a pick here lowers the level', async () => {
		render(ControlsDock, { props: {} });
		await pickHaiku();
		expect(get(activeSession).effortLevelOverride).toBe('high');
		expect(fitBanners().map(n => n.message)).toEqual(['Effort set to High: Haiku has no Max.']);
	});

	it('leaves with its chat', async () => {
		render(ControlsDock, { props: {} });
		await pickHaiku();
		expect(fitBanners()).toHaveLength(1);
		void activeSession.switchSession('chat-b');
		await waitFor(() => expect(get(activeSession).sessionId).toBe('chat-b'));
		await tick();
		expect(fitBanners()).toHaveLength(0);
	});

	it('leaves when the dock closes', async () => {
		const dock = render(ControlsDock, { props: {} });
		await pickHaiku();
		expect(fitBanners()).toHaveLength(1);
		dock.unmount();
		expect(fitBanners()).toHaveLength(0);
	});

	it('stays quiet when the model changes without a pick here', async () => {
		render(ControlsDock, { props: {} });
		await tick();
		// A chat's binding loading, or another tab's change: the store's model
		// moves without this dock's picker.
		activeSession.setModelOverride('haiku');
		await tick();
		await tick();
		expect(get(activeSession).effortLevelOverride).toBe('high');
		expect(fitBanners()).toHaveLength(0);
	});

	it('stays quiet when an opened chat runs a model without its level', async () => {
		render(ControlsDock, { props: {} });
		await tick();
		void activeSession.switchSession('chat-c');
		await waitFor(() => expect(get(activeSession).sessionId).toBe('chat-c'));
		activeSession.setModelOverride('haiku');
		await tick();
		await tick();
		expect(fitBanners()).toHaveLength(0);
	});

	it('stays quiet on first render of a chat whose level its model lacks', async () => {
		activeSession.setModelOverride('haiku');
		activeSession.current.effortLevelOverride = 'max';
		render(ControlsDock, { props: {} });
		await tick();
		await tick();
		expect(fitBanners()).toHaveLength(0);
	});
});
