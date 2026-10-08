import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
import { installMobileShell } from '../src/shell/register';

/**
 * Browser review fixture for the phone's one-bar header: the production
 * AppTopBar with the worker chip, the trail, the chat-actions chevron, the
 * private-access shield and the compact Git button. Every request is answered
 * here; nothing reaches a backend. The git stores are the git-panel stubs
 * (vite.one-bar.config.js).
 *
 *   ?scenario=normal      a project chat, clean branch, nothing shared
 *   ?scenario=git-access  9 changed files and an active access grant
 *   ?scenario=subagent    a subagent's chat (no Git, no private access)
 *   ?scenario=nogit       a chat whose project is not a git repository
 *   ?scenario=settings    a non-chat page
 *   ?unread=1             unread notifications (the dot on the menu)
 *   ?theme=light
 */
installMobileShell();

const params = new URLSearchParams(location.search);
const scenario = params.get('scenario') ?? 'normal';
const chat = scenario !== 'settings';
const subagent = scenario === 'subagent';
const SESSION = subagent ? 'fixture-subagent' : 'fixture-session';
const PARENT = 'fixture-session';
const TITLE = subagent ? 'Review the drawer history entry on Android' : 'Investigate why the Android worker bar flickers when the drawer opens on DeX';
const project = { id: 'ai-shuffle', name: 'ai-shuffle', path: '/workspace/ai-shuffle' };
const now = Date.now();

const installs = [
	{ id: 'unraid', device_name: 'Unraid', base_url: 'https://unraid.example', platform: 'linux', version: '0.8.24', capabilities: {}, last_seen: new Date().toISOString(), online: true, approved: true, is_self: true },
	{ id: 'desktopworker', device_name: 'DesktopWorker', base_url: 'https://desktopworker.example', platform: 'windows', version: '0.8.24', capabilities: {}, last_seen: new Date().toISOString(), online: true, approved: true, is_self: false }
];
const row = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
	id, title, profile_id: 'default', project_id: 'ai-shuffle', status: 'active', is_active: true, total_cost_usd: 0,
	total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 84200,
	turn_count: 3, tags: [], parent_session_id: null, fork_point_message_index: null, has_forks: false, hidden: false,
	created_at: new Date(now - 3600e3).toISOString(), updated_at: new Date().toISOString(),
	instance_id: 'unraid', device_name: 'Unraid', device_online: true, is_remote: false, controllable: true, mode: 'code', ...extra
});
const parentRow = row(PARENT, 'Investigate why the Android worker bar flickers when the drawer opens on DeX');
const childRow = row('fixture-subagent', 'Review the drawer history entry on Android', { hidden: true, parent_session_id: PARENT, is_worker: true });
const sessionRows = [parentRow, row('mem', 'Fix: memory health signals and running revisions')];
const grant = { id: 'g1', label: 'Unraid appdata', kind: 'folder', target: '/mnt/user/appdata/ai-shuffle', expires_at: Math.floor(now / 1000) + 3600, writable: false };

window.fetch = async (input, init) => {
	const url = new URL(input instanceof Request ? input.url : String(input), location.href);
	const path = url.pathname;
	let data: unknown = [];
	if (path.endsWith('/auth/status')) data = { authenticated: true, claude_authenticated: true, claude_token_valid: true, github_authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (path === '/api/v1/projects') data = [project];
	else if (path === '/api/v1/sessions/cursor' || path === '/api/v1/sessions/paginated') data = { items: sessionRows, total: sessionRows.length, has_more: false, next_cursor: null };
	else if (path.endsWith('/live')) data = { session_meta: { is_remote: false, mode: 'code', is_worker: path.includes('fixture-subagent') } };
	else if (path.startsWith('/api/v1/sessions/')) data = path.includes('fixture-subagent') ? childRow : parentRow;
	else if (path.startsWith('/api/v1/chat-access/')) data = { grants: scenario === 'git-access' ? [grant] : [], actions: [], requests: [], saved_connections: [], vault_available: false };
	else if (path.endsWith('/cluster/installs')) data = { installs, self_instance_id: 'unraid' };
	return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
window.WebSocket = class { constructor() { throw new Error('Fixture forbids backend WebSockets'); } } as unknown as typeof WebSocket;

const { theme } = await import('../../frontend/src/lib/stores/theme.svelte');
theme.setTheme(params.get('theme') === 'light' ? 'light' : 'dark');
const { auth } = await import('../../frontend/src/lib/stores/auth.svelte');
await auth.checkAuth();
const { appData } = await import('../../frontend/src/lib/stores/app-data.svelte');
await appData.loadProjects();
await appData.loadSessions();
const { layout } = await import('../../frontend/src/lib/stores/layout.svelte');
const { activeSession } = await import('../../frontend/src/lib/stores/active-session');
if (chat) {
	activeSession.newChat();
	activeSession.setProject(project.id);
	Object.assign(activeSession.current, { sessionId: SESSION, title: TITLE, mode: 'code', messages: [{ id: 'm1', type: 'user', content: TITLE }] });
	layout.openChat(SESSION);
} else {
	layout.openFeaturePage('settings');
}
if (params.get('unread')) {
	const { notify } = await import('../../frontend/src/lib/stores/notifications');
	notify.info('Worker DesktopWorker came online');
}

// Git: the git-panel stubs' snapshot.
const { gitSnapshot } = await import('../../frontend/tests/git-panel-stubs');
const branch = 'fix/android-worker-bar-flicker';
const files = Array.from({ length: 9 }, (_, i) => ({ file: `mobile/src/shell/file-${i}.svelte`, additions: 3 + i, deletions: i % 3, is_binary: false }));
const dirty = scenario === 'git-access';
gitSnapshot.set({
	contextKey: scenario, hydrated: true, stale: false, prState: null, prStateStale: false, githubAvailable: true,
	defaultBranch: 'main', currentBranch: branch,
	status: { is_git_repo: scenario !== 'nogit', current_branch: branch, default_branch: 'main', ahead: 0, differs_from_base: true, unpushed: 0 },
	branches: [{ name: branch, current: true, upstream: 'origin/x', ahead: 0, behind: 0, remote: false }, { name: 'main', current: false, remote: false }],
	diffStats: dirty ? { additions: 120, deletions: 14, files_changed: 9 } : { additions: 0, deletions: 0, files_changed: 0 },
	changedFiles: dirty ? files : [],
	openPr: null
});

const { default: Harness } = await import('./OneBarHarness.svelte');
await import('../src/mobile.css');
mount(Harness, { target: document.getElementById('app')!, props: { sessionId: SESSION, chat, subagent, gitRepo: scenario !== 'nogit' } });
