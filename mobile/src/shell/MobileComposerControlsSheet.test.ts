/**
 * The phone's chat controls (#1816): the quick bar under the composer and
 * the Chat controls panel it opens. Replaces the tests of the old in-place
 * sheet; the behaviours that sheet guaranteed (locked voice model, effort
 * explanation, fast mode only where supported, Away before the first message,
 * the shared searchable model picker with its context window, Escape going
 * back before closing) are pinned again here against the new layout.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import { platform } from '$lib/stores/platform.svelte';
import '$lib/test-stubs/composer-dom';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { api } from '$lib/api/client';
import { hydrateModelRegistry } from '$lib/constants/models';
import { buildModelPickerCatalog } from '$lib/constants/model-picker';
import { __resetWatches } from '$lib/stores/watches';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => platform.set('mobile'));
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	platform.set('desktop');
	appMode.set('code');
	activeSession.newChat();
	__resetWatches();
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
});

const ACCESS = [
	{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
	{ value: 'auto', label: 'Auto', description: 'Model decides per tool call' },
	{ value: 'acceptEdits', label: 'Accept Edits', description: 'File edits run without asking' },
	{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
];
const EFFORT = ['low', 'medium', 'high', 'xhigh', 'max'].map((value) => ({
	value, label: value === 'xhigh' ? 'XHigh' : value[0].toUpperCase() + value.slice(1), description: value
}));

function props(overrides: Record<string, unknown> = {}) {
	return {
		open: false,
		onClose: vi.fn(),
		onOpenChange: vi.fn(),
		modelOptions: [
			{ value: 'sonnet', label: 'Sonnet', description: 'Balanced', group: 'Claude', groupKey: 'claude', groupSubtext: 'Pro' },
			{ value: 'opus', label: 'Opus', description: 'Deep', group: 'Claude', groupKey: 'claude', groupSubtext: 'Pro' }
		],
		modelValue: 'opus',
		modelLabel: 'Opus',
		onModelSelect: vi.fn(),
		contextChoice: 'standard' as const,
		onContextChoiceSelect: vi.fn(),
		effortOptions: EFFORT,
		effortValue: 'high',
		effortDefaultValue: 'high',
		onEffortSelect: vi.fn(),
		permissionOptions: ACCESS,
		permissionValue: 'default',
		onPermissionSelect: vi.fn(),
		onToggleAway: vi.fn(),
		awayAvailable: true,
		showSetup: false,
		...overrides
	};
}

const bar = () => screen.getByRole('toolbar', { name: /Quick controls/ });
const cell = (name: RegExp) => within(bar()).getByRole('button', { name });
const panel = () => document.querySelector<HTMLElement>('.mcp')!;

/** A finger drag from `from` by (dx, dy), through the window listeners the sheet uses. */
async function drag(target: Element, dx: number, dy: number, from = { x: 100, y: 700 }) {
	await fireEvent.pointerDown(target, { pointerId: 7, button: 0, clientX: from.x, clientY: from.y });
	for (let i = 1; i <= 4; i++) {
		await fireEvent.pointerMove(window, { pointerId: 7, clientX: from.x + (dx * i) / 4, clientY: from.y + (dy * i) / 4 });
	}
	await fireEvent.pointerUp(window, { pointerId: 7, clientX: from.x + dx, clientY: from.y + dy });
}

