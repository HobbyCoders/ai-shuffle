import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import HookForm from './HookForm.svelte';
import type { Hook } from '$lib/api/client';

vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]) };
});
afterEach(cleanup);

const messageHook = {
	id: 'hook-m', name: 'Old message hook', event: 'Stop', hook_type: 'message',
	message_template: 'Summarize', enabled: true, timeout: 30000
} as Hook;

describe('HookForm hook types', () => {
	it('does not offer message hooks for new hooks', () => {
		render(HookForm, { onSave: vi.fn() });
		expect(screen.getByRole('button', { name: 'Bash' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Webhook' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Message' })).toBeNull();
	});

	it('shows an existing message hook as unsupported and will not save it as one', async () => {
		const onSave = vi.fn();
		const { component } = render(HookForm, { hook: messageHook, onSave });
		expect(screen.getByText(/Message hooks are not supported and never run/)).toBeTruthy();
		expect(component.getIsValid()).toBe(false);
		component.save();
		expect(onSave).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Bash' }));
		await fireEvent.input(screen.getByPlaceholderText(/curl -X POST/), { target: { value: 'echo hi' } });
		expect(component.getIsValid()).toBe(true);
		component.save();
		expect(onSave).toHaveBeenCalledOnce();
		expect(onSave.mock.calls[0][0]).toMatchObject({ hook_type: 'bash', command: 'echo hi' });
		expect(onSave.mock.calls[0][0].message_template).toBeUndefined();
	});
});
