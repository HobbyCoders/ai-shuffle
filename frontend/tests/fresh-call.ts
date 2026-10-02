import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

// Desktop fixture for #1616: the new-session composer's call button creates a
// conversation, then starts a call on it. Every request is answered here; the
// microphone is always denied, so a call stops before any audio or realtime
// connection. `?fail=1` makes the session create fail; `?device=worker` picks
// another install as the runner.
const params = new URLSearchParams(location.search);
const CREATED = 'fixture-call-session';
const calls: { method: string; path: string; body: unknown }[] = [];
(window as unknown as { fixtureCalls: typeof calls }).fixtureCalls = calls;
window.fetch = async (input, init) => {
	const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname;
	const method = (init?.method || 'GET').toUpperCase();
	calls.push({ method, path, body: typeof init?.body === 'string' ? JSON.parse(init.body) : null });
	let status = 200;
	let body: unknown = [];
	if (path.endsWith('/settings/voice')) body = { voice_mode_enabled: true };
	else if (path.endsWith('/auth/status')) body = { authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (path.includes('computer-use')) body = { available: false, enabled: false, active: false, reason: 'no-display' };
	else if (path.includes('commands')) body = { commands: [] };
	else if (path.includes('models')) body = { models: [] };
	else if (method === 'POST' && path === '/api/v1/sessions') {
		if (params.has('fail')) { status = 400; body = { detail: 'That agent no longer exists — pick another one.' }; }
		else body = { session_id: CREATED, mode: 'code' };
	} else if (method === 'POST' && path === `/api/v1/voice/sessions/${CREATED}/attach`) {
		body = { session_id: CREATED, client_secret: 'fixture-secret', model: 'gpt-realtime' };
	}
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
};
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
Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
	getUserMedia: async () => { throw new DOMException('Fixture denies the microphone', 'NotAllowedError'); },
} });
localStorage.clear();
sessionStorage.clear();

const [{ activeSession }, { auth }, { voiceCallState }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/auth.svelte'),
	import('../src/lib/services/voiceCall'),
	import('./FreshCallFixture.svelte'),
]);
await auth.checkAuth();
activeSession.newChat();
activeSession.setProfile('fixture-agent');
if (params.get('device') === 'worker') activeSession.setDeviceIntent({ id: 'worker-1', name: 'Studio PC' });
Object.assign(window, {
	fixtureSession: () => {
		let state: { sessionId: string | null; error: string | null } = { sessionId: null, error: null };
		activeSession.subscribe(s => { state = { sessionId: s.sessionId, error: s.error }; })();
		return state;
	},
	fixtureCall: () => {
		let state: { sessionId: string | null; status: string } = { sessionId: null, status: '' };
		voiceCallState.subscribe(s => { state = { sessionId: s.sessionId, status: s.status }; })();
		return state;
	},
});
mount(Fixture, { target: document.getElementById('app')! });
