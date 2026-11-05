<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	import Checkbox from '$lib/components/ui/Checkbox.svelte';
	/**
	 * InitRepoModal - Initialize or clone a git repository for a project
	 *
	 * Offers two modes:
	 * - "init": git init with optional initial commit and GitHub repo creation
	 * - "clone": clone an existing remote repository by URL, or pick one of the
	 *   connected GitHub account's repositories (GitHubRepoPicker fills the URL)
	 */
	import { FolderGit2, X, Github, Lock, Globe, Download } from 'lucide-svelte';
	import { initRepository, cloneRepository } from '$lib/api/git';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { layout } from '$lib/stores/layout.svelte';
	import GitHubRepoPicker from './GitHubRepoPicker.svelte';
	import Segmented from '$lib/components/settings/rows/Segmented.svelte';
	import type { GitHubRepo } from '$lib/api/github';

	interface Props {
		open: boolean;
		projectId: string;
		sessionId?: string;
		projectName?: string;
		onClose: () => void;
		onInitialized?: () => void;
	}

	let { open, projectId, sessionId, projectName = '', onClose, onInitialized }: Props = $props();

	type Mode = 'init' | 'clone';

	let mode = $state<Mode>('init');
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Init mode state
	let createInitialCommit = $state(true);
	let createGithubRepo = $state(false);
	let githubRepoName = $state('');
	let githubPrivate = $state(true);
	let repoNameRef = $state<HTMLInputElement | null>(null);

	// Clone mode state
	let cloneUrl = $state('');
	let cloneUrlRef = $state<HTMLInputElement | null>(null);

	// Derived
	const isCloneMode = $derived(mode === 'clone');
	const canSubmit = $derived(
		isCloneMode
			? cloneUrl.trim().length > 0
			: !createGithubRepo || githubRepoName.trim().length > 0
	);

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			mode = 'init';
			loading = false;
			error = null;
			createInitialCommit = true;
			createGithubRepo = false;
			githubPrivate = true;
			cloneUrl = '';
			// Pre-fill repo name from project name
			githubRepoName = (projectName || '')
				.toLowerCase()
				.replace(/[^a-z0-9-]/g, '-')
				.replace(/-+/g, '-')
				.replace(/^-|-$/g, '');
		}
	});

	// Auto-focus repo name input when GitHub section expands
	$effect(() => {
		if (createGithubRepo && repoNameRef) {
			setTimeout(() => repoNameRef?.focus(), 50);
		}
	});

	// Auto-focus clone URL input when switching to clone mode
	$effect(() => {
		if (isCloneMode && cloneUrlRef) {
			setTimeout(() => cloneUrlRef?.focus(), 50);
		}
	});

	async function handleSubmit() {
		loading = true;
		error = null;

		try {
			if (isCloneMode) {
				await cloneRepository(projectId, {
					clone_url: cloneUrl.trim(),
				}, sessionId);
			} else {
				await initRepository(projectId, {
					create_initial_commit: createInitialCommit,
					create_github_repo: createGithubRepo,
					github_repo_name: createGithubRepo ? githubRepoName.trim() : undefined,
					github_private: githubPrivate,
				}, sessionId);
			}
			onInitialized?.();
			onClose();
		} catch (e: any) {
			error = e?.detail || e?.message || (isCloneMode ? 'Failed to clone repository' : 'Failed to initialize repository');
		} finally {
			loading = false;
		}
	}

	function pickRepo(repo: GitHubRepo) {
		cloneUrl = repo.clone_url;
		error = null;
	}

	function openGitHubSettings() {
		onClose();
		layout.openFeaturePage('settings', 'category=integrations&item=github');
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			onClose();
		}
		// Enter on a focused button (Close, a mode or visibility choice) is that
		// button's own activation, not a submit.
		// Enter in the repository search is a search, not a submit either.
		const onButton = e.target instanceof Element && !!e.target.closest('button, [data-repo-picker]');
		if (e.key === 'Enter' && !onButton && !loading && canSubmit) {
			e.preventDefault();
			handleSubmit();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div class="ui-overlay ui-surface" role="dialog" aria-modal="true" aria-labelledby="init-repo-title" tabindex="-1" use:focusTrap={{ initial: 'input' }}>
		<button class="ui-scrim" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="ui-dialog">
			<div class="ui-dialog-header">
 <div><h2 id="init-repo-title" class="ui-dialog-title">Set up Git</h2>
 <p class="ui-dialog-subtitle">{isCloneMode ? 'Clone an existing repository into this project.' : 'Track changes, create branches, and collaborate.'}</p></div>
 <button class="ui-button ghost icon" onclick={onClose} aria-label="Close"><X size={16} strokeWidth={1.5} /></button>
 </div>
 <div class="ui-dialog-body init-body">
			<!-- Mode toggle -->
			<Segmented label="Repository source" bind:value={mode} disabled={loading} stretch
				options={[{ value: 'init', label: 'New repo', icon: FolderGit2 }, { value: 'clone', label: 'Clone', icon: Download }]} />

			{#if isCloneMode}
				<!-- Clone URL input -->
				<div class="clone-section">
					<div class="ui-field">
						<label class="ui-label" for="clone-url-input">Repository URL</label>
						<input
							id="clone-url-input"
							type="text"
							class="ui-input"
							placeholder="https://github.com/user/repo.git"
							bind:value={cloneUrl}
							bind:this={cloneUrlRef}
							disabled={loading}
						/>
						<span class="field-hint">HTTPS or SSH URL of the repository to clone</span>
					</div>
					<div class="divider" aria-hidden="true"><span>or pick one</span></div>
					<GitHubRepoPicker
						selectedUrl={cloneUrl}
						disabled={loading}
						onSelect={pickRepo}
						onConnect={openGitHubSettings}
					/>
				</div>
			{:else}
				<!-- Initial commit option -->
				<div class="checkbox-row">
					<Checkbox block bind:checked={createInitialCommit} disabled={loading} label="Create initial commit">
					<span class="checkbox-label">Create initial commit</span>
					<span class="checkbox-hint">Stage and commit all existing files</span></Checkbox>
				</div>

				<!-- GitHub repo option -->
				<div class="checkbox-row">
					<Checkbox block bind:checked={createGithubRepo} disabled={loading} label="Create GitHub repository">
					<span class="checkbox-label">
						<Github size={14} strokeWidth={1.5} />
						Create GitHub repository
					</span>
					<span class="checkbox-hint">Create a new repo and set it as remote</span></Checkbox>
				</div>

				<!-- GitHub options (expanded) -->
				{#if createGithubRepo}
					<div class="github-options">
						<div class="ui-field">
							<label class="ui-label" for="repo-name-input">Repository name</label>
							<input
								id="repo-name-input"
								type="text"
								class="ui-input"
								placeholder="my-project"
								bind:value={githubRepoName}
								bind:this={repoNameRef}
								disabled={loading}
							/>
						</div>

						<Segmented label="Repository visibility" value={githubPrivate ? 'private' : 'public'} disabled={loading}
							onchange={(value) => { githubPrivate = value === 'private'; }}
							options={[{ value: 'private', label: 'Private', icon: Lock }, { value: 'public', label: 'Public', icon: Globe }]} />
					</div>
				{/if}
			{/if}

			{#if error}
				<div class="error-msg" role="alert">{error}</div>
			{/if}

			</div>
			<div class="ui-dialog-footer">
			<!-- Action -->
			<button
				class="ui-button primary"
				disabled={loading || !canSubmit}
				onclick={handleSubmit}
			>
				{#if loading}
					{isCloneMode ? 'Cloning...' : 'Initializing...'}
				{:else}
					{isCloneMode ? 'Clone' : 'Initialize'}
				{/if}
			</button>
			</div>
		</div>
	</div>
{/if}

<style>
 .init-body { display: grid; gap: 12px; }

 .clone-section, .github-options { display: grid; gap: 10px; }
 .divider { text-align: center; font-size: 12px; color: var(--muted-foreground); }
 .checkbox-row { padding-block: 8px; border-bottom: 1px solid var(--border); }
 .checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 13px; }
 .checkbox-hint { display: block; font-size: 12px; color: var(--muted-foreground); }
 .field-hint { font-size: 12px; color: var(--muted-foreground); }
 .error-msg { color: var(--destructive); font-size: 13px; }
</style>
