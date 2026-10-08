import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
import { installMobileShell } from '../src/shell/register';

/**
 * Browser review fixture for the phone's chat controls (#1816): the
 * worker chip, the docked prompts, the composer with its quick bar and
 * the Chat controls panel, on the mockup's data. Every request is answered
 * here; nothing reaches a backend.
 *
 *   ?theme=vercel   the Vercel preset instead of the default dark theme
 *   ?view=assistant the assistant's own chat
 *   ?turn=idle      no turn running (Runs on moves now instead of queueing)
 *   ?prompt=question|access|none  what waits above the composer
 */
installMobileShell();

const params = new URLSearchParams(location.search);
const assistant = params.get('view') === 'assistant';
const turn = params.get('turn') ?? 'running';
const promptKind = params.get('prompt') ?? 'permission';
const SESSION = assistant ? 'fixture-assistant' : 'fixture-session';
const TITLE = assistant ? 'Assistant' : 'How does user install ai shuffle on linux non docker for desktop app?';
const project = { id: 'ai-shuffle', name: 'ai-shuffle', path: 'D:/Projects/ai-shuffle' };
const branch = 'docs/add-linux-desktop-install-guide';
const now = Date.now();

const installs = [
	{ id: 'desktopworker', device_name: 'DesktopWorker', base_url: 'https://desktopworker.example', platform: 'windows', version: '0.8.24', capabilities: {}, last_seen: new Date().toISOString(), online: true, approved: true, is_self: true },
	{ id: 'serverworker', device_name: 'Unraid', base_url: 'https://serverworker.example', platform: 'linux', version: '0.8.24', capabilities: {}, last_seen: new Date().toISOString(), online: true, approved: true, is_self: false }
];
const row = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
	id, title, profile_id: 'default', project_id: 'ai-shuffle', status: 'active', is_active: true, total_cost_usd: 0,
	total_tokens_in: 0, total_tokens_out: 0, cache_creation_tokens: 0, cache_read_tokens: 0, context_tokens: 84200,
	turn_count: 3, tags: [], parent_session_id: null, fork_point_message_index: null, has_forks: false,
	created_at: new Date(now - 3600e3).toISOString(), updated_at: new Date().toISOString(),
	instance_id: 'desktopworker', device_name: 'DesktopWorker', device_online: true, is_remote: false, controllable: true, mode: 'code', ...extra
});
const sessionRows = [
	row(SESSION, TITLE, { work_started_at: turn === 'idle' ? null : new Date(now - 60e3).toISOString(), has_running_tasks: true }),
	row('mem', 'Fix: memory health signals and running revisions', { work_started_at: new Date(now - 252e3).toISOString() }),
	row('u-tdarr', 'Check Tdarr GPU transcoding', { instance_id: 'serverworker', owner_device_id: 'serverworker', device_name: 'Unraid', is_remote: true, remote_runtime_state: 'working' })
];

