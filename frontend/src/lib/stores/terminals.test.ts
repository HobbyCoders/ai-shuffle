import { describe, it, expect, vi, beforeEach } from 'vitest';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import { TerminalApiError, type TerminalInfo, type TerminalTargets } from '$lib/api/terminals';
import { terminals, probeFromTargets, probeFromError, REQUEST_TTL, REQUEST_EXPIRED, WORKER_TERMINALS } from './terminals.svelte';
import { sidePanel, sessionPanelKey } from './side-panel.svelte';

const info = (id: string, over: Partial<TerminalInfo> = {}): TerminalInfo => ({
	id, target: { kind: 'worker' }, shell: 'bash', cwd: '/workspace/app', folder_source: 'project', session_id: 's1',
	state: 'live', exit_code: null, created_at: '', exited_at: null, busy: false, cols: 80, rows: 24, attached: 0, ...over,
});
const targets: TerminalTargets = {
	worker: { id: 'w', name: 'Unraid', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
	shells: [{ name: 'bash', default: true }, { name: 'sh', default: false }], default_shell: 'bash',
	default_folder: { path: '/workspace/app', source: 'project' }, limit: 8, open: 0,
};

beforeEach(() => {
	terminals.reset();
	Object.values(api).forEach((fn) => fn.mockReset());
	sidePanel.close(sessionPanelKey('s1'));
});

describe('probe mapping', () => {
	it('maps targets to a ready probe', () => {
		expect(probeFromTargets(targets)).toMatchObject({ status: 'ready', worker: 'Unraid', shells: ['bash', 'sh'], folder: '/workspace/app', folderSource: 'project' });
	});
	it('treats 401 and 403 as denied and keeps service codes', () => {
		expect(probeFromError(new TerminalApiError('no', 403, 'http_403'))).toEqual({ status: 'denied' });
		expect(probeFromError(new TerminalApiError('no', 401, 'http_401'))).toEqual({ status: 'denied' });
		expect(probeFromError(new TerminalApiError('Elsewhere', 409, 'chat_on_other_worker')))
			.toEqual({ status: 'error', code: 'chat_on_other_worker', message: 'Elsewhere' });
	});
});

describe('terminals store', () => {
	it('probes a chat once within the TTL and shares an in-flight probe', async () => {
		api.targets.mockResolvedValue(targets);
		await Promise.all([terminals.ensureProbe('s1'), terminals.ensureProbe('s1')]);
		await terminals.ensureProbe('s1');
		expect(api.targets).toHaveBeenCalledTimes(1);
		expect(terminals.probeFor('s1').status).toBe('ready');
		expect(terminals.capability.status).toBe('ready');
	});

	it('stops probing after a denial', async () => {
		api.targets.mockRejectedValue(new TerminalApiError('no', 403, 'http_403'));
		await terminals.ensureProbe('s1');
		await terminals.ensureProbe('s2');
		await terminals.ensureCapability();
		expect(api.targets).toHaveBeenCalledTimes(1);
		expect(terminals.capability.status).toBe('denied');
	});

	it('restores tabs from the list, keeping the existing order', async () => {
		api.list.mockResolvedValueOnce({ terminals: [info('a'), info('b')], open: 2, running: 0, limit: 8, worker: targets.worker });
		await terminals.load('s1');
		expect(terminals.tabsFor('s1')).toMatchObject({ ids: ['a', 'b'], active: 'b', loaded: true });
		terminals.select('s1', 'a');
		api.list.mockResolvedValueOnce({ terminals: [info('c'), info('b'), info('a')], open: 3, running: 1, limit: 8, worker: targets.worker });
		await terminals.load('s1');
		expect(terminals.tabsFor('s1')).toMatchObject({ ids: ['a', 'b', 'c'], active: 'a' });
		expect(terminals.counts).toEqual({ open: 3, running: 1, limit: 8 });
	});

	it('adds a created terminal as the active tab', async () => {
		api.create.mockResolvedValue(info('n'));
		const created = await terminals.create('s1', { cwd: null, shell: 'sh' });
		expect(api.create).toHaveBeenCalledWith({ session_id: 's1', cwd: null, shell: 'sh' });
		expect(created?.id).toBe('n');
		expect(terminals.tabsFor('s1')).toMatchObject({ ids: ['n'], active: 'n' });
	});

	it('shows the cap as a notice and marks the worker full', async () => {
		api.create.mockRejectedValue(new TerminalApiError('8 terminals are open. Close one first.', 429, 'limit_reached'));
		expect(await terminals.create('s1')).toBeNull();
		expect(terminals.notice('s1')).toBe('8 terminals are open. Close one first.');
		expect(terminals.counts.open).toBe(terminals.counts.limit);
	});

	it('takes the worker-wide count from a forced probe, up and back down', async () => {
		api.targets.mockResolvedValueOnce({ ...targets, open: 8 }).mockResolvedValueOnce({ ...targets, open: 3 });
		await terminals.ensureProbe('s1', true);
		expect(terminals.counts).toMatchObject({ open: 8, limit: 8 });
		await terminals.ensureProbe('s1', true);
		expect(terminals.counts.open).toBe(3);
	});

	it('drops a closed tab and picks its neighbour; a missing terminal is fine', async () => {
		api.list.mockResolvedValue({ terminals: [info('a'), info('b'), info('c')], open: 3, running: 0, limit: 8, worker: targets.worker });
		await terminals.load('s1');
		terminals.select('s1', 'b');
		api.close.mockResolvedValue({ closed: true });
		await terminals.close('s1', 'b');
		expect(terminals.tabsFor('s1')).toMatchObject({ ids: ['a', 'c'], active: 'c' });
		api.close.mockRejectedValue(new TerminalApiError('gone', 404, 'not_found'));
		await terminals.close('s1', 'c');
		expect(terminals.tabsFor('s1').ids).toEqual(['a']);
		expect(terminals.terminal('c')).toBeNull();
	});

	it('keeps the tab and shows why when closing fails', async () => {
		api.list.mockResolvedValue({ terminals: [info('a')], open: 1, running: 0, limit: 8, worker: targets.worker });
		await terminals.load('s1');
		api.close.mockRejectedValue(new TerminalApiError('Offline', 0, 'network'));
		await terminals.close('s1', 'a');
		expect(terminals.tabsFor('s1').ids).toEqual(['a']);
		expect(terminals.notice('s1')).toBe('Offline');
	});

	it('pastes through the paste route and returns the mode', async () => {
		api.list.mockResolvedValue({ terminals: [info('a')], open: 1, running: 0, limit: 8, worker: targets.worker });
		await terminals.load('s1');
		api.paste.mockResolvedValue({ mode: 'bracketed', terminal: info('a') });
		expect(await terminals.paste('s1', 'a', 'ls')).toBe('bracketed');
		expect(api.paste).toHaveBeenCalledWith('a', 'ls');
		api.paste.mockRejectedValue(new TerminalApiError('sh takes one line at a time.', 422, 'paste_multiline_unsupported'));
		expect(await terminals.paste('s1', 'a', 'a\nb')).toBeNull();
		expect(terminals.notice('s1')).toBe('sh takes one line at a time.');
	});

	it('ignores socket updates for terminals it does not own', () => {
		terminals.update(info('x'));
		terminals.setConnection('x', 'live');
		expect(terminals.terminal('x')).toBeNull();
		expect(terminals.connection('x').status).toBe('connecting');
	});

	it('queues a command block request, opens the panel, and hands it over once', () => {
		const block = { command: 'ls', cwd: null, shell: null, on: null, host: false };
		terminals.request('s1', 'paste', block);
		expect(sidePanel.forScope(sessionPanelKey('s1')).active).toBe('terminal');
		const pending = terminals.pendingRequest('s1')!;
		expect(pending).toMatchObject({ action: 'paste', block });
		expect(terminals.takeRequest('s1', pending.seq + 1)).toBeNull();
		expect(terminals.takeRequest('s1', pending.seq)).toMatchObject({ action: 'paste' });
		expect(terminals.takeRequest('s1', pending.seq)).toBeNull();
	});

	it('toggles the panel for a chat', () => {
		terminals.toggle('s1');
		expect(terminals.isOpen('s1')).toBe(true);
		terminals.toggle('s1');
		expect(terminals.isOpen('s1')).toBe(false);
		terminals.toggle(null);
		expect(terminals.isOpen(null)).toBe(false);
	});

	it('never writes terminal state to storage', async () => {
		const setItem = vi.spyOn(Storage.prototype, 'setItem');
		api.list.mockResolvedValue({ terminals: [info('a')], open: 1, running: 0, limit: 8, worker: targets.worker });
		api.paste.mockResolvedValue({ mode: 'plain', terminal: info('a') });
		await terminals.load('s1');
		await terminals.paste('s1', 'a', 'echo secret');
		const keys = setItem.mock.calls.map(([key]) => key);
		expect(setItem.mock.calls.some(([, value]) => String(value).includes('echo secret'))).toBe(false);
		expect(keys.some((key) => /terminal/i.test(key))).toBe(false);
		setItem.mockRestore();
	});

	it('drops a request the panel has not taken after the timeout, with a notice', () => {
		vi.useFakeTimers();
		try {
			terminals.request('s1', 'paste', { command: 'ls', cwd: null, shell: null, on: null, host: false });
			vi.advanceTimersByTime(REQUEST_TTL - 1);
			expect(terminals.pendingRequest('s1')).not.toBeNull();
			vi.advanceTimersByTime(1);
			expect(terminals.pendingRequest('s1')).toBeNull();
			expect(terminals.notice('s1')).toBe(REQUEST_EXPIRED);
		} finally {
			vi.useRealTimers();
		}
	});

	it('keeps a taken request and a later one apart from the timeout', () => {
		vi.useFakeTimers();
		try {
			const cmd = { command: 'ls', cwd: null, shell: null, on: null, host: false };
			terminals.request('s1', 'paste', cmd);
			const first = terminals.pendingRequest('s1')!;
			expect(terminals.takeRequest('s1', first.seq)).not.toBeNull();
			vi.advanceTimersByTime(REQUEST_TTL);
			expect(terminals.notice('s1')).toBeNull(); // nothing expired: it was handled
			terminals.request('s1', 'open', cmd);
			vi.advanceTimersByTime(REQUEST_TTL / 2);
			terminals.request('s1', 'paste', cmd); // a new click restarts the clock
			vi.advanceTimersByTime(REQUEST_TTL / 2);
			expect(terminals.pendingRequest('s1')?.action).toBe('paste');
		} finally {
			vi.useRealTimers();
		}
	});

	it('drops a waiting request when the toggle closes the panel', () => {
		terminals.request('s1', 'paste', { command: 'ls', cwd: null, shell: null, on: null, host: false });
		expect(terminals.isOpen('s1')).toBe(true);
		terminals.toggle('s1');
		expect(terminals.isOpen('s1')).toBe(false);
		expect(terminals.pendingRequest('s1')).toBeNull();
		expect(terminals.notice('s1')).toBeNull();
	});
});

describe('terminals with no chat (the Terminal page)', () => {
	it('probes, lists and creates without a chat id', async () => {
		api.targets.mockResolvedValue({ ...targets, default_folder: { path: '/workspace', source: 'workspace' } });
		await terminals.ensureProbe(WORKER_TERMINALS);
		expect(api.targets).toHaveBeenCalledWith(null);
		expect(terminals.probeFor(WORKER_TERMINALS)).toMatchObject({ status: 'ready', folder: '/workspace', folderSource: 'workspace' });

		api.create.mockResolvedValue(info('t9', { session_id: null, cwd: '/workspace', folder_source: 'workspace' }));
		const made = await terminals.create(WORKER_TERMINALS, { shell: 'bash' });
		expect(api.create).toHaveBeenCalledWith({ session_id: null, shell: 'bash' });
		expect(made?.id).toBe('t9');
		expect(terminals.tabsFor(WORKER_TERMINALS)).toMatchObject({ ids: ['t9'], active: 't9' });
	});

	it("lists only the worker's chatless terminals, never a chat's", async () => {
		api.list.mockResolvedValue({
			terminals: [info('chat', { session_id: 's1' }), info('own', { session_id: null })],
			open: 2, running: 2, limit: 8, worker: { id: 'w', name: 'Unraid', platform: 'linux' },
		});
		await terminals.load(WORKER_TERMINALS);
		expect(api.list).toHaveBeenCalledWith(null);
		expect(terminals.tabsFor(WORKER_TERMINALS).ids).toEqual(['own']);
		expect(terminals.terminal('chat')).toBeNull();
		// The worker-wide count still includes the chat's terminal.
		expect(terminals.counts.open).toBe(2);
	});

	it('a chat scope still sends its own id', async () => {
		api.create.mockResolvedValue(info('t1'));
		await terminals.create('s1');
		expect(api.create).toHaveBeenCalledWith({ session_id: 's1' });
	});
});
