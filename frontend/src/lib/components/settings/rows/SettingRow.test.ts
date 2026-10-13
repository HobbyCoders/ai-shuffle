import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SettingRowHarness from '$lib/test-stubs/SettingRowHarness.svelte';

afterEach(cleanup);

describe('SettingRow', () => {
	it('takes label and default from the registry and renders its anchor', () => {
		const { container } = render(SettingRowHarness, { id: 'general.timezone', value: 'local' });
		const row = container.querySelector('[data-setting-anchor="general.timezone"]');
		expect(row).not.toBeNull();
		expect(row?.textContent).toContain('Time zone');
		expect(row?.classList.contains('mod')).toBe(false);
		expect(screen.queryByRole('button', { name: /reset/i })).toBeNull();
	});

	it('marks a changed value and resets it', async () => {
		const onReset = vi.fn();
		const { container } = render(SettingRowHarness, { id: 'general.timezone', value: 'UTC', onReset });
		const row = container.querySelector('[data-setting-anchor="general.timezone"]')!;
		expect(row.classList.contains('mod')).toBe(true);
		expect(row.getAttribute('title')).toBe('Changed from default');
		await fireEvent.click(screen.getByRole('button', { name: 'Reset Time zone to default' }));
		expect(onReset).toHaveBeenCalledOnce();
	});

	it('compares structured values and honours an explicit default', () => {
		const { container } = render(SettingRowHarness, {
			label: 'Agents',
			value: ['a', 'b'],
			defaultValue: ['a', 'b'],
			onReset: () => {}
		});
		expect(container.querySelector('.st-row')?.classList.contains('mod')).toBe(false);
	});

	it('lets props override registry copy and shows a help tip', async () => {
		render(SettingRowHarness, { id: 'general.theme', hint: 'Follows the system', help: 'Longer detail here.' });
		expect(screen.getByText('Theme')).toBeTruthy();
		expect(screen.getByText('Follows the system')).toBeTruthy();
		const help = screen.getByRole('button', { name: 'About Theme' });
		await fireEvent.focus(help);
		const tip = await screen.findByRole('tooltip');
		expect(tip.textContent).toBe('Longer detail here.');
		expect(help.getAttribute('aria-describedby')).toBe(tip.id);
		await fireEvent.keyDown(help, { key: 'Escape' });
		expect(screen.queryByRole('tooltip')).toBeNull();
	});

	it('works without an id (per-item rows) and has no anchor', () => {
		const { container } = render(SettingRowHarness, { label: 'Scheduled wake-up' });
		expect(screen.getByText('Scheduled wake-up')).toBeTruthy();
		expect(container.querySelector('[data-setting-anchor]')).toBeNull();
	});

	it('hides reset and disables the expansion when read-only', () => {
		const { container } = render(SettingRowHarness, {
			id: 'general.timezone',
			value: 'UTC',
			onReset: () => {},
			readOnly: true,
			expandedText: 'Editor'
		});
		expect(screen.queryByRole('button', { name: /reset/i })).toBeNull();
		expect(container.querySelector('fieldset')?.disabled).toBe(true);
		expect(screen.getByText('Editor')).toBeTruthy();
	});
});