describe('quick bar', () => {
	it('shows each control\'s state through its own icon and label', () => {
		Object.assign(activeSession.current, {
			activeTasks: [
				{ id: 'a', subject: 'Agent', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: '', updatedAt: '' },
				{ id: 'b', subject: 'pytest', status: 'running', kind: 'shell', summary: null, toolUseId: null, startedAt: '', updatedAt: '' }
			]
		});
		activeSession.setDraft('');
		render(MobileComposerControlsSheet, { props: props({ fastSupported: true, fastEnabled: true, permissionValue: 'acceptEdits' }) });
		expect(cell(/^Model: Opus, fast mode$/).textContent).toContain('Opus');
		expect(cell(/^Reasoning effort: High$/).textContent).toContain('High');
		expect(cell(/^Tool access: Accept edits$/).textContent).toContain('Edits');
		expect(within(bar()).getByRole('button', { name: /^Context and usage/ })).toBeTruthy();
		expect(cell(/^All chat controls, 2 running$/).textContent).toContain('Controls');
		// No information lines: every cell is a one-word label.
		expect(bar().querySelectorAll('p, small')).toHaveLength(0);
	});

	it('styles Bypass like every other mode and names saving', () => {
		render(MobileComposerControlsSheet, { props: props({ permissionValue: 'bypassPermissions', permissionUpdating: true }) });
		const access = cell(/^Tool access: Bypass, saving$/);
		// No mode-specific class: Bypass uses the same theme tokens as Ask or Edits.
		expect([...access.classList].filter((c) => !c.startsWith('svelte-'))).toEqual(['qcell']);
		expect(access.getAttribute('aria-busy')).toBe('true');
	});

	it('opens a compact effort picker and applies the pick', async () => {
		const onEffortSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onEffortSelect }) });
		await fireEvent.click(cell(/^Reasoning effort/));
		const picker = screen.getByRole('dialog', { name: 'Reasoning effort' });
		expect(within(picker).getAllByRole('radio')).toHaveLength(5);
		expect(within(picker).getByRole('radio', { name: 'High' }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(within(picker).getByRole('radio', { name: 'Max' }));
		expect(onEffortSelect).toHaveBeenCalledExactlyOnceWith('max');
		expect(screen.queryByRole('dialog', { name: 'Reasoning effort' })).toBeNull();
	});

	it('opens a compact tool-access picker, with Bypass reachable by a deliberate tap', async () => {
		const onPermissionSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onPermissionSelect }) });
		await fireEvent.click(cell(/^Tool access/));
		const picker = screen.getByRole('dialog', { name: 'Tool access' });
		expect(within(picker).getAllByRole('radio').map((r) => r.textContent?.trim())).toEqual(['Ask', 'Auto', 'Accept edits', 'Bypass']);
		await fireEvent.click(within(picker).getByRole('radio', { name: 'Bypass' }));
		expect(onPermissionSelect).toHaveBeenCalledExactlyOnceWith('bypassPermissions');
	});

	it('closes a picker with Escape and on a tap elsewhere', async () => {
		render(MobileComposerControlsSheet, { props: props() });
		await fireEvent.click(cell(/^Reasoning effort/));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog', { name: 'Reasoning effort' })).toBeNull();
		await fireEvent.click(cell(/^Tool access/));
		await fireEvent.pointerDown(document.body);
		expect(screen.queryByRole('dialog', { name: 'Tool access' })).toBeNull();
	});

	it('steps effort one level with a sideways swipe', async () => {
		const onEffortSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onEffortSelect }) });
		await drag(cell(/^Reasoning effort/), 60, 0);
		expect(onEffortSelect).toHaveBeenCalledExactlyOnceWith('xhigh');
		await drag(cell(/^Reasoning effort/), -60, 0);
		expect(onEffortSelect).toHaveBeenLastCalledWith('medium');
	});

	it('steps tool access with a swipe but never onto Bypass', async () => {
		const onPermissionSelect = vi.fn();
		const view = render(MobileComposerControlsSheet, { props: props({ onPermissionSelect, permissionValue: 'auto' }) });
		await drag(cell(/^Tool access/), 60, 0);
		expect(onPermissionSelect).toHaveBeenCalledExactlyOnceWith('acceptEdits');
		onPermissionSelect.mockClear();
		await view.rerender(props({ onPermissionSelect, permissionValue: 'acceptEdits' }));
		await drag(cell(/^Tool access/), 120, 0);
		expect(onPermissionSelect).not.toHaveBeenCalled();
		// The swipe did not open the picker either.
		expect(screen.queryByRole('dialog', { name: 'Tool access' })).toBeNull();
	});

	it('locks effort where the model sets it, and says why on tap', async () => {
		render(MobileComposerControlsSheet, { props: props({ effortOptions: [], effortValue: null, effortExplanation: 'Set by the model' }) });
		const effort = cell(/^Reasoning effort: set by the model$/);
		expect(effort.textContent).toContain('Fixed');
		await fireEvent.click(effort);
		expect(screen.queryByRole('dialog', { name: 'Reasoning effort' })).toBeNull();
	});

	it('keeps an integrated voice model visible but locked', async () => {
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ modelLabel: 'PersonaPlex', modelLocked: true, onOpenChange }) });
		const model = cell(/^Model: PersonaPlex, locked$/);
		expect(model.getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(model);
		expect(onOpenChange).not.toHaveBeenCalled();
	});
});

