import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Switch from './Switch.svelte';
import SettingRowHarness from '$lib/test-stubs/SettingRowHarness.svelte';

afterEach(cleanup);

describe('Switch', () => {
	it('is a labelled role=switch that toggles aria-checked and reports the new value', async () => {
		const onchange = vi.fn();
		render(Switch, { checked: false, onchange, label: 'Sound cues' });
		const sw = screen.getByRole('switch', { name: 'Sound cues' });
		expect(sw.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(sw);
		expect(sw.getAttribute('aria-checked')).toBe('true');
		expect(onchange).toHaveBeenLastCalledWith(true);
		await fireEvent.click(sw);
		expect(onchange).toHaveBeenLastCalledWith(false);
	});

	it('does nothing while disabled', async () => {
		const onchange = vi.fn();
		render(Switch, { checked: true, onchange, disabled: true, label: 'Cues' });
		const sw = screen.getByRole('switch', { name: 'Cues' }) as HTMLButtonElement;
		expect(sw.disabled).toBe(true);
		await fireEvent.click(sw);
		expect(onchange).not.toHaveBeenCalled();
		expect(sw.getAttribute('aria-checked')).toBe('true');
	});

	it('has a small size', () => {
		render(Switch, { checked: false, size: 'sm', label: 'On' });
		expect(screen.getByRole('switch').classList.contains('sm')).toBe(true);
	});

	it('takes its name and description from the row it sits in', () => {
		render(SettingRowHarness, { label: 'Group tool calls', hint: 'Fewer lines' });
		const sw = screen.getByRole('switch', { name: 'Group tool calls' });
		const described = sw.getAttribute('aria-describedby');
		expect(described).toBeTruthy();
		expect(document.getElementById(described!)?.textContent).toBe('Fewer lines');
	});

	it('is disabled when Settings is read-only', () => {
		render(SettingRowHarness, { label: 'Group tool calls', readOnly: true });
		expect((screen.getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
	});
});