const calls: { method: string; path: string; body: unknown }[] = [];
(window as unknown as { fixtureCalls: typeof calls }).fixtureCalls = calls;
window.fetch = async (input, init) => {
	const url = new URL(input instanceof Request ? input.url : String(input), location.href);
	const path = url.pathname;
	const method = (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
	calls.push({ method, path, body: typeof init?.body === 'string' ? JSON.parse(init.body) : null });
	let data: unknown = [];
	if (path.endsWith('/auth/status')) data = { authenticated: true, claude_authenticated: true, claude_token_valid: true, github_authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (path.endsWith('/settings/voice')) data = { voice_mode_enabled: true };
	else if (path === '/api/v1/projects') data = [project];
	else if (path === '/api/v1/sessions/cursor' || path === '/api/v1/sessions/paginated') data = { items: sessionRows, total: sessionRows.length, has_more: false, next_cursor: null };
	else if (path.endsWith(`/sessions/${SESSION}/handoff`) && method === 'GET') data = { busy: { running: turn !== 'idle', tasks: turn === 'idle' ? [] : ['Fix: memory health signals and running revisions', 'pytest -q tests/test_memory_health.py'] }, pending: null };
	else if (path.endsWith(`/sessions/${SESSION}/handoff`) && method === 'POST') data = { queued: true, worker: 'Unraid' };
	else if (path.endsWith('/git/status')) data = { is_git_repo: true, current_branch: branch, is_clean: true, default_branch: 'main' };
	else if (path.endsWith('/git/branches')) data = { branches: [branch, 'main'].map((name) => ({ name, is_current: name === branch, is_remote: false })) };
	else if (path.endsWith('/git/worktrees')) data = { worktrees: [] };
	else if (path.endsWith('/cluster/installs')) data = { installs, self_instance_id: 'desktopworker' };
	else if (path.endsWith('/cluster/projects')) data = { projects: [] };
	else if (path.includes('/cluster/git-state')) data = { installs: [] };
	else if (path.includes('/away')) data = { away: false };
	else if (path.endsWith('/usage-limits') || path.includes('/usage')) data = { available: false, buckets: {}, provider_buckets: {} };
	return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
window.WebSocket = class { constructor() { throw new Error('Fixture forbids backend WebSockets'); } } as unknown as typeof WebSocket;

const { theme } = await import('../../frontend/src/lib/stores/theme.svelte');
theme.setTheme('dark');
if (params.get('theme') === 'vercel') theme.setPreset('vercel');
const { auth } = await import('../../frontend/src/lib/stores/auth.svelte');
await auth.checkAuth();
const { appData } = await import('../../frontend/src/lib/stores/app-data.svelte');
await appData.loadProjects();
await appData.loadSessions();
const { activeSession } = await import('../../frontend/src/lib/stores/active-session');
const { chatAccessSummary } = await import('../../frontend/src/lib/stores/chat-access');
activeSession.newChat();
activeSession.setProject(project.id);
activeSession.setModelOverride('opus');
await activeSession.setEffortLevelOverride('high');
// Seed an open chat the way the store holds one (as the unit tests do).
Object.assign(activeSession.current, {
	sessionId: SESSION,
	title: TITLE,
	mode: 'code',
	worktreeBranch: assistant ? null : branch,
	worktreeId: assistant ? null : 'wt-1',
	isStreaming: turn === 'running',
	messages: [
		{ id: 'm1', type: 'user', content: TITLE },
		{ id: 'm2', type: 'assistant', content: "There's no Linux desktop guide yet. I'll check what the desktop build produces, then write it." }
	],
	activeTasks: assistant ? [] : [
		{ id: 't1', subject: 'Fix: memory health signals and running revisions', status: 'running', kind: 'agent', summary: null, toolUseId: null, startedAt: new Date(now - 252e3).toISOString(), updatedAt: new Date().toISOString() },
		{ id: 't2', subject: 'pytest -q tests/test_memory_health.py', status: 'running', kind: 'shell', summary: null, toolUseId: null, startedAt: new Date(now - 58e3).toISOString(), updatedAt: new Date().toISOString() }
	],
	totalTokensIn: 3100, totalCacheReadTokens: 71400, totalCacheCreationTokens: 6200, totalTokensOut: 3500
});
activeSession.setDraft('');

// What waits on the owner. The permission is the mockup's Bash prompt.
const permissions = promptKind === 'none' || assistant ? [] : [
	{ request_id: 'p1', tool_name: 'Bash', tool_input: { command: 'cd desktop && npm run dist:linux' } }
];
const questions = promptKind === 'question' || promptKind === 'all' ? [{
	request_id: 'q1', tool_use_id: 'tu1',
	questions: [{ question: 'Which install format should the guide lead with?', header: 'Format', multiSelect: false, options: [
		{ label: 'AppImage', description: 'Recommended' }, { label: '.deb package', description: '' }, { label: 'Both, AppImage first', description: '' }
	] }]
}] : [];
if (promptKind === 'access' || promptKind === 'all') {
	chatAccessSummary.set({
		sessionId: SESSION, pending: 2, active: 0, saved: 0, error: false,
		items: [
			{ id: 'a1', kind: 'action', detail: '$ ls /mnt/user/appdata/ai-shuffle' },
			{ id: 'a2', kind: 'folder', detail: 'Compare the Linux paths with Unraid.' }
		],
		open: () => alert('Opens the private-access dialog'),
		decide: async () => true
	});
}

const { default: Harness } = await import('./ChatControlsHarness.svelte');
await import('../src/mobile.css');
mount(Harness, { target: document.getElementById('app')!, props: { permissions, questions, assistant, title: TITLE } });
