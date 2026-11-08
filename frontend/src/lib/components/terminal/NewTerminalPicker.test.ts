import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	peerChallenge: vi.fn(), requestPeerLink: vi.fn(), deviceLinks: vi.fn(),
}));
const key = vi.hoisted(() => ({ problem: null as string | null }));
vi.mock('$lib/terminal/device-key', () => ({
	deviceKey: vi.fn(async () => ({ keyId: 'kid', publicKey: 'pub', code: 'ABCD2345EFGH', sign: async () => 'sig' })),
	deviceKeyProblem: () => key.problem,
	deviceLabel: () => 'Chrome on Linux',
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import NewTerminalPicker from './NewTerminalPicker.svelte';
import type { ProbeState } from '$lib/terminal/gating';
import type { TerminalTarget } from '$lib/api/terminals';
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
import { LINK_POLL_MS } from '$lib/stores/peer-terminals.svelte';

type Ready = Extract<ProbeState, { status: 'ready' }>;

const local: TerminalTarget = { kind: 'worker', worker_id: 'local', name: 'Local', available: true };
const ssh: TerminalTarget = { kind: 'ssh', grant_id: 'grant', name: 'Approved host', available: true };
const peer = (over: Partial<TerminalTarget> = {}): TerminalTarget => ({ kind: 'worker', worker_id: 'peer', name: 'Peer', available: true, ...over });
const probeWith = (targets: TerminalTarget[], over: Partial<Ready> = {}): Ready => ({
	status: 'ready', supported: true, reason: null, worker: 'Local', shells: ['bash', 'sh'],
	defaultShell: 'bash', folder: '/local/project', folderSource: 'project', open: 0, limit: 8, targets, ...over,
});
const linked = { status: 'linked', receiver: 'peer', via: 'local', challenge: 'c1', expires_in: 60 };
const unlinked = { status: 'unlinked', receiver: 'peer', via: 'local' };
const waiting = { status: 'pending', receiver: 'peer', via: 'local', expires_in: 590 };

beforeEach(() => { key.problem = null; peerTerminals.reset(); Object.values(api).forEach((fn) => fn.mockReset()); });
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
		const off = { available: false };
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer(),
			peer({ ...off, worker_id: 'off', name: 'Laptop', reason_code: 'peer_offline', reason: 'The peer worker is offline.' }),
			peer({ ...off, worker_id: 'dis', name: 'Nas', reason_code: 'peer_terminals_disabled', reason: 'Turn on Terminals on other workers in Settings.' }),
			ssh]), onOpen: vi.fn() });
		const group = ui.getByRole('group', { name: 'Target' });
		expect((within(group).getByRole('radio', { name: 'Peer, Peer worker, Ready' }) as HTMLInputElement).disabled).toBe(false);
		const offline = within(group).getByRole('radio', { name: 'Laptop, Peer worker, Offline' }) as HTMLInputElement;
		expect(offline.disabled).toBe(true);
		expect(document.getElementById(offline.getAttribute('aria-describedby')!)!.textContent).toBe('The peer worker is offline.');
		expect((within(group).getByRole('radio', { name: 'Nas, Peer worker, Turned off' }) as HTMLInputElement).disabled).toBe(true);
		expect(ui.getByText('Turn on Terminals on other workers in Settings.')).toBeTruthy();
		expect((within(group).getByRole('radio', { name: 'Local, This worker' }) as HTMLInputElement).checked).toBe(true);
		expect(within(group).getByRole('radio', { name: 'Approved host, SSH grant' })).toBeTruthy();
	});

	it('does not silently retarget a selection when the target list changes', async () => {
		const onOpen = vi.fn();
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer(), ssh]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await ui.rerender({ probe: probeWith([local, ssh]), onOpen });
		expect(openButton(ui).disabled).toBe(true);
		expect(onOpen).not.toHaveBeenCalled();
	});

	it("starts on the chat's own worker, leaving this worker's folder out of it", () => {
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()], { chatWorkerId: 'peer' }), chatId: 'chat-1', onOpen: vi.fn() });
		expect((ui.getByRole('radio', { name: /^Peer/ }) as HTMLInputElement).checked).toBe(true);
		expect((ui.getByRole('textbox') as HTMLInputElement).value).toBe('');
	});

	it("disables a reachable worker where this browser can't hold a key, and says why", () => {
		const reason = 'This browser needs a secure connection.';
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()], { deviceKeyProblem: reason }), onOpen: vi.fn() });
		const radio = ui.getByRole('radio', { name: 'Peer, Peer worker, Needs a secure connection' }) as HTMLInputElement;
		expect(radio.disabled).toBe(true);
		expect(ui.getByText(reason)).toBeTruthy();
		expect((ui.getByRole('radio', { name: 'Local, This worker' }) as HTMLInputElement).disabled).toBe(false);
	});
});

