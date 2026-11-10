// A page-side terminal backend for journeys. The dev instance refuses
// terminals to the preview cookie by design (require_terminal_owner), so a
// journey that needs open shells answers the terminal REST routes and sockets
// inside the page instead. Everything else still goes to the dev instance.
//
// What it fakes: GET/POST /api/v1/terminals, /terminals/targets, DELETE
// /terminals/<id>, /terminals/monitor (no background commands),
// /terminals/device-links (no linked devices), and the
// terminal socket (/api/v1/ws/terminals/<id>): it attaches, replays, echoes
// input, and treats `sleep` + Enter as a running command until Ctrl+C.
// Monitor sockets are never opened (the fake lists no commands).
//
// Use: `await page.addInitScript(fakeTerminals)` before the page loads, or
// paste `fakeTerminals.toString()` into a page and call it (manual checks).

/** Runs inside the page. Self-contained: no closure over module scope. */
export function fakeTerminals() {
	if (window.__fakeTerminals) return;
	const worker = { id: 'dev-worker', name: 'Dev worker', platform: 'linux' };
	const peer = { kind: 'worker', worker_id: 'peer', name: 'Build box', available: true };
	const state = { terminals: new Map(), sockets: new Map(), next: 0, limit: 8 };
	window.__fakeTerminals = state;

	const info = (t) => ({ ...t });
	const scoped = (sessionId) => [...state.terminals.values()].filter((t) => (t.session_id ?? null) === (sessionId || null));
	const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
	const push = (id, frame) => {
		for (const socket of state.sockets.get(id) ?? []) socket._receive(typeof frame === 'string' ? frame : JSON.stringify(frame));
	};
	const output = (id, text) => {
		for (const socket of state.sockets.get(id) ?? []) socket._receive(new TextEncoder().encode(text).buffer);
	};

	const realFetch = window.fetch.bind(window);
	window.fetch = async (input, init = {}) => {
		const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
		const url = new URL(raw, location.href);
		const match = url.pathname.match(/\/api\/v1\/terminals(\/.*)?$/);
		if (!match) return realFetch(input, init);
		const rest = match[1] ?? '';
		const method = (init.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
		const sessionId = url.searchParams.get('session_id');
		if (rest === '/monitor') return json({ tasks: [] });
		if (rest === '/device-links') return json({ links: [], requests: [] });
		if (rest === '/targets') {
			return json({
				worker, supported: true, unsupported_reason: null,
				targets: [{ kind: 'worker', worker_id: worker.id, name: worker.name, available: true }, peer],
				shells: [{ name: 'bash', default: true }, { name: 'sh', default: false }], default_shell: 'bash',
				default_folder: { path: '/workspace/dev-sandbox', source: 'project' },
				chat_worker_id: null, limit: state.limit, open: state.terminals.size
			});
		}
		if (rest === '' && method === 'GET') {
			const list = scoped(sessionId);
			return json({ terminals: list.map(info), open: state.terminals.size, running: list.filter((t) => t.busy).length, limit: state.limit, worker });
		}
		if (rest === '' && method === 'POST') {
			const body = JSON.parse(String(init.body ?? '{}'));
			const id = `fake-${++state.next}`;
			const terminal = {
				id, target: body.target?.worker_id === peer.worker_id ? { kind: 'worker', worker_id: peer.worker_id } : { kind: 'worker', worker_id: worker.id },
				shell: body.shell || 'bash', cwd: body.cwd || '/workspace/dev-sandbox', folder_source: 'project',
				session_id: body.session_id ?? null, state: 'live', exit_code: null, created_at: new Date().toISOString(),
				exited_at: null, busy: false, cols: body.cols ?? 80, rows: body.rows ?? 24, attached: 0, line: ''
			};
			state.terminals.set(id, terminal);
			return json({ terminal: info(terminal) });
		}
		const one = rest.match(/^\/([^/]+)$/);
		if (one && method === 'DELETE') {
			const id = decodeURIComponent(one[1]);
			state.terminals.delete(id);
			for (const socket of state.sockets.get(id) ?? []) socket._close(4404);
			return json({ closed: true });
		}
		if (one && method === 'GET') {
			const terminal = state.terminals.get(decodeURIComponent(one[1]));
			return terminal ? json({ terminal: info(terminal) }) : json({ detail: 'No such terminal.', code: 'not_found' }, 404);
		}
		return json({ detail: 'Not faked.', code: 'not_faked' }, 501);
	};

	const RealSocket = window.WebSocket;
	class FakeTerminalSocket extends EventTarget {
		static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
		constructor(url, id) {
			super();
			this.url = url; this.id = id; this.readyState = 0; this.binaryType = 'blob';
			this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
			setTimeout(() => {
				const terminal = state.terminals.get(id);
				if (!terminal) return this._close(4404);
				if (!state.sockets.has(id)) state.sockets.set(id, new Set());
				state.sockets.get(id).add(this);
				this.readyState = 1;
				this.onopen?.(new Event('open'));
				this._receive(JSON.stringify({ type: 'attached', terminal: info(terminal) }));
				this._receive(new TextEncoder().encode(`${terminal.shell}$ `).buffer);
				this._receive(JSON.stringify({ type: 'replayed' }));
			}, 30);
		}
		_receive(data) {
			if (this.readyState !== 1) return;
			const event = new MessageEvent('message', { data });
			this.onmessage?.(event);
			this.dispatchEvent(event);
		}
		_close(code) {
			if (this.readyState === 3) return;
			this.readyState = 3;
			state.sockets.get(this.id)?.delete(this);
			const event = new CloseEvent('close', { code, wasClean: code === 1000 });
			this.onclose?.(event);
			this.dispatchEvent(event);
		}
		send(raw) {
			const frame = JSON.parse(raw);
			const terminal = state.terminals.get(this.id);
			if (frame.type !== 'input' || !terminal) return;
			for (const ch of frame.data) {
				if (ch === '\x03') {
					terminal.line = '';
					output(this.id, `^C\r\n${terminal.shell}$ `);
					if (terminal.busy) { terminal.busy = false; push(this.id, { type: 'state', terminal: info(terminal) }); }
				} else if (ch === '\r') {
					const command = terminal.line.trim();
					terminal.line = '';
					output(this.id, '\r\n');
					if (command.startsWith('sleep')) { terminal.busy = true; push(this.id, { type: 'state', terminal: info(terminal) }); }
					else output(this.id, `${command ? `${command}\r\n` : ''}${terminal.shell}$ `);
				} else if (!terminal.busy) {
					terminal.line += ch;
					output(this.id, ch);
				}
			}
		}
		close(code = 1000) { this._close(code); }
	}
	window.WebSocket = new Proxy(RealSocket, {
		construct(target, args) {
			const url = new URL(String(args[0]), location.href);
			const match = url.pathname.match(/\/api\/v1\/ws\/terminals\/(?!monitor\/)([^/]+)$/);
			return match ? new FakeTerminalSocket(url.href, decodeURIComponent(match[1])) : new target(...args);
		}
	});
}
