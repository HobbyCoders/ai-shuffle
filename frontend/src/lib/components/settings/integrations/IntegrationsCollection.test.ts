/**
 * Integrations collection, ported from ApiUserManager(.a11y).test.ts and
 * extended for the new detail: draft + Create, one-time key, auto-saved
 * fields, Rotate and Delete via InlineConfirm, Sessions, GitHub, coverage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { ApiUser } from '$lib/api/apiUsers';

const apiUsers = vi.hoisted(() => ({
	getApiUsers: vi.fn(),
	createApiUser: vi.fn(),
	updateApiUser: vi.fn(),
	deleteApiUser: vi.fn(),
	rotateApiUserKey: vi.fn()
}));
vi.mock('$lib/api/apiUsers', () => apiUsers);
const apiGet = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({
	api: { get: apiGet },
	getErrorMessage: (e: { detail?: string; message?: string }, f: string) => e?.detail || e?.message || f
}));
const gh = vi.hoisted(() => ({ status: vi.fn(), web: vi.fn(), token: vi.fn(), cancel: vi.fn(), logout: vi.fn() }));
vi.mock('$lib/api/auth', () => ({
	getGithubAuthStatus: gh.status,
	startGithubWebLogin: gh.web,
	loginGithubWithToken: gh.token,
	cancelGithubWebLogin: gh.cancel,
	logoutGithub: gh.logout
}));
const copyText = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/clipboard', () => ({ copyText, COPY_ERROR_MESSAGE: 'Could not copy' }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/components/settings/ModelPicker.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
const sessions = vi.hoisted(() => ({ list: [] as unknown[] }));
const loadApiUserSessions = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { readable } = await import('svelte/store');
	return {
		appData: { loadApiUserSessions },
		apiUserSessions: readable(sessions.list, (set) => {
			set(sessions.list);
		})
	};
});
const switchSession = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/active-session', () => ({ activeSession: { switchSession } }));
const openChat = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/layout.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { layout: { openChat, openFeaturePage: vi.fn() }, activeView: readable({ type: 'settings' }) };
});
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true), auth: { checkAuth: vi.fn() } };
});

import IntegrationsCollection from './IntegrationsCollection.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { searchSource } from '$lib/settings/entries/integrations';

const user = (over: Partial<ApiUser> = {}): ApiUser => ({
	id: 'u1',
	name: 'Discord Bot',
	username: null,
	description: 'Server bot',
	project_id: 'p1',
	profile_ids: ['a1'],
	is_active: true,
	model: null,
	effort: null,
	created_at: '2026-01-01T00:00:00Z',
	updated_at: '2026-01-01T00:00:00Z',
	last_used_at: null,
	...over
});

beforeEach(() => {
	vi.clearAllMocks();
	settingsNav.reset();
	sessions.list.length = 0;
	apiUsers.getApiUsers.mockResolvedValue([
		user(),
		user({ id: 'u3', name: 'Home Assistant', is_active: false, profile_ids: null, description: null })
	]);
	apiUsers.createApiUser.mockResolvedValue({ ...user({ id: 'u2', name: 'Phone' }), api_key: 'sk-new-key' });
	apiUsers.updateApiUser.mockImplementation(async (id: string, body: Partial<ApiUser>) => ({
		...user({ id }),
		...body
	}));
	apiUsers.deleteApiUser.mockResolvedValue(undefined);
	apiUsers.rotateApiUserKey.mockResolvedValue({ id: 'u1', api_key: 'sk-rotated' });
	apiGet.mockImplementation(async (path: string) =>
		path === '/agents'
			? [
					{ id: 'a1', name: 'Helper' },
					{ id: 'a2', name: 'Planner' }
				]
			: [{ id: 'p1', name: 'Website', path: '/w' }]
	);
	gh.status.mockResolvedValue({ authenticated: true, user: 'octocat', config_dir: '/home/app/.config/gh' });
	gh.logout.mockResolvedValue({ success: true });
	copyText.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

async function open(item?: string, tab?: string) {
	settingsNav.go({ category: 'integrations', ...(item ? { item } : {}), ...(tab ? { tab } : {}) });
	const view = render(IntegrationsCollection);
	await screen.findByRole('button', { name: /Discord Bot/ });
	return view;
}

describe('list', () => {
	it('lists GitHub and every API user, named for assistive tech', async () => {
		await open();
		expect(screen.getByRole('button', { name: /GitHub/ }).textContent).toContain('@octocat');
		expect(screen.getByRole('button', { name: /Discord Bot/ }).textContent).toContain('Agent default, 1 agent');
		expect(screen.getByRole('button', { name: /Home Assistant/ }).textContent).toContain('Disabled');
		expect(screen.getByText('3')).toBeTruthy();
		expect(searchSource().map((s) => s.title)).toContain('Discord Bot');
	});

	it('shows the empty state and keeps Add available after a load failure', async () => {
		apiUsers.getApiUsers.mockRejectedValue(new Error('offline'));
		settingsNav.go({ category: 'integrations' });
		render(IntegrationsCollection);
		expect(await screen.findByText('Could not load API users')).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Add API user' }) as HTMLButtonElement).disabled).toBe(false);
	});
});

describe('removed agent restrictions', () => {
	it('explains an obsolete restriction and requires a replacement before clearing it', async () => {
		apiUsers.getApiUsers.mockResolvedValue([user({ profile_ids: ['Explore'] })]);
		await open('u1', 'Access');
		expect(await screen.findByText(/Agent Explore was removed/)).toBeTruthy();
		const remove = screen.getByRole('button', { name: 'Remove Explore restriction' }) as HTMLButtonElement;
		expect(remove.disabled).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox', { name: /Planner/ }));
		await waitFor(() => expect(apiUsers.updateApiUser).toHaveBeenCalledWith('u1', { profile_ids: ['Explore', 'a2'] }));
		await waitFor(() => expect(remove.disabled).toBe(false));
		await fireEvent.click(remove);
		await waitFor(() => expect(apiUsers.updateApiUser).toHaveBeenCalledWith('u1', { profile_ids: ['a2'] }));
	});

	it('counts only agents that still exist, in the list and on the Access tab', async () => {
		apiUsers.getApiUsers.mockResolvedValue([user({ profile_ids: ['a1', 'Explore'] })]);
		await open('u1', 'Access');
		expect(screen.getByRole('button', { name: /Discord Bot/ }).textContent).toContain('Agent default, 1 agent');
		expect(await screen.findByText(/Agent Explore was removed/)).toBeTruthy();
		expect(screen.getByText('1 of 2')).toBeTruthy();
		expect(screen.queryByText('2 of 2')).toBeNull();
	});

	it('tells an unrestricted key apart from one restricted only to removed agents', async () => {
		apiUsers.getApiUsers.mockResolvedValue([
			user({ profile_ids: [] }),
			user({ id: 'u2', name: 'Old Bot', profile_ids: ['Explore'] })
		]);
		await open();
		expect(screen.getByRole('button', { name: /Discord Bot/ }).textContent).toContain('Agent default, all agents');
		expect(screen.getByRole('button', { name: /Old Bot/ }).textContent).toContain('Agent default, 0 agents');
	});

	it('says All agents on the Access tab for an unrestricted key, not 0 of N', async () => {
		apiUsers.getApiUsers.mockResolvedValue([user({ profile_ids: [] })]);
		await open('u1', 'Access');
		expect(await screen.findByText('All agents')).toBeTruthy();
		expect(screen.queryByText('0 of 2')).toBeNull();
	});

	it('keeps the k of N count on the Access tab for a restricted key', async () => {
		apiUsers.getApiUsers.mockResolvedValue([user({ profile_ids: ['a1'] })]);
		await open('u1', 'Access');
		expect(await screen.findByText('1 of 2')).toBeTruthy();
		expect(screen.queryByText('All agents')).toBeNull();
	});
});

describe('create', () => {
	it('adds one draft, requires a name, creates and shows the key once', async () => {
		await open();
		await fireEvent.click(screen.getByRole('button', { name: 'Add API user' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Add API user' }));
		expect(screen.getAllByRole('button', { name: /New API user/ })).toHaveLength(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Create API user' }));
		expect(await screen.findByText('Name is required')).toBeTruthy();
		expect(apiUsers.createApiUser).not.toHaveBeenCalled();

		await fireEvent.input(screen.getByRole('textbox', { name: 'Name' }), { target: { value: '  Phone  ' } });
		await fireEvent.change(screen.getByRole('combobox', { name: 'Project' }), { target: { value: 'p1' } });
		await fireEvent.click(screen.getByRole('tab', { name: 'Access' }));
		await fireEvent.click(screen.getByRole('checkbox', { name: /Planner/ }));
		apiUsers.getApiUsers.mockResolvedValue([user(), user({ id: 'u2', name: 'Phone' })]);
		await fireEvent.click(screen.getByRole('button', { name: 'Create API user' }));

		await waitFor(() =>
			expect(apiUsers.createApiUser).toHaveBeenCalledWith({
				name: 'Phone',
				description: undefined,
				project_id: 'p1',
				profile_ids: ['a2'],
				model: null,
				effort: null
			})
		);
		expect(await screen.findByText('sk-new-key')).toBeTruthy();
		expect(settingsNav.selectedItem('integrations')).toBe('u2');
		await fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));
		await waitFor(() => expect(copyText).toHaveBeenCalledWith('sk-new-key'));
		expect(await screen.findByRole('button', { name: 'Copied' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('sk-new-key')).toBeNull();
	});

	it('Cancel drops the draft', async () => {
		await open();
		await fireEvent.click(screen.getByRole('button', { name: 'Add API user' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(screen.queryByRole('button', { name: /New API user/ })).toBeNull();
		expect(settingsNav.selectedItem('integrations')).toBeNull();
	});
});

describe('one API user', () => {
	it('auto-saves a cleared description as null', async () => {
		await open('u1');
		const desc = screen.getByRole('textbox', { name: 'Description' }) as HTMLInputElement;
		expect(desc.value).toBe('Server bot');
		await fireEvent.focus(desc);
		await fireEvent.input(desc, { target: { value: '' } });
		await fireEvent.blur(desc);
		await waitFor(() => expect(apiUsers.updateApiUser).toHaveBeenCalledWith('u1', { description: null }));
		await fireEvent.click(screen.getByRole('switch', { name: 'Active' }));
		await waitFor(() => expect(apiUsers.updateApiUser).toHaveBeenLastCalledWith('u1', { is_active: false }));
	});

	it('says permissions always ask', async () => {
		await open('u1', 'Run');
		expect(screen.getByText('Ask, always')).toBeTruthy();
	});

	it('rotates the key only after "Replace the key now?"', async () => {
		await open('u1');
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate key' }));
		expect(apiUsers.rotateApiUserKey).not.toHaveBeenCalled();
		expect(screen.getByText('Replace the key now?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate key' }));
		expect(await screen.findByText('sk-rotated')).toBeTruthy();
	});

	it('shows a rotate failure and keeps the detail usable', async () => {
		apiUsers.rotateApiUserKey.mockRejectedValueOnce({ detail: 'Key store locked' });
		await open('u1');
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate key' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate key' }));
		expect((await screen.findByRole('alert')).textContent).toContain('Key store locked');
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('Key store locked')).toBeNull();
		expect((screen.getByRole('button', { name: 'Rotate key' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('deletes only after the confirm', async () => {
		await open('u1');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(apiUsers.deleteApiUser).not.toHaveBeenCalled();
		apiUsers.getApiUsers.mockResolvedValue([]);
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		expect(screen.getByText('Delete Discord Bot?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(apiUsers.deleteApiUser).toHaveBeenCalledWith('u1'));
		await waitFor(() => expect(settingsNav.selectedItem('integrations')).toBeNull());
		expect(screen.queryByRole('button', { name: /Discord Bot/ })).toBeNull();
	});

	it("opens one of the user's sessions in the chat and refreshes the list", async () => {
		sessions.list.push(
			{
				id: 'sess-old-1234',
				title: 'Older',
				api_user_id: 'u1',
				project_id: 'p1',
				profile_id: 'a1',
				updated_at: '2026-01-01T00:00:00Z',
				status: 'idle'
			},
			{
				id: 'sess-new-5678',
				title: '',
				api_user_id: 'u1',
				project_id: 'p1',
				updated_at: '2026-02-01T00:00:00Z',
				worktree_branch: 'bot/fix',
				worktree_id: 'w1'
			},
			{ id: 'other', title: 'Not mine', api_user_id: 'u9', updated_at: '2026-03-01T00:00:00Z' }
		);
		await open('u1', 'Sessions');
		expect(screen.getByRole('tab', { name: /Sessions/ }).textContent).toContain('2');
		const rows = screen.getAllByRole('button').filter((b) => b.classList.contains('ses'));
		expect(rows.map((r) => r.querySelector('.ses-name')?.textContent)).toEqual(['sess-new', 'Older']);
		expect(within(rows[1]).getByText('Helper, Website')).toBeTruthy();
		await fireEvent.click(rows[0]);
		expect(switchSession).toHaveBeenCalledWith(
			'sess-new-5678',
			'p1',
			{
				worktreeId: 'w1',
				worktreeBranch: 'bot/fix',
				worktreeDeleted: false,
				worktreeIntent: 'worktree'
			},
			null,
			null
		);
		expect(openChat).toHaveBeenCalledWith('sess-new-5678');
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh sessions' }));
		expect(loadApiUserSessions).toHaveBeenCalledOnce();
	});
});

describe('GitHub', () => {
	it('disconnects only after "Sign gh out?"', async () => {
		await open('github');
		expect(screen.getByText('/home/app/.config/gh')).toBeTruthy();
		gh.status.mockResolvedValue({ authenticated: false, user: null, config_dir: '' });
		await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
		expect(gh.logout).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
		await waitFor(() => expect(gh.logout).toHaveBeenCalledOnce());
		expect(await screen.findByRole('button', { name: 'Sign in with GitHub' })).toBeTruthy();
	});

	it('connects with a token and only shows connected after reading it back', async () => {
		gh.status.mockResolvedValue({ authenticated: false, user: null, config_dir: '' });
		gh.token.mockResolvedValue({ success: true });
		await open('github');
		await fireEvent.click(await screen.findByRole('button', { name: 'Access token' }));
		await fireEvent.input(screen.getByPlaceholderText('ghp_… or github_pat_…'), { target: { value: ' tok ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
		expect((await screen.findByRole('alert')).textContent).toBe('GitHub sign-in could not be confirmed. Please retry.');
		expect(gh.token).toHaveBeenCalledWith('tok');
	});
});

it('renders every Integrations registry entry where it lives', async () => {
	gh.status.mockResolvedValue({ authenticated: false, user: null, config_dir: '' });
	await assertCategoryCoverage('integrations', async ({ item, tab }) => (await open(item, tab)).container);
	// Detail rows are reached through searchSource(), not registry anchors.
	await open('github');
	await screen.findByRole('button', { name: 'Sign in with GitHub' });
	expect(
		Array.from(document.querySelectorAll('[data-setting-anchor]')).map((a) => a.getAttribute('data-setting-anchor'))
	).toEqual(['integrations.apiUsers']);
	expect(searchSource().find((s) => s.title === 'Connect GitHub')?.target).toEqual({
		category: 'integrations',
		item: 'github'
	});
});
