/**
 * #1610: plugin management lives in Settings. A "Manage plugins" deep link
 * (view type `plugins`) resolves to the Settings panel and opens its Plugins
 * section, which hosts the plugin hub; Build no longer routes it.
 */
import { cleanup, render, screen, within } from '@testing-library/svelte';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(async () => []), put: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/api/auth', () => ({
	getWorkspaceConfig: vi.fn(async () => ({ workspace_path: '/workspace' })),
	validateWorkspacePath: vi.fn(),
	setWorkspaceConfig: vi.fn()
}));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { auth: { checkAuth: vi.fn() }, isAdmin: writable(true) };
});
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { refreshAll: vi.fn(), refreshProviders: vi.fn() }
}));
vi.mock('$lib/stores/chatPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { chatPreferences: { ...writable(null), load: vi.fn() } };
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(), setTimezone: vi.fn() } };
});
// Other sections are outside this test and must not initialize their stores.
vi.mock('./settings/GeneralSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/OwnerAccessSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/providers/ProvidersSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/DevicesSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/ChatBehaviorSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/HotkeysSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/VoiceSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/ApiTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/SystemTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/LogsTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/LicenseSection.svelte', () => ({ default: () => {} }));
// Stand-in for the lazily loaded plugin hub: it shows which deep-linked item
// reached it.
vi.mock('$lib/components/deck/lazy/LazyPanel.svelte', async () => ({
	default: (await import('$lib/test-stubs/SettingsLazyPanel.svelte')).default
}));

import SettingsCard from './SettingsCard.svelte';
import { featurePanelKeyForView } from '$lib/components/deck/lazy/panels';

// jsdom has no Web Animations; the section switch fades.
const proto = Element.prototype as unknown as { animate?: unknown };
const hadAnimate = 'animate' in proto;
beforeAll(() => {
	if (!hadAnimate) proto.animate = () => ({ cancel() {}, finish() {}, onfinish: null, finished: Promise.resolve() });
});
afterAll(() => {
	if (!hadAnimate) delete proto.animate;
});
afterEach(() => cleanup());

describe('Settings → Plugins', () => {
	it('lists Plugins in the Settings rail', () => {
		render(SettingsCard);
		const nav = screen.getByRole('navigation', { name: 'Settings sections' });
		expect(within(nav).getByRole('button', { name: 'Plugins' })).toBeTruthy();
		expect(screen.queryByRole('region', { name: 'Plugins panel' })).toBeNull();
	});

	it('opens the Plugins section from a deep link and passes the item through', async () => {
		render(SettingsCard, { requestedTab: 'plugins', requestedItemId: 'connection:gmail' });
		const panel = await screen.findByRole('region', { name: 'Plugins panel' });
		expect(panel.getAttribute('data-initial-selected-id')).toBe('connection:gmail');
		const nav = screen.getByRole('navigation', { name: 'Settings sections' });
		expect(within(nav).getByRole('button', { name: 'Plugins' }).classList.contains('on')).toBe(true);
	});

	it('routes the old plugins view type to Settings, not Build', () => {
		expect(featurePanelKeyForView('plugins')).toBe('settings');
		expect(featurePanelKeyForView('settings')).toBe('settings');
		expect(featurePanelKeyForView('mcp')).toBe('build');
	});
});
