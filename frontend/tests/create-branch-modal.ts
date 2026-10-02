// New-session composer with a project selected: the real ChatInput, so the
// chip row, editor row and controls row stack exactly as on the page. The
// "New branch..." entries in its branch chips open CreateBranchModal.
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
// Install BEFORE loading production modules. No request may reach the real API.
window.fetch = async (input) => {
	const url = String(input);
	let body: unknown = {};
	if (url.includes('/auth/status'))
		body = {
			authenticated: true,
			has_any_credential: true,
			onboarding_complete: true,
			github_authenticated: true
		};
	else if (url.includes('computer-use'))
		body = { available: false, enabled: false, active: false, reason: 'no-display' };
	else if (url.includes('commands')) body = { commands: [] };
	else if (url.includes('profiles') || url.includes('installs')) body = [];
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
const [{ activeSession }, { auth }, { default: ChatInput }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/auth.svelte'),
	import('../src/lib/components/chat/ChatInput.svelte')
]);
await auth.checkAuth();
// `?mode=local` shows the checkout branch chip instead of the worktree one.
const intent = new URLSearchParams(location.search).get('mode') === 'local' ? 'local' : 'worktree';
Object.assign(activeSession.current, {
	sessionId: null,
	mode: 'code',
	project: 'fixture-project',
	worktreeIntent: intent
});
const target = document.getElementById('app')!;
// Centred like the new-session screen, so the editor row sits under the dialog.
target.style.cssText =
	'max-width: 760px; min-height: 100vh; margin: 0 auto; padding: 0 12px; display: flex; flex-direction: column; justify-content: center;';
mount(ChatInput, { target });
Object.assign(window, { createBranchFixture: { ready: true } });
