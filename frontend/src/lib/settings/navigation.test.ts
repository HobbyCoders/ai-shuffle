import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ pushState: vi.fn(), replaceState: vi.fn() }));
vi.mock('$app/navigation', () => ({ pushState: nav.pushState, replaceState: nav.replaceState, goto: vi.fn() }));

import { layout } from '$lib/stores/layout.svelte';
import {
	LEGACY_TAB_MAP,
	SettingsNavigation,
	decodeTarget,
	encodeTarget,
	openSettings,
	redirectTarget,
	settingsNav
} from './navigation.svelte';
import { openSettingsTab } from '$lib/api/account';

beforeEach(() => {
	nav.pushState.mockReset();
	nav.replaceState.mockReset();
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
		expect(decodeTarget('license')).toEqual({ category: 'cloud' });
		expect(decodeTarget('cloud')).toEqual({ category: 'cloud' });
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
			license: { category: 'cloud' },
			cloud: { category: 'cloud' },
			api: { category: 'integrations' },
			plugins: { category: 'plugins' },
			system: { category: 'security' },
			logs: { category: 'logs' }
		});
	});

	it('sends old License anchors to the Cloud page', () => {
		expect(decodeTarget('category=system&anchor=system.license')).toEqual({ category: 'cloud', anchor: 'cloud.account' });
		expect(decodeTarget('category=system&anchor=system.licenseKey&open=system.licenseKey')).toEqual({
			category: 'cloud',
			anchor: 'cloud.account',
			open: 'cloud.account'
		});
		// The Plan key and Deactivate rows are gone: the plan follows the signed-in account.
		expect(redirectTarget({ category: 'cloud', anchor: 'cloud.key' })).toEqual({ category: 'cloud', anchor: 'cloud.account' });
		expect(redirectTarget({ category: 'system', anchor: 'system.seats' })).toEqual({ category: 'cloud', anchor: 'cloud.remoteWorkers' });
		expect(redirectTarget({ category: 'system', anchor: 'system.checkUpdates' })).toEqual({ category: 'system', anchor: 'system.checkUpdates' });
	});

	it('moves an in-app go() to an old anchor onto Cloud', () => {
		const nav = new SettingsNavigation(false);
		nav.go({ category: 'system', anchor: 'system.deactivate' });
		expect(nav.category).toBe('cloud');
		expect(nav.pendingAnchor).toBe('cloud.account');
	});
});

