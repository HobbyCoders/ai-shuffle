import { mount, tick } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
// Install BEFORE loading production modules. No request may reach the real API.
const requests: string[] = [];
// Server stand-in for the durable hint (app/db/composer_suggestions.py): one
// hint per session, saved only for that session's latest turn, read on demand.
const retained = new Map<string, { session_id: string; turn_id: string; suggestion: string }>();
const latestTurn = new Map<string, string>();
window.fetch = async (input, init) => {
	const url = String(input);
	requests.push(`${init?.method ?? 'GET'} ${url}`);
	let body: unknown = {};
	const hintRead = url.match(/\/sessions\/([^/]+)\/composer-suggestion(?:\?|$)/);
	if (hintRead) {
		// Eligible only while that turn is still the latest and has completed.
		const id = decodeURIComponent(hintRead[1]);
		const hint = retained.get(id);
		const live = activeSession.current.sessionId === id && activeSession.current.isStreaming;
		body = hint && hint.turn_id === latestTurn.get(id) && !live ? hint : null;
	} else if (url.includes('/auth/status'))
		body = { authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (url.includes('computer-use'))
		body = { available: false, enabled: false, active: false, reason: 'no-display' };
	else if (url.includes('commands')) body = { commands: [] };
	else if (url.includes('profiles')) body = [];
	else if (url.includes('models')) body = { models: [] };
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'Content-Type': 'application/json' }
	});
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
if (new URLSearchParams(location.search).has('stt')) {
	(await import('./stt-live-fixture')).installSttFixture();
}
localStorage.clear();
sessionStorage.clear();
const [
	{ activeSession },
	{ chatPreferences },
	{ composerSuggestion },
	{ auth },
	{ default: Fixture }
] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/chatPreferences.svelte'),
	import('../src/lib/stores/session/composer-suggestion'),
	import('../src/lib/stores/auth.svelte'),
	import('./ComposerSuggestionFixture.svelte')
]);
const { createMessageHandlers } = await import('../src/lib/stores/session/ws-message-handlers');
const transport = createMessageHandlers({
	getState: () => activeSession.current
} as import('../src/lib/stores/session/ws-message-handlers').MessageHandlerDeps);
await auth.checkAuth();
Object.assign(activeSession.current, {
	sessionId: 'fixture',
	mode: 'code',
	liveTurnId: null,
	isStreaming: false,
	draft: '',
	profile: 'fixture-profile',
	project: 'fixture-project'
});
let sends = 0;
activeSession.sendMessage = async () => {
	sends++;
	return true;
};
chatPreferences.updatePreference('suggestNextResponse', true);
mount(Fixture, { target: document.getElementById('app')! });
let serial = 0;
const literal = '<img src=x onerror="window.unsafeSuggestion=true"> **Keep this literal**';
Object.assign(window, {
	suggestionFixture: {
		requests,
		literal,
		suggestions: composerSuggestion,
		get sends() {
			return sends;
		},
		get draft() {
			return activeSession.current.draft;
		},
		get session() {
			return activeSession.current.sessionId;
		},
		state(patch: Record<string, unknown>) {
			Object.assign(activeSession.current, patch);
			activeSession.setWorktreeBranch('fixture');
		},
		async start() {
			latestTurn.set(activeSession.current.sessionId!, `turn-${++serial}`);
			this.state({ liveTurnId: `turn-${serial}`, isStreaming: true, draft: '' });
			await tick();
			return `turn-${serial}`;
		},
		async settle() {
			this.state({ liveTurnId: null, isStreaming: false });
			await tick();
		},
		receive(turn = `turn-${serial}`, session = activeSession.current.sessionId!, text = literal) {
			// A late generation for an older turn is not saved, but still notifies.
			if (latestTurn.get(session) === turn)
				retained.set(session, { session_id: session, turn_id: turn, suggestion: text });
			transport.handleMessage({
				type: 'composer_suggestion',
				session_id: session,
				turn_id: turn,
				suggestion: text
			});
		},
		async offer(text = literal) {
			await this.start();
			this.receive(undefined, undefined, text);
			await this.settle();
		},
		async clear() {
			const editor = document.querySelector('[contenteditable="true"]') as HTMLElement;
			editor.focus();
			document.execCommand('selectAll');
			document.execCommand('delete');
			await tick();
		}
	}
});
