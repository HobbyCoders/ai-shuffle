<script lang="ts">
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
	<div class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="init-repo-title" tabindex="-1" use:focusTrap={{ initial: 'input' }}>
		<button class="modal-backdrop-close" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="modal">
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon">
				{#if isCloneMode}
					<Download size={20} strokeWidth={1.5} />
				{:else}
					<FolderGit2 size={20} strokeWidth={1.5} />
				{/if}
			</div>

			<!-- Title & description -->
			<h2 id="init-repo-title" class="modal-title">Set up Git</h2>
			<p class="modal-description">
				{#if isCloneMode}
					Clone an existing repository into this project.
				{:else}
					Initialize a git repository for this project to track changes,
					create branches, and collaborate.
				{/if}
			</p>

			<!-- Mode toggle -->
			<div class="mode-toggle">
				<button
					class="mode-btn"
					class:active={mode === 'init'}
					aria-pressed={mode === 'init'}
					onclick={() => mode = 'init'}
					disabled={loading}
				>
					<FolderGit2 size={13} strokeWidth={1.5} />
					New repo
				</button>
				<button
					class="mode-btn"
					class:active={mode === 'clone'}
					aria-pressed={mode === 'clone'}
					onclick={() => mode = 'clone'}
					disabled={loading}
				>
					<Download size={13} strokeWidth={1.5} />
					Clone
				</button>
			</div>

			{#if isCloneMode}
				<!-- Clone URL input -->
				<div class="clone-section">
					<div class="field">
						<label class="field-label" for="clone-url-input">Repository URL</label>
						<input
							id="clone-url-input"
							type="text"
							class="input"
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
				<label class="checkbox-row">
					<input
						type="checkbox"
						bind:checked={createInitialCommit}
						disabled={loading}
					/>
					<span class="checkbox-label">Create initial commit</span>
					<span class="checkbox-hint">Stage and commit all existing files</span>
				</label>

				<!-- GitHub repo option -->
				<label class="checkbox-row">
					<input
						type="checkbox"
						bind:checked={createGithubRepo}
						disabled={loading}
					/>
					<span class="checkbox-label">
						<Github size={14} strokeWidth={1.5} />
						Create GitHub repository
					</span>
					<span class="checkbox-hint">Create a new repo and set it as remote</span>
				</label>

				<!-- GitHub options (expanded) -->
				{#if createGithubRepo}
					<div class="github-options">
						<div class="field">
							<label class="field-label" for="repo-name-input">Repository name</label>
							<input
								id="repo-name-input"
								type="text"
								class="input"
								placeholder="my-project"
								bind:value={githubRepoName}
								bind:this={repoNameRef}
								disabled={loading}
							/>
						</div>

						<div class="visibility-toggle">
							<button
								class="vis-btn"
								class:active={githubPrivate}
								aria-pressed={githubPrivate}
								onclick={() => githubPrivate = true}
								disabled={loading}
							>
								<Lock size={13} strokeWidth={1.5} />
								Private
							</button>
							<button
								class="vis-btn"
								class:active={!githubPrivate}
								aria-pressed={!githubPrivate}
								onclick={() => githubPrivate = false}
								disabled={loading}
							>
								<Globe size={13} strokeWidth={1.5} />
								Public
							</button>
						</div>
					</div>
				{/if}
			{/if}

			{#if error}
				<div class="error-msg" role="alert">{error}</div>
			{/if}

			<!-- Action -->
			<button
				class="continue-btn"
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
{/if}

<style>
	.modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		animation: fade-in 150ms ease;
	}

	.modal-backdrop-close {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(4px);
		border: none;
		cursor: default;
		padding: 0;
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.modal {
		position: relative;
		width: 90%;
		max-width: 420px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 16px;
		padding: 28px;
		box-shadow: var(--shadow-l);
		max-height: calc(100dvh - 32px);
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 12px;
		animation: modal-in 200ms ease;
	}

	@keyframes modal-in {
		from { opacity: 0; transform: scale(0.97) translateY(4px); }
		to { opacity: 1; transform: scale(1) translateY(0); }
	}

	.close-btn {
		position: absolute;
		top: 16px;
		right: 16px;
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.close-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.modal-icon {
		width: 44px;
		height: 44px;
		border-radius: 12px;
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--foreground);
	}

	.modal-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		line-height: 1.2;
	}

	.modal-description {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.5;
	}

	/* Mode toggle (segmented control) */
	.mode-toggle {
		display: flex;
		gap: 2px;
		padding: 3px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
	}

	.mode-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 7px 12px;
		font-size: 0.8125rem;
		font-weight: 500;
		border: 1px solid transparent;
		border-radius: 8px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.mode-btn:hover:not(:disabled):not(.active) {
		color: var(--foreground);
	}

	.mode-btn.active {
		background: var(--card);
		border-color: var(--border);
		color: var(--foreground);
		box-shadow: 0 1px 2px color-mix(in oklch, var(--foreground) 5%, transparent);
	}

	.mode-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Clone section */
	.clone-section {
		display: flex;
		flex-direction: column;
		gap: 10px;
		animation: expand-in 150ms ease;
	}

	.divider {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.divider::before,
	.divider::after {
		content: '';
		flex: 1;
		height: 1px;
		background: var(--border);
	}

	/* Checkbox rows */
	.checkbox-row {
		display: grid;
		grid-template-columns: 18px 1fr;
		grid-template-rows: auto auto;
		gap: 0 10px;
		align-items: center;
		padding: 10px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		cursor: pointer;
		transition: border-color 150ms ease;
	}

	.checkbox-row:hover {
		border-color: var(--ring);
	}

	.checkbox-row input[type="checkbox"] {
		grid-row: 1 / -1;
		width: 16px;
		height: 16px;
		accent-color: var(--primary);
		cursor: pointer;
		margin: 0;
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		line-height: 1.3;
	}

	.checkbox-hint {
		grid-column: 2;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.3;
	}

	/* GitHub options section */
	.github-options {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		animation: expand-in 150ms ease;
	}

	@keyframes expand-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.field-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.field-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.3;
	}

	.input {
		width: 100%;
		padding: 10px 14px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--foreground);
		font-size: 0.875rem;
		font-family: var(--font-mono);
		outline: none;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 15%, transparent);
	}

	.input:disabled {
		opacity: 0.5;
	}

	/* Visibility toggle */
	.visibility-toggle {
		display: flex;
		gap: 6px;
	}

	.vis-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 12px;
		font-size: 0.8125rem;
		font-weight: 500;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.vis-btn:hover:not(:disabled) {
		border-color: var(--foreground);
		color: var(--foreground);
	}

	.vis-btn.active {
		background: var(--foreground);
		border-color: var(--foreground);
		color: var(--background);
	}

	.vis-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.error-msg {
		padding: 8px 12px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: 8px;
		color: var(--destructive);
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.continue-btn {
		width: 100%;
		padding: 10px 20px;
		margin-top: 4px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 10px;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
		background: var(--foreground);
		color: var(--background);
	}

	.continue-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.continue-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}
</style>
