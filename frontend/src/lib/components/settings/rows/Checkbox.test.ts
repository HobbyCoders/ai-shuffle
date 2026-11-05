import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Checkbox from './Checkbox.svelte';

afterEach(cleanup);

describe('Settings Checkbox', () => {
	it('keeps a native labelled checkbox and forwards change events', async () => {
		const onchange = vi.fn();
		render(Checkbox, { checked: false, 'aria-label': 'Read files', onchange });
		const control = screen.getByRole('checkbox', { name: 'Read files' }) as HTMLInputElement;
		expect(control.checked).toBe(false);
		await fireEvent.click(control);
		expect(control.checked).toBe(true);
		expect(onchange).toHaveBeenCalledOnce();
	});
	it('keeps initial selection, form name and disabled state', () => {
		render(Checkbox, { checked: true, disabled: true, name: 'files', 'aria-label': 'Files' });
		const control = screen.getByRole('checkbox', { name: 'Files' }) as HTMLInputElement;
		expect(control.checked).toBe(true);
		expect(control.disabled).toBe(true);
		expect(control.name).toBe('files');
	});
});