describe('opening on a peer worker', () => {
	async function pick(probe: Ready = probeWith([local, peer()]), extra: Record<string, unknown> = {}) {
		const onOpen = vi.fn(async () => ({ ok: true as const }));
		const ui = render(NewTerminalPicker, { probe, onOpen, ...extra });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		return { ui, onOpen };
	}

	it("signs the open for a linked device, with this chat's id on the chat's own worker", async () => {
		api.peerChallenge.mockResolvedValue(linked);
		const { ui, onOpen } = await pick(probeWith([local, peer()], { chatWorkerId: 'peer' }), { chatId: 'chat-1' });
		await fireEvent.click(openButton(ui));
		await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
		const choice = (onOpen.mock.calls[0] as unknown[])[0] as { cwd: null; shell: null; target: { worker_id: string; proof: { payload: string; key_id: string; signature: string } } };
		expect(choice.target.worker_id).toBe('peer');
		expect(choice.target.proof.key_id).toBe('kid');
		expect(JSON.parse(choice.target.proof.payload)).toMatchObject({ action: 'terminal.open', receiver: 'peer', via: 'local', challenge: 'c1',
			target: { session_id: 'chat-1', cwd: null, shell: null } });
	});

	it('never signs a chat id for a worker that is not the chat\'s own', async () => {
		api.peerChallenge.mockResolvedValue(linked);
		const { ui, onOpen } = await pick(probeWith([local, peer()]), { chatId: 'chat-1' });
		await fireEvent.click(openButton(ui));
		await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
		const choice = (onOpen.mock.calls[0] as unknown[])[0] as { target: { proof: { payload: string } } };
		expect(JSON.parse(choice.target.proof.payload).target.session_id).toBeNull();
	});

	it('links an unknown device with the code worked out here, then opens without another click', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValueOnce(waiting).mockResolvedValue(linked);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'kid', expires_in: 600 });
		const { ui, onOpen } = await pick();
		await fireEvent.click(openButton(ui));
		const status = await ui.findByRole('status');
		expect(within(status).getByText('Link this device to Peer')).toBeTruthy();
		expect(within(status).getByRole('img', { name: /Link code for Peer/ })).toBeTruthy();
		expect(api.requestPeerLink).toHaveBeenCalledWith('peer', 'pub', 'Chrome on Linux');
		expect(JSON.stringify(api.requestPeerLink.mock.calls)).not.toContain('ABCD2345EFGH');
		await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1), { timeout: LINK_POLL_MS * 3 });
		await waitFor(() => expect(ui.queryByRole('status')).toBeNull());
	}, 15_000);

	it('leaves the code to the panel when the panel already shows this link', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValue(waiting);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'kid', expires_in: 600 });
		void peerTerminals.authorize('peer', 'Peer', 'terminal.open', { session_id: null, cwd: null, shell: null });
		await waitFor(() => expect(peerTerminals.flow('peer')?.phase).toBe('linking'));
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen: vi.fn(), linkCardShownFor: 'peer' });
		expect(ui.queryByRole('status')).toBeNull();
		expect((ui.getByRole('radio', { name: /^Peer/ }) as HTMLInputElement).checked).toBe(true);
		peerTerminals.cancel('peer');
	});

	it('opens nothing after Stop waiting', async () => {
		api.peerChallenge.mockResolvedValueOnce(unlinked).mockResolvedValue(waiting);
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'kid', expires_in: 600 });
		const { ui, onOpen } = await pick();
		await fireEvent.click(openButton(ui));
		await fireEvent.click(await ui.findByRole('button', { name: 'Stop waiting' }));
		api.peerChallenge.mockResolvedValue(linked);
		await new Promise((resolve) => setTimeout(resolve, LINK_POLL_MS + 200));
		expect(onOpen).not.toHaveBeenCalled();
		expect(peerTerminals.flow('peer')).toBeNull();
	}, 10_000);

	it('keeps the folder field and the link when the peer rejects the folder', async () => {
		api.peerChallenge.mockResolvedValue(linked);
		const onOpen = vi.fn(async () => ({ ok: false as const, code: 'bad_folder', message: "that folder doesn't exist." }));
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.input(ui.getByRole('textbox'), { target: { value: '/nope' } });
		await fireEvent.click(openButton(ui));
		const alert = await ui.findByRole('alert');
		expect(alert.textContent).toContain("Peer: that folder doesn't exist.");
		expect(peerTerminals.flow('peer')).toBeNull();
	});

	it('explains any other refused open and offers to try again', async () => {
		api.peerChallenge.mockResolvedValue(linked);
		const onOpen = vi.fn(async () => ({ ok: false as const, code: 'device_link_required', message: 'x' }));
		const ui = render(NewTerminalPicker, { probe: probeWith([local, peer()]), onOpen });
		await fireEvent.click(ui.getByRole('radio', { name: /^Peer/ }));
		await fireEvent.click(openButton(ui));
		const alert = await ui.findByRole('alert');
		expect(alert.textContent).toContain('This device is no longer linked to Peer.');
		expect(ui.getByRole('button', { name: 'Try again' })).toBeTruthy();
	});
});
