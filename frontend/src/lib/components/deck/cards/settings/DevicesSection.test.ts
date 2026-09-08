import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Install } from '$lib/api/installs';
import type { PairedClient } from '$lib/api/pairing';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
const appData = vi.hoisted(() => ({ dropDeviceSessions: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData }));
import DevicesSection from './DevicesSection.svelte';
import { resourceSync } from '$lib/services/resource-sync';

const worker = (id: string, extra: Partial<Install> = {}): Install => ({ id, device_name: 'Desk worker', base_url: null, platform: 'linux', version: '1.0', capabilities: {}, last_seen: null, online: true, approved: true, ...extra });
const client = (id: string, extra: Partial<PairedClient> = {}): PairedClient => ({ id, device_id: id, device_name: 'My phone', platform: 'android', client_type: 'mobile', scopes: ['controller'], session_expires_at: null, paired_at: '2026-09-01T10:00:00Z', last_seen: null, revoked_at: null, created_at: '', updated_at: '', ...extra });
let workers: Install[];
let clients: PairedClient[];
const advertise = { url: null, source: 'none', env_pinned: false, override: null, instance_id: 'self' };
const disconnected = { signed_in: false, status: 'disconnected', code: null, qr_svg: null, error: null };
function reads(path: string): unknown {
    if (path === '/cluster/installs') return { installs: [...workers], self_instance_id: 'self' };
    if (path === '/pairing/clients') return { clients: [...clients] };
    if (path === '/cluster/advertise') return advertise;
    if (path === '/cluster/secret/status') return { configured: true, env_pinned: false };
    if (path === '/pairing/endpoints') return { endpoints: [] };
    if (path === '/mobile/hub/connection') return disconnected;
    throw new Error('Unexpected GET ' + path);
}
beforeEach(() => {
    vi.resetAllMocks();
    workers = [worker('self', { device_name: 'This computer' }), worker('shared')];
    clients = [client('shared'), client('browser', { device_name: 'Work browser', client_type: 'browser', platform: 'linux-browser' })];
    api.get.mockImplementation(async (path: string) => reads(path));
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });
function row(key: string): HTMLElement {
    const el = document.querySelector(`[data-device-key="${key}"]`);
    expect(el).not.toBeNull();
    return el as HTMLElement;
}

it.each(['browser', 'linux-browser'])('labels legacy %s clients as browsers even when client_type is mobile', async (platform) => {
    clients = [client('legacy', { platform, client_type: 'mobile' })];
    render(DevicesSection);
    await screen.findByText('3 devices');
    const device = row('client:legacy');
    expect(within(device).getByText('Browser')).toBeTruthy();
    expect(within(device).queryByText('Phone')).toBeNull();
    expect(device.querySelector('svg.lucide-globe')).not.toBeNull();
});

