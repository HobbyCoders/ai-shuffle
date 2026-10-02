/** Pairing codes, ported from AddDevicePanel.test.ts. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import { PairingCodes, NO_REACHABLE_ADDRESS, enrollmentCommand, type PairingItem } from './pairing-codes.svelte';

const endpoint = { url: 'https://worker.example.com', kind: 'https', label: 'Worker', secure: true, loopback: false, qr_eligible: true, source: 'current' };
function grant(purpose: string) {
	const base = { grant: { id: 'offer', purpose, expires_at: new Date(Date.now() + 300_000).toISOString() }, code: 'TESTONLYCODE', display_code: 'TEST-ONLY-CODE', pairing_url: purpose === 'controller' ? 'https://worker.example.com/pair#token=TESTONLYCODE' : null, qr_svg: '<svg></svg>' };
	return purpose === 'network:join' ? { ...base, join: { url: 'shuffle://network/join?endpoint=x&code=TEST-ONLY-CODE', endpoint: endpoint.url, code: 'TEST-ONLY-CODE', fingerprint: 'abcd', device_name: 'Desk' } } : base;
}
let enrolled = true;
let legacy = false;
let codes: PairingCodes;

beforeEach(() => {
	vi.resetAllMocks();
	enrolled = true;
	legacy = false;
	api.get.mockResolvedValue({ endpoints: [endpoint] });
	api.post.mockImplementation(async (_path: string, input: { purpose: string }) => grant(input.purpose));
	codes = new PairingCodes({ enrolled: () => enrolled, allowLegacyRunner: () => legacy });
});
afterEach(() => { codes.reset(); vi.useRealTimers(); });

async function added(): Promise<PairingItem> {
	const id = await codes.add();
	await vi.waitFor(() => expect(codes.byId(id)?.grant ?? codes.byId(id)?.error).toBeTruthy());
	return codes.byId(id)!;
}

it('mints a worker invite first on an enrolled worker, carrying the reachable address', async () => {
	const item = await added();
	expect(item.kind).toBe('worker');
	expect(codes.kinds).toEqual(['worker', 'client']);
	expect(api.get).toHaveBeenCalledExactlyOnceWith('/pairing/endpoints');
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'network:join', endpoint_url: endpoint.url, device_label: null });
	expect(codes.link(item)).toContain('shuffle://network/join');
	expect(codes.countdown(item)).toMatch(/^[45]:\d\d$/);
});

it('only pairs phones and browsers on a worker without a network', async () => {
	enrolled = false;
	const item = await added();
	expect(item.kind).toBe('client');
	expect(codes.kinds).toEqual(['client']);
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: endpoint.url, device_label: null });
	expect(codes.link(item)).toBe('https://worker.example.com/pair#token=TESTONLYCODE');
});

it('never embeds a loopback address and refuses a worker invite without a reachable one', async () => {
	api.get.mockResolvedValue({ endpoints: [{ ...endpoint, url: 'http://127.0.0.1:8000', loopback: true, qr_eligible: false }] });
	const id = await codes.add();
	await vi.waitFor(() => expect(codes.endpointsLoaded).toBe(true));
	const item = codes.byId(id)!;
	expect(codes.blocker(item)).toBe(NO_REACHABLE_ADDRESS);
	expect(api.post).not.toHaveBeenCalled();
	await codes.change(item, { kind: 'client' });
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: null, device_label: null });
});

it('withdraws the old code when the kind changes', async () => {
	const item = await added();
	await codes.change(item, { kind: 'client' });
	expect(api.delete).toHaveBeenCalledWith('/pairing/grants/offer');
	expect(api.post).toHaveBeenLastCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: endpoint.url, device_label: null });
	expect(item.grant?.grant.purpose).toBe('controller');
});

it('keeps a code to cancel again when cancelling fails', async () => {
	api.delete.mockRejectedValueOnce(new Error('Could not cancel')).mockResolvedValueOnce(undefined);
	const item = await added();
	expect(await codes.cancel(item)).toBe(false);
	expect(item.error).toBe('Could not cancel');
	expect(codes.items).toHaveLength(1);
	expect(await codes.cancel(codes.items[0])).toBe(true);
	expect(api.delete).toHaveBeenCalledWith('/pairing/grants/offer');
	expect(codes.items).toHaveLength(0);
});

it('retries addresses after a failure and can still create a code', async () => {
	api.get.mockRejectedValueOnce(new Error('Addresses unavailable')).mockResolvedValueOnce({ endpoints: [endpoint] });
	enrolled = false;
	const item = await added();
	expect(codes.endpointError).toBe('Addresses unavailable');
	expect(api.post).toHaveBeenCalledWith('/pairing/grants', { purpose: 'controller', endpoint_url: null, device_label: null });
	await codes.loadEndpoints();
	expect(api.get).toHaveBeenCalledTimes(2);
	expect(codes.endpointError).toBe('');
	expect(item.endpointUrl).toBe(endpoint.url);
});

it('expires the code and allows a fresh one', async () => {
	vi.useFakeTimers();
	enrolled = false;
	const id = await codes.add();
	await vi.advanceTimersByTimeAsync(0);
	expect(codes.byId(id)!.grant).not.toBeNull();
	await vi.advanceTimersByTimeAsync(300_000);
	const item = codes.byId(id)!;
	expect(item.grant).toBeNull();
	expect(item.expired).toBe(true);
	await codes.create(item);
	expect(item.grant).not.toBeNull();
	expect(item.expired).toBe(false);
});

it('offers the shared-database enrollment only when allowed', async () => {
	enrolled = false;
	expect(codes.kinds).not.toContain('runner');
	legacy = true;
	expect(codes.kinds).toContain('runner');
	const item = await added();
	await codes.change(item, { kind: 'runner' });
	expect(api.post).toHaveBeenLastCalledWith('/pairing/grants', { purpose: 'runner', endpoint_url: null, device_label: null });
	expect(enrollmentCommand(item.grant!.display_code)).toBe('python run_local.py --join TEST-ONLY-CODE');
});
