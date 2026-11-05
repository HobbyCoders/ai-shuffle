import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import HookForm from './HookForm.svelte';
import type { Hook } from '$lib/api/hooks';

vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]) };
});
afterEach(cleanup);

describe('HookForm hook types', () => {
	it('does not hydrate a signing secret, including legacy responses', () => {
		const hook = { name: 'Notify', event: 'Stop', hook_type: 'webhook', url: 'https://example.test', has_secret: true, secret: 'dummy-legacy-secret' } as unknown as Hook;
		const onSave = vi.fn();
		const { component } = render(HookForm, { hook, onSave });
		expect((screen.getByPlaceholderText('HMAC secret for signature') as HTMLInputElement).value).toBe('');
		component.save();
		expect(onSave.mock.calls[0][0].secret).toBeUndefined();
		component.refreshRemote(hook);
		expect((screen.getByPlaceholderText('HMAC secret for signature') as HTMLInputElement).value).toBe('');
	});

	it('offers exactly the bash and webhook types', () => {
		render(HookForm, { onSave: vi.fn() });
		expect(screen.getByRole('button', { name: 'Bash' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Webhook' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Message' })).toBeNull();
	});

	it('saves a bash hook with only the fields of its type', async () => {
		const onSave = vi.fn();
		const { component } = render(HookForm, { onSave });
		expect(component.getIsValid()).toBe(false);

		await fireEvent.input(screen.getByPlaceholderText(/Slack Notification/), { target: { value: 'Notify' } });
		await fireEvent.input(screen.getByPlaceholderText(/curl -X POST/), { target: { value: 'echo hi' } });
		expect(component.getIsValid()).toBe(true);
		component.save();
		expect(onSave).toHaveBeenCalledOnce();
		expect(onSave.mock.calls[0][0]).toMatchObject({ hook_type: 'bash', command: 'echo hi' });
		expect(onSave.mock.calls[0][0].url).toBeUndefined();
	});
});
