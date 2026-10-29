// A case's unrecorded evidence: what the page did around a step the flight
// recorder does not cover (a case's `setup`, where a first message is typed
// and sent to stage the chat). The runner attaches one trace per case page and
// prints it when the case fails, so a failed setup send shows whether the text
// was in the composer, whether Send was enabled, whether the click sent a
// `query` over the WebSocket (or a request went out), and what the console said.
//
// It records shapes, never content: request method and path (no query
// string), WebSocket frame types with a few flags, console error and warning
// lines, page errors, and the send helper's own notes (composer text length
// and its first characters, which are the journey's own synthetic prompt).

const MAX_EVENTS = 300;
const traces = new WeakMap();

/** Start tracing `page`; returns the trace. Call before the page loads. */
export function attachTrace(page) {
	const started = Date.now();
	const events = [];
	let dropped = 0;
	const push = (line) => {
		events.push(`+${String(Date.now() - started).padStart(6)}ms  ${line}`);
		if (events.length > MAX_EVENTS) {
			events.shift();
			dropped += 1;
		}
	};
	const pathOf = (url) => {
		try {
			return new URL(url).pathname;
		} catch {
			return String(url).split('?')[0];
		}
	};
	// The app's own API only (not Vite's /src/lib/api/*.ts module loads).
	const isApi = (url) => {
		try {
			return new URL(url).pathname.startsWith('/api/v1/');
		} catch {
			return false;
		}
	};
	page.on('console', (message) => {
		const type = message.type();
		if (type !== 'error' && type !== 'warning') return;
		const text = message.text();
		// The browser's own line for an HTTP error repeats a ← line above.
		if (text.startsWith('Failed to load resource:')) return;
		push(`console.${type}: ${text.slice(0, 300)}`);
	});
	page.on('pageerror', (error) => push(`pageerror: ${String(error?.message || error).slice(0, 300)}`));
	page.on('request', (request) => {
		if (!isApi(request.url())) return;
		push(`→ ${request.method()} ${pathOf(request.url())}`);
	});
	page.on('response', (response) => {
		if (!isApi(response.url())) return;
		push(`← ${response.status()} ${response.request().method()} ${pathOf(response.url())}`);
	});
	page.on('requestfailed', (request) => {
		if (!isApi(request.url())) return;
		push(`✗ ${request.method()} ${pathOf(request.url())}: ${request.failure()?.errorText || 'failed'}`);
	});
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame()) push(`* navigated ${pathOf(frame.url())}`);
	});
	page.on('websocket', (socket) => {
		const name = pathOf(socket.url());
		push(`ws open ${name}`);
		const describe = (payload) => {
			if (typeof payload !== 'string') return 'binary';
			try {
				const frame = JSON.parse(payload);
				const flags = [];
				if (frame.type === 'query') {
					flags.push(`session=${frame.session_id ? 'set' : 'none'}`);
					flags.push(`request_id=${frame.request_id ? 'set' : 'none'}`);
					flags.push(`profile=${frame.profile ? 'set' : 'none'}`);
					flags.push(`prompt_chars=${(frame.prompt || '').length}`);
				}
				return [frame.type || frame.event || 'untyped', ...flags].join(' ');
			} catch {
				return `text(${payload.length})`;
			}
		};
		// Frames received are many (every streamed token); keep their types only
		// and fold repeats so the interesting ones stay in the buffer.
		let lastReceived = null;
		let repeats = 0;
		const flushRepeats = () => {
			if (repeats) push(`ws ⇣ … ${repeats} more ${lastReceived}`);
			repeats = 0;
		};
		socket.on('framesent', (frame) => {
			flushRepeats();
			lastReceived = null;
			push(`ws ⇡ ${describe(frame.payload)}`);
		});
		socket.on('framereceived', (frame) => {
			const kind = describe(frame.payload);
			if (kind === lastReceived) {
				repeats += 1;
				return;
			}
			flushRepeats();
			lastReceived = kind;
			push(`ws ⇣ ${kind}`);
		});
		socket.on('close', () => {
			flushRepeats();
			push(`ws close ${name}`);
		});
	});
	const trace = {
		note: push,
		text: () => [dropped ? `(${dropped} earlier events dropped)` : '', ...events].filter(Boolean).join('\n')
	};
	traces.set(page, trace);
	return trace;
}

/** Add a line to `page`'s trace (no-op when the runner attached none). */
export function traceNote(page, line) {
	traces.get(page)?.note(line);
}
