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
		request: { id: 'r/1', kind: 'machine', purpose: 'Check disk usage on 10.0.0.5', expires_at: inAnHour(), suggested_host: '10.0.0.5', suggested_port: 22, suggested_username: 'ops' },
		savedConnections: saved,
		vaultAvailable: true,
		onComplete,
		...overrides
	};
	const rendered = render(PrivateAccessRequest, props);
	return { onComplete, ...rendered };
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
	it('offers the saved server matching the structured hints and grants it without sending credentials', async () => {
		const { onComplete } = mount();
		expect((screen.getByLabelText('Connection') as HTMLSelectElement).value).toBe('build');
		expect(screen.getByText('ops@10.0.0.5:22')).toBeTruthy();
		expect(screen.queryByText('Save this connection')).toBeNull();
		await fireEvent.change(screen.getByLabelText('Access for'), { target: { value: '240' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post).toHaveBeenCalledWith(endpoint, { connection: null, saved_id: 'build', minutes: 240, save_connection: false });
	});

	it.each([
		{ suggested_host: '10.0.0.146', suggested_port: 22, suggested_username: 'x' },
		{ suggested_host: '10.0.0.5', suggested_port: 2222, suggested_username: 'ops' },
		{ suggested_host: '10.0.0.5', suggested_port: 22, suggested_username: 'root' },
		{ suggested_host: '10.0.0.50' },
		{ suggested_port: 22, suggested_username: 'ops' },
		{}
	])('defaults to new rather than an unrelated saved connection for %j', hints => {
		mount({ request: { id: 'r/1', kind: 'machine', purpose: 'Check 10.0.0.5 and 10.0.0.9', expires_at: inAnHour(), ...hints } });
		expect((screen.getByLabelText('Connection') as HTMLSelectElement).value).toBe('');
		expect(field('Server IP address').value).toBe(hints.suggested_host ?? '');
		expect(field('Port').value).toBe(String(hints.suggested_port ?? 22));
		expect(field('Username').value).toBe(hints.suggested_username ?? '');
		expect(field('Password').value).toBe('');
	});

	it.each(['10.0.0.5', 'BUILD.Example.', '2001:db8::5'])('matches only the host when port and user are omitted: %s', host => {
		mount({
			request: { id: 'r/1', kind: 'machine', purpose: 'Inspect server', expires_at: inAnHour(), suggested_host: host },
			savedConnections: [{ ...saved[1], target: `ops@${host.toLowerCase().replace(/\.$/, '')}:2222` }]
		});
		expect((screen.getByLabelText('Connection') as HTMLSelectElement).value).toBe('build');
	});

	it('matches canonical IPv6 saved targets against expanded literals', () => {
		mount({
			request: { id: 'r/1', kind: 'machine', purpose: 'Inspect server', expires_at: inAnHour(), suggested_host: '2001:0db8:0:0:0:0:0:5', suggested_port: 2222, suggested_username: 'ops' },
			savedConnections: [{ ...saved[1], target: 'ops@2001:db8::5:2222' }]
		});
		expect((screen.getByLabelText('Connection') as HTMLSelectElement).value).toBe('build');
	});

	it('prefills editable new-machine fields and submits the reviewed values', async () => {
		mount({ request: { id: 'r/1', kind: 'machine', purpose: 'Inspect VM', expires_at: inAnHour(), suggested_host: '10.0.0.146', suggested_port: 2222, suggested_username: 'x' } });
		expect(field('Server IP address').value).toBe('10.0.0.146');
		expect(field('Port').value).toBe('2222');
		expect(field('Username').value).toBe('x');
		await fireEvent.input(field('Server IP address'), { target: { value: '10.0.0.147' } });
		await fireEvent.input(field('Port'), { target: { value: '22' } });
		await fireEvent.input(field('Username'), { target: { value: 'deploy' } });
		await fireEvent.input(field('Password'), { target: { value: 'card-only' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		expect(m.post).toHaveBeenCalledWith(endpoint, expect.objectContaining({ saved_id: '', connection: expect.objectContaining({ host: '10.0.0.147', port: 22, username: 'deploy' }) }));
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
		// Folders are confined on Linux and macOS workers, so the note names neither.
		expect(screen.getByText(/Kernel file confinement: no shell/)).toBeTruthy();
		expect(screen.queryByText(/Linux file confinement/)).toBeNull();
		await fireEvent.input(field('Folder on this worker'), { target: { value: '/home/me/notes' } });
		await fireEvent.click(screen.getByRole('checkbox', { name: /Allow text writes/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post.mock.calls[0][1]).toMatchObject({ saved_id: '', save_connection: false, connection: { kind: 'folder', label: 'Folder', path: '/home/me/notes', writable: true } });
	});

	it('prefills a proposed folder but preserves edits across refreshes and a failed approval', async () => {
		const request = { id: 'r/1', kind: 'folder', purpose: 'Test a folder', expires_at: inAnHour(), suggested_path: '/workspace/test' };
		const { rerender, onComplete } = mount({ owner: 'Unraid', request });
		expect(field('Folder on Unraid').value).toBe('/workspace/test');
		expect(m.post).not.toHaveBeenCalled();
		expect(screen.getByText(/mounted container path/)).toBeTruthy();
		await fireEvent.input(field('Folder on Unraid'), { target: { value: '/workspace/private-choice' } });
		await rerender({ request: { ...request } });
		expect(field('Folder on Unraid').value).toBe('/workspace/private-choice');
		m.post.mockRejectedValueOnce({ detail: 'Folder not found on the worker that runs this chat.' });
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(screen.getByText(/Folder not found/)).toBeTruthy());
		expect(onComplete).not.toHaveBeenCalled();
		expect(field('Folder on Unraid').value).toBe('/workspace/private-choice');
		await fireEvent.input(field('Folder on Unraid'), { target: { value: '/workspace/corrected' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post.mock.calls[1][1].connection).toMatchObject({ path: '/workspace/corrected', writable: false });
	});

	it('names the owning device for a mirrored conversation and posts to this worker', async () => {
		const { onComplete } = mount({ owner: 'Owner PC', savedConnections: [], request: { id: 'r/1', kind: 'folder', purpose: 'Read my notes', expires_at: inAnHour() } });
		expect(screen.getByText(/Sent only to Owner PC through this worker/)).toBeTruthy();
		await fireEvent.input(field('Folder on Owner PC'), { target: { value: '/home/me/notes' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Grant access' }));
		await waitFor(() => expect(onComplete).toHaveBeenCalledOnce());
		expect(m.post.mock.calls[0][0]).toBe(endpoint);
		cleanup();
		mount({ owner: 'Owner PC', savedConnections: [] });
		expect(screen.getByText(/Encrypted on Owner PC for future requests/)).toBeTruthy();
	});

	it('routes service requests into guided preparation, not the credential form', () => {
		mount({ request: { id: 'r/1', kind: 'service', purpose: 'Read Cloudflare inventory', expires_at: inAnHour(), service_provider: 'cloudflare', suggested_url: 'https://api.cloudflare.com/client/v4' } });
		expect(screen.getByRole('region', { name: 'Private service connection' })).toBeTruthy();
		expect(field('Service address').value).toBe('https://api.cloudflare.com/client/v4');
		expect(screen.queryByLabelText('API key or token')).toBeNull();
		expect(m.post).not.toHaveBeenCalled();
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
