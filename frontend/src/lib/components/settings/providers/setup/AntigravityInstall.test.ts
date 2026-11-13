import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { AntigravityInstallJob, AntigravityInstallStatus } from '$lib/api/antigravity';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, post: mocks.post },
	getErrorMessage: (e: Error, fallback: string) => e?.message ?? fallback }));
import AntigravityInstall from './AntigravityInstall.svelte';
import { antigravity } from '../antigravity.svelte';

const idle: AntigravityInstallJob = { kind: 'install', state: 'idle', version: null, error: null, message: null };
const installed: AntigravityInstallStatus = { installed: true, version: '1.5.6', source: 'app', can_install: true,
	install: idle, workers_requiring_update: [] };
const meta = { auth_modes: [], warning: '', terms_url: '', dashboard_url: '', guard_default: true, default_models: [] };
let current: AntigravityInstallStatus;

beforeEach(() => {
	vi.clearAllMocks();
	(antigravity as unknown as { cancelPoll(): void }).cancelPoll();
	antigravity.status = null; antigravity.meta = null; antigravity.error = null; antigravity.retry = null;
	antigravity.starting = false; antigravity.loading = false;
	current = { ...installed };
	mocks.get.mockImplementation(async (path: string) => path.endsWith('/status') ? current : meta);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('shows the installed version with Check for update, and says when it is up to date', async () => {
	render(AntigravityInstall);
	expect(await screen.findByText('1.5.6')).toBeTruthy();
	const check = screen.getByRole('button', { name: 'Check for update' });
	const done: AntigravityInstallJob = { kind: 'update', state: 'succeeded', version: '1.5.6', error: null,
		message: 'Antigravity is up to date (1.5.6).' };
	mocks.post.mockImplementation(async () => { current = { ...current, install: done }; return { ...done, state: 'running' }; });
	await fireEvent.click(check);
	expect(mocks.post).toHaveBeenCalledWith('/antigravity/update');
	expect(await screen.findByText('Checking Google for a newer Antigravity')).toBeTruthy();
	expect((screen.getByRole('button', { name: 'Checking' }) as HTMLButtonElement).disabled).toBe(true);
	expect(await screen.findByText('Antigravity is up to date (1.5.6).', {}, { timeout: 4000 })).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Check for update' })).toBeTruthy();
});

it('says what the update did, and shows a failed check as an alert', async () => {
	current = { ...installed, version: '1.5.7', source: 'path',
		install: { kind: 'update', state: 'succeeded', version: '1.5.7', error: null,
			message: "Antigravity updated from 1.5.6 to 1.5.7. AI Shuffle's own copy now runs instead of the agy on PATH." } };
	render(AntigravityInstall);
	expect(await screen.findByText(/updated from 1\.5\.6 to 1\.5\.7/)).toBeTruthy();
	expect(screen.getByText("Google's agent, found on this worker's PATH")).toBeTruthy();
	cleanup();
	antigravity.status = null;
	current = { ...installed, install: { kind: 'update', state: 'failed', version: null,
		error: 'Update check failed: network down', message: 'Update check failed: network down' } };
	render(AntigravityInstall);
	expect((await screen.findByRole('alert')).textContent).toBe('Update check failed: network down');
	expect(screen.getByRole('button', { name: 'Check for update' })).toBeTruthy();
});

it('names the platform when Google publishes no build for this worker', async () => {
	const message = 'Google publishes no Antigravity build for this worker (linux riscv64).';
	current = { ...installed, installed: false, version: null, source: null, can_install: false,
		install: { kind: 'install', state: 'failed', version: null, error: message, message } };
	render(AntigravityInstall);
	expect((await screen.findByRole('alert')).textContent).toBe(message);
	await waitFor(() => expect((screen.getByRole('button', { name: 'Install Antigravity' }) as HTMLButtonElement).disabled).toBe(true));
});

it('says why up front on a worker Google publishes no build for', async () => {
	const message = 'Google publishes no Antigravity build for this worker (linux riscv64).';
	current = { ...installed, installed: false, version: null, source: null, can_install: false,
		unsupported_message: message, install: { kind: 'install', state: 'idle', version: null, error: null, message: null } };
	render(AntigravityInstall);
	expect(await screen.findByText((text) => text.startsWith(message))).toBeTruthy();
	expect((screen.getByRole('button', { name: 'Install Antigravity' }) as HTMLButtonElement).disabled).toBe(true);
});

it('says it once when an install failed on a worker Google publishes no build for', async () => {
	const message = 'Google publishes no Antigravity build for this worker (linux riscv64).';
	current = { ...installed, installed: false, version: null, source: null, can_install: false,
		unsupported_message: message,
		install: { kind: 'install', state: 'failed', version: null, error: message, message } };
	render(AntigravityInstall);
	expect(await screen.findAllByText((text) => text.startsWith(message))).toHaveLength(1);
	expect(screen.queryByRole('alert')).toBeNull();
	const install = screen.getByRole('button', { name: 'Install Antigravity' });
	expect(install.getAttribute('aria-describedby')).toBe('antigravity-unsupported');
});

it('re-reads the status on open, so a job run elsewhere is not shown stale', async () => {
	render(AntigravityInstall);
	expect(await screen.findByText('1.5.6')).toBeTruthy();
	cleanup();
	current = { ...installed, version: '1.5.8', install: { kind: 'update', state: 'running', version: null, error: null, message: null } };
	render(AntigravityInstall);
	expect(await screen.findByText('Checking Google for a newer Antigravity')).toBeTruthy();
	expect((screen.getByRole('button', { name: 'Checking' }) as HTMLButtonElement).disabled).toBe(true);
	current = { ...current, install: { kind: 'update', state: 'succeeded', version: '1.5.8', error: null,
		message: 'Antigravity updated from 1.5.6 to 1.5.8.' } };
	expect(await screen.findByText('Antigravity updated from 1.5.6 to 1.5.8.', {}, { timeout: 4000 })).toBeTruthy();
	expect(screen.getByText('1.5.8')).toBeTruthy();
});

it('sends one request for a double click, and Try again repeats a failed check', async () => {
	render(AntigravityInstall);
	const check = await screen.findByRole('button', { name: 'Check for update' });
	let fail = true;
	mocks.post.mockImplementation(async () => {
		await new Promise((r) => setTimeout(r, 20));
		if (fail) throw new Error('Could not reach the worker');
		return { kind: 'update', state: 'running', version: null, error: null, message: null };
	});
	await fireEvent.click(check);
	await fireEvent.click(check);
	expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the worker');
	expect(mocks.post).toHaveBeenCalledTimes(1);
	fail = false;
	await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(2));
	expect(mocks.post).toHaveBeenLastCalledWith('/antigravity/update');
	await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
});

function deferred<T>() {
	let resolve!: (v: T) => void, reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

it('keeps a failed check\'s error when an older status read lands after it', async () => {
	antigravity.meta = meta as never; antigravity.status = { ...installed };
	const slow = deferred<AntigravityInstallStatus>();
	mocks.get.mockImplementation((path: string) => path.endsWith('/status') ? slow.promise : Promise.resolve(meta));
	mocks.post.mockRejectedValue(new Error('Could not reach the worker'));
	render(AntigravityInstall); // its forced re-read stays pending
	await fireEvent.click(await screen.findByRole('button', { name: 'Check for update' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the worker');
	slow.resolve({ ...installed });
	await new Promise((r) => setTimeout(r, 0));
	expect(screen.getByRole('alert').textContent).toBe('Could not reach the worker');
	expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
});

it('runs one polling chain when a poll in flight is overtaken by a refresh and a new check', async () => {
	vi.useFakeTimers();
	antigravity.meta = meta as never; antigravity.status = { ...installed };
	const running: AntigravityInstallJob = { kind: 'update', state: 'running', version: null, error: null, message: null };
	const done = { ...installed, install: { ...running, state: 'succeeded', message: 'Antigravity is up to date (1.5.6).' } };
	mocks.post.mockResolvedValue(running);
	const inFlight = deferred<AntigravityInstallStatus>();
	mocks.get.mockImplementationOnce(() => inFlight.promise);
	await antigravity.install('update');
	await vi.advanceTimersByTimeAsync(2000); // the poll's read is now in flight
	mocks.get.mockImplementation(async (path: string) => path.endsWith('/status') ? done : meta);
	await antigravity.load(true); // sees the job finished: Check is enabled again
	await antigravity.install('update'); // timer A
	inFlight.resolve({ ...installed, install: running }); // the old poll lands
	await vi.advanceTimersByTimeAsync(0);
	mocks.get.mockClear();
	mocks.get.mockImplementation(async (path: string) => path.endsWith('/status') ? { ...installed, install: running } : meta);
	await vi.advanceTimersByTimeAsync(2100);
	expect(mocks.get).toHaveBeenCalledTimes(1); // only timer A polled
});
