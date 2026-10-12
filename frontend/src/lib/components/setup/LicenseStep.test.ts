import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ getLicense: vi.fn(), activate: vi.fn() }));
vi.mock('$lib/api/license', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/license')>()),
	getLicense: mocks.getLicense,
	activateLicense: mocks.activate,
}));
import LicenseStep from './LicenseStep.svelte';

const base = {
	mode: 'cloud', state: 'not_linked', linked: false, remote_worker_limit: 0, lease_state: 'none',
	message: 'Not linked to AI Shuffle Cloud. Local use is free.', allowed: true, reason: null, banner: null, plan: null,
	seats: 0, seats_used: null, account_id: null, license_id: null, key_hint: null, install_id: null,
	issued_at: null, expires_at: null, grace: false, grace_until: null, last_refresh_at: null,
	next_refresh_at: null, last_error: null, server_status: null, server_configured: true,
};

beforeEach(() => { vi.resetAllMocks(); });
afterEach(() => cleanup());

describe('setup Cloud step', () => {
	it('says local use is free and continues without a key', async () => {
		mocks.getLicense.mockResolvedValue({ ...base, server_configured: false });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText('Local use is free')).toBeTruthy();
		expect(screen.queryByLabelText(/plan key/i)).toBeNull();
		expect(document.body.textContent).not.toMatch(/unlicensed|agents do not start/i);
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('lets setup continue while the status request is still pending', async () => {
		mocks.getLicense.mockReturnValue(new Promise(() => {}));
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(screen.getByRole('status').textContent).toContain('Reading the Cloud status');
		const next = screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement;
		expect(next.disabled).toBe(false);
		await fireEvent.click(next);
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('activates an optional paid plan key and then continues', async () => {
		mocks.getLicense.mockResolvedValue(base);
		mocks.activate.mockResolvedValue({
			activated: true, ...base, state: 'paid', lease_state: 'active', plan: 'monthly', seats: 3, seats_used: 1,
			remote_worker_limit: 3, key_hint: 'WXYZ'
		});
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		await fireEvent.input(await screen.findByLabelText(/plan key/i), { target: { value: ' ASH-AAAAA-BBBBB-CCCCC-DDDDD ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect(mocks.activate).toHaveBeenCalledWith('ASH-AAAAA-BBBBB-CCCCC-DDDDD');
		expect(await screen.findByText('Paid Cloud plan active')).toBeTruthy();
		expect(screen.getByText(/Monthly, key ending WXYZ/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('explains a full seat count and still continues', async () => {
		mocks.getLicense.mockResolvedValue(base);
		const detail = 'All seats are in use.';
		mocks.activate.mockResolvedValue({ activated: false, code: 'seats_full', detail, seats: 3, installs: [] });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		await fireEvent.input(await screen.findByLabelText(/plan key/i), { target: { value: 'ASH-1' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		// The server's message is shown as it is, once.
		await waitFor(() => expect(screen.getByRole('alert').textContent?.trim()).toBe(detail));
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});
});
