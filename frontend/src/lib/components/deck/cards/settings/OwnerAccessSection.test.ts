import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { OwnerStatus } from '$lib/api/owner';

const owner = vi.hoisted(() => ({
	fetchOwnerStatus: vi.fn(), changeOwnerPassword: vi.fn(), createOwnerPassword: vi.fn(), setTrustedLan: vi.fn()
}));
vi.mock('$lib/api/owner', () => owner);
const refresh = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/owner-access.svelte', () => ({ ownerAccess: { refresh } }));
vi.mock('$lib/api/client', () => ({ getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback }));
import OwnerAccessSection from './OwnerAccessSection.svelte';

const status = (over: Partial<OwnerStatus> = {}): OwnerStatus => ({
	password_set: true, setup_allowed: false, required: false, trusted_lan: false,
	login_available: true, desktop_handoff: false, min_length: 12, ...over
});
const loaded = (over: Partial<OwnerStatus> = {}) => ({ ok: true as const, status: status(over) });
const input = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
async function fill(values: Record<string, string>) {
	for (const [name, value] of Object.entries(values)) await fireEvent.input(input(name), { target: { value } });
	await fireEvent.submit(screen.getByRole('form', { name: 'Owner password' }));
}

beforeEach(() => {
	vi.clearAllMocks();
	owner.fetchOwnerStatus.mockResolvedValue(loaded());
	owner.changeOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.createOwnerPassword.mockResolvedValue({ status: 'ok' });
	owner.setTrustedLan.mockResolvedValue({ trusted_lan: true });
	refresh.mockResolvedValue(null);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('owner password change', () => {
	it('enforces the worker minimum and the confirmation before calling the API', async () => {
		render(OwnerAccessSection);
		expect(await screen.findByText('Change the owner password')).toBeTruthy();
		expect(input('New password').placeholder).toBe('New password (at least 12 characters)');
		await fill({ 'Current password': 'old-secret', 'New password': 'short', 'Repeat the new password': 'short' });
		expect(screen.getByRole('alert').textContent).toBe('Use at least 12 characters.');
		await fill({ 'New password': 'long-enough-pass', 'Repeat the new password': 'long-enough-typo' });
		expect(screen.getByRole('alert').textContent).toBe('The two new passwords are different.');
		expect(owner.changeOwnerPassword).not.toHaveBeenCalled();
	});

	it('changes the password with the current one, announces the sign-out of other browsers and clears the fields', async () => {
		render(OwnerAccessSection);
		await screen.findByText('Change the owner password');
		await fill({ 'Current password': 'old-secret', 'New password': 'long-enough-pass', 'Repeat the new password': 'long-enough-pass' });
		await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Password changed. Every other browser was signed out.'));
		expect(owner.changeOwnerPassword).toHaveBeenCalledWith('old-secret', 'long-enough-pass');
		expect(owner.createOwnerPassword).not.toHaveBeenCalled();
		expect(refresh).toHaveBeenCalledOnce();
		expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2);
		expect([input('Current password').value, input('New password').value, input('Repeat the new password').value]).toEqual(['', '', '']);
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('creates the first password without asking for a current one', async () => {
		owner.fetchOwnerStatus.mockResolvedValue(loaded({ password_set: false, min_length: 8 }));
		render(OwnerAccessSection);
		expect(await screen.findByText('Set an owner password')).toBeTruthy();
		expect(screen.queryByLabelText('Current password')).toBeNull();
		await fill({ 'New password': 'eight-ch', 'Repeat the new password': 'eight-ch' });
		await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Owner password set.'));
		expect(owner.createOwnerPassword).toHaveBeenCalledWith('eight-ch');
	});

	it('shows the worker rejection and keeps what was typed', async () => {
		owner.changeOwnerPassword.mockRejectedValueOnce({ detail: 'The current password is wrong.' });
		render(OwnerAccessSection);
		await screen.findByText('Change the owner password');
		await fill({ 'Current password': 'nope', 'New password': 'long-enough-pass', 'Repeat the new password': 'long-enough-pass' });
		expect((await screen.findByRole('alert')).textContent).toBe('The current password is wrong.');
		expect(screen.queryByRole('status')).toBeNull();
		expect(input('New password').value).toBe('long-enough-pass');
		expect(refresh).not.toHaveBeenCalled();
	});
});

describe('trusted LAN', () => {
	it('needs an explicit confirmation to turn on and reverts the box when saving fails', async () => {
		const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValue(true);
		vi.stubGlobal('confirm', confirm);
		render(OwnerAccessSection);
		const box = (await screen.findByRole('checkbox', { name: 'Trusted LAN, no login' })) as HTMLInputElement;
		await fireEvent.click(box);
		expect(box.checked).toBe(false);
		expect(owner.setTrustedLan).not.toHaveBeenCalled();
		owner.setTrustedLan.mockRejectedValueOnce({ detail: 'Not allowed from here' });
		await fireEvent.click(box);
		expect(owner.setTrustedLan).toHaveBeenCalledWith(true);
		expect((await screen.findByRole('alert')).textContent).toBe('Not allowed from here');
		expect(box.checked).toBe(false);
	});

	it('turns off without a prompt and reloads the owner status', async () => {
		const confirm = vi.fn();
		vi.stubGlobal('confirm', confirm);
		owner.fetchOwnerStatus.mockResolvedValue(loaded({ trusted_lan: true }));
		render(OwnerAccessSection);
		const box = (await screen.findByRole('checkbox', { name: 'Trusted LAN, no login' })) as HTMLInputElement;
		expect(box.checked).toBe(true);
		await fireEvent.click(box);
		await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
		expect(confirm).not.toHaveBeenCalled();
		expect(owner.setTrustedLan).toHaveBeenCalledWith(false);
	});
});

describe('status load failures', () => {
	it('shows an error with a retry instead of loading forever when the worker does not answer', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'unreachable' });
		render(OwnerAccessSection);
		expect((await screen.findByRole('alert')).textContent).toContain('Could not load owner access status');
		expect(screen.queryByText('Loading…')).toBeNull();
		expect(screen.queryByRole('form', { name: 'Owner password' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		expect(await screen.findByText('Change the owner password')).toBeTruthy();
		expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2);
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('stays on the error when the retry fails too', async () => {
		owner.fetchOwnerStatus.mockResolvedValue({ ok: false, reason: 'unreachable' });
		render(OwnerAccessSection);
		await screen.findByRole('button', { name: 'Try again' });
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(owner.fetchOwnerStatus).toHaveBeenCalledTimes(2));
		expect((await screen.findByRole('button', { name: 'Try again' })).hasAttribute('disabled')).toBe(false);
		expect(screen.getByRole('alert').textContent).toContain('Could not load owner access status');
		expect(screen.queryByText('Loading…')).toBeNull();
	});

	it('explains that an older worker has no owner passwords', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce({ ok: false, reason: 'unsupported' });
		render(OwnerAccessSection);
		expect((await screen.findByRole('alert')).textContent).toContain('does not support owner passwords');
		expect(screen.queryByText('Loading…')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
	});

	it('keeps the form when the reload after a save fails', async () => {
		owner.fetchOwnerStatus.mockResolvedValueOnce(loaded()).mockResolvedValueOnce({ ok: false, reason: 'unreachable' });
		render(OwnerAccessSection);
		await screen.findByText('Change the owner password');
		await fill({ 'Current password': 'old-secret', 'New password': 'long-enough-pass', 'Repeat the new password': 'long-enough-pass' });
		await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Password changed. Every other browser was signed out.'));
		expect(screen.getByText('Change the owner password')).toBeTruthy();
		expect(screen.queryByRole('alert')).toBeNull();
	});
});
