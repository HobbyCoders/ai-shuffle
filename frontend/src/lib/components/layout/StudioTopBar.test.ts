import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';

vi.mock('$lib/stores/auth.svelte', () => ({
	username: writable<string | null>('Local owner'),
	isAdmin: writable(true),
}));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: {} }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: writable({}) }));
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(), patch: vi.fn(), delete: vi.fn() },
	forkSession: vi.fn(),
}));

import StudioTopBar from './StudioTopBar.svelte';
import { isAdmin, username } from '$lib/stores/auth.svelte';
import { api } from '$lib/api/client';
import { ACCOUNT_CHANGED_EVENT, disconnectAccount, setAccountUsername } from '$lib/api/account';

const connection = (signed_in = true, profile_ready = true) => ({ signed_in, profile_ready });
const profile = (name = 'shuffle.person') => ({ account: { username: name } });
function mockAccount(name = 'shuffle.person', signedIn = true, ready = true) {
	vi.mocked(api.get).mockImplementation(async (path) =>
		(path === '/mobile/hub/connection' ? connection(signedIn, ready) : profile(name)) as never);
}
async function expectBadge(name: string, letters: string) {
	await waitFor(() => expect(screen.getByTitle(name).textContent).toBe(letters));
}

beforeEach(() => {
	vi.clearAllMocks();
	(username as unknown as ReturnType<typeof writable<string | null>>).set('Local owner');
	(isAdmin as unknown as ReturnType<typeof writable<boolean>>).set(true);
	mockAccount();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it.each([
	['shuffle.person', 'SP'], ['quickkill0', 'QU'], ['  jane-doe  ', 'JD'],
	['jane_doe', 'JD'], ['jane doe', 'JD'], ['x', 'X'],
])('uses the Shuffle name %s for both initials and hover text', async (name, letters) => {
	mockAccount(name);
	render(StudioTopBar);
	await expectBadge(name.trim(), letters);
	expect(api.get).toHaveBeenCalledWith('/mobile/hub/connection', { cache: 'no-store' });
	expect(api.get).toHaveBeenCalledWith('/mobile/hub/account/display', { cache: 'no-store' });
});

it.each([[false, true], [true, false]])('keeps the local name when signed_in=%s and profile_ready=%s', async (signedIn, ready) => {
	mockAccount('shuffle.person', signedIn, ready);
	render(StudioTopBar);
	await expectBadge('Local owner', 'LO');
	expect(api.get).not.toHaveBeenCalledWith('/mobile/hub/account/display', expect.anything());
});

it('falls back for an empty profile', async () => {
	mockAccount('   ');
	render(StudioTopBar);
	await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
	await expectBadge('Local owner', 'LO');
});

it('clears a previous account name when refreshing fails', async () => {
	render(StudioTopBar);
	await expectBadge('shuffle.person', 'SP');
	vi.mocked(api.get).mockRejectedValue(new Error('Offline'));
	window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
	await expectBadge('Local owner', 'LO');
});

it('does not request owner-only metadata for a paired guest', async () => {
	(isAdmin as unknown as ReturnType<typeof writable<boolean>>).set(false);
	(username as unknown as ReturnType<typeof writable<string | null>>).set('Guest tablet');
	render(StudioTopBar);
	await expectBadge('Guest tablet', 'GT');
	expect(api.get).not.toHaveBeenCalled();
});

it('keeps the placeholder when no account name is available', async () => {
	(isAdmin as unknown as ReturnType<typeof writable<boolean>>).set(false);
	(username as unknown as ReturnType<typeof writable<string | null>>).set(null);
	render(StudioTopBar);
	await expectBadge('Account', '·');
});

it('refreshes after saving the account username without remounting', async () => {
	render(StudioTopBar);
	await expectBadge('shuffle.person', 'SP');
	mockAccount('new.name');
	vi.mocked(api.patch).mockResolvedValue(profile('new.name'));
	await setAccountUsername('new.name');
	await expectBadge('new.name', 'NN');
});

it('clears the account on sign-out and ignores an older profile response', async () => {
	render(StudioTopBar);
	await expectBadge('shuffle.person', 'SP');
	let resolve!: (value: unknown) => void;
	const pending = new Promise((done) => { resolve = done; });
	vi.mocked(api.get).mockResolvedValueOnce(connection()).mockReturnValueOnce(pending as never);
	window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
	await waitFor(() => expect(api.get).toHaveBeenCalledTimes(4));
	mockAccount('', false);
	vi.mocked(api.delete).mockResolvedValue(connection(false));
	await disconnectAccount();
	await expectBadge('Local owner', 'LO');
	resolve(profile('old.account'));
	await tick();
	await expectBadge('Local owner', 'LO');
});

it('detects an account linked after the bar mounted and stops polling on unmount', async () => {
	vi.useFakeTimers();
	mockAccount('', false);
	const view = render(StudioTopBar);
	await vi.advanceTimersByTimeAsync(0);
	mockAccount('new.account');
	await vi.advanceTimersByTimeAsync(30_000);
	await tick();
	expect(screen.getByTitle('new.account').textContent).toBe('NA');
	view.unmount();
	vi.mocked(api.get).mockClear();
	window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
	await vi.advanceTimersByTimeAsync(60_000);
	expect(api.get).not.toHaveBeenCalled();
});

it('coalesces focus bursts and pauses polling in hidden tabs', async () => {
	vi.useFakeTimers();
	let hidden = true;
	vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
	render(StudioTopBar);
	await vi.advanceTimersByTimeAsync(60_000);
	expect(api.get).not.toHaveBeenCalled();
	hidden = false;
	document.dispatchEvent(new Event('visibilitychange'));
	await vi.advanceTimersByTimeAsync(0);
	for (let i = 0; i < 10; i++) window.dispatchEvent(new Event('focus'));
	await vi.advanceTimersByTimeAsync(0);
	expect(api.get).toHaveBeenCalledTimes(2);
	hidden = true;
	document.dispatchEvent(new Event('visibilitychange'));
	await vi.advanceTimersByTimeAsync(120_000);
	expect(api.get).toHaveBeenCalledTimes(2);
	hidden = false;
	document.dispatchEvent(new Event('visibilitychange'));
	await vi.advanceTimersByTimeAsync(0);
	expect(api.get).toHaveBeenCalledTimes(4);
});

it('backs off failures despite focus and resets after a successful refresh', async () => {
	vi.useFakeTimers();
	vi.mocked(api.get).mockRejectedValue(new Error('429'));
	render(StudioTopBar);
	await vi.advanceTimersByTimeAsync(0);
	expect(api.get).toHaveBeenCalledTimes(1);
	for (let i = 0; i < 10; i++) window.dispatchEvent(new Event('focus'));
	await vi.advanceTimersByTimeAsync(59_999);
	expect(api.get).toHaveBeenCalledTimes(1);
	await vi.advanceTimersByTimeAsync(1);
	expect(api.get).toHaveBeenCalledTimes(2);
	mockAccount();
	await vi.advanceTimersByTimeAsync(120_000);
	expect(api.get).toHaveBeenCalledTimes(4);
	await vi.advanceTimersByTimeAsync(30_000);
	expect(api.get).toHaveBeenCalledTimes(6);
});

it('drops the account name when owner access is lost', async () => {
	render(StudioTopBar);
	await expectBadge('shuffle.person', 'SP');
	(isAdmin as unknown as ReturnType<typeof writable<boolean>>).set(false);
	await expectBadge('Local owner', 'LO');
});
