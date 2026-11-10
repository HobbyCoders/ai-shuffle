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
		['not linked', base, 3],
		['on a free account', { ...base, state: 'free', linked: true, plan: 'free', remote_worker_limit: 3 }, 3],
		['on a free account that names its tier', { ...base, state: 'free', linked: true, plan: 'free', tier: 'free', remote_worker_limit: 3 }, 3],
		// An older hub gives a free account two: say what the account really adds.
		['on an older hub\'s free account', { ...base, state: 'free', linked: true, plan: 'free', seats: 2, remote_worker_limit: 2 }, 2],
		['on a lapsed paid plan', { ...base, state: 'free', linked: true, plan: 'free', seats: 3, remote_worker_limit: 3 }, 3],
	])('says local use is free, with no key to enter, %s', async (_name, status, workers) => {
		mocks.getLicense.mockResolvedValue(status);
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText('Local use is free')).toBeTruthy();
		expect(document.body.textContent?.replace(/\s+/g, ' ')).toContain(`account adds ${workers} remote workers, with no card`);
		expect(document.body.textContent).not.toMatch(/founder|yearly|\/year/i);
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
		[{ seats: null, remote_worker_limit: 0 }, '10 remote workers', 'Paid Cloud plan active'],
		[{ seats: 12, remote_worker_limit: 12 }, '12 remote workers', 'Paid Cloud plan active'],
		[{ seats: 100, seats_used: 2, remote_worker_limit: 100 }, 'unlimited remote workers', 'Paid Cloud plan active'],
		[{ tier: 'pro', seats: 10, remote_worker_limit: 10 }, '10 remote workers', 'Pro plan active'],
		[{ tier: 'ultimate', seats: 30, remote_worker_limit: 30 }, '30 remote workers', 'Ultimate plan active'],
		// The count is the account's, whatever the tier usually includes.
		[{ tier: 'pro', seats: 25, remote_worker_limit: 25 }, '25 remote workers', 'Pro plan active'],
		[{ tier: 'custom', seats: 100, remote_worker_limit: 100 }, 'unlimited remote workers', 'Paid Cloud plan active'],
		[{ tier: 'enterprise', seats: 12, remote_worker_limit: 12 }, '12 remote workers', 'Paid Cloud plan active'],
		// An older hub's paid plan.
		[{ seats: 4, remote_worker_limit: 4 }, '4 remote workers', 'Paid Cloud plan active'],
		// The hub's limit, not its seat count, is what the account gets.
		[{ seats: 100, remote_worker_limit: 4 }, '4 remote workers', 'Paid Cloud plan active'],
		[{ tier: 'custom', seats: 4, remote_worker_limit: 100 }, 'unlimited remote workers', 'Paid Cloud plan active'],
	])('shows a paid plan that came with the signed-in account (%o)', async (extra, shows, heading) => {
		mocks.getLicense.mockResolvedValue({ ...base, state: 'paid', linked: true, plan: 'paid', ...extra });
		const onContinue = vi.fn();
		render(LicenseStep, { onContinue });
		expect(await screen.findByText(heading)).toBeTruthy();
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
