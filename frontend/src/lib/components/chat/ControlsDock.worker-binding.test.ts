/**
 * #418 on the desktop composer: opening a hidden Task worker from its parent
 * must show the worker's own agent, access and Fast — loading while its `/live`
 * metadata is in flight, never the parent's agent, Ask or Fast off.
 *
 * Drives the real activeSession store: the switch, the `/live` hydration and
 * the dock / agent chip that read it.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import ControlsDock from './ControlsDock.svelte';
import ProfileSelector from './ProfileSelector.svelte';
import '../../test-stubs/composer-dom';
import { api } from '$lib/api/client';
import { sessionEphemeralCache } from '$lib/stores/session-cache';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn((url: unknown) => String(url).includes('/events?')
		? new Promise(() => {})
		: Promise.resolve({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) })));
});

vi.mock('$lib/api/client', async (importOriginal) => ({
	...await importOriginal<Record<string, unknown>>(),
	getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} })
}));

const WORKER_META = {
	is_worker: true, mode: 'code', project_id: 'proj-1', model: 'gpt-6.1-sol', provider_id: 'codex', effort: 'high',
	profile_id: 'investigator', permission_mode: 'bypassPermissions', fast: true,
};

let releaseLive: ((value: unknown) => void) | null = null;

beforeEach(async () => {
	releaseLive = null;
	vi.spyOn(api, 'get').mockImplementation(((path: string) => {
		if (path === '/agents') return Promise.resolve([
			{ id: 'overseer', name: 'Overseer' }, { id: 'investigator', name: 'Investigator' },
		]);
		if (path.startsWith('/sessions/worker/live')) return new Promise<unknown>(resolve => { releaseLive = resolve; });
		return Promise.reject(new Error('not found'));
	}) as typeof api.get);
	await appData.loadProfiles();
	// The parent: an Overseer project chat on Ask with Fast off.
	appMode.set('code');
	activeSession.newChat();
	activeSession.setProject('proj-1', null);
	activeSession.setProfile('overseer');
	activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
	await activeSession.setPermissionModeOverride('default');
	await activeSession.setFastMode(false);
});

afterEach(() => {
	cleanup();
	sessionEphemeralCache.remove('worker');
	vi.restoreAllMocks();
	activeSession.current.sessionId = null;
	activeSession.newChat();
});
afterAll(() => vi.unstubAllGlobals());

function renderComposer() {
	render(ProfileSelector);
	render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
}

async function openEffort() {
	await fireEvent.click(screen.getByRole('button', { name: /^Effort:/ }));
}

describe('desktop composer on a hidden Task worker (#418)', () => {
	it('shows agent, access and Fast loading — not the parent\'s — until /live answers', async () => {
		renderComposer();
		expect(screen.getByRole('button', { name: 'Agent: Overseer' })).toBeTruthy();

		void activeSession.switchSession('worker');
		await waitFor(() => expect(get(activeSession).sessionId).toBe('worker'));

		const agent = await screen.findByRole('button', { name: 'Agent: Loading agent…' });
		expect(agent.getAttribute('aria-busy')).toBe('true');
		expect(agent.classList.contains('unset')).toBe(false);
		const access = screen.getByRole('button', { name: /^Permission mode: loading/ });
		expect(access.getAttribute('aria-busy')).toBe('true');
		expect(screen.queryByRole('button', { name: /^Permission mode: Ask/ })).toBeNull();

		// Fast is never offered as a made-up Off: the worker's model is not
		// known yet either, and when it is, the toggle waits too (see below).
		await openEffort();
		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();

		releaseLive!({ session_meta: WORKER_META });
		await screen.findByRole('button', { name: 'Agent: Investigator' });
		expect(screen.getByRole('button', { name: /^Permission mode: Bypass/ })).toBeTruthy();
		const hydrated = await screen.findByRole('button', { name: 'Fast mode' }) as HTMLButtonElement;
		expect(hydrated.disabled).toBe(false);
		expect(hydrated.getAttribute('aria-pressed')).toBe('true');
	});

	it('a reopened worker whose model is cached but Fast is not waits on the toggle', async () => {
		// Left before its Fast arrived: the cached copy knows the model, agent
		// and access, and records Fast as not known.
		sessionEphemeralCache.save('worker', {
			title: 'Worker', draft: '', scrollTop: 0, project: 'proj-1', projectV2Id: null,
			modelOverride: 'prov:codex:gpt-6.1-sol', effortLevelOverride: 'high',
			profile: 'investigator', permissionModeOverride: 'bypassPermissions', fastMode: false,
			bindingHydrated: { profile: true, permission: true }, bindingPicks: {},
			worktreeIntent: 'local', deviceIntent: null, worktreeId: null, worktreeBranch: '',
			worktreeBaseBranch: 'main', selectedWorktreeBranch: null, worktreeMode: 'new', worktreeDeleted: false,
		});
		renderComposer();
		void activeSession.switchSession('worker');
		await screen.findByRole('button', { name: 'Agent: Investigator' });
		expect(get(activeSession).bindingUnhydrated).toEqual({ fast: true });
		await openEffort();
		const fast = await screen.findByRole('button', { name: 'Fast mode: loading' }) as HTMLButtonElement;
		expect(fast.disabled).toBe(true);
		expect(fast.getAttribute('aria-pressed')).toBe('mixed');
		releaseLive!({ session_meta: WORKER_META });
		const hydrated = await screen.findByRole('button', { name: 'Fast mode' });
		expect(hydrated.getAttribute('aria-pressed')).toBe('true');
	});

	it('a worker whose agent was deleted asks for one instead of loading forever', async () => {
		renderComposer();
		void activeSession.switchSession('worker');
		await screen.findByRole('button', { name: 'Agent: Loading agent…' });
		releaseLive!({ session_meta: { ...WORKER_META, profile_id: null } });
		await waitFor(() => expect(get(activeSession).bindingUnhydrated).toEqual({}));
		// The amber unset chip that asks for an agent, not the loading one.
		const chip = await screen.findByRole('button', { name: 'Agent: Agent' });
		expect(chip.classList.contains('unset')).toBe(true);
		expect(chip.classList.contains('pending')).toBe(false);
		expect(chip.hasAttribute('aria-busy')).toBe(false);
	});

	it('the access menu selects nothing while the worker\'s own access is loading', async () => {
		renderComposer();
		void activeSession.switchSession('worker');
		await screen.findByRole('button', { name: /^Permission mode: loading/ });
		await fireEvent.click(screen.getByRole('button', { name: /^Permission mode: loading/ }));
		const checked = screen.getAllByRole('menuitemradio').filter(item => item.getAttribute('aria-checked') === 'true');
		expect(checked).toHaveLength(0);
		releaseLive!({ session_meta: WORKER_META });
		await waitFor(() => expect(screen.getByRole('menuitemradio', { name: /Bypass/ }).getAttribute('aria-checked')).toBe('true'));
	});
});
