<script lang="ts">
	/**
	 * InitRepoModal - Initialize a git repository for a project
	 *
	 * Offers git init with optional initial commit and GitHub repo creation.
	 */
	import { FolderGit2, X, Github, Lock, Globe } from 'lucide-svelte';
	import { initRepository } from '$lib/api/git';

	interface Props {
		open: boolean;
		projectId: string;
		projectName?: string;
		onClose: () => void;
		onInitialized?: () => void;
	}

	let { open, projectId, projectName = '', onClose, onInitialized }: Props = $props();

	let loading = $state(false);
	let error = $state<string | null>(null);
	let createInitialCommit = $state(true);
	let createGithubRepo = $state(false);
	let githubRepoName = $state('');
	let githubPrivate = $state(true);
	let repoNameRef = $state<HTMLInputElement | null>(null);

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			loading = false;
			error = null;
			createInitialCommit = true;
			createGithubRepo = false;
			githubPrivate = true;
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

	async function handleInit() {
		loading = true;
		error = null;

		try {
			await initRepository(projectId, {
				create_initial_commit: createInitialCommit,
				create_github_repo: createGithubRepo,
				github_repo_name: createGithubRepo ? githubRepoName.trim() : undefined,
				github_private: githubPrivate,
			});
			onInitialized?.();
			onClose();
		} catch (e: any) {
			error = e?.detail || e?.message || 'Failed to initialize repository';
		} finally {
			loading = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			onClose();
		}
		if (e.key === 'Enter' && !loading) {
			if (!createGithubRepo || githubRepoName.trim()) {
				e.preventDefault();
				handleInit();
			}
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div class="modal-backdrop" role="dialog" aria-modal="true">
		<button class="modal-backdrop-close" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="modal">
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon">
				<FolderGit2 size={20} strokeWidth={1.5} />
			</div>

			<!-- Title & description -->
			<h2 class="modal-title">Set up Git</h2>
			<p class="modal-description">
				Initialize a git repository for this project to track changes,
				create branches, and collaborate.
			</p>

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
							onclick={() => githubPrivate = true}
							disabled={loading}
						>
							<Lock size={13} strokeWidth={1.5} />
							Private
						</button>
						<button
							class="vis-btn"
							class:active={!githubPrivate}
							onclick={() => githubPrivate = false}
							disabled={loading}
						>
							<Globe size={13} strokeWidth={1.5} />
							Public
						</button>
					</div>
				</div>
			{/if}

			{#if error}
				<div class="error-msg">{error}</div>
			{/if}

			<!-- Action -->
			<button
				class="continue-btn"
				disabled={loading || (createGithubRepo && !githubRepoName.trim())}
				onclick={handleInit}
			>
				{loading ? 'Initializing...' : 'Initialize'}
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
