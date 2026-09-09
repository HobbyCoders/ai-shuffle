import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import AddDevicePanel from './AddDevicePanel.svelte';
const endpoint = { url: 'https://worker.example.com', kind: 'https', label: 'Worker', secure: true, loopback: false, qr_eligible: true, source: 'current' };
function grant(purpose: string) {
	const base = { grant: { id: 'offer', purpose, expires_at: new Date(Date.now() + 300_000).toISOString() }, code: 'TESTONLYCODE', display_code: 'TEST-ONLY-CODE', pairing_url: purpose === 'controller' ? 'https://worker.example.com/pair#token=TESTONLYCODE' : null, qr_svg: '<svg></svg>' };
	return purpose === 'network:join' ? { ...base, join: { url: 'shuffle://network/join?endpoint=https%3A%2F%2Fworker.example.com&code=TEST-ONLY-CODE&fp=abcd', endpoint: endpoint.url, code: 'TEST-ONLY-CODE', fingerprint: 'abcd', device_name: 'Desk' } } : base;
}
beforeEach(() => {
	vi.resetAllMocks();
	api.get.mockResolvedValue({ endpoints: [endpoint] });
	api.post.mockImplementation(async (_path: string, input: { purpose: string }) => grant(input.purpose));
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('defaults to a worker invite on an enrolled worker and embeds the reachable address', async () => {
	render(AddDevicePanel, { props: { enrolled: true } });
	const worker = await screen.findByRole('tab', { name: 'Worker' });
	expect(worker.getAttribute('aria-selected')).toBe('true');
	expect(api.get).toHaveBeenCalledExactlyOnceWith('/pairing/endpoints');
	await fireEvent.click(screen.getByRole('button', { name: 'Create worker invite' }));
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'network:join', endpoint_url: endpoint.url, device_label: null });
	expect(await screen.findByText('Worker invite')).toBeTruthy();
	expect(screen.getByText(/It joins immediately/)).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Copy invite link' })).toBeTruthy();
	expect(screen.getByLabelText('Worker invite QR code')).toBeTruthy();
	expect(screen.queryByText(/fingerprint/i)).toBeNull();
	expect(screen.queryByText(/approv/i)).toBeNull();
});

it('only pairs phones and browsers on a worker without a network', async () => {
	render(AddDevicePanel, { props: { enrolled: false } });
	await screen.findByRole('tab', { name: 'Phone or browser' });
	expect(screen.queryByRole('tab', { name: 'Worker' })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Create pairing code' }));
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: endpoint.url, device_label: null });
	expect(await screen.findByRole('button', { name: 'Copy pairing link' })).toBeTruthy();
	expect(screen.getByText(/It connects immediately/)).toBeTruthy();
});

it('switches to a phone code on an enrolled worker and never embeds a loopback address', async () => {
	api.get.mockResolvedValue({ endpoints: [{ ...endpoint, url: 'http://127.0.0.1:8000', loopback: true, qr_eligible: false }] });
	render(AddDevicePanel, { props: { enrolled: true } });
	await screen.findByRole('tab', { name: 'Worker' });
	expect((screen.getByRole('button', { name: 'Create worker invite' }) as HTMLButtonElement).disabled).toBe(true);
	expect(screen.getByRole('alert').textContent).toContain('No address other computers can reach');
	await fireEvent.click(screen.getByRole('tab', { name: 'Phone or browser' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Create pairing code' }));
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: null, device_label: null });
});

it('keeps a code available to cancel again when cancellation fails', async () => {
	api.delete.mockRejectedValueOnce(new Error('Could not cancel')).mockResolvedValueOnce(undefined);
	render(AddDevicePanel, { props: { enrolled: false } });
	await fireEvent.click(await screen.findByRole('button', { name: 'Create pairing code' }));
	await fireEvent.click(await screen.findByRole('button', { name: 'Cancel code' }));
	expect((await screen.findByRole('alert')).textContent).toContain('Could not cancel');
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel code' }));
	expect(api.delete).toHaveBeenCalledWith('/pairing/grants/offer');
	expect(await screen.findByRole('button', { name: 'Create pairing code' })).toBeTruthy();
});

it('offers a retry when addresses fail to load and still creates a code', async () => {
	api.get.mockRejectedValueOnce(new Error('Addresses unavailable')).mockResolvedValueOnce({ endpoints: [endpoint] });
	render(AddDevicePanel, { props: { enrolled: false } });
	expect((await screen.findByRole('alert')).textContent).toContain('Addresses unavailable');
	await fireEvent.click(screen.getByRole('button', { name: 'Retry addresses' }));
	expect(api.get).toHaveBeenCalledTimes(2);
	expect(screen.queryByRole('alert')).toBeNull();
});

it('announces expiry and permits a fresh code', async () => {
	vi.useFakeTimers();
	render(AddDevicePanel, { props: { enrolled: false } });
	await tick(); await vi.advanceTimersByTimeAsync(0);
	await fireEvent.click(screen.getByRole('button', { name: 'Create pairing code' }));
	await vi.advanceTimersByTimeAsync(300_000);
	expect(screen.getByText('The code expired. Create a new one.')).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Create pairing code' })).toBeTruthy();
});

it('shows the shared-database enrollment only when explicitly allowed', async () => {
	render(AddDevicePanel, { props: { enrolled: false, allowLegacyRunner: true } });
	await fireEvent.click(await screen.findByRole('tab', { name: 'Shared-database worker' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Create enrollment code' }));
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'runner', endpoint_url: null, device_label: null });
	expect(screen.getByRole('button', { name: 'Copy enrollment command' }).textContent).toContain('python run_local.py --join');
});
