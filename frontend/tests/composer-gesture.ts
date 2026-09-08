import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';

const params = new URLSearchParams(location.search);
const compact = params.get('layout') !== 'centered';
const mode = params.get('mode') === 'code' ? 'code' : 'chat';
const branch = 'feature/mobile-composer-with-a-deliberately-long-branch-name';
const project = { id: 'composer-fixture', name: 'mobile-composer-repository-with-a-deliberately-long-name', path: '/fixture/composer' };

// Install isolated transports before importing the real component/store graph.
// No request reaches a backend, and every navigation starts with the same picks.
localStorage.setItem('ai-shuffle.sessionMode', mode);
window.fetch = async (input) => {
	const path = new URL(input instanceof Request ? input.url : String(input), location.href).pathname;
	let data: unknown = [];
	if (path.endsWith('/auth/status')) data = { authenticated: true, claude_authenticated: true, claude_token_valid: true, github_authenticated: true, has_any_credential: true, onboarding_complete: true };
	else if (path === '/api/v1/projects') data = [project];
	else if (path.endsWith('/git/status')) data = { is_git_repo: true, current_branch: branch, is_clean: true, default_branch: 'main' };
	else if (path.endsWith('/git/branches')) data = { branches: [branch, 'main', 'feature/another-branch'].map(name => ({ name, is_current: name === branch, is_remote: false })) };
	else if (path.endsWith('/git/worktrees')) data = { worktrees: [] };
	else if (path.endsWith('/sync-worktrees')) data = { is_git_repo: true, active_worktrees: 0, cleaned_up: [] };
	else if (path.endsWith('/git/graph')) data = { commits: [], total: 0 };
	else if (path.endsWith('/git/changed-files')) data = { files: [] };
	else if (path.endsWith('/git/pr-state')) data = { branch, lookup_failed: false };
	else if (path.endsWith('/cluster/installs')) data = { installs: [], self_instance_id: 'fixture' };
	else if (path.endsWith('/cluster/projects')) data = { projects: [] };
	return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
window.WebSocket = class { constructor() { throw new Error('Fixture forbids backend WebSockets'); } } as unknown as typeof WebSocket;

const { auth } = await import('../src/lib/stores/auth.svelte');
await auth.checkAuth();
const { activeSession } = await import('../src/lib/stores/active-session');
activeSession.newChat();
if (mode === 'code') {
	const { appData } = await import('../src/lib/stores/app-data.svelte');
	const { git } = await import('../src/lib/stores/git.svelte');
	await appData.loadProjects();
	activeSession.setProject(project.id);
	activeSession.setWorktreeIntent(params.get('worktree') === 'off' ? 'local' : 'worktree');
	activeSession.setWorktreeBaseBranch(branch);
	await git.loadRepository(project.id);
	// Exercise ContextPillBar's active-session row separately from ExecutionModeBar.
	if (params.get('context') === 'active') activeSession.addSystemMessage('Fixture conversation already has a message.');
}
const { default: ComposerGesture } = await import('./ComposerGesture.svelte');
mount(ComposerGesture, { target: document.getElementById('app')!, props: { compact } });
