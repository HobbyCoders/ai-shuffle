import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ post: vi.fn(), checkAuth: vi.fn(), authenticated: false }));
vi.mock('$lib/api/client', () => ({
	api: { post: mocks.post },
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e.detail || e.message || fallback
}));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: mocks.checkAuth, get current() { return { status: { authenticated: mocks.authenticated } }; } }
}));
import OwnerSignIn from './OwnerSignIn.svelte';

const popup = { opener: {} as unknown, close: vi.fn(), location: { replace: vi.fn() } };
const requestId = 'r'.repeat(43);
const completion = 'c'.repeat(43);
const start = () => ({ request_id: requestId, authorization_url: 'https://accounts.example.test/browser/authorize?request=test', expires_at: new Date(Date.now() + 600000).toISOString() });

beforeEach(() => {
	mocks.post.mockReset(); mocks.checkAuth.mockReset(); mocks.authenticated = false;
	popup.close.mockReset(); popup.location.replace.mockReset(); popup.opener = {};
	vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('browser owner sign-in', () => {
	it('accepts completion only from its own account tab and checks the resulting worker session', async () => {
		mocks.post.mockImplementation(async (path, body) => path.endsWith('/start') ? start() : { status: body.completion_secret === completion ? 'authenticated' : 'pending' });
		mocks.checkAuth.mockImplementation(async () => { mocks.authenticated = true; });
		const done = vi.fn();
		const page = render(OwnerSignIn, { onAuthenticated: done });
		await fireEvent.click(page.getByRole('button', { name: 'Sign in to AI Shuffle' }));
		await waitFor(() => expect(popup.location.replace).toHaveBeenCalled());
		const emit = (origin: string, source: unknown, id = requestId) => {
			const event = new MessageEvent('message', { origin, data: { type: 'shuffle.browser.complete', request_id: id, completion_secret: completion } });
			Object.defineProperty(event, 'source', { value: source });
			window.dispatchEvent(event);
		};
		emit('https://attacker.example', popup);
		emit('https://accounts.example.test', window);
		emit('https://accounts.example.test', popup, 'wrong-request');
		await waitFor(() => expect(mocks.post.mock.calls.length).toBeGreaterThanOrEqual(3), { timeout: 4000 });
		expect(mocks.post.mock.calls.filter(call => call[0].endsWith('/poll')).every(call => !call[1].completion_secret)).toBe(true);
		expect(done).not.toHaveBeenCalled();
		emit('https://accounts.example.test', popup);
		await waitFor(() => expect(done).toHaveBeenCalledOnce(), { timeout: 4000 });
		expect(mocks.post.mock.calls.at(-1)?.[1]).toEqual({ completion_secret: completion });
		expect(popup.close).toHaveBeenCalledOnce();
	});

	it('shows a denied owner grant without navigating into the worker', async () => {
		mocks.post.mockResolvedValueOnce(start()).mockRejectedValueOnce({ detail: 'This account does not own the worker.' });
		const done = vi.fn();
		const page = render(OwnerSignIn, { onAuthenticated: done });
		await fireEvent.click(page.getByRole('button', { name: 'Sign in to AI Shuffle' }));
		await waitFor(() => expect(page.getByRole('alert').textContent).toContain('does not own'));
		expect(done).not.toHaveBeenCalled();
	});

	it('supports the completion code when the browser blocks the popup handoff', async () => {
		vi.mocked(window.open).mockReturnValue(null);
		mocks.post.mockImplementation(async (path, body) => path.endsWith('/start') ? start() : { status: body.completion_secret === completion ? 'authenticated' : 'pending' });
		mocks.checkAuth.mockImplementation(async () => { mocks.authenticated = true; });
		const done = vi.fn();
		const page = render(OwnerSignIn, { onAuthenticated: done });
		await fireEvent.click(page.getByRole('button', { name: 'Sign in to AI Shuffle' }));
		await waitFor(() => expect(page.getByRole('link', { name: 'Open sign-in' })).toBeTruthy());
		await fireEvent.click(page.getByRole('button', { name: 'Use a completion code' }));
		await fireEvent.input(page.getByLabelText('Code from the account tab'), { target: { value: completion } });
		await fireEvent.click(page.getByRole('button', { name: 'Complete sign-in' }));
		await waitFor(() => expect(done).toHaveBeenCalledOnce(), { timeout: 4000 });
		expect(mocks.post.mock.calls.at(-1)?.[1]).toEqual({ completion_secret: completion });
	});

	it('rejects unsafe authorization URLs before navigating the account tab', async () => {
		mocks.post.mockResolvedValueOnce({ ...start(), authorization_url: 'javascript:alert(1)' });
		const page = render(OwnerSignIn, { onAuthenticated: vi.fn() });
		await fireEvent.click(page.getByRole('button', { name: 'Sign in to AI Shuffle' }));
		await waitFor(() => expect(page.getByRole('alert').textContent).toContain('Invalid sign-in address'));
		expect(popup.location.replace).not.toHaveBeenCalled();
		expect(mocks.post).toHaveBeenCalledTimes(1);
	});

	it('ignores a late start response after the user cancels', async () => {
		let resolve!: (result: ReturnType<typeof start>) => void;
		mocks.post.mockReturnValueOnce(new Promise(r => { resolve = r; }));
		const done = vi.fn();
		const page = render(OwnerSignIn, { onAuthenticated: done });
		await fireEvent.click(page.getByRole('button', { name: 'Sign in to AI Shuffle' }));
		await fireEvent.click(page.getByRole('button', { name: 'Cancel' }));
		resolve(start());
		await waitFor(() => expect(page.getByRole('button', { name: 'Sign in to AI Shuffle' })).toBeTruthy());
		expect(mocks.post).toHaveBeenCalledTimes(1);
		expect(popup.location.replace).not.toHaveBeenCalled();
		expect(done).not.toHaveBeenCalled();
	});
});
