import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e?.detail ?? e?.message ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e
}));

import CloudPage from './CloudPage.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';

/** The three shapes `GET /api/v1/license` returns (PR #2298, "API contract for Settings → Cloud"). */
const notLinked = {
	mode: 'cloud',
	state: 'not_linked',
	linked: false,
	allowed: true,
	reason: null,
	banner: null,
	message: 'Not linked to AI Shuffle Cloud. Local use is free.',
	remote_worker_limit: 0,
	lease_state: 'none',
	plan: null,
	seats: 0,
	seats_used: null,
	account_id: null,
	license_id: null,
	key_hint: null,
	install_id: null,
	issued_at: null,
	expires_at: null,
	grace: false,
	grace_until: null,
	last_refresh_at: null,
	next_refresh_at: null,
	last_error: null,
	server_status: null,
	server_configured: true
};
const free = { ...notLinked, state: 'free', linked: true, message: 'Free account: 1 remote worker.', remote_worker_limit: 1, plan: 'free', seats: 1 };
const paid = {
	...notLinked,
	state: 'paid',
	linked: true,
	message: 'Paid Cloud plan. Local use does not depend on this plan.',
	remote_worker_limit: 3,
	lease_state: 'active',
	plan: 'monthly',
	seats: 3,
	seats_used: 1,
	account_id: 'acct-example',
	license_id: 'plan-example',
	install_id: 'install-example',
	key_hint: '7QK2',
	issued_at: '2026-10-11T00:00:00Z',
	expires_at: '2026-10-25T12:00:00Z',
	last_refresh_at: '2026-10-11T00:00:00Z',
	next_refresh_at: '2026-10-12T00:00:00Z'
};

let status: Record<string, unknown>;

