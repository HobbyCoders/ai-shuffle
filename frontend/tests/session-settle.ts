import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

// No request leaves this fixture. `window.settleReply` decides how the
// PATCH /sessions/{id}/active endpoint answers; the rest are empty successes.
declare global {
	interface Window {
		settleReply: { status: number; detail?: string };
	}
}
window.settleReply = { status: 200 };
const row = {
	id: 'fixture-session', profile_id: 'default', project_id: null, title: 'Fixture conversation',
	status: 'idle', is_active: true, total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0,
	turn_count: 1, tags: [], created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
};
window.fetch = async (input, init) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	let body: unknown = {};
	let status = 200;
	if (url.pathname.endsWith('/sessions/fixture-session/active') && init?.method === 'PATCH') {
		status = window.settleReply.status;
		const wanted = JSON.parse(String(init.body)).is_active;
		if (status < 400) row.is_active = wanted;
		body = status < 400 ? row : { detail: window.settleReply.detail };
	} else if (url.pathname.includes('/sessions/cursor')) body = { items: [row], has_more: false, next_cursor: null };
	else if (url.pathname.includes('/sessions')) body = [row];
	else if (url.pathname.includes('/notifications')) body = { notifications: [], total: 0 };
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
};
class LocalSocket {
	static OPEN = 1; static CLOSED = 3; readyState = 1;
	send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

await import('../src/lib/stores/active-session');
const [{ appData }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/app-data.svelte'), import('./SessionSettleFixture.svelte'),
]);
Object.assign(appData.current, { sessions: [{ ...row }], projects: [], clusterProjects: [] });
mount(Fixture, { target: document.getElementById('app')! });
