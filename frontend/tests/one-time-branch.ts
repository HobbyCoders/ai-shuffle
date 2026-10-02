import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

// No request leaves this fixture: the composer, session store and preference
// store are production code, the transport below is local. The session-create
// POST can be held so a script can act while it is in flight.
type Outcome = 'ok' | 'fail';
const creates: Record<string, unknown>[] = [];
let held: ((outcome: Outcome) => void) | null = null;
let holdNext = false;
const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
window.fetch = async (input, init) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	if (url.pathname.endsWith('/sessions/with-worktree') && init?.method === 'POST') {
		const body = JSON.parse(String(init.body));
		creates.push(body);
		const outcome: Outcome = holdNext ? await new Promise<Outcome>((resolve) => { held = resolve; }) : 'ok';
		holdNext = false; held = null;
		if (outcome === 'fail') return json({ detail: 'worktree add failed (fixture)' }, 500);
		return json({ session_id: `session-${creates.length}`, branch_name: body.branch_name,
			base_branch: body.base_branch, worktree_path: `/w/${creates.length}`, status: 'ok' });
	}
	if (url.pathname.includes('/installs')) return json({ installs: [], self_instance_id: 'local' });
	// The composer restores a remembered branch only for a project in this catalog.
	if (url.pathname.endsWith('/projects')) return json([{ id: 'shuffle-scratch', name: 'shuffle-scratch', path: '/p/shuffle-scratch' }]);
	if (url.pathname.endsWith('/sessions') || url.pathname.endsWith('/profiles')) return json([]);
	return json({});
};
class LocalSocket {
	static OPEN = 1; static CLOSED = 3; readyState = 1;
	send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

// Enter the session/app-data import cycle through active-session, as the app shell does.
const { activeSession } = await import('../src/lib/stores/active-session');
const [{ appData }, { appMode }, { composerPreferences }, { createdBranches }, { default: Fixture }] = await Promise.all([
	import('../src/lib/stores/app-data.svelte'),
	import('../src/lib/stores/sessionMode.svelte'),
	import('../src/lib/stores/composer-preferences'),
	import('./one-time-branch-stubs'),
	import('./OneTimeBranchFixture.svelte'),
]);

await appData.loadProjects();
appMode.set('code', false);
activeSession.setProfile('claude-code');
activeSession.setProject('shuffle-scratch', null);
activeSession.setWorktreeIntent('worktree');

(window as unknown as Record<string, unknown>).oneTimeBranch = {
	holdNextCreate() { holdNext = true; },
	finishCreate(outcome: Outcome) { held?.(outcome); },
	get creates() { return creates; },
	get createdBranches() { return createdBranches; },
	get checkoutClaimed() { return composerPreferences.checkoutClaimed; },
	storedCheckout: () => JSON.parse(localStorage.getItem('ai-shuffle-composer:checkout') ?? 'null'),
	state: () => {
		const s = activeSession.current;
		return { sessionId: s.sessionId, worktreeBranch: s.worktreeBranch, worktreeCreateNewBranch: s.worktreeCreateNewBranch,
			isCreatingWorktree: s.isCreatingWorktree, worktreeError: s.worktreeError };
	},
};

mount(Fixture, { target: document.getElementById('app')! });
