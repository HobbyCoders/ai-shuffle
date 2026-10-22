/**
 * The receiving worker's approval list (#196): it shows who asks, how long
 * ago, and the same code the requesting worker shows; approval needs the
 * owner to confirm the code matches; deny and revoke close it down.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({ peerRequests: vi.fn(), approvePeerRequest: vi.fn(), revokePeerRequest: vi.fn() }));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import PeerRequestList from './PeerRequestList.svelte';
import { TerminalApiError, type PeerRequest } from '$lib/api/terminals';
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';

const request = (over: Partial<PeerRequest> = {}): PeerRequest => ({
	request_id: 'r1', worker_id: 'dev_a', status: 'pending', used: false, expires_in: 98,
	confirmation_code: 'K7Q2XM', source_name: 'Laptop', age_seconds: 22, ...over,
});

beforeEach(() => { peerTerminals.reset(); Object.values(api).forEach((fn) => fn.mockReset()); });
afterEach(() => { cleanup(); peerTerminals.reset(); });

async function mount(requests: PeerRequest[]) {
	api.peerRequests.mockResolvedValue({ requests });
	const ui = render(PeerRequestList, { workerName: 'Desk' });
	await waitFor(() => expect(api.peerRequests).toHaveBeenCalled());
	return ui;
}

describe('pending requests', () => {
	it('shows the requesting worker, the request age, the code and the countdown', async () => {
		const ui = await mount([request()]);
		const card = await ui.findByRole('article', { name: 'Laptop wants a shell on Desk' });
		expect(within(card).getByText('Requested 22s ago')).toBeTruthy();
		expect(within(card).getByRole('img', { name: 'Code from Laptop: K 7 Q, 2 X M' })).toBeTruthy();
		expect(within(card).getByText('1:38')).toBeTruthy();
		expect(card.textContent).toContain('Approving opens one interactive shell on Desk');
	});

	it('only approves after the owner confirms the code matches', async () => {
		const ui = await mount([request()]);
		const approve = (await ui.findByRole('button', { name: 'Approve one shell' })) as HTMLButtonElement;
		expect(approve.disabled).toBe(true);
		expect(ui.getByText('Compare the code first')).toBeTruthy();
		api.approvePeerRequest.mockResolvedValue(request({ status: 'approved', expires_in: 600 }));
		api.peerRequests.mockResolvedValue({ requests: [request({ status: 'approved', expires_in: 600 })] });
		await fireEvent.click(ui.getByRole('checkbox', { name: 'The code matches on Laptop' }));
		expect(approve.disabled).toBe(false);
		await fireEvent.click(approve);
		expect(api.approvePeerRequest).toHaveBeenCalledWith('r1');
		const card = await ui.findByRole('article', { name: 'Approved for Laptop' });
		expect(within(card).getByText(/Waiting for it to open/)).toBeTruthy();
		expect(within(card).getByRole('button', { name: 'Revoke' })).toBeTruthy();
	});

	it('denies a request', async () => {
		const ui = await mount([request()]);
		api.revokePeerRequest.mockResolvedValue({ revoked: true });
		api.peerRequests.mockResolvedValue({ requests: [] });
		await fireEvent.click(await ui.findByRole('button', { name: 'Deny' }));
		expect(api.revokePeerRequest).toHaveBeenCalledWith('r1');
		await waitFor(() => expect(ui.queryByRole('article')).toBeNull());
	});

	it('explains a refused approval on the card', async () => {
		const ui = await mount([request()]);
		api.approvePeerRequest.mockRejectedValue(new TerminalApiError('Recipient terminal request expired or disabled', 403, 'http_403'));
		await fireEvent.click(await ui.findByRole('checkbox', { name: 'The code matches on Laptop' }));
		await fireEvent.click(ui.getByRole('button', { name: 'Approve one shell' }));
		expect((await ui.findByRole('alert')).textContent).toContain('expired, was already approved, or peer terminals were turned off');
	});
});

describe('approved shells', () => {
	it('revokes an open shell from the receiving worker', async () => {
		const ui = await mount([request({ status: 'approved', used: true, expires_in: 540 })]);
		const card = await ui.findByRole('article', { name: 'Laptop has a shell on Desk' });
		expect(within(card).getByText('9:00')).toBeTruthy();
		api.revokePeerRequest.mockResolvedValue({ revoked: true });
		api.peerRequests.mockResolvedValue({ requests: [] });
		await fireEvent.click(within(card).getByRole('button', { name: 'Revoke' }));
		expect(api.revokePeerRequest).toHaveBeenCalledWith('r1');
		await waitFor(() => expect(ui.queryByRole('article')).toBeNull());
	});
});

it('draws nothing when no request waits', async () => {
	const ui = await mount([]);
	expect(ui.queryByRole('region')).toBeNull();
});

it('stops polling when the list unmounts', async () => {
	vi.useFakeTimers();
	try {
		api.peerRequests.mockResolvedValue({ requests: [] });
		const ui = render(PeerRequestList, { workerName: 'Desk' });
		await vi.advanceTimersByTimeAsync(3_100);
		expect(api.peerRequests).toHaveBeenCalledTimes(2);
		ui.unmount();
		await vi.advanceTimersByTimeAsync(10_000);
		expect(api.peerRequests).toHaveBeenCalledTimes(2);
	} finally {
		vi.useRealTimers();
	}
});
