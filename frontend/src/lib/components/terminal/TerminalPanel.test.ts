/**
 * TerminalPanel with the emulator and the socket faked (#1945 security
 * review): what reaches the paste route, what reaches the socket's input
 * channel, when focus moves, and which tab a paste goes to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
	peerRequests: vi.fn(),
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

/** Every fake emulator, in the order the tabs mounted. */
const fx = vi.hoisted(() => ({
	terms: [] as {
		options: Record<string, unknown>;
		input: HTMLTextAreaElement | null;
		onData: ((data: string) => void) | null;
		keyHandler: ((event: KeyboardEvent) => boolean) | null;
		selection: string;
		onSelection: (() => void) | null;
		addons: unknown[];
	}[],
	/** Every string sent over a socket's input channel, with its terminal. */
	socketInput: [] as { id: string; data: string }[],
}));

vi.mock('@xterm/xterm', () => ({
	Terminal: class {
		cols = 80;
		rows = 24;
		buffer = { active: { length: 0, getLine: () => null } };
		record: (typeof fx.terms)[number];
		constructor(options: Record<string, unknown>) {
			this.record = { options, input: null, onData: null, keyHandler: null, selection: '', onSelection: null, addons: [] };
			fx.terms.push(this.record);
		}
		loadAddon(addon: unknown) { this.record.addons.push(addon); }
		open(host: HTMLElement) {
			const input = document.createElement('textarea');
			input.className = 'xterm-helper-textarea';
			host.appendChild(input);
			this.record.input = input;
		}
		onData(cb: (data: string) => void) { this.record.onData = cb; }
		onSelectionChange(cb: () => void) { this.record.onSelection = cb; }
		attachCustomKeyEventHandler(cb: (event: KeyboardEvent) => boolean) { this.record.keyHandler = cb; }
		focus() { this.record.input?.focus(); }
		write() {}
		reset() {}
		clear() {}
		dispose() {}
		hasSelection() { return !!this.record.selection; }
		getSelection() { return this.record.selection; }
		clearSelection() {}
	},
}));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
vi.mock('@xterm/addon-web-links', () => ({
	WebLinksAddon: class {
		constructor(public handler: (event: MouseEvent, uri: string) => void, public options: Record<string, unknown>) {}
	},
}));
vi.mock('@xterm/addon-search', () => ({ SearchAddon: class { findNext() { return false; } findPrevious() { return false; } clearDecorations() {} } }));
vi.mock('$lib/terminal/connection', () => ({
	TerminalConnection: class {
		constructor(private id: string, private handlers: { onStatus(s: string, m: string | null): void }) {}
		connect() { this.handlers.onStatus('live', null); }
		input(data: string) { fx.socketInput.push({ id: this.id, data }); return true; }
		resize() {}
		retry() {}
		dispose() {}
	},
}));

import TerminalPanel from './TerminalPanel.svelte';
import { get } from 'svelte/store';
import { composerInsert } from '$lib/stores/composer-insert';
import TerminalPage from './TerminalPage.svelte';
import { TerminalApiError, type TerminalInfo, type TerminalTargets } from '$lib/api/terminals';
import { terminals, WORKER_TERMINALS } from '$lib/stores/terminals.svelte';
import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { sidePanel, sessionPanelKey } from '$lib/stores/side-panel.svelte';
import type { CommandBlock } from '$lib/terminal/commands';

