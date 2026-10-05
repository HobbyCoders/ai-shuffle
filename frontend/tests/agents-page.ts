// Mounts the real Agents page (BuildPage → ProfileCard) against faked API
// replies, for browser checks of #1825 without a worker.
//
//   ?theme=vercel            Vercel preset (default: the app's default dark)
//   ?mobile=1                the phone shell (platform 'mobile')
//   ?default=on|unset|404    GET /settings/subagent-default (default: on)
//   ?colors=0                the agent API without avatar_color
//   ?empty=1                 built-ins only
//   ?loading=1               /agents never answers
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

const params = new URLSearchParams(location.search);
const withColors = params.get('colors') !== '0';
const defaultMode = params.get('default') ?? 'on';

const day = 86_400_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const agent = (o: Record<string, unknown>) => ({
	is_builtin: false,
	is_modified: false,
	created_at: ago(30 * day),
	updated_at: ago(2 * 3_600_000),
	session_count: 0,
	...(withColors ? { avatar_color: null } : {}),
	...o
});

let agents: any[] = [
	agent({
		id: 'juniper',
		name: 'Juniper',
		description: 'Everyday helper for inbox triage, notes, reminders and quick research.',
		session_count: 142,
		...(withColors ? { avatar_color: 'warning' } : {}),
		config: {
			provider_models: {},
			tool_selection_mode: 'disallow',
			disallowed_tools: ['NotebookEdit'],
			system_prompt: { type: 'custom', content: "You are Juniper, the owner's personal assistant. Keep replies short and plain." }
		}
	}),
	agent({
		id: 'release-captain',
		name: 'Release Captain',
		description: 'Cuts releases: checks CI, tags builds and drafts the changelog.',
		session_count: 23,
		updated_at: ago(day),
		...(withColors ? { avatar_color: 'blue' } : {}),
		config: {
			provider_models: { __native__: 'sonnet' },
			tool_selection_mode: 'allow',
			allowed_tools: ['Read', 'Grep', 'Glob', 'Bash', 'Edit', 'mcp__github__*'],
			agent_invoke_mode: 'custom',
			enabled_agents: ['Explore', 'general-purpose'],
			max_turns: 60,
			cwd: 'D:/Projects/ai-shuffle',
			system_prompt: { type: 'custom', content: 'You cut AI Shuffle releases.\n\n1. Confirm main is green.\n2. Draft the changelog.\n3. Ask before tagging.' }
		}
	}),
	agent({
		id: 'general-purpose',
		name: 'General Purpose',
		is_builtin: true,
		description: 'Coding agent for backend and general implementation work: features, bug fixes, refactors, scripts, tests and tooling.',
		session_count: 311,
		updated_at: ago(12 * day),
		config: { provider_models: { __native__: 'opus' }, tool_selection_mode: 'all', system_prompt: { type: 'custom', content: 'You are a coding subagent for AI Shuffle.' } }
	}),
	agent({
		id: 'Explore',
		name: 'Explore',
		is_builtin: true,
		description: 'Read-only search agent for broad fan-out searches across many files.',
		session_count: 488,
		updated_at: ago(12 * day),
		config: { provider_models: { __native__: 'haiku' }, tool_selection_mode: 'allow', allowed_tools: ['Read', 'Grep', 'Glob', 'Bash'], system_prompt: { type: 'custom', content: 'You are a file search specialist.' } }
	}),
	agent({
		id: 'Plan',
		name: 'Plan',
		is_builtin: true,
		description: 'Software architect agent for designing implementation plans.',
		session_count: 57,
		updated_at: ago(12 * day),
		config: { provider_models: {}, tool_selection_mode: 'allow', allowed_tools: ['Read', 'Grep', 'Glob'], system_prompt: { type: 'custom', content: 'You are a software architect.' } }
	}),
	agent({
		id: 'frontend-designer',
		name: 'Frontend Designer',
		is_builtin: true,
		is_modified: true,
		description: 'Frontend work that renders something: components, pages, layouts, responsive design and animation.',
		session_count: 96,
		updated_at: ago(3 * day),
		config: { provider_models: { __native__: 'opus' }, tool_selection_mode: 'all', system_prompt: { type: 'custom', content: 'You are a Frontend Designer.' } }
	})
];
if (params.get('empty') === '1') agents = agents.filter((a) => a.is_builtin);

const identities = [
	{ id: 'juniper', name: 'Juniper', avatar_id: 'fox' },
	{ id: 'release-captain', name: 'Release Captain', avatar_id: 'bolt' },
	{ id: 'general-purpose', name: 'General Purpose', avatar_id: 'robot' },
	{ id: 'Explore', name: 'Explore', avatar_id: 'owl' },
	{ id: 'Plan', name: 'Plan', avatar_id: 'orbit' },
	{ id: 'frontend-designer', name: 'Frontend Designer', avatar_id: 'spark' }
];