beforeEach(() => {
	vi.resetAllMocks();
	settingsNav.reset();
	status = { ...notLinked };
	transport.get.mockImplementation(async (path: string) => (path === '/license' ? { ...status } : {}));
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

const state = () => screen.getByTestId('cloud-state').textContent ?? '';
const tierNamed = (name: string) =>
	within(screen.getByTestId('cloud-tiers')).getByText(name).closest('li') as HTMLElement;

function expectNoLicenceWording() {
	const text = document.body.textContent ?? '';
	expect(text).not.toMatch(/unlicensed|not activated|agents? (stopped|paused)|new agent runs pause/i);
}

describe('not linked', () => {
	it('says what a free account adds and links through the Devices sign-in', async () => {
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		expect(screen.getByRole('heading', { level: 1, name: 'Cloud' })).toBeTruthy();
		expect(screen.getByTestId('cloud-message').textContent).toContain('Local use is free');
		expect(tierNamed('This worker').getAttribute('aria-current')).toBe('true');
		expect(tierNamed('Free account').textContent).toContain('remote worker, no card');
		expect(screen.getByText('A free account adds one')).toBeTruthy();
		expect(screen.getByTestId('cloud-seats').textContent).toContain('None');
		const plans = screen.getByRole('link', { name: /See plans/ });
		expect(plans.getAttribute('href')).toBe('https://hobbycoders.com/ai-shuffle/pricing');
		expect(plans.getAttribute('target')).toBe('_blank');
		expect(screen.queryByText('Deactivate on this worker')).toBeNull();
		expectNoLicenceWording();

		await fireEvent.click(screen.getByRole('button', { name: 'Link account' }));
		expect(settingsNav.category).toBe('devices');
		expect(settingsNav.selectedItem('devices')).toBe('account');
	});

	it('opens the Cloud guide in the Docs panel', async () => {
		const open = vi.spyOn(layout, 'openFeaturePage');
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		await fireEvent.click(screen.getByRole('button', { name: /How Cloud works/ }));
		expect(open).toHaveBeenCalledExactlyOnceWith('docs', 'getting-started/cloud');
	});

	it('hides the key entry in builds without a lease server', async () => {
		status = { ...notLinked, server_configured: false };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		expect(screen.queryByText('Plan key')).toBeNull();
		expect(screen.getByRole('button', { name: 'Link account' })).toBeTruthy();
	});
});

describe('free', () => {
	it('shows the included remote worker, not a usage count, and an upgrade link', async () => {
		status = { ...free };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(screen.getByText('Linked to AI Shuffle Cloud')).toBeTruthy();
		expect(screen.getByTestId('cloud-seats').textContent).toContain('1 remote worker included');
		expect(screen.getByTestId('cloud-seats').textContent).not.toContain('1 of 1');
		expect(tierNamed('Free account').getAttribute('aria-current')).toBe('true');
		expect(tierNamed('This worker').classList.contains('reached')).toBe(true);
		const upgrade = screen.getByRole('link', { name: /Upgrade/ });
		expect(upgrade.getAttribute('href')).toBe('https://hobbycoders.com/ai-shuffle/pricing');
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expect(screen.queryByText('Deactivate on this worker')).toBeNull();
		expectNoLicenceWording();
	});
});

describe('paid', () => {
	it('shows the plan, seats used of total, the renewal date and the key', async () => {
		status = { ...paid };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByText('Monthly')).toBeTruthy();
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 3');
		expect(screen.getByText('Paid access until')).toBeTruthy();
		expect(screen.getByTestId('cloud-renewal').textContent).toContain('Oct 25, 2026');
		expect(screen.getByRole('button', { name: /^Plan key/ }).textContent).toContain('ASH-…7QK2');
		expect(tierNamed('Paid plan').getAttribute('aria-current')).toBe('true');
		expect(screen.queryByRole('link', { name: /Upgrade|See plans/ })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expectNoLicenceWording();
	});

	it('still offers linking when a paid key runs without a linked account', async () => {
		status = { ...paid, linked: false };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByText('Plan key, account not linked')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Link account' })).toBeTruthy();
	});

	it('shows a payment grace period on the plan row, not as a banner', async () => {
		status = { ...paid, lease_state: 'grace', grace: true, grace_until: '2026-10-20T12:00:00Z' };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByText(/Payment grace until Oct 20/)).toBeTruthy();
		expect(document.querySelector('.st-banner')).toBeNull();
	});

	it('checks the plan on request', async () => {
		status = { ...paid };
		transport.post.mockResolvedValue({ ...paid, last_refresh_at: new Date().toISOString() });
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		await fireEvent.click(screen.getByRole('button', { name: 'Check the plan now' }));
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/refresh'));
		expect((await screen.findByRole('status')).textContent).toContain('Checked with AI Shuffle Cloud');
		expect(screen.getByText('Checked just now')).toBeTruthy();
	});

	it('deactivates only after confirming and falls back to the returned state', async () => {
		status = { ...paid };
		transport.post.mockResolvedValue({ ...free, seat_released: true });
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
		expect(transport.post).not.toHaveBeenCalled();
		expect(screen.getByText("Free this worker's paid seat?")).toBeTruthy();
		const confirm = screen.getAllByRole('button', { name: 'Deactivate' }).at(-1)!;
		await fireEvent.click(confirm);
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/deactivate'));
		expect((await screen.findByRole('status')).textContent).toContain('seat freed. Local use is unaffected');
		await waitFor(() => expect(state()).toContain('Free'));
	});
});

describe('a saved key that no longer grants paid access', () => {
	// After expiry, an invalid key or a seat freed elsewhere the backend falls
	// back to free (linked) or not_linked, but the key stays on this worker.
	const lapsedCases = [
		{ lease_state: 'expired', state: 'free', linked: true, shows: 'Ended Oct 25, 2026' },
		{ lease_state: 'invalid', state: 'not_linked', linked: false, shows: 'Key not accepted' },
		{ lease_state: 'seat_lost', state: 'free', linked: true, shows: 'Seat freed elsewhere' }
	] as const;

	for (const c of lapsedCases) {
		it(`keeps Check now and Deactivate for ${c.lease_state}`, async () => {
			status = {
				...paid,
				state: c.state,
				linked: c.linked,
				lease_state: c.lease_state,
				remote_worker_limit: c.state === 'free' ? 1 : 0,
				plan: c.state === 'free' ? 'free' : null
			};
			transport.post.mockImplementation(async (path: string) =>
				path === '/license/refresh' ? { ...status } : { ...status, key_hint: null, lease_state: 'none', seat_released: true }
			);
			render(CloudPage);
			await waitFor(() => expect(state()).toContain(c.state === 'free' ? 'Free' : 'Not linked'));
			expect(screen.getByTestId('cloud-renewal').textContent).toContain(c.shows);
			expect(screen.queryByText('Monthly')).toBeNull();
			expect(screen.getByRole('button', { name: /^Plan key/ }).textContent).toContain('ASH-…7QK2');
			expect(screen.getByRole('link', { name: /Upgrade|See plans/ })).toBeTruthy();
			expectNoLicenceWording();

			await fireEvent.click(screen.getByRole('button', { name: 'Check the plan now' }));
			await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/refresh'));

			expect(screen.getByText('Forgets the saved key')).toBeTruthy();
			await fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }));
			expect(screen.getByText('Forget the saved plan key?')).toBeTruthy();
			await fireEvent.click(screen.getAllByRole('button', { name: 'Deactivate' }).at(-1)!);
			await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/deactivate'));
			await waitFor(() => expect(screen.queryByText('Deactivate on this worker')).toBeNull());
		});
	}
});

