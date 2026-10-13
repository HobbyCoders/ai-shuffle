/**
 * Assistant mode, in a real browser, with no backend.
 *
 * Mounts the production MainPanel (desktop shell) or MobileAppLayout (phone
 * shell, ?shell=mobile) over a transcript built by the real reducer. Every
 * request is answered here; nothing reaches a worker, the hub or a provider.
 *
 * Query: chat=home|ordinary · state=idle|talked · jobs=0|1|5 · ask=1 · shell=desktop|mobile
 */
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

const params = new URLSearchParams(location.search);
const chat = params.get('chat') === 'ordinary' ? 'ordinary' : 'home';
const idle = params.get('state') === 'idle';
const jobs = Number(params.get('jobs') ?? '0');
// ask=1: an approval the assistant asks in words (assistant_confirm); ask=card: one without it.
const ask = params.get('ask') ?? '';
const mobileShell = params.get('shell') === 'mobile';
const sessionId = chat === 'home' ? 'home' : 'ordinary-1';
const now = Date.now();
const minutesAgo = (m: number) => new Date(now - m * 60_000).toISOString();

const requests: string[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
window.fetch = async (input: RequestInfo | URL) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	requests.push(url.pathname);
	if (url.pathname.endsWith('/auth/status')) {
		return json({ authenticated: true, is_admin: true, setup_required: false, has_any_credential: true, claude_authenticated: true });
	}
	return json({}, 404);
};
class LocalSocket {
	static OPEN = 1; static CLOSED = 3; readyState = 3;
	send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

// Globbed, not imported: the phone shell's files are typed by the mobile app,
// so the web type check must not follow them from here.
const phoneShell = import.meta.glob(['../../mobile/src/mobile.css', '../../mobile/src/shell/register.ts', '../../mobile/src/shell/MobileAppLayout.svelte']);
if (mobileShell) {
	await phoneShell['../../mobile/src/mobile.css']();
	const { installMobileShell } = await phoneShell['../../mobile/src/shell/register.ts']() as { installMobileShell: () => void };
	installMobileShell();
}

const [{ activeSession, streamingSessions }, { reduceAll }, { acceptAssistant }, { layout }, { auth }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/session/v2/reducer'),
	import('../src/lib/stores/assistant'),
	import('../src/lib/stores/layout.svelte'),
	import('../src/lib/stores/auth.svelte'),
]);
await auth.checkAuth();

let seq = 0;
const event = (type: string, fields: Record<string, unknown>) => ({
	type, id: `evt-${++seq}`, seq, session_id: sessionId,
	created_at: new Date(now - (60 - seq) * 60_000).toISOString(), ...fields,
});
const transcript = idle ? [] : [
	event('turn.started', { turn_id: 't1', source: 'user' }),
	event('message.user', { turn_id: 't1', content: 'Can you fix the sidebar bug and keep an eye on the release checks?' }),
	event('message.assistant', { turn_id: 't1', kind: 'thinking', content: 'The sidebar bug needs a worker; the checks need a watcher.' }),
	event('tool.use', { turn_id: 't1', tool_use_id: 'tu-1', name: 'Bash', input: { command: 'git log --oneline -5' } }),
	event('tool.result', { turn_id: 't1', tool_use_id: 'tu-1', content_preview: 'a1b2c3 Fix header\nd4e5f6 Bump deps' }),
	event('tool.use', { turn_id: 't1', tool_use_id: 'tu-2', name: 'Read', input: { file_path: 'frontend/src/lib/components/layout/Sidebar.svelte' } }),
	event('tool.result', { turn_id: 't1', tool_use_id: 'tu-2', content_preview: '<script lang="ts">…' }),
	event('subagent.started', { turn_id: 't1', task_id: 'w1', worker_session_id: 'chat-w1', tool_use_id: 'tu-3', description: 'Fixing the sidebar bug', agent_name: 'Frontend Designer' }),
	event('tool.use', { turn_id: 't1', tool_use_id: 'tu-4', name: 'mcp__fluid_bundled__mcp__builtins__StartSession', input: { title: 'Waiting on checks for the release', prompt: 'Watch CI', request_id: 'r1' } }),
	event('tool.result', { turn_id: 't1', tool_use_id: 'tu-4', content_preview: '{"session_id": "chat-s1", "status": "queued"}' }),
	event('message.assistant', { turn_id: 't1', kind: 'text', content: 'On it. One job is fixing the sidebar bug and another is watching the release checks. I will tell you when both are done.' }),
	event('turn.completed', { turn_id: 't1' }),
	// A started chat reports back; the raw notification belongs under Details.
	event('turn.started', { turn_id: 't1b', source: 'unpark' }),
	event('message.user', { turn_id: 't1b', content: [
		'<task-notification>', '<kind>session</kind>', '<task-id>chat-s1</task-id>', '<status>completed</status>',
		'<summary>Release checks</summary>', '<result>All 41 checks passed on main.</result>', '</task-notification>',
	].join(String.fromCharCode(10)) }),
	event('message.assistant', { turn_id: 't1b', kind: 'text', content: 'The release checks all passed.' }),
	event('turn.completed', { turn_id: 't1b' }),
	event('turn.started', { turn_id: 't2', source: 'user' }),
	event('message.user', { turn_id: 't2', content: 'Thanks. What is on my plate today?' }),
	event('message.assistant', { turn_id: 't2', kind: 'text', content: 'Two open pull requests need a review, and the release is waiting on its checks.' }),
	event('turn.completed', { turn_id: 't2' }),
];
const state = reduceAll(transcript as never);

