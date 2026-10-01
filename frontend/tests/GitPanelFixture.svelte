<script lang="ts">
	// Mounts the production GitHeaderActions against git-panel-stubs.ts.
	// window.gitPanelFixture switches the repository state, the streaming lock
	// and the operation feedback; verify-git-panel.mjs drives it.
	import { get } from 'svelte/store';
	import GitHeaderActions from '../src/lib/components/git/GitHeaderActions.svelte';
	import { gitSnapshot, gitOperationState } from './git-panel-stubs';
	import { chatAccessSummary } from '../src/lib/stores/chat-access';
	import { openHeaderDropdown } from '../src/lib/stores/header-dropdowns';

	let isStreaming = $state(false);
	let operationInProgress = $state(false);
	// A worktree session whose status reads "not a repository" (#1615).
	let isGitRepo = $state(true);
	let worktreeBranch = $state<string | null>(null);
	// argc tells a bare row click (no option bag) from an editor submit.
	const calls: { name: string; argc: number; options?: unknown; open: string | null }[] = [];
	const record = (name: string) => (...args: unknown[]) => {
		calls.push({ name, argc: args.length, options: args[0], open: get(openHeaderDropdown) });
	};

	const branch = 'feature/inline-git-editors-with-a-long-name';
	const files = Array.from({ length: 9 }, (_, i) => ({
		file: `src/lib/components/deeply/nested/path/file-${i}.svelte`,
		additions: 3 + i,
		deletions: i % 3,
		is_binary: false
	}));
	const status = { is_git_repo: true, current_branch: branch, default_branch: 'main', ahead: 2, differs_from_base: true, unpushed: 2 };
	const branches = (ahead: number, behind: number) => [
		{ name: branch, current: true, upstream: 'origin/x', ahead, behind, remote: false },
		{ name: 'main', current: false, remote: false },
		{ name: 'release/1.2', current: false, remote: false }
	];
	/** dirty: uncommitted changes. ahead/behind: relative to upstream. openpr: clean, PR open.
	 *  unreadable: a worktree poll read "not a repository" while the earlier diff
	 *  stays in the store. checking: that reading is a placeholder still loading. */
	const scenarios = {
		dirty: { branches: branches(0, 0), diffStats: { additions: 120, deletions: 14, files_changed: 9 }, changedFiles: files },
		ahead: { branches: branches(2, 0) },
		behind: { branches: branches(0, 3) },
		openpr: { branches: branches(0, 0), openPr: { number: 1565, title: 'Inline editors', url: 'https://example.test/pr/1565', mergeable: true } },
		unreadable: { branches: branches(0, 0), diffStats: { additions: 120, deletions: 14, files_changed: 9 }, changedFiles: files, status: { is_git_repo: false }, currentBranch: null },
		checking: { branches: [], status: { is_git_repo: false }, currentBranch: null, hydrated: false, stale: true }
	};
	type Scenario = keyof typeof scenarios;
	function scenario(name: Scenario) {
		isGitRepo = !('status' in scenarios[name]);
		worktreeBranch = isGitRepo ? null : branch;
		gitSnapshot.set({
			contextKey: name,
			hydrated: true,
			stale: false,
			prState: null,
			prStateStale: false,
			githubAvailable: true,
			defaultBranch: 'main',
			currentBranch: branch,
			status,
			diffStats: null,
			changedFiles: [],
			openPr: null,
			...scenarios[name]
		});
	}

	function startOp(result: 'running' | 'error' = 'running') {
		const failed = result === 'error';
		const error = failed ? 'Push rejected: remote has new commits' : null;
		operationInProgress = !failed;
		gitOperationState.set({
			active: true,
			status: result,
			title: `Shipping ${branch}`,
			error,
			steps: [
				{ id: 'c', label: 'Commit', status: 'done', detail: 'abc1234 Add inline editors' },
				{ id: 'p', label: 'Push', status: failed ? 'error' : 'running', error: error ?? undefined },
				{ id: 'pr', label: 'Create PR', status: 'pending' },
				{ id: 'm', label: 'Merge', status: 'pending' }
			]
		});
	}
	function endOp() {
		operationInProgress = false;
		gitOperationState.set({ active: false, status: 'idle', title: null, steps: [], error: null });
	}

	scenario((new URLSearchParams(location.search).get('s') as Scenario | null) ?? 'dirty');
	chatAccessSummary.set({ sessionId: 's1', pending: 0, active: 2, saved: 1, error: false, open: record('access') });
	Object.assign(window, {
		gitPanelFixture: {
			calls,
			scenario,
			startOp,
			endOp,
			setStreaming: (value: boolean) => (isStreaming = value),
			get open() {
				return get(openHeaderDropdown);
			}
		}
	});
</script>

<header class="fx-header">
	<span class="fx-title">Fixture chat title that is fairly long</span>
	<GitHeaderActions
		sessionId={null}
		{isStreaming}
		{operationInProgress}
		{isGitRepo}
		{worktreeBranch}
		projectName="ai-shuffle"
		onClickCommit={record('commit')}
		onClickPush={record('push')}
		onClickPull={record('pull')}
		onClickCreatePR={record('pr')}
		onClickShipIt={record('shipit')}
		onOpenDiffReview={record('review')}
		onDownloadChat={record('download')}
	/>
</header>
<main class="fx-main">
	<p id="fx-outside">Outside content</p>
	<!-- Stands in for a toast action ("Pull first") that starts an operation from outside the panel. -->
	<button id="fx-toast" type="button" onclick={() => startOp('running')}>Pull first</button>
	{#each Array(30) as _, i (i)}<p>Chat message filler line {i}</p>{/each}
</main>

<style>
	.fx-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--border); background: var(--background); position: sticky; top: 0; z-index: 10; }
	.fx-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	.fx-main { padding: 12px; color: var(--muted-foreground); }
	:global(body) { background: var(--background); color: var(--foreground); margin: 0; }
</style>