const T = (name: string, description: string, risk: string | null = null, extra = {}) => ({ name, description, risk, ...extra });
const surface = {
	discovered_at: new Date().toISOString(),
	errors: [{ group: 'mcp:postgres', message: 'Did not answer. Try refresh.' }],
	groups: [
		{ id: 'harness', label: 'Built-in tools', kind: 'harness', tools: [T('Read', 'Read a file.', 'read'), T('Write', 'Create or overwrite a file.', 'write'), T('Edit', 'Exact string replacement.', 'write'), T('Bash', 'Run a shell command.', 'exec'), T('Grep', 'Search file contents.', 'read'), T('Glob', 'Find files by pattern.', 'read'), T('WebFetch', 'Fetch a URL.', 'network'), T('Task', 'Hand work to another agent.'), T('NotebookEdit', 'Edit a notebook cell.', 'write')] },
		{ id: 'app', label: 'App tools', kind: 'app', server: 'app', tools: [T('mcp__app__Recall', 'Search long-term memory.', 'read'), T('mcp__app__Remember', 'Save a fact.', 'write'), T('mcp__app__Monitor', 'Run a background command.', 'exec')] },
		{ id: 'ai', label: 'AI tools', kind: 'ai', server: 'ai-tools', tools: [T('mcp__ai-tools__generateImage', 'Make an image.', 'network'), T('mcp__ai-tools__generateVideo', 'Make a video.', 'network', { available: false })] },
		{ id: 'computer_use', label: 'Computer use', kind: 'computer_use', server: 'computer-use', tools: [T('mcp__computer-use__screenshot', 'Capture the screen.', 'read')] },
		{ id: 'mcp:github', label: 'github', kind: 'mcp', server: 'github', tools: [T('mcp__github__list_pull_requests', 'List PRs.', 'network'), T('mcp__github__create_release', 'Publish a release.', 'network')] },
		{ id: 'mcp:postgres', label: 'postgres', kind: 'mcp', server: 'postgres', tools: [] }
	]
};

let subagentDefault: any =
	defaultMode === '404'
		? null
		: {
				model: defaultMode === 'unset' ? null : 'sonnet',
				allowed: ['sonnet', 'haiku']
			};

localStorage.setItem(
	'aihub_groups',
	JSON.stringify({
		projects: { groups: [], assignments: {} },
		profiles: { groups: [{ name: 'Shipping', collapsed: false }, { name: 'Research', collapsed: false }], assignments: { 'release-captain': 'Shipping', 'general-purpose': 'Shipping' } },
		subagents: { groups: [], assignments: {} }
	})
);

const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const realFetch = window.fetch.bind(window);
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
	const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
	if (!url.pathname.startsWith('/api/v1/')) return realFetch(input, init);
	const path = url.pathname.slice('/api/v1'.length);
	const method = (init?.method ?? 'GET').toUpperCase();
	const body = init?.body ? JSON.parse(String(init.body)) : undefined;
	await new Promise((r) => setTimeout(r, 120));
	if (path === '/auth/status') return json({ authenticated: true, is_admin: true, setup_required: false });
	if (path === '/agents' && method === 'GET') {
		if (params.get('loading') === '1') return new Promise<Response>(() => {});
		return json(agents);
	}
	if (path === '/agents' && method === 'POST') {
		agents = [...agents, agent({ ...body, updated_at: new Date().toISOString() })];
		identities.push({ id: body.id, name: body.name, avatar_id: 'robot' });
		return json(body);
	}
	const one = path.match(/^\/agents\/([^/]+)$/);
	if (one && method === 'PUT') {
		agents = agents.map((a) => (a.id === one[1] ? { ...a, ...body, is_modified: a.is_builtin ? true : a.is_modified, updated_at: new Date().toISOString() } : a));
		return json(agents.find((a) => a.id === one[1]));
	}
	if (one && method === 'DELETE') {
		agents = agents.filter((a) => a.id !== one[1]);
		return json({});
	}
	const restore = path.match(/^\/agents\/([^/]+)\/restore$/);
	if (restore) {
		agents = agents.map((a) => (a.id === restore[1] ? { ...a, is_modified: false } : a));
		return json({});
	}
	if (path === '/agents/templates') return json([{ id: 'reviewer', name: 'Code reviewer', description: '', prompt: 'You review code.' }]);
	if (path.startsWith('/tools/surface')) return json(surface);
	if (path === '/settings/subagent-default') {
		if (!subagentDefault) return json({ detail: 'Not Found' }, 404);
		if (method === 'PUT') subagentDefault = { model: body.model, allowed: body.allowed };
		return json(subagentDefault);
	}
	if (path === '/assistant') return json({ agent_id: 'juniper', home_session_id: 'h1', configured: true, enabled: true, status: 'ready', research_sources: 2, agent: identities[0] });
	if (path === '/assistant/agents') return json(identities);
	const identity = path.match(/^\/assistant\/agents\/([^/]+)\/identity$/);
	if (identity) {
		const id = decodeURIComponent(identity[1]);
		const row = identities.find((i) => i.id === id) ?? { id, name: id, avatar_id: 'robot' };
		Object.assign(row, body);
		return json(row);
	}
	return json({ detail: 'Not Found' }, 404);
};

await import('../src/lib/stores/active-session');
const [{ auth }, { platform }, { theme }, { appData }, { default: BuildPage }] = await Promise.all([
	import('../src/lib/stores/auth.svelte'),
	import('../src/lib/stores/platform.svelte'),
	import('../src/lib/stores/theme.svelte'),
	import('../src/lib/stores/app-data.svelte'),
	import('../src/lib/components/layout/BuildPage.svelte')
]);
if (params.get('mobile') === '1') platform.set('mobile');
theme.setTheme('dark');
theme.setPreset(params.get('theme') === 'vercel' ? 'vercel' : null);
await auth.checkAuth().catch(() => {});
void appData.loadProfiles();
mount(BuildPage, { target: document.getElementById('app')!, props: { requestedItemId: params.get('open') } });
