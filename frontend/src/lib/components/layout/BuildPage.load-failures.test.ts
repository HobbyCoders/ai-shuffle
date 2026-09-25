/**
 * BuildPage warms the sub-nav badge counts on mount. The stores rethrow a failed
 * load, so the page must catch it: a backend failure leaves the badges hidden
 * and never becomes an unhandled rejection.
 */
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import BuildPreferencePanel from '$lib/test-stubs/BuildPreferencePanel.svelte';
const mocks = vi.hoisted(() => ({
	loadGlobalServers: vi.fn(() => Promise.reject(new Error('mcp offline'))),
	loadAppHooks: vi.fn(() => Promise.reject(new Error('hooks offline')))
}));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { register: vi.fn(), save: vi.fn() } }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadProfiles: vi.fn() }, profiles: writable([]) }));
vi.mock('$lib/stores/mcp.svelte', () => ({ mcp: { loadGlobalServers: mocks.loadGlobalServers }, globalMcpServers: writable([]) }));
vi.mock('$lib/stores/plugins.svelte', () => ({ plugins: { loadInstalled: vi.fn() }, installedPlugins: writable([]) }));
vi.mock('$lib/stores/hooks.svelte', () => ({ hooks: { loadAppHooks: mocks.loadAppHooks }, projectHooks: writable([]) }));
vi.mock('$lib/components/deck/lazy/lazy-component', () => ({ createLazyComponent: () => ({ preload: vi.fn() }) }));
vi.mock('$lib/components/deck/lazy/LazyPanel.svelte', () => ({ default: BuildPreferencePanel }));
import BuildPage from './BuildPage.svelte';

afterEach(cleanup);

it('catches failed badge-count loads and renders the nav without counts', async () => {
	const unhandled = vi.fn();
	process.on('unhandledRejection', unhandled);
	try {
		localStorage.clear();
		const page = render(BuildPage);
		expect(mocks.loadGlobalServers).toHaveBeenCalled();
		expect(mocks.loadAppHooks).toHaveBeenCalled();
		await new Promise((r) => setTimeout(r, 0));
		expect(unhandled).not.toHaveBeenCalled();
		expect(page.getByRole('button', { name: 'MCP Servers' }).textContent?.trim()).toBe('MCP Servers');
		expect(page.getByRole('button', { name: 'Hooks' }).textContent?.trim()).toBe('Hooks');
	} finally {
		process.off('unhandledRejection', unhandled);
	}
});
