/**
 * API users (Settings → API): load states, errors shown above a still-usable list, create / edit payloads (including
 * clearing a field), one-time key reveal + copy, rotate, confirm-before-delete,
 * and opening a connector session from the Sessions list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { ApiUser } from '$lib/api/apiUsers';

const apiUsers = vi.hoisted(() => ({
	getApiUsers: vi.fn(), createApiUser: vi.fn(), updateApiUser: vi.fn(), deleteApiUser: vi.fn(), rotateApiUserKey: vi.fn()
}));
vi.mock('$lib/api/apiUsers', () => apiUsers);
const apiGet = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get: apiGet } }));
const copyText = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/clipboard', () => ({ copyText, COPY_ERROR_MESSAGE: 'Could not copy' }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/components/settings/ModelPicker.svelte', () => ({ default: () => {} }));
const sessions = vi.hoisted(() => ({ list: [] as unknown[] }));
const loadApiUserSessions = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { appData: { loadApiUserSessions }, apiUserSessions: readable(sessions.list, (set) => { set(sessions.list); }) };
});
const switchSession = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/active-session', () => ({ activeSession: { switchSession } }));
const openChat = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat } }));

import ApiUserManager from './ApiUserManager.svelte';

const user = (over: Partial<ApiUser> = {}): ApiUser => ({
	id: 'u1', name: 'Discord Bot', username: null, description: 'Server bot', project_id: 'p1',
	profile_ids: ['a1'], is_active: true, model: null, effort: null,
	created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', last_used_at: null, ...over
} as ApiUser);

beforeEach(() => {
	vi.clearAllMocks();
	sessions.list.length = 0;
	apiUsers.getApiUsers.mockResolvedValue([user()]);
	apiUsers.createApiUser.mockResolvedValue({ ...user({ id: 'u2', name: 'Phone' }), api_key: 'sk-new-key' });
	apiUsers.updateApiUser.mockResolvedValue(user());
	apiUsers.deleteApiUser.mockResolvedValue(undefined);
	apiUsers.rotateApiUserKey.mockResolvedValue({ id: 'u1', api_key: 'sk-rotated' });
	apiGet.mockImplementation(async (path: string) => path === '/agents'
		? [{ id: 'a1', name: 'Helper' }, { id: 'a2', name: 'Planner' }]
		: [{ id: 'p1', name: 'Website', path: '/w' }]);
	copyText.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

async function mount() {
	render(ApiUserManager);
	await screen.findByText('Discord Bot');
}

describe('ApiUserManager list', () => {
	it('shows loading, then the empty state', async () => {
		apiUsers.getApiUsers.mockResolvedValue([]);
		render(ApiUserManager);
		expect(screen.getByText('Loading API users...')).toBeTruthy();
		expect(await screen.findByText(/No API users yet/)).toBeTruthy();
	});

	it('shows a load failure', async () => {
		apiUsers.getApiUsers.mockRejectedValue(new Error('offline'));
		render(ApiUserManager);
		expect(await screen.findByText('Failed to load data')).toBeTruthy();
		// The (empty) list and its create action stay rendered under the banner.
		expect(screen.getByRole('button', { name: 'Add API User' })).toBeTruthy();
	});

	it('shows an action error above the list and keeps the list and its actions usable', async () => {
		apiUsers.rotateApiUserKey.mockRejectedValueOnce({ detail: 'Key store locked' });
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate API key for Discord Bot' }));
		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Key store locked');
		// The list is still rendered, after the banner, and its actions still work.
		const row = screen.getByText('Discord Bot');
		expect(alert.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		const rotate = screen.getByRole('button', { name: 'Rotate API key for Discord Bot' }) as HTMLButtonElement;
		expect(rotate.disabled).toBe(false);
		expect((screen.getByRole('button', { name: 'Add API User' }) as HTMLButtonElement).disabled).toBe(false);
		await fireEvent.click(rotate);
		expect(await screen.findByText('sk-rotated')).toBeTruthy();
		expect(apiUsers.rotateApiUserKey).toHaveBeenCalledTimes(2);
		expect(screen.getByRole('alert').textContent).toContain('Key store locked');
		await fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('Key store locked')).toBeNull();
		expect(screen.getByText('Discord Bot')).toBeTruthy();
	});

	it('shows the project name and agent count on a row', async () => {
		await mount();
		expect(screen.getByText('Website')).toBeTruthy();
		expect(screen.getByText('1 agent')).toBeTruthy();
	});
});

describe('ApiUserManager create', () => {
	it('requires a name', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Add API User' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
		expect(await screen.findByText('Name is required')).toBeTruthy();
		expect(apiUsers.createApiUser).not.toHaveBeenCalled();
	});

	it('creates with the chosen project and agents, reveals the key once and copies it', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Add API User' }));
		await fireEvent.input(screen.getByLabelText(/^Name/), { target: { value: '  Phone  ' } });
		await fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'p1' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Planner/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));

		await waitFor(() => expect(apiUsers.createApiUser).toHaveBeenCalledWith({
			name: 'Phone', description: undefined, project_id: 'p1', profile_ids: ['a2'], model: null, effort: null
		}));
		expect(await screen.findByText('sk-new-key')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Create' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Copy Key' }));
		await waitFor(() => expect(copyText).toHaveBeenCalledWith('sk-new-key'));
		expect(await screen.findByRole('button', { name: 'Copied!' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('sk-new-key')).toBeNull();
	});

	it('keeps the form with the worker error when create fails', async () => {
		apiUsers.createApiUser.mockRejectedValue({ detail: 'Name already in use' });
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Add API User' }));
		await fireEvent.input(screen.getByLabelText(/^Name/), { target: { value: 'Discord Bot' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
		expect(await screen.findByText('Name already in use')).toBeTruthy();
		expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe('Discord Bot');
	});
});

describe('ApiUserManager edit / rotate / delete', () => {
	it('prefills the form and sends a cleared description and project as null', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Edit key Discord Bot' }));
		expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe('Discord Bot');
		expect((screen.getByLabelText('Project') as HTMLSelectElement).value).toBe('p1');
		expect((screen.getByRole('checkbox', { name: /Helper/ }) as HTMLInputElement).checked).toBe(true);

		await fireEvent.input(screen.getByLabelText('Description'), { target: { value: '' } });
		await fireEvent.change(screen.getByLabelText('Project'), { target: { value: '' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Helper/ }));
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Active' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Update' }));

		await waitFor(() => expect(apiUsers.updateApiUser).toHaveBeenCalledWith('u1', {
			name: 'Discord Bot', description: null, project_id: null, profile_ids: [], is_active: false, model: null, effort: null
		}));
		await waitFor(() => expect(screen.queryByRole('button', { name: 'Update' })).toBeNull());
	});

	it('rotates a key and reveals the new one; a failure shows an error', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate API key for Discord Bot' }));
		expect(await screen.findByText('sk-rotated')).toBeTruthy();
		expect(apiUsers.rotateApiUserKey).toHaveBeenCalledWith('u1');

		apiUsers.rotateApiUserKey.mockRejectedValue({ detail: 'Key store locked' });
		await fireEvent.click(screen.getByRole('button', { name: 'Rotate API key for Discord Bot' }));
		expect(await screen.findByText('Key store locked')).toBeTruthy();
	});

	it('deletes only after Confirm', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete key Discord Bot' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(apiUsers.deleteApiUser).not.toHaveBeenCalled();

		apiUsers.getApiUsers.mockResolvedValue([]);
		await fireEvent.click(screen.getByRole('button', { name: 'Delete key Discord Bot' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
		await waitFor(() => expect(apiUsers.deleteApiUser).toHaveBeenCalledWith('u1'));
		expect(await screen.findByText(/No API users yet/)).toBeTruthy();
	});
});

describe('ApiUserManager sessions', () => {
	it('lists the user\'s sessions newest first and opens one in the chat', async () => {
		sessions.list.push(
			{ id: 'sess-old-1234', title: 'Older', api_user_id: 'u1', project_id: 'p1', updated_at: '2026-01-01T00:00:00Z', status: 'idle' },
			{ id: 'sess-new-5678', title: '', api_user_id: 'u1', project_id: 'p1', updated_at: '2026-02-01T00:00:00Z', worktree_branch: 'bot/fix', worktree_id: 'w1' },
			{ id: 'other', title: 'Not mine', api_user_id: 'u9', updated_at: '2026-03-01T00:00:00Z' }
		);
		await mount();
		const toggle = screen.getByRole('button', { name: /Sessions/ });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		const rows = screen.getAllByRole('button').filter((b) => b.classList.contains('session-row'));
		expect(rows.map((r) => within(r).getAllByText(/./)[0].textContent?.trim())).toEqual(['sess-new', 'Older']);

		await fireEvent.click(rows[0]);
		expect(switchSession).toHaveBeenCalledWith('sess-new-5678', 'p1', {
			worktreeId: 'w1', worktreeBranch: 'bot/fix', worktreeDeleted: false, worktreeIntent: 'worktree'
		}, null, null);
		expect(openChat).toHaveBeenCalledWith('sess-new-5678');

		await fireEvent.click(screen.getByRole('button', { name: 'Refresh sessions' }));
		expect(loadApiUserSessions).toHaveBeenCalledOnce();
	});
});
