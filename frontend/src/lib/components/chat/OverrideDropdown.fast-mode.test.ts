/**
 * The fast-tier toggle the effort dropdown carries in its popover header.
 *
 * It is deliberately NOT a menu item: flipping the service tier must leave the
 * popover open so the user can keep tuning the run binding.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import OverrideDropdown from './OverrideDropdown.svelte';

const icon = createRawSnippet(() => ({ render: () => `<span>icon</span>` }));

afterEach(() => cleanup());

function props(overrides: Record<string, unknown> = {}) {
	return {
		modes: [
			{ value: 'low', label: 'Low', description: 'Quick' },
			{ value: 'high', label: 'High', description: 'Thorough' }
		],
		currentValue: 'low',
		onSelect: vi.fn(),
		icon,
		title: 'Effort Level',
		isOpen: true,
		onOpenChange: vi.fn(),
		...overrides
	};
}

describe('OverrideDropdown fast-mode toggle', () => {
	it('renders the toggle in the popover header when the model supports it', () => {
		render(OverrideDropdown, { props: props({ fastSupported: true, fastEnabled: false }) });

		const toggle = screen.getByRole('button', { name: 'Fast mode' });
		expect(toggle.getAttribute('aria-pressed')).toBe('false');
		expect(toggle.getAttribute('title')).toMatch(/clears the prompt cache/i);
		// Top-right of the header, not a row in the list.
		expect(toggle.closest('.override-dropdown-header')).toBeTruthy();
	});

	it('reflects the enabled state through aria-pressed', () => {
		render(OverrideDropdown, { props: props({ fastSupported: true, fastEnabled: true }) });

		expect(screen.getByRole('button', { name: 'Fast mode' }).getAttribute('aria-pressed')).toBe(
			'true'
		);
	});

	it('is absent when the model does not support the tier', () => {
		render(OverrideDropdown, { props: props({ fastSupported: false }) });

		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
	});

	it('is absent while the dropdown is closed', () => {
		render(OverrideDropdown, { props: props({ isOpen: false, fastSupported: true }) });

		expect(screen.queryByRole('button', { name: 'Fast mode' })).toBeNull();
	});

	it('toggles without closing the dropdown or selecting a mode', async () => {
		const onToggleFast = vi.fn();
		const onOpenChange = vi.fn();
		const onSelect = vi.fn();
		render(OverrideDropdown, {
			props: props({ fastSupported: true, onToggleFast, onOpenChange, onSelect })
		});

		await fireEvent.click(screen.getByRole('button', { name: 'Fast mode' }));

		expect(onToggleFast).toHaveBeenCalledTimes(1);
		expect(onOpenChange).not.toHaveBeenCalled();
		expect(onSelect).not.toHaveBeenCalled();
		expect(screen.getByRole('menu', { name: 'Effort Level' })).toBeTruthy();
	});
});
