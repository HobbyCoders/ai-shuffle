import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

// The real Sidebar against a controlled local transport: no request leaves the
// page. `?ui=mobile` renders the phone shell's panel (the drawer content) and
// `?assistant=ready` gives the owner a set-up assistant.
const params = new URLSearchParams(location.search);
const phone = params.get('ui') === 'mobile';
const assistantReady = params.get('assistant') === 'ready';
// `?older=1`: the server also holds older history behind the first page:
// settled chats, a long run of archived ones (Settings owns those), a page and
// a half of chats from a deleted project (the sidebar drops them), and a last
// few settled ones. Exercises the "Load older sessions" shelf.
const olderHistory = params.get('older') === '1';

declare global {
	interface Window {
		fixture: { updateRequests: unknown[]; patches: { id: string; body: unknown }[]; cursorRequests: string[] };
	}
}
window.fixture = { updateRequests: [], patches: [], cursorRequests: [] };

const hour = 3_600_000;
const day = 24 * hour;
const now = Date.now();
const iso = (ago: number) => new Date(now - ago).toISOString();
let rows: Record<string, unknown>[] = [
	['home', 'Assistant home', null, true, 1 * hour, {}],
	['a1', 'Redesign the mobile side panel', 'shuffle', true, 0.1 * hour, { worktree_branch: 'feat/side-panel', pr_state: 'open', pr_number: 1690 }],
	['a2', 'Fix the update check on desktop', 'shuffle', true, 2 * hour, {}],
	['a3', 'Draft the release notes for 0.8.23', 'notes', true, 5 * hour, {}],
	['s1', 'Why does compaction thrash on long turns?', 'shuffle', false, 3 * hour, {}],
	['s2', 'Postgres pool exhaustion repro', 'shuffle', false, 9 * hour, { is_remote: true, instance_id: 'laptop', device_name: 'Laptop' }],
	['s3', 'Rename the build tab', 'shuffle', false, 1.2 * day, {}],
	['s4', 'Weekly grocery list', null, false, 1.5 * day, { mode: 'chat' }],
	['s5', 'Play Console internal track upload', 'notes', false, 3 * day, {}],
	['s6', 'Sketch a settings page with fewer toggles', 'shuffle', false, 8 * day, {}],
].map(([id, title, project, active, ago, extra]) => ({
	id, title, profile_id: 'default', project_id: project,
	project_name: project === 'shuffle' ? 'ai-shuffle' : project === 'notes' ? 'notes' : null,
	status: 'idle', is_active: active, total_cost_usd: 0, total_tokens_in: 0, total_tokens_out: 0,
	turn_count: 3, tags: [], work_ms: Math.round(((ago as number) % 7 + 1) * 97_000), created_at: iso((ago as number) + hour), updated_at: iso(ago as number),
	is_remote: false, mode: 'code', ...(extra as object),
})).filter((row) => assistantReady || row.id !== 'home');
const olderRows: Record<string, unknown>[] = olderHistory
	? [
		...Array.from({ length: 90 }, (_, i) => ({ id: `older${i}`, title: `Older settled chat ${i + 1}`, status: 'idle', project: 'shuffle' })),
		...Array.from({ length: 150 }, (_, i) => ({ id: `ar${i}`, title: `Archived chat ${i + 1}`, status: 'archived', project: 'shuffle' })),
		...Array.from({ length: 150 }, (_, i) => ({ id: `gone${i}`, title: `Deleted project chat ${i + 1}`, status: 'idle', project: 'deleted' })),
		...Array.from({ length: 3 }, (_, i) => ({ id: `oldest${i}`, title: `Oldest settled chat ${i + 1}`, status: 'idle', project: 'notes' })),
	].map(({ id, title, status, project }, i) => ({
		...rows[rows.length - 1], id, title, status, is_active: false,
		project_id: project, project_name: project === 'shuffle' ? 'ai-shuffle' : project,
		created_at: iso(10 * day + i * hour + hour), updated_at: iso(10 * day + i * hour),
	}))
	: [];

/** Keyset paging over the fixture rows, honouring the sidebar's filters. */
function cursorPage(url: URL) {
	window.fixture.cursorRequests.push(url.search);
	let pool = [...rows, ...olderRows];
	if (url.searchParams.get('exclude_archived') === 'true') pool = pool.filter((r) => r.status !== 'archived');
	if (url.searchParams.get('active_only') === 'true') pool = pool.filter((r) => r.is_active);
	if (!olderHistory) return { items: pool, has_more: false, next_cursor: null, next_cursor_id: null };
	const limit = Number(url.searchParams.get('limit') || 50);
	const start = Number(url.searchParams.get('cursor') || 0);
	const items = pool.slice(start, start + limit);
	const hasMore = start + limit < pool.length;
	return {
		items,
		has_more: hasMore,
		next_cursor: hasMore ? String(start + limit) : null,
		next_cursor_id: hasMore ? String(items[items.length - 1].id) : null,
	};
}
const projects = [
	{ id: 'shuffle', name: 'ai-shuffle' },
	{ id: 'notes', name: 'notes' },
];
const assistant = {
	status: assistantReady ? 'ready' : 'not_configured',
	home_session_id: assistantReady ? 'home' : null,
	assistant_name: assistantReady ? 'Juniper' : null,
	avatar_id: 'robot',
};

window.fetch = async (input, init) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	const path = url.pathname.replace(/^\/api\/v1/, '');
	let body: unknown = {};
	let status = 200;
	const active = path.match(/^\/sessions\/([^/]+)\/active$/);
	if (active && init?.method === 'PATCH') {
		const request = JSON.parse(String(init.body));
		window.fixture.patches.push({ id: active[1], body: request });
		const row = rows.find((r) => r.id === active[1])!;
		row.is_active = request.is_active;
		if (request.archived !== undefined) row.status = request.archived ? 'archived' : 'idle';
		body = row;
	} else if (path.startsWith('/sessions/cursor')) body = cursorPage(url);
	else if (path === '/sessions') body = rows;
	else if (path === '/projects') body = projects;
	else if (path === '/assistant') body = assistant;
	else if (path.startsWith('/assistant')) body = [];
	else if (path === '/settings/application-updates/workers') body = { allow_updates: true, update_all_workers: false, workers: [] };
	else if (path === '/settings/application-updates' && init?.method === 'POST') {
		window.fixture.updateRequests.push(JSON.parse(String(init.body)));
		body = { results: [{ device_name: 'This worker', accepted: true, reason: 'Check requested' }] };
	} else if (path.includes('/notifications')) body = { notifications: [], total: 0 };
	else if (path.includes('/preferences')) status = 404;
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
};
class LocalSocket {
	static OPEN = 1; static CLOSED = 3; readyState = 1;
	send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

const { platform } = await import('../src/lib/stores/platform.svelte');
if (phone) platform.set('mobile');
await import('../src/lib/stores/active-session');
const [{ appData }, { acceptAssistant }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/app-data.svelte'),
	import('../src/lib/stores/assistant'),
	import('./SidePanelFixture.svelte'),
]);
Object.assign(appData.current, { sessions: rows.map((r) => ({ ...r })), projects, clusterProjects: [] });
// The older-history mode needs the store's real cursor state, so it loads the
// first page the way the app does on boot.
if (olderHistory) await appData.loadSessions(null);
if (assistantReady) acceptAssistant(assistant as never);
mount(Fixture, { target: document.getElementById('app')!, props: { phone } });
