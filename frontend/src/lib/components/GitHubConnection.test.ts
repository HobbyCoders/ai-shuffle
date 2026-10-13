import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';

const mocks = vi.hoisted(() => ({ status: vi.fn(), web: vi.fn(), token: vi.fn(), cancel: vi.fn(), logout: vi.fn(), check: vi.fn() }));
vi.mock('$lib/api/auth', () => ({ getGithubAuthStatus: mocks.status, startGithubWebLogin: mocks.web,
    loginGithubWithToken: mocks.token, cancelGithubWebLogin: mocks.cancel, logoutGithub: mocks.logout }));
vi.mock('$lib/api/client', () => ({ getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e.detail || e.message || fallback }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { checkAuth: mocks.check } }));
import GitHubConnection from './GitHubConnection.svelte';

beforeEach(() => {
    vi.resetAllMocks();
    mocks.status.mockResolvedValue({ authenticated: false, user: null });
    mocks.cancel.mockResolvedValue({ success: true });
    mocks.check.mockResolvedValue(undefined);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
async function open() {
    const view = render(GitHubConnection);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in with GitHub' }).hasAttribute('disabled')).toBe(false));
    return view;
}
async function enterToken() {
    await open();
    await fireEvent.click(screen.getByRole('button', { name: 'Personal access token' }));
    const field = screen.getByLabelText('GitHub personal access token');
    await fireEvent.input(field, { target: { value: '  public-token  ' } });
    return field;
}

describe('GitHub connection', () => {
    it('offers browser sign-in and a masked personal token form', async () => {
        const field = await enterToken();
        expect(field.getAttribute('type')).toBe('password');
        expect(screen.getByRole('button', { name: 'Web sign-in' })).toBeTruthy();
        expect(screen.getByRole('link', { name: 'Create a token' }).getAttribute('href')).toBe('https://github.com/settings/tokens');
    });

    it('displays success:false errors and retains the token for correction', async () => {
        mocks.token.mockResolvedValue({ success: false, error: 'GitHub rejected this token: expired' });
        const field = await enterToken();
        await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'GitHub rejected this token: expired');
        expect(mocks.token).toHaveBeenCalledWith('public-token');
        expect((field as HTMLInputElement).value).toBe('  public-token  ');
        expect(mocks.check).not.toHaveBeenCalled();
    });

    it('only shows connected after reading authenticated status back', async () => {
        mocks.token.mockResolvedValue({ success: true });
        await enterToken();
        mocks.status.mockResolvedValue({ authenticated: false, user: null });
        await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'GitHub sign-in could not be confirmed. Please retry.');
        expect(screen.queryByRole('button', { name: 'Disconnect GitHub' })).toBeNull();
        mocks.status.mockResolvedValue({ authenticated: true, user: 'octocat' });
        await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
        expect(await screen.findByText('GitHub is connected as @octocat.')).toBeTruthy();
        expect(mocks.check).toHaveBeenCalled();
    });

    it('treats authenticated without a username as connected', async () => {
        mocks.status.mockResolvedValue({ authenticated: true, user: null });
        render(GitHubConnection);
        expect(await screen.findByText('GitHub is connected.')).toBeTruthy();
    });

    it('shows transport errors and re-enables the token action', async () => {
        mocks.token.mockRejectedValue({ detail: 'Network unavailable' });
        await enterToken();
        await fireEvent.click(screen.getByRole('button', { name: 'Connect token' }));
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Network unavailable');
        expect(screen.getByRole('button', { name: 'Connect token' }).hasAttribute('disabled')).toBe(false);
    });

    it('shows the real device code, approval link, and stops polling on success', async () => {
        await open();
        vi.useFakeTimers();
        mocks.web.mockResolvedValue({ success: true, user_code: 'ABCD-1234' });
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        await tick();
        expect(screen.getByText('ABCD-1234')).toBeTruthy();
        expect(screen.getByRole('link', { name: /Open GitHub/ }).getAttribute('href')).toBe('https://github.com/login/device');
        mocks.status.mockResolvedValue({ authenticated: true, user: 'octocat' });
        await vi.advanceTimersByTimeAsync(3000); await tick();
        expect(screen.getByText('GitHub is connected as @octocat.')).toBeTruthy();
        const calls = mocks.status.mock.calls.length;
        await vi.advanceTimersByTimeAsync(6000);
        expect(mocks.status).toHaveBeenCalledTimes(calls);
    });

    it('surfaces a failed web start without starting a poll', async () => {
        mocks.web.mockResolvedValue({ success: false, error: 'GitHub CLI is not installed' });
        await open();
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'GitHub CLI is not installed');
        expect(mocks.status).toHaveBeenCalledTimes(1);
    });

    it('cancels an outstanding sign-in before enabling token login', async () => {
        await open();
        vi.useFakeTimers();
        mocks.web.mockResolvedValue({ success: true, user_code: 'ABCD-1234' });
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        expect(screen.getByRole('button', { name: 'Personal access token' }).hasAttribute('disabled')).toBe(true);
        await fireEvent.click(screen.getByRole('button', { name: 'Cancel sign-in' }));
        expect(mocks.cancel).toHaveBeenCalledOnce();
        await vi.advanceTimersByTimeAsync(6000);
        expect(mocks.status).toHaveBeenCalledTimes(1);
        expect(screen.getByRole('button', { name: 'Personal access token' }).hasAttribute('disabled')).toBe(false);
    });

    it('stops polling on unmount and on backend expiry', async () => {
        const view = await open();
        vi.useFakeTimers();
        mocks.web.mockResolvedValue({ success: true, user_code: 'ABCD-1234' });
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        mocks.status.mockResolvedValue({ authenticated: false, web_login_error: 'Sign-in expired' });
        await vi.advanceTimersByTimeAsync(3000); await tick();
        expect(screen.getByRole('alert').textContent).toBe('Sign-in expired');
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        view.unmount();
        const calls = mocks.status.mock.calls.length;
        await vi.advanceTimersByTimeAsync(6000);
        expect(mocks.status).toHaveBeenCalledTimes(calls);
    });

    it('reports its state to a parent summary and drops its own heading when bare', async () => {
        const onstate = vi.fn();
        render(GitHubConnection, { props: { bare: true, onstate } });
        expect(onstate).toHaveBeenCalledWith(expect.objectContaining({ loading: true, authenticated: false }));
        await waitFor(() => expect(onstate).toHaveBeenLastCalledWith({ loading: false, authenticated: false, user: null, pending: false, error: '' }));
        expect(screen.queryByRole('heading', { name: 'GitHub' })).toBeNull();
        mocks.web.mockResolvedValue({ success: true, user_code: 'ABCD-1234' });
        await fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
        await waitFor(() => expect(onstate).toHaveBeenLastCalledWith(expect.objectContaining({ pending: true })));
    });
});
