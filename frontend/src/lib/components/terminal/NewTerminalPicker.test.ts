import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	requestPeer: vi.fn(), peerAuthorization: vi.fn(), revokePeerAuthorization: vi.fn(),
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import NewTerminalPicker from './NewTerminalPicker.svelte';
import type { ProbeState } from '$lib/terminal/gating';
import { TerminalApiError, type TerminalTarget } from '$lib/api/terminals';
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';

type Ready = Extract<ProbeState, { status: 'ready' }>;

const LEASE = 'lease_'.padEnd(28, 'x');
const local: TerminalTarget = { kind: 'worker', worker_id: 'local', name: 'Local', available: true };
const ssh: TerminalTarget = { kind: 'ssh', grant_id: 'grant', name: 'Approved host', available: true };
const peer = (over: Partial<TerminalTarget> = {}): TerminalTarget => ({
	kind: 'worker', worker_id: 'peer', name: 'Peer', available: false,
	reason_code: 'recipient_approval_required', reason: 'The owner must approve this terminal directly on the receiving worker.', ...over,
});
const probeWith = (targets: TerminalTarget[]): Ready => ({
	status: 'ready', supported: true, reason: null, worker: 'Local', shells: ['bash', 'sh'],
	defaultShell: 'bash', folder: '/local/project', folderSource: 'project', open: 0, limit: 8, targets,
});
const pending = (over: Record<string, unknown> = {}) => ({
	authorization_id: LEASE, worker_id: 'peer', request_id: 'r1', status: 'pending', used: false, expires_in: 120,
	confirmation_code: 'K7Q2XM', source_name: 'Local', age_seconds: 0, ...over,
});

beforeEach(() => { peerTerminals.reset(); Object.values(api).forEach((fn) => fn.mockReset()); });
afterEach(() => { cleanup(); peerTerminals.reset(); });

const openButton = (ui: { getByRole: (role: string, options: { name: string }) => HTMLElement }) => ui.getByRole('button', { name: 'Open terminal' }) as HTMLButtonElement;

describe('without peer workers (peer terminals off: the picker as before)', () => {
	it('shows no target choice for this worker alone', () => {
		const ui = render(NewTerminalPicker, { probe: probeWith([local]), onOpen: vi.fn() });
		expect(ui.queryByLabelText('Target')).toBeNull();
		expect(ui.container.querySelector('input[name="terminal-target"]')).toBeNull();
		expect(ui.queryByText(/approv/i)).toBeNull();
		expect((ui.getByRole('textbox') as HTMLInputElement).value).toBe('/local/project');
		expect(openButton(ui).disabled).toBe(false);
	});

	it('keeps the Target dropdown for SSH grants and never asks for credentials or a command', async () => {
		const onOpen = vi.fn();
		const ui = render(NewTerminalPicker, { probe: probeWith([local, ssh]), onOpen });
		expect(ui.getByLabelText('Target').tagName).toBe('SELECT');
		await fireEvent.change(ui.getByLabelText('Target'), { target: { value: 'ssh:grant' } });
		expect(ui.queryByRole('textbox')).toBeNull();
		expect(ui.getByText(/Revoking the grant closes/)).toBeTruthy();
		await fireEvent.click(openButton(ui));
		expect(onOpen).toHaveBeenCalledWith({ cwd: null, shell: null, target: { kind: 'ssh', grant_id: 'grant', worker_id: undefined } });
	});
});

