import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ getLicense: vi.fn() }));
vi.mock('$lib/api/license', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/license')>()),
	getLicense: mocks.getLicense,
}));
import LicenseStep from './LicenseStep.svelte';

const base = {
	mode: 'cloud', source: 'account', state: 'not_linked', linked: false, remote_worker_limit: 0,
	message: 'Not linked to AI Shuffle Cloud. Local use is free.', allowed: true, reason: null, banner: null, plan: null,
	seats: null, seats_used: null, expires_at: null, key_hint: null, install_id: null, lease_state: 'none',
	server_configured: false,
};

beforeEach(() => { vi.resetAllMocks(); });
afterEach(() => cleanup());

function expectNoKeyEntry() {
	expect(screen.queryByRole('textbox')).toBeNull();
	expect(screen.queryByLabelText(/key/i)).toBeNull();
	expect(screen.queryByRole('button', { name: 'Activate' })).toBeNull();
	expect(document.body.textContent).not.toMatch(/ASH-|unlicensed|agents do not start/i);
}

describe('setup Cloud step', () => {
	it.each([
		['not linked', base],
		['on a free account', { ...base, state: 'free', linked: true, plan: 'free', remote_worker_limit: 2 }],
	])('says local use is free, with no key to enter, %s', async (_name, status) => {
		mocks.getLicense.mockResolvedValue(status);
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText('Local use is free')).toBeTruthy();
		expect(document.body.textContent).toContain('adds two remote workers');
		expect(document.body.textContent).toContain('follows the account you sign in with');
		expectNoKeyEntry();
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

	it.each([
		[{ seats: null, remote_worker_limit: 4 }, '4 remote workers'],
		[{ seats: 12, remote_worker_limit: 12 }, '12 remote workers'],
		[{ seats: 100, seats_used: 2, remote_worker_limit: 100 }, 'unlimited remote workers'],
	])('shows a paid plan that came with the signed-in account (%o)', async (extra, shows) => {
		mocks.getLicense.mockResolvedValue({ ...base, state: 'paid', linked: true, plan: 'paid', ...extra });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText('Paid Cloud plan active')).toBeTruthy();
		expect(document.body.textContent?.replace(/\s+/g, ' ')).toContain(`From your signed-in Cloud account: ${shows} and sync.`);
		expectNoKeyEntry();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('explains a status error and still continues', async () => {
		mocks.getLicense.mockRejectedValue(new Error('offline'));
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect((await screen.findByRole('alert')).textContent).toBeTruthy();
		expect(screen.getByText(/Local use does not need it/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		expect(onContinue).toHaveBeenCalledOnce();
	});
});