acceptAssistant({
	configured: true, agent_id: 'assistant', home_session_id: 'home', enabled: true, status: 'no_sources', research_sources: 0,
	agent: { id: 'assistant', name: 'Juniper', avatar_id: 'owl' }, assistant_name: 'Juniper', avatar_id: 'owl', avatar_presets: [],
	introduction: { preferred_name: 'Sam' }, proactive_guard_ready: true,
} as never);

const subjects = ['Fixing the sidebar bug', 'Updating the release notes', 'Reviewing pull request 1712', 'Cleaning up old branches'];
const tasks = Array.from({ length: Math.min(jobs, 4) }, (_, i) => ({
	id: `w${i + 1}`, subject: subjects[i], status: 'running', kind: 'agent', summary: null,
	toolUseId: i === 0 ? 'tu-3' : null, startedAt: minutesAgo(6 + i * 7), updatedAt: minutesAgo(1), agent: 'Frontend Designer',
}));
if (jobs >= 5) streamingSessions.set(new Map([['chat-s1', { sessionId: 'chat-s1' }]]) as never);

layout.openChat(sessionId);
const current = activeSession.current as unknown as Record<string, unknown>;
current.sessionId = sessionId;
current.title = chat === 'home' ? 'Juniper' : 'Sidebar fixes';
current.messages = state.messages;
current.isLoadingSession = false;
current.isRestoringSession = false;
current.wsConnected = true;
current.activeTasks = tasks;
// A home with only background work is parked, not answering.
current.isStreaming = !!ask;
const request = { request_id: 'perm-1', session_id: sessionId, tool_name: 'Bash', tool_input: { command: 'git push origin main' }, created_at: minutesAgo(0) };
current.pendingPermissions = ask === '1'
	? [{ ...request, assistant_confirm: { question: 'Do you authorize me to run `git push origin main`? Please answer yes or no.' } }]
	: ask === 'card' ? [request] : [];
activeSession.setDraft('');

const fixture = { requests, mounted: false };
(window as unknown as { assistantChat: typeof fixture }).assistantChat = fixture;
const target = document.getElementById('app')!;
if (mobileShell) {
	const { default: MobileAppLayout } = await phoneShell['../../mobile/src/shell/MobileAppLayout.svelte']() as { default: Parameters<typeof mount>[0] };
	mount(MobileAppLayout, { target, props: { isAdmin: true, onNewSession() {}, onSettledOpenSession() {}, onOpenSession() {}, onOpenFeature() {}, onOpenSpotlight() {} } as never });
} else {
	const { default: MainPanel } = await import('../src/lib/components/layout/MainPanel.svelte');
	mount(MainPanel, { target, props: {} });
}
fixture.mounted = true;
