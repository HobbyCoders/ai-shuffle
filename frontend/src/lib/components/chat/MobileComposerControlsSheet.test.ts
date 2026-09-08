import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import { platform } from '$lib/stores/platform.svelte';
import '../../test-stubs/composer-dom';

afterEach(() => { cleanup(); vi.restoreAllMocks(); platform.set('desktop'); });
afterAll(() => vi.unstubAllGlobals());
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});

it('explains no adjustable effort and keeps fast mode available', async () => {
	const onToggleFast = vi.fn();
	render(MobileComposerControlsSheet, { props: props({ effortOptions: [], effortValue: null, fastSupported: true, onToggleFast }) });
	expect(screen.getByText('Adjustable reasoning effort is not available for this model in AI Shuffle.')).toBeTruthy();
	expect(document.querySelectorAll('.effort-choices button')).toHaveLength(0);
	await fireEvent.click(screen.getByRole('switch', { name: 'Fast mode' }));
	expect(onToggleFast).toHaveBeenCalledOnce();
});
it('reserves a hero window, falling back to a usable normal-flow panel on short empty surfaces', async () => {
	platform.set('mobile');
	const surface = document.createElement('div');
	surface.className = 'chat-view is-empty';
	const setHeight = (value: number) => Object.defineProperty(surface, 'clientHeight', { configurable: true, value });
	setHeight(421);
	const slot = document.createElement('div'); slot.className = 'input-slot';
	surface.append(slot); document.body.append(surface);
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return { height: this === slot ? 346 : this.classList.contains('panel-reveal') ? 220 : 0 } as DOMRect;
	});
	const scrollIntoView = vi.fn();
	const { unmount, rerender } = render(MobileComposerControlsSheet, { target: slot, props: props() });
	const panel = screen.getByRole('region', { name: 'Chat controls' });
	panel.scrollIntoView = scrollIntoView;
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('199px');
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('199px');
	// 126px editor + 194px panel used to cover this entire 320px surface.
	setHeight(320);
	await fireEvent(window, new Event('resize'));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('194px');
	expect(slot.hasAttribute('data-panel-flow')).toBe(true);
	await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' }));
	scrollIntoView.mockClear();
	await fireEvent.focusIn(panel);
	await waitFor(() => expect(scrollIntoView).toHaveBeenCalledOnce());
	setHeight(200);
	await fireEvent(window, new Event('resize'));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('160px');
	expect(slot.hasAttribute('data-panel-flow')).toBe(true);
	// Exactly 96px of hero + the minimum 160px panel still fits sticky.
	setHeight(382);
	await fireEvent(window, new Event('resize'));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('160px');
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	setHeight(381);
	await fireEvent(window, new Event('resize'));
	expect(slot.hasAttribute('data-panel-flow')).toBe(true);
	await rerender(props({ open: false }));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('');
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	await rerender(props());
	expect(slot.hasAttribute('data-panel-flow')).toBe(true);
	platform.set('desktop');
	await fireEvent(window, new Event('resize'));
	expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe('');
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	platform.set('mobile');
	await fireEvent(window, new Event('resize'));
	expect(slot.hasAttribute('data-panel-flow')).toBe(true);
	surface.classList.remove('is-empty');
	await waitFor(() => expect(panel.style.getPropertyValue('--empty-panel-limit')).toBe(''));
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	surface.classList.add('is-empty');
	await waitFor(() => expect(slot.hasAttribute('data-panel-flow')).toBe(true));
	unmount();
	expect(slot.hasAttribute('data-panel-flow')).toBe(false);
	surface.remove();
});
it('supports a single effort option', async () => {
	const onEffortSelect = vi.fn();
	render(MobileComposerControlsSheet, { props: props({ effortOptions: [{ value: 'high', label: 'High', description: 'Thorough' }], onEffortSelect }) });
	await fireEvent.click(screen.getByRole('button', { name: 'High' }));
	expect(onEffortSelect).toHaveBeenCalledWith('high');
});

