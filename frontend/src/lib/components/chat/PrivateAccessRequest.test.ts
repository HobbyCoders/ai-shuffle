import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const m = vi.hoisted(() => ({ post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: m,
	getErrorMessage: (e: { detail?: string }, fallback: string) => e?.detail ?? fallback
}));
import PrivateAccessRequest from './PrivateAccessRequest.svelte';

const saved = [
	{ id: 'lab', label: 'Lab box', target: 'root@10.0.0.9:22', fingerprint: 'SHA256:lab' },
	{ id: 'build', label: 'Build box', target: 'ops@10.0.0.5:22', fingerprint: 'SHA256:build' }
];
const inAnHour = () => Math.floor(Date.now() / 1000) + 3600;
function mount(overrides: Record<string, unknown> = {}) {
	const onComplete = vi.fn();
	const props = {
		sessionId: 's 1',
		request: { id: 'r/1', kind: 'machine', purpose: 'Check disk usage on 10.0.0.5', expires_at: inAnHour() },
		savedConnections: saved,
		vaultAvailable: true,
		onComplete,
		...overrides
	};
	render(PrivateAccessRequest, props);
	return { onComplete };
}
const endpoint = '/chat-access/s%201/requests/r%2F1';
const field = (name: string) => screen.getByLabelText(name) as HTMLInputElement;

beforeEach(() => {
	vi.clearAllMocks();
	Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
	m.post.mockResolvedValue({});
	m.delete.mockResolvedValue({});
});
afterEach(cleanup);