it('renders one continuous mixed list with a combined count and collision-safe IDs', async () => {
    clients[0].device_name = workers[1].device_name;
    render(DevicesSection);
    expect(await screen.findByText('4 devices')).toBeTruthy();
    const list = screen.getByRole('list', { name: 'Devices' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(4);
    expect(screen.getAllByRole('list')).toHaveLength(1);
    expect(row('worker:shared').textContent).toContain('Worker');
    expect(row('client:shared').textContent).toContain('Phone');
    expect(row('client:browser').textContent).toContain('Browser');
    expect(within(row('client:shared')).queryByText(/online|offline/i)).toBeNull();
    expect(within(list).getAllByText('Desk worker')).toHaveLength(2);
});

it('removes the intro and secret requests/UI, and keeps connection controls collapsed', async () => {
    render(DevicesSection);
    await screen.findByText('4 devices');
    expect(screen.queryByText(/Every AI Shuffle install/)).toBeNull();
    expect(screen.queryByText(/Cluster secret/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /rotate/i })).toBeNull();
    expect(api.get.mock.calls.some(([path]) => path.includes('/secret'))).toBe(false);
    expect(screen.queryByText('Pair a controller')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Pair device' })).toBeNull();
    expect(screen.getByText('Connection settings').closest('details')?.open).toBe(false);
});

it.each(['/cluster/installs', '/pairing/clients', '/cluster/advertise'])('keeps healthy devices when %s fails and offers a scoped retry', async (failedPath) => {
    api.get.mockImplementation(async (path: string) => {
        if (path === failedPath) throw new Error('Source unavailable');
        return reads(path);
    });
    render(DevicesSection);
    expect(await screen.findByText('Source unavailable', { exact: false })).toBeTruthy();
    if (failedPath !== '/cluster/installs') expect(screen.getByText('This computer')).toBeTruthy();
    if (failedPath !== '/pairing/clients') expect(screen.getByText('My phone')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Log in' })).toBeTruthy();
    const retryName = failedPath === '/cluster/installs' ? 'Retry workers' : failedPath === '/pairing/clients' ? 'Retry phones and browsers' : 'Retry connection settings';
    api.get.mockImplementation(async (path: string) => reads(path));
    await fireEvent.click(screen.getByRole('button', { name: retryName }));
    await waitFor(() => expect(screen.queryByText('Source unavailable', { exact: false })).toBeNull());
    expect(screen.getByText('4 devices')).toBeTruthy();
});

it('confirms client revocation through the client API without touching a worker with the same ID', async () => {
    render(DevicesSection);
    await screen.findByText('4 devices');
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Revoke My phone' }));
    expect(api.delete).not.toHaveBeenCalled();
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Confirm revoke' }));
    expect(api.delete).toHaveBeenCalledWith('/pairing/clients/shared');
    expect(row('worker:shared')).toBeTruthy();
    expect(document.querySelector('[data-device-key="client:shared"]')).toBeNull();
    expect(screen.getByText('3 devices')).toBeTruthy();
    expect(appData.dropDeviceSessions).not.toHaveBeenCalled();
});

it('preserves worker rename, approval, confirmed revoke/removal and self protections', async () => {
    workers.push(worker('pending', { device_name: 'New worker', approved: false }));
    api.patch.mockResolvedValue(worker('shared', { device_name: 'Renamed worker' }));
    api.put.mockImplementation(async (path: string, body: { approved: boolean }) => worker(path.includes('pending') ? 'pending' : 'shared', { approved: body.approved }));
    render(DevicesSection);
    await screen.findByText('5 devices');
    expect(within(row('worker:self')).queryByRole('button', { name: /Revoke|Remove/ })).toBeNull();
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Rename Desk worker' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Renamed worker' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    expect(api.patch).toHaveBeenCalledWith('/cluster/installs/shared', { device_name: 'Renamed worker' });
    await fireEvent.click(within(row('worker:pending')).getByRole('button', { name: 'Approve New worker' }));
    expect(api.put).toHaveBeenCalledWith('/cluster/installs/pending/approval', { approved: true });
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Revoke Renamed worker' }));
    expect(api.put).not.toHaveBeenCalledWith('/cluster/installs/shared/approval', { approved: false });
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Confirm revoke' }));
    expect(api.put).toHaveBeenCalledWith('/cluster/installs/shared/approval', { approved: false });
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Remove Desk worker' }));
    expect(api.delete).not.toHaveBeenCalled();
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Confirm remove' }));
    expect(api.delete).toHaveBeenCalledWith('/cluster/installs/shared');
    expect(appData.dropDeviceSessions).toHaveBeenCalledWith('shared');
});

it('honors is_self even when the response self ID differs', async () => {
    workers[1].is_self = true;
    render(DevicesSection);
    await screen.findByText('4 devices');
    expect(within(row('worker:shared')).queryByRole('button', { name: /Revoke|Remove/ })).toBeNull();
});

it('refreshes newly paired clients and preserves unsaved worker names', async () => {
    vi.useFakeTimers();
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Rename Desk worker' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Unsaved name' } });
    workers[1] = { ...workers[1], device_name: 'Changed elsewhere' };
    clients.push(client('late', { device_name: 'Newly paired phone' }));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(screen.getByText('Newly paired phone')).toBeTruthy();
    expect(screen.getByText('5 devices')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Device name' }) as HTMLInputElement).value).toBe('Unsaved name');
    expect(api.patch).not.toHaveBeenCalled();
});

it('does not resurrect a revoked client from a stale list response', async () => {
    vi.useFakeTimers();
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    let resolve!: (value: unknown) => void;
    api.get.mockImplementation((path: string) => path === '/pairing/clients' ? new Promise(done => { resolve = done; }) : Promise.resolve(reads(path)));
    await vi.advanceTimersByTimeAsync(10_000);
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Revoke My phone' }));
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Confirm revoke' }));
    resolve({ clients });
    await tick(); await vi.advanceTimersByTimeAsync(0);
    expect(document.querySelector('[data-device-key="client:shared"]')).toBeNull();
    expect(screen.getByText('3 devices')).toBeTruthy();
});

it('shows the shared empty state only after both sources loaded', async () => {
    workers = []; clients = [];
    render(DevicesSection);
    expect(await screen.findByText('No devices yet. Add a device to get started.')).toBeTruthy();
    expect(screen.getByText('0 devices')).toBeTruthy();
});

it('keeps the enrollment panel mounted when closed and shows a later pairing in the list', async () => {
    vi.useFakeTimers();
    api.post.mockResolvedValue({ grant: { id: 'offer', purpose: 'controller', expires_at: new Date(Date.now() + 300_000).toISOString() }, code: 'TESTONLYCODE', display_code: 'TEST ONLY CODE', pairing_url: null, qr_svg: null });
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole('button', { name: 'Add device' }));
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole('button', { name: 'Pair phone or browser' }));
    expect(screen.getByRole('button', { name: 'Cancel code' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('button', { name: 'Cancel code' })).toBeNull();
    clients.push(client('late', { device_name: 'Newly paired phone' }));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(screen.getByText('Newly paired phone')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Add device' }));
    expect(screen.getByRole('button', { name: 'Cancel code' })).toBeTruthy();
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.get.mock.calls.filter(([path]) => path === '/pairing/endpoints')).toHaveLength(1);
});

it('retains previous devices on a refresh failure and marks the count as partial', async () => {
    vi.useFakeTimers();
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    api.get.mockImplementation(async (path: string) => { if (path === '/pairing/clients') throw new Error('Client refresh failed'); return reads(path); });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(screen.getByText('My phone')).toBeTruthy();
    expect(screen.getByText('This computer')).toBeTruthy();
    expect(screen.getByText('4 loaded')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Client refresh failed');
});

it('keeps unsaved address edits across resource refreshes and uses the real address API', async () => {
    vi.useFakeTimers();
    api.put.mockResolvedValue({ ...advertise, url: 'https://new.example.com', override: 'https://new.example.com' });
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    const details = screen.getByText('Connection settings').closest('details')!;
    details.open = true;
    await fireEvent.input(screen.getByLabelText("This worker's address"), { target: { value: 'https://new.example.com' } });
    resourceSync.invalidate(['settings']);
    await vi.advanceTimersByTimeAsync(100);
    expect((screen.getByLabelText("This worker's address") as HTMLInputElement).value).toBe('https://new.example.com');
    await fireEvent.click(screen.getByRole('button', { name: 'Save address' }));
    expect(api.put).toHaveBeenCalledWith('/cluster/advertise', { base_url: 'https://new.example.com' });
});

it('keeps a removed worker draft without counting it as a device', async () => {
    vi.useFakeTimers();
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(within(row('worker:shared')).getByRole('button', { name: 'Rename Desk worker' }));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Device name' }), { target: { value: 'Unsaved name' } });
    workers = workers.filter(({ id }) => id !== 'shared');
    await vi.advanceTimersByTimeAsync(10_000);
    expect((screen.getByRole('textbox', { name: 'Device name' }) as HTMLInputElement).value).toBe('Unsaved name');
    expect(screen.getByText('3 devices')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Save name' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Copy your unsaved name/)).toBeTruthy();
});

it('retains failed client revocation for retry without removing the row', async () => {
    api.delete.mockRejectedValueOnce(new Error('Revoke failed')).mockResolvedValueOnce(undefined);
    render(DevicesSection);
    await screen.findByText('4 devices');
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Revoke My phone' }));
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Confirm revoke' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Revoke failed');
    expect(screen.getByText('4 devices')).toBeTruthy();
    await fireEvent.click(within(row('client:shared')).getByRole('button', { name: 'Confirm revoke' }));
    expect(screen.getByText('3 devices')).toBeTruthy();
});

it('lets slow list requests finish rather than superseding them on every poll', async () => {
    vi.useFakeTimers();
    const responses = new Map<string, (value: unknown) => void>();
    api.get.mockImplementation((path: string) => ['/cluster/installs', '/pairing/clients'].includes(path)
        ? new Promise(done => { responses.set(path, done); })
        : Promise.resolve(reads(path)));
    render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(20_000);
    expect(api.get.mock.calls.filter(([path]) => path === '/cluster/installs')).toHaveLength(1);
    expect(api.get.mock.calls.filter(([path]) => path === '/pairing/clients')).toHaveLength(1);
    for (const [path, resolve] of responses) resolve(reads(path));
    await tick(); await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText('4 devices')).toBeTruthy();
});

it('stops polling both device sources and account status when unmounted', async () => {
    vi.useFakeTimers();
    const view = render(DevicesSection);
    await tick(); await vi.advanceTimersByTimeAsync(0);
    view.unmount();
    const count = api.get.mock.calls.length;
    await vi.advanceTimersByTimeAsync(20_000);
    expect(api.get).toHaveBeenCalledTimes(count);
});