describe('target selection with peer workers', () => {
	it("lists every worker with its availability and the reason it can't be used", () => {
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer(),
			peer({ worker_id: 'off', name: 'Laptop', reason_code: 'peer_offline', reason: 'The peer worker is offline.' }),
			peer({ worker_id: 'dis', name: 'Nas', reason_code: 'peer_terminals_disabled', reason: 'Enable NETWORK_PEER_TERMINALS on both workers.' }),
			ssh]), onOpen: vi.fn() });
		const group = ui.getByRole('group', { name: 'Target' });
		const needs = within(group).getByRole('radio', { name: 'Peer, Peer worker, Needs approval' }) as HTMLInputElement;
		expect(needs.disabled).toBe(false);
		expect(ui.getByText('Approve on Peer to open one shell.')).toBeTruthy();
		const offline = within(group).getByRole('radio', { name: 'Laptop, Peer worker, Offline' }) as HTMLInputElement;
		expect(offline.disabled).toBe(true);
		expect(offline.getAttribute('aria-describedby')).toBeTruthy();
		expect(document.getElementById(offline.getAttribute('aria-describedby')!)!.textContent).toBe('The peer worker is offline.');
		expect((within(group).getByRole('radio', { name: 'Nas, Peer worker, Turned off' }) as HTMLInputElement).disabled).toBe(true);
		expect(ui.getByText('Enable NETWORK_PEER_TERMINALS on both workers.')).toBeTruthy();
		expect((within(group).getByRole('radio', { name: 'Local, This worker' }) as HTMLInputElement).checked).toBe(true);
		expect(within(group).getByRole('radio', { name: 'Approved host, SSH grant' })).toBeTruthy();
	});

	it('does not silently retarget a selection when the target list changes', async () => {
		const onOpen = vi.fn();
		const approved = peer({ available: true, reason_code: null, reason: null, authorization_id: LEASE });
		const ui = render(NewTerminalPicker, { probe: probeWith([local, approved, ssh]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await ui.rerender({ probe: probeWith([local, ssh]), onOpen });
		expect(openButton(ui).disabled).toBe(true);
		expect(onOpen).not.toHaveBeenCalled();
	});
});

describe('requesting a peer shell', () => {
	async function requested(result: Record<string, unknown> = pending()) {
		api.requestPeer.mockResolvedValue(result);
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen: vi.fn(), onCancel: vi.fn() });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		expect(ui.queryByRole('textbox')).toBeNull();
		expect(ui.getByText(/must approve this shell on Peer itself/)).toBeTruthy();
		await fireEvent.click(ui.getByRole('button', { name: 'Request approval' }));
		expect(api.requestPeer).toHaveBeenCalledWith('peer');
		return ui;
	}

	it('shows the code, the target worker and the countdown while waiting', async () => {
		const ui = await requested();
		const status = await ui.findByRole('status');
		expect(within(status).getByText('Waiting for approval on Peer')).toBeTruthy();
		expect(within(status).getByRole('img', { name: 'Code for Peer: K 7 Q, 2 X M' })).toBeTruthy();
		expect(within(status).getByText('2:00')).toBeTruthy();
		expect(status.textContent).toContain('approve the request only if it shows this same code');
		expect(ui.queryByRole('button', { name: 'Open terminal' })).toBeNull();
		expect(ui.getByRole('button', { name: 'Cancel the request to Peer' })).toBeTruthy();
	});

	it('opens once approved, on the peer, with no local folder or shell', async () => {
		const onOpen = vi.fn().mockResolvedValue({ ok: true });
		api.requestPeer.mockResolvedValue(pending());
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.click(ui.getByRole('button', { name: 'Request approval' }));
		await ui.findByText('Waiting for approval on Peer');
		api.peerAuthorization.mockResolvedValue(pending({ status: 'approved', expires_in: 600 }));
		await peerTerminals.poll('peer');
		expect(api.peerAuthorization).toHaveBeenCalledWith(LEASE);
		expect(await ui.findByText(/Approved on Peer for one shell/)).toBeTruthy();
		expect((ui.getByRole('textbox') as HTMLInputElement).value).toBe('');
		await fireEvent.click(openButton(ui));
		expect(onOpen).toHaveBeenCalledWith({ cwd: null, shell: null, target: { kind: 'worker', worker_id: 'peer', authorization_id: LEASE } });
		await waitFor(() => expect(peerTerminals.flow('peer')).toBeNull());
	});

	it('says the request ended when the other worker lets it expire or denies it', async () => {
		const ui = await requested();
		api.peerAuthorization.mockResolvedValue({ authorization_id: LEASE, worker_id: 'peer', status: 'expired', reason_code: 'authorization_expired' });
		await peerTerminals.poll('peer');
		const alert = await ui.findByRole('alert');
		expect(within(alert).getByText('No approval from Peer')).toBeTruthy();
		expect(alert.textContent).toContain('expired, or it was denied or revoked on Peer');
		expect((ui.getByRole('button', { name: 'Request again' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('treats a refused status check (403) as an ended request', async () => {
		const ui = await requested();
		api.peerAuthorization.mockRejectedValue(new TerminalApiError('Owner authorization required', 403, 'http_403'));
		await peerTerminals.poll('peer');
		expect((await ui.findByRole('alert')).textContent).toContain('no longer valid');
	});

	it('keeps waiting, and says so, when the other worker stops answering', async () => {
		const ui = await requested();
		api.peerAuthorization.mockRejectedValue(new TerminalApiError('The peer is unavailable or refused terminal access.', 503, 'peer_unavailable'));
		await peerTerminals.poll('peer');
		expect(await ui.findByText("Can't reach Peer right now. Still waiting…")).toBeTruthy();
		expect(ui.getByText('Waiting for approval on Peer')).toBeTruthy();
	});

	it('cancels a pending request on the server', async () => {
		const ui = await requested();
		api.revokePeerAuthorization.mockResolvedValue({ revoked: true });
		await fireEvent.click(await ui.findByRole('button', { name: 'Cancel the request to Peer' }));
		expect(api.revokePeerAuthorization).toHaveBeenCalledWith(LEASE);
		expect(await ui.findByRole('button', { name: 'Request approval' })).toBeTruthy();
	});

	it.each([
		[new TerminalApiError('The peer needs an authenticated HTTPS route.', 503, 'https_required'), 'Peer needs an authenticated HTTPS route'],
		[new TerminalApiError('The peer worker is offline.', 409, 'peer_offline'), 'The peer worker is offline.'],
		[new TerminalApiError('Unknown peer worker', 404, 'http_404'), 'no longer a known worker'],
		[new TerminalApiError('Enable NETWORK_PEER_TERMINALS on both workers.', 409, 'peer_terminals_disabled'), 'Peer terminals are turned off'],
	])('explains a refused request (%s)', async (error, text) => {
		api.requestPeer.mockRejectedValue(error);
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen: vi.fn() });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.click(ui.getByRole('button', { name: 'Request approval' }));
		const alert = await ui.findByRole('alert');
		expect(within(alert).getByText("Couldn't request a shell on Peer")).toBeTruthy();
		expect(alert.textContent).toContain(text);
		expect(ui.getByRole('button', { name: 'Request again' })).toBeTruthy();
	});

	it('does not offer to open when the recipient answers unavailable', async () => {
		const ui = await requested({ ...pending(), status: 'unavailable', available: false, reason_code: 'peer_terminals_disabled',
			reason: 'Enable NETWORK_PEER_TERMINALS on both workers.' });
		const alert = await ui.findByRole('alert');
		expect(within(alert).getByText('Peer is unavailable')).toBeTruthy();
		expect(ui.queryByRole('button', { name: 'Open terminal' })).toBeNull();
		expect((ui.getByRole('button', { name: 'Request again' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('keeps the approval when the peer rejects the folder, so the owner can fix it and open again', async () => {
		const onOpen = vi.fn()
			.mockResolvedValueOnce({ ok: false, code: 'bad_folder', message: "The folder /nope doesn't exist on this worker." })
			.mockResolvedValueOnce({ ok: true });
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer({ available: true, reason_code: null, reason: null, authorization_id: LEASE })]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		expect(ui.getByText('Approved for one shell. Open it below.')).toBeTruthy();
		const box = ui.getByRole('textbox') as HTMLInputElement;
		await fireEvent.input(box, { target: { value: '/nope' } });
		await fireEvent.click(openButton(ui));
		expect(onOpen).toHaveBeenLastCalledWith({ cwd: '/nope', shell: null, target: { kind: 'worker', worker_id: 'peer', authorization_id: LEASE } });
		const alert = await ui.findByRole('alert');
		expect(alert.textContent).toContain("Peer: The folder /nope doesn't exist on this worker.");
		expect(alert.textContent).toContain('the approval still stands');
		expect(box.getAttribute('aria-invalid')).toBe('true');
		expect(ui.queryByRole('button', { name: /Request/ })).toBeNull();
		expect(openButton(ui).disabled).toBe(true);
		await fireEvent.input(box, { target: { value: '/workspace' } });
		expect(openButton(ui).disabled).toBe(false);
		await fireEvent.click(openButton(ui));
		expect(onOpen).toHaveBeenLastCalledWith({ cwd: '/workspace', shell: null, target: { kind: 'worker', worker_id: 'peer', authorization_id: LEASE } });
	});

	it('explains any other refused open and allows a new request', async () => {
		const onOpen = vi.fn().mockResolvedValue({ ok: false, code: 'peer_unavailable', message: 'The terminal worker is unavailable or owner access expired.' });
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer({ available: true, reason_code: null, reason: null, authorization_id: LEASE })]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.click(openButton(ui));
		const alert = await ui.findByRole('alert');
		expect(within(alert).getByText("The shell on Peer didn't open")).toBeTruthy();
		expect(alert.textContent).toContain('Peer stopped answering before the shell opened.');
	});

	it('revokes an unused approval', async () => {
		api.requestPeer.mockResolvedValue(pending({ status: 'approved', expires_in: 600 }));
		api.revokePeerAuthorization.mockResolvedValue({ revoked: true });
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen: vi.fn() });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.click(ui.getByRole('button', { name: 'Request approval' }));
		await fireEvent.click(await ui.findByRole('button', { name: 'Revoke the approval on Peer' }));
		expect(api.revokePeerAuthorization).toHaveBeenCalledWith(LEASE);
	});

	it('revokes an approval the worker reported (after a reload) with no flow in memory', async () => {
		api.revokePeerAuthorization.mockResolvedValue({ revoked: true });
		const onTargetsChanged = vi.fn();
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer({ available: true, reason_code: null, reason: null, authorization_id: LEASE })]),
			onOpen: vi.fn(), onTargetsChanged });
		await fireEvent.click(ui.getByRole('radio', { name: 'Peer, Peer worker, Approved' }));
		expect(peerTerminals.flow('peer')).toBeNull();
		await fireEvent.click(ui.getByRole('button', { name: 'Revoke the approval on Peer' }));
		await waitFor(() => expect(api.revokePeerAuthorization).toHaveBeenCalledTimes(1));
		expect(api.revokePeerAuthorization).toHaveBeenCalledWith(LEASE);
		expect(onTargetsChanged).toHaveBeenCalledTimes(1);
		expect(await ui.findByRole('radio', { name: 'Peer, Peer worker, Needs approval' })).toBeTruthy();
		expect(ui.queryByRole('button', { name: 'Open terminal' })).toBeNull();
		expect(ui.getByRole('button', { name: 'Request approval' })).toBeTruthy();
	});

	it('returns to a pending request when the picker opens again', async () => {
		await requested();
		cleanup();
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen: vi.fn() });
		expect((ui.getByRole('radio', { name: /^Peer/ }) as HTMLInputElement).checked).toBe(true);
		expect(await ui.findByText('Waiting for approval on Peer')).toBeTruthy();
	});
});
