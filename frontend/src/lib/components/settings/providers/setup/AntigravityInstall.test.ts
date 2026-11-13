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
	antigravity.status = null; antigravity.meta = null; antigravity.error = null;
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
