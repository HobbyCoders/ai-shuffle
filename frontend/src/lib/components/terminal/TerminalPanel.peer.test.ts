/**
 * TerminalPanel with peer terminals (#196): feature off leaves the panel as
 * it was, a peer shell names the worker it runs on and never offers an
 * in-place restart, a failed peer open is explained in the picker, and the
 * receiving worker lists devices waiting to link only when peer terminals are on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
	peerChallenge: vi.fn(), requestPeerLink: vi.fn(), deviceLinks: vi.fn(), approveDeviceLink: vi.fn(),
}));
vi.mock('$lib/terminal/device-key', () => ({
	deviceKey: vi.fn(async () => ({ keyId: 'kid', publicKey: 'pub', code: 'ABCD2345EFGH', sign: async () => 'sig' })),
	deviceKeyProblem: () => null,
	deviceLabel: () => 'Chrome on Linux',
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});
vi.mock('@xterm/xterm', () => ({
	Terminal: class {
		cols = 80; rows = 24; buffer = { active: { length: 0, getLine: () => null } };
		loadAddon() {} open(host: HTMLElement) { host.appendChild(document.createElement('textarea')); }
		onData() {} onSelectionChange() {} attachCustomKeyEventHandler() {} focus() {} write() {} reset() {} clear() {} dispose() {}
		hasSelection() { return false; } getSelection() { return ''; } clearSelection() {}
	},
}));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
vi.mock('@xterm/addon-search', () => ({ SearchAddon: class { findNext() { return false; } findPrevious() { return false; } clearDecorations() {} } }));
vi.mock('$lib/terminal/connection', () => ({
	TerminalConnection: class {
		constructor(private id: string, private handlers: { onStatus(s: string, m: string | null): void }) {}
		connect() { this.handlers.onStatus('live', null); }
		input() { return true; } resize() {} retry() {} dispose() {}
	},
}));

import TerminalPanel from './TerminalPanel.svelte';
import { TerminalApiError, type TerminalInfo, type TerminalTargets } from '$lib/api/terminals';
import { terminals } from '$lib/stores/terminals.svelte';
import { LINK_POLL_MS, peerTerminals } from '$lib/stores/peer-terminals.svelte';
import { sidePanel, sessionPanelKey } from '$lib/stores/side-panel.svelte';

const info = (id: string, over: Partial<TerminalInfo> = {}): TerminalInfo => ({
	id, target: { kind: 'worker', worker_id: 'w' }, shell: 'bash', cwd: '/workspace/app', folder_source: 'project', session_id: 's1',
	state: 'live', exit_code: null, created_at: '', exited_at: null, busy: false, cols: 80, rows: 24, attached: 0, ...over,
});
const base: TerminalTargets = {
	worker: { id: 'w', name: 'ServerWorker', platform: 'linux' }, supported: true, unsupported_reason: null,
	targets: [{ kind: 'worker', worker_id: 'w', name: 'ServerWorker', available: true }],
	shells: [{ name: 'bash', default: true }, { name: 'sh', default: false }], default_shell: 'bash',
	default_folder: { path: '/workspace/app', source: 'project' }, limit: 8, open: 0,
};
const withPeer = (over: Record<string, unknown> = {}): TerminalTargets => ({
	...base,
	targets: [...base.targets, { kind: 'worker', worker_id: 'dev_b', name: 'Desk', available: true, ...over }],
});
const linked = { status: 'linked', receiver: 'dev_b', via: 'w', challenge: 'c1', expires_in: 60 };
const provenTarget = expect.objectContaining({ kind: 'worker', worker_id: 'dev_b', proof: expect.objectContaining({ key_id: 'kid', signature: 'sig' }) });

beforeEach(() => {
	terminals.reset();
	peerTerminals.reset();
	sidePanel.close(sessionPanelKey('s1'));
	Object.values(api).forEach((fn) => fn.mockReset());
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	api.deviceLinks.mockResolvedValue({ links: [], requests: [] });
	api.peerChallenge.mockResolvedValue(linked);
});
afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	peerTerminals.reset();
});

async function mount(targets: TerminalTargets, listed: TerminalInfo[] = []) {
	api.targets.mockResolvedValue(targets);
	api.list.mockResolvedValue({ terminals: listed, open: listed.length, running: 0, limit: 8, worker: targets.worker });
	const view = render(TerminalPanel, { sessionId: 's1' });
	await waitFor(() => expect(terminals.tabsFor('s1').loaded && terminals.probeFor('s1').status === 'ready').toBe(true));
	return view;
}

describe('peer terminals off (the default)', () => {
	it('keeps the picker as it was and never asks for waiting devices', async () => {
		const view = await mount(base);
		const form = view.getByRole('form', { name: 'New terminal' });
		expect(form.querySelector('input[name="terminal-target"]')).toBeNull();
		expect(within(form).queryByLabelText('Target')).toBeNull();
		expect(within(form).queryByText(/link/i)).toBeNull();
		expect(within(form).getByRole('button', { name: 'Open terminal' })).toBeTruthy();
		expect(view.queryByRole('region', { name: 'Devices linked to ServerWorker' })).toBeNull();
		expect(api.deviceLinks).not.toHaveBeenCalled();
	});
});

describe('a shell on a peer worker', () => {
	it('names the peer worker in the status line, not this worker', async () => {
		const view = await mount(withPeer(),
			[info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner' })]);
		const status = view.container.querySelector('.status .meta')!;
		expect(status.textContent).toContain('Desk');
		expect(status.textContent).not.toContain('ServerWorker');
		expect(view.container.querySelector('.tab .tab-host')?.textContent).toBe('Desk');
	});

	it('does not offer Restart once the remote shell exits', async () => {
		const view = await mount(withPeer(), [info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, state: 'exited', exit_code: 0 })]);
		const ended = await view.findByRole('group', { name: 'Shell exited' });
		expect(within(ended).queryByRole('button', { name: /Restart/ })).toBeNull();
	});

	it('closes a peer shell through the same route as a local one, from the tab or the exited card', async () => {
		api.close.mockResolvedValue({ closed: true });
		const peerShell = { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner' };
		const view = await mount(withPeer(), [info('t1', peerShell), info('t2', { ...peerShell, state: 'exited', exit_code: 0 })]);
		expect(view.getAllByRole('tab').map((tab) => tab.getAttribute('title'))).toEqual(['bash · Desk: /home/owner', 'bash · Desk: /home/owner']);
		await fireEvent.click(view.getByRole('button', { name: 'Close terminal 1' }));
		await waitFor(() => expect(api.close).toHaveBeenCalledWith('t1'));
		await waitFor(() => expect(view.getAllByRole('tab')).toHaveLength(1));
		const ended = await view.findByRole('group', { name: 'Shell exited' });
		await fireEvent.click(within(ended).getByRole('button', { name: /Close/ }));
		await waitFor(() => expect(api.close).toHaveBeenCalledWith('t2'));
		await waitFor(() => expect(view.queryAllByRole('tab')).toHaveLength(0));
	});

	it('offers no Restart for a worker shell while this worker is not yet identified', async () => {
		const view = await mount({ ...base, targets: [] }, [info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, state: 'exited', exit_code: 0 })]);
		const ended = await view.findByRole('group', { name: 'Shell exited' });
		expect(within(ended).queryByRole('button', { name: /Restart/ })).toBeNull();
	});

	it('still offers Restart for a local shell', async () => {
		const view = await mount(withPeer(), [info('t1', { state: 'exited', exit_code: 0 })]);
		const ended = await view.findByRole('group', { name: 'Shell exited' });
		expect(within(ended).getByRole('button', { name: /Restart/ })).toBeTruthy();
	});

	it('shows a rejected folder in the picker and retries the same target with a new signature', async () => {
		const view = await mount(withPeer());
		api.create.mockRejectedValueOnce(new TerminalApiError("The folder /nope doesn't exist on this worker.", 400, 'bad_folder'));
		await fireEvent.click(view.getByRole('radio', { name: /Desk/ }));
		await fireEvent.input(view.getByRole('textbox'), { target: { value: '/nope' } });
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ cwd: '/nope', shell: null, target: provenTarget })));
		const alert = await view.findByRole('alert');
		expect(alert.textContent).toContain("The folder /nope doesn't exist");
		expect(view.container.querySelector('.notice')).toBeNull();
		await waitFor(() => expect(api.targets).toHaveBeenCalledTimes(2));
		api.create.mockResolvedValueOnce(info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner' }));
		await fireEvent.input(view.getByRole('textbox'), { target: { value: '' } });
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		await waitFor(() => expect(api.create).toHaveBeenLastCalledWith(expect.objectContaining({ cwd: null, target: provenTarget })));
		expect(api.peerChallenge).toHaveBeenCalledTimes(2);
		await waitFor(() => expect(view.container.querySelector('.status .meta')?.textContent).toContain('Desk'));
	});

	it('explains any other refused peer open in the picker, not the notice bar', async () => {
		const view = await mount(withPeer());
		api.create.mockRejectedValue(new TerminalApiError('The terminal worker is unavailable or owner access expired.', 503, 'peer_unavailable'));
		await fireEvent.click(view.getByRole('radio', { name: /Desk/ }));
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		expect((await view.findByText("The shell on Desk didn't open")).closest('[role="alert"]')).toBeTruthy();
		expect(view.container.querySelector('.notice')).toBeNull();
		await waitFor(() => expect(api.targets).toHaveBeenCalledTimes(2));
		expect(await view.findByRole('button', { name: 'Try again' })).toBeTruthy();
	});
});

describe('a chat on another worker', () => {
	const onDesk = (over: Record<string, unknown> = {}): TerminalTargets => ({ ...withPeer(over), chat_worker_id: 'dev_b' });

	it("opens a command block's paste on the chat's worker, signed for that action", async () => {
		api.paste.mockResolvedValue({ pasted: true });
		api.create.mockResolvedValue(info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner', shell: 'bash' }));
		const view = await mount(onDesk());
		terminals.request('s1', 'paste', { command: 'echo hi', cwd: null, shell: 'bash', on: null, host: false } as never);
		await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ target: provenTarget })));
		const sent = api.create.mock.calls[0][0] as { target: { proof: { payload: string } } };
		expect(JSON.parse(sent.target.proof.payload)).toMatchObject({ action: 'terminal.open_and_paste', receiver: 'dev_b',
			target: { session_id: 's1' } });
		await waitFor(() => expect(view.container.querySelector('.status .meta')?.textContent).toContain('Desk'));
	});

	it('keeps one link card through a second press, and that press finishes the job', async () => {
		const block = { command: 'echo hi', cwd: null, shell: 'bash', on: null, host: false } as never;
		api.peerChallenge.mockResolvedValueOnce({ status: 'unlinked', receiver: 'dev_b', via: 'w' })
			.mockResolvedValue({ status: 'pending', receiver: 'dev_b', via: 'w', expires_in: 590 });
		api.requestPeerLink.mockResolvedValue({ status: 'pending', key_id: 'kid', expires_in: 600 });
		api.paste.mockResolvedValue({ pasted: true });
		api.create.mockResolvedValue(info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner', shell: 'bash' }));
		const view = await mount(onDesk());
		const cards = () => view.container.querySelectorAll('.link-card').length;
		terminals.request('s1', 'paste', block);
		await waitFor(() => expect(cards()).toBe(1));
		terminals.request('s1', 'paste', block);
		// The first press wakes from its poll, finds itself replaced and must leave the card alone.
		await new Promise((resolve) => setTimeout(resolve, LINK_POLL_MS + 500));
		expect(cards()).toBe(1);
		api.peerChallenge.mockResolvedValue(linked);
		await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1), { timeout: LINK_POLL_MS * 3 });
		await waitFor(() => expect(cards()).toBe(0));
	}, 15_000);

	it("shows the worker's own reason when the chat's worker can't open a shell", async () => {
		const view = await mount(onDesk({ available: false, reason_code: 'peer_offline', reason: 'The peer worker is offline.' }));
		expect(await view.findByText('The peer worker is offline.')).toBeTruthy();
		expect(api.create).not.toHaveBeenCalled();
	});
});

describe('the receiving worker', () => {
	it('shows devices waiting to link, with no code, while peer terminals are on', async () => {
		api.deviceLinks.mockResolvedValue({ links: [], requests: [{ request_id: 'r1', label: 'Chrome on Linux', worker_id: 'dev_b', worker_name: 'Desk',
			age_seconds: 20, expires_in: 500 }] });
		const view = await mount(withPeer());
		const region = await view.findByRole('region', { name: 'Devices linked to ServerWorker' });
		expect(within(region).getByText('Chrome on Linux')).toBeTruthy();
		expect(within(region).getByLabelText('Code shown on the device')).toBeTruthy();
		expect(region.textContent).not.toMatch(/[A-Z2-7]{12}/);
	});
});
