import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks, getErrorMessage: (cause: { detail?: string; message?: string }) => cause.detail ?? cause.message }));
import LicenseSection from './LicenseSection.svelte';

const base = {
	mode: 'licensed', state: 'active', allowed: true, reason: null, banner: null, plan: 'monthly', seats: 3, seats_used: 2,
	account_id: 'acct', license_id: 'lic', key_hint: '7QK2', install_id: 'inst', issued_at: '2026-09-20T00:00:00Z',
	expires_at: '2026-10-04T00:00:00Z', grace: false, grace_until: null, last_refresh_at: '2026-09-20T00:00:00Z',
	next_refresh_at: null, last_error: null, server_status: null, server_configured: true
};

beforeEach(() => { vi.resetAllMocks(); vi.spyOn(window, 'confirm').mockReturnValue(true); });
afterEach(cleanup);

it('shows the beta notice and no key entry in beta builds', async () => {
	mocks.get.mockResolvedValue({ ...base, mode: 'beta', state: 'beta', plan: 'beta' });
	render(LicenseSection, { isAdmin: true });
	expect((await screen.findByTestId('license-beta')).textContent).toContain('no license needed');
	expect(screen.queryByLabelText('License key')).toBeNull();
});

it('shows status, plan, seats used and expiry for a licensed worker', async () => {
	mocks.get.mockResolvedValue(base);
	render(LicenseSection, { isAdmin: false });
	expect((await screen.findByTestId('license-state')).textContent).toBe('Active');
	expect(screen.getByTestId('license-seats').textContent).toBe('2 of 3 in use');
	expect(screen.getByText('Monthly')).toBeTruthy();
	expect(screen.getByText('…7QK2')).toBeTruthy();
	expect(screen.getByText('Lease valid until')).toBeTruthy();
	// Only the owner can change the license.
	expect(screen.queryByLabelText('License key')).toBeNull();
	expect(screen.queryByRole('button', { name: 'Deactivate on this worker' })).toBeNull();
});

it('activates with a key and tells the banner to re-read', async () => {
	mocks.get.mockResolvedValue({ ...base, state: 'unlicensed', allowed: false, reason: 'No license is active on this worker.', plan: null });
	mocks.post.mockResolvedValue({ activated: true, ...base });
	const changed = vi.fn();
	window.addEventListener('license-changed', changed);
	render(LicenseSection, { isAdmin: true });
	expect((await screen.findByTestId('license-state')).textContent).toBe('Not activated');
	const input = screen.getByLabelText('License key');
	await fireEvent.input(input, { target: { value: ' ASH-AAAAA-BBBBB-CCCCC-DDDDD ' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/license/activate', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' }));
	expect((await screen.findByRole('status')).textContent).toContain('licensed');
	expect(screen.getByTestId('license-state').textContent).toBe('Active');
	expect(changed).toHaveBeenCalled();
	window.removeEventListener('license-changed', changed);
});

it('when every seat is taken, lists the workers and frees one with the key', async () => {
	mocks.get.mockResolvedValue({ ...base, state: 'unlicensed', allowed: false, plan: null });
	mocks.post.mockImplementation(async (path: string) => path === '/license/activate'
		? { activated: false, code: 'seats_full', detail: 'All 3 workers on this license are in use.', seats: 3,
			installs: [{ install_id: 'old-box-1', name: 'Old box', app_version: '0.8.0', last_seen: '2026-08-01T00:00:00Z' }] }
		: { ok: true, seats_used: 2 });
	render(LicenseSection, { isAdmin: true });
	await fireEvent.input(await screen.findByLabelText('License key'), { target: { value: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
	expect((await screen.findByRole('alert')).textContent).toContain('All 3 workers');
	expect(screen.getByText('Old box')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/license/release', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD', install_id: 'old-box-1' }));
	expect((await screen.findByRole('status')).textContent).toContain('Freed the seat of Old box');
});

it('shows the grace banner and deactivates only after confirming', async () => {
	mocks.get.mockResolvedValue({ ...base, state: 'grace', grace: true, grace_until: '2026-10-01T00:00:00Z',
		banner: { severity: 'warning', message: 'Your last payment failed.' } });
	mocks.post.mockResolvedValue({ ...base, state: 'unlicensed', allowed: false, seat_released: true });
	render(LicenseSection, { isAdmin: true });
	expect((await screen.findByTestId('license-state')).textContent).toBe('Payment grace period');
	expect(screen.getByText('Your last payment failed.')).toBeTruthy();
	expect(screen.getByText('Payment grace until')).toBeTruthy();
	(window.confirm as ReturnType<typeof vi.fn>).mockReturnValueOnce(false);
	await fireEvent.click(screen.getByRole('button', { name: 'Deactivate on this worker' }));
	expect(mocks.post).not.toHaveBeenCalled();
	await fireEvent.click(screen.getByRole('button', { name: 'Deactivate on this worker' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/license/deactivate'));
	expect((await screen.findByRole('status')).textContent).toContain('seat freed');
});

it('reports activation errors', async () => {
	mocks.get.mockResolvedValue({ ...base, state: 'unlicensed', allowed: false, plan: null });
	mocks.post.mockRejectedValue({ detail: 'License key not recognised', status: 404 });
	render(LicenseSection, { isAdmin: true });
	await fireEvent.input(await screen.findByLabelText('License key'), { target: { value: 'ASH-X' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
	expect((await screen.findByRole('alert')).textContent).toBe('License key not recognised');
});
