/**
 * Claude resets (Claude > Usage): loading, the weekly grants and the 5-hour
 * reset behind an inline confirm, the idempotency key reused on retry, the
 * worker's sign-in message on 409, and the re-read after a claim.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import type { ClaudeResetsResponse, ClaudeWeeklyGrant } from '$lib/api/claude-resets';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	weekly: vi.fn(),
	session: vi.fn(),
	loadUsage: vi.fn(),
	randomUUID: vi.fn()
}));

vi.mock('$lib/api/claude-resets', () => ({
	getClaudeResets: mocks.get,
	claimClaudeWeeklyReset: mocks.weekly,
	claimClaudeSessionReset: mocks.session
}));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { current: { usage: null }, loadUsage: mocks.loadUsage }
}));

import ClaudeUsage from './tabs/ClaudeUsage.svelte';
import { ClaudeResets } from './claude-resets.svelte';
import type { LoopGuard } from './loop-guard.svelte';

function grant(partial: Partial<ClaudeWeeklyGrant> = {}): ClaudeWeeklyGrant {
	return {
		id: 'launch',
		label: 'Launch bonus',
		resets_total: 3,
		resets_left: 2,
		starts_at: null,
		ends_at: '2026-10-30T12:00:00Z',
		clears: ['seven_day'],
		paused: false,
		usable_now: true,
		use_requires_limit: true,
		blocking: [],
		...partial
	};
}

function status(partial: Partial<ClaudeResetsResponse> = {}): ClaudeResetsResponse {
	return {
		weekly: {
			eligible: true,
			ineligible_reason: null,
			at_limit: true,
			exhausted: ['seven_day'],
			grants: [grant()],
			next_grant_id: 'launch',
			weekly_resets_at: null,
			cooldown_until: null
		},
		session: {
			eligible: true,
			ineligible_reason: null,
			available: true,
			next_available_at: null,
			weekly_resets_at: null,
			resets_per_week: 1
		},
		...partial
	};
}

function mount() {
	const resets = new ClaudeResets();
	const guard = { status: null } as unknown as LoopGuard;
	const view = render(ClaudeUsage, { guard, resets });
	return { ...view, resets };
}

beforeEach(() => {
	Object.values(mocks).forEach((mock) => mock.mockReset());
	mocks.loadUsage.mockResolvedValue(undefined);
	mocks.randomUUID.mockReturnValue('uuid-1');
	vi.stubGlobal('crypto', { randomUUID: mocks.randomUUID });
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('status', () => {
	it('loads on open and shows both reset types', async () => {
		mocks.get.mockResolvedValue(status());
		const { getByText, getAllByRole, getByRole } = mount();
		await waitFor(() => expect(getByText('Launch bonus')).toBeTruthy());
		expect(getByText('5-hour reset')).toBeTruthy();
		expect(getByText('Once a week')).toBeTruthy();
		expect(getByText(/2 of 3 left, by/)).toBeTruthy();
		expect(getByText('2 weekly left')).toBeTruthy();
		expect(getAllByRole('button', { name: 'Use' })).toHaveLength(1);
		expect(getByRole('button', { name: 'Reset' })).toBeTruthy();
		expect(mocks.get).toHaveBeenCalledOnce();
	});

	it('explains why nothing can be used', async () => {
		mocks.get.mockResolvedValue(
			status({
				weekly: { ...status().weekly!, at_limit: false, grants: [grant({ usable_now: false })] },
				session: {
					...status().session!,
					eligible: false,
					ineligible_reason: 'not_at_wall',
					available: false
				}
			})
		);
		const { getByText, queryByRole } = mount();
		await waitFor(() => expect(getByText('Opens at the weekly limit')).toBeTruthy());
		expect(getByText('Opens at the 5-hour limit')).toBeTruthy();
		expect(queryByRole('button', { name: 'Use' })).toBeNull();
		expect(queryByRole('button', { name: 'Reset' })).toBeNull();
	});

	it('shows an account without either program', async () => {
		mocks.get.mockResolvedValue({ weekly: null, session: null });
		const { getByText } = mount();
		await waitFor(() => expect(getByText('No resets on this plan')).toBeTruthy());
	});

	it('shows the sign-in message and retries', async () => {
		mocks.get
			.mockRejectedValueOnce({ status: 409, detail: 'Claude Code’s sign-in has expired. Run a Claude turn.' })
			.mockResolvedValueOnce(status());
		const { getByText, getAllByText, getByRole } = mount();
		await waitFor(() => expect(getByText('Could not load resets')).toBeTruthy());
		expect(getAllByText(/Run a Claude turn/).length).toBeGreaterThan(0);
		await fireEvent.click(getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(getByText('Launch bonus')).toBeTruthy());
	});
});

describe('weekly', () => {
	it('asks first, then spends the grant under a key and re-reads', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.weekly.mockResolvedValue({
			result: 'reset',
			reason: null,
			resets_left: 1,
			cleared: ['seven_day'],
			weekly_resets_at: null,
			cooldown_until: null
		});
		const { getByRole, getByText, getAllByText } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		expect(getByText('Can’t be undone')).toBeTruthy();
		expect(mocks.weekly).not.toHaveBeenCalled();
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(getAllByText('Weekly limit reset. 1 reset left.').length).toBeGreaterThan(0));
		expect(mocks.weekly).toHaveBeenCalledWith({ grant_id: 'launch', idempotency_key: 'uuid-1' });
		expect(mocks.get).toHaveBeenCalledTimes(2);
		expect(mocks.loadUsage).toHaveBeenCalledOnce();
	});

	it('reuses the key after a failure', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.weekly.mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValueOnce({
			result: 'already_used',
			reason: 'already_used',
			resets_left: 1,
			cleared: [],
			weekly_resets_at: null,
			cooldown_until: null
		});
		const { getByRole } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Connection lost'));
		await fireEvent.click(getByRole('button', { name: 'Use' }));
		await fireEvent.click(getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(mocks.weekly).toHaveBeenCalledTimes(2));
		expect(mocks.weekly.mock.calls[1][0].idempotency_key).toBe('uuid-1');
		expect(mocks.randomUUID).toHaveBeenCalledTimes(1);
	});

	it('Cancel forgets the key', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.randomUUID.mockReturnValueOnce('uuid-1').mockReturnValueOnce('uuid-2');
		mocks.weekly.mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValue({
			result: 'reset',
			reason: null,
			resets_left: 1,
			cleared: [],
			weekly_resets_at: null,
			cooldown_until: null
		});
		const { getByRole, resets } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Use' })).toBeTruthy());
		await resets.useWeekly('launch');
		resets.cancel('launch');
		await resets.useWeekly('launch');
		expect(mocks.weekly.mock.calls.map((c) => c[0].idempotency_key)).toEqual(['uuid-1', 'uuid-2']);
	});
});

describe('5-hour', () => {
	it('resets after confirming', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.session.mockResolvedValue({ result: 'reset', next_available_at: null });
		const { getByRole, getByText, getAllByText } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Reset' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Reset' }));
		await fireEvent.click(getByRole('button', { name: 'Reset' }));
		await waitFor(() => expect(getAllByText('5-hour limit reset.').length).toBeGreaterThan(0));
		expect(mocks.session).toHaveBeenCalledOnce();
		expect(mocks.loadUsage).toHaveBeenCalledOnce();
	});

	it('keeps a 409 message visible', async () => {
		mocks.get.mockResolvedValue(status());
		mocks.session.mockRejectedValue({ status: 409, detail: 'Run a Claude turn to refresh it' });
		const { getByRole } = mount();
		await waitFor(() => expect(getByRole('button', { name: 'Reset' })).toBeTruthy());
		await fireEvent.click(getByRole('button', { name: 'Reset' }));
		await fireEvent.click(getByRole('button', { name: 'Reset' }));
		await waitFor(() => expect(getByRole('alert').textContent).toContain('Run a Claude turn'));
		expect(mocks.loadUsage).not.toHaveBeenCalled();
	});
});

it('hides resets when the CLI is signed out', () => {
	const guard = { status: null } as unknown as LoopGuard;
	const { queryByText } = render(ClaudeUsage, { guard, resets: null });
	expect(queryByText('Resets')).toBeNull();
	expect(mocks.get).not.toHaveBeenCalled();
});
