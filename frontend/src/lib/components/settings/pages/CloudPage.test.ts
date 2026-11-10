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

/** The shapes `GET /api/v1/license` returns now that the plan follows the signed-in account. */
const notLinked = {
	mode: 'cloud',
	source: 'account',
	state: 'not_linked',
	linked: false,
	allowed: true,
	reason: null,
	banner: null,
	message: 'Not linked to AI Shuffle Cloud. Local use is free.',
	remote_worker_limit: 0,
	plan: null,
	seats: null,
	seats_used: null,
	expires_at: null,
	key_hint: null,
	install_id: null,
	lease_state: 'none',
	server_configured: false
};
const free = { ...notLinked, state: 'free', linked: true, message: 'Free account: 3 remote workers.', remote_worker_limit: 3, plan: 'free' };
const paid = {
	...notLinked,
	state: 'paid',
	linked: true,
	message: 'Paid plan: 10 remote workers.',
	remote_worker_limit: 10,
	plan: 'paid',
	seats: 10,
	seats_used: 1,
	expires_at: '2026-10-25T12:00:00Z'
};

let status: Record<string, unknown>;

beforeEach(() => {
	vi.resetAllMocks();
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	settingsNav.reset();
	status = { ...notLinked };
	transport.get.mockImplementation(async (path: string) => (path === '/license' ? { ...status } : {}));
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

const state = () => screen.getByTestId('cloud-state').textContent ?? '';
const seats = () => screen.getByTestId('cloud-seats').textContent?.replace(/\s+/g, ' ').trim() ?? '';
const tierNamed = (name: string) =>
	within(screen.getByTestId('cloud-tiers')).getByText(name).closest('.st-row') as HTMLElement;
const currentRow = () => screen.getByTestId('cloud-tiers').querySelector('[aria-current]')?.closest('.st-row') as HTMLElement;

/** Nothing on the page talks about keys, leases or blocked agents any more. */
function expectNoKeyWording() {
	const text = document.body.textContent ?? '';
	expect(text).not.toMatch(/unlicensed|not activated|agents? (stopped|paused)|new agent runs pause/i);
	expect(text).not.toMatch(/plan key|license key|ASH-|deactivate|check now|lease/i);
	expect(screen.queryByRole('textbox')).toBeNull();
}

function expectPlanFollowsAccount() {
	expect(screen.getByTestId('cloud-follows').textContent).toBe('Your plan follows your Cloud account across workers. Local use stays free.');
}

describe('not linked', () => {
	it('says what a free account adds and links through the Devices sign-in', async () => {
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		expect(screen.getByRole('heading', { level: 1, name: 'Cloud' })).toBeTruthy();
		expect(screen.getAllByRole('heading', { name: 'Plans' })).toHaveLength(1);
		expect(screen.queryByTestId('cloud-message')).toBeNull();
		expect(tierNamed('This worker').querySelector('[aria-current]')?.getAttribute('aria-current')).toBe('true');
		expect(tierNamed('This worker').textContent).toContain('Local use, no account');
		expect(tierNamed('Free account').textContent).toMatch(/3\s*·\s*remote workers, no card/);
		expect(tierNamed('Pro plan').textContent).toMatch(/10\s*·\s*remote workers and sync/);
		expect(tierNamed('Ultimate plan').textContent).toMatch(/30\s*·\s*remote workers and sync/);
		expect(within(screen.getByTestId('cloud-tiers')).queryByText('Paid plan')).toBeNull();
		expect(screen.getByRole('link', { name: /See plans/ }).closest('.st-row')).toBe(tierNamed('Ultimate plan'));
		expect(document.body.textContent).not.toMatch(/founder|yearly|\/year/i);
		expect(screen.queryByTestId('cloud-seats')).toBeNull();
		expect(screen.queryByText('Renews')).toBeNull();
		const plans = screen.getByRole('link', { name: /See plans/ });
		expect(plans.getAttribute('href')).toBe('https://hobbycoders.com/ai-shuffle/pricing');
		expect(plans.getAttribute('target')).toBe('_blank');
		expectPlanFollowsAccount();
		expectNoKeyWording();

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

	it('never calls a key route', async () => {
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		expect(transport.get).toHaveBeenCalledWith('/license');
		expect(transport.post).not.toHaveBeenCalled();
	});
});

describe('free', () => {
	it('keeps an account-specific remote-worker limit in the plan row', async () => {
		status = { ...free, remote_worker_limit: 5, seats_used: 3 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(tierNamed('Free account').textContent).toMatch(/5\s*·\s*remote workers/);
		expect(seats()).toBe('3 of 5 in use');
	});

	it('shows the 3 included remote workers and an upgrade link', async () => {
		status = { ...free };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(screen.getByText('Linked to AI Shuffle Cloud')).toBeTruthy();
		expect(seats()).toBe('3 remote workers included');
		expect(tierNamed('Free account').querySelector('[aria-current]')?.getAttribute('aria-current')).toBe('true');
		expect(tierNamed('This worker').querySelector('[aria-current]')).toBeNull();
		const upgrade = screen.getByRole('link', { name: /See plans/ });
		expect(upgrade.getAttribute('href')).toBe('https://hobbycoders.com/ai-shuffle/pricing');
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expect(screen.queryByText('Renews')).toBeNull();
		expectPlanFollowsAccount();
		expectNoKeyWording();
	});

	it('shows an older hub\'s 2-worker free account as 2', async () => {
		status = { ...free, message: 'Free account: 2 remote workers.', seats: 2, remote_worker_limit: 2 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(currentRow()).toBe(tierNamed('Free account'));
		expect(tierNamed('Free account').textContent).toMatch(/2\s*·\s*remote workers, no card/);
		expect(seats()).toBe('2 remote workers included');
	});

	it('shows how many of the 3 are in use when the account says', async () => {
		status = { ...free, seats_used: 1 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 3 in use');
	});
});

describe('paid, from a hub that names no plan', () => {
	it('shows seats in use of total and the renewal date, with no upgrade', async () => {
		status = { ...paid };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 10 in use');
		expect(screen.getByText('Renews')).toBeTruthy();
		expect(screen.getByTestId('cloud-renewal').textContent).toContain('Oct 25, 2026');
		expect(tierNamed('Paid plan').querySelector('[aria-current]')?.getAttribute('aria-current')).toBe('true');
		expect(screen.getByRole('link', { name: /See plans/ }).closest('.st-row')?.textContent).toContain('Paid plan');
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expectPlanFollowsAccount();
		expectNoKeyWording();
	});

	it('defaults to 10 remote workers when the account gives no seat count', async () => {
		status = { ...paid, seats: null, seats_used: null, remote_worker_limit: 0 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('10 remote workers included');
		expect(tierNamed('Paid plan').textContent).toMatch(/10\s*·\s*remote workers and sync/);
		expect(screen.queryByRole('meter')).toBeNull();
	});

	it('shows the account seat count on the paid card', async () => {
		status = { ...paid, seats: 12, seats_used: 3, remote_worker_limit: 12 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(tierNamed('Paid plan').textContent).toMatch(/12\s*·\s*remote workers and sync/);
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('3 of 12 in use');
	});

	it('shows an older hub\'s 4-worker paid plan as 4', async () => {
		status = { ...paid, message: 'Paid plan: 4 remote workers.', seats: 4, seats_used: 1, remote_worker_limit: 4 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(currentRow()).toBe(tierNamed('Paid plan'));
		expect(tierNamed('Paid plan').textContent).toMatch(/4\s*·\s*remote workers and sync/);
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 4 in use');
	});

	it.each([
		[{ seats: 100, remote_worker_limit: 4 }, '1 of 4 in use', /4\s*·\s*remote workers and sync/],
		[{ seats: 4, remote_worker_limit: 100 }, '1 in use', /Unlimited/],
	])('shows the hub limit, not the seat count, when they differ (%o)', async (extra, shown, row) => {
		status = { ...paid, seats_used: 1, ...extra };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe(shown);
		expect(tierNamed('Paid plan').textContent).toMatch(row);
	});

	it('shows 100 seats with 2 in use as unlimited, without a meter', async () => {
		status = { ...paid, seats: 100, seats_used: 2, remote_worker_limit: 100 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('2 in use');
		expect(screen.getByText('2 in use')).toBeTruthy();
		expect(tierNamed('Paid plan').textContent).toContain('Unlimited');
		expect(screen.queryByRole('meter')).toBeNull();
	});

	it('keeps the meter for 99 seats, just under unlimited', async () => {
		status = { ...paid, seats: 99, seats_used: 2, remote_worker_limit: 99 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('2 of 99 in use');
		expect(tierNamed('Paid plan').textContent).toMatch(/99\s*·\s*remote workers and sync/);
		expect(tierNamed('Paid plan').textContent).not.toContain('Unlimited');
	});

	it.each([100, 1000])('shows unlimited remote workers for %i seats', async (count) => {
		status = { ...paid, seats: count, seats_used: null, remote_worker_limit: count };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('Unlimited remote workers');
		expect(screen.queryByText(/in use/)).toBeNull();
	});

	it('shows a midnight-UTC renewal on its own date west of UTC', async () => {
		const tz = process.env.TZ;
		process.env.TZ = 'America/New_York';
		try {
			// Guard: in this zone the local date of the instant is the day before.
			expect(new Date('2026-11-20T00:00:00Z').getDate()).toBe(19);
			status = { ...paid, expires_at: '2026-11-20T00:00:00+00:00', renews_at: 1795132800000 };
			render(CloudPage);
			await waitFor(() => expect(state()).toContain('Paid'));
			expect(screen.getByTestId('cloud-renewal').textContent).toContain('Nov 20, 2026');
		} finally {
			if (tz === undefined) delete process.env.TZ;
			else process.env.TZ = tz;
		}
	});

	it('leaves out the renewal row when the account has no date', async () => {
		status = { ...paid, expires_at: null };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.queryByText('Renews')).toBeNull();
	});
});

describe('named plans (tier)', () => {
	it('shows Pro as the current plan with the account count, and no extra Paid plan row', async () => {
		status = { ...paid, tier: 'pro', seats: 10, remote_worker_limit: 10, seats_used: 4 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Pro'));
		expect(currentRow()).toBe(tierNamed('Pro plan'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('4 of 10 in use');
		expect(tierNamed('Ultimate plan').textContent).toMatch(/30\s*·\s*remote workers and sync/);
		expect(within(screen.getByTestId('cloud-tiers')).queryByText('Paid plan')).toBeNull();
		expect(screen.getByRole('link', { name: /See plans/ }).closest('.st-row')).toBe(tierNamed('Ultimate plan'));
		expect(screen.getByTestId('cloud-renewal').textContent).toContain('Oct 25, 2026');
	});

	it('shows Ultimate as the current plan', async () => {
		status = { ...paid, tier: 'ultimate', seats: 30, remote_worker_limit: 30, seats_used: 12 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Ultimate'));
		expect(currentRow()).toBe(tierNamed('Ultimate plan'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('12 of 30 in use');
		expect(tierNamed('Pro plan').textContent).toMatch(/10\s*·\s*remote workers and sync/);
		expect(within(screen.getByTestId('cloud-tiers')).queryByText('Paid plan')).toBeNull();
	});

	it('takes the worker count from the account, never from the tier', async () => {
		status = { ...paid, tier: 'pro', seats: 25, remote_worker_limit: 25, seats_used: null };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Pro'));
		expect(tierNamed('Pro plan').textContent).toMatch(/25\s*·\s*remote workers and sync/);
		expect(seats()).toBe('25 remote workers included');
	});

	it('keeps a custom 100-seat licence on the Paid plan row as unlimited', async () => {
		status = { ...paid, tier: 'custom', seats: 100, remote_worker_limit: 100, seats_used: null };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(currentRow()).toBe(tierNamed('Paid plan'));
		expect(tierNamed('Paid plan').textContent).toContain('Unlimited');
		expect(seats()).toBe('Unlimited remote workers');
		expect(tierNamed('Ultimate plan').textContent).toMatch(/30\s*·/);
		expect(screen.getByRole('link', { name: /See plans/ }).closest('.st-row')).toBe(tierNamed('Paid plan'));
	});

	it.each(['enterprise', null, undefined])('falls back to the paid wording for tier %s', async (tier) => {
		status = { ...paid, tier, seats: 12, remote_worker_limit: 12, seats_used: 3 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(currentRow()).toBe(tierNamed('Paid plan'));
		expect(tierNamed('Paid plan').textContent).toMatch(/12\s*·\s*remote workers and sync/);
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('3 of 12 in use');
	});

	it('ignores a paid tier on a free account', async () => {
		status = { ...free, tier: 'pro' };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(currentRow()).toBe(tierNamed('Free account'));
		expect(tierNamed('Pro plan').textContent).toMatch(/10\s*·/);
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
	it('renders a row for every Cloud entry on a paid plan', async () => {
		status = { ...paid, linked: false };
		await assertCategoryCoverage('cloud', () => render(CloudPage).container, {
			settle: () => waitFor(() => expect(state()).toContain('Paid'))
		});
	});
});
