import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

// #1636 fixture: a fresh tab reopens on the New Chat view saved in the layout,
// while the device's server-side snapshot still names an older session. The
// real app bootstrap, session store and composer run; every request and the
// WebSocket are answered here.
//   ?fail=1            the worktree session create fails (500)
//   ?checkout=existing a remembered existing worktree instead of a staged branch
//   ?ws=down           the WebSocket never opens
//   ?hold=sessions     the startup session-list load waits for fixtureRelease()
//   ?hold=projects     the startup project-catalog load waits for fixtureRelease()
const params = new URLSearchParams(location.search);
const OLD = 'old-session';
const CREATED = 'created-session';
const PROJECT = 'shuffle-scratch';
const BRANCH = 'live/1528-explicit';

localStorage.clear();
sessionStorage.clear();
localStorage.setItem('ai-shuffle-layout', JSON.stringify({ activeView: { type: 'session', sessionId: null } }));
localStorage.setItem('ai-shuffle.sessionMode', 'code');
const checkout = params.get('checkout') === 'existing'
	? { kind: 'existing', projectId: PROJECT, projectV2Id: null, worktreeId: 'wt-1', branch: BRANCH }
	: { kind: 'branch', projectId: PROJECT, projectV2Id: null, branch: BRANCH, baseBranch: 'main', createNew: false };
const picks: Record<string, unknown> = {
	profile: 'claude-code', project: { projectId: PROJECT, projectV2Id: null }, worktreeIntent: 'worktree', checkout,
};
for (const [key, value] of Object.entries(picks)) localStorage.setItem(`ai-shuffle-composer:${key}`, JSON.stringify(value));

const requests: { method: string; path: string; body: unknown }[] = [];
const frames: Record<string, unknown>[] = [];
let releaseLoads: () => void = () => {};
const loadsHeld = params.has('hold') ? new Promise<void>(resolve => { releaseLoads = resolve; }) : Promise.resolve();
const hold = async (load: string) => { if (params.get('hold') === load) await loadsHeld; };
const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

window.fetch = async (input, init) => {
	const url = new URL(input instanceof Request ? input.url : String(input), location.href);
	const path = url.pathname;
	const method = (init?.method || 'GET').toUpperCase();
	requests.push({ method, path, body: typeof init?.body === 'string' ? JSON.parse(init.body) : null });
	if (path.endsWith('/auth/status')) return json({ authenticated: true, is_admin: false, has_any_credential: true, onboarding_complete: true });
	if (path.endsWith('/preferences/all')) return json({ preferences: {}, versions: {} });
	if (path.includes('/preferences/active_session')) {
		// The server copy of this device's last open session (a new tab has no
		// per-tab copy): the session the earlier check was working in.
		return json({ key: 'active_session', value: {
			sessionId: OLD, title: 'Earlier work', profile: 'claude-code', project: PROJECT, mode: 'code',
			worktreeIntent: 'worktree', worktreeBranch: BRANCH, worktreeMode: 'new',
		} });
	}
	if (path.endsWith('/cluster/projects')) return json({ projects: [] });
	if (path.endsWith('/projects')) {
		await hold('projects');
		return json([{ id: PROJECT, name: PROJECT, path: `/p/${PROJECT}` }]);
	}
	if (path.endsWith('/agents')) return json([{ id: 'claude-code', name: 'Claude Code', config: {} }]);
	if (path.endsWith('/sessions/paginated')) return json({ sessions: [], total: 0, limit: 100, offset: 0 });
	if (path.endsWith('/tags')) return json([]);
	if (path.endsWith('/sessions/cursor')) {
		await hold('sessions');
		return json({ items: [], next_cursor: null, has_more: false, total: 0 });
	}
	if (path.endsWith('/sessions/with-worktree') && method === 'POST') {
		if (params.has('fail')) return json({ detail: 'Internal server error' }, 500);
		const body = JSON.parse(String(init?.body));
		return json({ session_id: CREATED, branch_name: body.branch_name, base_branch: body.base_branch, worktree_path: '/w/1', status: 'ok' });
	}
	// An empty transcript feed that stays open, so a restored session settles.
	if (path.endsWith('/events')) {
		const stream = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('event: caught_up\ndata: {"seq":0}\n\n')); } });
		return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } });
	}
	return json({});
};

class FixtureSocket {
	static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
	readyState = params.get('ws') === 'down' ? 0 : 1;
	onopen: ((event: unknown) => void) | null = null;
	onclose: ((event: unknown) => void) | null = null;
	onmessage: ((event: unknown) => void) | null = null;
	onerror: ((event: unknown) => void) | null = null;
	constructor(readonly url: string) {
		if (this.readyState === 1) setTimeout(() => this.onopen?.({}), 0);
	}
	send(data: string) { if (this.url.includes('/ws/chat')) frames.push(JSON.parse(data)); }
	close() { this.readyState = 3; }
	addEventListener() {}
	removeEventListener() {}
}
window.WebSocket = FixtureSocket as unknown as typeof WebSocket;
window.EventSource = FixtureSocket as unknown as typeof EventSource;

const [{ activeSession }, { auth }, { notifications }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/auth.svelte'),
	import('../src/lib/stores/notifications'),
	import('./ComposerSendGuardFixture.svelte'),
]);
await auth.checkAuth();
Object.assign(window, {
	fixtureRequests: requests,
	fixtureFrames: frames,
	fixtureRelease: () => releaseLoads(),
	// Composer notices go to the notification center, which this fixture leaves out.
	fixtureNotices: () => notifications.current.history.map(n => n.message),
	fixtureSession: () => {
		const s = activeSession.current;
		return { sessionId: s.sessionId, error: s.error, worktreeError: s.worktreeError, worktreeBranch: s.worktreeBranch,
			worktreeId: s.worktreeId, messages: s.messages.length, isRestoringSession: s.isRestoringSession };
	},
});
mount(Fixture, { target: document.getElementById('app')! });
