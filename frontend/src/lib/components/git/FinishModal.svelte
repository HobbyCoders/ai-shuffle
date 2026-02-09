<script lang="ts">
	/**
	 * FinishModal - Merge PR and optionally clean up worktree/branch
	 *
	 * Shows merge method selection (squash, merge, rebase), a cleanup toggle
	 * for worktree/branch deletion, and a merge action button.
	 */
	import {
		CheckCircle2, GitBranch, GitMerge, X,
		ToggleLeft, ToggleRight
	} from 'lucide-svelte';

	type MergeMethod = 'squash' | 'merge' | 'rebase';

	interface Props {
		open: boolean;
		pr: { number: number; title: string; url: string } | null;
		branchName: string;
		isWorktreeMode: boolean;
		onClose: () => void;
		onFinish: (mergeMethod: MergeMethod, cleanup: boolean) => void;
	}

	let { open, pr, branchName, isWorktreeMode, onClose, onFinish }: Props = $props();

	let mergeMethod = $state<MergeMethod>('squash');
	let cleanupAfterMerge = $state(true);

	const branchDisplay = $derived(
		branchName.length > 30 ? branchName.slice(0, 27) + '...' : branchName
	);

	const prTitleDisplay = $derived(
		pr ? (pr.title.length > 35 ? pr.title.slice(0, 32) + '...' : pr.title) : ''
	);

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			mergeMethod = 'squash';
			cleanupAfterMerge = true;
		}
	});

	function handleMerge() {
		onClose();
		onFinish(mergeMethod, cleanupAfterMerge);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open && pr}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="modal-backdrop" onclick={onClose}>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div class="modal" onclick={(e) => e.stopPropagation()}>
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon">
				<CheckCircle2 size={20} strokeWidth={1.5} />
			</div>

			<!-- Title -->
			<h2 class="modal-title">Finish and merge</h2>

			<!-- Info rows -->
			<div class="info-rows">
				<div class="info-row">
					<span class="info-label">Branch</span>
					<span class="info-value">
						<GitBranch size={13} strokeWidth={1.5} />
						{branchDisplay}
					</span>
				</div>
				<div class="info-row">
					<span class="info-label">Pull request</span>
					<span class="info-value">
						<a href={pr.url} target="_blank" rel="noopener noreferrer" class="pr-link">
							#{pr.number} {prTitleDisplay}
						</a>
					</span>
				</div>
			</div>

			<!-- Merge method -->
			<div class="field">
				<span class="field-label">Merge method</span>
				<div class="radio-group">
					<label class="radio-option" class:selected={mergeMethod === 'squash'}>
						<input type="radio" name="mergeMethod" value="squash" bind:group={mergeMethod} />
						<span class="radio-dot"></span>
						<GitMerge size={16} strokeWidth={1.5} />
						<span class="radio-text">Squash and merge</span>
						{#if mergeMethod === 'squash'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>

					<label class="radio-option" class:selected={mergeMethod === 'merge'}>
						<input type="radio" name="mergeMethod" value="merge" bind:group={mergeMethod} />
						<span class="radio-dot"></span>
						<GitMerge size={16} strokeWidth={1.5} />
						<span class="radio-text">Create a merge commit</span>
						{#if mergeMethod === 'merge'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>

					<label class="radio-option" class:selected={mergeMethod === 'rebase'}>
						<input type="radio" name="mergeMethod" value="rebase" bind:group={mergeMethod} />
						<span class="radio-dot"></span>
						<GitBranch size={16} strokeWidth={1.5} />
						<span class="radio-text">Rebase and merge</span>
						{#if mergeMethod === 'rebase'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>
				</div>
			</div>

			<!-- Cleanup toggle -->
			<button class="toggle-row" onclick={() => cleanupAfterMerge = !cleanupAfterMerge}>
				{#if cleanupAfterMerge}
					<ToggleRight size={22} />
				{:else}
					<ToggleLeft size={22} />
				{/if}
				<span>
					{isWorktreeMode
						? 'Delete local worktree and branch after merge'
						: 'Delete local branch and checkout default branch after merge'}
				</span>
			</button>

			<!-- Action -->
			<button class="continue-btn" onclick={handleMerge}>
				Merge pull request
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
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(4px);
		animation: fade-in 150ms ease;
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.modal {
		position: relative;
		width: 90%;
		max-width: 440px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 16px;
		padding: 28px;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 14px;
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
		background: color-mix(in oklch, var(--success) 12%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--success);
	}

	.modal-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		line-height: 1.2;
	}

	/* Info rows */
	.info-rows {
		display: flex;
		flex-direction: column;
	}

	.info-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 8px 0;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
	}

	.info-label {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.info-value {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		color: var(--foreground);
		font-weight: 500;
	}

	.info-value :global(svg) {
		flex-shrink: 0;
		opacity: 0.6;
	}

	.pr-link {
		color: var(--primary);
		text-decoration: none;
		font-weight: 500;
	}

	.pr-link:hover {
		text-decoration: underline;
	}

	/* Field */
	.field {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.field-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	/* Radio group */
	.radio-group {
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	.radio-option {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		cursor: pointer;
		transition: all 150ms ease;
		border-radius: 8px;
		position: relative;
	}

	.radio-option:hover {
		background: var(--hover-overlay);
	}

	.radio-option input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.radio-dot {
		display: none;
	}

	.radio-option :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.radio-option.selected :global(svg) {
		color: var(--foreground);
	}

	.radio-text {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		flex: 1;
	}

	.radio-option.selected .radio-text {
		color: var(--foreground);
		font-weight: 500;
	}

	.check-mark {
		font-size: 0.875rem;
		color: var(--foreground);
		margin-left: auto;
	}

	/* Toggle */
	.toggle-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 4px 0;
		background: none;
		border: none;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
	}

	.toggle-row :global(svg) {
		color: var(--muted-foreground);
		transition: color 150ms ease;
	}

	/* Continue button */
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

	.continue-btn:hover {
		opacity: 0.9;
	}
</style>
