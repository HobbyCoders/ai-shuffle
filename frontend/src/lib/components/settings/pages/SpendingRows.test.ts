/**
 * Settings > Usage > Spending (#371 step 5): the daily spending limit and the
 * job time limit moved here from the retired Assistant page. They still read
 * and write the server's one limits record; switching the cap off asks first
 * and sends the explicit confirmation the server requires.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import SpendingRows from './SpendingRows.svelte';

const BASE = {
	spend_limit_enabled: true, daily_spend_limit_usd: 20, job_limit_enabled: true, max_job_minutes: 120,
	spent_today_usd: 4.5, day: '2026-10-22', timezone: 'UTC', resets_at: '2026-10-23T00:00:00Z',
	limit_reached: false, warning: false
};
let stored: typeof BASE;
let status = 200;
const patches: Array<{ url: string; body: Record<string, unknown> }> = [];

beforeEach(() => {
	stored = { ...BASE };
	status = 200;
	patches.length = 0;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (status !== 200) return new Response(JSON.stringify({ detail: 'Owner only' }), { status });
		if (init?.method === 'PATCH') {
			const body = JSON.parse(String(init.body));
			patches.push({ url, body });
			const { confirm_spend_limit_off: _confirm, ...rest } = body;
			stored = { ...stored, ...rest };
		}
		return new Response(JSON.stringify(stored), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

async function ready() {
	render(SpendingRows);
	const cap = await screen.findByRole('switch', { name: 'Daily spending limit' });
	await waitFor(() => expect(cap.hasAttribute('disabled')).toBe(false));
	return cap;
}

it("shows today's spend against the limit, read from the limits record", async () => {
	await ready();
	expect(screen.getByRole('heading', { level: 2 }).textContent?.trim()).toBe('Spending');
	const meter = screen.getByRole('meter', { name: 'Spent today' });
	expect(meter.getAttribute('aria-valuetext')).toMatch(/4\.50 of .*20/);
	expect(meter.getAttribute('data-state')).toBe('ok');
	expect(String(vi.mocked(fetch).mock.calls[0][0])).toMatch(/\/owner\/limits$/);
});

it('asks before turning the cap off, then sends the confirmation', async () => {
	const cap = await ready();
	await fireEvent.click(cap);
	expect(patches).toHaveLength(0);
	await fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
	await waitFor(() => expect(patches).toHaveLength(1));
	expect(patches[0].body).toEqual({ spend_limit_enabled: false, confirm_spend_limit_off: true });
	await waitFor(() => expect(screen.getByRole('switch', { name: 'Daily spending limit' }).getAttribute('aria-checked')).toBe('false'));
});

it('cancelling leaves the cap on and saves nothing', async () => {
	const cap = await ready();
	await fireEvent.click(cap);
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(patches).toHaveLength(0);
	expect(screen.getByRole('switch', { name: 'Daily spending limit' }).getAttribute('aria-checked')).toBe('true');
});

it('turns the job limit off without asking', async () => {
	await ready();
	await fireEvent.click(screen.getByRole('switch', { name: 'Longest background job' }));
	await waitFor(() => expect(patches).toHaveLength(1));
	expect(patches[0].body).toEqual({ job_limit_enabled: false });
});

it('marks a reached limit and says when work resumes', async () => {
	stored = { ...BASE, spent_today_usd: 20, limit_reached: true };
	render(SpendingRows);
	const meter = await screen.findByRole('meter', { name: 'Spent today' });
	expect(meter.getAttribute('data-state')).toBe('reached');
	expect(screen.getByText(/Limit reached/)).toBeTruthy();
});

it('keeps every row for a non-owner, marked owner only', async () => {
	status = 403;
	render(SpendingRows);
	expect(await screen.findByText('Owner only')).toBeTruthy();
	expect(screen.getByRole('switch', { name: 'Daily spending limit' }).hasAttribute('disabled')).toBe(true);
});
