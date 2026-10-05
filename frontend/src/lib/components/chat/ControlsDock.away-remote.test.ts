/**
 * Away mode in the composer for a session another worker owns: the badge
 * follows the session row this worker mirrors, so a toggle made on another
 * device or worker shows here without reopening the chat.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';
import { api } from '$lib/api/client';
import { getModels, hydrateModelRegistry } from '$lib/constants/models';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) }));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} }) };
});

const NOW = '2026-10-05T12:00:00Z';

function remoteRow(id: string, away: boolean | null | undefined): Record<string, unknown> {
	const row: Record<string, unknown> = {
		id, title: 'Remote chat', status: 'idle', is_active: true, profile_id: 'default', project_id: null,
		total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0,
		cache_read_tokens: 0, context_tokens: 0, turn_count: 1, tags: [], parent_session_id: null,
		fork_point_message_index: null, has_forks: false, created_at: NOW, updated_at: NOW,
		owner_device_id: 'dev-serverworker', device_name: 'Unraid', is_remote: true, controllable: true,
		permission_mode: 'bypassPermissions'
	};
	if (away !== undefined) row.away_mode = away;
	return row;
}

let rows: Record<string, unknown>[] = [];
let detailAway: boolean | null = null;

beforeEach(async () => {
	appMode.set('code');
	activeSession.newChat();
	const registryGet = vi.spyOn(api, 'get').mockResolvedValue({ models: getModels(), providers: [], claude: { auth_kind: 'subscription', plan_type: null } });
	await hydrateModelRegistry();
	registryGet.mockRestore();
	vi.spyOn(api, 'get').mockImplementation(((path: string) => {
		if (path.startsWith('/sessions/cursor')) {
			return Promise.resolve({ items: rows, has_more: false, next_cursor: null, next_cursor_id: null });
		}
		const id = path.match(/^\/sessions\/([^/?]+)$/)?.[1];
		if (id) return Promise.resolve({ ...remoteRow(id, detailAway), messages: [] });
		return Promise.reject(new Error(`unmocked ${path}`));
	}) as never);
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	rows = [];
	appMode.set('code');
	activeSession.newChat();
});
afterAll(() => vi.unstubAllGlobals());

async function openRemote(id: string) {
	await activeSession.switchSession(id);
	await waitFor(() => expect(get(activeSession).sessionId).toBe(id));
	render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
}

const permission = () => screen.getByRole('button', { name: /^Permission mode:/ });

describe('ControlsDock Away mode on a remote-owned session', () => {
	it('shows Away from the mirrored session when the chat opens', async () => {
		detailAway = true;
		rows = [remoteRow('remote-open', true)];
		await appData.loadSessions();
		await openRemote('remote-open');
		await waitFor(() => expect(permission().getAttribute('aria-label')).toContain('Away mode on'));
		expect(permission().textContent).toContain('Away');
	});

	it('follows a toggle made on another device or worker while the chat is open', async () => {
		detailAway = false;
		rows = [remoteRow('remote-live', false)];
		await appData.loadSessions();
		await openRemote('remote-live');
		await waitFor(() => expect(permission().getAttribute('aria-label')).not.toContain('Away'));

		// The owner published Away on; this worker's mirror applied it and the
		// session list reloaded (resources_changed "sessions").
		rows = [remoteRow('remote-live', true)];
		await appData.loadSessions();
		await waitFor(() => expect(permission().getAttribute('aria-label')).toContain('Away mode on'));

		rows = [remoteRow('remote-live', false)];
		await appData.loadSessions();
		await waitFor(() => expect(permission().getAttribute('aria-label')).not.toContain('Away'));
	});

	it('reads as off when the owning worker is too old to send the flag', async () => {
		detailAway = null;
		rows = [remoteRow('remote-old', undefined)];
		await appData.loadSessions();
		await openRemote('remote-old');
		await waitFor(() => expect(permission().getAttribute('aria-label')).toMatch(/^Permission mode:/));
		expect(permission().getAttribute('aria-label')).not.toContain('Away');
	});
});