const info = (id: string, over: Partial<TerminalInfo> = {}): TerminalInfo => ({
	id, target: { kind: 'worker' }, shell: 'bash', cwd: '/workspace/app', folder_source: 'project', session_id: 's1',
	state: 'live', exit_code: null, created_at: '', exited_at: null, busy: false, cols: 80, rows: 24, attached: 0, ...over,
});
const targets: TerminalTargets = {
	worker: { id: 'w', name: 'ServerWorker', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
	shells: [{ name: 'bash', default: true }, { name: 'sh', default: false }], default_shell: 'bash',
	default_folder: { path: '/workspace/app', source: 'project' }, limit: 8, open: 0,
};
const block = (command: string, over: Partial<CommandBlock> = {}): CommandBlock => ({ command, cwd: null, shell: null, on: null, host: false, ...over });

let created = 0;

beforeEach(() => {
	terminals.reset();
	sidePanel.close(sessionPanelKey('s1'));
	Object.values(api).forEach((fn) => fn.mockReset());
	fx.terms.length = 0;
	fx.socketInput.length = 0;
	created = 0;
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	api.targets.mockResolvedValue(targets);
	api.list.mockResolvedValue({ terminals: [], open: 0, running: 0, limit: 8, worker: targets.worker });
	api.create.mockImplementation(async (body: { cwd?: string | null; shell?: string | null }) => {
		created += 1;
		return info(`t${created}`, { shell: body.shell ?? 'bash', cwd: body.cwd ?? '/workspace/app' });
	});
	api.peerRequests.mockResolvedValue({ requests: [] });
	api.paste.mockImplementation(async (id: string) => ({ mode: 'bracketed', terminal: terminals.terminal(id) ?? info(id) }));
});
afterEach(() => {
	cleanup();
	peerTerminals.reset();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

async function mount(listed: TerminalInfo[] = []) {
	api.list.mockResolvedValue({ terminals: listed, open: listed.length, running: 0, limit: 8, worker: targets.worker });
	const view = render(TerminalPanel, { sessionId: 's1' });
	await waitFor(() => expect(terminals.tabsFor('s1').loaded && terminals.probeFor('s1').status === 'ready').toBe(true));
	return view;
}

function clipboardPaste(index: number, text: string) {
	const host = fx.terms[index].input!.parentElement!;
	const event = new Event('paste', { bubbles: true, cancelable: true });
	Object.defineProperty(event, 'clipboardData', { value: { getData: () => text } });
	host.dispatchEvent(event);
	return event;
}

const sentText = () => fx.socketInput.map((entry) => entry.data).join('');

describe('Monitor watch mode', () => {
	it('removes process controls and the mobile key bar while watching, then returns to shells', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ tasks: [] }))));
		const view = await mount([info('t9', { busy: true })]);
		await fireEvent.click(view.getByRole('button', { name: 'Watch command' }));
		expect(await view.findByText('Read-only')).toBeTruthy();
		expect(view.queryByRole('button', { name: 'Stop' })).toBeNull();
		expect(view.queryByRole('button', { name: 'New terminal' })).toBeNull();
		expect(view.queryByRole('button', { name: 'Close terminal 1' })).toBeNull();
		await fireEvent.click(view.getByRole('button', { name: 'Back to shells' }));
		expect(view.getByRole('button', { name: 'Stop' })).toBeTruthy();
		expect(fx.socketInput).toEqual([]);
	});
});

describe('Windows picker', () => {
	it('opens the seeded Windows folder using the discovered PowerShell shell', async () => {
		api.targets.mockResolvedValue({ ...targets,
			worker: { id: 'w', name: 'DesktopWorker', platform: 'windows' },
			shells: [{ name: 'powershell', default: true }], default_shell: 'powershell',
			default_folder: { path: String.raw`C:\Projects\app`, source: 'project' },
		});
		const view = await mount();
		expect((view.getByRole('textbox', { name: /^Folder/ }) as HTMLInputElement).value).toBe(String.raw`C:\Projects\app`);
		const button = view.getByRole('button', { name: 'Open terminal' }) as HTMLButtonElement;
		expect(button.disabled).toBe(false);
		await fireEvent.click(button);
		await waitFor(() => expect(api.create).toHaveBeenCalledWith({ session_id: 's1', cwd: null, shell: 'powershell' }));
	});
});

