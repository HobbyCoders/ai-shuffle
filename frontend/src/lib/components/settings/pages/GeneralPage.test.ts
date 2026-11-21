import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const native = vi.hoisted(() => ({
	bridge: null as null | { platform: string; loginItem?: { get: ReturnType<typeof vi.fn>; set: ReturnType<typeof vi.fn> } },
	get: vi.fn(), set: vi.fn()
}));
vi.mock('$lib/services/desktop', () => ({ get desktop() { return native.bridge; } }));

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport, getErrorMessage: (_e: unknown, fallback: string) => fallback }));
vi.mock('$lib/stores/theme.svelte', async () => {
	const { writable } = await import('svelte/store');
	return {
		theme: { setTheme: vi.fn(), setPreset: vi.fn() },
		themePreference: writable('system'),
		currentPresetId: writable(null),
		themePresets: []
	};
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { timezonePreference: { ...writable('local'), load: vi.fn(async () => {}), setTimezone: vi.fn() } };
});

import GeneralPage from './GeneralPage.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
import { saveStatus } from '$lib/settings/save-status.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { query } from '$lib/settings/search';

const switchFor = (name: string) => screen.getByRole('switch', { name }) as HTMLButtonElement;

beforeEach(() => {
	native.bridge = null;
	native.get.mockReset().mockResolvedValue('disabled');
	native.set.mockReset();
	transport.get.mockResolvedValue({});
	transport.put.mockResolvedValue({});
	chatPreferences.reset();
	saveStatus.reset();
});
afterEach(cleanup);

