// Mounts the real ChatView with a seeded transcript so the user and assistant
// copy actions render in their production layout: a settled multi-segment
// turn (commentary, tool call, final answer) and a reply that is still
// streaming. Every request is answered locally; nothing reaches an API.
// `?platform=mobile` stamps the phone app's platform marker on <html> and
// loads the phone shell's global stylesheet (mobile/src/mobile.css) last, as
// the mobile page does, so the touch styling applies at any width.
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

const params = new URLSearchParams(location.search);
if (params.get('platform') === 'mobile') {
	// As mobile/src/routes/+layout.svelte does before anything renders.
	const { setPlatformForMobileApp } = await import('../src/lib/stores/platform.svelte');
	setPlatformForMobileApp();
	await import('../../mobile/src/mobile.css');
}
if (params.get('theme') === 'light') document.documentElement.classList.remove('dark');
else document.documentElement.classList.add('dark');

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

export const COMMENTARY = 'Checking the **build** config first.';
export const FINAL_ANSWER = [
	'## Fix',
	'',
	'- Pin `vite` to **7.3**',
	'- See [the docs](https://example.test/docs)',
	'',
	'```ts',
	'export const answer = 42;',
	'```'
].join('\n');
const now = new Date().toISOString();
const streaming = params.get('streaming') === '1';
const messages = [
	{ id: 'u1', role: 'user', content: 'Why does the build fail?', timestamp: now },
	{ id: 'a1', role: 'assistant', type: 'text', content: COMMENTARY, timestamp: now },
	{
		id: 't1',
		role: 'assistant',
		type: 'tool_use',
		toolName: 'Read',
		toolId: 'tool-1',
		toolInput: { file_path: 'vite.config.js' },
		toolResult: 'export default {}',
		toolStatus: 'complete',
		timestamp: now
	},
	{
		id: 'a2',
		role: 'assistant',
		type: 'text',
		content: FINAL_ANSWER,
		timestamp: now,
		metadata: { total_cost_usd: 0.0123, duration_ms: 2500, tokens_out: 50 }
	},
	{ id: 'u2', role: 'user', content: 'And the tests?', timestamp: now },
	{
		id: 'a3',
		role: 'assistant',
		type: 'text',
		content: 'Running the suite now, half',
		timestamp: now,
		streaming
	}
];
Object.assign(activeSession.current, {
	sessionId: 'fixture',
	mode: 'code',
	liveTurnId: streaming ? 'u2' : null,
	isStreaming: streaming,
	profile: 'fixture-profile',
	project: 'fixture-project',
	messages
});
mount(Fixture, { target: document.getElementById('app')! });
Object.assign(window, { messageCopyFixtureReady: true, FINAL_ANSWER, COMMENTARY });
