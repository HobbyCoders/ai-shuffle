<script lang="ts">
	/**
	 * CreateBranchModal - Codex-style branch creation modal
	 *
	 * Clean modal with git icon, branch name input, optional prefix tags,
	 * and a "Continue" button.
	 */
	import { GitBranch, X } from 'lucide-svelte';
	import { git } from '$lib/stores/git.svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';

	interface Props {
		open: boolean;
		defaultName?: string;
		/** When true, the modal does NOT create the branch itself — it just
		 *  validates the name and hands it back via `onCreated`. Used by cloud
		 *  mode, where the remote worker creates the branch on send. */
		stageOnly?: boolean;
		onClose: () => void;
		onCreated?: (branchName: string) => void;
	}

	let { open, defaultName = '', stageOnly = false, onClose, onCreated }: Props = $props();

	let branchName = $state('');
	let showPrefix = $state(false);
	let prefix = $state('codex/');
	let loading = $state(false);
	let error = $state<string | null>(null);
	let inputRef = $state<HTMLInputElement | null>(null);

	const PREFIXES = ['codex/', 'feature/', 'fix/', 'hotfix/', 'docs/', 'chore/'];

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			// Sanitize default name for branch format
			const sanitized = (defaultName || '')
				.toLowerCase()
				.replace(/[^a-z0-9-]/g, '-')
				.replace(/-+/g, '-')
				.replace(/^-|-$/g, '')
				.slice(0, 60);
			branchName = '';
			showPrefix = false;
			prefix = 'codex/';
			loading = false;
			error = null;
			// Focus input after render
			setTimeout(() => inputRef?.focus(), 50);
		}
	});

	function togglePrefix() {
		showPrefix = !showPrefix;
		if (showPrefix) {
			// Add prefix if not already there
			if (!PREFIXES.some(p => branchName.startsWith(p))) {
				branchName = prefix + branchName;
			}
		} else {
			// Remove prefix
			for (const p of PREFIXES) {
				if (branchName.startsWith(p)) {
					branchName = branchName.slice(p.length);
					break;
				}
			}
		}
	}

	function selectPrefix(newPrefix: string) {
		// Remove current prefix and apply new one
		let bare = branchName;
		for (const p of PREFIXES) {
			if (bare.startsWith(p)) {
				bare = bare.slice(p.length);
				break;
			}
		}
		prefix = newPrefix;
		branchName = newPrefix + bare;
	}

	async function handleCreate() {
		const name = branchName.trim();
		if (!name) return;

		// Staging mode (cloud): don't create here — hand the name back so the
		// caller can defer creation to the remote worker on send.
		if (stageOnly) {
			onCreated?.(name);
			onClose();
			return;
		}

		loading = true;
		error = null;

		try {
			await git.createBranch(name);
			onCreated?.(name);
			onClose();
		} catch (e: any) {
			error = e?.detail || e?.message || 'Failed to create branch';
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
	}

	function handleInputKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && branchName.trim() && !loading) {
			e.preventDefault();
			handleCreate();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="create-branch-title" tabindex="-1" use:focusTrap={{ initial: 'input' }}>
		<button class="modal-backdrop-close" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="modal">
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon">
				<GitBranch size={20} strokeWidth={1.5} />
			</div>

			<!-- Title & description -->
			<h2 id="create-branch-title" class="modal-title">Create a branch</h2>
			<p class="modal-description">
				This branch will be created from the current HEAD. Once created,
				you can work on isolated changes without affecting the main branch.
			</p>

			<!-- Branch name field -->
			<div class="field">
				<div class="field-header">
					<label class="field-label" for="branch-name-input">Branch name</label>
					<button class="prefix-link" onclick={togglePrefix}>
						{showPrefix ? 'Remove prefix' : 'Set prefix'}
					</button>
				</div>

				{#if showPrefix}
					<div class="prefix-tags">
						{#each PREFIXES as p}
							<button
								class="prefix-tag"
								class:active={prefix === p}
								onclick={() => selectPrefix(p)}
							>
								{p}
							</button>
						{/each}
					</div>
				{/if}

				<input
					id="branch-name-input"
					type="text"
					class="input"
					placeholder="Enter a branch name"
					bind:value={branchName}
					bind:this={inputRef}
					onkeydown={handleInputKeydown}
					disabled={loading}
				/>
			</div>

			{#if error}
				<div class="error-msg">{error}</div>
			{/if}

			<!-- Action -->
			<button
				class="continue-btn"
				disabled={!branchName.trim() || loading}
				onclick={handleCreate}
			>
				{loading ? 'Creating...' : 'Continue'}
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

	.field {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin-top: 4px;
	}

	.field-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.field-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.prefix-link {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: none;
		border: none;
		cursor: pointer;
		padding: 0;
		transition: color 150ms ease;
	}

	.prefix-link:hover {
		color: var(--foreground);
		text-decoration: underline;
	}

	.prefix-tags {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.prefix-tag {
		padding: 3px 10px;
		font-size: 0.6875rem;
		font-weight: 500;
		font-family: var(--font-mono);
		border: 1px solid var(--border);
		border-radius: 100px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.prefix-tag:hover {
		border-color: var(--foreground);
		color: var(--foreground);
	}

	.prefix-tag.active {
		background: var(--foreground);
		border-color: var(--foreground);
		color: var(--background);
	}

	.input {
		width: 100%;
		padding: 10px 14px;
		background: var(--background);
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
