import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const nav = vi.hoisted(() => ({ openSettings: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: mocks }));
vi.mock('$lib/settings/navigation.svelte', () => nav);
import LicenseBanner from './LicenseBanner.svelte';

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

const licensed = (banner: object | null) => ({ mode: 'licensed', state: banner ? 'grace' : 'active', allowed: true, banner });

it('stays silent in beta and when the license is fine', async () => {
	mocks.get.mockResolvedValueOnce({ mode: 'beta', state: 'beta', banner: null });
	render(LicenseBanner);
	await waitFor(() => expect(mocks.get).toHaveBeenCalledWith('/license'));
	expect(screen.queryByRole('status')).toBeNull();
	expect(screen.queryByRole('alert')).toBeNull();
	cleanup();
	mocks.get.mockResolvedValueOnce(licensed(null));
	render(LicenseBanner);
	await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
	expect(screen.queryByRole('status')).toBeNull();
});

it('shows a dismissible warning during payment grace', async () => {
	mocks.get.mockResolvedValue(licensed({ severity: 'warning', message: 'Your last payment failed.' }));
	render(LicenseBanner);
	expect((await screen.findByRole('status')).textContent).toContain('Your last payment failed.');
	await fireEvent.click(screen.getByRole('button', { name: 'Open license' }));
	expect(nav.openSettings).toHaveBeenCalledWith({ category: 'system', anchor: 'system.license' });
	await fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
	expect(screen.queryByRole('status')).toBeNull();
});

it('keeps the unlicensed notice up and re-reads when the license changes', async () => {
	mocks.get.mockResolvedValueOnce({ mode: 'licensed', state: 'unlicensed', allowed: false,
		banner: { severity: 'error', message: 'No license is active on this worker.' } });
	render(LicenseBanner);
	expect((await screen.findByRole('alert')).textContent).toContain('No license is active');
	expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull();
	mocks.get.mockResolvedValueOnce(licensed(null));
	window.dispatchEvent(new Event('license-changed'));
	await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
});
