import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import LogsTab from './LogsTab.svelte';
import { api } from '$lib/api/client';
import { activeView } from '$lib/stores/layout.svelte';

vi.mock('$lib/api/client', () => ({ api: { get: vi.fn() }, fetchAndDownload: vi.fn() }));
vi.mock('svelte/transition', () => ({ fade: () => ({ duration: 0 }) }));
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { activeView: writable({ type: 'settings' }) };
});
const view = activeView as unknown as ReturnType<typeof writable<{ type: string }>>;

beforeEach(() => {
	vi.useFakeTimers();
	vi.mocked(api.get).mockReset().mockResolvedValue({ lines: [], total: 0, returned: 0, levels: [] });
	view.set({ type: 'settings' });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('stops the retained Logs panel polling when another feature is active', async () => {
	render(LogsTab);
	await tick();
	expect(api.get).toHaveBeenCalledOnce();
	view.set({ type: 'session' });
	await tick();
	await vi.advanceTimersByTimeAsync(30_000);
	expect(api.get).toHaveBeenCalledOnce();
	view.set({ type: 'settings' });
	await tick();
	expect(api.get).toHaveBeenCalledTimes(2);
});

it('names the log level filter for assistive tech', async () => {
	render(LogsTab);
	await tick();
	expect(screen.getByRole('combobox', { name: 'Log level' })).toBeTruthy();
});
