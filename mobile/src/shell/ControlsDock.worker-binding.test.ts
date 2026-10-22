/**
 * #418 on the phone: the controls sheet of a hidden Task worker opened from
 * its parent shows the worker's own agent, tool access and Fast — loading
 * while its `/live` metadata is in flight, never the parent's agent, Ask or
 * Fast off. Drives the real store through the switch and the hydration.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import ControlsDock from '$lib/components/chat/ControlsDock.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { api } from '$lib/api/client';
import { installMobileShell } from './register';
import '$lib/test-stubs/composer-dom';

installMobileShell();
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn((url: unknown) => String(url).includes('/events?')
		? new Promise(() => {})
		: Promise.resolve({ ok: false, status: 404, json: async () => ({}) })));
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
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
	sessionEphemeralCache.remove('worker');
	vi.restoreAllMocks();
	activeSession.current.sessionId = null;
	activeSession.newChat();
});
afterAll(() => vi.unstubAllGlobals());

async function openControls() {
	const view = render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
	view.component.setMobileSheetOpen(true);
	await tick();
	return view;
}

const panel = () => document.querySelector<HTMLElement>('.mcp')!;
const panelAccess = () => within(panel().querySelector<HTMLElement>('[role="radiogroup"][aria-label="Tool access"]')!);
const fastSwitch = () => within(panel()).queryByRole('switch', { name: /^Fast mode/ }) as HTMLButtonElement | null;
const agentChip = () => within(panel()).getByRole('button', { name: /^Agent:/ });

describe('phone controls on a hidden Task worker (#418)', () => {
	it('shows the worker\'s agent, access and Fast — loading first, never the parent\'s', async () => {
		// Reopened after leaving mid-load: its model is cached, the rest is not.
		sessionEphemeralCache.save('worker', {
			title: 'Worker', draft: '', scrollTop: 0, project: 'proj-1', projectV2Id: null,
			modelOverride: 'prov:codex:gpt-6.1-sol', effortLevelOverride: 'high',
			profile: '', permissionModeOverride: null, fastMode: false,
			bindingHydrated: {}, bindingPicks: {},
			worktreeIntent: 'local', deviceIntent: null, worktreeId: null, worktreeBranch: '',
			worktreeBaseBranch: 'main', selectedWorktreeBranch: null, worktreeMode: 'new', worktreeDeleted: false,
		});
		await openControls();
		expect(agentChip().getAttribute('aria-label')).toBe('Agent: Overseer');

		void activeSession.switchSession('worker');
		await waitFor(() => expect(get(activeSession).sessionId).toBe('worker'));
		await tick();

		// Loading, not the parent's Overseer or the amber "pick one".
		await waitFor(() => expect(agentChip().getAttribute('aria-label')).toBe('Agent: Loading agent…'));
		expect(agentChip().getAttribute('aria-busy')).toBe('true');
		// Quick bar and panel: no access is claimed, Ask included.
		expect(screen.getByRole('button', { name: /^Tool access: loading/ })).toBeTruthy();
		expect(panelAccess().getAllByRole('radio').filter(r => r.getAttribute('aria-checked') === 'true')).toHaveLength(0);
		// Fast is neither On nor Off and cannot be flipped from a guess.
		expect(fastSwitch()!.getAttribute('aria-checked')).toBe('mixed');
		expect(fastSwitch()!.disabled).toBe(true);
		expect(fastSwitch()!.getAttribute('aria-label')).toBe('Fast mode: loading');

		releaseLive!({ session_meta: WORKER_META });
		await waitFor(() => expect(agentChip().getAttribute('aria-label')).toBe('Agent: Investigator'));
		expect(screen.getByRole('button', { name: /^Tool access: Bypass/ })).toBeTruthy();
		expect(panelAccess().getByRole('radio', { name: 'Bypass' }).getAttribute('aria-checked')).toBe('true');
		expect(fastSwitch()!.getAttribute('aria-checked')).toBe('true');
		expect(fastSwitch()!.disabled).toBe(false);
	});
});