describe('openSettings', () => {
	it('opens the Settings page with the target encoded when Settings is not on screen', () => {
		const open = vi.spyOn(layout, 'openFeaturePage');
		openSettings({ category: 'devices', item: 'req-1' });
		expect(open).toHaveBeenCalledExactlyOnceWith('settings', 'category=devices&item=req-1');
	});

	it('leaves a retained off-screen shell target for the shell to apply after outgoing history retires', () => {
		settingsNav.mounted = 1;
		settingsNav.stacked = true;
		settingsNav.resetStack();
		layout.openFeaturePage('automations');
		openSettings({ category: 'providers', item: 'provider:claude', anchor: 'providers.claudeMode' });
		expect(layout.current.activeView).toEqual({
			type: 'settings',
			sessionId: null,
			itemId: 'category=providers&item=provider%3Aclaude&anchor=providers.claudeMode'
		});
		expect(nav.pushState).not.toHaveBeenCalled();
		expect(settingsNav.view).toBe('index');
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
		expect(open).toHaveBeenCalledExactlyOnceWith('settings', 'category=cloud');
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

describe('keeping the reader\'s place in one pane (#1043)', () => {
	it('keeps a chosen page when the shell becomes one pane, with Back to the index', () => {
		settingsNav.selectCategory('security');
		settingsNav.stacked = true;
		settingsNav.startStack();
		expect(settingsNav.view).toBe('page');
		expect(settingsNav.category).toBe('security');
		expect(settingsNav.depth).toBe(1);
		settingsNav.followHistory({});
		expect(settingsNav.view).toBe('index');
	});

	it('builds both Back steps for a chosen detail', () => {
		settingsNav.selectItem('thing', 'providers');
		settingsNav.stacked = true;
		settingsNav.startStack();
		expect(settingsNav.view).toBe('detail');
		expect(settingsNav.depth).toBe(2);
	});

	it('starts at the index when no page was chosen', () => {
		settingsNav.stacked = true;
		settingsNav.startStack();
		expect(settingsNav.view).toBe('index');
		expect(settingsNav.depth).toBe(0);
	});

	it('restores a chosen page after a reload without a second Back step', () => {
		settingsNav.selectCategory('security');
		const reloaded = new SettingsNavigation();
		reloaded.stacked = true;
		reloaded.startStack({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'security' });
		expect(reloaded.view).toBe('page');
		expect(reloaded.category).toBe('security');
		expect(reloaded.depth).toBe(1);
	});

	describe('a resize never brings back an old page', () => {
		const security = { settingsView: 'page', settingsDepth: 1, settingsCategory: 'security' } as const;

		it('keeps the page chosen while wide, on the entry already there, with Back to the index', () => {
			settingsNav.selectCategory('cloud');
			settingsNav.stacked = true;
			settingsNav.startStack(security);
			expect(settingsNav.view).toBe('page');
			expect(settingsNav.category).toBe('cloud');
			expect(settingsNav.depth).toBe(1);
			expect(nav.pushState).not.toHaveBeenCalled();
			expect(nav.replaceState).toHaveBeenCalledWith('', expect.objectContaining({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'cloud' }));
			settingsNav.followHistory({});
			expect(settingsNav.view).toBe('index');
		});

		it('keeps the entry when the reader is still on its page', () => {
			settingsNav.selectCategory('security');
			settingsNav.stacked = true;
			settingsNav.startStack(security);
			expect(settingsNav.category).toBe('security');
			expect(settingsNav.depth).toBe(1);
			expect(nav.replaceState).not.toHaveBeenCalled();
			expect(nav.pushState).not.toHaveBeenCalled();
		});

		it('takes over a reload entry the wide layout has since moved on from', () => {
			settingsNav.selectCategory('cloud');
			const reloaded = new SettingsNavigation();
			reloaded.stacked = true;
			reloaded.startStack(security);
			expect(reloaded.category).toBe('cloud');
			expect(reloaded.view).toBe('page');
		});

		it('builds a page step under a detail chosen while wide', () => {
			settingsNav.selectItem('anthropic', 'providers');
			settingsNav.stacked = true;
			settingsNav.startStack(security);
			expect(settingsNav.view).toBe('detail');
			expect(nav.replaceState).toHaveBeenCalledWith('', expect.objectContaining({ settingsView: 'page', settingsCategory: 'providers', settingsDepth: 1 }));
			expect(nav.pushState).toHaveBeenCalledWith('', expect.objectContaining({ settingsView: 'detail', settingsItem: 'anthropic', settingsDepth: 2 }));
			settingsNav.followHistory({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'providers' });
			expect(settingsNav.view).toBe('page');
			expect(settingsNav.category).toBe('providers');
		});

		it('skips the leftover entries under a deep stack on the way back to the index', () => {
			const go = vi.spyOn(history, 'go').mockImplementation(() => {});
			settingsNav.selectCategory('cloud');
			settingsNav.stacked = true;
			settingsNav.startStack({ settingsView: 'detail', settingsDepth: 2, settingsCategory: 'providers', settingsItem: 'old' });
			expect(settingsNav.depth).toBe(2);
			settingsNav.followHistory({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'providers' });
			expect(settingsNav.view).toBe('index');
			expect(settingsNav.category).toBe('cloud');
			expect(go).toHaveBeenCalledWith(-1);
			settingsNav.followHistory({});
			expect(settingsNav.view).toBe('index');
			expect(settingsNav.depth).toBe(0);
		});

		it('a deep link lands above a leftover entry, and Back skips that entry', () => {
			const go = vi.spyOn(history, 'go').mockImplementation(() => {});
			settingsNav.stacked = true;
			settingsNav.resetStack('index', security);
			settingsNav.go({ category: 'cloud' });
			expect(settingsNav.view).toBe('page');
			expect(settingsNav.depth).toBe(2);
			settingsNav.followHistory(security);
			expect(settingsNav.view).toBe('index');
			expect(settingsNav.category).toBe('cloud');
			expect(go).toHaveBeenCalledWith(-1);
		});

		it('a reload keeps skipping the entries left under its stack', () => {
			const go = vi.spyOn(history, 'go').mockImplementation(() => {});
			settingsNav.selectCategory('cloud');
			const top = { settingsView: 'page', settingsDepth: 2, settingsCategory: 'cloud', settingsStaleBelow: 2 } as const;
			const reloaded = new SettingsNavigation();
			reloaded.stacked = true;
			reloaded.startStack(top);
			expect(reloaded.depth).toBe(2);
			reloaded.followHistory({ settingsView: 'page', settingsDepth: 1, settingsCategory: 'providers' });
			expect(reloaded.view).toBe('index');
			expect(go).toHaveBeenCalledWith(-1);
		});
	});

	it('does not reopen a page after the reader went back to the index, or left Settings', () => {
		settingsNav.selectCategory('security');
		settingsNav.stacked = true;
		settingsNav.startStack();
		settingsNav.followHistory({});
		expect(new SettingsNavigation().view).toBe('page');
		const backed = new SettingsNavigation();
		backed.stacked = true;
		backed.startStack();
		expect(backed.view).toBe('index');

		settingsNav.selectCategory('security');
		settingsNav.forgetPlace();
		const reopened = new SettingsNavigation();
		reopened.stacked = true;
		reopened.startStack();
		expect(reopened.view).toBe('index');
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
