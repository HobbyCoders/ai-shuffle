import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';

afterEach(() => cleanup());

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
		computerUseEnabled: true,
		computerUseStopped: true,
		computerUseActive: false,
		computerUseBusy: false,
		onToggleComputerUse: vi.fn(),
		...overrides
	};
}

describe('MobileComposerControlsSheet', () => {
	it('portals one dialog to body and groups secondary composer controls', () => {
		render(MobileComposerControlsSheet, { props: props() });

		const dialog = screen.getByRole('dialog', { name: /chat controls/i });
		expect(dialog.parentElement).toBe(document.body.querySelector('.sheet-layer'));
		expect(screen.getByText('Reasoning effort')).toBeTruthy();
		expect(screen.getByText('Tool access')).toBeTruthy();
		expect(screen.getByRole('switch', { name: /computer control/i })).toBeTruthy();
	});

	it('opens the model subpage, filters models, and applies a selection', async () => {
		const onModelSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onModelSelect }) });

		await fireEvent.click(screen.getByRole('button', { name: /sonnet.*model used/i }));
		expect(screen.getByRole('dialog', { name: /choose model/i })).toBeTruthy();

		await fireEvent.input(screen.getByRole('textbox', { name: /search models/i }), {
			target: { value: 'opus' }
		});
		expect(screen.queryByRole('option', { name: /sonnet/i })).toBeNull();
		await fireEvent.click(screen.getByRole('option', { name: /opus/i }));
		expect(onModelSelect).toHaveBeenCalledWith('opus');
	});

	it('does not focus model search when the model subpage opens', async () => {
		render(MobileComposerControlsSheet, { props: props() });

		await fireEvent.click(screen.getByRole('button', { name: /sonnet.*model used/i }));
		const dialog = screen.getByRole('dialog', { name: /choose model/i });
		await waitFor(() => expect(document.activeElement).toBe(dialog));
		expect(screen.getByRole('textbox', { name: /search models/i })).not.toBe(
			document.activeElement
		);
	});

	it('reuses the shared picker and exposes its fixed context choice', async () => {
		const onContextChoiceSelect = vi.fn();
		render(MobileComposerControlsSheet, {
			props: props({ onContextChoiceSelect })
		});

		await fireEvent.click(screen.getByRole('button', { name: /sonnet.*model used/i }));

		expect(document.querySelector('[data-model-picker-panel]')).toBeTruthy();
		const context = screen.getByRole('combobox', { name: /context window/i }) as HTMLSelectElement;
		expect(context.value).toBe('1m');
		await fireEvent.change(context, { target: { value: 'standard' } });
		expect(onContextChoiceSelect).toHaveBeenCalledWith('standard');
	});

	it('applies effort, access, and computer-control changes without closing the sheet', async () => {
		const onEffortSelect = vi.fn();
		const onPermissionSelect = vi.fn();
		const onToggleComputerUse = vi.fn();
		render(MobileComposerControlsSheet, {
			props: props({ onEffortSelect, onPermissionSelect, onToggleComputerUse })
		});

		await fireEvent.input(screen.getByRole('slider', { name: /reasoning effort/i }), {
			target: { value: '1' }
		});
		await fireEvent.click(screen.getByRole('button', { name: /auto.*model decides/i }));
		await fireEvent.click(screen.getByRole('switch', { name: /computer control/i }));

		expect(onEffortSelect).toHaveBeenCalledWith('high');
		expect(onPermissionSelect).toHaveBeenCalledWith('auto');
		expect(onToggleComputerUse).toHaveBeenCalledTimes(1);
		expect(screen.getByRole('dialog')).toBeTruthy();
	});

	it('uses a slider for reasoning and has no attach or explicit close action', () => {
		render(MobileComposerControlsSheet, { props: props() });

		expect(screen.getByRole('slider', { name: /reasoning effort/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /attach/i })).toBeNull();
		expect(document.querySelector('.sheet-header [aria-label="Close chat controls"]')).toBeNull();
		expect(screen.queryByRole('button', { name: /done/i })).toBeNull();
	});

	it('shows the fast-mode toggle in the effort card when the model supports it', () => {
		render(MobileComposerControlsSheet, { props: props({ fastSupported: true, fastEnabled: true }) });

		const toggle = screen.getByRole('button', { name: 'Fast mode' });
		expect(toggle.getAttribute('aria-pressed')).toBe('true');
		expect(toggle.getAttribute('title')).toMatch(/clears the prompt cache/i);
		expect(toggle.closest('.effort-slider-card')).toBeTruthy();
	});

	it('hides the fast-mode toggle for a model without the tier', () => {
		render(MobileComposerControlsSheet, { props: props({ fastSupported: false }) });

		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
	});

	it('flips fast mode without closing the sheet', async () => {
		const onToggleFast = vi.fn();
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, {
			props: props({ fastSupported: true, fastEnabled: false, onToggleFast, onClose })
		});

		await fireEvent.click(screen.getByRole('button', { name: 'Fast mode' }));

		expect(onToggleFast).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog')).toBeTruthy();
	});

	it('uses Escape to go back from models before dismissing the sheet', async () => {
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('button', { name: /sonnet.*model used/i }));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.getByText('Reasoning effort')).toBeTruthy();

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);
	});
});
