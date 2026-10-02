import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ pushState: vi.fn() }));
vi.mock('$app/navigation', () => ({ pushState: nav.pushState, goto: vi.fn() }));

import { layout } from '$lib/stores/layout.svelte';
import {
	LEGACY_TAB_MAP,
	SettingsNavigation,
	decodeTarget,
	encodeTarget,
	openSettings,
	settingsNav
} from './navigation.svelte';
import { openSettingsTab } from '$lib/api/account';

beforeEach(() => {
	nav.pushState.mockReset();
	settingsNav.reset();
	settingsNav.mounted = 0;
	sessionStorage.clear();
	layout.openChat(null);
});
afterEach(() => vi.restoreAllMocks());

describe('settings targets', () => {
	it('round-trips through encodeTarget/decodeTarget', () => {
		const target = { category: 'providers' as const, item: 'c-sub', tab: 'Policy', anchor: 'providers.loopGuard', open: 'x' };
		expect(decodeTarget(encodeTarget(target))).toEqual(target);
		expect(encodeTarget({ category: 'logs' })).toBe('category=logs');
	});

	it('accepts bare category ids and legacy tab names, and rejects junk', () => {
		expect(decodeTarget('voice')).toEqual({ category: 'voice' });
		expect(decodeTarget('license')).toEqual({ category: 'system', anchor: 'system.license' });
		expect(decodeTarget('not-a-tab')).toBeNull();
		expect(decodeTarget('category=nope')).toBeNull();
		expect(decodeTarget('')).toBeNull();
		expect(decodeTarget(null)).toBeNull();
	});

	it('maps every old tab to its new home', () => {
		expect(LEGACY_TAB_MAP).toEqual({
			account: { category: 'general' },
			chat: { category: 'general' },
			voice: { category: 'voice' },
			providers: { category: 'providers' },
			license: { category: 'system', anchor: 'system.license' },
			api: { category: 'integrations' },
			plugins: { category: 'plugins' },
			system: { category: 'security' },
			logs: { category: 'logs' }
		});
	});
});

describe('openSettings', () => {
	it('opens the Settings page with the target encoded when Settings is not on screen', () => {
		const open = vi.spyOn(layout, 'openFeaturePage');
		openSettings({ category: 'devices', item: 'req-1' });
		expect(open).toHaveBeenCalledExactlyOnceWith('settings', 'category=devices&item=req-1');
	});

	it('moves in place when Settings is mounted and on screen', () => {
		layout.openFeaturePage('settings');
		settingsNav.mounted = 1;
		const open = vi.spyOn(layout, 'openFeaturePage');
		openSettings({ category: 'logs' });
		expect(open).not.toHaveBeenCalled();
		expect(settingsNav.category).toBe('logs');
	});

	// Moved from SettingsCard.account.test.ts: the remote-access refusal in
	// AccountPanel calls openSettingsTab('license').
	it('keeps openSettingsTab working for old tab names and ignores unknown ones', () => {
		const open = vi.spyOn(layout, 'openFeaturePage');
		openSettingsTab('not-a-tab');
		expect(open).not.toHaveBeenCalled();
		openSettingsTab('license');
		expect(open).toHaveBeenCalledExactlyOnceWith('settings', 'category=system&anchor=system.license');
	});
});

describe('settings navigation state', () => {
	it('selects categories, items and tabs', () => {
		settingsNav.selectCategory('providers');
		settingsNav.selectItem('o-key');
		expect(settingsNav.selectedItem('providers')).toBe('o-key');
		expect(settingsNav.tabOf('providers', 'o-key', ['Overview', 'Models'])).toBe('Overview');
		settingsNav.setTab('providers', 'o-key', 'Models');
		expect(settingsNav.tabOf('providers', 'o-key', ['Overview', 'Models'])).toBe('Models');
		// A stored tab the item no longer offers falls back to the first.
		expect(settingsNav.tabOf('providers', 'o-key', ['Overview'])).toBe('Overview');
	});

	it('go() lands on the item, tab, anchor and expansion', () => {
		settingsNav.go({ category: 'security', anchor: 'security.secInstr', open: 'security.secInstr' });
		expect(settingsNav.category).toBe('security');
		expect(settingsNav.pendingAnchor).toBe('security.secInstr');
		expect(settingsNav.consumeOpen('other')).toBe(false);
		expect(settingsNav.consumeOpen('security.secInstr')).toBe(true);
		expect(settingsNav.pendingOpen).toBeNull();

		settingsNav.go({ category: 'providers', item: 'c-sub', tab: 'Policy' });
		expect(settingsNav.selectedItem('providers')).toBe('c-sub');
		expect(settingsNav.tabOf('providers', 'c-sub', ['Overview', 'Policy'])).toBe('Policy');
		expect(settingsNav.pendingItem).toBe('c-sub');
		expect(settingsNav.view).toBe('detail');
	});

	it('remembers the last place for the session', () => {
		settingsNav.selectCategory('logs');
		settingsNav.selectItem('l2');
		settingsNav.setTab('logs', 'l2', 'Raw');
		const restored = new SettingsNavigation();
		expect(restored.category).toBe('logs');
		expect(restored.selectedItem('logs')).toBe('l2');
		expect(restored.tabs['logs:l2']).toBe('Raw');
	});

	it('survives malformed session storage', () => {
		sessionStorage.setItem('settings.nav', '{oops');
		expect(new SettingsNavigation().category).toBe('general');
	});
});

describe('stacked (phone) history', () => {
	beforeEach(() => {
		settingsNav.stacked = true;
		settingsNav.resetStack('index');
	});

	it('pushes one history entry per step deeper', () => {
		settingsNav.selectCategory('devices');
		expect(nav.pushState).toHaveBeenLastCalledWith('', {
			settingsView: 'page',
			settingsDepth: 1,
			settingsCategory: 'devices'
		});
		settingsNav.selectItem('w-1');
		expect(nav.pushState).toHaveBeenLastCalledWith('', {
			settingsView: 'detail',
			settingsDepth: 2,
			settingsCategory: 'devices',
			settingsItem: 'w-1'
		});
		expect(settingsNav.view).toBe('detail');
		expect(settingsNav.depth).toBe(2);
	});

	it('pushes page then detail when a deep link lands on an item from the index', () => {
		settingsNav.go({ category: 'integrations', item: 'github' });
		expect(nav.pushState).toHaveBeenCalledTimes(2);
		expect(nav.pushState.mock.calls.map(([, s]) => s.settingsView)).toEqual(['page', 'detail']);
	});

	it('Back pops through history, and followHistory restores the step', () => {
		const back = vi.spyOn(history, 'back').mockImplementation(() => {});
		settingsNav.selectCategory('devices');
		settingsNav.selectItem('w-1');
		settingsNav.back();
		expect(back).toHaveBeenCalledOnce();
		settingsNav.followHistory({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'devices' });
		expect(settingsNav.view).toBe('page');
		expect(settingsNav.depth).toBe(1);
		settingsNav.followHistory({});
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.depth).toBe(0);
	});

	it('steps back without history when nothing was pushed', () => {
		const back = vi.spyOn(history, 'back');
		settingsNav.view = 'detail';
		settingsNav.back();
		expect(back).not.toHaveBeenCalled();
		expect(settingsNav.view).toBe('page');
	});

	it('does not push on wide layouts', () => {
		settingsNav.stacked = false;
		settingsNav.selectCategory('logs');
		settingsNav.selectItem('l1');
		expect(nav.pushState).not.toHaveBeenCalled();
	});
});