describe('pasting', () => {
	it('asks before a multi-line paste and sends nothing until Paste', async () => {
		const view = await mount();
		terminals.request('s1', 'paste', block('echo one\necho two\necho three'));
		await waitFor(() => expect(view.getByRole('alertdialog')).toBeTruthy());
		expect(view.getByText('Paste 3 lines?')).toBeTruthy();
		expect(api.paste).not.toHaveBeenCalled();
		expect(fx.socketInput).toEqual([]);
		await fireEvent.click(view.getByRole('button', { name: 'Paste' }));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'echo one\necho two\necho three'));
		expect(sentText()).not.toContain('echo');
	});

	it('sends a block through the paste route, never the socket input', async () => {
		const view = await mount();
		terminals.request('s1', 'paste', block('npm test'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'npm test'));
		await waitFor(() => expect(view.getByText('Press Enter to run · Esc to clear')).toBeTruthy());
		expect(sentText()).not.toContain('npm test');
		expect(fx.socketInput).toEqual([]);
	});

	it('catches a clipboard paste and takes the same path', async () => {
		const view = await mount([info('t9')]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		const event = clipboardPaste(0, 'ls -la');
		expect(event.defaultPrevented).toBe(true);
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t9', 'ls -la'));
		clipboardPaste(0, 'a\nb');
		await waitFor(() => expect(view.getByText('Paste 2 lines?')).toBeTruthy());
		expect(api.paste).toHaveBeenCalledTimes(1);
		expect(fx.socketInput).toEqual([]);
	});

	it('clears a waiting paste with Esc', async () => {
		const view = await mount();
		terminals.request('s1', 'paste', block('make build'));
		await waitFor(() => expect(view.getByText('Press Enter to run · Esc to clear')).toBeTruthy());
		const handled = fx.terms[0].keyHandler!({ type: 'keydown', key: 'Escape', preventDefault() {} } as KeyboardEvent);
		expect(handled).toBe(false);
		expect(fx.socketInput).toEqual([{ id: 't1', data: '\x05\x15' }]);
		await waitFor(() => expect(view.queryByText('Press Enter to run · Esc to clear')).toBeNull());
	});

	it('refuses a multi-line paste into an sh terminal', async () => {
		const view = await mount([info('t9', { shell: 'sh' })]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		clipboardPaste(0, 'echo a\necho b');
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain('sh can take one line at a time'));
		expect(view.queryByRole('alertdialog')).toBeNull();
		expect(api.paste).not.toHaveBeenCalled();
		// One line is fine in sh.
		clipboardPaste(0, 'echo a');
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t9', 'echo a'));
	});

	it('refuses a multi-line block for sh before opening a tab', async () => {
		const view = await mount();
		terminals.request('s1', 'paste', block('echo a\necho b', { shell: 'sh' }));
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain('sh can take one line at a time'));
		expect(api.create).not.toHaveBeenCalled();
		expect(api.paste).not.toHaveBeenCalled();
	});
});