describe('plan key', () => {
	it('activates a key from the not-linked state', async () => {
		transport.post.mockResolvedValue({ activated: true, ...paid, linked: false });
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		await fireEvent.click(screen.getByRole('button', { name: /^Plan key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'Plan key' }), { target: { value: ' ASH-AAAAA-BBBBB-CCCCC-DDDDD ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		await waitFor(() => expect(transport.post).toHaveBeenCalledWith('/license/activate', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' }));
		expect((await screen.findByRole('status')).textContent).toContain('Paid plan active on this worker');
		expect(state()).toContain('Paid');
	});

	it('when every seat is taken, lists the workers and frees one with the key', async () => {
		status = { ...free };
		transport.post.mockImplementation(async (path: string) =>
			path === '/license/activate'
				? {
						activated: false,
						code: 'seats_full',
						detail: 'All seats are in use.',
						seats: 3,
						installs: [{ install_id: 'old-box-1', name: 'Old worker', last_seen: '2026-10-11T00:00:00Z' }]
					}
				: { ok: true, seats_used: 2 }
		);
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		await fireEvent.click(screen.getByRole('button', { name: /^Plan key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'Plan key' }), { target: { value: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect((await screen.findByRole('alert')).textContent).toBe('All seats are in use.');
		const seats = screen.getByRole('group', { name: 'Workers using this plan' });
		await fireEvent.click(within(seats).getByRole('button', { name: 'Free seat' }));
		await fireEvent.click(within(seats).getAllByRole('button', { name: 'Free seat' }).at(-1)!);
		await waitFor(() =>
			expect(transport.post).toHaveBeenCalledWith('/license/release', { key: 'ASH-AAAAA-BBBBB-CCCCC-DDDDD', install_id: 'old-box-1' })
		);
		expect((await screen.findByRole('status')).textContent).toContain('Freed the seat of Old worker');
	});

	it('reports activation errors without touching local use', async () => {
		transport.post.mockRejectedValue({ detail: 'Plan key not recognised', status: 404 });
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		await fireEvent.click(screen.getByRole('button', { name: /^Plan key/ }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'Plan key' }), { target: { value: 'ASH-X' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Activate' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Plan key not recognised');
		expect(state()).toContain('Not linked');
	});
});

describe('loading', () => {
	it('says Unavailable when the status cannot be read', async () => {
		transport.get.mockRejectedValue(new Error('offline'));
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Unavailable'));
		expect(screen.queryByRole('heading', { name: 'Plan' })).toBeNull();
	});
});

describe('coverage', () => {
	it('renders a row for every Cloud entry across the paid and free states', async () => {
		status = { ...paid, linked: false };
		await assertCategoryCoverage('cloud', () => render(CloudPage).container, {
			settle: () => waitFor(() => expect(state()).toContain('Paid')),
			skip: { 'cloud.upgrade': 'only when not on a paid plan; the free and not-linked tests render it' }
		});
	});
});
