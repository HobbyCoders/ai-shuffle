import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import PairingPanel from './PairingPanel.svelte';
const endpoint = { url: 'https://worker.example.com', kind: 'https', label: 'Worker', secure: true, loopback: false, qr_eligible: true, source: 'current' };
function grant(purpose = 'controller') { return { grant: { id: 'offer', purpose, expires_at: new Date(Date.now() + 300_000).toISOString() }, code: 'TESTONLYCODE', display_code: 'TEST-ONLY-CODE', pairing_url: 'https://worker.example.com/pair', qr_svg: null }; }
beforeEach(() => {
    vi.resetAllMocks();
    api.get.mockResolvedValue({ endpoints: [endpoint], clients: [] });
    api.post.mockImplementation(async (_path: string, input: { purpose: string }) => grant(input.purpose));
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('loads only enrollment endpoints, not a second list of clients', async () => {
    render(PairingPanel);
    await screen.findByRole('button', { name: 'Pair phone or browser' });
    expect(api.get).toHaveBeenCalledExactlyOnceWith('/pairing/endpoints');
    expect(screen.queryByRole('button', { name: 'Enroll worker' })).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByText(/paired devices|No controller devices|Pair a controller/i)).toBeNull();
    expect(screen.getByText('Pairing address').closest('details')?.open).toBe(false);
    expect(screen.getByText(/For a worker with its own database, use Invite another worker/)).toBeTruthy();
    expect(screen.getByText(/network keys, and future device pairing remain admin-only/)).toBeTruthy();
});

it('creates phone/browser grants using the selected eligible endpoint', async () => {
    render(PairingPanel);
    await fireEvent.click(await screen.findByRole('button', { name: 'Pair phone or browser' }));
    expect(api.post).toHaveBeenCalledWith('/pairing/grants', { endpoint_url: endpoint.url, device_label: null, purpose: 'controller' });
    expect(await screen.findByRole('button', { name: 'Copy pairing link' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel code' })).toBeTruthy();
});

it('keeps worker enrollment code-only and provides the real join command when explicitly allowed', async () => {
    render(PairingPanel, { props: { allowWorkerEnrollment: true } });
    await fireEvent.click(await screen.findByRole('button', { name: 'Enroll worker' }));
    expect(api.post).toHaveBeenCalledWith('/pairing/grants', { endpoint_url: null, device_label: null, purpose: 'runner' });
    expect(await screen.findByText('Worker enrollment code')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy enrollment command' }).textContent).toContain('python run_local.py --join');
});

it('never encodes loopback addresses in a controller grant', async () => {
    api.get.mockResolvedValue({ endpoints: [{ ...endpoint, url: 'http://127.0.0.1:8000', loopback: true, qr_eligible: false }] });
    render(PairingPanel);
    await fireEvent.click(await screen.findByRole('button', { name: 'Pair phone or browser' }));
    expect(api.post).toHaveBeenCalledWith('/pairing/grants', { endpoint_url: null, device_label: null, purpose: 'controller' });
});

it('keeps a code available to cancel again when cancellation fails', async () => {
    api.delete.mockRejectedValueOnce(new Error('Could not cancel code')).mockResolvedValueOnce(undefined);
    render(PairingPanel);
    await fireEvent.click(await screen.findByRole('button', { name: 'Pair phone or browser' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel code' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Could not cancel code');
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel code' }));
    expect(api.delete).toHaveBeenCalledWith('/pairing/grants/offer');
    expect(await screen.findByRole('button', { name: 'Pair phone or browser' })).toBeTruthy();
});

it('offers a retry and code-only enrollment when addresses fail to load', async () => {
    api.get.mockRejectedValueOnce(new Error('Addresses unavailable')).mockResolvedValueOnce({ endpoints: [endpoint] });
    render(PairingPanel);
    expect((await screen.findByRole('alert')).textContent).toContain('Addresses unavailable');
    expect(screen.getByRole('button', { name: 'Pair phone or browser' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry pairing addresses' }));
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).toBeNull();
});

it('announces expiry and permits a fresh code', async () => {
    vi.useFakeTimers();
    render(PairingPanel);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole('button', { name: 'Pair phone or browser' }));
    await vi.advanceTimersByTimeAsync(300_000);
    expect(screen.getByText('Pairing code expired. Create a new code.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pair phone or browser' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel code' })).toBeNull();
});
