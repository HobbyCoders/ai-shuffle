/** IDs created by this case's browser context, never by a timestamp sweep.
 * The device socket broadcasts other tabs' creations too, so only an echoed
 * request_id from this context's new-session query establishes ownership. */
export function trackCaseChats(context) {
	const ids = new Set();
	const requests = new Set();
	const pending = [];
	const errors = [];
	const observePage = page => page.on('websocket', socket => {
		socket.on('framesent', ({ payload }) => {
			let frame;
			try { frame = JSON.parse(String(payload)); } catch { return; }
			if (frame.type === 'query' && !frame.session_id && typeof frame.request_id === 'string') {
				requests.add(frame.request_id);
			}
		});
		socket.on('framereceived', ({ payload }) => {
			let frame;
			try { frame = JSON.parse(String(payload)); } catch { return; }
			if (['session_created', 'start'].includes(frame.type) && requests.has(frame.request_id)
				&& typeof frame.session_id === 'string') ids.add(frame.session_id);
		});
	});
	context.on('page', observePage);
	for (const page of context.pages()) observePage(page);
	context.on('response', response => {
		if (response.request().method() !== 'POST' || !response.ok()
			|| !/^\/api\/v1\/sessions(?:\/with-worktree)?$/.test(new URL(response.url()).pathname)) return;
		pending.push(response.json().then(body => {
			if (typeof body.session_id === 'string') ids.add(body.session_id);
		}).catch(error => errors.push(error)));
	});
	return async () => {
		await Promise.all(pending);
		if (errors.length) throw errors[0];
		return [...ids];
	};
}
