import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
const m = vi.hoisted(() => ({ post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: m }));
import ServiceAccessRequest from './ServiceAccessRequest.svelte';

const url = 'https://api.cloudflare.com/client/v4';
const token = 'test-only-private-token';
const endpoint = '/chat-access/s%201/requests/r%2F1';
const field = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
function mount(overrides: Record<string, unknown> = {}) {
	const request = { id: 'r/1', kind: 'service', purpose: 'Read Cloudflare inventory', expires_at: Date.now() / 1000 + 86400, service_provider: 'cloudflare', suggested_url: url };
	const onComplete = vi.fn();
	return { request, onComplete, ...render(ServiceAccessRequest, { sessionId: 's 1', request, savedConnections: [], vaultAvailable: true, onComplete, ...overrides }) };
}
async function prepare() { await fireEvent.click(screen.getByRole('button', { name: 'Continue to setup' })); }
async function review() { await prepare(); await fireEvent.click(screen.getByRole('button', { name: 'I have a token' })); }
async function approve() {
	await fireEvent.input(field('API key or token'), { target: { value: token } });
	await fireEvent.click(screen.getByRole('checkbox', { name: /I approve access/ }));
}
beforeEach(() => {
	vi.clearAllMocks();
	Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
	m.post.mockResolvedValue({}); m.delete.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('guided private service access', () => {
	it('prefills the official endpoint, keeps setup separate from secrets and uses only official guide links', async () => {
		mount();
		expect(field('Service address').value).toBe(url);
		expect(field('Service address').readOnly).toBe(true);
		expect(screen.queryByLabelText('API key or token')).toBeNull();
		expect(screen.queryByRole('timer')).toBeNull();
		await prepare();
		expect(screen.getByText(/Do not use your Global API key/)).toBeTruthy();
		expect(screen.getByText(/only Read permissions/)).toBeTruthy();
		expect(screen.getByText(/not Cloudflare OAuth/)).toBeTruthy();
		const links = screen.getAllByRole('link') as HTMLAnchorElement[];
		expect(links).toHaveLength(3);
		for (const link of links) {
			expect(['dash.cloudflare.com', 'developers.cloudflare.com']).toContain(new URL(link.href).hostname);
			expect(link.rel).toBe('noopener noreferrer');
		}
		expect(screen.queryByLabelText('API key or token')).toBeNull();
		expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Prepare your API token' }));
		expect(m.post).not.toHaveBeenCalled();
	});

	it('remains usable after four minutes and describes preparation, restart and grant timing separately', async () => {
		vi.useFakeTimers(); mount(); await prepare();
		await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
		expect(screen.getByText(/up to 24 hours/)).toBeTruthy();
		expect(screen.getByText(/this worker restarts/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'I have a token' }));
		expect(field('API key or token').disabled).toBe(false);
		expect(screen.getByText(/Access duration starts when approved/)).toBeTruthy();
		expect(screen.getByText(/One-shot action approvals have their own deadlines/)).toBeTruthy();
		expect(m.post).not.toHaveBeenCalled();
	});

	it('requires explicit consent and submits a read-only grant, scrubbing the payload afterwards', async () => {
		let finish!: () => void;
		m.post.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
		const { onComplete } = mount(); await review();
		await fireEvent.input(field('API key or token'), { target: { value: token } });
		const grant = screen.getByRole('button', { name: 'Grant access' }) as HTMLButtonElement;
		expect(grant.disabled).toBe(true);
		await fireEvent.submit(grant.form!); expect(m.post).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('checkbox', { name: /I approve access/ }));
		await fireEvent.click(grant);
		expect(m.post).toHaveBeenCalledWith(endpoint, {
			connection: { kind: 'service', label: 'Cloudflare', minutes: 60, url, auth_header: 'Authorization', auth_scheme: 'Bearer', secret: token, writable: false, fingerprint: '' },
			saved_id: '', minutes: 60, save_connection: false
		});
		expect(field('API key or token').closest('fieldset')?.disabled).toBe(true);
		finish(); await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(field('API key or token').value).toBe('');
		expect(m.post.mock.calls[0][1].connection.secret).toBe('');
	});

	it('keeps a token only in component memory for a correctable retry, without storing or reflecting it', async () => {
		const storage = vi.spyOn(Storage.prototype, 'setItem');
		m.post.mockRejectedValueOnce({ status: 422, detail: token });
		const { request, rerender, onComplete } = mount(); await review(); await approve();
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
		expect(screen.getByRole('alert').textContent).not.toContain(token);
		expect(field('API key or token').value).toBe(token);
		expect(m.post.mock.calls[0][1].connection.secret).toBe('');
		await rerender({ request: { ...request } });
		expect(field('API key or token').value).toBe(token);
		expect(storage).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledTimes(2);
	});

	it('clears credentials and consent on panel close but retains non-secret choices for resuming', async () => {
		const { rerender } = mount(); await review(); await approve();
		await rerender({ clearSecretsKey: 1 });
		expect(field('API key or token').value).toBe('');
		expect(field('Resource name').value).toBe('Cloudflare');
		expect((screen.getByRole('checkbox', { name: /I approve access/ }) as HTMLInputElement).checked).toBe(false);
		expect(m.post).not.toHaveBeenCalled();
	});

	it('does not resurrect a token after closing while a submit fails', async () => {
		let reject!: (error: unknown) => void;
		m.post.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
		const { rerender } = mount(); await review(); await approve();
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await rerender({ clearSecretsKey: 1 }); reject({ status: 500 });
		await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
		expect(field('API key or token').value).toBe('');
	});

	it.each([401, 403, 404, 409, 410])('clears secrets and prevents retry on terminal HTTP %s', async (status) => {
		m.post.mockRejectedValueOnce({ status });
		mount(); await review(); await approve();
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/no longer available/));
		expect(field('API key or token').value).toBe('');
		await fireEvent.submit(screen.getByRole('button', { name: 'Grant access' }).closest('form')!);
		expect(m.post).toHaveBeenCalledTimes(1);
	});

	it('expires preparation locally and prevents stale submission', async () => {
		vi.useFakeTimers(); mount(); await review(); await approve();
		await vi.advanceTimersByTimeAsync(86400 * 1000);
		expect(screen.getByRole('alert').textContent).toMatch(/Preparation expired/);
		expect(field('API key or token').value).toBe('');
		await fireEvent.submit(screen.getByRole('button', { name: 'Grant access' }).closest('form')!);
		expect(m.post).not.toHaveBeenCalled();
	});

	it('cancels explicitly, clears tokens even on cancellation failure, and allows retrying cancellation', async () => {
		m.delete.mockRejectedValueOnce({ status: 500, detail: token });
		const { onComplete } = mount(); await review(); await approve();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel request' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Could not cancel/));
		expect(field('API key or token').value).toBe('');
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel request' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.delete).toHaveBeenLastCalledWith(endpoint); expect(m.post).not.toHaveBeenCalled();
	});

	it('supports custom HTTPS authentication, explicit pinning and writable opt-in without exposing the token', async () => {
		let received: any;
		m.post.mockImplementation(async (_: string, body: unknown) => { received = structuredClone(body); });
		mount(); await fireEvent.change(field('Service'), { target: { value: 'custom' } });
		await fireEvent.input(field('Service address'), { target: { value: 'https://home.example/api' } });
		await prepare(); expect(screen.queryByRole('link')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'I have a token' }));
		await fireEvent.click(screen.getByText('Authentication and certificate settings'));
		await fireEvent.input(field('Credential header'), { target: { value: 'X-API-Key' } });
		await fireEvent.input(field('Scheme'), { target: { value: '' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Trust a specific certificate/ }));
		const fingerprint = field('Certificate SHA-256 fingerprint');
		expect(fingerprint.required).toBe(true);
		await fireEvent.input(fingerprint, { target: { value: 'bad' } }); expect(fingerprint.checkValidity()).toBe(false);
		await fireEvent.input(fingerprint, { target: { value: 'AB'.repeat(32) } }); expect(fingerprint.checkValidity()).toBe(true);
		await fireEvent.click(screen.getByRole('checkbox', { name: /Allow changes/ }));
		await fireEvent.click(screen.getByRole('checkbox', { name: /Save this connection/ }));
		await approve(); await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(received).toBeTruthy());
		expect(received.connection).toMatchObject({ url: 'https://home.example/api', auth_header: 'X-API-Key', auth_scheme: '', writable: true, fingerprint: 'AB'.repeat(32), secret: token });
		expect(received.save_connection).toBe(true);
	});

	it.each(['http://example.com', 'https://key:secret@example.com', 'https://example.com?token=secret', 'https://example.com#secret'])('rejects unsafe API address %s without sending it', async (address) => {
		mount(); await fireEvent.change(field('Service'), { target: { value: 'custom' } });
		await fireEvent.input(field('Service address'), { target: { value: address } }); await prepare();
		expect(screen.getByRole('alert').textContent).toMatch(/HTTPS API base/);
		expect(screen.queryByRole('button', { name: 'I have a token' })).toBeNull(); expect(m.post).not.toHaveBeenCalled();
	});

	it('requires choosing a saved service, shows its scope and routes through the current worker', async () => {
		const { onComplete } = mount({ owner: 'Owner PC', savedConnections: [
			{ id: 'ssh', kind: 'machine', label: 'SSH', target: 'host', fingerprint: '' },
			{ id: 'cf', kind: 'service', label: 'Cloudflare account', target: url, fingerprint: '', writable: false }
		] });
		expect(field('Connection').value).toBe('');
		expect(screen.queryByRole('option', { name: /SSH/ })).toBeNull();
		await fireEvent.change(field('Connection'), { target: { value: 'cf' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue to review' }));
		expect(screen.queryByLabelText('API key or token')).toBeNull();
		expect(screen.getByText('Read-only · GET and HEAD only')).toBeTruthy();
		expect(screen.getByText(/Sent only to Owner PC through this worker/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('checkbox', { name: /I approve access/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledWith(endpoint, { connection: null, saved_id: 'cf', minutes: 60, save_connection: false });
	});

	it('refuses collection in insecure contexts and does not allow saving with an unavailable vault', async () => {
		Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
		mount({ vaultAvailable: false }); await review();
		expect(field('API key or token').disabled).toBe(true);
		expect((screen.getByRole('checkbox', { name: /Save this connection/ }) as HTMLInputElement).disabled).toBe(true);
		expect(screen.getByRole('alert').textContent).toMatch(/HTTPS connection/);
	});

	it('clears token and consent on access changes, back navigation, ownership change, and unmount', async () => {
		const { rerender, unmount } = mount(); await review(); await approve();
		await fireEvent.change(field('Access for'), { target: { value: '15' } });
		expect(field('API key or token').value).toBe('');
		await approve(); await rerender({ owner: 'Other PC' }); expect(field('API key or token').value).toBe('');
		await approve(); await fireEvent.click(screen.getByRole('button', { name: /Back/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'I have a token' })); expect(field('API key or token').value).toBe('');
		await approve(); unmount(); await tick(); mount(); await review(); expect(field('API key or token').value).toBe('');
	});
});
