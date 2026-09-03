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
		...overrides
	};
}

describe('MobileComposerControlsSheet', () => {
	it('portals one dialog to body and groups secondary composer controls', () => {
		render(MobileComposerControlsSheet, { props: props() });

		const dialog = screen.getByRole('dialog', { name: /chat controls/i });
		expect(dialog.parentElement).toBe(document.body.querySelector('.sheet-layer'));
		expect(screen.getByText('Chat setup')).toBeTruthy();
		expect(screen.getByRole('button', { name: /reasoning effort/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /tool access/i })).toBeTruthy();
		// Computer use is agent configuration now: no control for it here.
		expect(screen.queryByRole('switch', { name: /computer control/i })).toBeNull();
	});

	it('drops chat setup and tool access outside the code lane', () => {
		// A chat/voice composer has no project to configure and a fixed web-only
		// tool surface, so both sections go rather than sit empty under a heading.
		render(MobileComposerControlsSheet, { props: props({ showCodeControls: false }) });

		expect(screen.queryByText('Chat setup')).toBeNull();
		expect(screen.queryByRole('button', { name: /tool access/i })).toBeNull();
		expect(screen.getByRole('button', { name: /model: sonnet/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /reasoning effort/i })).toBeTruthy();
	});

	it('opens the model subpage, filters models, and applies a selection', async () => {
		const onModelSelect = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onModelSelect }) });

		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
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

		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
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

		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));

		expect(document.querySelector('[data-model-picker-panel]')).toBeTruthy();
		expect(screen.getByRole('radiogroup', { name: /context window/i })).toBeTruthy();
		expect(screen.getByRole('radio', { name: /1m.*extended/i }).getAttribute('aria-checked')).toBe(
			'true'
		);
		await fireEvent.click(screen.getByRole('radio', { name: /200k.*normal/i }));
		expect(onContextChoiceSelect).toHaveBeenCalledWith('standard');
	});

	it('applies effort and access changes without closing the sheet', async () => {
		const onEffortSelect = vi.fn();
		const onPermissionSelect = vi.fn();
		render(MobileComposerControlsSheet, {
			props: props({ onEffortSelect, onPermissionSelect })
		});

		await fireEvent.click(screen.getByRole('button', { name: /reasoning effort/i }));
		await fireEvent.click(screen.getByRole('option', { name: /high/i }));
		await fireEvent.click(screen.getByRole('button', { name: /tool access/i }));
		await fireEvent.click(screen.getByRole('option', { name: /auto.*model decides/i }));

		expect(onEffortSelect).toHaveBeenCalledWith('high');
		expect(onPermissionSelect).toHaveBeenCalledWith('auto');
		expect(screen.getByRole('dialog')).toBeTruthy();
	});

	it('uses compact dropdowns for reasoning and has no attach or explicit close action', () => {
		render(MobileComposerControlsSheet, { props: props() });

		expect(screen.getByRole('button', { name: /reasoning effort/i })).toBeTruthy();
		expect(screen.queryByRole('slider', { name: /reasoning effort/i })).toBeNull();
		expect(screen.queryByRole('button', { name: /attach/i })).toBeNull();
		expect(document.querySelector('.sheet-header [aria-label="Close chat controls"]')).toBeNull();
		expect(screen.queryByRole('button', { name: /done/i })).toBeNull();
	});

	it('shows the fast-mode checkbox when the model supports it', () => {
		render(MobileComposerControlsSheet, { props: props({ fastSupported: true, fastEnabled: true }) });

		const toggle = screen.getByRole('checkbox', { name: /fast mode/i }) as HTMLInputElement;
		expect(toggle.checked).toBe(true);
		expect(screen.getByText(/clears the prompt cache/i)).toBeTruthy();
	});

	it('hides the fast-mode checkbox for a model without the tier', () => {
		render(MobileComposerControlsSheet, { props: props({ fastSupported: false }) });

		expect(screen.queryByRole('checkbox', { name: /fast mode/i })).toBeNull();
	});

	it('flips fast mode without closing the sheet', async () => {
		const onToggleFast = vi.fn();
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, {
			props: props({ fastSupported: true, fastEnabled: false, onToggleFast, onClose })
		});

		await fireEvent.click(screen.getByRole('checkbox', { name: /fast mode/i }));

		expect(onToggleFast).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog')).toBeTruthy();
	});

	it('uses Escape to go back from models before dismissing the sheet', async () => {
		const onClose = vi.fn();
		render(MobileComposerControlsSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('button', { name: /model: sonnet/i }));
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
		expect(screen.getByRole('button', { name: /reasoning effort/i })).toBeTruthy();

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);
	});
});