function props(overrides: Record<string, unknown> = {}) {
	return {
		open: true,
		onClose: vi.fn(),
		modelOptions: [
			{
				value: 'sonnet',
				label: 'Sonnet',
				description: 'Balanced speed and depth',
				group: 'Claude',
				groupKey: 'claude',
				groupSubtext: 'Pro'
			},
			{
				value: 'opus',
				label: 'Opus',
				description: 'Deep reasoning',
				group: 'Claude',
				groupKey: 'claude',
				groupSubtext: 'Pro'
			}
		],
		modelValue: 'sonnet',
		modelLabel: 'Sonnet',
		modelDefaultValue: 'sonnet',
		modelOverridden: false,
		onModelSelect: vi.fn(),
		contextChoice: '1m' as const,
		onContextChoiceSelect: vi.fn(),
		effortOptions: [
			{ value: 'low', label: 'Low', description: 'Quick' },
			{ value: 'high', label: 'High', description: 'Thorough' }
		],
		effortValue: 'low',
		effortDefaultValue: 'low',
		effortOverridden: false,
		onEffortSelect: vi.fn(),
		permissionOptions: [
			{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
			{ value: 'auto', label: 'Auto', description: 'Model decides per tool call' }
		],
		permissionValue: 'default',
		permissionDefaultValue: 'default',
		permissionOverridden: false,
		onPermissionSelect: vi.fn(),
		...overrides
	};
}

describe('integrated mobile composer controls', () => {
	it.each(['overview', 'models'])('keeps upward and downward swipes on the %s drawer native', async page => {
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onClose }) });
		if (page === 'models') await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
		const panel = screen.getByRole('region', { name: page === 'models' ? 'Choose model' : 'Chat controls' });
		const target = page === 'models' ? screen.getByRole('option', { name: /sonnet/i }) : panel;
		for (const dy of [-90, 90]) {
			for (const [type, y] of [['touchstart', 100], ['touchmove', 100 + dy], ['touchend', 100 + dy]] as const) {
				const point = { identifier: 1, clientX: 100, clientY: y };
				const event = new Event(type, { bubbles: true, cancelable: true });
				Object.assign(event, { touches: type === 'touchend' ? [] : [point], changedTouches: [point] });
				await fireEvent(target, event);
				expect(event.defaultPrevented).toBe(false);
			}
			expect(onClose).not.toHaveBeenCalled();
		}
	});
	it('renders in place without a modal, backdrop, scroll lock, or focus theft', async () => {
		const editor = document.createElement('textarea');
		document.body.append(editor);
		editor.focus();
		const { container } = render(MobileComposerControlsSheet, { props: props() });
		await Promise.resolve();
		expect(container.contains(screen.getByRole('region', { name: 'Chat controls' }))).toBe(true);
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.body.style.overflow).not.toBe('hidden');
		expect(document.activeElement).toBe(editor);
		expect(screen.queryByText('Close toolbar')).toBeNull();
		editor.remove();
	});

	it('hides the handle completely when closed and keeps controls inert', () => {
		const { container } = render(MobileComposerControlsSheet, { props: props({ open: false }) });
		expect(container.querySelector('.panel-handle')).toBeNull();
		expect((container.querySelector('.panel-reveal') as HTMLElement).inert).toBe(true);
		expect(screen.queryByRole('button', { name: 'High' })).toBeNull();
	});

	it('has no visible handle when open and guards the expanding tap', () => {
		const { container } = render(MobileComposerControlsSheet, { props: props({ inertToolbar: true }) });
		expect(container.querySelector('.panel-handle')).toBeNull();
		expect((container.querySelector('.panel-reveal') as HTMLElement).inert).toBe(true);
	});
	it('shows applicable session context directly without a setup page or native selects', () => {
		const { container } = render(MobileComposerControlsSheet, { props: props() });
		expect(screen.getByRole('toolbar', { name: 'Session context' })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^Agent:/ })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Chat setup' })).toBeNull();
		expect(container.querySelector('select')).toBeNull();
	});

	it('drops setup and tool access outside the code lane', () => {
		render(MobileComposerControlsSheet, { props: props({ showCodeControls: false }) });
		expect(screen.queryByRole('button', { name: 'Chat setup' })).toBeNull();
		expect(screen.queryByRole('group', { name: 'Tool access' })).toBeNull();
		expect(screen.getByRole('button', { name: /model: sonnet/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'High' })).toBeTruthy();
	});

	it('uses the shared searchable model picker and returns to the controls after selection', async () => {
		const onModelSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onModelSelect }) });
		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
		expect(screen.getByRole('region', { name: 'Choose model' })).toBeTruthy();
		expect(document.querySelector('[data-model-picker-panel]')).toBeTruthy();
		await fireEvent.input(screen.getByRole('textbox', { name: /search models/i }), { target: { value: 'opus' } });
		expect(screen.queryByRole('option', { name: /sonnet/i })).toBeNull();
		await fireEvent.click(screen.getByRole('option', { name: /opus/i }));
		expect(onModelSelect).toHaveBeenCalledWith('opus');
		expect(screen.getByRole('region', { name: 'Chat controls' })).toBeTruthy();
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /model: sonnet/i })));
	});

	it('does not summon the keyboard when the picker opens and preserves context selection', async () => {
		const onContextChoiceSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onContextChoiceSelect }) });
		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('region', { name: 'Choose model' })));
		expect(screen.getByRole('radio', { name: /1m.*extended/i }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(screen.getByRole('radio', { name: /200k.*standard/i }));
		expect(onContextChoiceSelect).toHaveBeenCalledWith('standard');
	});

	it('applies effort and access directly without an expanding menu or closing the panel', async () => {
		const onEffortSelect = vi.fn(), onPermissionSelect = vi.fn(), onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onEffortSelect, onPermissionSelect, onClose }) });
		await fireEvent.click(screen.getByRole('button', { name: 'High' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Auto: Model decides per tool call' }));
		expect(onEffortSelect).toHaveBeenCalledWith('high');
		expect(onPermissionSelect).toHaveBeenCalledWith('auto');
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.queryByRole('listbox')).toBeNull();
	});

	it('exposes supported fast mode as a switch with its cost/cache explanation', async () => {
		const onToggleFast = vi.fn(), onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ fastSupported: true, fastEnabled: true, onToggleFast, onClose }) });
		const toggle = screen.getByRole('switch', { name: 'Fast mode' });
		expect(toggle.getAttribute('aria-checked')).toBe('true');
		expect(document.getElementById(toggle.getAttribute('aria-describedby')!)?.textContent).toMatch(/price.*prompt cache/i);
		await fireEvent.click(toggle);
		expect(onToggleFast).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
	});

	it('hides fast mode for unsupported models', () => {
		render(MobileComposerControlsSheet, { props: props() });
		expect(screen.queryByRole('switch', { name: 'Fast mode' })).toBeNull();
	});

	it('scopes Escape to the panel, goes back first, then requests closure', async () => {
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onClose }) });
		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.getByRole('region', { name: 'Choose model' })).toBeTruthy();
		await fireEvent.keyDown(screen.getByRole('region', { name: 'Choose model' }), { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
		const model = screen.getByRole('button', { name: /model: sonnet/i });
		model.focus();
		await fireEvent.keyDown(model, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);
		expect(document.activeElement).toBe(model);
	});

	it('does not trap Tab or consume Escape already handled by a child', async () => {
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onClose }) });
		const handle = screen.getByRole('region', { name: 'Chat controls' });
		const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
		await fireEvent(handle, tab);
		expect(tab.defaultPrevented).toBe(false);
		const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		escape.preventDefault();
		await fireEvent(handle, escape);
		expect(onClose).not.toHaveBeenCalled();
	});

});
