import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';

const mocks = vi.hoisted(() => ({ pushState: vi.fn() }));

vi.mock('$app/navigation', () => ({ pushState: mocks.pushState, goto: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { auth: { checkAuth: vi.fn() }, isAdmin: writable(true) };
});
vi.mock('$lib/stores/theme.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		theme: { setTheme: vi.fn(), setPreset: vi.fn(), current: { preference: 'dark', resolved: 'dark', presetId: null } },
		themePreference: writable('dark'),
		currentPresetId: writable(null),
		themePresets: [{ id: 'mono', label: 'Mono', styles: { light: {}, dark: {} } }]
	};
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(async () => {}), setTimezone: vi.fn() } };
});

import { page } from '$app/state';
import { isAdmin } from '$lib/stores/auth.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { registerShortcut } from '$lib/services/keyboard';
import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
import { settingsNav, encodeTarget } from '$lib/settings/navigation.svelte';
import SettingsShell from './SettingsShell.svelte';

const admin = isAdmin as unknown as { set: (v: boolean) => void };

beforeEach(() => {
	settingsNav.reset();
	sessionStorage.clear();
	mocks.pushState.mockReset();
	admin.set(true);
	page.state = {};
});
afterEach(() => {
	cleanup();
	platform.set('desktop');
	vi.restoreAllMocks();
});

function nav() {
	return screen.getByRole('navigation', { name: 'Settings' });
}

describe('SettingsShell index', () => {
	it('lists categories in three groups and opens General', async () => {
		render(SettingsShell);
		const index = nav();
		for (const group of ['Personal', 'AI', 'This worker']) expect(within(index).getByText(group)).toBeTruthy();
		const names = within(index)
			.getAllByRole('button')
			.map((b) => b.textContent?.trim());
		expect(names).toEqual([
			'General',
			'Archived chats',
			'Voice',
			'Providers',
			'Default models',
			'Integrations',
			'Plugins',
			'Devices',
			'Security',
			'System',
			'Logs'
		]);
		expect(within(index).getByRole('button', { name: 'General' }).getAttribute('aria-current')).toBe('page');
		expect(screen.getByRole('heading', { level: 1, name: 'General' })).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Appearance' })).toBeTruthy();
	});

	it('moves between categories with Up and Down', async () => {
		render(SettingsShell);
		const general = within(nav()).getByRole('button', { name: 'General' });
		general.focus();
		await fireEvent.keyDown(general, { key: 'ArrowDown' });
		expect(document.activeElement).toBe(within(nav()).getByRole('button', { name: 'Archived chats' }));
	});

	it('loads a collection page on first visit and focuses its heading', async () => {
		render(SettingsShell);
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Logs' }));
		const heading = await screen.findByRole('heading', { level: 1, name: 'Logs' });
		expect(screen.getByPlaceholderText('Search logs')).toBeTruthy();
		expect(screen.getByRole('separator', { name: 'Resize list' })).toBeTruthy();
		await waitFor(() => expect(document.activeElement).toBe(heading));
		expect(within(nav()).getByRole('button', { name: 'Logs' }).getAttribute('aria-current')).toBe('page');
	});

	it('shows an attention count from a category module', async () => {
		const { MODULES } = await import('$lib/settings/registry');
		const before = MODULES.logs;
		MODULES.logs = { entries: [], attention: () => ({ count: 3, tone: 'err' }) };
		try {
			render(SettingsShell);
			const logs = within(nav()).getByRole('button', { name: /Logs/ });
			expect(logs.textContent).toContain('3');
			expect(within(logs).getByText(/items need attention/)).toBeTruthy();
		} finally {
			MODULES.logs = before;
		}
	});
});

describe('SettingsShell search', () => {
	it('replaces the nav with results and jumps to the row, flashing it', async () => {
		const { container } = render(SettingsShell);
		await fireEvent.click(within(nav()).getByRole('button', { name: 'Logs' }));
		await screen.findByRole('heading', { level: 1, name: 'Logs' });

		const box = screen.getByRole('combobox', { name: 'Search settings' });
		await fireEvent.input(box, { target: { value: 'timezone' } });
		expect(box.getAttribute('aria-expanded')).toBe('true');
		const list = screen.getByRole('listbox', { name: 'Search results' });
		const option = within(list).getAllByRole('option')[0];
		expect(option.textContent).toContain('Time zone');
		expect(option.textContent).toContain('General, Region');
		expect(box.getAttribute('aria-activedescendant')).toBe(option.id);
		expect(within(nav()).queryByRole('button', { name: 'Voice' })).toBeNull();

		await fireEvent.keyDown(box, { key: 'Enter' });
		expect((box as HTMLInputElement).value).toBe('');
		expect(within(nav()).getByRole('button', { name: 'Voice' })).toBeTruthy();
		await screen.findByRole('heading', { level: 1, name: 'General' });
		await waitFor(() =>
			expect(container.querySelector('[data-setting-anchor="general.timezone"]')?.classList.contains('st-flash')).toBe(true)
		);
	});

	it('moves through results with arrows and clears with Esc', async () => {
		render(SettingsShell);
		const box = screen.getByRole('combobox', { name: 'Search settings' });
		await fireEvent.input(box, { target: { value: 'the' } });
		const options = within(screen.getByRole('listbox')).getAllByRole('option');
		expect(options.length).toBeGreaterThan(1);
		await fireEvent.keyDown(box, { key: 'ArrowDown' });
		expect(options[1].getAttribute('aria-selected')).toBe('true');
		await fireEvent.keyDown(box, { key: 'Escape' });
		expect((box as HTMLInputElement).value).toBe('');
		expect(box.getAttribute('aria-expanded')).toBe('false');
	});

	it('says when nothing matches', async () => {
		render(SettingsShell);
		await fireEvent.input(screen.getByRole('combobox', { name: 'Search settings' }), { target: { value: 'zzzz' } });
		expect(screen.getByText('No setting matches “zzzz”')).toBeTruthy();
	});

	it('focuses search with "/" outside fields', async () => {
		render(SettingsShell);
		const general = within(nav()).getByRole('button', { name: 'General' });
		general.focus();
		await fireEvent.keyDown(general, { key: '/' });
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
	});
});

