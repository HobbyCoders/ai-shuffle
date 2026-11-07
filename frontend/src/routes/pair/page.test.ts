import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ exchange: vi.fn(), checkAuth: vi.fn(), goto: vi.fn() }));
vi.mock('$lib/api/pairing', () => ({ exchangePairingGrant: mocks.exchange }));
vi.mock('$lib/stores/auth.svelte', async () => {
	const { readable } = await import('svelte/store');
	return { auth: { checkAuth: mocks.checkAuth }, isAdmin: readable(false) };
});
vi.mock('$app/navigation', () => ({ goto: mocks.goto }));
vi.mock('$lib/services/desktop', () => ({ desktop: null }));

import PairPage from './+page.svelte';

beforeEach(() => {
	vi.clearAllMocks();
	localStorage.clear();
	vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('pairing on a plain-HTTP address', () => {
	it.each(['missing', 'throwing'])('connects with %s randomUUID and retains the device id on retry', async (mode) => {
		vi.stubGlobal('crypto', {
			getRandomValues: crypto.getRandomValues.bind(crypto),
			randomUUID: mode === 'missing' ? undefined : () => { throw new Error('Insecure context'); }
		});
		mocks.exchange.mockRejectedValueOnce(new Error('Code expired')).mockResolvedValueOnce({});
		const { getByRole, getByLabelText, getByText } = render(PairPage);
		await fireEvent.input(getByLabelText('Device name'), { target: { value: 'Test browser' } });
		await fireEvent.input(getByLabelText('Pairing code'), { target: { value: 'ABCD-EFGH-IJKL' } });
		await fireEvent.click(getByRole('button', { name: 'Connect device' }));
		await waitFor(() => expect(mocks.exchange).toHaveBeenCalledTimes(1));
		const deviceId = mocks.exchange.mock.calls[0][0].deviceId;
		expect(deviceId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
		expect(localStorage.getItem('shuffle-paired-device-id')).toBe(deviceId);
		expect(getByText('Code expired')).toBeTruthy();
		await fireEvent.click(getByRole('button', { name: 'Connect device' }));
		await waitFor(() => expect(mocks.goto).toHaveBeenCalledWith('/'));
		expect(mocks.exchange).toHaveBeenLastCalledWith(expect.objectContaining({ deviceId, deviceName: 'Test browser' }));
		expect(mocks.checkAuth).toHaveBeenCalledOnce();
	});
});