describe('focus', () => {
	function deferredPaste() {
		let release!: () => void;
		api.paste.mockImplementation((id: string) => new Promise((resolve) => {
			release = () => resolve({ mode: 'bracketed', terminal: terminals.terminal(id) ?? info(id) });
		}));
		return () => release();
	}

	it('leaves focus in a text field the owner went back to', async () => {
		const view = await mount();
		const chat = document.createElement('textarea');
		document.body.appendChild(chat);
		const button = document.createElement('button');
		document.body.appendChild(button);
		const release = deferredPaste();
		button.focus();
		terminals.request('s1', 'paste', block('rm -rf build'), button);
		await waitFor(() => expect(api.paste).toHaveBeenCalled());
		chat.focus(); // back to the chat box while the shell starts
		release();
		await waitFor(() => expect(view.getByText(/Check this command/)).toBeTruthy());
		await new Promise((r) => setTimeout(r, 0));
		expect(document.activeElement).toBe(chat);
	});

	it('leaves a text field focused when a multi-line confirm appears', async () => {
		const view = await mount();
		const chat = document.createElement('input');
		document.body.appendChild(chat);
		chat.focus();
		terminals.request('s1', 'paste', block('a\nb'));
		await waitFor(() => expect(view.getByRole('alertdialog')).toBeTruthy());
		await new Promise((r) => setTimeout(r, 0));
		expect(document.activeElement).toBe(chat);
	});

	it('moves focus to the terminal from the clicked button', async () => {
		await mount();
		const button = document.createElement('button');
		document.body.appendChild(button);
		button.focus();
		terminals.request('s1', 'paste', block('ls'), button);
		await waitFor(() => expect(api.paste).toHaveBeenCalled());
		await waitFor(() => expect(document.activeElement).toBe(fx.terms[0].input));
	});

	it("leaves focus in the panel's own search box", async () => {
		const view = await mount([info('t9')]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		await fireEvent.click(view.getByRole('button', { name: 'Search output' }));
		const find = await waitFor(() => view.getByRole('textbox', { name: 'Find in output' }));
		find.focus();
		terminals.request('s1', 'paste', block('ls'));
		await waitFor(() => expect(api.paste).toHaveBeenCalled());
		await new Promise((r) => setTimeout(r, 0));
		expect(document.activeElement).toBe(find);
	});

	it('moves focus to the terminal from the page body', async () => {
		await mount();
		(document.activeElement as HTMLElement | null)?.blur();
		terminals.request('s1', 'open', block('ls'));
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		await waitFor(() => expect(document.activeElement).toBe(fx.terms[0].input));
	});
});

describe('stale requests', () => {
	it('drops a waiting request when the panel closes', async () => {
		let answer!: (value: TerminalTargets) => void;
		api.targets.mockReturnValueOnce(new Promise((resolve) => { answer = resolve; })); // a slow check
		const view = render(TerminalPanel, { sessionId: 's1' });
		terminals.request('s1', 'paste', block('ls'));
		view.unmount();
		expect(terminals.pendingRequest('s1')).toBeNull();
		answer(targets);
		// Opening the panel again later pastes nothing.
		await mount();
		await new Promise((r) => setTimeout(r, 0));
		expect(api.create).not.toHaveBeenCalled();
		expect(api.paste).not.toHaveBeenCalled();
	});

	it('drops it with a notice when the capability check fails', async () => {
		api.targets.mockRejectedValue(new TerminalApiError("Can't reach the worker.", 0, 'network'));
		const view = render(TerminalPanel, { sessionId: 's1' });
		terminals.request('s1', 'paste', block('ls'));
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain('Nothing was pasted.'));
		expect(terminals.pendingRequest('s1')).toBeNull();
		expect(api.paste).not.toHaveBeenCalled();
	});

	it('drops it with a notice when the check refuses', async () => {
		api.targets.mockRejectedValue(new TerminalApiError('no', 403, 'http_403'));
		const view = render(TerminalPanel, { sessionId: 's1' });
		terminals.request('s1', 'open', block('ls'));
		await waitFor(() => expect(terminals.pendingRequest('s1')).toBeNull());
		expect(view.getByRole('alert').textContent).toContain('No terminal opened.');
		expect(api.create).not.toHaveBeenCalled();
	});

	it('drops it after the timeout and says so in the panel', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		api.targets.mockReturnValue(new Promise(() => {}));
		const view = render(TerminalPanel, { sessionId: 's1' });
		terminals.request('s1', 'paste', block('ls'));
		vi.advanceTimersByTime(15_000);
		vi.useRealTimers();
		expect(terminals.pendingRequest('s1')).toBeNull();
		await waitFor(() => expect(view.getByRole('alert').textContent).toContain("didn't open in time"));
	});
});

