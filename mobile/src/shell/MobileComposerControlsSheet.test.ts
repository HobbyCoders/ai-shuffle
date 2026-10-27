/**
 * The phone's chat controls (#1816): the quick bar under the composer and
 * the Chat controls panel it opens. Replaces the tests of the old in-place
 * sheet; the behaviours that sheet guaranteed (locked voice model, effort
 * explanation, fast mode only where supported, Away before the first message,
 * the shared searchable model picker with its context window, Escape going
 * back before closing) are pinned again here against the new layout.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import { platform } from '$lib/stores/platform.svelte';
import '$lib/test-stubs/composer-dom';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { api } from '$lib/api/client';
import { hydrateModelRegistry } from '$lib/constants/models';
import { buildModelPickerCatalog } from '$lib/constants/model-picker';
import { __resetWatches } from '$lib/stores/watches';
import { page } from '$app/state';

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
	page.state = {};
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
	it('opens from Controls, and closes from its hidden Close button, the scrim and Escape', async () => {
		const onOpenChange = vi.fn();
		const view = render(MobileComposerControlsSheet, { props: props({ onOpenChange }) });
		expect(panel().getAttribute('aria-hidden')).toBe('true');
		await fireEvent.click(cell(/^All chat controls/));
		expect(onOpenChange).toHaveBeenLastCalledWith(true);
		await view.rerender(props({ onOpenChange, open: true }));
		expect(panel().getAttribute('aria-hidden')).toBe('false');
		expect(screen.getByRole('dialog', { name: 'Chat controls' })).toBe(panel());
		// No visible close: the button is there for keyboards and screen readers only.
		const close = screen.getByRole('button', { name: 'Close chat controls' });
		expect(close.classList.contains('mcp-close')).toBe(true);
		expect(within(panel()).getByRole('heading', { name: 'Chat controls' })).toBeTruthy();
		await fireEvent.click(close);
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

	describe('the usage sheet grip', () => {
		const sheet = () => document.querySelector<HTMLElement>('.usage-popover');
		const grip = () => document.querySelector<HTMLElement>('.usage-grip')!;
		const usageCell = () => document.querySelector<HTMLElement>('.qcell.usage')!;

		async function openUsageSheet(onUsageOpenChange = vi.fn(), onOpenChange = vi.fn()) {
			render(MobileComposerControlsSheet, { props: props({ onUsageOpenChange, onOpenChange }) });
			await fireEvent.click(within(bar()).getByRole('button', { name: /^Context and usage/ }));
			expect(sheet()).not.toBeNull();
			return onUsageOpenChange;
		}

		/** A drag on the grip with controlled timing, so the release speed is known. */
		async function gripDrag(dy: number, msPerStep: number, onHold?: () => void) {
			const from = { x: 180, y: 300 };
			let at = 1000;
			await fireEvent.pointerDown(grip(), { pointerId: 9, button: 0, clientX: from.x, clientY: from.y });
			for (let i = 1; i <= 4; i++) {
				at += msPerStep;
				const move = createEvent.pointerMove(window, { pointerId: 9, clientX: from.x, clientY: from.y + (dy * i) / 4 });
				Object.defineProperty(move, 'timeStamp', { value: at });
				await fireEvent(window, move);
			}
			onHold?.();
			await fireEvent.pointerUp(window, { pointerId: 9, clientX: from.x, clientY: from.y + dy });
		}

		it('shows an aria-hidden grip at the top of the sheet', async () => {
			await openUsageSheet();
			expect(sheet()!.firstElementChild).toBe(grip());
			expect(grip().getAttribute('aria-hidden')).toBe('true');
		});

		it('follows the finger and closes through the bar on a long drag down', async () => {
			const onUsageOpenChange = await openUsageSheet();
			onUsageOpenChange.mockClear();
			await gripDrag(300, 120, () => {
				expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('300px');
				expect(usageCell().classList.contains('held')).toBe(true);
			});
			await waitFor(() => expect(sheet()).toBeNull());
			expect(onUsageOpenChange).toHaveBeenLastCalledWith(false);
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('');
		});

		it('closes on a quick downward flick even when it is short', async () => {
			const onUsageOpenChange = await openUsageSheet();
			await gripDrag(60, 4);
			await waitFor(() => expect(sheet()).toBeNull());
			expect(onUsageOpenChange).toHaveBeenLastCalledWith(false);
		});

		it('springs back on a short slow drag, and barely moves when pulled up', async () => {
			const onUsageOpenChange = await openUsageSheet();
			onUsageOpenChange.mockClear();
			await gripDrag(40, 120);
			expect(sheet()).not.toBeNull();
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('');
			await gripDrag(-200, 120, () => {
				expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('-30px');
			});
			await new Promise((r) => setTimeout(r, 300));
			expect(sheet()).not.toBeNull();
			expect(onUsageOpenChange).not.toHaveBeenCalled();
		});

		it('keeps the drag from reaching the bar, the document or the chat as a click', async () => {
			const onOpenChange = vi.fn();
			const onUsageOpenChange = await openUsageSheet(vi.fn(), onOpenChange);
			const outside = vi.fn();
			document.addEventListener('click', outside);
			try {
				await gripDrag(40, 120);
				await fireEvent.click(grip());
				expect(outside).not.toHaveBeenCalled();
				expect(sheet()).not.toBeNull();
				// An upward drag on the grip never starts the bar's pull-up.
				await gripDrag(-300, 120);
				expect(onOpenChange).not.toHaveBeenCalled();
				expect(onUsageOpenChange).toHaveBeenCalledTimes(1);
			} finally {
				document.removeEventListener('click', outside);
			}
		});

		it('springs back without closing when the pointer is cancelled mid-drag', async () => {
			const onUsageOpenChange = await openUsageSheet();
			onUsageOpenChange.mockClear();
			await fireEvent.pointerDown(grip(), { pointerId: 11, button: 0, clientX: 180, clientY: 300 });
			for (let i = 1; i <= 4; i++) await fireEvent.pointerMove(window, { pointerId: 11, clientX: 180, clientY: 300 + i * 80 });
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('320px');
			await fireEvent.pointerCancel(window, { pointerId: 11, clientX: 180, clientY: 620 });
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('');
			expect(usageCell().classList.contains('held')).toBe(false);
			await new Promise((r) => setTimeout(r, 300));
			expect(sheet()).not.toBeNull();
			expect(onUsageOpenChange).not.toHaveBeenCalled();
		});

		it('clears the pending close when the sheet unmounts mid-slide', async () => {
			const view = render(MobileComposerControlsSheet, { props: props() });
			await fireEvent.click(within(bar()).getByRole('button', { name: /^Context and usage/ }));
			const setSpy = vi.spyOn(globalThis, 'setTimeout');
			const clearSpy = vi.spyOn(globalThis, 'clearTimeout');
			await gripDrag(300, 120);
			// The slide-out's timer: the one the release scheduled for its 240ms.
			const call = setSpy.mock.calls.findIndex(([, ms]) => ms === 240);
			expect(call).toBeGreaterThanOrEqual(0);
			const timer = setSpy.mock.results[call].value;
			clearSpy.mockClear();
			view.unmount();
			expect(clearSpy).toHaveBeenCalledWith(timer);
		});

		it('still pulls Chat controls up from the usage ring on the bar', async () => {
			const onOpenChange = vi.fn();
			render(MobileComposerControlsSheet, { props: props({ onOpenChange }) });
			await drag(within(bar()).getByRole('button', { name: /^Context and usage/ }), 0, -400);
			expect(onOpenChange).toHaveBeenLastCalledWith(true);
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('');
		});

		it('does not open Chat controls when the sheet content is pulled up', async () => {
			const onOpenChange = vi.fn();
			await openUsageSheet(vi.fn(), onOpenChange);
			await drag(sheet()!.querySelector('.ctx-section')!, 0, -400, { x: 180, y: 600 });
			expect(onOpenChange).not.toHaveBeenCalled();
			expect(sheet()).not.toBeNull();
		});

		it('does not move the sheet when its content is dragged down', async () => {
			await openUsageSheet();
			await drag(sheet()!.querySelector('.ctx-section')!, 0, 300, { x: 180, y: 600 });
			expect(usageCell().style.getPropertyValue('--usage-drag')).toBe('');
			expect(sheet()).not.toBeNull();
		});

		it('still closes on a tap outside and on Escape', async () => {
			const onUsageOpenChange = await openUsageSheet();
			await fireEvent.click(document.body);
			expect(sheet()).toBeNull();
			expect(onUsageOpenChange).toHaveBeenLastCalledWith(false);
			await fireEvent.click(within(bar()).getByRole('button', { name: /^Context and usage/ }));
			expect(sheet()).not.toBeNull();
			await fireEvent.keyDown(document, { key: 'Escape' });
			expect(sheet()).toBeNull();
		});
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
		expect(away.getAttribute('aria-checked')).toBe('false');
		expect(away.textContent).toContain('Available after the first message');
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
		const row = within(panel()).getByRole('button', { name: 'Model: Sol 6.1' });
		// The row carries the provider's own mark, not a letter tile.
		expect(row.querySelector('svg.provider-glyph')).toBeTruthy();
		await fireEvent.click(row);
		expect(screen.getByRole('dialog', { name: 'Model' })).toBe(panel());
		expect(panel().classList.contains('sub')).toBe(true);
		expect(within(panel()).getByRole('button', { name: 'Back to chat controls' })).toBeTruthy();
		expect(within(panel()).getByRole('textbox', { name: /Search models/i })).toBeTruthy();
		await fireEvent.click(within(panel()).getByRole('radio', { name: '1M · Extended' }));
		expect(onContextChoiceSelect).toHaveBeenLastCalledWith('1m');
		// Escape goes back to the controls first.
		await fireEvent.keyDown(window, { key: 'Escape' });
		await waitFor(() => expect(screen.getByRole('dialog', { name: 'Chat controls' })).toBe(panel()));
	});

	it('shows a running-tasks chip that opens the running list as a page', async () => {
		Object.assign(activeSession.current, {
			activeTasks: [{ id: 'a', subject: 'Agent', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: '', updatedAt: '' }]
		});
		activeSession.setDraft('');
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onOpenChange }) });
		// The stubbed router never records the panel's history entry; ignore its mount-time close.
		onOpenChange.mockClear();
		const row = within(panel()).getByRole('button', { name: '1 running: 1 agent. Open the list' });
		expect(row.textContent).toContain('1 task running');
		await fireEvent.click(row);
		// A page inside the same sheet: the panel stays open.
		expect(onOpenChange).not.toHaveBeenCalledWith(false);
		expect(screen.getByRole('dialog', { name: 'Running tasks' })).toBe(panel());
		expect(within(panel().querySelector<HTMLElement>('[data-page="tasks"]')!).getByRole('searchbox', { name: 'Search tasks' })).toBeTruthy();
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
		expect(cell(/^All chat controls$/)).toBeTruthy();
	});

	it('keeps the row with no live watch while a worker is chosen for new ones (#565)', async () => {
		const get = api.get.bind(api);
		vi.spyOn(api, 'get').mockImplementation(((path: string, options?: unknown) =>
			path === '/watches'
				? Promise.resolve({ watches: [], preferred_worker_id: 'dev_laptop' })
				: get(path, options as never)) as typeof api.get);
		activeSession.setDraft('');
		render(MobileComposerControlsSheet, { props: props({ open: true }) });
		expect(await within(panel()).findByRole('button', { name: 'Watches: Where new watches run. Open the list' })).toBeTruthy();
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

	it('draws Bypass in the destructive colour: value, selected segment and help line', () => {
		render(MobileComposerControlsSheet, { props: props({ open: true, permissionValue: 'bypassPermissions' }) });
		const access = within(panel()).getByRole('radiogroup', { name: 'Tool access' });
		const bypass = within(access).getByRole('radio', { name: 'Bypass' });
		expect(bypass.getAttribute('aria-checked')).toBe('true');
		expect(bypass.classList.contains('danger')).toBe(true);
		const stack = access.closest('.mcp-stack')!;
		expect(stack.querySelector('.mcp-val')!.classList.contains('danger')).toBe(true);
		expect(stack.querySelector('.access-help')!.classList.contains('danger')).toBe(true);
	});

	it('slides a page in, and Back, Escape and a header drag all leave it', async () => {
		const onOpenChange = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ open: true, onOpenChange }) });
		// The stubbed router never records the panel's history entry; ignore its mount-time close.
		onOpenChange.mockClear();
		const usage = within(panel()).getByRole('button', { name: /^Context and usage/ });
		await fireEvent.click(usage);
		expect(panel().getAttribute('aria-label')).toBe('Context and usage');
		expect(panel().querySelector('[data-page="usage"]')!.classList.contains('active')).toBe(true);
		expect(panel().querySelector('[data-page="main"]')!.classList.contains('under')).toBe(true);
		// Back returns to the list, focused on the row that opened the page.
		await fireEvent.click(within(panel()).getByRole('button', { name: 'Back to chat controls' }));
		expect(panel().getAttribute('aria-label')).toBe('Chat controls');
		await waitFor(() => expect(document.activeElement).toBe(usage));
		// Escape on a page goes back first, then closes.
		await fireEvent.click(usage);
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(panel().getAttribute('aria-label')).toBe('Chat controls');
		expect(onOpenChange).not.toHaveBeenCalledWith(false);
		// Dragging the header down closes the whole sheet from a page.
		await fireEvent.click(usage);
		Object.defineProperty(panel(), 'offsetHeight', { configurable: true, value: 700 });
		await drag(document.querySelector('.mcp-head')!, 0, 400, { x: 200, y: 120 });
		expect(onOpenChange).toHaveBeenLastCalledWith(false);
	});

	it('returns from a page to the list on Android Back, keeping the panel open', async () => {
		const onOpenChange = vi.fn();
		page.state = { mobileControls: true };
		render(MobileComposerControlsSheet, { props: props({ open: true, onOpenChange }) });
		onOpenChange.mockClear();
		await fireEvent.click(within(panel()).getByRole('button', { name: /^Context and usage/ }));
		expect(panel().getAttribute('aria-label')).toBe('Context and usage');
		// The page pushed its own entry over the panel's; Back pops just that one.
		page.state = { mobileControls: true, mobileControlsPage: 'usage' };
		await tick();
		page.state = { mobileControls: true };
		await waitFor(() => expect(panel().getAttribute('aria-label')).toBe('Chat controls'));
		expect(onOpenChange).not.toHaveBeenCalledWith(false);
	});

	it('opens Export as a page with its button pinned under the choices', async () => {
		Object.assign(activeSession.current, { sessionId: 'chat-1', messages: [{ role: 'user', content: 'hi' }] });
		activeSession.setDraft('');
		render(MobileComposerControlsSheet, { props: props({ open: true }) });
		await fireEvent.click(within(within(panel()).getByRole('group', { name: 'More actions' })).getByRole('button', { name: 'Export chat' }));
		expect(panel().getAttribute('aria-label')).toBe('Export chat');
		const page = panel().querySelector<HTMLElement>('[data-page="export"]')!;
		const choices = within(page).getByRole('radiogroup', { name: 'Export format' });
		expect(within(choices).getByRole('radio', { name: /Export for sharing/ }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(within(choices).getByRole('radio', { name: /Private backup/ }));
		const primary = within(page).getByRole('button', { name: 'Download private backup' });
		expect(primary.closest('.page-foot')).toBeTruthy();
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
		expect(within(dialog).getByRole('heading', { name: 'Model' })).toBeTruthy();
		expect(within(dialog).getByRole('heading', { name: 'Permissions' })).toBeTruthy();
		expect(within(dialog).queryByRole('heading', { name: 'Setup' })).toBeNull();
	});
});
