/**
 * TerminalPanel with the emulator and the socket faked (#1945 security
 * review): what reaches the paste route, what reaches the socket's input
 * channel, when focus moves, and which tab a paste goes to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
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
			this.record = { options, input: null, onData: null, keyHandler: null };
			fx.terms.push(this.record);
		}
		loadAddon() {}
		open(host: HTMLElement) {
			const input = document.createElement('textarea');
			input.className = 'xterm-helper-textarea';
			host.appendChild(input);
			this.record.input = input;
		}
		onData(cb: (data: string) => void) { this.record.onData = cb; }
		attachCustomKeyEventHandler(cb: (event: KeyboardEvent) => boolean) { this.record.keyHandler = cb; }
		focus() { this.record.input?.focus(); }
		write() {}
		reset() {}
		clear() {}
		dispose() {}
		hasSelection() { return false; }
		getSelection() { return ''; }
		clearSelection() {}
	},
}));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
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
import { TerminalApiError, type TerminalInfo, type TerminalTargets } from '$lib/api/terminals';
import { terminals } from '$lib/stores/terminals.svelte';
import { sidePanel, sessionPanelKey } from '$lib/stores/side-panel.svelte';
import type { CommandBlock } from '$lib/terminal/commands';

const info = (id: string, over: Partial<TerminalInfo> = {}): TerminalInfo => ({
	id, target: { kind: 'worker' }, shell: 'bash', cwd: '/workspace/app', folder_source: 'project', session_id: 's1',
	state: 'live', exit_code: null, created_at: '', exited_at: null, busy: false, cols: 80, rows: 24, attached: 0, ...over,
});
const targets: TerminalTargets = {
	worker: { id: 'w', name: 'Unraid', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
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
	api.paste.mockImplementation(async (id: string) => ({ mode: 'bracketed', terminal: terminals.terminal(id) ?? info(id) }));
});
afterEach(() => {
	cleanup();
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
