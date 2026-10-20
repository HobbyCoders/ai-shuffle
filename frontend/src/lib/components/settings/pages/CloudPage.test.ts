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
const free = { ...notLinked, state: 'free', linked: true, message: 'Free account: 2 remote workers.', remote_worker_limit: 2, plan: 'free' };
const paid = {
	...notLinked,
	state: 'paid',
	linked: true,
	message: 'Paid Cloud plan. Local use does not depend on this plan.',
	remote_worker_limit: 4,
	plan: 'paid',
	seats: 4,
	seats_used: 1,
	expires_at: '2026-10-25T12:00:00Z'
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
const seats = () => screen.getByTestId('cloud-seats').textContent?.replace(/\s+/g, ' ').trim() ?? '';
const tierNamed = (name: string) =>
	within(screen.getByTestId('cloud-tiers')).getByText(name).closest('li') as HTMLElement;

/** Nothing on the page talks about keys, leases or blocked agents any more. */
function expectNoKeyWording() {
	const text = document.body.textContent ?? '';
	expect(text).not.toMatch(/unlicensed|not activated|agents? (stopped|paused)|new agent runs pause/i);
	expect(text).not.toMatch(/plan key|license key|ASH-|deactivate|check now|lease/i);
	expect(screen.queryByRole('textbox')).toBeNull();
}

function expectPlanFollowsAccount() {
	expect(screen.getByTestId('cloud-follows').textContent).toMatch(/plan follows the Cloud account you sign in with/);
	expect(screen.getByTestId('cloud-follows').textContent).toMatch(/another worker .* same plan/);
}

describe('not linked', () => {
	it('says what a free account adds and links through the Devices sign-in', async () => {
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Not linked'));
		expect(screen.getByRole('heading', { level: 1, name: 'Cloud' })).toBeTruthy();
		expect(screen.getByTestId('cloud-message').textContent).toContain('Local use is free');
		expect(tierNamed('This worker').getAttribute('aria-current')).toBe('true');
		expect(tierNamed('This worker').textContent).toContain('Local use, no account');
		expect(tierNamed('Free account').textContent).toMatch(/2\s*remote workers, no card/);
		expect(tierNamed('Paid plan').textContent).toMatch(/4\s*remote workers and sync/);
		expect(screen.getByText('Local use stays free')).toBeTruthy();
		expect(screen.getByText('A free account adds two')).toBeTruthy();
		expect(seats()).toBe('None');
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
	it('shows the 2 included remote workers and an upgrade link', async () => {
		status = { ...free };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(screen.getByText('Linked to AI Shuffle Cloud')).toBeTruthy();
		expect(seats()).toBe('2 remote workers included');
		expect(tierNamed('Free account').getAttribute('aria-current')).toBe('true');
		expect(tierNamed('This worker').classList.contains('reached')).toBe(true);
		const upgrade = screen.getByRole('link', { name: /Upgrade/ });
		expect(upgrade.getAttribute('href')).toBe('https://hobbycoders.com/ai-shuffle/pricing');
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expect(screen.queryByText('Renews')).toBeNull();
		expectPlanFollowsAccount();
		expectNoKeyWording();
	});

	it('shows how many of the 2 are in use when the account says', async () => {
		status = { ...free, seats_used: 1 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Free'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 2 in use');
	});
});

describe('paid', () => {
	it('shows seats in use of total and the renewal date, with no upgrade', async () => {
		status = { ...paid };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('1 of 4 in use');
		expect(screen.getByText('Renews')).toBeTruthy();
		expect(screen.getByTestId('cloud-renewal').textContent).toContain('Oct 25, 2026');
		expect(tierNamed('Paid plan').getAttribute('aria-current')).toBe('true');
		expect(screen.queryByRole('link', { name: /Upgrade|See plans/ })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Link account' })).toBeNull();
		expectPlanFollowsAccount();
		expectNoKeyWording();
	});

	it('defaults to 4 remote workers when the account gives no seat count', async () => {
		status = { ...paid, seats: null, seats_used: null, remote_worker_limit: 4 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('4 remote workers included');
		expect(tierNamed('Paid plan').textContent).toMatch(/4\s*remote workers and sync/);
		expect(screen.queryByRole('meter')).toBeNull();
	});

	it('shows the account seat count on the paid card', async () => {
		status = { ...paid, seats: 12, seats_used: 3, remote_worker_limit: 12 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(tierNamed('Paid plan').textContent).toMatch(/12\s*remote workers and sync/);
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('3 of 12 in use');
	});

	it('shows 100 seats with 2 in use as unlimited, without a meter', async () => {
		status = { ...paid, seats: 100, seats_used: 2, remote_worker_limit: 100 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('Unlimited remote workers');
		expect(screen.getByText('2 in use')).toBeTruthy();
		expect(tierNamed('Paid plan').textContent).toContain('Unlimited');
		expect(screen.queryByRole('meter')).toBeNull();
	});

	it('keeps the meter for 99 seats, just under unlimited', async () => {
		status = { ...paid, seats: 99, seats_used: 2, remote_worker_limit: 99 };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.getByRole('meter', { name: 'Remote workers in use' }).getAttribute('aria-valuetext')).toBe('2 of 99 in use');
		expect(tierNamed('Paid plan').textContent).toMatch(/99\s*remote workers and sync/);
		expect(tierNamed('Paid plan').textContent).not.toContain('Unlimited');
	});

	it.each([100, 1000])('shows unlimited remote workers for %i seats', async (count) => {
		status = { ...paid, seats: count, seats_used: null, remote_worker_limit: count };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(seats()).toBe('Unlimited remote workers');
		expect(screen.queryByText(/in use/)).toBeNull();
	});

	it('leaves out the renewal row when the account has no date', async () => {
		status = { ...paid, expires_at: null };
		render(CloudPage);
		await waitFor(() => expect(state()).toContain('Paid'));
		expect(screen.queryByText('Renews')).toBeNull();
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
			settle: () => waitFor(() => expect(state()).toContain('Paid')),
			skip: { 'cloud.upgrade': 'only when not on a paid plan; the free and not-linked tests render it' }
		});
	});
});
