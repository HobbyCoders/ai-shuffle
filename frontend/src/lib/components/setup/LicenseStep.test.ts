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
	mode: 'licensed', state: 'unlicensed', allowed: false, reason: 'No license', banner: null, plan: null,
	seats: null, seats_used: null, account_id: null, license_id: null, key_hint: null, install_id: null,
	issued_at: null, expires_at: null, grace: false, grace_until: null, last_refresh_at: null,
	next_refresh_at: null, last_error: null, server_status: null, server_configured: true,
};

beforeEach(() => { vi.resetAllMocks(); });
afterEach(() => cleanup());

describe('setup license step', () => {
	it('says a beta build needs no key and continues', async () => {
		mocks.getLicense.mockResolvedValue({ ...base, mode: 'beta', state: 'beta', allowed: true, plan: 'beta' });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText('Beta: no license needed')).toBeTruthy();
		expect(screen.queryByLabelText('License key')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('activates a key and then continues', async () => {
		mocks.getLicense.mockResolvedValue(base);
		mocks.activate.mockResolvedValue({ activated: true, ...base, state: 'trialing', allowed: true, plan: 'monthly', key_hint: 'WXYZ' });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		await fireEvent.input(await screen.findByLabelText('License key'), { target: { value: ' ASH-AAAAA-BBBBB-CCCCC-DDDDD ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect(mocks.activate).toHaveBeenCalledWith('ASH-AAAAA-BBBBB-CCCCC-DDDDD');
		expect(await screen.findByText('This worker is licensed')).toBeTruthy();
		expect(screen.getByText(/Monthly, key ending WXYZ/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('lets the owner add the key later, and explains a full seat count', async () => {
		mocks.getLicense.mockResolvedValue(base);
		mocks.activate.mockResolvedValue({ activated: false, code: 'seats_full', detail: 'All 3 workers are in use.', seats: 3, installs: [] });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		await fireEvent.input(await screen.findByLabelText('License key'), { target: { value: 'ASH-1' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('All 3 workers are in use.'));
		await fireEvent.click(screen.getByRole('button', { name: 'Add it later' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});
});
