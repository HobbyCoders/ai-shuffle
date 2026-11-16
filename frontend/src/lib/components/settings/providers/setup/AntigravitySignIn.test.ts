import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { AntigravityLogin } from '$lib/api/antigravity';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post, delete: mocks.delete },
	getErrorMessage: (e: Error, fallback: string) => e?.message ?? fallback }));
import AntigravitySignIn from './AntigravitySignIn.svelte';

const waiting: AntigravityLogin = { login_id: 'l1', provider_id: 'p1', state: 'awaiting_code',
	url: 'https://accounts.google.com/o/oauth2/auth?fake=1', message: null };
let held: AntigravityLogin;

beforeEach(() => {
	vi.clearAllMocks();
	held = { ...waiting };
	mocks.get.mockImplementation(async (path: string) => path === '/antigravity/providers/p1/login/l1' ? held : null);
	mocks.delete.mockResolvedValue({ cancelled: true });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('leaves the waiting state when the sign-in fails in another tab, while unfocused (#1533)', async () => {
	vi.useFakeTimers();
	const onEnded = vi.fn();
	render(AntigravitySignIn, { rowId: 'p1', pending: waiting, onSignedIn: vi.fn(), onEnded });
	expect(screen.getByText('Waiting for the code from Google')).toBeTruthy();
	// No focus or visibility event happens: only the poll can notice.
	await vi.advanceTimersByTimeAsync(3000);
	expect(mocks.get).toHaveBeenCalledWith('/antigravity/providers/p1/login/l1');
	expect(screen.getByText('Waiting for the code from Google')).toBeTruthy();
	// The other tab's wrong code ends it on the worker.
	held = { ...waiting, state: 'ended', url: null };
	await vi.advanceTimersByTimeAsync(3000);
	await waitFor(() => expect(screen.queryByText('Waiting for the code from Google')).toBeNull());
	expect(screen.getByRole('alert').textContent).toMatch(/ended in another tab or expired/);
	expect(screen.getByRole('button', { name: 'Start sign-in' })).toBeTruthy();
	expect(onEnded).toHaveBeenCalledTimes(1);
	// Ended: the poll stops, and the stale pending prop does not bring it back.
	const reads = mocks.get.mock.calls.length;
	await vi.advanceTimersByTimeAsync(9000);
	expect(mocks.get.mock.calls.length).toBe(reads);
	// Only the cheap state read ran, never the status probe.
	expect(mocks.get.mock.calls.every(([path]) => !String(path).endsWith('/status'))).toBe(true);
});

it('keeps waiting while another tab is checking a code', async () => {
	vi.useFakeTimers();
	render(AntigravitySignIn, { rowId: 'p1', pending: waiting, onSignedIn: vi.fn() });
	held = { ...waiting, state: 'completing' };
	await vi.advanceTimersByTimeAsync(6000);
	expect(screen.getByText('Waiting for the code from Google')).toBeTruthy();
	expect(screen.queryByRole('alert')).toBeNull();
});

it('lets its own Finish decide, not a poll that lands meanwhile', async () => {
	vi.useFakeTimers();
	const onSignedIn = vi.fn();
	let finish: (v: AntigravityLogin) => void = () => {};
	mocks.post.mockImplementation(() => new Promise((r) => { finish = r; }));
	render(AntigravitySignIn, { rowId: 'p1', pending: waiting, onSignedIn });
	await fireEvent.input(screen.getByLabelText('Code from Google'), { target: { value: 'good' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Finish sign-in' }));
	held = { ...waiting, state: 'ended', url: null };
	await vi.advanceTimersByTimeAsync(3000);
	finish({ ...waiting, state: 'signed_in', url: null });
	await vi.advanceTimersByTimeAsync(0);
	await waitFor(() => expect(onSignedIn).toHaveBeenCalled());
	expect(screen.queryByRole('alert')).toBeNull();
});
