// Mounts the real ChatView with a seeded transcript so TurnNavigator renders in
// its production layout. Every request is answered locally; nothing reaches an API.
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
window.fetch = async (input) => {
	const url = String(input);
	let body: unknown = {};
	if (url.includes('/auth/status'))
		body = { authenticated: true, has_any_credential: true, onboarding_complete: true };
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
localStorage.clear();
sessionStorage.clear();
const [{ activeSession }, { auth }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/auth.svelte'),
	import('./TurnNavigatorFixture.svelte')
]);
await auth.checkAuth();
const turns = Number(new URLSearchParams(location.search).get('turns') || 10);
const messages = [];
for (let i = 0; i < turns; i++) {
	messages.push({
		id: `u${i}`,
		role: 'user',
		content: `Prompt ${i + 1}: check the timeline\nSecond line of prompt ${i + 1} with more detail.`
	});
	messages.push({
		id: `a${i}`,
		role: 'assistant',
		content:
			`Reply ${i + 1}. ` + 'This paragraph pads the transcript so each turn has height. '.repeat(6)
	});
}
Object.assign(activeSession.current, {
	sessionId: 'fixture',
	mode: 'code',
	liveTurnId: null,
	// A local chat: an unset isRemote reads as remote, and MirrorSyncGate would
	// hold the transcript waiting on a sync-status the fixture never answers.
	isRemote: false,
	isStreaming: false,
	profile: 'fixture-profile',
	project: 'fixture-project',
	messages
});
mount(Fixture, { target: document.getElementById('app')! });
Object.assign(window, { turnFixtureReady: true });