describe('Chat controls panel', () => {
	it('opens from Controls, and closes from its chevron, the scrim and Escape', async () => {
		const onOpenChange = vi.fn();
		const view = render(MobileComposerControlsSheet, { props: props({ onOpenChange }) });
		expect(panel().getAttribute('aria-hidden')).toBe('true');
		await fireEvent.click(cell(/^All chat controls/));
		expect(onOpenChange).toHaveBeenLastCalledWith(true);
		await view.rerender(props({ onOpenChange, open: true }));
		expect(panel().getAttribute('aria-hidden')).toBe('false');
		expect(screen.getByRole('dialog', { name: 'Chat controls' })).toBe(panel());
		await fireEvent.click(screen.getByRole('button', { name: 'Close chat controls' }));
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
		onOpenChange.mockClear();
		await fireEvent.click(document.querySelector('.mcp-scrim')!);
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
		onOpenChange.mockClear();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
	});

	it('opens when the quick bar is dragged up, not on a short drag down', async () => {
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onOpenChange }) });
		await drag(bar(), 0, 30);
		expect(onOpenChange).not.toHaveBeenCalled();
		await drag(bar(), 0, -400);
		expect(onOpenChange).toHaveBeenLastCalledWith(true);
	});

	it('closes when its header is dragged down', async () => {
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onOpenChange, open: true }) });
		Object.defineProperty(panel(), 'offsetHeight', { configurable: true, value: 700 });
		await drag(document.querySelector('.mcp-head')!, 0, 400, { x: 200, y: 120 });
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
	});

	it('holds labels, values and controls with no description paragraphs', () => {
		render(MobileComposerControlsSheet, { props: props({ open: true, fastSupported: true }) });
		const dialog = panel();
		expect(within(dialog).getByRole('button', { name: 'Model: Opus' })).toBeTruthy();
		expect(within(dialog).getByRole('button', { name: /Context and usage/ })).toBeTruthy();
		expect(within(dialog).getByRole('switch', { name: 'Away mode' })).toBeTruthy();
		expect(within(dialog).getByRole('switch', { name: 'Fast mode' })).toBeTruthy();
		expect(within(dialog).getByRole('radiogroup', { name: 'Reasoning effort' })).toBeTruthy();
		expect(within(dialog).getByRole('radiogroup', { name: 'Tool access' })).toBeTruthy();
		expect(within(dialog).getByRole('group', { name: 'More actions' })).toBeTruthy();
		// Helper text is five words or fewer.
		for (const help of dialog.querySelectorAll('.tg-help, .access-help')) {
			expect(help.textContent!.trim().split(/\s+/).length).toBeLessThanOrEqual(5);
		}
	});

	it('applies effort and tool access from the panel', async () => {
		const onEffortSelect = vi.fn();
		const onPermissionSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onEffortSelect, onPermissionSelect }) });
		const efforts = within(panel()).getByRole('radiogroup', { name: 'Reasoning effort' });
		await fireEvent.click(within(efforts).getByRole('radio', { name: /Low/ }));
		expect(onEffortSelect).toHaveBeenCalledExactlyOnceWith('low');
		const access = within(panel()).getByRole('radiogroup', { name: 'Tool access' });
		await fireEvent.click(within(access).getByRole('radio', { name: /Accept edits/ }));
		expect(onPermissionSelect).toHaveBeenCalledExactlyOnceWith('acceptEdits');
	});

	it('keeps Away locked until the chat has started', async () => {
		const onToggleAway = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onToggleAway, awayAvailable: false }) });
		const away = within(panel()).getByRole('switch', { name: 'Away mode' });
		expect(away.getAttribute('aria-disabled')).toBe('true');
		expect(away.textContent).toContain('After first message');
		await fireEvent.click(away);
		expect(onToggleAway).not.toHaveBeenCalled();
	});

	it('shows Away on the quick bar, as the desktop composer does, without opening the panel', async () => {
		const view = render(MobileComposerControlsSheet, { props: props({ awayEnabled: true }) });
		const access = cell(/^Tool access: Ask, Away mode on$/);
		expect(access.querySelector('[data-away-badge]')).toBeTruthy();
		await view.rerender(props({ awayEnabled: false }));
		expect(cell(/^Tool access: Ask$/).querySelector('[data-away-badge]')).toBeNull();
		// Before the first message Away cannot be on, whatever the store says.
		await view.rerender(props({ awayEnabled: true, awayAvailable: false }));
		expect(cell(/^Tool access: Ask$/).querySelector('[data-away-badge]')).toBeNull();
	});

	it('offers fast mode only where the model supports it', async () => {
		const onToggleFast = vi.fn();
		const view = render(MobileComposerControlsSheet, { props: props({ open: true, fastSupported: true, onToggleFast }) });
		await fireEvent.click(within(panel()).getByRole('switch', { name: 'Fast mode' }));
		expect(onToggleFast).toHaveBeenCalledOnce();
		await view.rerender(props({ open: true, fastSupported: false }));
		expect(within(panel()).queryByRole('switch', { name: 'Fast mode' })).toBeNull();
	});

	it('opens the app\'s model picker, with its context window, and returns after a pick', async () => {
		vi.spyOn(api, 'get').mockResolvedValueOnce({
			models: [{ value: 'opus', label: 'Opus', family: 'opus', context_window: 1_000_000 }], default: 'opus', legacy_map: {},
			providers: [{ id: 'codex', name: 'ChatGPT', provider_type: 'chatgpt', default_model: 'gpt-6.1-sol',
				models: [{ id: 'gpt-6.1-sol', label: 'Sol 6.1', context_window: 272_000, context_window_choices: [272_000, 1_000_000] }] }]
		});
		await hydrateModelRegistry();
		const catalog = buildModelPickerCatalog();
		const onModelSelect = vi.fn();
		const onContextChoiceSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({
			open: true, modelOptions: catalog.options, modelGroups: catalog.groups,
			modelValue: 'prov:codex:gpt-6.1-sol', modelLabel: 'Sol 6.1', onModelSelect, onContextChoiceSelect
		}) });
		await fireEvent.click(within(panel()).getByRole('button', { name: 'Model: Sol 6.1' }));
		expect(screen.getByRole('dialog', { name: 'Choose model' })).toBe(panel());
		expect(within(panel()).getByRole('textbox', { name: /Search models/i })).toBeTruthy();
		await fireEvent.click(within(panel()).getByRole('radio', { name: '1M · Extended' }));
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('1m');
		// Escape goes back to the controls first.
		await fireEvent.keyDown(window, { key: 'Escape' });
		await waitFor(() => expect(screen.getByRole('dialog', { name: 'Chat controls' })).toBe(panel()));
	});

	it('shows a running-tasks summary row that opens the running list', async () => {
		Object.assign(activeSession.current, {
			activeTasks: [{ id: 'a', subject: 'Agent', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: '', updatedAt: '' }]
		});
		activeSession.setDraft('');
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onOpenChange }) });
		const row = within(panel()).getByRole('button', { name: '1 running: 1 agent. Open the list' });
		expect(row.textContent).toContain('1 agent');
		await fireEvent.click(row);
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
	});

	it('gives the owner\'s watches the same row, without the running badge (#371)', async () => {
		const get = api.get.bind(api);
		vi.spyOn(api, 'get').mockImplementation(((path: string, options?: unknown) =>
			path === '/watches'
				? Promise.resolve({ watches: [
					{ id: 'w1', kind: 'watch', what: 'Inbox', status: 'active', last_fired_at: null, cost_usd: 0, can_pause: true, can_stop: true },
					{ id: 'w2', kind: 'watch', what: 'CI', status: 'paused', last_fired_at: null, cost_usd: 0, can_pause: false, can_stop: true },
					{ id: 'w3', kind: 'watch', what: 'Old', status: 'stopped', last_fired_at: null, cost_usd: 0, can_pause: false, can_stop: false }
				] })
				: get(path, options as never)) as typeof api.get);
		activeSession.setDraft('');
		render(MobileComposerControlsSheet, { props: props({ open: true }) });
		const row = await within(panel()).findByRole('button', { name: '2 watches: 1 paused. Open the list' });
		expect(row.textContent).toContain('2 watches');
		expect(row.textContent).toContain('1 paused');
		expect(cell(/^All chat controls$/)).toBeTruthy();
	});

	it('carries Call, Browser and Export as actions, and no stash button', async () => {
		const onBrowserToggle = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onBrowserToggle }) });
		const actions = within(panel()).getByRole('group', { name: 'More actions' });
		expect(within(actions).getByRole('button', { name: 'Live browser view' })).toBeTruthy();
		expect(within(actions).getByRole('button', { name: 'Export chat' })).toBeTruthy();
		expect(within(panel()).queryByRole('button', { name: /stash/i })).toBeNull();
		await fireEvent.click(within(actions).getByRole('button', { name: 'Live browser view' }));
		expect(onBrowserToggle).toHaveBeenCalledOnce();
	});

	it('has no Phone cell: the phone opens when the agent uses it', () => {
		render(MobileComposerControlsSheet, { props: props({ open: true, onBrowserToggle: vi.fn(),
			terminal: { open: false, disabledReason: null, toggle: vi.fn(), preload: vi.fn() } }) });
		const actions = within(panel()).getByRole('group', { name: 'More actions' });
		expect(within(actions).getByRole('button', { name: 'Terminal' })).toBeTruthy();
		expect(within(panel()).queryByRole('button', { name: /phone|emulator/i })).toBeNull();
	});

	it('shows a Terminal cell only when the worker offers terminals, and it closes the sheet', async () => {
		const { unmount } = render(MobileComposerControlsSheet, { props: props({ open: true }) });
		expect(within(panel()).queryByRole('button', { name: 'Terminal' })).toBeNull();
		unmount();
		const toggle = vi.fn();
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onOpenChange,
			terminal: { open: false, disabledReason: 'That chat runs on DesktopWorker.', toggle, preload: vi.fn() } }) });
		const button = within(panel()).getByRole('button', { name: 'Terminal' });
		expect(button.getAttribute('aria-pressed')).toBe('false');
		expect(button.getAttribute('title')).toBe('That chat runs on DesktopWorker.');
		await fireEvent.click(button);
		expect(toggle).toHaveBeenCalledOnce();
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
	});
});

describe('every chat (#371 step 5)', () => {
	it('keeps Away mode and Tool access in the panel even without setup tiles', () => {
		render(MobileComposerControlsSheet, { props: props({
			open: true, showSetup: false, showEffort: false, fastSupported: true
		}) });
		expect(cell(/^Tool access: Ask$/)).toBeTruthy();
		const dialog = panel();
		expect(within(dialog).getByRole('switch', { name: 'Fast mode' })).toBeTruthy();
		expect(within(dialog).getByRole('switch', { name: 'Away mode' })).toBeTruthy();
		expect(within(dialog).getByRole('radiogroup', { name: 'Tool access' })).toBeTruthy();
		expect(within(dialog).getByRole('heading', { name: 'Model and setup' })).toBeTruthy();
	});
});