describe('PrivateAccessRequest', () => {
	it('offers the saved server named in the request and grants it without sending credentials', async () => {
		const { onComplete } = mount();
		expect((screen.getByLabelText('Connection') as HTMLSelectElement).value).toBe('build');
		expect(screen.getByText('ops@10.0.0.5:22')).toBeTruthy();
		expect(screen.queryByText('Save this connection')).toBeNull();
		await fireEvent.change(screen.getByLabelText('Access for'), { target: { value: '240' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledWith(endpoint, { connection: null, saved_id: 'build', minutes: 240, save_connection: false });
	});

	it('keeps the full saved host key in an initially collapsed disclosure', async () => {
		const fingerprint = `SHA256:${'a'.repeat(64)}`;
		mount({ savedConnections: [{ ...saved[1], fingerprint }] });
		const summary = screen.getByText('Host-key fingerprint');
		const disclosure = summary.closest('details')!;
		expect(disclosure.open).toBe(false);
		expect(disclosure.querySelector('code')?.textContent).toBe(fingerprint);
		await fireEvent.click(summary);
		expect(disclosure.open).toBe(true);
		expect(screen.getByText('ops@10.0.0.5:22')).toBeTruthy();
	});

	it('sends a new connection with save_connection and wipes the password from the form', async () => {
		let finish!: () => void;
		m.post.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
		const { onComplete } = mount();
		await fireEvent.change(screen.getByLabelText('Connection'), { target: { value: '' } });
		await fireEvent.input(field('Server IP address'), { target: { value: ' 10.0.0.7 ' } });
		await fireEvent.input(field('Username'), { target: { value: ' deploy ' } });
		await fireEvent.input(field('Password'), { target: { value: 'hunter2' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Save this connection/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect(m.post).toHaveBeenCalledWith(endpoint, {
			connection: {
				kind: 'machine', label: 'SSH server', minutes: 60, path: '', writable: false, host: '10.0.0.7', port: 22,
				username: 'deploy', fingerprint: '', password: 'hunter2', private_key: '', passphrase: ''
			},
			saved_id: '', minutes: 60, save_connection: true
		});
		// The secret leaves the form as soon as it is sent, and the card shows it is busy.
		expect(field('Password').value).toBe('');
		expect((screen.getByRole('button', { name: 'Connecting…' }) as HTMLButtonElement).disabled).toBe(true);
		finish();
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		// The body the request carried is scrubbed afterwards too.
		expect(m.post.mock.calls[0][1].connection.password).toBe('');
	});

	it('uses a private key instead of the password and pins a typed host key', async () => {
		mount({ savedConnections: [] });
		await fireEvent.input(field('Server IP address'), { target: { value: '10.0.0.7' } });
		await fireEvent.input(field('Username'), { target: { value: 'deploy' } });
		await fireEvent.click(screen.getByRole('button', { name: /Use a private key/ }));
		expect(screen.queryByLabelText('Password')).toBeNull();
		await fireEvent.input(screen.getByLabelText('Private key'), { target: { value: 'KEY' } });
		await fireEvent.click(screen.getByRole('button', { name: /Verify the host key/ }));
		await fireEvent.input(field('Host-key fingerprint'), { target: { value: ' SHA256:abc ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await waitFor(() => expect(m.post).toHaveBeenCalledOnce());
		const { connection } = m.post.mock.calls[0][1];
		expect(connection).toMatchObject({ password: '', fingerprint: 'SHA256:abc' });
	});

	it('cannot save a connection while the vault is locked', () => {
		mount({ savedConnections: [], vaultAvailable: false });
		expect((screen.getByRole('checkbox', { name: /Save this connection/ }) as HTMLInputElement).disabled).toBe(true);
		expect(screen.getByText(/Secure storage is locked or unavailable/)).toBeTruthy();
	});

	it('grants a writable folder with the chosen duration', async () => {
		const { onComplete } = mount({ request: { id: 'r/1', kind: 'folder', purpose: 'Read my notes', expires_at: inAnHour() } });
		expect(screen.queryByLabelText('Connection')).toBeNull();
		await fireEvent.input(field('Folder on this worker'), { target: { value: '/home/me/notes' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Allow text writes/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post.mock.calls[0][1]).toMatchObject({ saved_id: '', save_connection: false, connection: { kind: 'folder', label: 'Folder', path: '/home/me/notes', writable: true } });
	});

	it('collects an HTTPS service credential privately and only offers saved services', async () => {
		let finish!: () => void;
		m.post.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
		const { onComplete } = mount({ request: { id: 'r/1', kind: 'service', purpose: 'Query the inventory API', expires_at: inAnHour() } });
		// Saved SSH machines are not credentials for an API service.
		expect(screen.queryByLabelText('Connection')).toBeNull();
		expect(screen.getByText('API access')).toBeTruthy();
		await fireEvent.input(field('Service address'), { target: { value: ' https://api.example.com/v1 ' } });
		await fireEvent.input(field('Credential header'), { target: { value: 'X-API-Key' } });
		await fireEvent.input(field('Scheme'), { target: { value: '' } });
		await fireEvent.input(field('API key or token'), { target: { value: 'api-key-0123456789' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Allow changes/ }));
		await fireEvent.click(screen.getByRole('checkbox', { name: /Save this connection/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect(m.post).toHaveBeenCalledWith(endpoint, {
			connection: {
				kind: 'service', label: 'API service', minutes: 60, url: 'https://api.example.com/v1',
				auth_header: 'X-API-Key', auth_scheme: '', secret: 'api-key-0123456789', writable: true
			},
			saved_id: '', minutes: 60, save_connection: true
		});
		expect(field('API key or token').value).toBe('');
		finish();
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post.mock.calls[0][1].connection.secret).toBe('');
	});

	it('reuses a saved service named in the request without resending the credential', async () => {
		const services = [
			...saved,
			{ id: 'grafana', label: 'Grafana', target: 'https://grafana.lan.example/api', fingerprint: '', kind: 'service', writable: false },
			{ id: 'stock', label: 'Stock', target: 'https://stock.example.com', fingerprint: '', kind: 'service', writable: true }
		];
		const { onComplete } = mount({
			request: { id: 'r/1', kind: 'service', purpose: 'Check stock.example.com levels', expires_at: inAnHour() },
			savedConnections: services
		});
		const select = screen.getByLabelText('Connection') as HTMLSelectElement;
		expect(select.value).toBe('stock');
		expect([...select.options].map(o => o.value)).toEqual(['', 'grafana', 'stock']);
		expect(screen.getByText('Allows changes after approval')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledWith(endpoint, { connection: null, saved_id: 'stock', minutes: 60, save_connection: false });
	});

	it('declines through DELETE and shows a failure without completing', async () => {
		m.delete.mockRejectedValueOnce({ detail: 'Request already answered' });
		const { onComplete } = mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Decline' }));
		expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Request already answered');
		expect(onComplete).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Decline' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.delete).toHaveBeenLastCalledWith(endpoint);
	});

	it('shows a grant failure and keeps the card open', async () => {
		m.post.mockRejectedValueOnce({ detail: 'Host key mismatch' });
		const { onComplete } = mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect((await screen.findByRole('alert')).textContent).toBe('Host key mismatch');
		expect(onComplete).not.toHaveBeenCalled();
	});

	it('refuses to collect secrets outside a secure context or after the request expired', async () => {
		Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
		mount();
		expect((await screen.findByRole('alert')).textContent).toMatch(/HTTPS connection/);
		expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(true);
		cleanup();
		Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
		mount({ request: { id: 'r/1', kind: 'machine', purpose: 'x', expires_at: Math.floor(Date.now() / 1000) - 5 } });
		expect(screen.getByRole('timer').textContent).toBe('Expired');
		const connect = screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement;
		expect(connect.disabled).toBe(true);
		await fireEvent.submit(connect.form!);
		expect(m.post).not.toHaveBeenCalled();
	});
});
