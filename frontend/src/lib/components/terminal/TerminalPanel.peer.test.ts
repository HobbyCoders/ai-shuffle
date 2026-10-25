/**
 * TerminalPanel with peer terminals (#196): feature off leaves the panel as
 * it was, a peer shell names the worker it runs on and never offers an
 * in-place restart, a failed peer open is explained in the picker, and the
 * receiving worker lists requests only when peer terminals are on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
	requestPeer: vi.fn(), peerAuthorization: vi.fn(), revokePeerAuthorization: vi.fn(), peerRequests: vi.fn(),
	approvePeerRequest: vi.fn(), revokePeerRequest: vi.fn(),
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
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
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
	targets: [...base.targets, { kind: 'worker', worker_id: 'dev_b', name: 'Desk', available: false,
		reason_code: 'recipient_approval_required', reason: 'The owner must approve this terminal directly on the receiving worker.', ...over }],
});

beforeEach(() => {
	terminals.reset();
	peerTerminals.reset();
	sidePanel.close(sessionPanelKey('s1'));
	Object.values(api).forEach((fn) => fn.mockReset());
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	api.peerRequests.mockResolvedValue({ requests: [] });
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
	it('keeps the picker as it was and never asks for peer requests', async () => {
		const view = await mount(base);
		const form = view.getByRole('form', { name: 'New terminal' });
		expect(form.querySelector('input[name="terminal-target"]')).toBeNull();
		expect(within(form).queryByLabelText('Target')).toBeNull();
		expect(within(form).queryByText(/approval/i)).toBeNull();
		expect(within(form).getByRole('button', { name: 'Open terminal' })).toBeTruthy();
		expect(view.queryByRole('region', { name: 'Terminal requests from other workers' })).toBeNull();
		expect(api.peerRequests).not.toHaveBeenCalled();
	});
});

describe('a shell on a peer worker', () => {
	it('names the peer worker in the status line, not this worker', async () => {
		const view = await mount(withPeer({ available: true, reason_code: null, reason: null, authorization_id: 'a'.repeat(24) }),
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
		expect(within(ended).getByText(/asks for approval again/)).toBeTruthy();
	});

	it('closes a peer shell through the same route as a local one, from the tab or the exited card', async () => {
		api.close.mockResolvedValue({ closed: true });
		const peerShell = { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner' };
		const view = await mount(withPeer(), [info('t1', peerShell), info('t2', { ...peerShell, state: 'exited', exit_code: 0 })]);
		expect(view.getAllByRole('tab').map((tab) => tab.getAttribute('title'))).toEqual(['Desk: /home/owner', 'Desk: /home/owner']);
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

	it('shows a rejected folder in the picker, keeps the approval and retries the same target', async () => {
		const approved = withPeer({ available: true, reason_code: null, reason: null, authorization_id: 'a'.repeat(24) });
		const view = await mount(approved);
		api.create.mockRejectedValueOnce(new TerminalApiError("The folder /nope doesn't exist on this worker.", 400, 'bad_folder'));
		await fireEvent.click(view.getByRole('radio', { name: /Desk/ }));
		await fireEvent.input(view.getByRole('textbox'), { target: { value: '/nope' } });
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		const target = { kind: 'worker', worker_id: 'dev_b', authorization_id: 'a'.repeat(24) };
		expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ cwd: '/nope', shell: null, target }));
		const alert = await view.findByRole('alert');
		expect(alert.textContent).toContain("The folder /nope doesn't exist");
		expect(view.container.querySelector('.notice')).toBeNull();
		await waitFor(() => expect(api.targets).toHaveBeenCalledTimes(2));
		api.create.mockResolvedValueOnce(info('t1', { target: { kind: 'worker', worker_id: 'dev_b' }, cwd: '/home/owner' }));
		await fireEvent.input(view.getByRole('textbox'), { target: { value: '' } });
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		expect(api.create).toHaveBeenLastCalledWith(expect.objectContaining({ cwd: null, target }));
		await waitFor(() => expect(view.container.querySelector('.status .meta')?.textContent).toContain('Desk'));
	});

	it('explains any other refused peer open in the picker, not the notice bar', async () => {
		const view = await mount(withPeer({ available: true, reason_code: null, reason: null, authorization_id: 'a'.repeat(24) }));
		api.create.mockRejectedValue(new TerminalApiError('The terminal worker is unavailable or owner access expired.', 503, 'peer_unavailable'));
		api.targets.mockResolvedValue(withPeer());
		await fireEvent.click(view.getByRole('radio', { name: /Desk/ }));
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		expect((await view.findByText("The shell on Desk didn't open")).closest('[role="alert"]')).toBeTruthy();
		expect(view.container.querySelector('.notice')).toBeNull();
		await waitFor(() => expect(api.targets).toHaveBeenCalledTimes(2));
		expect(await view.findByRole('button', { name: 'Request again' })).toBeTruthy();
	});
});

describe('the receiving worker', () => {
	it('lists requests from other workers above the terminals while peer terminals are on', async () => {
		api.peerRequests.mockResolvedValue({ requests: [{ request_id: 'r1', worker_id: 'dev_b', status: 'pending', used: false,
			expires_in: 100, confirmation_code: 'K7Q2XM', source_name: 'Desk', age_seconds: 20 }] });
		const view = await mount(withPeer());
		const region = await view.findByRole('region', { name: 'Terminal requests from other workers' });
		expect(within(region).getByText('Desk wants a shell on ServerWorker')).toBeTruthy();
		expect(within(region).getByRole('img', { name: 'Code from Desk: K 7 Q, 2 X M' })).toBeTruthy();
	});
});