describe('SettingsShell Ctrl+K', () => {
	it('takes Ctrl+K while mounted and gives it back to Spotlight on unmount', async () => {
		const spotlight = vi.fn();
		const reg = registerShortcut({ id: 'spotlight', description: 'Spotlight', key: 'k', cmdOrCtrl: true, category: 'navigation', action: spotlight });
		try {
			const view = render(SettingsShell);
			await tick();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).not.toHaveBeenCalled();
			await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));

			view.unmount();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).toHaveBeenCalledOnce();
		} finally {
			reg.unregister();
		}
	});

	it('leaves Ctrl+K to Spotlight while the kept-alive panel is off screen', async () => {
		const spotlight = vi.fn();
		const reg = registerShortcut({ id: 'spotlight', description: 'Spotlight', key: 'k', cmdOrCtrl: true, category: 'navigation', action: spotlight });
		try {
			render(SettingsShell, { context: new Map([[FEATURE_PANEL_CONTEXT, { active: false }]]) });
			await tick();
			await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
			expect(spotlight).toHaveBeenCalledOnce();
		} finally {
			reg.unregister();
		}
	});
});

describe('SettingsShell read-only', () => {
	it('shows the approval banner and disables controls', async () => {
		admin.set(false);
		render(SettingsShell);
		const banner = screen.getByText('This device needs approval').closest('[role="status"]')!;
		expect(banner.textContent).toContain('Approve from an approved device');
		const theme = screen.getByRole('group', { name: 'Theme' });
		for (const name of ['Light', 'Dark', 'System']) {
			expect((within(theme).getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
		}
		expect((screen.getByRole('combobox', { name: 'Time zone' }) as HTMLSelectElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Open Devices' }));
		await screen.findByRole('heading', { level: 1, name: 'Devices' });
	});
});

describe('SettingsShell deep links', () => {
	it('applies initialItemId, clears it, and reveals the anchor', async () => {
		const clear = vi.spyOn(layout, 'clearFeatureItemId');
		const { container } = render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'general', anchor: 'general.timezone' })
		});
		expect(clear).toHaveBeenCalled();
		await waitFor(() =>
			expect(container.querySelector('[data-setting-anchor="general.timezone"]')?.classList.contains('st-flash')).toBe(true)
		);
	});

	it('lands legacy tab names', async () => {
		settingsNav.selectCategory('voice');
		render(SettingsShell, { initialItemId: 'logs' });
		await screen.findByRole('heading', { level: 1, name: 'Logs' });
	});

	it('opens an expandable row named by the target', async () => {
		render(SettingsShell, {
			initialItemId: encodeTarget({ category: 'general', anchor: 'general.themeStyle', open: 'general.themeStyle' })
		});
		await screen.findByRole('group', { name: 'Theme styles' });
	});
});

describe('SettingsShell stacked mode', () => {
	it('shows one pane at a time and pushes a history entry per step', async () => {
		platform.set('mobile');
		const back = vi.spyOn(history, 'back').mockImplementation(() => {});
		const { container } = render(SettingsShell);
		const shell = container.querySelector('.settings-shell')!;
		await tick();
		expect(shell.hasAttribute('data-stacked')).toBe(true);
		expect(shell.getAttribute('data-view')).toBe('index');

		await fireEvent.click(within(nav()).getByRole('button', { name: 'Voice' }));
		expect(shell.getAttribute('data-view')).toBe('page');
		expect(mocks.pushState).toHaveBeenCalledWith('', expect.objectContaining({ settingsView: 'page', settingsDepth: 1 }));

		await fireEvent.click(screen.getByRole('button', { name: 'Back to Settings' }));
		expect(back).toHaveBeenCalledOnce();
		page.state = {};
		await tick();
		expect(shell.getAttribute('data-view')).toBe('index');
	});

	it('Ctrl+K returns to the index before focusing search', async () => {
		platform.set('mobile');
		const go = vi.spyOn(history, 'go').mockImplementation(() => {});
		const { container } = render(SettingsShell);
		await tick();
		await fireEvent.click(within(nav()).getByRole('button', { name: 'System' }));
		await fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
		expect(go).toHaveBeenCalledWith(-1);
		page.state = {};
		await tick();
		expect(container.querySelector('.settings-shell')?.getAttribute('data-view')).toBe('index');
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Search settings' })));
	});
});