describe('tab reuse', () => {
	it('never pastes into a tab that still shows a paste hint', async () => {
		const view = await mount();
		terminals.request('s1', 'paste', block('echo a'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'echo a'));
		await waitFor(() => expect(view.getByText('Press Enter to run · Esc to clear')).toBeTruthy());
		terminals.request('s1', 'paste', block('rm -rf build'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t2', 'rm -rf build'));
		expect(api.create).toHaveBeenCalledTimes(2);
	});

	it('reuses a tab once Enter has cleared its prompt', async () => {
		await mount();
		terminals.request('s1', 'paste', block('echo a'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'echo a'));
		fx.terms[0].onData!('\r');
		terminals.request('s1', 'paste', block('echo b'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'echo b'));
		expect(api.create).toHaveBeenCalledTimes(1);
	});

	it('opens a new tab rather than paste after half-typed text', async () => {
		await mount();
		terminals.request('s1', 'open', block('ls'));
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		fx.terms[0].onData!('git sta');
		terminals.request('s1', 'paste', block('ls'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t2', 'ls'));
	});

	it('does not paste into a restored tab it knows nothing about', async () => {
		await mount([info('t9')]);
		terminals.request('s1', 'paste', block('ls'));
		await waitFor(() => expect(api.paste).toHaveBeenCalledWith('t1', 'ls'));
	});
});

describe('tab strip', () => {
	it('scrolls a new tab into view when the strip is too narrow to show it', async () => {
		const view = await mount([info('t9')]);
		const strip = await waitFor(() => view.getByRole('tablist'));
		let scrollLeft = 0;
		Object.defineProperty(strip, 'scrollLeft', { get: () => scrollLeft, set: (v: number) => { scrollLeft = v; } });
		const rect = (left: number, right: number) => ({ left, right, top: 0, bottom: 30, width: right - left, height: 30, x: left, y: 0, toJSON() {} }) as DOMRect;
		vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
			if (this === strip) return rect(0, 120);
			if (this.classList?.contains('tab') && this.querySelector('[role="tab"]')?.id === 'terminal-tab-t1') return rect(150, 230); // the new tab, past the right edge
			return rect(0, 60);
		});
		terminals.request('s1', 'paste', block('ls')); // the restored tab may hold text, so a new one opens
		await waitFor(() => expect(view.getByRole('tab', { selected: true }).id).toBe('terminal-tab-t1'));
		await waitFor(() => expect(scrollLeft).toBe(110));
		vi.restoreAllMocks();
	});
});

describe('emulator options', () => {
	it('turns xterm logging off', async () => {
		await mount([info('t9')]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		expect(fx.terms[0].options.logLevel).toBe('off');
	});
});

describe('links in output', () => {
	it('opens web addresses in a new tab and refuses other schemes', async () => {
		const open = vi.fn();
		vi.stubGlobal('open', open);
		await mount([info('t9')]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		const links = fx.terms[0].addons.find((addon) => typeof (addon as { handler?: unknown }).handler === 'function') as
			{ handler: (event: MouseEvent, uri: string) => void } | undefined;
		expect(links).toBeTruthy();
		links!.handler(new MouseEvent('click'), 'http://localhost:5173/');
		expect(open).toHaveBeenCalledWith('http://localhost:5173/', '_blank', 'noopener,noreferrer');
		// OSC 8 hyperlinks take the same path, so a program cannot open a script URL.
		const osc8 = fx.terms[0].options.linkHandler as { activate: (event: MouseEvent, text: string) => void; allowNonHttpProtocols: boolean };
		expect(osc8.allowNonHttpProtocols).toBe(false);
		osc8.activate(new MouseEvent('click'), 'javascript:alert(1)');
		osc8.activate(new MouseEvent('click'), 'https://example.com/docs');
		expect(open).toHaveBeenCalledTimes(2);
		expect(open).toHaveBeenLastCalledWith('https://example.com/docs', '_blank', 'noopener,noreferrer');
	});
});

describe('send selection to chat', () => {
	afterEach(() => composerInsert.set(null));

	it('is off until text is selected, then hands a fenced block to the composer without sending', async () => {
		const view = await mount([info('t9')]);
		await waitFor(() => expect(fx.terms).toHaveLength(1));
		const button = view.getByRole('button', { name: 'Send selection to chat' }) as HTMLButtonElement;
		expect(button.disabled).toBe(true);

		fx.terms[0].selection = 'npm ERR! missing script: dev   \n';
		fx.terms[0].onSelection?.();
		await waitFor(() => expect(button.disabled).toBe(false));
		await fireEvent.click(button);

		expect(get(composerInsert)).toMatchObject({ sessionId: 's1', text: '```text\nnpm ERR! missing script: dev\n```' });
		// Nothing typed into the shell, nothing pasted.
		expect(fx.socketInput).toEqual([]);
		expect(api.paste).not.toHaveBeenCalled();
		expect(view.getByText('Selection added to your message. Nothing was sent.')).toBeTruthy();
	});

	it('is not offered on the Terminal page, which has no chat', async () => {
		api.targets.mockResolvedValue(targets);
		api.list.mockResolvedValue({ terminals: [info('own', { session_id: null })], open: 1, running: 0, limit: 8, worker: targets.worker });
		const view = render(TerminalPage);
		await waitFor(() => expect(view.getByRole('button', { name: 'Copy output' })).toBeTruthy());
		expect(view.queryByRole('button', { name: 'Send selection to chat' })).toBeNull();
	});
});

describe('Terminal page (no chat)', () => {
	const workspace = { ...targets, default_folder: { path: '/workspace', source: 'workspace' } };

	async function mountPage(listed: TerminalInfo[] = []) {
		api.targets.mockResolvedValue(workspace);
		api.list.mockResolvedValue({ terminals: listed, open: listed.length, running: 0, limit: 8, worker: targets.worker });
		const view = render(TerminalPage);
		await waitFor(() => expect(terminals.tabsFor(WORKER_TERMINALS).loaded && terminals.probeFor(WORKER_TERMINALS).status === 'ready').toBe(true));
		return view;
	}

	it('opens a shell in the workspace folder with no chat id', async () => {
		const view = await mountPage();
		expect(api.targets).toHaveBeenCalledWith(null);
		expect(api.list).toHaveBeenCalledWith(null);
		expect(view.getByText(/Not tied to a chat/)).toBeTruthy();
		expect((view.getByRole('textbox', { name: /^Folder/ }) as HTMLInputElement).value).toBe('/workspace');
		expect(view.getByText('Workspace folder')).toBeTruthy();
		await fireEvent.click(view.getByRole('button', { name: 'Open terminal' }));
		await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
		expect(api.create.mock.calls[0][0]).toMatchObject({ session_id: null, cwd: null, shell: 'bash' });
		await waitFor(() => expect(view.getByRole('tablist', { name: 'Terminals on this worker' })).toBeTruthy());
		expect(fx.terms).toHaveLength(1);
	});

	it("reattaches the worker's chatless shells and leaves a chat's alone", async () => {
		const view = await mountPage([info('own', { session_id: null, cwd: '/workspace' }), info('chat', { session_id: 's1' })]);
		const tabs = view.getAllByRole('tab');
		expect(tabs).toHaveLength(1);
		expect(tabs[0].id).toBe('terminal-tab-own');
	});

	it('shows the refusal, not a shell, to a viewer the worker denies', async () => {
		api.targets.mockRejectedValue(new TerminalApiError('Agent previews can\'t use terminals.', 403, 'http_403'));
		const view = render(TerminalPage);
		await waitFor(() => expect(view.getByText('Only the owner can use terminals')).toBeTruthy());
		expect(view.queryByRole('button', { name: 'Open terminal' })).toBeNull();
		expect(api.create).not.toHaveBeenCalled();
	});

	it('closing the shell closes it on the worker', async () => {
		api.close.mockResolvedValue({ closed: true });
		const view = await mountPage([info('own', { session_id: null, cwd: '/workspace' })]);
		await fireEvent.click(view.getByRole('button', { name: 'Close terminal 1' }));
		await waitFor(() => expect(api.close).toHaveBeenCalledWith('own'));
		await waitFor(() => expect(terminals.tabsFor(WORKER_TERMINALS).ids).toEqual([]));
	});
});

describe('peer workers', () => {
	const local = { kind: 'worker', worker_id: 'w', name: 'ServerWorker', available: true };
	const peerRow = { kind: 'worker', worker_id: 'peer', name: 'Acceptance Windows B', available: false,
		reason_code: 'recipient_approval_required', reason: 'Approve a shell on Acceptance Windows B.' };
	const withPeer: TerminalTargets = { ...targets, targets: [local, peerRow] };
	const ready = () => terminals.tabsFor(WORKER_TERMINALS).loaded && terminals.probeFor(WORKER_TERMINALS).status === 'ready';

	it('picks up a newly enrolled peer on a page left open, without reopening it', async () => {
		api.targets.mockResolvedValue({ ...targets, targets: [local] });
		const view = render(TerminalPage);
		await waitFor(() => expect(ready()).toBe(true));
		expect(view.queryByRole('radio', { name: /Acceptance Windows B/ })).toBeNull();
		const before = api.targets.mock.calls.length;

		// The worker joins a network: the server announces a `network` change.
		api.targets.mockResolvedValue(withPeer);
		resourceSync.invalidate(['network']);

		await waitFor(() => expect(view.getByRole('radio', { name: /Acceptance Windows B/ })).toBeTruthy());
		expect(api.targets.mock.calls.length).toBe(before + 1);
		expect(api.targets).toHaveBeenLastCalledWith(null);
	});

	it('ignores unrelated changes and stops listening once closed', async () => {
		const view = await mount();
		const before = api.targets.mock.calls.length;
		resourceSync.invalidate(['notes']);
		await new Promise((r) => setTimeout(r, 150));
		expect(api.targets.mock.calls.length).toBe(before);

		view.unmount();
		resourceSync.invalidate(['network']);
		await new Promise((r) => setTimeout(r, 150));
		expect(api.targets.mock.calls.length).toBe(before);
	});

	it('re-probes a stale answer when the owner uses the open picker', async () => {
		const view = await mount();
		const before = api.targets.mock.calls.length;
		const folder = () => view.getByRole('textbox', { name: /^Folder/ });
		await fireEvent.focusIn(folder());
		expect(api.targets.mock.calls.length).toBe(before); // still fresh: no request

		const now = Date.now();
		vi.spyOn(Date, 'now').mockReturnValue(now + 61_000);
		api.targets.mockResolvedValue(withPeer);
		await fireEvent.focusIn(folder());
		await waitFor(() => expect(view.getByRole('radio', { name: /Acceptance Windows B/ })).toBeTruthy());
		expect(api.targets.mock.calls.length).toBe(before + 1);
		vi.restoreAllMocks();
	});

	it('shows a long peer name on the tab in full as a tooltip', async () => {
		api.targets.mockResolvedValue(withPeer);
		const view = await mount([info('p1', { target: { kind: 'worker', worker_id: 'peer' } })]);
		const badge = await waitFor(() => {
			const el = view.container.querySelector('.tab-host');
			expect(el).toBeTruthy();
			return el as HTMLElement;
		});
		expect(badge.textContent).toBe('Acceptance Windows B');
		expect(badge.title).toBe('Acceptance Windows B');
	});

	it('keeps the peer badge on one line, cut with an ellipsis', () => {
		// jsdom applies no component CSS, so read the rule itself: without
		// nowrap a long name wraps onto several lines inside the 35 px tab.
		const source = readFileSync('src/lib/components/terminal/TerminalPanel.svelte', 'utf8');
		const rule = source.match(/\.tab-host\s*\{([^}]*)\}/)?.[1] ?? '';
		expect(rule).toMatch(/white-space:\s*nowrap/);
		expect(rule).toMatch(/text-overflow:\s*ellipsis/);
		expect(rule).toMatch(/overflow:\s*hidden/);
		expect(rule).toMatch(/max-width:/);
		// It must be able to shrink: a fixed-width badge ran out of a narrow
		// (390 px) tab and under its close button, cut with no ellipsis.
		expect(rule).toMatch(/min-width:\s*0/);
		expect(rule).not.toMatch(/flex-shrink:\s*0/);
		expect(rule).toMatch(/flex:\s*0 1 auto/);
	});
});
