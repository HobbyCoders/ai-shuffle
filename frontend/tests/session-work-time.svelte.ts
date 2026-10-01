import { mount, tick } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

// Install BEFORE loading production modules. No request may reach the real API.
window.fetch = async () =>
	new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
class MockSocket {
	static OPEN = 1;
	static CLOSED = 3;
	readyState = 1;
	send() {}
	close() {}
	addEventListener() {}
	removeEventListener() {}
}
window.WebSocket = MockSocket as unknown as typeof WebSocket;
window.EventSource = MockSocket as unknown as typeof EventSource;
localStorage.clear();

// The session store first, as the app shell loads it: app-data and
// active-session import each other, and only this order initialises both.
await import('../src/lib/stores/active-session');
const { default: Fixture } = await import('./SessionWorkTimeFixture.svelte');
const { createMessageHandlers } = await import('../src/lib/stores/session/ws-message-handlers');
type Deps = import('../src/lib/stores/session/ws-message-handlers').MessageHandlerDeps;
type Status = import('../src/lib/components/layout/session-card-data').SessionCardStatus;
type Session = import('../src/lib/api/client').Session;

const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString();
function row(id: string, title: string, extra: Partial<Session>): Session {
	return {
		id,
		profile_id: 'default',
		project_id: null,
		title,
		status: 'active',
		is_active: true,
		total_cost_usd: 0,
		total_tokens_in: 0,
		total_tokens_out: 0,
		cache_creation_tokens: 0,
		cache_read_tokens: 0,
		context_tokens: 0,
		turn_count: 3,
		tags: [],
		parent_session_id: null,
		fork_point_message_index: null,
		has_forks: false,
		created_at: iso(3 * 3_600_000),
		updated_at: iso(2 * 3_600_000),
		branch: 'claude/session-worktime-1619',
		...extra
	};
}

const props = $state({
	rows: [
		{ session: row('a', 'Cumulative run time on the sidebar card', { work_ms: 754_000 }), status: 'done' as Status },
		{ session: row('b', 'Parked on background agents', { work_ms: 5_400_000 }), status: 'parked' as Status },
		{
			session: row('c', 'Settled yesterday', { is_active: false, work_ms: 42_000 }),
			status: 'idle' as Status,
			compact: true
		}
	]
});

mount(Fixture, { target: document.getElementById('app')!, props });

const handlers = createMessageHandlers({
	getState: () => ({}),
	mergeSessionPatch(sessionId: string, patch: Record<string, unknown>) {
		const target = props.rows.find((r) => r.session.id === sessionId);
		if (!target) return false;
		target.session = { ...target.session, ...patch };
		return true;
	},
	appLoadSessions: () => {},
	appLoadApiUserSessions: () => {}
} as unknown as Deps).handleMessage;

Object.assign(window, {
	workFixture: {
		timer(id: string) {
			return document.querySelector(`[data-row="${id}"] .timer`)?.textContent ?? null;
		},
		title(id: string) {
			return document.querySelector(`[data-row="${id}"] .timer`)?.getAttribute('title') ?? null;
		},
		isLive(id: string) {
			return !!document.querySelector(`[data-row="${id}"] .timer`)?.classList.contains('live');
		},
		/** A prompt blocks the turn: the server banks the run so far and pauses. */
		async prompt(id: string, bankedMs: number) {
			props.rows.find((r) => r.session.id === id)!.status = 'needs_permission';
			handlers({
				type: 'user_session_updated',
				session_id: id,
				updates: { work_ms: bankedMs, work_started_at: null }
			});
			await tick();
		},
		/** The answer resumes the live interval from now. */
		async answer(id: string) {
			handlers({ type: 'user_session_updated', session_id: id, updates: { work_started_at: iso(0) } });
			props.rows.find((r) => r.session.id === id)!.status = 'working';
			await tick();
		},
		/** The server's push when a turn takes the live slot. */
		async start(id: string, msAgo = 0) {
			handlers({ type: 'user_session_updated', session_id: id, updates: { work_started_at: iso(msAgo) } });
			props.rows.find((r) => r.session.id === id)!.status = 'working';
			await tick();
		},
		/** The done frame (status flips) and the banking push, in either order. */
		async finish(id: string, status: Status, bankedMs: number) {
			props.rows.find((r) => r.session.id === id)!.status = status;
			const target = props.rows.find((r) => r.session.id === id)!;
			target.session = { ...target.session, updated_at: new Date().toISOString() };
			handlers({
				type: 'user_session_updated',
				session_id: id,
				updates: { work_ms: bankedMs, work_started_at: null }
			});
			await tick();
		}
	}
});
