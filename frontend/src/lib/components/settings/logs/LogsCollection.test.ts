/**
 * Logs: polling only while Settings and the tab are visible (ported from
 * LogsTab.visibility.test.ts), the named level filter, debounced search,
 * refresh, export, the live count, newest-first order, the detail facts,
 * "Same logger", stable ids and coverage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';

vi.mock('$lib/api/client', () => ({ api: { get: vi.fn() }, fetchAndDownload: vi.fn() }));
vi.mock('$lib/stores/layout.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { activeView: writable({ type: 'settings' }), layout: { openFeaturePage: vi.fn() } };
});
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { isAdmin: writable(true) };
});

import { api, fetchAndDownload } from '$lib/api/client';
import { activeView } from '$lib/stores/layout.svelte';
import LogsCollection from './LogsCollection.svelte';
import { LogsModel, withIds, levelTone, sortLevels } from './logs-model.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';

const view = activeView as unknown as ReturnType<typeof writable<{ type: string }>>;
const lines = [
	{ timestamp: '2026-10-01T14:40:01.120Z', level: 'INFO', logger_name: 'app.api.sessions', message: 'POST /turns 202' },
	{ timestamp: '2026-10-01T14:41:02.230Z', level: 'ERROR', logger_name: 'app.providers.grok', message: 'Token refresh failed: 401' },
	{ timestamp: '2026-10-01T14:42:03.340Z', level: 'INFO', logger_name: 'app.api.sessions', message: 'GET /sessions 200' }
];
let model: LogsModel;

beforeEach(() => {
	vi.useFakeTimers();
	settingsNav.reset();
	vi.mocked(api.get).mockReset().mockResolvedValue({ lines, total: 12408, returned: 3, levels: ['INFO', 'ERROR', 'AUDIT'] });
	vi.mocked(fetchAndDownload).mockReset().mockResolvedValue(undefined as never);
	view.set({ type: 'settings' });
	model = new LogsModel();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function mount(item?: string) {
	settingsNav.go({ category: 'logs', ...(item ? { item } : {}) });
	const result = render(LogsCollection, { props: { model } });
	await tick();
	await vi.advanceTimersByTimeAsync(0);
	return result;
}

describe('polling', () => {
	it('stops while another feature is active and refreshes on return', async () => {
		await mount();
		expect(api.get).toHaveBeenCalledOnce();
		await vi.advanceTimersByTimeAsync(10_000);
		expect(api.get).toHaveBeenCalledTimes(2);
		view.set({ type: 'session' });
		await tick();
		await vi.advanceTimersByTimeAsync(30_000);
		expect(api.get).toHaveBeenCalledTimes(2);
		view.set({ type: 'settings' });
		await tick();
		expect(api.get).toHaveBeenCalledTimes(3);
	});

	it('pauses while the browser tab is hidden', async () => {
		await mount();
		Object.defineProperty(document, 'hidden', { configurable: true, value: true });
		document.dispatchEvent(new Event('visibilitychange'));
		await tick();
		await vi.advanceTimersByTimeAsync(30_000);
		expect(api.get).toHaveBeenCalledOnce();
		Object.defineProperty(document, 'hidden', { configurable: true, value: false });
		document.dispatchEvent(new Event('visibilitychange'));
		await tick();
		expect(api.get).toHaveBeenCalledTimes(2);
	});

	it('stops when the collection unmounts', async () => {
		const { unmount } = await mount();
		unmount();
		await vi.advanceTimersByTimeAsync(30_000);
		expect(api.get).toHaveBeenCalledOnce();
	});
});

describe('toolbar', () => {
	it('names the level filter and refetches with the level', async () => {
		await mount();
		const select = screen.getByRole('combobox', { name: 'Log level' }) as HTMLSelectElement;
		expect(Array.from(select.options).map((o) => o.text)).toEqual(['All levels', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'AUDIT']);
		await fireEvent.change(select, { target: { value: 'ERROR' } });
		await tick();
		expect(vi.mocked(api.get).mock.lastCall?.[0]).toBe('/logs?limit=500&level=ERROR');
	});

	it('debounces the search 300ms', async () => {
		await mount();
		const box = screen.getByPlaceholderText('Search logs');
		await fireEvent.input(box, { target: { value: 'tok' } });
		await fireEvent.input(box, { target: { value: 'token' } });
		await vi.advanceTimersByTimeAsync(299);
		expect(api.get).toHaveBeenCalledOnce();
		await vi.advanceTimersByTimeAsync(1);
		await tick();
		expect(vi.mocked(api.get).mock.lastCall?.[0]).toBe('/logs?limit=500&search=token');
	});

	it('refreshes, exports and shows the live count', async () => {
		await mount();
		expect(screen.getByText('3 of 12,408')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh logs' }));
		expect(api.get).toHaveBeenCalledTimes(2);
		await fireEvent.click(screen.getByRole('button', { name: 'Export' }));
		expect(fetchAndDownload).toHaveBeenCalledWith('/logs/export', 'ai-shuffle-logs.log');
	});
});

describe('entries', () => {
	it('lists newest first and shows Time, Level and Logger', async () => {
		await mount();
		const items = Array.from(document.querySelectorAll('[data-settings-item]')) as HTMLElement[];
		expect(items.map((i) => i.textContent)).toEqual([
			expect.stringContaining('GET /sessions 200'),
			expect.stringContaining('Token refresh failed'),
			expect.stringContaining('POST /turns 202')
		]);
		await fireEvent.click(items[1]);
		const facts = document.querySelector('.st-facts') as HTMLElement;
		expect(Array.from(facts.querySelectorAll('dt')).map((d) => d.textContent)).toEqual(['Time', 'Level', 'Logger']);
		expect(within(facts).getByText('ERROR')).toBeTruthy();
		expect(within(facts).getByText('app.providers.grok')).toBeTruthy();
		expect(screen.getByRole('heading', { level: 2, name: 'Error' })).toBeTruthy();
	});

	it('narrows to the same logger and clears it', async () => {
		await mount();
		const items = () => Array.from(document.querySelectorAll('[data-settings-item]')) as HTMLElement[];
		await fireEvent.click(items()[0]);
		await fireEvent.click(screen.getByRole('button', { name: /Same logger/ }));
		expect(items()).toHaveLength(2);
		await fireEvent.click(screen.getByRole('button', { name: 'Show every logger' }));
		expect(items()).toHaveLength(3);
	});

	it('keeps a selected line after it scrolls out of the window', async () => {
		await mount();
		const first = Array.from(document.querySelectorAll('[data-settings-item]'))[2] as HTMLElement;
		await fireEvent.click(first);
		vi.mocked(api.get).mockResolvedValue({ lines: lines.slice(1), total: 12409, returned: 2, levels: [] });
		await vi.advanceTimersByTimeAsync(10_000);
		expect(screen.getAllByText('POST /turns 202').length).toBeGreaterThan(0);
		expect(screen.getByRole('heading', { level: 2, name: 'Info' })).toBeTruthy();
	});
});

describe('helpers', () => {
	it('gives identical lines distinct, stable ids', () => {
		const twice = [lines[0], lines[0]];
		const a = withIds(twice).map((e) => e.id);
		expect(new Set(a).size).toBe(2);
		expect(withIds(twice).map((e) => e.id)).toEqual(a);
	});

	it('maps levels to tones and orders unknown levels last', () => {
		expect(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map(levelTone)).toEqual(['off', 'ok', 'warn', 'err', 'err']);
		expect(sortLevels(['AUDIT', 'INFO'])).toEqual(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'AUDIT']);
	});

	it('counts errors for the index badge', async () => {
		await model.fetch();
		expect(model.errorCount).toBe(1);
	});
});

it('renders every Logs registry entry', async () => {
	vi.useRealTimers();
	await assertCategoryCoverage('logs', () => {
		settingsNav.go({ category: 'logs' });
		return render(LogsCollection, { props: { model: new LogsModel() } }).container;
	});
});
