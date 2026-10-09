// Set up Git dialog with the GitHub repository picker in its clone section.
// `?state=connected` (default) serves 45 repositories with server-side search
// and paging like GET /api/v1/github/repos; `disconnected` and `error` show
// the other two states.
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

const state = new URLSearchParams(location.search).get('state') || 'connected';
const OWNERS = ['hobbycoders', 'acme-org', 'friend'];
const DAY = 86_400_000;
const repos = Array.from({ length: 45 }, (_, i) => {
	const owner = OWNERS[i % OWNERS.length];
	const name = i === 0 ? 'ai-shuffle' : `project-${String(i).padStart(2, '0')}`;
	const full_name = `${owner}/${name}`;
	return {
		full_name,
		name,
		owner,
		private: i % 2 === 0,
		fork: i === 4,
		archived: i === 7,
		description: i % 3 === 0 ? `Description of ${name}` : null,
		clone_url: `https://github.com/${full_name}.git`,
		html_url: `https://github.com/${full_name}`,
		default_branch: 'main',
		updated_at: new Date(Date.now() - (i * i + 1) * DAY / 2).toISOString()
	};
});

// Install BEFORE loading production modules. No request may reach the real API.
window.fetch = async (input) => {
	const url = new URL(String(input), location.href);
	const json = (body: unknown, status = 200) =>
		new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
	if (!url.pathname.endsWith('/github/repos')) return json({});
	const page = Number(url.searchParams.get('page') || 1);
	const perPage = Number(url.searchParams.get('per_page') || 30);
	if (state === 'error')
		return json({ detail: 'GitHub could not list your repositories: HTTP 503: Service Unavailable' }, 502);
	if (state === 'disconnected')
		return json({ connected: false, items: [], total: 0, page, per_page: perPage, has_more: false, truncated: false });
	const terms = (url.searchParams.get('q') || '').toLowerCase().split(/\s+/).filter(Boolean);
	const found = repos.filter((r) => terms.every((t) => `${r.full_name} ${r.description ?? ''}`.toLowerCase().includes(t)));
	const items = found.slice((page - 1) * perPage, page * perPage);
	return json({ connected: true, items, total: found.length, page, per_page: perPage, has_more: page * perPage < found.length, truncated: false });
};
localStorage.clear();
sessionStorage.clear();

const { default: InitRepoModal } = await import('../src/lib/components/git/InitRepoModal.svelte');
const noop = () => {};
mount(InitRepoModal, {
	target: document.getElementById('app')!,
	props: { open: true, projectId: 'fixture', sessionId: undefined, projectName: 'Fixture project', onClose: noop, onInitialized: noop }
});
Object.assign(window, { cloneRepoPickerFixture: { ready: true } });
