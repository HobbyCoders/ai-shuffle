<script lang="ts">
	/**
	 * PullModal - Pull latest changes from remote
	 *
	 * Shows behind count, strategy selection (merge/rebase),
	 * and warns if there are uncommitted changes.
	 */
	import {
		ArrowDownToLine, GitBranch, GitMerge, X, AlertTriangle
	} from 'lucide-svelte';

	type PullStrategy = 'merge' | 'rebase';

	interface Props {
		open: boolean;
		branchName: string;
		commitsBehind: number;
		hasChanges: boolean;
		onClose: () => void;
		onPull: (strategy: PullStrategy) => void;
	}

	let { open, branchName, commitsBehind, hasChanges, onClose, onPull }: Props = $props();

	let strategy = $state<PullStrategy>('rebase');

	const branchDisplay = $derived(
		branchName.length > 30 ? branchName.slice(0, 27) + '...' : branchName
	);

	const commitWord = $derived(commitsBehind === 1 ? 'commit' : 'commits');

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			strategy = 'rebase';
		}
	});

	function handlePull() {
		onClose();
		onPull(strategy);
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

{#if open}
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
				<ArrowDownToLine size={20} strokeWidth={1.5} />
			</div>

			<!-- Title -->
			<h2 class="modal-title">Pull latest changes</h2>

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
					<span class="info-label">Behind</span>
					<span class="info-value">
						{commitsBehind} {commitWord} behind upstream
					</span>
				</div>
			</div>

			<!-- Warning for uncommitted changes -->
			{#if hasChanges}
				<div class="warning-box">
					<AlertTriangle size={15} strokeWidth={1.5} />
					<span>You have uncommitted changes. Commit or stash them before pulling.</span>
				</div>
			{/if}

			<!-- Strategy selection -->
			<div class="field">
				<span class="field-label">Strategy</span>
				<div class="radio-group">
					<label class="radio-option" class:selected={strategy === 'rebase'}>
						<input type="radio" name="pullStrategy" value="rebase" bind:group={strategy} />
						<span class="radio-dot"></span>
						<GitBranch size={16} strokeWidth={1.5} />
						<span class="radio-text">Rebase</span>
						{#if strategy === 'rebase'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>

					<label class="radio-option" class:selected={strategy === 'merge'}>
						<input type="radio" name="pullStrategy" value="merge" bind:group={strategy} />
						<span class="radio-dot"></span>
						<GitMerge size={16} strokeWidth={1.5} />
						<span class="radio-text">Merge</span>
						{#if strategy === 'merge'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>
				</div>
			</div>

			<!-- Action -->
			<button class="continue-btn" onclick={handlePull} disabled={hasChanges}>
				Pull {commitsBehind} {commitWord}
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
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--primary);
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

	/* Warning box */
	.warning-box {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--warning, #f59e0b) 10%, transparent);
		color: var(--warning, #f59e0b);
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.warning-box :global(svg) {
		flex-shrink: 0;
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

	.continue-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.continue-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
