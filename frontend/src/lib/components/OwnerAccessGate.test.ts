import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import type { OwnerStatus } from '$lib/api/owner';

const owner = vi.hoisted(() => ({ createOwnerPassword: vi.fn(), ownerLogin: vi.fn() }));
vi.mock('$lib/api/owner', () => owner);

import OwnerAccessGate from './OwnerAccessGate.svelte';

const base: OwnerStatus = {
	password_set: true, setup_allowed: false, required: false, trusted_lan: false,
	login_available: true, desktop_handoff: false, min_length: 12,
};

afterEach(() => {
	cleanup();
	owner.createOwnerPassword.mockReset();
	owner.ownerLogin.mockReset();
});

describe('OwnerAccessGate sign-in', () => {
	it('shows a rejected password, keeps the gate closed, and signs in on the next try', async () => {
		const onDone = vi.fn();
		owner.ownerLogin.mockRejectedValueOnce({ status: 401, detail: 'Wrong password' });
		const view = render(OwnerAccessGate, { status: base, authenticated: false, onDone, onRetry: vi.fn() });
		const input = view.getByLabelText('Owner password') as HTMLInputElement;
		const submit = view.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement;
		expect(submit.disabled).toBe(true); // nothing typed yet

		await fireEvent.input(input, { target: { value: 'not-it' } });
		await fireEvent.submit(input.form!);
		await waitFor(() => expect(view.getByRole('alert').textContent).toBe('Wrong password'));
		expect(onDone).not.toHaveBeenCalled();
		expect(submit.disabled).toBe(false);

		owner.ownerLogin.mockResolvedValueOnce({ status: 'ok' });
		await fireEvent.input(input, { target: { value: 'the-real-one' } });
		await fireEvent.submit(input.form!);
		await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
		expect(owner.ownerLogin).toHaveBeenLastCalledWith('the-real-one');
		expect(input.value).toBe('');
		expect(view.queryByRole('alert')).toBeNull();
	});
});

describe('OwnerAccessGate password creation', () => {
	it('checks length and confirmation locally before saving, honouring the worker minimum', async () => {
		const onDone = vi.fn();
		owner.createOwnerPassword.mockResolvedValueOnce({ status: 'ok' });
		const status = { ...base, password_set: false, setup_allowed: true, required: true, login_available: false, min_length: 16 };
		const view = render(OwnerAccessGate, { status, authenticated: false, onDone, onRetry: vi.fn() });
		expect(view.getByRole('heading', { name: 'Set an owner password' })).toBeTruthy();
		const pw = view.getByLabelText(/New password \(at least 16 characters\)/) as HTMLInputElement;
		const confirm = view.getByLabelText('Repeat it') as HTMLInputElement;

		await fireEvent.input(pw, { target: { value: 'fifteen-chars!!' } });
		await fireEvent.input(confirm, { target: { value: 'fifteen-chars!!' } });
		await fireEvent.submit(pw.form!);
		expect(view.getByRole('alert').textContent).toBe('Use at least 16 characters.');

		await fireEvent.input(pw, { target: { value: 'sixteen-chars!!!' } });
		await fireEvent.input(confirm, { target: { value: 'sixteen-chars!!?' } });
		await fireEvent.submit(pw.form!);
		expect(view.getByRole('alert').textContent).toBe('The two passwords are different.');
		expect(owner.createOwnerPassword).not.toHaveBeenCalled();

		await fireEvent.input(confirm, { target: { value: 'sixteen-chars!!!' } });
		await fireEvent.submit(pw.form!);
		await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
		expect(owner.createOwnerPassword).toHaveBeenCalledExactlyOnceWith('sixteen-chars!!!');
		expect(owner.ownerLogin).not.toHaveBeenCalled();
	});

	it('offers no form to a browser that can neither create nor sign in, only a retry', async () => {
		const onRetry = vi.fn();
		const status = { ...base, login_available: false };
		const view = render(OwnerAccessGate, { status, authenticated: false, onDone: vi.fn(), onRetry });
		expect(view.container.querySelector('form')).toBeNull();
		expect(view.getByRole('status').textContent).toMatch(/cannot sign in/);
		await fireEvent.click(view.getByRole('button', { name: 'Try again' }));
		expect(onRetry).toHaveBeenCalledOnce();
	});
});