describe('General page', () => {
	it('renders a row for every General entry', async () => {
		native.bridge = { platform: 'win32', loginItem: native };
		await assertCategoryCoverage('general', () => render(GeneralPage).container);
	});

	it('groups the chat preferences under Conversation and Thinking and tools', () => {
		render(GeneralPage);
		const groups = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim());
		expect(groups).toEqual(['Appearance', 'Conversation', 'Thinking and tools', 'Region']);
		expect(screen.getByText(/uses an extra model call/i)).toBeTruthy();
	});

	it('saves a switch through the chat preferences store and shows the Saved tick', async () => {
		render(GeneralPage);
		const expand = switchFor('Expand thinking by default');
		expect(expand.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(expand);
		expect(chatPreferences.current.thinkingStartExpanded).toBe(true);
		expect(expand.getAttribute('aria-checked')).toBe('true');
		expect(saveStatus.visible).toBe(true);
	});

	it('marks a changed row and resets it to the default', async () => {
		render(GeneralPage);
		await fireEvent.click(switchFor('Group tool calls'));
		expect(chatPreferences.current.groupToolCalls).toBe(false);
		await fireEvent.click(screen.getByRole('button', { name: 'Reset Group tool calls to default' }));
		expect(chatPreferences.current.groupToolCalls).toBe(true);
	});

	it('disables the thinking options while thinking blocks are hidden', async () => {
		render(GeneralPage);
		expect(switchFor('Expand thinking by default').disabled).toBe(false);
		await fireEvent.click(switchFor('Show thinking blocks'));
		expect(chatPreferences.current.showThinkingBlocks).toBe(false);
		expect(switchFor('Expand thinking by default').disabled).toBe(true);
		expect(switchFor('Show thinking summary').disabled).toBe(true);
		expect(switchFor('Expand tool calls by default').disabled).toBe(false);
	});

	it.each([null, { platform: 'linux' }, { platform: 'win32' }])('omits startup from the page and search without native capability (%j)', (bridge) => {
		native.bridge = bridge;
		render(GeneralPage);
		expect(screen.queryByRole('switch', { name: 'Start AI Shuffle at sign-in' })).toBeNull();
		expect(query('startup').some((i) => i.target.anchor === 'general.startAtSignIn')).toBe(false);
		expect(native.get).not.toHaveBeenCalled();
	});

	it('indexes startup and only changes the switch when the OS confirms the write', async () => {
		native.bridge = { platform: 'win32', loginItem: native };
		// Not the first result: "Check for updates at startup" has the word in its title.
		expect(query('startup').map((i) => i.target)).toContainEqual({ category: 'general', anchor: 'general.startAtSignIn' });
		render(GeneralPage);
		const control = await screen.findByRole('switch', { name: 'Start AI Shuffle at sign-in' });
		expect(native.set).not.toHaveBeenCalled();
		let finish!: (result: { state: string; failed: boolean }) => void;
		native.set.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		await fireEvent.click(control);
		expect(control.hasAttribute('disabled')).toBe(true);
		expect(control.getAttribute('aria-checked')).toBe('false');
		finish({ state: 'disabled', failed: true });
		await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/Could not turn on startup/));
		expect(control.getAttribute('aria-checked')).toBe('false');
		native.set.mockResolvedValueOnce({ state: 'enabled', failed: false });
		await fireEvent.click(control);
		await waitFor(() => expect(control.getAttribute('aria-checked')).toBe('true'));
		expect(saveStatus.visible).toBe(false);
	});

	it.each(['read', 'write'])('coalesces a focus refresh during a native %s', async (operation) => {
		native.bridge = { platform: 'win32', loginItem: native };
		let finish!: () => void;
		if (operation === 'read') {
			native.get.mockImplementationOnce(() => new Promise((resolve) => {
				finish = () => resolve('enabled');
			}));
		}
		render(GeneralPage);
		if (operation === 'write') {
			const control = await screen.findByRole('switch', { name: 'Start AI Shuffle at sign-in' });
			native.set.mockImplementationOnce(() => new Promise((resolve) => {
				finish = () => resolve({ state: 'enabled', failed: false });
			}));
			await fireEvent.click(control);
		}
		await waitFor(() => expect(finish).toBeTypeOf('function'));
		native.get.mockResolvedValueOnce('blocked');
		await fireEvent.focus(window);
		await fireEvent.focus(window);
		expect(native.get).toHaveBeenCalledTimes(1);
		finish();
		await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Startup is off.'));
		expect(switchFor('Start AI Shuffle at sign-in').getAttribute('aria-checked')).toBe('false');
		expect(native.get).toHaveBeenCalledTimes(2);
	});

	it('shows external denial on focus and lets the user remove a blocked item', async () => {
		native.bridge = { platform: 'darwin', loginItem: native };
		native.get.mockResolvedValueOnce('enabled');
		render(GeneralPage);
		const control = await screen.findByRole('switch', { name: 'Start AI Shuffle at sign-in', checked: true });
		native.get.mockResolvedValueOnce('blocked');
		await fireEvent.focus(window);
		await waitFor(() => expect(control.getAttribute('aria-checked')).toBe('false'));
		expect(screen.getByRole('status').textContent).toContain('Login Items & Extensions');
		native.set.mockResolvedValueOnce({ state: 'disabled', failed: false });
		await fireEvent.click(screen.getByRole('button', { name: 'Remove startup item' }));
		expect(native.set).toHaveBeenCalledWith(false);
		await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove startup item' })).toBeNull());
	});

	it.each(['rejects', 'returns unknown', 'returns unavailable'])('does not claim Off when a turn-off write %s', async (mode) => {
		native.bridge = { platform: 'win32', loginItem: native };
		native.get.mockResolvedValue('enabled');
		let fail!: () => void;
		native.set.mockImplementationOnce(() => new Promise((resolve, reject) => {
			fail = () => mode === 'rejects' ? reject(Error('OS denied'))
				: resolve({ state: mode === 'returns unknown' ? 'unknown' : 'unavailable', failed: true });
		}));
		render(GeneralPage);
		const control = await screen.findByRole('switch', { name: 'Start AI Shuffle at sign-in', checked: true });
		await fireEvent.click(control);
		expect((control as HTMLButtonElement).disabled).toBe(true);
		expect(control.getAttribute('aria-checked')).toBe('true');
		fail();
		await waitFor(() => expect(screen.queryByRole('switch', { name: 'Start AI Shuffle at sign-in' })).toBeNull());
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		await screen.findByRole('switch', { name: 'Start AI Shuffle at sign-in', checked: true });
		expect(native.set.mock.calls).toEqual([[false]]);
	});

	it('does not render a switch during the initial read or on an unavailable host', async () => {
		native.bridge = { platform: 'darwin', loginItem: native };
		let finish!: (state: string) => void;
		native.get.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		render(GeneralPage);
		expect(screen.queryByRole('switch', { name: 'Start AI Shuffle at sign-in' })).toBeNull();
		expect(screen.getByRole('status').textContent).toMatch(/Checking startup settings/);
		finish('unavailable');
		await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/unavailable/));
		expect(screen.queryByRole('switch', { name: 'Start AI Shuffle at sign-in' })).toBeNull();
	});

	it('hides the unconfirmed switch if reading fails and can retry safely', async () => {
		native.bridge = { platform: 'darwin', loginItem: native };
		native.get.mockRejectedValueOnce(Error('OS denied'));
		render(GeneralPage);
		await fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(switchFor('Start AI Shuffle at sign-in').disabled).toBe(false));
		expect(native.set).not.toHaveBeenCalled();
	});

	it('follows a change that arrives through the store', async () => {
		render(GeneralPage);
		chatPreferences.updatePreference('toolsStartExpanded', true);
		await Promise.resolve();
		expect(switchFor('Expand tool calls by default').getAttribute('aria-checked')).toBe('true');
	});
});
